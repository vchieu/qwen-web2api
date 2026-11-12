"""OpenAI-compatible HTTP server for Qwen Web."""
import hmac
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from . import __version__
from .config import CONFIG
from .models import MODELS, resolve_model
from .qwen import QwenError, generate, generate_stream, log, upstream_models
from .tools import (build_response_format_instruction, is_required_tool_choice,
                    looks_like_missed_tool_call, messages_to_prompt,
                    missing_required_params, parse_tool_calls, strip_code_fence,
                    tool_names, tool_parameters, tool_required_params)

ERR_INVALID = "invalid_request_error"
ERR_API = "api_error"
ERR_RATE = "rate_limit_error"
SSE_PING_INTERVAL = 5


def _usage(prompt, text, upstream=None):
    if isinstance(upstream, dict) and upstream:
        p = upstream.get("prompt_tokens", upstream.get("input_tokens"))
        c = upstream.get("completion_tokens", upstream.get("output_tokens"))
        if isinstance(p, int) and isinstance(c, int):
            return {"prompt_tokens": p, "completion_tokens": c,
                    "total_tokens": p + c}
    p, c = len(prompt) // 4, len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _map_error(e):
    status = getattr(e, "status", None)
    if status == 429:
        return 429, f"upstream rate limited: {e}", ERR_RATE, "rate_limit_exceeded"
    return 503, f"upstream error: {e}", ERR_API, None


def _normalize_stop(stop):
    if isinstance(stop, str):
        return [stop] if stop else []
    if isinstance(stop, list):
        return [s for s in stop if isinstance(s, str) and s]
    return []


def _apply_stop(text, stops):
    if not text or not stops:
        return text
    cut = None
    for s in stops:
        i = text.find(s)
        if i != -1 and (cut is None or i < cut):
            cut = i
    return text[:cut] if cut is not None else text


def _apply_max_tokens(text, max_tokens):
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        return text, False
    limit = max_tokens * 4
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _text_format(text_field):
    """Responses API text.format -> Chat response_format."""
    if not isinstance(text_field, dict):
        return None
    fmt = text_field.get("format")
    if not isinstance(fmt, dict):
        return None
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") == "json_schema":
        if isinstance(fmt.get("json_schema"), dict):
            return {"type": "json_schema", "json_schema": fmt["json_schema"]}
        return {"type": "json_schema",
                "json_schema": {"schema": fmt.get("schema", {})}}
    return None


def _norm_resp_tools(tools):
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({"type": "function", "function": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }})
    return out or None


def _responses_messages(inp, instructions):
    msgs = []
    if instructions:
        msgs.append({"role": "system", "content": instructions})
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
        return msgs
    for item in inp or []:
        if isinstance(item, str):
            msgs.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "function_call_output":
            out = item.get("output", "")
            if isinstance(out, list):
                out = "\n".join(c.get("text", "") for c in out
                                if isinstance(c, dict)
                                and c.get("type") in ("output_text", "text"))
            msgs.append({"role": "tool",
                         "tool_call_id": item.get("call_id", ""),
                         "content": out})
        elif t == "function_call":
            call = {"id": item.get("call_id") or item.get("id")
                          or f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": item.get("name", ""),
                                 "arguments": item.get("arguments") or "{}"}}
            if msgs and msgs[-1].get("role") == "assistant" \
                    and msgs[-1].get("tool_calls"):
                msgs[-1]["tool_calls"].append(call)
            else:
                msgs.append({"role": "assistant", "content": None,
                             "tool_calls": [call]})
        elif t == "input_text":
            msgs.append({"role": "user", "content": item.get("text", "")})
        elif item.get("role"):
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            content = item.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict)
                    and c.get("type") in ("input_text", "output_text", "text"))
            msgs.append({"role": role, "content": content})
    return msgs


class _Heartbeat:
    """SSE keep-alive while a buffered turn works upstream."""

    def __init__(self, handler, stop, gone):
        self.h, self.stop, self.gone = handler, stop, gone
        self.lock = threading.Lock()

    def ping(self):
        with self.lock:
            if self.stop.is_set():
                return False
            try:
                self.h._sse_write(b": ping\n\n")
                return True
            except (BrokenPipeError, ConnectionResetError):
                self.gone.set()
                return False
            except Exception:
                return False

    def run(self):
        while not self.stop.wait(SSE_PING_INTERVAL):
            if not self.ping():
                return


class QwenHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log(f"{self.client_address[0]} {fmt % args}")

    # ------------------------------------------------------------ helpers --
    def _route(self):
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        provided = []
        auth = (self.headers.get("Authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            provided.append(auth[7:].strip())
        for h in ("x-api-key", "x-goog-api-key"):
            v = self.headers.get(h)
            if v:
                provided.append(v.strip())
        for pair in urllib.parse.urlparse(self.path).query.split("&"):
            if pair.startswith("key="):
                provided.append(urllib.parse.unquote(pair[4:]))
        return any(isinstance(k, str) and hmac.compare_digest(v.encode(), k.encode())
                   for v in provided for k in keys)

    def _cors(self):
        origins = CONFIG.get("cors_origins") or []
        if not origins:
            return
        req_origin = self.headers.get("Origin", "")
        origin = "*" if "*" in origins else (
            req_origin if req_origin in origins else "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, x-api-key")

    def _security_ok(self):
        if CONFIG.get("api_keys"):
            return True
        host = self.headers.get("Host", "")
        if host:
            hn = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
            allowed = {"localhost", "127.0.0.1", "::1"}
            bind = (CONFIG.get("host") or "").lower().strip("[]")
            if bind and bind not in ("0.0.0.0", "::", "*"):
                allowed.add(bind)
            if hn and hn not in allowed:
                return False
        origin = self.headers.get("Origin")
        origins = CONFIG.get("cors_origins") or []
        if origin and "*" not in origins and origin not in origins:
            return False
        return True

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status, message, type_=ERR_API, code=None):
        self.send_json({"error": {"message": message, "type": type_,
                                  "param": None, "code": code}}, status)

    def _sse_write(self, data):
        lock = getattr(self, "_sse_lock", None)
        if lock is None:
            self.wfile.write(data)
            self.wfile.flush()
            return
        with lock:
            self.wfile.write(data)
            self.wfile.flush()

    def _start_sse(self):
        self._sse_lock = threading.Lock()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        stop, gone = threading.Event(), threading.Event()
        hb = _Heartbeat(self, stop, gone)
        threading.Thread(target=hb.run, daemon=True).start()
        return stop.set, gone, hb

    def _sse_chunk(self, cid, model, delta, finish, include_usage=False):
        chunk = {"id": cid, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": model,
                 "choices": [{"index": 0, "delta": delta, "logprobs": None,
                              "finish_reason": finish}]}
        if include_usage:
            chunk["usage"] = None
        self._sse_write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

    def _sse_usage(self, cid, model, prompt, text, upstream):
        chunk = {"id": cid, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": model, "choices": [],
                 "usage": _usage(prompt, text, upstream)}
        self._sse_write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

    def _sse_error(self, status, message, type_=ERR_API, code=None):
        payload = {"error": {"message": message, "type": type_,
                             "param": None, "code": code}}
        self._sse_write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())

    def _sse_done(self):
        self._sse_write(b"data: [DONE]\n\n")

    def _parse(self, body):
        try:
            return json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return None

    def _read_body(self):
        limit = int(CONFIG.get("max_body_bytes", 32 * 1024 * 1024))
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        t = self.rfile.readline(65536)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > limit:
                    raise ValueError("request body too large")
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("truncated request body")
                chunks.append(data)
                cr = self.rfile.read(1)
                if cr == b"\r":
                    self.rfile.read(1)
            return b"".join(chunks)
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise ValueError("invalid Content-Length header")
        if n < 0:
            raise ValueError("invalid Content-Length header")
        if n > limit:
            raise ValueError("request body too large")
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise ValueError("incomplete request body")
        return data

    def _resolve(self, req):
        raw = req.get("model")
        base = raw.split("@think=")[0] if isinstance(raw, str) else raw
        if (CONFIG.get("strict_models") and isinstance(base, str) and base
                and base not in MODELS
                and not any(m["id"] == base for m in upstream_models())):
            self.send_api_error(404, f"model '{raw}' not found",
                                ERR_INVALID, "model_not_found")
            return None
        name, upstream, think, err = resolve_model(
            raw or CONFIG["default_model"], CONFIG["default_model"],
            bool(CONFIG.get("thinking")))
        if err:
            self.send_api_error(400, err, ERR_INVALID)
            return None
        effort = req.get("reasoning_effort")
        if not isinstance(effort, str) and isinstance(req.get("reasoning"), dict):
            effort = req["reasoning"].get("effort")
        if isinstance(effort, str):
            e = effort.strip().lower()
            if e in ("minimal", "low", "none"):
                think = False
            elif e in ("medium", "high"):
                think = True
        echo = raw if isinstance(raw, str) and raw else name
        log(f"[model] {echo} -> upstream={upstream} thinking={think}")
        return echo, upstream, think

    # ---------------------------------------------------------- tool turn --
    def _run_turn(self, prompt, upstream, think, tools_active, allowed,
                  schemas, required_params, required_tool, client_gone=None):
        attempts = 1
        if required_tool or (tools_active and CONFIG.get("tool_retry_on_miss")):
            attempts = 1 + max(0, int(CONFIG.get("tool_retry_attempts", 1)))
        text = reasoning = ""
        calls, usage, nudge = None, {}, ""
        for attempt in range(attempts):
            if attempt and client_gone is not None and client_gone.is_set():
                log("tool retry skipped: client disconnected")
                break
            call_prompt = prompt + nudge if nudge else prompt
            result = generate(call_prompt, upstream, think)
            text, reasoning, usage = (result["content"], result["reasoning"],
                                      result["usage"])
            calls = None
            if tools_active and text:
                text, calls, _dropped = parse_tool_calls(text, allowed, schemas)
            if calls:
                missing = missing_required_params(calls, required_params)
                if missing and attempt + 1 < attempts:
                    nudge = ("\n\nIMPORTANT: Your previous tool call was missing "
                             "required parameter(s): " + ", ".join(missing)
                             + ". Emit the tool_call again with every required "
                             "parameter.")
                    continue
                if missing:
                    raise QwenError("model emitted invalid tool call; missing: "
                                    + ", ".join(missing))
                return text, reasoning, calls, usage
            if required_tool:
                nudge = "\n\nIMPORTANT: Respond with a tool_call block ONLY."
                continue
            if attempt + 1 >= attempts:
                break
            if tools_active and (looks_like_missed_tool_call(text)
                                 or not text.strip()):
                nudge = ("\n\nIMPORTANT: Do not describe a tool call -- make it. "
                         "If answering requires reading or changing something, "
                         "call the tool now; otherwise give your final answer.")
                continue
            break
        if required_tool and not calls:
            raise QwenError("model did not emit a usable required tool call; "
                            "please retry")
        return text, reasoning, calls, usage

    # ------------------------------------------------------------- routes --
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        try:
            path = self._route()
            if path not in ("/", "/health") and not self._authorized():
                return self.send_api_error(401, "invalid api key",
                                           ERR_INVALID, "invalid_api_key")
            if path in ("/", "/health"):
                self.send_json({"status": "ok", "version": __version__})
            elif path in ("/v1/models", "/models"):
                seen, data = set(), []
                for n, c in MODELS.items():
                    seen.add(n)
                    data.append({"id": n, "object": "model",
                                 "created": 1700000000, "owned_by": "qwen",
                                 "description": c.get("desc", "")})
                for m in upstream_models():
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        data.append({"id": m["id"], "object": "model",
                                     "created": 1700000000, "owned_by": "qwen",
                                     "description": m.get("desc") or m.get("name", "")})
                self.send_json({"object": "list", "data": data})
            elif path.startswith("/v1/models/") or path.startswith("/models/"):
                mid = path.rsplit("/", 1)[-1]
                if mid in MODELS or any(m["id"] == mid for m in upstream_models()):
                    self.send_json({"id": mid, "object": "model",
                                     "created": 1700000000, "owned_by": "qwen"})
                else:
                    self.send_api_error(404, f"model '{mid}' not found",
                                        ERR_INVALID, "model_not_found")
            else:
                self.send_api_error(404, f"not found: {path}", ERR_INVALID)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")
            try:
                self.send_api_error(500, str(e))
            except Exception:
                pass

    def do_POST(self):
        try:
            path = self._route()
            if not self._authorized():
                return self.send_api_error(401, "invalid api key",
                                           ERR_INVALID, "invalid_api_key")
            if not self._security_ok():
                return self.send_api_error(403, "forbidden host/origin", ERR_INVALID)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_api_error(415, "Content-Type must be application/json",
                                           ERR_INVALID)
            body = self._read_body()
            if path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat(body)
            elif path in ("/v1/responses", "/responses"):
                self._handle_responses(body)
            else:
                self.send_api_error(404, f"not found: {path}", ERR_INVALID)
        except ValueError as e:
            self.send_api_error(413 if "too large" in str(e) else 400,
                                str(e), ERR_INVALID)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_api_error(500, str(e))
            except Exception:
                pass

    # ---------------------------------------------------- chat completions --
    def _handle_chat(self, body):
        req = self._parse(body)
        if not isinstance(req, dict):
            return self.send_api_error(400, "invalid JSON body", ERR_INVALID)
        resolved = self._resolve(req)
        if not resolved:
            return
        echo, upstream, think = resolved
        messages = req.get("messages", [])
        if not isinstance(messages, list):
            return self.send_api_error(400, "messages must be a list", ERR_INVALID)
        n = req.get("n", 1)
        if isinstance(n, int) and n > 1:
            return self.send_api_error(400, "n>1 is not supported",
                                       ERR_INVALID, "unsupported_value")
        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt = messages_to_prompt(messages, tools, tool_choice)
        rf = build_response_format_instruction(req.get("response_format"))
        if rf:
            prompt += rf
        if not prompt.strip():
            return self.send_api_error(400, "empty prompt", ERR_INVALID)
        stream = bool(req.get("stream"))
        stops = _normalize_stop(req.get("stop"))
        max_tokens = req.get("max_tokens") or req.get("max_completion_tokens")
        include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        tools_active = bool(tools) and tool_choice != "none"
        required_tool = is_required_tool_choice(tool_choice)
        allowed = tool_names(tools) or None
        schemas = tool_parameters(tools) or None
        required_params = tool_required_params(tools) or None

        if stream and not rf and not tools_active:
            return self._stream_chat(cid, echo, prompt, upstream, think,
                                     stops, max_tokens, include_usage)

        stop_ping = gone = None
        if stream:
            stop_ping, gone, _hb = self._start_sse()
            self._sse_chunk(cid, echo, {"role": "assistant", "content": ""},
                            None, include_usage)
        try:
            text, reasoning, calls, usage = self._run_turn(
                prompt, upstream, think, tools_active, allowed, schemas,
                required_params, required_tool, gone)
        except Exception as e:
            if stream:
                try:
                    self._sse_error(*_map_error(e))
                    self._sse_done()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if stop_ping:
                        stop_ping()
                return
            return self.send_api_error(*_map_error(e))
        finally:
            if stop_ping:
                stop_ping()

        if rf and text:
            text = strip_code_fence(text)
        text = _apply_stop(text, stops)
        text, truncated = _apply_max_tokens(text, max_tokens)
        if not text and not calls and not reasoning:
            if stream:
                try:
                    self._sse_error(503, "empty response from upstream", ERR_API)
                    self._sse_done()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self.send_api_error(503, "empty response from upstream", ERR_API)
        msg = {"role": "assistant", "content": text or None, "refusal": None}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if calls:
            msg["tool_calls"] = calls
        finish = "tool_calls" if calls else ("length" if truncated else "stop")
        if stream:
            try:
                if reasoning:
                    self._sse_chunk(cid, echo, {"reasoning_content": reasoning},
                                    None, include_usage)
                if text:
                    self._sse_chunk(cid, echo, {"content": text}, None, include_usage)
                for i, tc in enumerate(calls or []):
                    self._sse_chunk(cid, echo,
                                    {"tool_calls": [{"index": i, **tc}]},
                                    None, include_usage)
                self._sse_chunk(cid, echo, {}, finish, include_usage)
                if include_usage:
                    self._sse_usage(cid, echo, prompt, text, usage)
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_json({
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": echo,
            "choices": [{"index": 0, "message": msg, "logprobs": None,
                         "finish_reason": finish}],
            "usage": _usage(prompt, text, usage),
        })

    def _stream_chat(self, cid, model, prompt, upstream, think,
                     stops, max_tokens, include_usage):
        stop_ping, _gone, _hb = self._start_sse()
        self._sse_chunk(cid, model, {"role": "assistant", "content": ""},
                        None, include_usage)
        full, finish, buf = "", "stop", ""
        hold = max((len(s) for s in stops), default=0)
        gen = generate_stream(prompt, upstream, think)
        try:
            for kind, delta in gen:
                if kind == "reasoning":
                    if delta:
                        self._sse_chunk(cid, model,
                                        {"reasoning_content": delta},
                                        None, include_usage)
                    continue
                if not delta:
                    continue
                buf += delta
                hit = None
                for s in stops:
                    i = buf.find(s)
                    if i != -1 and (hit is None or i < hit):
                        hit = i
                if hit is not None:
                    piece = buf[:hit]
                    if piece:
                        full += piece
                        self._sse_chunk(cid, model, {"content": piece},
                                        None, include_usage)
                    buf = ""
                    break
                if max_tokens and (len(full) + len(buf)) > max_tokens * 4:
                    allowed = max(0, max_tokens * 4 - len(full))
                    piece = buf[:allowed]
                    if piece:
                        full += piece
                        self._sse_chunk(cid, model, {"content": piece},
                                        None, include_usage)
                    buf = ""
                    finish = "length"
                    break
                if len(buf) > hold:
                    piece = buf[:len(buf) - hold] if hold else buf
                    buf = buf[-hold:] if hold else ""
                    if piece:
                        full += piece
                        self._sse_chunk(cid, model, {"content": piece},
                                        None, include_usage)
            else:
                if buf:
                    full += buf
                    self._sse_chunk(cid, model, {"content": buf},
                                    None, include_usage)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            log(f"stream error: {e}")
            try:
                self._sse_error(*_map_error(e))
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        finally:
            stop_ping()
            close = getattr(gen, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        self._sse_chunk(cid, model, {}, finish, include_usage)
        if include_usage:
            self._sse_usage(cid, model, prompt, full, None)
        self._sse_done()

    # ---------------------------------------------------------- responses --
    def _handle_responses(self, body):
        req = self._parse(body)
        if not isinstance(req, dict):
            return self.send_api_error(400, "invalid JSON body", ERR_INVALID)
        resolved = self._resolve(req)
        if not resolved:
            return
        echo, upstream, think = resolved
        tools = _norm_resp_tools(req.get("tools"))
        messages = _responses_messages(req.get("input", []), req.get("instructions"))
        tool_choice = req.get("tool_choice") or "auto"
        prompt = messages_to_prompt(messages, tools, tool_choice)
        rf = req.get("response_format") or _text_format(req.get("text"))
        rfi = build_response_format_instruction(rf)
        if rfi:
            prompt += rfi
        if not prompt.strip():
            return self.send_api_error(400, "empty input", ERR_INVALID)
        tools_active = bool(tools) and tool_choice != "none"
        required_tool = is_required_tool_choice(tool_choice)
        allowed = tool_names(tools) or None
        schemas = tool_parameters(tools) or None
        required_params = tool_required_params(tools) or None
        stream = bool(req.get("stream"))
        rid = f"resp_{uuid.uuid4().hex[:16]}"
        created = int(time.time())
        seq = 0

        def emit(evt, **fields):
            nonlocal seq
            seq += 1
            obj = {"type": evt, "sequence_number": seq, **fields}
            self._sse_write(
                f"event: {evt}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())

        def resp_obj(status, output, usage):
            return {"id": rid, "object": "response", "created_at": created,
                    "status": status, "model": echo, "output": output,
                    "error": None,
                    "instructions": req.get("instructions")
                    if isinstance(req.get("instructions"), str) else None,
                    "tools": req.get("tools")
                    if isinstance(req.get("tools"), list) else [],
                    "tool_choice": tool_choice,
                    "metadata": req.get("metadata")
                    if isinstance(req.get("metadata"), dict) else None,
                    "parallel_tool_calls": True,
                    "temperature": req.get("temperature", 1),
                    "top_p": req.get("top_p", 1), "usage": usage}

        stop_ping = gone = None
        if stream:
            stop_ping, gone, _hb = self._start_sse()
            emit("response.created", response=resp_obj("in_progress", [], None))
            emit("response.in_progress", response=resp_obj("in_progress", [], None))
        try:
            text, _reasoning, calls, usage = self._run_turn(
                prompt, upstream, think, tools_active, allowed, schemas,
                required_params, required_tool, gone)
        except Exception as e:
            if stream:
                try:
                    failed = resp_obj("failed", [], None)
                    failed["error"] = {"message": str(e)}
                    emit("response.failed", response=failed)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if stop_ping:
                        stop_ping()
                return
            return self.send_api_error(*_map_error(e))
        finally:
            if stop_ping:
                stop_ping()

        if rfi and text:
            text = strip_code_fence(text)
        text, truncated = _apply_max_tokens(text, req.get("max_output_tokens"))
        if not text and not calls:
            if stream:
                failed = resp_obj("failed", [], None)
                failed["error"] = {"message": "empty response from upstream"}
                try:
                    emit("response.failed", response=failed)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self.send_api_error(503, "empty response from upstream", ERR_API)

        output = []
        for tc in calls or []:
            output.append({"type": "function_call", "id": tc["id"],
                           "call_id": tc["id"], "name": tc["function"]["name"],
                           "arguments": tc["function"]["arguments"],
                           "status": "completed"})
        if text or not calls:
            output.append({"type": "message",
                           "id": f"msg_{uuid.uuid4().hex[:12]}",
                           "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text",
                                        "text": text or "", "annotations": []}]})
        u = _usage(prompt, text, usage)
        resp_usage = {"input_tokens": u["prompt_tokens"],
                      "output_tokens": u["completion_tokens"],
                      "total_tokens": u["total_tokens"]}
        status = "incomplete" if truncated else "completed"
        if not stream:
            return self.send_json(resp_obj(status, output, resp_usage))
        try:
            for oi, item in enumerate(output):
                if item["type"] == "function_call":
                    pending = {**item, "arguments": "", "status": "in_progress"}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    emit("response.function_call_arguments.delta",
                         item_id=item["id"], output_index=oi,
                         delta=item["arguments"])
                    emit("response.function_call_arguments.done",
                         item_id=item["id"], output_index=oi,
                         arguments=item["arguments"])
                    emit("response.output_item.done", output_index=oi, item=item)
                else:
                    pending = {**item, "status": "in_progress", "content": []}
                    emit("response.output_item.added", output_index=oi, item=pending)
                    for ci, part in enumerate(item["content"]):
                        f = {"item_id": item["id"], "output_index": oi,
                             "content_index": ci}
                        emit("response.content_part.added", **f,
                             part={"type": "output_text", "text": "",
                                   "annotations": []})
                        emit("response.output_text.delta", **f, delta=part["text"])
                        emit("response.output_text.done", **f, text=part["text"])
                        emit("response.content_part.done", **f, part=part)
                    emit("response.output_item.done", output_index=oi, item=item)
            emit("response.incomplete" if truncated else "response.completed",
                 response=resp_obj(status, output, resp_usage))
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True