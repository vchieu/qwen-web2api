"""Qwen Chat (chat.qwen.ai) upstream client.

Payload/headers mirror a real browser request captured from DevTools.
Access tokens expire after ~15 minutes, so the refresh_token cookie is used
to mint a new one automatically and persist it back to token.json.
"""
import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .config import CONFIG

try:
    from curl_cffi import requests as _cffi
except Exception:
    _cffi = None


class QwenError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


_ssl_ctx = None


def _ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def log(msg):
    if not CONFIG.get("log_requests"):
        return
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    path = CONFIG.get("log_file")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _dump_raw(raw):
    path = CONFIG.get("debug_raw_file") or "qwen-raw.log"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                     f"{len(raw)} chars =====\n{raw}\n")
    except OSError:
        pass


def transport_name():
    if _cffi is not None and CONFIG.get("impersonate"):
        return f"curl_cffi (impersonate={CONFIG['impersonate']})"
    return "urllib (no impersonation)"


# --------------------------------------------------------------- credentials
_cred_cache = ("", "", 0.0)          # token, cookie, mtime
_cred_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresh_last_fail = 0.0
_refresh_path = None                 # remembered working refresh endpoint


def _read_credentials():
    """Raw (token, cookie) from CONFIG / token_file / env. No refresh."""
    global _cred_cache
    tok = (CONFIG.get("token") or os.environ.get("QWEN_TOKEN") or "").strip()
    cookie = (CONFIG.get("cookie") or "").strip()
    tf = CONFIG.get("token_file")
    if tf and os.path.exists(tf):
        try:
            mtime = os.path.getmtime(tf)
            with _cred_lock:
                c_tok, c_cookie, c_mtime = _cred_cache
                if mtime == c_mtime and c_tok:
                    tok, cookie = c_tok, c_cookie or cookie
                else:
                    with open(tf, encoding="utf-8-sig") as f:
                        raw = f.read().strip()
                    f_tok, f_cookie = "", ""
                    if raw.startswith("{"):
                        data = json.loads(raw)
                        f_tok = str(data.get("token") or "").strip()
                        f_cookie = str(data.get("cookie") or "").strip()
                    else:
                        f_tok = raw
                    tok = f_tok or tok
                    cookie = f_cookie or cookie
                    _cred_cache = (tok, cookie, mtime)
        except Exception as e:
            log(f"token load error: {e}")
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return tok, cookie


def _jwt_exp(tok):
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def _cookie_value(cookie, name):
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)", cookie or "")
    return m.group(1).strip() if m else ""


def _persist_credentials(tok, cookie):
    global _cred_cache
    tf = CONFIG.get("token_file") or "token.json"
    try:
        tmp = tf + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"token": tok, "cookie": cookie}, fh,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, tf)
        with _cred_lock:
            _cred_cache = (tok, cookie, os.path.getmtime(tf))
        CONFIG["token"], CONFIG["cookie"] = tok, cookie
    except Exception as e:
        log(f"could not persist refreshed token: {e}")


def _extract_token(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("token", "access_token", "jwt", "accessToken"):
        v = obj.get(key)
        if isinstance(v, str) and v.count(".") == 2:
            return v
    for key in ("data", "result"):
        nested = obj.get(key)
        if isinstance(nested, dict):
            got = _extract_token(nested)
            if got:
                return got
    return None


def refresh_access_token(tok, cookie, force=False):
    """Mint a new access token from the refresh_token cookie. None on failure."""
    global _refresh_last_fail, _refresh_path
    if not CONFIG.get("auto_refresh", True):
        return None
    refresh = _cookie_value(cookie, "refresh_token")
    if not refresh:
        return None
    with _refresh_lock:
        if not force and time.time() - _refresh_last_fail < 30:
            return None
        base = CONFIG["base_url"].rstrip("/")
        headers = _browser_headers(tok, cookie)
        headers["Accept"] = "application/json, text/plain, */*"
        paths = ([_refresh_path] if _refresh_path
                 else list(CONFIG.get("refresh_paths") or []))
        for path in paths:
            for method in ("POST", "GET"):
                try:
                    body = b"{}" if method == "POST" else None
                    r = _request(method, base + path, body, headers, timeout=30)
                    try:
                        raw = r.read().decode("utf-8", "replace")
                        if r.status >= 400:
                            continue
                        new = _extract_token(json.loads(raw))
                        if not new:
                            continue
                        set_cookie = r.header("set-cookie")
                        rotated = _cookie_value(set_cookie, "refresh_token")
                        if rotated:
                            cookie = re.sub(
                                r"(^|;\s*)refresh_token=[^;]*",
                                lambda m: (m.group(1) or "") + "refresh_token=" + rotated,
                                cookie)
                        _refresh_path = path
                        _persist_credentials(new, cookie)
                        exp = _jwt_exp(new)
                        log(f"[auth] access token refreshed via {method} {path}"
                            + (f" (expires in {exp - int(time.time())}s)" if exp else ""))
                        return new
                    finally:
                        r.close()
                except Exception as e:
                    log(f"[auth] refresh {method} {path} failed: {e}")
        _refresh_last_fail = time.time()
        log("[auth] token refresh failed on every candidate path -- capture the "
            "real refresh request in DevTools (filter 'refresh') and put its "
            "path into config refresh_paths")
        return None


def load_token():
    tok, cookie = _read_credentials()
    if not tok:
        return tok, cookie
    exp = _jwt_exp(tok)
    margin = int(CONFIG.get("refresh_margin_sec", 120))
    if exp and exp - time.time() < margin:
        new = refresh_access_token(tok, cookie)
        if new:
            tok, cookie = _read_credentials()
    return tok, cookie


# ------------------------------------------------------------------ headers
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


def _browser_headers(tok, cookie, chat_id=None):
    base = CONFIG["base_url"].rstrip("/")
    h = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "authorization": f"Bearer {tok}",
        "origin": base,
        "referer": base + (f"/c/{chat_id}" if chat_id else "/"),
        "source": "web",
        "timezone": time.strftime("%a %b %d %Y %H:%M:%S GMT%z"),
        "x-request-id": str(uuid.uuid4()),
        "x-accel-buffering": "no",
        "user-agent": CONFIG.get("user_agent") or _UA,
    }
    if CONFIG.get("bx_v"):
        h["bx-v"] = str(CONFIG["bx_v"])
    if CONFIG.get("bx_ua"):
        h["bx-ua"] = str(CONFIG["bx_ua"])
    if CONFIG.get("bx_umidtoken"):
        h["bx-umidtoken"] = str(CONFIG["bx_umidtoken"])
    ck = cookie or ""
    if "token=" not in ck:
        ck = (ck + "; " if ck else "") + f"token={tok}"
    h["cookie"] = ck
    for k, v in (CONFIG.get("extra_headers") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            h[k.lower()] = v
    return h


def _headers(chat_id=None):
    tok, cookie = load_token()
    if not tok:
        raise QwenError("no Qwen token configured (token / token_file)", status=401)
    return _browser_headers(tok, cookie, chat_id)


# ---------------------------------------------------------------- transport
class _Resp:
    def __init__(self, status, headers, body=None, lines=None, close=None):
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self._lines = lines
        self._close = close

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default) or default

    def read(self):
        if self._body is None:
            try:
                self._body = b"".join(self._lines or [])
            except Exception:
                self._body = b""
        return self._body

    def lines(self):
        return self._lines if self._lines is not None else iter(())

    def close(self):
        if self._close is not None:
            try:
                self._close()
            except Exception:
                pass


_cffi_sess = None
_cffi_key = None
_cffi_lock = threading.Lock()


def _session():
    global _cffi_sess, _cffi_key
    imp = CONFIG.get("impersonate") or "chrome"
    proxy = CONFIG.get("proxy")
    key = (imp, proxy, CONFIG.get("request_timeout_sec"))
    with _cffi_lock:
        if _cffi_sess is None or _cffi_key != key:
            if _cffi_sess is not None:
                try:
                    _cffi_sess.close()
                except Exception:
                    pass
            kw = {"impersonate": imp,
                  "timeout": CONFIG.get("request_timeout_sec", 300)}
            if proxy:
                kw["proxies"] = {"http": proxy, "https": proxy}
            _cffi_sess = _cffi.Session(**kw)
            _cffi_key = key
        return _cffi_sess


def _impersonating():
    return _cffi is not None and bool(CONFIG.get("impersonate"))


def _request(method, url, body=None, headers=None, stream=False, timeout=None):
    timeout = timeout or CONFIG.get("request_timeout_sec", 300)
    if _impersonating():
        try:
            r = _session().request(method, url, data=body, headers=headers,
                                   stream=stream, timeout=timeout,
                                   allow_redirects=True)
        except Exception as e:
            raise QwenError(f"transport error (curl_cffi): {e}")
        if stream:
            return _Resp(r.status_code, dict(r.headers),
                         lines=r.iter_lines(), close=r.close)
        return _Resp(r.status_code, dict(r.headers), body=r.content, close=r.close)
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        if CONFIG.get("proxy"):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": CONFIG["proxy"],
                                             "https": CONFIG["proxy"]}),
                urllib.request.HTTPSHandler(context=_ctx()))
            r = opener.open(req, timeout=timeout)
        else:
            r = urllib.request.urlopen(req, context=_ctx(), timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            content = e.read()
        except Exception:
            content = b""
        return _Resp(e.code, dict(e.headers or {}), body=content)
    except Exception as e:
        raise QwenError(f"transport error: {e}")
    if stream:
        return _Resp(r.status, dict(r.headers), lines=r, close=r.close)
    return _Resp(r.status, dict(r.headers), body=r.read(), close=r.close)


# ----------------------------------------------------------------- anti-bot
_ANTIBOT_MARKERS = ("x5secdata", "rgv587", "punish", "captcha",
                    "_____tmd_____", "baxia", "nocaptcha")
_ANTIBOT_HELP = (
    " Anti-bot (baxia) blocked the request. Copy bx-ua / bx-umidtoken / bx-v "
    "and the FULL cookie from a fresh browser /api/v2/chat/completions "
    "request into config.json + token.json, keep impersonate='chrome', and "
    "make sure user_agent matches the browser."
)


def _antibot_error(resp, body_text=""):
    punish = resp.header("bxpunish")
    flagged = bool(punish and str(punish) != "0")
    if not flagged and body_text:
        low = body_text.lower()
        flagged = any(m in low for m in _ANTIBOT_MARKERS)
    if not flagged:
        return None
    tag = f" [bxpunish={punish}]" if punish else ""
    return QwenError(f"Qwen anti-bot blocked the request{tag}." + _ANTIBOT_HELP
                     + (f" Body: {body_text.strip()[:400]}" if body_text else ""),
                     status=403)


def _http_error(resp):
    body = resp.read().decode("utf-8", "replace")
    ab = _antibot_error(resp, body)
    if ab is not None:
        return ab
    msg = f"HTTP {resp.status} from Qwen upstream"
    if resp.status in (401, 403):
        msg += " -- token rejected/expired"
    if body:
        msg += ": " + body[:500]
    return QwenError(msg, status=resp.status)


_api_ver = 0


def _remember(path):
    global _api_ver
    _api_ver = 2 if "/v2/" in path else 1


def _candidates(v2, v1):
    pin = int(CONFIG.get("api_version") or 0) or _api_ver
    if pin == 1:
        return [v1]
    if pin == 2:
        return [v2]
    return [v2, v1]


# ------------------------------------------------------------- chat / models
def create_chat(model):
    """Return (chat_id, parent_id). Body verbatim from browser capture."""
    payload = {
        "chatId": "",
        "models": [model],
        "project_id": "",
        "timestamp": int(time.time() * 1000),
        "chat_type": "t2t",
        "chat_mode": CONFIG.get("chat_mode", "normal"),
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    last = None
    for path in _candidates("/api/v2/chats/new", "/api/chats/new"):
        r = _request("POST", CONFIG["base_url"] + path, body, _headers(), timeout=60)
        try:
            if r.status in (404, 405):
                last = _http_error(r)
                continue
            if r.status >= 400:
                raise _http_error(r)
            raw = r.read().decode("utf-8", "replace")
            if CONFIG.get("debug_raw"):
                _dump_raw("CREATE CHAT >>>\n" + raw)
            data = json.loads(raw)
            _remember(path)
            break
        finally:
            r.close()
    else:
        raise last or QwenError("chat creation failed")
    if not isinstance(data, dict) or data.get("success") is False:
        raise QwenError(f"chat creation failed: {json.dumps(data, ensure_ascii=False)[:400]}")
    d = data.get("data") or {}
    cid = d.get("id") or d.get("chat_id")
    if not cid:
        raise QwenError(f"chat creation returned no id: {json.dumps(data)[:400]}")
    return cid, None


def delete_chat(cid):
    if not CONFIG.get("delete_chats") or not cid:
        return
    for path in (f"/api/v2/chats/{cid}", f"/api/chats/{cid}"):
        try:
            r = _request("DELETE", CONFIG["base_url"] + path, None,
                         _headers(cid), timeout=15)
            r.close()
            if r.status < 400:
                return
        except Exception:
            continue


_models_cache = {"ts": 0.0, "data": []}


def _model_items(data):
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("data") or items.get("models") or items.get("list")
    return items if isinstance(items, list) else []


def upstream_models():
    now = time.time()
    if now - _models_cache["ts"] < 600 and _models_cache["data"]:
        return _models_cache["data"]
    out = []
    for path in _candidates("/api/v2/models", "/api/models"):
        try:
            r = _request("GET", CONFIG["base_url"] + path, None,
                         _headers(), timeout=30)
            try:
                if r.status >= 400:
                    continue
                data = json.loads(r.read().decode("utf-8", "replace"))
            finally:
                r.close()
            for m in _model_items(data):
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                info = m.get("info") if isinstance(m.get("info"), dict) else {}
                meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
                out.append({"id": m["id"], "name": m.get("name") or m["id"],
                            "desc": meta.get("short_description")
                                    or meta.get("description") or ""})
            if out:
                _remember(path)
                break
        except Exception as e:
            log(f"model list fetch failed ({path}): {e}")
    _models_cache.update(ts=now, data=out)
    return out


# ------------------------------------------------------------------ payload
def _payload(prompt, model, chat_id, think, parent_id=None):
    """Exact browser shape: timestamp in SECONDS, no top-level chat_type."""
    ts = int(time.time())
    msg = {
        "id": None,
        "fid": str(uuid.uuid4()),
        "parentId": parent_id,
        "childrenIds": [str(uuid.uuid4())],
        "role": "user",
        "content": prompt,
        "user_action": "chat",
        "files": [],
        "timestamp": ts,
        "models": [model],
        "model": "",
        "chat_type": "t2t",
        "feature_config": {
            "thinking_enabled": bool(think),
            "output_schema": "phase",
            "research_mode": CONFIG.get("research_mode", "normal"),
            "auto_thinking": False,
            "thinking_mode": "Thinking",
            "thinking_format": CONFIG.get("thinking_format", "summary"),
            "auto_search": bool(CONFIG.get("auto_search", True)),
        },
        "extra": {"meta": {"subChatType": "t2t"}},
        "sub_chat_type": "t2t",
        "parent_id": parent_id,
    }
    return {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chatId": chat_id,
        "parentId": parent_id or "",
        "chat_id": chat_id,
        "chat_mode": CONFIG.get("chat_mode", "normal"),
        "model": model,
        "parent_id": parent_id,
        "messages": [msg],
        "timestamp": ts,
    }


# --------------------------------------------------------------- SSE parsing
def _sse_events(resp, raw_tail=None):
    for raw in resp.lines():
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
                else str(raw)).strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if raw_tail is not None:
            raw_tail.append(data)
            if len(raw_tail) > 20:
                del raw_tail[0]
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _event_error(obj):
    if obj.get("success") is False:
        d = obj.get("data") or {}
        if isinstance(d, dict):
            return d.get("details") or d.get("code") or json.dumps(obj)[:400]
        return json.dumps(obj)[:400]
    err = obj.get("error")
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)[:400]
    if isinstance(err, str):
        return err
    if not obj.get("choices") and not obj.get("usage"):
        code, msg = obj.get("code"), obj.get("message")
        if msg and code not in (None, 0, "0", "OK", "ok", "Success", "success"):
            return f"{code}: {msg}"
    for ch in obj.get("choices") or []:
        if isinstance(ch, dict):
            d = ch.get("delta") or {}
            if isinstance(d, dict) and d.get("status") in ("failed", "error"):
                return d.get("message") or "upstream delta status=failed"
    return None


def _deltas(obj):
    for ch in obj.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        d = ch.get("delta") or ch.get("message") or {}
        if not isinstance(d, dict):
            continue
        phase = d.get("phase") or ""
        rc = d.get("reasoning_content")
        if isinstance(rc, str) and rc:
            yield "reasoning", rc
        c = d.get("content")
        if isinstance(c, str) and c:
            yield ("reasoning" if phase in ("think", "thinking", "thinking_summary")
                   else "content"), c
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield "content", part["text"]


def _finished(obj):
    for ch in obj.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        d = ch.get("delta") or {}
        if isinstance(d, dict) and d.get("status") == "finished":
            return True
        if ch.get("finish_reason"):
            return True
    return False


class _StreamText:
    def __init__(self):
        self.total = {"content": "", "reasoning": ""}
        self.last_raw = {"content": None, "reasoning": None}
        self.cumulative = None

    def feed(self, kind, text):
        prev = self.last_raw[kind]
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
        self.last_raw[kind] = text
        return delta


def generate_stream(prompt, model, think, usage_out=None):
    attempts = max(1, int(CONFIG.get("retry_attempts", 3)))
    last_err = None
    refreshed = False
    for attempt in range(attempts):
        tracker = _StreamText()
        emitted = False
        chat_id = None
        raw_tail, raw_events, events = [], [], 0
        try:
            chat_id, parent_id = create_chat(model)
            payload = _payload(prompt, model, chat_id, think, parent_id)
            body = json.dumps(payload, ensure_ascii=False).encode()
            if CONFIG.get("debug_raw"):
                _dump_raw("REQUEST >>>\n" + json.dumps(payload, ensure_ascii=False))
            resp, last = None, None
            paths = _candidates("/api/v2/chat/completions", "/api/chat/completions")
            for i, path in enumerate(paths):
                url = (CONFIG["base_url"] + path + "?"
                       + urllib.parse.urlencode({"chat_id": chat_id}))
                r = _request("POST", url, body, _headers(chat_id), stream=True)
                if r.status in (404, 405) and i + 1 < len(paths):
                    last = _http_error(r)
                    r.close()
                    continue
                resp = r
                _remember(path)
                break
            if resp is None:
                raise last or QwenError("no completion endpoint available")
            try:
                if resp.status in (401, 403) and not refreshed:
                    tok, cookie = _read_credentials()
                    if refresh_access_token(tok, cookie, force=True):
                        refreshed = True
                        raise QwenError("token refreshed, retrying", status=401)
                if resp.status >= 400:
                    raise _http_error(resp)
                ab = _antibot_error(resp)
                if ab is not None:
                    raise ab
                ctype = resp.header("content-type").lower()
                if "event-stream" not in ctype:
                    raw = resp.read().decode("utf-8", "replace")
                    events = 1
                    if CONFIG.get("debug_raw"):
                        _dump_raw("NON-SSE RESPONSE >>>\n" + raw)
                    ab = _antibot_error(resp, raw)
                    if ab is not None:
                        raise ab
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        raise QwenError(f"non-JSON body ({ctype}): {raw[:400]}",
                                        status=502)
                    err = _event_error(obj)
                    if err:
                        raise QwenError(f"Qwen upstream: {err}")
                    u = obj.get("usage")
                    if isinstance(u, dict) and usage_out is not None:
                        usage_out.update(u)
                    for kind, text in _deltas(obj):
                        d = tracker.feed(kind, text)
                        if d:
                            emitted = True
                            yield kind, d
                    if not tracker.total["content"] and not tracker.total["reasoning"]:
                        raise QwenError("upstream returned JSON instead of SSE, "
                                        f"no text extracted. Body: {raw[:600]}",
                                        status=502)
                else:
                    for obj in _sse_events(resp, raw_tail):
                        events += 1
                        if CONFIG.get("debug_raw"):
                            raw_events.append(json.dumps(obj, ensure_ascii=False))
                        err = _event_error(obj)
                        if err:
                            raise QwenError(f"Qwen upstream: {err}")
                        u = obj.get("usage")
                        if isinstance(u, dict) and usage_out is not None:
                            usage_out.update(u)
                        for kind, text in _deltas(obj):
                            d = tracker.feed(kind, text)
                            if d:
                                emitted = True
                                yield kind, d
                        if _finished(obj):
                            break
                    if CONFIG.get("debug_raw"):
                        _dump_raw("\n".join(raw_events))
                    if not tracker.total["content"] and not tracker.total["reasoning"]:
                        raise QwenError(
                            f"upstream returned no text ({events} SSE events). "
                            "Last events:\n" + "\n".join(raw_tail[-5:])[:800],
                            status=502)
            finally:
                resp.close()
            delete_chat(chat_id)
            return
        except Exception as e:
            last_err = e if isinstance(e, QwenError) else QwenError(str(e))
            if emitted:
                raise last_err
            if attempt < attempts - 1:
                log(f"retry {attempt + 1}/{attempts}: {last_err}")
                time.sleep(CONFIG.get("retry_delay_sec", 2))
    raise last_err


def generate(prompt, model, think):
    t0 = time.monotonic()
    usage, content, reasoning = {}, [], []
    for kind, text in generate_stream(prompt, model, think, usage):
        (reasoning if kind == "reasoning" else content).append(text)
    out = {"content": "".join(content), "reasoning": "".join(reasoning),
           "usage": usage}
    log(f"[timing] generate: {time.monotonic() - t0:.1f}s, "
        f"{len(out['content'])} chars (+{len(out['reasoning'])} reasoning)")
    return out