"""Shared primitives for qwen-web2api.

Upstream calls now always run inside the chat.qwen.ai tab through the browser
bridge (see bridge.py + extension/bridge.js); Python never talks to the Qwen
API directly. This module only keeps the error type and the logger shared by
the rest of the package.
"""
import sys
import time

from .config import CONFIG


class QwenError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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
