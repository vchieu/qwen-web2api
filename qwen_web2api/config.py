"""Configuration management."""
import json
import os
import threading

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8082,

    # Browser bridge (extension-facing server).
    "bridge_port": 8083,
    "bridge_worker_ttl": 20,
    "bridge_job_timeout": 600,
    "bridge_poll_wait": 20,

    "api_keys": [],
    "cors_origins": [],

    "default_model": "qwen3.7-plus",
    "strict_models": False,
    "thinking": False,

    # Chay moi job trong temporary chat (?temporary-chat=true).
    "temporary_chat": False,
    # Cho phep extension chon model trong UI theo job.model.
    "bridge_select_model": True,

    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "max_body_bytes": 32 * 1024 * 1024,
    "prompt_max_bytes": 262144,

    "tool_retry_on_miss": True,
    "tool_retry_attempts": 1,

    "log_requests": True,
    "log_file": None,
}

CONFIG = dict(DEFAULT_CONFIG)
_config_lock = threading.Lock()


def load_config(path=None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    pkg_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    for p in ("./config.json",
              pkg_cfg,
              os.path.expanduser("~/.config/qwen-web2api/config.json")):
        if os.path.exists(p):
            return p
    return None
