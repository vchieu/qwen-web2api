"""Browser bridge: chay upstream call ben trong tab chat.qwen.ai that.

Ly do: endpoint completions duoc bao ve boi baxia. Header bx-ua do JS cua
trang ky cho TUNG request, x5sec het han nhanh -- Python khong tai tao duoc.
Chay fetch() trong trang dung session that + header anti-bot that.

  * OpenAI API van o CONFIG["port"] (8082) -- server.py import truc tiep
    generate/generate_stream tu module nay.
  * Module nay chay HTTP server thu hai o CONFIG["bridge_port"] (8083).
  * /requeue tra job ve hang doi khi extension phai navigate doi che do
    (temporary chat); /poll nhan them danh sach model de /v1/models merge.
"""
import collections
import json
import queue
import threading
import time
import uuid
import tempfile
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .config import CONFIG
from . import qwen

_ALLOWED_ORIGIN = "https://chat.qwen.ai"
_lock = threading.Lock()
_pending = queue.Queue()
_jobs = {}
_models = []
_last_seen = 0.0
# Reference to shared circular buffer in qwen.py
_recent_log = qwen._recent_log


def _log(msg):
    qwen.log("[bridge] " + msg)


def _debug_log(msg):
    if qwen.is_debug():
        _log("[debug] " + msg)


def worker_alive():
    return (time.time() - _last_seen) < float(CONFIG.get("bridge_worker_ttl", 20))


def temporary_chat_enabled():
    """Doc ca hai kieu key; `temporary_chat` duoc uu tien khi bat."""
    if CONFIG.get("temporary_chat"):
        return True
    return bool(CONFIG.get("temporary-chat"))


def known_models():
    """Ban sao danh sach model do extension bao ve (id + name)."""
    with _lock:
        return [dict(m) for m in _models]


def _remember_models(items):
    """Cap nhat danh sach model tu /poll; bo qua entry sai, giu toi da 200."""
    global _models
    clean = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        mid = mid.strip()
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            name = mid
        clean.append({"id": mid, "name": name.strip()})
        if len(clean) >= 200:
            break
    if clean:
        with _lock:
            _models = clean


def status():
    with _lock:
        n = len(_jobs)
        n_models = len(_models)
    return {"worker": worker_alive(), "pending": _pending.qsize(), "jobs": n,
            "temporary_chat": temporary_chat_enabled(),
            "select_model": bool(CONFIG.get("bridge_select_model", True)),
            "attachment_prompt": str(CONFIG.get("attachment_prompt", "Please read the text and action")),
            "debug": qwen.is_debug(),
            "models": n_models,
            "last_seen_ago": round(time.time() - _last_seen, 1)
            if _last_seen else None}


class _Tracker:
    """Gom delta; tu nhan dien incremental vs cumulative."""

    def __init__(self):
        self.total = {"content": "", "reasoning": ""}
        self.last = {"content": None, "reasoning": None}
        self.cumulative = None

    def feed(self, kind, text):
        prev = self.last[kind]
        if self.cumulative is None and prev is not None:
            self.cumulative = bool(text.startswith(prev) and len(text) > len(prev))
        if self.cumulative:
            base = self.total[kind]
            if text.startswith(base):
                delta = text[len(base):]
                self.total[kind] = text
            else:
                delta = text
                self.total[kind] = base + text
        else:
            delta = text
            self.total[kind] += text
        self.last[kind] = text
        return delta


def generate_stream(prompt, model, think, usage_out=None, file_info=None):
    """Yield (kind, text) -- interface server.py goi cho luong SSE.

    file_info (dict, optional): thông tin file tạm chứa prompt quá dài,
    bridge.js sẽ upload file này qua UI trước khi gửi message ngắn.
    """
    attempts = max(1, int(CONFIG.get("retry_attempts", 2)))
    timeout = float(CONFIG.get("bridge_job_timeout", 300))
    last_err = None
    for attempt in range(attempts):
        if not worker_alive():
            raise qwen.QwenError(
                "browser bridge: chua co tab chat.qwen.ai ket noi. Mo "
                "https://chat.qwen.ai (da dang nhap), giu tab mo va bat "
                "extension. Kiem tra: curl http://127.0.0.1:"
                f"{CONFIG.get('bridge_port', 8083)}/status", status=503)
        jid = uuid.uuid4().hex
        q = queue.Queue()
        with _lock:
            _jobs[jid] = q
        if file_info and file_info.get("attachment_prompt"):
            # Lượt cuối của user được tách riêng ra: dùng prompt (chứa tin nhắn cuối + tool/rf/nudge nếu có)
            att_prompt = str(prompt or file_info["attachment_prompt"])
        else:
            # Fallback (tin nhắn đơn quá dài hoặc không phải user): dùng attachment_prompt cấu hình,
            # không dùng prompt vì prompt fallback chứa "[Attached file: ...]" dễ khiến model gọi nhầm tool đọc file.
            att_prompt = str(CONFIG.get("attachment_prompt", "Please read the text and action"))
        _debug_log(f"queueing job {jid} (model={model}, prompt_len={len(prompt or '')}, file={bool(file_info)})")
        _pending.put({"job_id": jid, "model": model, "prompt": prompt,
                      "think": bool(think), "ts": time.time(),
                      "file_info": file_info,
                      "attachment_prompt": att_prompt,
                      "managed": True})
        tracker = _Tracker()
        emitted = False
        try:
            while True:
                try:
                    ev = q.get(timeout=timeout)
                except queue.Empty:
                    _log(f"[orphan] job {jid} timed out after {timeout}s without response from browser tab")
                    raise qwen.QwenError(
                        "browser bridge: tab khong tra ket qua (timeout)",
                        status=504)
                kind = ev.get("kind")
                if kind == "delta":
                    text = ev.get("text") or ""
                    k = "reasoning" if ev.get("phase") == "reasoning" else "content"
                    if text:
                        d = tracker.feed(k, text)
                        if d:
                            emitted = True
                            yield k, d
                elif kind == "tool_call":
                    # Native function_call từ Qwen (extension bắt được) ->
                    # chuyển thành text fence để server.parse_tool_calls xử lý.
                    if "# Tool Use" not in (prompt or ""):
                        continue
                    name = ev.get("name") or ""
                    args = ev.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args, strict=False)
                        except ValueError:
                            pass
                    if not isinstance(args, (dict, str)):
                        args = "{}"
                    if isinstance(args, str) and not args.strip():
                        args = "{}"
                    if name:
                        block = json.dumps({"name": name, "arguments": args},
                                           ensure_ascii=False)
                        d = tracker.feed("content", f"\n```tool_call\n{block}\n```\n")
                        if d:
                            emitted = True
                            yield "content", d
                elif kind == "usage":
                    if usage_out is not None and isinstance(ev.get("usage"), dict):
                        usage_out.update(ev["usage"])
                elif kind in ("debug", "log"):
                    _debug_log("extension: " + str(ev.get("message") or ""))
                elif kind == "warn":
                    # Extension khong lam duoc thu gi do (model picker...) nhung
                    # request van chay tiep; chi ghi log de debug.
                    _log("extension warn: " + str(ev.get("message") or ""))
                elif kind == "error":
                    raise qwen.QwenError(
                        "browser bridge: " + str(ev.get("message") or "unknown"),
                        status=502)
                elif kind == "done":
                    if not tracker.total["content"] and not tracker.total["reasoning"]:
                        raise qwen.QwenError(
                            "browser bridge: upstream khong tra ve text nao",
                            status=502)
                    _debug_log(f"job {jid} completed successfully ({len(tracker.total['content'])} content, {len(tracker.total['reasoning'])} reasoning)")
                    return
        except Exception as e:
            last_err = e if isinstance(e, qwen.QwenError) else qwen.QwenError(str(e))
            if emitted:
                _debug_log(f"job {jid} raising after partial stream: {last_err}")
                raise last_err
            if attempt < attempts - 1:
                _log(f"retry {attempt + 1}/{attempts}: {last_err}")
                time.sleep(CONFIG.get("retry_delay_sec", 2))
        finally:
            with _lock:
                _jobs.pop(jid, None)
    raise last_err


def generate(prompt, model, think, file_info=None):
    t0 = time.monotonic()
    usage, content, reasoning = {}, [], []
    for kind, text in generate_stream(prompt, model, think, usage, file_info=file_info):
        (reasoning if kind == "reasoning" else content).append(text)
    out = {"content": "".join(content), "reasoning": "".join(reasoning),
           "usage": usage}
    qwen.log(f"[timing] bridge generate: {time.monotonic() - t0:.1f}s, "
             f"{len(out['content'])} chars (+{len(out['reasoning'])} reasoning)")
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _origin_ok(self):
        origin = self.headers.get("Origin", "")
        return origin == _ALLOWED_ORIGIN or origin.startswith("chrome-extension://")

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin == _ALLOWED_ORIGIN or origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        # Serve file prompt tạm được tạo bởi server.py khi prompt quá dài
        if path.startswith("/file/"):
            filename = os.path.basename(path[len("/file/"):])
            if not filename.startswith("qwen_prompt_"):
                return self._json({"error": "invalid file"}, 400)

            tmpdir = os.path.abspath(tempfile.gettempdir())
            filepath = os.path.join(tmpdir, filename)

            # Chỉ serve khi file nằm trực tiếp trong tmpdir
            if (os.path.exists(filepath)
                    and os.path.dirname(os.path.abspath(filepath)) == tmpdir):
                try:
                    with open(filepath, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(content)
                    # Khong xoa tai day: server co the retry cung file_info
                    # (tool retry), va server.py tu don file sau khi luot goi xong.
                    return
                except Exception as e:
                    _log(f"file serve error: {e}")
            self._json({"error": "not found"}, 404)
            return

        if path == "/status":
            return self._json(status())
        if path == "/debug":
            if not qwen.is_debug():
                return self._json({"error": "debug mode not enabled; set \"debug\": true in config"}, 403)
            with _lock:
                n_jobs = len(_jobs)
                n_models = len(_models)
            safe_keys = ("debug", "log_requests", "temporary_chat", "temporary-chat",
                         "bridge_select_model", "default_model", "retry_attempts",
                         "bridge_job_timeout", "bridge_poll_wait", "bridge_worker_ttl",
                         "attachment_prompt", "log_file", "bridge_port", "port")
            cfg_snapshot = {k: CONFIG[k] for k in safe_keys if k in CONFIG}
            return self._json({
                "config": cfg_snapshot,
                "bridge": {
                    "worker_alive": worker_alive(),
                    "last_seen_ago": round(time.time() - _last_seen, 1) if _last_seen else None,
                    "pending_jobs": _pending.qsize(),
                    "active_jobs": n_jobs,
                    "known_models_count": n_models,
                    "temporary_chat": temporary_chat_enabled(),
                },
                "recent_log": qwen.get_recent_log(),
            })
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        global _last_seen
        path = self.path.split("?")[0].rstrip("/")
        if not self._origin_ok():
            return self._json({"error": "forbidden origin"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 8 * 1024 * 1024:
            return self._json({"error": "body too large"}, 413)
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "invalid JSON"}, 400)
        _last_seen = time.time()

        if isinstance(data.get("models"), list):
            _remember_models(data["models"])

        if path == "/requeue":
            job = data.get("job")
            if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) \
                    or not job["job_id"].strip():
                return self._json({"error": "invalid job"}, 400)
            job = dict(job)
            job["ts"] = time.time()
            _pending.put(job)
            _log(f"requeued job {job['job_id']}")
            return self._json({"ok": True})

        if path == "/poll":
            worker = data.get("worker", "unknown")
            deadline = time.time() + float(CONFIG.get("bridge_poll_wait", 20))
            ttl = float(CONFIG.get("bridge_job_timeout", 300))
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    _debug_log(f"poll: no job after {CONFIG.get('bridge_poll_wait', 20)}s (queue empty)")
                    return self._json({"job": None})
                try:
                    job = _pending.get(timeout=remaining)
                except queue.Empty:
                    _debug_log(f"poll: no job after {CONFIG.get('bridge_poll_wait', 20)}s (queue empty)")
                    return self._json({"job": None})

                jid = job.get("job_id")
                age = time.time() - job.get("ts", 0)
                if age > ttl:
                    _log(f"[orphan] dropping expired job {jid} (queued {age:.1f}s > ttl {ttl}s)")
                    continue

                if job.get("managed"):
                    with _lock:
                        is_active = jid in _jobs
                    if not is_active:
                        _log(f"[orphan] dropping orphan job {jid} (caller disconnected or cancelled)")
                        continue

                _debug_log(f"worker '{worker}' polled job {jid} (model={job.get('model')}, age={age:.1f}s)")
                return self._json({"job": job})

        if path == "/event":
            jid = data.get("job_id")
            kind = data.get("kind")
            with _lock:
                q = _jobs.get(jid)
            if q is not None:
                q.put(data)
            else:
                # Job khong con trong _jobs (da ket thuc hoac tab reload sau khi job da settled/timed out)
                if kind not in ("ping",):
                    msg = data.get("message") or ""
                    if jid in (None, "", "none"):
                        _log(f"extension warn: {msg}")
                    elif data.get("tab_reloaded") or data.get("tab_reloading"):
                        _log(f"extension warn: {msg}")
                    elif kind == "warn":
                        _log(f"extension warn (orphan job {jid}): {msg}")
                    else:
                        _debug_log(f"event '{kind}' for unknown/orphan job {jid}: {msg}")
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server():
    port = int(CONFIG.get("bridge_port", 8083))
    srv = _Server(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _log(f"listening on http://127.0.0.1:{port}")
    return srv