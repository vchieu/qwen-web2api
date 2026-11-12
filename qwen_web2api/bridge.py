"""Browser bridge: chay upstream call ben trong tab chat.qwen.ai that.

Ly do: endpoint completions duoc bao ve boi baxia. Header bx-ua do JS cua
trang ky cho TUNG request, x5sec het han nhanh -- Python khong tai tao duoc.
Chay fetch() trong trang dung session that + header anti-bot that.

  * OpenAI API van o CONFIG["port"] (8082) -- server.py khong doi.
  * Module nay chay HTTP server thu hai o CONFIG["bridge_port"] (8083).
  * install() thay qwen.generate_stream/generate bang ban bridge.
"""
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .config import CONFIG
from . import qwen

_ALLOWED_ORIGIN = "https://chat.qwen.ai"
_lock = threading.Lock()
_pending = queue.Queue()
_jobs = {}
_last_seen = 0.0


def _log(msg):
    qwen.log("[bridge] " + msg)


def worker_alive():
    return (time.time() - _last_seen) < float(CONFIG.get("bridge_worker_ttl", 20))


def status():
    with _lock:
        n = len(_jobs)
    return {"worker": worker_alive(), "pending": _pending.qsize(), "jobs": n,
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


def generate_stream(prompt, model, think, usage_out=None):
    """Yield (kind, text) -- cung interface voi qwen.generate_stream."""
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
        _pending.put({"job_id": jid, "model": model, "prompt": prompt,
                      "think": bool(think), "ts": time.time()})
        tracker = _Tracker()
        emitted = False
        try:
            while True:
                try:
                    ev = q.get(timeout=timeout)
                except queue.Empty:
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
                elif kind == "usage":
                    if usage_out is not None and isinstance(ev.get("usage"), dict):
                        usage_out.update(ev["usage"])
                elif kind == "error":
                    raise qwen.QwenError(
                        "browser bridge: " + str(ev.get("message") or "unknown"),
                        status=502)
                elif kind == "done":
                    if not tracker.total["content"] and not tracker.total["reasoning"]:
                        raise qwen.QwenError(
                            "browser bridge: upstream khong tra ve text nao",
                            status=502)
                    return
        except Exception as e:
            last_err = e if isinstance(e, qwen.QwenError) else qwen.QwenError(str(e))
            if emitted:
                raise last_err
            if attempt < attempts - 1:
                _log(f"retry {attempt + 1}/{attempts}: {last_err}")
                time.sleep(CONFIG.get("retry_delay_sec", 2))
        finally:
            with _lock:
                _jobs.pop(jid, None)
    raise last_err


def generate(prompt, model, think):
    t0 = time.monotonic()
    usage, content, reasoning = {}, [], []
    for kind, text in generate_stream(prompt, model, think, usage):
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
        if self.path.split("?")[0].rstrip("/") == "/status":
            return self._json(status())
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

        if path == "/poll":
            deadline = time.time() + float(CONFIG.get("bridge_poll_wait", 20))
            ttl = float(CONFIG.get("bridge_job_timeout", 300))
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return self._json({"job": None})
                try:
                    job = _pending.get(timeout=remaining)
                except queue.Empty:
                    return self._json({"job": None})
                if time.time() - job.get("ts", 0) <= ttl:
                    return self._json({"job": job})

        if path == "/event":
            with _lock:
                q = _jobs.get(data.get("job_id"))
            if q is not None:
                q.put(data)
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


def install():
    """Thay ca ten trong qwen LAN trong server (server import truc tiep)."""
    from . import server as _server
    qwen.generate_stream = generate_stream
    qwen.generate = generate
    _server.generate_stream = generate_stream
    _server.generate = generate
    _log("installed: upstream calls now run inside the browser tab")