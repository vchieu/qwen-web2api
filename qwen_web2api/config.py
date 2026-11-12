"""Configuration management."""
import json
import os
import threading

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8082,
    "base_url": "https://chat.qwen.ai",

    "token": None,
    "token_file": "token.json",
    "cookie": None,

    # Browser anti-bot (baxia) values copied from a real request.
    "bx_v": None,
    "bx_ua": None,
    "bx_umidtoken": None,
    "extra_headers": {},
    "user_agent": None,
    "proxy": None,
    "impersonate": "chrome",

    # Access token lives ~15 min; refresh_token cookie lives ~30 days.
    "auto_refresh": True,
    "refresh_paths": [
        "/api/v2/auths/refresh",
        "/api/v1/auths/refresh",
        "/api/auths/refresh",
        "/api/v2/auth/refresh",
        "/api/v2/users/refresh/token", "/api/v2/auths/"
    ],
    "refresh_margin_sec": 120,

    "api_keys": [],
    "cors_origins": [],

    "default_model": "qwen3.7-plus",
    "strict_models": False,
    "thinking": False,
    "chat_mode": "normal",
    "api_version": 2,
    "delete_chats": False,

    # feature_config mirrors the browser payload.
    "auto_search": True,
    "research_mode": "normal",
    "thinking_format": "summary",

    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 300,
    "max_body_bytes": 32 * 1024 * 1024,
    "prompt_max_bytes": 262144,

    "tool_retry_on_miss": True,
    "tool_retry_attempts": 1,

    "log_requests": True,
    "log_file": None,
    "debug_raw": False,
    "debug_raw_file": "qwen-raw.log",
}

CONFIG = dict(DEFAULT_CONFIG)
_config_lock = threading.Lock()


def load_config(path=None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    for p in ("./config.json",
              os.path.expanduser("~/.config/qwen-web2api/config.json")):
        if os.path.exists(p):
            return p
    return None