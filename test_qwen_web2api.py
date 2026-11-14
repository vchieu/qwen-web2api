"""Unit tests for qwen-web2api -- single file, standard library only.

Run:
    python -m unittest test_qwen_web2api -v
    # or
    pytest test_qwen_web2api.py -q

No network, no browser, no Qwen token needed: upstream calls are stubbed or
isolated and the bridge endpoints run on a loopback server. Keep this file
self-contained so it can be pasted into an AI chat as-is; do not split it
unless it grows past ~1500 lines.
"""
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
        self._bridge = {
            "_last_seen": bridge._last_seen,
            "_jobs": bridge._jobs,
            "_pending": bridge._pending,
            "_models": list(bridge._models),
        }
        self._recent_log_snapshot = list(qwen._recent_log)
        CONFIG["log_requests"] = False
        CONFIG["retry_attempts"] = 1
        CONFIG["retry_delay_sec"] = 0

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self._config)
        with qwen._log_lock:
            qwen._recent_log.clear()
            qwen._recent_log.extend(self._recent_log_snapshot)
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

    def test_tool_block_routes_final_answer_through_completion_tool(self):
        defs = [{"type": "function",
                 "function": {"name": "attempt_completion"}}]
        p = tools.messages_to_prompt([{"role": "user", "content": "go"}], defs)
        self.assertIn("matching completion tool", p)
        self.assertIn("attempt_completion", p)
        plain = tools.messages_to_prompt([{"role": "user", "content": "go"}])
        self.assertNotIn("matching completion tool", plain)

    def test_crlf_is_normalized_in_prompt(self):
        p = tools.messages_to_prompt([{"role": "user",
                                       "content": "a\r\nb\rc"}])
        self.assertEqual(p, "a\nb\nc")

    def test_max_bytes_override_truncates_prompt(self):
        msgs = [{"role": "user", "content": f"message-{i:02d}-" + "x" * 40}
                for i in range(30)]
        p = tools.messages_to_prompt(msgs, max_bytes=400)
        self.assertLessEqual(len(p.encode("utf-8")), 400)
        self.assertIn("message-29-", p)
        self.assertGreater(len(tools.messages_to_prompt(msgs).encode("utf-8")), 400)

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
        # XML-style tag
        self.assertTrue(tools.looks_like_tool_call(
            '<tool_call>\n{"name": "f", "arguments": {}}\n</tool_call>'))

    def test_xml_tool_call_tag_is_parsed(self):
        """<tool_call>...</tool_call> format được nhận dạng và parse."""
        text = ('<tool_call>\n'
                '{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                '</tool_call>')
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"read_file"}, {"read_file": {"path"}})
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"path": "a.py"})
        self.assertEqual(clean, "")

    def test_xml_tool_call_mixed_with_fence(self):
        """XML tag và fence block cùng tồn tại trong cùng một response."""
        text = ('Step 1\n'
                '<tool_call>\n'
                '{"name": "read_file", "arguments": {"path": "a.py"}}\n'
                '</tool_call>\n'
                'Step 2\n'
                '```tool_call\n'
                '{"name": "read_file", "arguments": {"path": "b.py"}}\n'
                '```')
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"read_file"}, {"read_file": {"path"}})
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 2)
        paths = [json.loads(c["function"]["arguments"])["path"] for c in calls]
        self.assertIn("a.py", paths)
        self.assertIn("b.py", paths)
        self.assertIn("Step 1", clean)
        self.assertIn("Step 2", clean)

    def test_xml_tool_call_undeclared_is_dropped(self):
        text = ('<tool_call>\n'
                '{"name": "other", "arguments": {}}\n'
                '</tool_call>')
        clean, calls, dropped = tools.parse_tool_calls(text, {"f"}, None)
        self.assertEqual(calls, [])
        self.assertEqual(dropped, ["undeclared tool 'other'"])
        self.assertEqual(clean, text)

    def test_shorthand_tool_name_without_key_is_parsed(self):
        """Model sinh '{"grep", "arguments": {...}}' thiếu key 'name'."""
        text = ('```tool_call\n'
                '{"grep", "arguments": {"pattern": "^_cookie_pool_lock", "path": "gemini_web2api.py"}}\n'
                '```')
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"grep"}, {"grep": {"pattern", "path"}})
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "grep")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["pattern"], "^_cookie_pool_lock")
        self.assertEqual(args["path"], "gemini_web2api.py")
        self.assertEqual(clean, "")

    def test_prose_with_shorthand_tool_call_splits_cleanly(self):
        """User report: prose kèm tool_call shorthand được tách ra và giữ lại text."""
        prose = (
            "I have read the attached document. Based on the conversation history...\n\n"
            "- Phase 1 is complete\n"
            "- Phase 2 state variables are added\n\n"
            "Let me now implement Phase 2."
        )
        fence = (
            '```tool_call\n'
            '{"grep", "arguments": {"pattern": "^_cookie_pool_lock = threading\\\\.Lock\\\\(\\\\)", '
            '"path": "gemini_web2api.py"}}\n'
            '```'
        )
        full_text = f"{prose}\n\n{fence}"
        clean, calls, dropped = tools.parse_tool_calls(
            full_text, {"grep"}, {"grep": {"pattern", "path"}})
        self.assertEqual(dropped, [])
        self.assertEqual(clean, prose)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "grep")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["path"], "gemini_web2api.py")
        self.assertIn("_cookie_pool_lock", args["pattern"])

    def test_single_key_dict_tool_call(self):
        """Model sinh '{"grep": {"pattern": "abc", "path": "x.py"}}'."""
        text = '```tool_call\n{"grep": {"pattern": "abc", "path": "x.py"}}\n```'
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"grep"}, {"grep": {"pattern", "path"}})
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "grep")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args, {"pattern": "abc", "path": "x.py"})
        self.assertEqual(clean, "")

    def test_lenient_json_single_quotes_and_trailing_comma(self):
        """Single quotes và trailing comma được sửa tự động."""
        text = "```tool_call\n{'name': 'f', 'arguments': {'q': 'val',},}\n```"
        clean, calls, dropped = tools.parse_tool_calls(
            text, {"f"}, {"f": {"q"}})
        self.assertEqual(dropped, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "f")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"q": "val"})
        self.assertEqual(clean, "")

    def test_looks_like_missed_tool_call(self):
        self.assertTrue(tools.looks_like_missed_tool_call("Let me read it."))
        self.assertTrue(tools.looks_like_missed_tool_call("I'll check the log"))
        self.assertTrue(tools.looks_like_missed_tool_call("minh se doc file"))
        self.assertFalse(tools.looks_like_missed_tool_call(
            "Let me read it\n```tool_call\n{}\n```"))
        self.assertFalse(tools.looks_like_missed_tool_call(
            "Let me read it\n<tool_call>\n{}\n</tool_call>"))
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
class TestQwenShared(_Sandbox):

    def test_log_silent_when_disabled(self):
        CONFIG["log_requests"] = False
        err = io.StringIO()
        with mock.patch("sys.stderr", new=err):
            qwen.log("nope")
        self.assertEqual(err.getvalue(), "")

    def test_log_writes_stderr_and_file(self):
        CONFIG["log_requests"] = True
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "qwen.log")
            CONFIG["log_file"] = path
            err = io.StringIO()
            with mock.patch("sys.stderr", new=err):
                qwen.log("hello bridge")
            self.assertIn("hello bridge", err.getvalue())
            with open(path, encoding="utf-8") as f:
                self.assertIn("hello bridge", f.read())

    def test_qwen_error_status_roundtrip(self):
        e = qwen.QwenError("boom", status=503)
        self.assertEqual(str(e), "boom")
        self.assertEqual(e.status, 503)
        self.assertIsNone(qwen.QwenError("plain").status)

    def test_debug_log_disabled_by_default(self):
        CONFIG["log_requests"] = True
        CONFIG["debug"] = False
        err = io.StringIO()
        with mock.patch("sys.stderr", new=err):
            qwen.debug_log("hidden debug")
        self.assertEqual(err.getvalue(), "")

    def test_debug_log_writes_when_enabled(self):
        CONFIG["log_requests"] = True
        CONFIG["debug"] = True
        err = io.StringIO()
        with mock.patch("sys.stderr", new=err):
            qwen.debug_log("visible debug")
        self.assertIn("[debug] visible debug", err.getvalue())

    def test_is_debug_accepts_aliases(self):
        CONFIG["debug"] = False
        CONFIG["debug_log"] = False
        self.assertFalse(qwen.is_debug())
        CONFIG["debug"] = True
        self.assertTrue(qwen.is_debug())
        CONFIG["debug"] = False
        CONFIG["debug_log"] = True
        self.assertTrue(qwen.is_debug())


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

    def test_status_reports_bridge_features(self):
        CONFIG["temporary_chat"] = True
        CONFIG["bridge_select_model"] = False
        CONFIG["debug"] = True
        bridge._models = []
        st = bridge.status()
        self.assertTrue(st["temporary_chat"])
        self.assertFalse(st["select_model"])
        self.assertTrue(st["debug"])
        self.assertEqual(st["attachment_prompt"], "Please read the text and action")
        self.assertEqual(st["models"], 0)
        bridge._models = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        self.assertEqual(bridge.status()["models"], 2)
        self.assertEqual(bridge.known_models()[0], {"id": "a", "name": "A"})

    def test_status_accepts_hyphenated_temporary_key(self):
        CONFIG["temporary_chat"] = False
        CONFIG["temporary-chat"] = True
        self.assertTrue(bridge.status()["temporary_chat"])
        CONFIG.pop("temporary-chat", None)
        self.assertFalse(bridge.status()["temporary_chat"])

    def test_known_models_is_a_copy(self):
        bridge._models = [{"id": "a", "name": "A"}]
        out = bridge.known_models()
        self.assertEqual(out, [{"id": "a", "name": "A"}])
        out.append({"id": "b", "name": "B"})
        out[0]["name"] = "changed"
        self.assertEqual(bridge.known_models(), [{"id": "a", "name": "A"}])

    def test_warn_event_does_not_break_stream(self):
        pending = _fake_pending([
            {"kind": "warn", "message": "model picker missing"},
            {"kind": "delta", "text": "hi"},
            {"kind": "done"},
        ])
        with mock.patch.object(bridge, "worker_alive", return_value=True), \
                mock.patch.object(bridge, "_pending", pending):
            out = list(bridge.generate_stream("hi", "m", False))
        self.assertEqual(out, [("content", "hi")])

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
        self.assertEqual(job["attachment_prompt"], "Please read the text and action")
        self.assertEqual(bridge._jobs, {})

    def test_generate_stream_with_custom_attachment_prompt(self):
        pending = _fake_pending([{"kind": "delta", "text": "ok"}, {"kind": "done"}])
        with mock.patch.object(bridge, "worker_alive", return_value=True), \
                mock.patch.object(bridge, "_pending", pending):
            list(bridge.generate_stream(
                "my prompt", "m", False,
                file_info={"path": "x", "attachment_prompt": "my prompt"}))
        job = pending.jobs[0]
        self.assertEqual(job["attachment_prompt"], "my prompt")

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

    def test_poll_drops_orphan_managed_job(self):
        CONFIG["bridge_poll_wait"] = 0.05
        # Job co managed=True nhung khong co trong bridge._jobs -> phai bi drop
        bridge._pending.put({"job_id": "orphan_1", "ts": time.time(), "managed": True})
        status, body = self._req("POST", "/poll", body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"job": None})

    def test_poll_drops_expired_job(self):
        CONFIG["bridge_poll_wait"] = 0.05
        CONFIG["bridge_job_timeout"] = 10
        # Job co ts qua cu (het han ttl) -> phai bi drop
        bridge._pending.put({"job_id": "exp_1", "ts": time.time() - 100})
        status, body = self._req("POST", "/poll", body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"job": None})

    def test_event_handles_tab_reloaded_warning(self):
        # Gui event canh bao tab reload ma khong co active job van thanh cong va duoc ghi log
        status, body = self._req("POST", "/event", body={
            "job_id": "job_reloaded",
            "kind": "warn",
            "tab_reloaded": True,
            "stage": "file_uploaded",
            "message": "Browser/Tab da tu reload khi dang xu ly job"
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_event_handles_none_job_id_warning(self):
        status, body = self._req("POST", "/event", body={
            "job_id": "none",
            "kind": "warn",
            "message": "model refresh fail: connection error"
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

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
        data = json.loads(body)
        self.assertTrue(data["worker"])
        self.assertIn("temporary_chat", data)
        self.assertIn("select_model", data)
        self.assertIn("models", data)

    def test_poll_publishes_models(self):
        CONFIG["bridge_poll_wait"] = 0.05
        bridge._models = []
        status, _body = self._req("POST", "/poll", body={"models": [
            {"id": "qwen3.8-max", "name": "Qwen3.8-Max"},
            {"id": ""},
            {"id": 3},
            "junk",
            {"name": "no id"},
            {"id": " qwen-live "},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(bridge.known_models(), [
            {"id": "qwen3.8-max", "name": "Qwen3.8-Max"},
            {"id": "qwen-live", "name": "qwen-live"},
        ])

    def test_requeue_roundtrips_job(self):
        CONFIG["bridge_poll_wait"] = 0.5
        while True:
            try:
                bridge._pending.get_nowait()
            except queue.Empty:
                break
        status, body = self._req("POST", "/requeue", body={
            "job": {"job_id": "rq1", "prompt": "hi", "ts": 1}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        status, body = self._req("POST", "/poll", body={})
        job = json.loads(body)["job"]
        self.assertEqual(job["job_id"], "rq1")
        self.assertEqual(job["prompt"], "hi")
        self.assertGreater(job["ts"], 1)

    def test_requeue_rejects_invalid_job(self):
        status, body = self._req("POST", "/requeue",
                                 body={"job": {"prompt": "x"}})
        self.assertEqual(status, 400)
        self.assertIn(b"invalid job", body)
        status, body = self._req("POST", "/requeue", body={"job": "x"})
        self.assertEqual(status, 400)
        self.assertIn(b"invalid job", body)


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

    def test_prepare_large_prompt_splits_history_and_last_user_message(self):
        msgs = [
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": "x" * 50},
            {"role": "assistant", "content": "y" * 20},
            {"role": "user", "content": "how to fix?"},
        ]
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 50):
            file_info, short = server.prepare_large_prompt(msgs)
        try:
            self.assertIsNotNone(file_info)
            self.assertTrue(os.path.exists(file_info["path"]))
            self.assertTrue(file_info["filename"].startswith("qwen_prompt_"))
            with open(file_info["path"], encoding="utf-8") as f:
                content = f.read()
            self.assertTrue(content.startswith("[Conversation History]\n\n"))
            self.assertIn("be concise", content)
            self.assertIn("x" * 50, content)
            self.assertIn("y" * 20, content)
            self.assertNotIn("how to fix?", content)
            self.assertEqual(len(short), 1)
            self.assertEqual(short[0], {"role": "user", "content": "how to fix?"})
            self.assertEqual(file_info.get("attachment_prompt"), "how to fix?")
        finally:
            server._remove_prompt_file(file_info)
        self.assertFalse(os.path.exists(file_info["path"]))

    def test_prepare_large_prompt_splits_with_tools(self):
        tools = [{"type": "function", "function": {"name": "test_tool", "parameters": {}}}]
        msgs = [
            {"role": "user", "content": "x" * 3000},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "run tool now"},
        ]
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 2000):
            file_info, short = server.prepare_large_prompt(msgs, tools=tools)
        try:
            self.assertIsNotNone(file_info)
            self.assertEqual(short, [{"role": "user", "content": "run tool now"}])
            self.assertIn("# Tool Use", file_info["attachment_prompt"])
            self.assertIn("run tool now", file_info["attachment_prompt"])
        finally:
            server._remove_prompt_file(file_info)

    def test_prepare_large_prompt_history_file_fits_paste_limit(self):
        # Qwen khong chuyen paste > 131072 ky tu thanh file: file lich su phai bi
        # cat (giu tin moi nhat) de <= FILE_MAX_CHARS.
        msgs = [{"role": "system", "content": "SYS"}]
        msgs += [{"role": "user" if i % 2 == 0 else "assistant",
                  "content": f"m{i:02d}-" + "x" * 300} for i in range(20)]
        msgs.append({"role": "user", "content": "last question"})
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 1000), \
                mock.patch.object(server, "FILE_MAX_CHARS", 2000):
            file_info, short = server.prepare_large_prompt(msgs)
        try:
            self.assertIsNotNone(file_info)
            with open(file_info["path"], encoding="utf-8") as f:
                content = f.read()
            self.assertLessEqual(len(content), 2000)
            self.assertEqual(file_info["size"], len(content))
            self.assertIn("[...truncated...]", content)
            self.assertIn("m19-", content)
            self.assertEqual(short, [{"role": "user",
                                      "content": "last question"}])
        finally:
            server._remove_prompt_file(file_info)

    def test_prepare_large_prompt_does_not_split_when_last_msg_not_user(self):
        msgs = [
            {"role": "user", "content": "x" * 50},
            {"role": "assistant", "content": "y" * 10},
        ]
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 50):
            file_info, short = server.prepare_large_prompt(msgs)
        try:
            self.assertIsNotNone(file_info)
            self.assertIsNone(file_info.get("attachment_prompt"))
            self.assertIn("[Attached file:", short[0]["content"])
        finally:
            server._remove_prompt_file(file_info)

    def test_prepare_large_prompt_does_not_split_when_last_user_msg_too_large(self):
        msgs = [
            {"role": "user", "content": "x" * 40},
            {"role": "user", "content": "y" * 60},
        ]
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 50):
            file_info, short = server.prepare_large_prompt(msgs)
        try:
            self.assertIsNotNone(file_info)
            self.assertIsNone(file_info.get("attachment_prompt"))
            self.assertIn("[Attached file:", short[0]["content"])
        finally:
            server._remove_prompt_file(file_info)

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

    def test_resolve_strict_models_uses_bridge_list(self):
        CONFIG["strict_models"] = True
        h = _bare_handler()
        with mock.patch.object(bridge, "known_models",
                               return_value=[{"id": "qwen-live",
                                              "name": "Live"}]):
            self.assertEqual(h._resolve({"model": "qwen-live"}),
                             ("qwen-live", "qwen-live", False))
            self.assertIsNone(h._resolve({"model": "nope"}))
        self.assertEqual(h.errors,
                         [(404, "model 'nope' not found", "model_not_found")])

    def test_models_endpoint_merges_bridge_list(self):
        h = _bare_handler(path="/v1/models")
        h.send_json = lambda data, status=200: h.errors.append((status, data))
        with mock.patch.object(bridge, "known_models",
                               return_value=[{"id": "qwen-live",
                                              "name": "Live"}]):
            h.do_GET()
        self.assertEqual(h.errors[0][0], 200)
        ids = [m["id"] for m in h.errors[0][1]["data"]]
        self.assertIn("qwen3.8-max", ids)
        self.assertIn("qwen-live", ids)

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


# ------------------------------------------------------------ server turn ----
class TestServerTurn(_Sandbox):

    def test_plain_text(self):
        h = _bare_handler()
        result = {"content": "hello", "reasoning": "",
                  "usage": {"prompt_tokens": 2}}
        with mock.patch.object(bridge, "generate", return_value=result) as gen:
            text, reasoning, calls, usage = h._run_turn(
                "prompt", "m", False, False, None, None, None, False)
        self.assertEqual((text, reasoning, calls, usage),
                         ("hello", "", None, {"prompt_tokens": 2}))
        gen.assert_called_once_with("prompt", "m", False, file_info=None)

    def test_tool_call_prose_kept_as_text(self):
        h = _bare_handler()
        content = ('Let me check.\n```tool_call\n'
                   '{"name": "read_file", "arguments": {"path": "a.py"}}\n```')
        result = {"content": content, "reasoning": "upstream thinking", "usage": {}}
        with mock.patch.object(bridge, "generate", return_value=result):
            text, reasoning, calls, _usage = h._run_turn(
                "prompt", "m", False, True, {"read_file"},
                {"read_file": {"path"}}, None, False)
        self.assertEqual(text, "Let me check.")
        self.assertEqual(reasoning, "upstream thinking")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")

    def test_missing_required_param_raises(self):
        h = _bare_handler()
        content = '```tool_call\n{"name": "read_file", "arguments": {}}\n```'
        result = {"content": content, "reasoning": "", "usage": {}}
        with mock.patch.object(bridge, "generate", return_value=result):
            with self.assertRaises(qwen.QwenError) as ctx:
                h._run_turn("prompt", "m", False, True, {"read_file"},
                            {"read_file": {"path"}},
                            {"read_file": {"path"}}, False)
        self.assertIn("missing", str(ctx.exception))

    def test_required_tool_without_call_raises(self):
        h = _bare_handler()
        result = {"content": "khong goi tool", "reasoning": "", "usage": {}}
        with mock.patch.object(bridge, "generate", return_value=result):
            with self.assertRaises(qwen.QwenError) as ctx:
                h._run_turn("prompt", "m", False, False, None, None, None, True)
        self.assertIn("required tool call", str(ctx.exception))

    def test_streaming_reasoning_deltas(self):
        h = _bare_handler()
        stream_events = [("reasoning", "Thinking step 1... "),
                         ("reasoning", "Thinking step 2... "),
                         ("content", "Final answer")]
        reasoning_deltas = []

        def on_reasoning(delta):
            reasoning_deltas.append(delta)

        def mock_generate_stream(prompt, model, think, usage_out=None, file_info=None):
            if usage_out is not None:
                usage_out.update({"prompt_tokens": 10})
            yield from stream_events

        with mock.patch.object(bridge, "generate_stream", side_effect=mock_generate_stream):
            text, reasoning, calls, usage = h._run_turn(
                "prompt", "m", True, False, None, None, None, False,
                on_reasoning_delta=on_reasoning)
        self.assertEqual(text, "Final answer")
        self.assertEqual(reasoning, "Thinking step 1... Thinking step 2... ")
        self.assertEqual(reasoning_deltas, ["Thinking step 1... ", "Thinking step 2... "])
        self.assertEqual(usage.get("prompt_tokens"), 10)


# ---------------------------------------------------------------- config ----
class TestConfig(_Sandbox):

    def test_default_config_has_required_keys(self):
        for key in ("host", "port", "bridge_port", "bridge_worker_ttl",
                    "bridge_job_timeout", "bridge_poll_wait", "temporary_chat",
                    "bridge_select_model", "attachment_prompt", "default_model", "prompt_max_bytes",
                    "retry_attempts", "tool_retry_attempts", "log_requests", "debug"):
            self.assertIn(key, DEFAULT_CONFIG)

    def test_default_config_dropped_direct_keys(self):
        for key in ("token", "token_file", "cookie", "bx_v", "bx_ua",
                    "bx_umidtoken", "extra_headers", "user_agent", "proxy",
                    "impersonate", "auto_refresh", "refresh_paths",
                    "refresh_margin_sec", "chat_mode", "api_version",
                    "delete_chats", "auto_search", "research_mode",
                    "thinking_format", "request_timeout_sec", "debug_raw",
                    "debug_raw_file", "base_url", "bridge_enabled"):
            self.assertNotIn(key, DEFAULT_CONFIG)

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


# ------------------------------------------------------ bridge debug log ----
class TestBridgeDebugLog(_Sandbox):
    """Tests for the new debug logging improvements."""

    def test_recent_log_buffer_is_populated(self):
        """_log() phải ghi vào _recent_log deque."""
        from collections import deque
        self.assertIsInstance(bridge._recent_log, deque)
        import collections as _col
        old = list(bridge._recent_log)
        CONFIG["log_requests"] = True
        bridge._log("test message for deque")
        new = list(bridge._recent_log)
        # Phải có ít nhất 1 entry mới hơn
        self.assertGreater(len(new), len(old))
        self.assertTrue(any("test message for deque" in s for s in new))

    def test_server_logs_appear_in_recent_log(self):
        """Log từ server.py và qwen.py phải được lưu vào recent_log."""
        CONFIG["log_requests"] = True
        CONFIG["debug"] = True
        qwen.log("server test log event")
        qwen.debug_log("server test debug event")
        logs = qwen.get_recent_log()
        self.assertTrue(any("server test log event" in s for s in logs))
        self.assertTrue(any("server test debug event" in s for s in logs))

    def test_debug_endpoint_forbidden_when_debug_off(self):
        """GET /debug trả 403 khi debug=false."""
        CONFIG["debug"] = False
        srv = bridge._Server(("127.0.0.1", 0), bridge._Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/debug")
            r = conn.getresponse()
            self.assertEqual(r.status, 403)
            body = json.loads(r.read())
            self.assertIn("debug mode", body.get("error", ""))
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(2)

    def test_debug_endpoint_returns_state_when_enabled(self):
        """GET /debug trả config + bridge state khi debug=true."""
        CONFIG["debug"] = True
        CONFIG["log_requests"] = True
        bridge._last_seen = time.time()
        srv = bridge._Server(("127.0.0.1", 0), bridge._Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/debug")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            data = json.loads(r.read())
            self.assertIn("config", data)
            self.assertIn("bridge", data)
            self.assertIn("recent_log", data)
            self.assertIn("debug", data["config"])
            self.assertTrue(data["config"]["debug"])
            self.assertIn("worker_alive", data["bridge"])
            self.assertIn("active_jobs", data["bridge"])
            self.assertIn("known_models_count", data["bridge"])
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(2)

    def test_debug_endpoint_safe_keys_only(self):
        """GET /debug không expose api_keys hay cookie."""
        CONFIG["debug"] = True
        CONFIG["log_requests"] = True
        CONFIG["api_keys"] = ["secret_key_123"]
        bridge._last_seen = time.time()
        srv = bridge._Server(("127.0.0.1", 0), bridge._Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/debug")
            r = conn.getresponse()
            data = json.loads(r.read())
            self.assertNotIn("api_keys", data.get("config", {}))
            # secret không lộ trong recent_log
            self.assertFalse(
                any("secret_key_123" in s for s in data.get("recent_log", [])))
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(2)

    def test_generate_stream_emitted_raise_logs_debug(self):
        """generate_stream raise sau partial stream ghi debug log."""
        events = [
            {"kind": "delta", "text": "partial"},
            {"kind": "error", "message": "mid-stream boom"},
        ]
        pending = _fake_pending(events)
        CONFIG["debug"] = True
        CONFIG["log_requests"] = True
        logged = []
        orig_debug_log = bridge._debug_log
        bridge._debug_log = lambda msg: logged.append(msg)
        try:
            with mock.patch.object(bridge, "worker_alive", return_value=True), \
                    mock.patch.object(bridge, "_pending", pending):
                with self.assertRaises(qwen.QwenError):
                    list(bridge.generate_stream("hi", "m", False))
            # Phải có log về partial stream raise
            self.assertTrue(any("partial" in s or "raising" in s for s in logged),
                            f"Expected partial-stream debug log, got: {logged}")
        finally:
            bridge._debug_log = orig_debug_log


# --------------------------------------------- server large prompt log ----
class TestServerLargePromptLog(_Sandbox):

    def test_prepare_large_prompt_logs_split(self):
        """prepare_large_prompt split path ghi debug_log."""
        CONFIG["debug"] = True
        CONFIG["log_requests"] = True
        logged = []
        with mock.patch("qwen_web2api.server.debug_log",
                        side_effect=lambda m: logged.append(m)):
            long_history = "X" * 135000
            last_msg = {"role": "user", "content": "Y" * 100}
            messages = [{"role": "user", "content": long_history}, last_msg]
            fi, _msgs = server.prepare_large_prompt(messages)
        try:
            # Phải có log về split
            self.assertTrue(any("split" in s for s in logged),
                            f"Expected split debug log, got: {logged}")
        finally:
            if fi and fi.get("path"):
                try:
                    os.unlink(fi["path"])
                except OSError:
                    pass

    def test_prepare_large_prompt_logs_fallback_reason(self):
        """prepare_large_prompt fallback (1 message) ghi log lý do."""
        CONFIG["log_requests"] = True
        logged = []
        with mock.patch("qwen_web2api.server.log",
                        side_effect=lambda m: logged.append(m)):
            long_single = {"role": "user", "content": "Z" * 140000}
            fi, _msgs = server.prepare_large_prompt([long_single])
        try:
            # Phải log "cannot split" kèm lý do
            self.assertTrue(any("cannot split" in s for s in logged),
                            f"Expected fallback log, got: {logged}")
        finally:
            if fi and fi.get("path"):
                try:
                    os.unlink(fi["path"])
                except OSError:
                    pass

    def test_prepare_large_prompt_no_log_when_under_limit(self):
        """prepare_large_prompt không log gì khi prompt ngắn."""
        logged = []
        with mock.patch("qwen_web2api.server.log",
                        side_effect=lambda m: logged.append(m)), \
             mock.patch("qwen_web2api.server.debug_log",
                        side_effect=lambda m: logged.append(m)):
            fi, _msgs = server.prepare_large_prompt([
                {"role": "user", "content": "short"}
            ])
        self.assertIsNone(fi)
        self.assertEqual(logged, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
