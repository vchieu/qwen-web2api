"""Shared primitives for qwen-web2api.

Upstream calls now always run inside the chat.qwen.ai tab through the browser
bridge (see bridge.py + extension/bridge.js); Python never talks to the Qwen
API directly. This module only keeps the error type and the logger shared by
the rest of the package.
"""
import collections
import sys
import threading
import time

from .config import CONFIG

_recent_log = collections.deque(maxlen=100)
_log_lock = threading.Lock()


def get_recent_log():
    with _log_lock:
        return list(_recent_log)


class QwenError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def is_debug():
    """Kiem tra xem co bat log debug khong (chap nhan ca 'debug' va 'debug_log')."""
    return bool(CONFIG.get("debug") or CONFIG.get("debug_log") or CONFIG.get("verbose_log"))


def debug_log(msg):
    """Ghi log debug chi khi debug duoc bat."""
    if not is_debug():
        return
    log(f"[debug] {msg}")


def log(msg):
    if not CONFIG.get("log_requests"):
        return
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        _recent_log.append(line)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    path = CONFIG.get("log_file")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
