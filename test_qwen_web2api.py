"""Unit tests for qwen-web2api -- single file, standard library only.

Run:
    python -m unittest test_qwen_web2api -v
    # or
    pytest test_qwen_web2api.py -q

No network, no browser, no Qwen token needed: upstream / transport / config
are stubbed or isolated. Keep this file self-contained so it can be pasted
into an AI chat as-is; do not split it unless it grows past ~1500 lines.
"""
import base64
import copy
import http.client
import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock

from qwen_web2api import bridge, models, qwen, server, tools
from qwen_web2api.config import CONFIG, DEFAULT_CONFIG, find_config, load_config


# --------------------------------------------------------------- helpers ----
def _bare_handler(path="/", headers=None):
    """QwenHandler without a socket: enough for the pure helper methods."""
    h = object.__new__(server.QwenHandler)
    h.path = path
    h.headers = dict(headers or {})
    h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h.errors = []
    h.send_api_error = lambda status, message, type_=server.ERR_API, code=None: \
        h.errors.append((status, message, code))
    return h


def _sse_lines(*payloads):
    """Bytes lines shaped like upstream SSE."""
    out = []
    for p in payloads:
        out.append(("data: " + json.dumps(p) + "\n").encode())
        out.append(b"\n")
    return out


def _fake_pending(events):
    """Stand-in for bridge._pending that immediately answers every job."""

    class Pending:
        def __init__(self):
            self.jobs = []

        def put(self, job):
            self.jobs.append(job)
            q = bridge._jobs[job["job_id"]]
            for ev in events:
                q.put(ev)

    return Pending()


class _Sandbox(unittest.TestCase):
    """Snapshot/restore global state so tests never leak into each other."""

    def setUp(self):
        self._config = copy.deepcopy(CONFIG)
        self._qwen = {
            "_cred_cache": qwen._cred_cache,
            "_refresh_last_fail": qwen._refresh_last_fail,
            "_refresh_path": qwen._refresh_path,
            "_api_ver": qwen._api_ver,
            "_models_cache": dict(qwen._models_cache),
        }
        self._bridge = {
            "_last_seen": bridge._last_seen,
            "_jobs": bridge._jobs,
            "_pending": bridge._pending,
        }
        CONFIG["log_requests"] = False
        CONFIG["debug_raw"] = False
        CONFIG["retry_attempts"] = 1
        CONFIG["retry_delay_sec"] = 0
        CONFIG["token"] = None
        CONFIG["cookie"] = None
        CONFIG["token_file"] = None

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self._config)
        for k, v in self._qwen.items():
            if k == "_models_cache":
                qwen._models_cache.clear()
                qwen._models_cache.update(v)
            else:
                setattr(qwen, k, v)
        for k, v in self._bridge.items():
            if k == "_jobs":
                bridge._jobs.clear()
                bridge._jobs.update(v)
            else:
                setattr(bridge, k, v)


# ------------------------------------------------------------------ tools ----
class TestToolsPrompt(_Sandbox):

    def test_basic_layout_and_roles(self):
        p = tools.messages_to_prompt([
            {"role": "system", "content": "be nice"},
            {"role": "developer", "content": "dev rules"},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual(
            p,
            "[System instruction]: be nice\n\n"
            "[System instruction]: dev rules\n\nhi")

    def test_content_parts_are_flattened(self):
        content = [
            {"type": "text", "text": "a"},
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "input_text", "text": "b"},
            "c",
        ]
        self.assertEqual(tools._stringify_content(content), "a\nb\nc")
        self.assertEqual(tools._stringify_content(None), "")
        self.assertEqual(tools._stringify_content(123), "123")

    def test_assistant_tool_calls_absorb_following_results(self):
        messages = [
            {"role": "user", "content": "read x"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file",
                             "arguments": '{"path": "x.py"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "DATA"},
            {"role": "tool", "tool_call_id": "call_1", "content": "DUP"},
        ]
        p = tools.messages_to_prompt(messages)
        self.assertIn("[Assistant]: \n```tool_call", p)
        self.assertIn('"name": "read_file"', p)
        self.assertIn('"arguments": {"path": "x.py"}', p)
        self.assertIn("[Tool result for read_file (id=call_1)]: DATA", p)
        # Every consecutive tool message is absorbed into the assistant turn,
        # so a client-side duplicate shows up twice (upstream behaviour).
        self.assertEqual(p.count("DATA"), 1)
        self.assertEqual(p.count("DUP"), 1)

    def test_assistant_invalid_arguments_become_empty_object(self):
        p = tools.messages_to_prompt([{
            "role": "assistant", "content": "checking",
            "tool_calls": [{"id": "c", "function": {
                "name": "f", "arguments": "not-json"}}]}])
        self.assertIn("[Assistant]: checking", p)
        self.assertIn('"arguments": {}', p)

    def test_assistant_dict_arguments_are_dumped(self):
        p = tools.messages_to_prompt([{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c", "function": {
                "name": "f", "arguments": {"a": 1}}}]}])
        self.assertIn('"arguments": {"a": 1}', p)

    def test_orphan_tool_result_uses_history_name(self):
        p = tools.messages_to_prompt([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_9", "function": {"name": "grep",
                                             "arguments": "{}"}}]},
            {"role": "user", "content": "next"},
            {"role": "tool", "tool_call_id": "call_9", "content": "R"},
        ])
        self.assertIn("[Tool result for grep (id=call_9)]: R", p)

    def test_tool_block_is_pinned_at_head_with_reminder(self):
        tools_def = [{"type": "function", "function": {
            "name": "read_file", "description": "read it",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}}}}}]
        p = tools.messages_to_prompt([{"role": "user", "content": "go"}],
                                     tools_def)
        self.assertTrue(p.startswith("# Tool Use"))
        self.assertIn("Available tools:", p)
        self.assertIn('"name": "read_file"', p)
        self.assertTrue(p.endswith("you have not.]"))

    def test_tool_choice_none_removes_tool_block(self):
        p = tools.messages_to_prompt([{"role": "user", "content": "go"}],
                                     [{"type": "function", "function": {
                                         "name": "f"}}],
                                     tool_choice="none")
        self.assertNotIn("# Tool Use", p)
        self.assertNotIn("[Reminder: you have working tools", p)

    def test_tool_choice_instructions(self):
        base = [{"type": "function", "function": {"name": "f"}}]
        msg = [{"role": "user", "content": "go"}]

        req = tools.messages_to_prompt(msg, base, tool_choice="required")
        self.assertIn("You MUST call at least one tool", req)

        one = tools.messages_to_prompt(
            msg, base, tool_choice={"type": "function",
                                    "function": {"name": "f"}})
        self.assertIn('You MUST call the tool "f"', one)

        bad = tools.messages_to_prompt(msg, base, tool_choice={"name": "f"})
        self.assertIn('You MUST call the tool "f"', bad)

        auto = tools.messages_to_prompt(msg, base, tool_choice="auto")
        self.assertNotIn("You MUST call", auto)

    def test_tool_metadata_helpers(self):
        defs = [
            {"type": "function", "function": {
                "name": "a",
                "parameters": {"properties": {"x": {}, "y": {}},
                               "required": ["x"]}}},
            {"type": "function", "function": {"name": "b"}},
            {"name": "c", "parameters": {"properties": {"z": {}}}},
            {"type": "function"},
            "junk",
        ]
        self.assertEqual(tools.tool_names(defs), {"a", "b", "c"})
        self.assertEqual(tools.tool_parameters(defs),
                         {"a": {"x", "y"}, "b": set(), "c": {"z"}})
        self.assertEqual(tools.tool_required_params(defs), {"a": {"x"}})
        self.assertEqual(tools.tool_names(None), set())
        self.assertEqual(tools.tool_names([]), set())

    def test_missing_required_params(self):
        calls = [{"function": {"name": "a", "arguments": "{}"}},
                 {"function": {"name": "a", "arguments": "not json"}},
                 {"function": {"name": "a", "arguments": "[]"}},
                 {"function": {"name": "unknown", "arguments": "{}"}}]
        self.assertEqual(tools.missing_required_params(calls, {"a": {"x", "y"}}),
                         ["a.x", "a.y"])
        self.assertEqual(tools.missing_required_params(calls, None), [])

    def test_is_required_tool_choice(self):
        self.assertTrue(tools.is_required_tool_choice("required"))
        self.assertTrue(tools.is_required_tool_choice(
            {"function": {"name": "f"}}))
        self.assertTrue(tools.is_required_tool_choice({"name": "f"}))
        self.assertFalse(tools.is_required_tool_choice("auto"))
        self.assertFalse(tools.is_required_tool_choice("none"))
        self.assertFalse(tools.is_required_tool_choice(None))

    def test_truncation_keeps_newest_messages(self):
        CONFIG["prompt_max_bytes"] = 400
        msgs = [{"role": "user", "content": f"message-{i:02d}-" + "x" * 40}
                for i in range(30)]
        p = tools.messages_to_prompt(msgs)
        self.assertIn("[...truncated...]", p)
        self.assertIn("message-29-", p)
        self.assertNotIn("message-00-", p)
        self.assertLessEqual(len(p.encode("utf-8")), 400)

    def test_truncation_keeps_pinned_tool_block(self):
        # Keep the budget large enough that real messages still fit next to
        # the pinned tool block and the trailing reminder.
        CONFIG["prompt_max_bytes"] = 2000
        defs = [{"type": "function", "function": {
            "name": "read_file", "description": "d",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}}}}}]
        msgs = [{"role": "user", "content": f"message-{i:02d}-" + "x" * 40}
                for i in range(30)]
        p = tools.messages_to_prompt(msgs, defs)
        self.assertTrue(p.startswith("# Tool Use"))
        self.assertIn("[...truncated...]", p)
        self.assertIn("message-29-", p)
        self.assertLessEqual(len(p.encode("utf-8")), 2000)


class TestToolsParsing(_Sandbox):

    def test_parses_fenced_call_and_strips_it(self):
        text = ('Let me look.\n```tool_call\n'
                '{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                '```\nDone.')
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"read_file"}, {"read_file": {"path"}})
        self.assertEqual(clean, "Let me look.\n\nDone.")
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["type"], "function")
        self.assertTrue(call["id"].startswith("call_"))
        self.assertEqual(call["function"]["name"], "read_file")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"path": "a.py"})

    def test_mid_sentence_fence_is_not_a_call(self):
        text = ('Use this: ```tool_call\n{"name": "f", "arguments": {}}'
                '\n``` done')
        clean, calls, dropped = tools.parse_tool_calls(text, {"f"}, None)
        self.assertEqual(calls, [])
        self.assertEqual(dropped, [])
        self.assertEqual(clean, text.strip())

    def test_undeclared_tool_is_dropped_and_text_kept(self):
        text = '```tool_call\n{"name": "other", "arguments": {}}\n```'
        clean, calls, dropped = tools.parse_tool_calls(text, {"f"}, None)
        self.assertEqual(calls, [])
        self.assertEqual(dropped, ["undeclared tool 'other'"])
        self.assertEqual(clean, text)

    def test_missing_name_is_dropped(self):
        text = '```tool_call\n{"arguments": {}}\n```'
        _clean, calls, dropped = tools.parse_tool_calls(text, {"f"}, None)
        self.assertEqual(calls, [])
        self.assertEqual(dropped, ["no tool name"])

    def test_invalid_json_body_is_dropped(self):
        text = '```tool_call\n{not json}\n```'
        _clean, calls, dropped = tools.parse_tool_calls(text, None, None)
        self.assertEqual(calls, [])
        self.assertEqual(dropped, ["body is not valid JSON"])

    def test_name_prefix_is_normalized(self):
        text = ('```tool_call\n'
                '{"name": "functions.read_file", "arguments": {"p": 1}}\n'
                '```')
        _clean, calls, dropped = tools.parse_tool_calls(
            text, {"read_file"}, {"read_file": {"p"}})
        self.assertEqual(dropped, [])
        self.assertEqual(calls[0]["function"]["name"], "read_file")

    def test_name_suffix_match_on_dotted_names(self):
        text = '```tool_call\n{"name": "other.read_file", "arguments": {}}\n```'
        _clean, calls, dropped = tools.parse_tool_calls(
            text, {"ns.read_file"}, {"ns.read_file": set()})
        self.assertEqual(dropped, [])
        self.assertEqual(calls[0]["function"]["name"], "ns.read_file")

    def test_json_fence_requires_call_shape(self):
        wrapped = ('```json\n{"name": "f", "arguments": {"a": 1}}\n```')
        _c, calls, _d = tools.parse_tool_calls(wrapped, {"f"}, None)
        self.assertEqual(len(calls), 1)

        flat = '```json\n{"name": "f", "a": 1}\n```'
        _c, calls, _d = tools.parse_tool_calls(flat, {"f"}, {"f": {"a"}})
        self.assertEqual(len(calls), 1)

        _c, calls, _d = tools.parse_tool_calls(flat, {"f"}, None)
        self.assertEqual(calls, [])

        _c, calls, _d = tools.parse_tool_calls(
            '```json\n{"name": "nope", "arguments": {}}\n```', {"f"}, None)
        self.assertEqual(calls, [])

    def test_argument_extraction_variants(self):
        cases = [
            ('{"name": "f", "input": {"q": "x"}}', {"q": "x"}),
            ('{"name": "f", "args": {"q": "x"}}', {"q": "x"}),
            ('{"name": "f", "q": "x"}', {"q": "x"}),
            ('{"name": "f", "parameters": {"q": "x"}}', {"q": "x"}),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                text = "```tool_call\n" + body + "\n```"
                _c, calls, dropped = tools.parse_tool_calls(
                    text, {"f"}, {"f": {"q"}})
                self.assertEqual(dropped, [])
                self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                                 expected)

    def test_string_arguments_are_normalized(self):
        text = ('```tool_call\n'
                '{"name": "f", "arguments": "{\\"q\\": \\"x\\"}"}\n```')
        _c, calls, _d = tools.parse_tool_calls(text, {"f"}, {"f": {"q"}})
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"q": "x"})

    def test_unusable_arguments_degrade_to_empty_object(self):
        text = ('```tool_call\n{"name": "f", "arguments": "not json"}\n```')
        _c, calls, dropped = tools.parse_tool_calls(text, {"f"}, None)
        self.assertEqual(dropped, [])
        self.assertEqual(calls[0]["function"]["arguments"], "{}")

    def test_multiple_blocks_and_fence_inside_string(self):
        text = ('a\n```tool_call\n{"name": "f", "arguments": {"x": "```"}}\n```'
                '\nb\n```tool_call\n{"name": "f", "arguments": {}}\n```\nc')
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"f"}, {"f": set()})
        self.assertEqual(len(calls), 2)
        self.assertEqual(dropped, [])
        self.assertIn("```", calls[0]["function"]["arguments"])
        self.assertEqual(clean, "a\n\nb\n\nc")

    def test_looks_like_tool_call(self):
        self.assertTrue(tools.looks_like_tool_call(
            '```tool_call\n{"name": "f", "arguments": {}}\n```'))
        self.assertTrue(tools.looks_like_tool_call(
            '```json\n{"name": "f", "arguments": {}}\n```'))
        self.assertFalse(tools.looks_like_tool_call(
            '```json\n{"name": "f"}\n```'))
        self.assertFalse(tools.looks_like_tool_call("plain answer"))

    def test_looks_like_missed_tool_call(self):
        self.assertTrue(tools.looks_like_missed_tool_call("Let me read it."))
        self.assertTrue(tools.looks_like_missed_tool_call("I'll check the log"))
        self.assertTrue(tools.looks_like_missed_tool_call("minh se doc file"))
        self.assertFalse(tools.looks_like_missed_tool_call(
            "Let me read it\n```tool_call\n{}\n```"))
        self.assertFalse(tools.looks_like_missed_tool_call(
            "Let me read " + "x" * 1200))
        self.assertFalse(tools.looks_like_missed_tool_call("Xong roi."))

    def test_build_response_format_instruction(self):
        self.assertEqual(tools.build_response_format_instruction(None), "")
        self.assertIn("single valid JSON object",
                      tools.build_response_format_instruction(
                          {"type": "json_object"}))
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        out = tools.build_response_format_instruction(
            {"type": "json_schema", "json_schema": {"schema": schema}})
        self.assertIn("JSON Schema:", out)
        self.assertIn('"a"', out)
        out = tools.build_response_format_instruction(
            {"type": "json_schema", "json_schema": schema})
        self.assertIn('"a"', out)
        self.assertEqual(tools.build_response_format_instruction(
            {"type": "text"}), "")

    def test_strip_code_fence(self):
        self.assertEqual(tools.strip_code_fence('```json\n{"a": 1}\n```'),
                         '{"a": 1}')
        self.assertEqual(tools.strip_code_fence("```\ntext\n```"), "text")
        self.assertEqual(tools.strip_code_fence("plain"), "plain")
        self.assertEqual(tools.strip_code_fence(""), "")


# ----------------------------------------------------------------- models ----
class TestModels(_Sandbox):

    def test_alias_with_thinking(self):
        name, upstream, think, err = models.resolve_model(
            "qwen3.7-plus-thinking")
        self.assertEqual((name, upstream, think, err),
                         ("qwen3.7-plus-thinking", "qwen3.7-plus", True, None))

    def test_alias_without_thinking(self):
        name, upstream, think, err = models.resolve_model("qwen3.8-max")
        self.assertEqual((name, upstream, think, err),
                         ("qwen3.8-max", "qwen3.8-max", False, None))

    def test_unknown_model_passes_through(self):
        self.assertEqual(models.resolve_model("gpt-4o"),
                         ("gpt-4o", "gpt-4o", False, None))
        self.assertEqual(models.resolve_model("gpt-4o", thinking_default=True),
                         ("gpt-4o", "gpt-4o", True, None))

    def test_think_override(self):
        self.assertEqual(models.resolve_model("qwen3.7-plus@think=1")[2], True)
        self.assertEqual(
            models.resolve_model("qwen3.7-plus-thinking@think=0")[2], False)
        self.assertEqual(
            models.resolve_model("qwen3.7-plus@think=off")[2], False)

    def test_invalid_think_level(self):
        self.assertEqual(models.resolve_model("x@think=banana"),
                         (None, None, None, "Invalid think level: banana"))

    def test_empty_and_none_use_default(self):
        self.assertEqual(models.resolve_model("", default="qwen3.8-max"),
                         ("qwen3.8-max", "qwen3.8-max", False, None))
        self.assertEqual(models.resolve_model(None, default="qwen3.8-max"),
                         ("qwen3.8-max", "qwen3.8-max", False, None))


# ------------------------------------------------------------------ qwen ----
class TestQwenCredentials(_Sandbox):

    def test_jwt_exp(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 1700000000}).encode()).decode().rstrip("=")
        self.assertEqual(qwen._jwt_exp(f"h.{payload}.s"), 1700000000)
        self.assertIsNone(qwen._jwt_exp("not-a-jwt"))
        self.assertIsNone(qwen._jwt_exp("a.b.c"))

    def test_cookie_value(self):
        ck = "a=1; refresh_token=xyz; b=2"
        self.assertEqual(qwen._cookie_value(ck, "refresh_token"), "xyz")
        self.assertEqual(qwen._cookie_value("", "refresh_token"), "")
        self.assertEqual(qwen._cookie_value("xrefresh_token=no", "refresh_token"),
                         "")

    def test_extract_token(self):
        self.assertEqual(qwen._extract_token({"token": "a.b.c"}), "a.b.c")
        self.assertEqual(qwen._extract_token({"token": "nodots"}), None)
        self.assertEqual(
            qwen._extract_token({"data": {"result": {"accessToken": "x.y.z"}}}),
            "x.y.z")
        self.assertIsNone(qwen._extract_token("nope"))

    def test_read_credentials_from_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"token": "t1", "cookie": "c1"}, f)
            CONFIG["token_file"] = path
            with mock.patch.dict(os.environ):
                os.environ.pop("QWEN_TOKEN", None)
                self.assertEqual(qwen._read_credentials(), ("t1", "c1"))

    def test_read_credentials_strips_bearer_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("Bearer abc.def.ghi")
            CONFIG["token_file"] = path
            with mock.patch.dict(os.environ):
                os.environ.pop("QWEN_TOKEN", None)
                tok, _cookie = qwen._read_credentials()
            self.assertEqual(tok, "abc.def.ghi")

    def test_persist_credentials_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "token.json")
            CONFIG["token_file"] = path
            qwen._persist_credentials("tok", "ck")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data, {"token": "tok", "cookie": "ck"})
            self.assertEqual(CONFIG["token"], "tok")
            self.assertEqual(CONFIG["cookie"], "ck")


class TestQwenTransport(_Sandbox):

    def test_browser_headers(self):
        CONFIG["extra_headers"] = {"X-Extra": "1"}
        CONFIG["bx_v"] = "bv"
        CONFIG["bx_ua"] = "bu"
        CONFIG["bx_umidtoken"] = "bt"
        h = qwen._browser_headers("tok", "a=1")
        self.assertEqual(h["authorization"], "Bearer tok")
        self.assertEqual(h["cookie"], "a=1; token=tok")
        self.assertEqual(h["bx-v"], "bv")
        self.assertEqual(h["bx-ua"], "bu")
        self.assertEqual(h["bx-umidtoken"], "bt")
        self.assertEqual(h["x-extra"], "1")
        self.assertIn("referer", h)

    def test_browser_headers_keep_existing_token_cookie(self):
        h = qwen._browser_headers("tok", "token=old; b=2", chat_id="cid")
        self.assertEqual(h["cookie"], "token=old; b=2")
        self.assertTrue(h["referer"].endswith("/c/cid"))

    def test_candidates(self):
        CONFIG["api_version"] = 2
        self.assertEqual(qwen._candidates("/v2", "/v1"), ["/v2"])
        CONFIG["api_version"] = 1
        self.assertEqual(qwen._candidates("/v2", "/v1"), ["/v1"])
        CONFIG["api_version"] = 0
        qwen._api_ver = 2
        self.assertEqual(qwen._candidates("/v2", "/v1"), ["/v2"])
        qwen._api_ver = 0
        self.assertEqual(qwen._candidates("/v2", "/v1"), ["/v2", "/v1"])

    def test_model_items(self):
        self.assertEqual(qwen._model_items({"data": [{"id": "a"}]}),
                         [{"id": "a"}])
        self.assertEqual(
            qwen._model_items({"data": {"models": [{"id": "b"}]}}),
            [{"id": "b"}])
        self.assertEqual(
            qwen._model_items({"data": {"list": [{"id": "c"}]}}),
            [{"id": "c"}])
        self.assertEqual(qwen._model_items({"data": "nope"}), [])
        self.assertEqual(qwen._model_items(None), [])

    def test_payload_shape(self):
        CONFIG["chat_mode"] = "normal"
        CONFIG["auto_search"] = True
        p = qwen._payload("hello", "m1", "cid", True, None)
        self.assertTrue(p["stream"])
        self.assertEqual(p["chat_id"], "cid")
        self.assertEqual(p["model"], "m1")
        msg = p["messages"][0]
        self.assertEqual(msg["content"], "hello")
        self.assertEqual(msg["models"], ["m1"])
        self.assertTrue(msg["feature_config"]["thinking_enabled"])
        self.assertTrue(msg["feature_config"]["auto_search"])
        self.assertEqual(len(str(p["timestamp"])), 10)   # seconds

    def test_antibot_error(self):
        e = qwen._antibot_error(qwen._Resp(403, {"bxpunish": "1"}), "")
        self.assertIsNotNone(e)
        self.assertEqual(e.status, 403)
        self.assertIn("bxpunish=1", str(e))
        self.assertIsNone(qwen._antibot_error(
            qwen._Resp(200, {"bxpunish": "0"}), "all good"))
        e = qwen._antibot_error(qwen._Resp(200, {}), "please solve captcha")
        self.assertIsNotNone(e)

    def test_http_error(self):
        e = qwen._http_error(qwen._Resp(401, {}, body=b"denied"))
        self.assertEqual(e.status, 401)
        self.assertIn("token rejected/expired", str(e))
        self.assertIn("denied", str(e))
        e = qwen._http_error(qwen._Resp(500, {}, body=b"boom"))
        self.assertEqual(e.status, 500)
        self.assertNotIn("token rejected", str(e))


class TestQwenStreamParsing(_Sandbox):

    def test_sse_events_skips_noise_and_stops_at_done(self):
        lines = [b": keep-alive\n", b"\n",
                 b'data: {"a": 1}\n', b"data: not-json\n",
                 b'data: {"a": 2}\n', b"data: [DONE]\n",
                 b'data: {"a": 3}\n']
        resp = qwen._Resp(200, {}, lines=iter(lines))
        self.assertEqual(list(qwen._sse_events(resp)), [{"a": 1}, {"a": 2}])

    def test_sse_events_raw_tail_is_capped(self):
        lines = _sse_lines(*[{"i": i} for i in range(25)])
        tail = []
        list(qwen._sse_events(qwen._Resp(200, {}, lines=iter(lines)), tail))
        self.assertEqual(len(tail), 20)

    def test_event_error(self):
        self.assertEqual(qwen._event_error(
            {"success": False, "data": {"details": "bad"}}), "bad")
        self.assertEqual(qwen._event_error({"error": {"message": "x"}}), "x")
        self.assertEqual(qwen._event_error({"error": "s"}), "s")
        self.assertEqual(qwen._event_error({"code": 1, "message": "y"}), "1: y")
        self.assertEqual(qwen._event_error(
            {"choices": [{"delta": {"status": "failed", "message": "boom"}}]}),
            "boom")
        self.assertIsNone(qwen._event_error({"choices": [{"delta": {
            "content": "hi"}}]}))
        self.assertIsNone(qwen._event_error({"code": 0, "message": "OK"}))

    def test_deltas(self):
        obj = {"choices": [{"delta": {
            "content": "hi", "reasoning_content": "think"}}]}
        self.assertEqual(list(qwen._deltas(obj)),
                         [("reasoning", "think"), ("content", "hi")])
        obj = {"choices": [{"delta": {"content": "hm", "phase": "think"}}]}
        self.assertEqual(list(qwen._deltas(obj)), [("reasoning", "hm")])
        obj = {"choices": [{"delta": {"content": [
            {"text": "a"}, {"other": 1}]}}]}
        self.assertEqual(list(qwen._deltas(obj)), [("content", "a")])
        self.assertEqual(list(qwen._deltas({"choices": [None]})), [])

    def test_finished(self):
        self.assertTrue(qwen._finished(
            {"choices": [{"finish_reason": "stop"}]}))
        self.assertTrue(qwen._finished(
            {"choices": [{"delta": {"status": "finished"}}]}))
        self.assertFalse(qwen._finished({"choices": [{"delta": {}}]}))
        self.assertFalse(qwen._finished({}))

    def test_stream_text_incremental_then_cumulative(self):
        t = qwen._StreamText()
        self.assertEqual(t.feed("content", "he"), "he")
        self.assertEqual(t.feed("content", "hello"), "llo")
        self.assertEqual(t.total["content"], "hello")
        self.assertEqual(t.feed("reasoning", "r"), "r")
        self.assertEqual(t.total["reasoning"], "r")

    def test_generate_stream_relays_deltas(self):
        resp = qwen._Resp(200, {"content-type": "text/event-stream"}, lines=iter([
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
            b"data: [DONE]\n",
        ]))
        with mock.patch.object(qwen, "create_chat", return_value=("cid", None)), \
                mock.patch.object(qwen, "_headers", return_value={}), \
                mock.patch.object(qwen, "_request", return_value=resp), \
                mock.patch.object(qwen, "delete_chat"):
            out = list(qwen.generate_stream("hi", "m", False))
        self.assertEqual(out, [("content", "Hel"), ("content", "lo")])

    def test_generate_stream_usage_and_non_sse_json(self):
        body = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }).encode()
        resp = qwen._Resp(200, {"content-type": "application/json"}, body=body)
        usage = {}
        with mock.patch.object(qwen, "create_chat", return_value=("cid", None)), \
                mock.patch.object(qwen, "_headers", return_value={}), \
                mock.patch.object(qwen, "_request", return_value=resp), \
                mock.patch.object(qwen, "delete_chat"):
            out = list(qwen.generate_stream("hi", "m", False, usage))
        self.assertEqual(out, [("content", "hi")])
        self.assertEqual(usage, {"prompt_tokens": 2, "completion_tokens": 1})

    def test_generate_stream_surfaces_event_error(self):
        resp = qwen._Resp(200, {"content-type": "text/event-stream"}, lines=iter([
            b'data: {"success": false, "data": {"details": "quota"}}\n',
        ]))
        with mock.patch.object(qwen, "create_chat", return_value=("cid", None)), \
                mock.patch.object(qwen, "_headers", return_value={}), \
                mock.patch.object(qwen, "_request", return_value=resp), \
                mock.patch.object(qwen, "delete_chat"):
            with self.assertRaises(qwen.QwenError) as ctx:
                list(qwen.generate_stream("hi", "m", False))
        self.assertIn("quota", str(ctx.exception))

    def test_generate_stream_refreshes_once_on_401(self):
        CONFIG["retry_attempts"] = 2
        denied = qwen._Resp(401, {"content-type": "text/plain"}, body=b"no")
        ok = qwen._Resp(200, {"content-type": "text/event-stream"}, lines=iter([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b"data: [DONE]\n",
        ]))
        with mock.patch.object(qwen, "create_chat", return_value=("cid", None)), \
                mock.patch.object(qwen, "_headers", return_value={}), \
                mock.patch.object(qwen, "_request",
                                  side_effect=[denied, ok]) as req, \
                mock.patch.object(qwen, "_read_credentials",
                                  return_value=("t", "c")), \
                mock.patch.object(qwen, "refresh_access_token",
                                  return_value="newtok"), \
                mock.patch.object(qwen, "delete_chat"):
            out = list(qwen.generate_stream("hi", "m", False))
        self.assertEqual(out, [("content", "ok")])
        self.assertEqual(req.call_count, 2)

    def test_generate_aggregates_stream(self):
        chunks = [("content", "a"), ("reasoning", "r"), ("content", "b")]
        with mock.patch.object(qwen, "generate_stream",
                               return_value=iter(chunks)):
            out = qwen.generate("p", "m", False)
        self.assertEqual(out["content"], "ab")
        self.assertEqual(out["reasoning"], "r")
        self.assertEqual(out["usage"], {})


# ---------------------------------------------------------------- bridge ----
class TestBridgeCore(_Sandbox):

    def test_tracker_detects_cumulative_mode(self):
        t = bridge._Tracker()
        self.assertEqual(t.feed("content", "he"), "he")
        self.assertEqual(t.feed("content", "hello"), "llo")
        self.assertEqual(t.total["content"], "hello")
        self.assertEqual(t.feed("content", "hello!"), "!")

    def test_worker_alive_and_status(self):
        bridge._last_seen = time.time()
        self.assertTrue(bridge.worker_alive())
        st = bridge.status()
        self.assertTrue(st["worker"])
        self.assertEqual(st["jobs"], 0)
        bridge._last_seen = 0
        self.assertFalse(bridge.worker_alive())

    def test_no_browser_means_503(self):
        bridge._last_seen = 0
        with self.assertRaises(qwen.QwenError) as ctx:
            list(bridge.generate_stream("hi", "m", False))
        self.assertEqual(ctx.exception.status, 503)

    def test_generate_stream_happy_path(self):
        events = [
            {"kind": "delta", "text": "Hel", "phase": "content"},
            {"kind": "delta", "text": "Hello"},
            {"kind": "usage", "usage": {"prompt_tokens": 3}},
            {"kind": "done"},
        ]
        pending = _fake_pending(events)
        usage = {}
        with mock.patch.object(bridge, "worker_alive", return_value=True), \
                mock.patch.object(bridge, "_pending", pending):
            out = list(bridge.generate_stream(
                "hi", "m", True, usage, file_info={"path": "x"}))
        self.assertEqual(out, [("content", "Hel"), ("content", "lo")])
        self.assertEqual(usage, {"prompt_tokens": 3})
        job = pending.jobs[0]
        self.assertEqual(job["prompt"], "hi")
        self.assertEqual(job["model"], "m")
        self.assertTrue(job["think"])
        self.assertEqual(job["file_info"], {"path": "x"})
        self.assertEqual(bridge._jobs, {})

    def test_native_tool_call_becomes_text_fence(self):
        events = [
            {"kind": "tool_call", "name": "read_file",
             "arguments": '{"path": "a.py"}'},
            {"kind": "done"},
        ]
        pending = _fake_pending(events)
        with mock.patch.object(bridge, "worker_alive", return_value=True), \
                mock.patch.object(bridge, "_pending", pending):
            out = list(bridge.generate_stream(
                "# Tool Use\n...", "m", False))
        text = "".join(t for _k, t in out)
        _clean, calls, dropped = tools.parse_tool_calls(
            text, {"read_file"}, {"read_file": {"path"}})
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"path": "a.py"})

    def test_native_tool_call_ignored_without_tool_prompt(self):
        events = [{"kind": "tool_call", "name": "read_file",
                   "arguments": {}}, {"kind": "done"}]
        pending = _fake_pending(events)
        with mock.patch.object(bridge, "worker_alive", return_value=True), \
                mock.patch.object(bridge, "_pending", pending):
            with self.assertRaises(qwen.QwenError) as ctx:
                list(bridge.generate_stream("plain chat", "m", False))
        self.assertEqual(ctx.exception.status, 502)

    def test_error_event_is_502(self):
        pending = _fake_pending([{"kind": "error", "message": "boom"}])
        with mock.patch.object(bridge, "worker_alive", return_value=True), \
                mock.patch.object(bridge, "_pending", pending):
            with self.assertRaises(qwen.QwenError) as ctx:
                list(bridge.generate_stream("hi", "m", False))
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("boom", str(ctx.exception))

    def test_generate_aggregates_stream(self):
        chunks = [("content", "a"), ("reasoning", "r"), ("content", "b")]
        with mock.patch.object(bridge, "generate_stream",
                               return_value=iter(chunks)):
            out = bridge.generate("p", "m", False)
        self.assertEqual(out["content"], "ab")
        self.assertEqual(out["reasoning"], "r")


class TestBridgeHttp(_Sandbox):
    """Real loopback HTTP server for the extension-facing endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.srv = bridge._Server(("127.0.0.1", 0), bridge._Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(2)

    def _req(self, method, path, body=None, origin="chrome-extension://x"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Origin": origin}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_file_endpoint_serves_temp_prompt(self):
        fd, path = tempfile.mkstemp(prefix="qwen_prompt_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("big prompt")
        try:
            status, body = self._req(
                "GET", "/file/" + os.path.basename(path))
            self.assertEqual(status, 200)
            self.assertEqual(body, b"big prompt")
        finally:
            os.unlink(path)

    def test_file_endpoint_rejects_other_names(self):
        status, body = self._req("GET", "/file/secret.txt")
        self.assertEqual(status, 400)
        self.assertIn(b"invalid file", body)

    def test_file_endpoint_missing_file_is_404(self):
        status, _body = self._req("GET", "/file/qwen_prompt_missing.txt")
        self.assertEqual(status, 404)

    def test_file_endpoint_only_serves_tmpdir_root(self):
        with tempfile.TemporaryDirectory() as sub:
            name = "qwen_prompt_nested_%s.txt" % os.getpid()
            nested = os.path.join(sub, name)
            with open(nested, "w", encoding="utf-8") as f:
                f.write("nested")
            status, _body = self._req("GET", "/file/" + name)
        self.assertEqual(status, 404)

    def test_post_requires_extension_or_qwen_origin(self):
        # No body: the handler checks Origin before reading it, and a body
        # left unread would reset the loopback connection on Windows.
        status, body = self._req("POST", "/poll", body=None,
                                 origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden origin", body)

    def test_poll_times_out_without_jobs(self):
        CONFIG["bridge_poll_wait"] = 0.05
        status, body = self._req("POST", "/poll", body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"job": None})

    def test_poll_returns_queued_job(self):
        CONFIG["bridge_poll_wait"] = 0.5
        bridge._pending.put({"job_id": "j1", "ts": time.time()})
        status, body = self._req("POST", "/poll", body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["job"]["job_id"], "j1")

    def test_event_routes_to_job_queue(self):
        q = queue.Queue()
        bridge._jobs["job_x"] = q
        try:
            event = {"job_id": "job_x", "kind": "delta", "text": "x"}
            status, body = self._req("POST", "/event", body=event)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})
            self.assertEqual(q.get_nowait()["kind"], "delta")
        finally:
            bridge._jobs.pop("job_x", None)

    def test_status_endpoint(self):
        bridge._last_seen = time.time()
        status, body = self._req("GET", "/status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["worker"])


# ---------------------------------------------------------------- server ----
class TestServerHelpers(_Sandbox):

    def test_usage(self):
        self.assertEqual(server._usage("abcd", "efgh"),
                         {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2})
        self.assertEqual(server._usage("abcd", "efgh", {
            "prompt_tokens": 10, "completion_tokens": 5}),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.assertEqual(server._usage("abcd", "efgh", {
            "input_tokens": 7, "output_tokens": 2}),
            {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})
        self.assertEqual(server._usage("abcd", "efgh", {"prompt_tokens": 5}),
                         {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2})

    def test_stop_helpers(self):
        self.assertEqual(server._normalize_stop("STOP"), ["STOP"])
        self.assertEqual(server._normalize_stop(["a", "", 3, None]), ["a"])
        self.assertEqual(server._normalize_stop(None), [])
        self.assertEqual(server._apply_stop("hello world", ["world"]),
                         "hello ")
        self.assertEqual(server._apply_stop("hello world", []), "hello world")
        self.assertEqual(server._apply_stop("", ["x"]), "")
        self.assertEqual(server._apply_stop("aXbY", ["Y", "X"]), "a")

    def test_max_tokens(self):
        self.assertEqual(server._apply_max_tokens("abcdefghij", 2),
                         ("abcdefgh", True))
        self.assertEqual(server._apply_max_tokens("abc", 2), ("abc", False))
        self.assertEqual(server._apply_max_tokens("abc", None), ("abc", False))
        self.assertEqual(server._apply_max_tokens("abc", 0), ("abc", False))
        self.assertEqual(server._apply_max_tokens("abc", "3"), ("abc", False))

    def test_text_format(self):
        self.assertEqual(server._text_format(
            {"format": {"type": "json_object"}}), {"type": "json_object"})
        nested = {"type": "json_schema",
                  "json_schema": {"name": "x", "schema": {"type": "object"}}}
        self.assertEqual(server._text_format({"format": nested}), nested)
        self.assertEqual(server._text_format(
            {"format": {"type": "json_schema",
                        "schema": {"type": "object"}}}),
            {"type": "json_schema",
             "json_schema": {"schema": {"type": "object"}}})
        self.assertIsNone(server._text_format({"format": {"type": "text"}}))
        self.assertIsNone(server._text_format(None))

    def test_norm_resp_tools(self):
        self.assertIsNone(server._norm_resp_tools(None))
        self.assertIsNone(server._norm_resp_tools([{"type": "web_search"}]))
        out = server._norm_resp_tools([
            {"type": "function", "function": {"name": "f"}},
            {"type": "function", "function": {}},
        ])
        self.assertEqual(out, [{"type": "function", "function": {
            "name": "f", "description": "", "parameters": {}}}])

    def test_responses_messages_from_string(self):
        msgs = server._responses_messages("hello", "sys")
        self.assertEqual(msgs, [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "hello"}])

    def test_responses_messages_function_call_roundtrip(self):
        msgs = server._responses_messages([
            {"type": "function_call", "call_id": "c1", "name": "read_file",
             "arguments": '{"p": 1}'},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "output_text", "text": "RESULT"}]},
        ], None)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertEqual(msgs[0]["tool_calls"][0]["id"], "c1")
        self.assertEqual(msgs[1], {"role": "tool", "tool_call_id": "c1",
                                   "content": "RESULT"})

    def test_responses_messages_appends_adjacent_call(self):
        msgs = server._responses_messages([
            {"type": "function_call", "call_id": "c0", "name": "a",
             "arguments": "{}"},
            {"type": "function_call", "call_id": "c1", "name": "b",
             "arguments": "{}"},
        ], None)
        self.assertEqual(len(msgs), 1)
        self.assertEqual([c["id"] for c in msgs[0]["tool_calls"]],
                         ["c0", "c1"])

    def test_responses_messages_keeps_plain_assistant_tool_calls(self):
        msgs = server._responses_messages([
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c0", "function": {"name": "a",
                                                     "arguments": "{}"}}]},
            {"type": "function_call", "call_id": "c1", "name": "b",
             "arguments": "{}"},
        ], None)
        # A plain assistant item keeps its tool_calls out of the prompt, so
        # the following function_call starts its own message.
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["content"], None)
        self.assertEqual(msgs[1]["tool_calls"][0]["id"], "c1")

    def test_responses_messages_misc_shapes(self):
        msgs = server._responses_messages([
            "plain",
            {"type": "input_text", "text": "typed"},
            {"role": "developer", "content": [
                {"type": "input_text", "text": "a"},
                {"type": "input_text", "text": "b"}]},
            42,
        ], None)
        self.assertEqual(msgs, [
            {"role": "user", "content": "plain"},
            {"role": "user", "content": "typed"},
            {"role": "system", "content": "a\nb"},
        ])

    def test_prepare_large_prompt_small_stays_inline(self):
        msgs = [{"role": "user", "content": "x" * 50}]
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 50):
            file_info, out = server.prepare_large_prompt(msgs)
        self.assertIsNone(file_info)
        self.assertIs(out, msgs)

    def test_prepare_large_prompt_writes_temp_file(self):
        msgs = [{"role": "user", "content": "x" * 51}]
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 50):
            file_info, short = server.prepare_large_prompt(msgs)
        try:
            self.assertIsNotNone(file_info)
            self.assertTrue(os.path.exists(file_info["path"]))
            self.assertTrue(file_info["filename"].startswith("qwen_prompt_"))
            self.assertEqual(file_info["size"], 51)
            with open(file_info["path"], encoding="utf-8") as f:
                self.assertEqual(f.read(), "x" * 51)
            self.assertEqual(len(short), 1)
            self.assertIn(file_info["filename"], short[0]["content"])
            self.assertIn("Please read", short[0]["content"])
        finally:
            server._remove_prompt_file(file_info)
        self.assertFalse(os.path.exists(file_info["path"]))

    def test_remove_prompt_file_is_idempotent(self):
        server._remove_prompt_file(None)
        server._remove_prompt_file({"path": os.path.join(
            tempfile.gettempdir(), "qwen_prompt_does_not_exist.txt")})

    def test_route(self):
        self.assertEqual(_bare_handler(path="/v1/chat/completions/")._route(),
                         "/v1/chat/completions")
        self.assertEqual(_bare_handler(path="/v1/models?x=1")._route(),
                         "/v1/models")
        self.assertEqual(_bare_handler(path="/")._route(), "/")
        self.assertEqual(_bare_handler(path="")._route(), "/")

    def test_authorized(self):
        h = _bare_handler(headers={"Authorization": "Bearer k"})
        self.assertTrue(h._authorized())          # no keys configured

        CONFIG["api_keys"] = ["k"]
        self.assertTrue(h._authorized())
        self.assertFalse(_bare_handler(
            headers={"Authorization": "Bearer nope"})._authorized())
        self.assertTrue(_bare_handler(
            headers={"x-api-key": "k"})._authorized())
        self.assertTrue(_bare_handler(
            headers={"x-goog-api-key": "k"})._authorized())
        self.assertTrue(_bare_handler(
            path="/v1/models?key=k")._authorized())
        self.assertFalse(_bare_handler(path="/v1/models")._authorized())

    def test_security_ok(self):
        self.assertTrue(_bare_handler()._security_ok())
        self.assertFalse(_bare_handler(
            headers={"Host": "evil.example"})._security_ok())
        self.assertTrue(_bare_handler(
            headers={"Host": "127.0.0.1:8082"})._security_ok())
        self.assertTrue(_bare_handler(
            headers={"Host": "localhost:8082"})._security_ok())
        self.assertFalse(_bare_handler(
            headers={"Origin": "https://evil.example"})._security_ok())

        CONFIG["cors_origins"] = ["https://ok.example"]
        self.assertTrue(_bare_handler(
            headers={"Origin": "https://ok.example"})._security_ok())
        self.assertFalse(_bare_handler(
            headers={"Origin": "https://evil.example"})._security_ok())

        CONFIG["api_keys"] = ["k"]
        self.assertTrue(_bare_handler(
            headers={"Host": "evil.example"})._security_ok())

    def test_resolve_model(self):
        CONFIG["strict_models"] = False
        h = _bare_handler()
        self.assertEqual(h._resolve({"model": "qwen3.8-max-thinking"}),
                         ("qwen3.8-max-thinking", "qwen3.8-max", True))
        self.assertEqual(
            h._resolve({"model": "qwen3.7-plus", "reasoning_effort": "low"}),
            ("qwen3.7-plus", "qwen3.7-plus", False))
        self.assertEqual(
            h._resolve({"model": "qwen3.7-plus", "reasoning_effort": "high"}),
            ("qwen3.7-plus", "qwen3.7-plus", True))
        self.assertEqual(
            h._resolve({"model": "qwen3.7-plus",
                        "reasoning": {"effort": "minimal"}}),
            ("qwen3.7-plus", "qwen3.7-plus", False))
        self.assertEqual(h.errors, [])

    def test_resolve_invalid_think_reports_400(self):
        CONFIG["strict_models"] = False
        h = _bare_handler()
        self.assertIsNone(h._resolve({"model": "x@think=maybe"}))
        self.assertEqual(h.errors, [(400, "Invalid think level: maybe", None)])

    def test_read_body_content_length(self):
        h = _bare_handler(headers={"Content-Length": "5"})
        h.rfile = io.BytesIO(b"hello")
        self.assertEqual(h._read_body(), b"hello")

        h = _bare_handler(headers={})
        self.assertEqual(h._read_body(), b"")

    def test_read_body_chunked(self):
        h = _bare_handler(headers={"Transfer-Encoding": "chunked"})
        h.rfile = io.BytesIO(b"5\r\nhello\r\n0\r\n\r\n")
        self.assertEqual(h._read_body(), b"hello")

    def test_read_body_errors(self):
        h = _bare_handler(headers={"Transfer-Encoding": "chunked"})
        h.rfile = io.BytesIO(b"zz\r\n")
        with self.assertRaises(ValueError):
            h._read_body()

        with self.assertRaises(ValueError):
            _bare_handler(headers={"Content-Length": "abc"})._read_body()

        with self.assertRaises(ValueError):
            _bare_handler(headers={"Content-Length": "-1"})._read_body()

        h = _bare_handler(headers={"Content-Length": "5"})
        h.rfile = io.BytesIO(b"abc")
        with self.assertRaises(ValueError):
            h._read_body()

        CONFIG["max_body_bytes"] = 4
        h = _bare_handler(headers={"Content-Length": "5"})
        h.rfile = io.BytesIO(b"hello")
        with self.assertRaises(ValueError) as ctx:
            h._read_body()
        self.assertIn("too large", str(ctx.exception))

    def test_sse_writers(self):
        h = _bare_handler()
        h._sse_chunk("cid", "m", {"content": "hi"}, None, True)
        h._sse_usage("cid", "m", "abcd", "efgh", None)
        h._sse_error(503, "boom")
        h._sse_done()
        lines = h.wfile.getvalue().decode().split("\n\n")
        chunk = json.loads(lines[0][len("data: "):])
        self.assertEqual(chunk["object"], "chat.completion.chunk")
        self.assertEqual(chunk["choices"][0]["delta"]["content"], "hi")
        self.assertIsNone(chunk["usage"])
        usage = json.loads(lines[1][len("data: "):])
        self.assertEqual(usage["usage"]["total_tokens"], 2)
        err = json.loads(lines[2][len("data: "):])
        self.assertEqual(err["error"]["message"], "boom")
        self.assertEqual(lines[3].strip(), "data: [DONE]")


# ---------------------------------------------------------------- config ----
class TestConfig(_Sandbox):

    def test_default_config_has_required_keys(self):
        for key in ("host", "port", "base_url", "default_model",
                    "prompt_max_bytes", "retry_attempts",
                    "tool_retry_attempts", "log_requests"):
            self.assertIn(key, DEFAULT_CONFIG)

    def test_load_config_merges(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"port": 9999, "api_keys": ["k"]}, f)
            load_config(path)
        self.assertEqual(CONFIG["port"], 9999)
        self.assertEqual(CONFIG["api_keys"], ["k"])
        self.assertEqual(CONFIG["host"], DEFAULT_CONFIG["host"])

    def test_load_config_missing_file_is_noop(self):
        before = dict(CONFIG)
        load_config(os.path.join(tempfile.gettempdir(),
                                 "qwen-web2api-no-such-config.json"))
        self.assertEqual(CONFIG, before)

    def test_find_config_returns_none_when_absent(self):
        with mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(find_config())


if __name__ == "__main__":
    unittest.main(verbosity=2)
