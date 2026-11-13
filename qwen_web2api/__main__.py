"""Entry point: python -m qwen_web2api"""
import argparse
import os
import sys
import uuid

from . import __version__
from .config import CONFIG, _config_lock, find_config, load_config
from .server import QwenHandler, ThreadedServer


def _guard_bind(host, allow_insecure):
    if allow_insecure or CONFIG.get("api_keys"):
        return
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    key = uuid.uuid4().hex + uuid.uuid4().hex
    sys.stderr.write(f"Auto-generated API key (use as Bearer token): {key}\n")
    sys.stderr.flush()
    with _config_lock:
        CONFIG["api_keys"] = [key]


def main():
    p = argparse.ArgumentParser(description="Qwen Web -> OpenAI API proxy")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--config")
    p.add_argument("--allow-insecure", action="store_true")
    p.add_argument("--version", action="version",
                   version=f"qwen-web2api {__version__}")
    a = p.parse_args()

    cp = a.config or os.environ.get("QWEN_WEB2API_CONFIG") or find_config()
    if cp:
        load_config(cp)
    with _config_lock:
        if a.host:
            CONFIG["host"] = a.host
        if a.port:
            CONFIG["port"] = a.port

    _guard_bind(CONFIG["host"], a.allow_insecure)

    from . import bridge as _bridge
    _bridge.start_server()

    srv = ThreadedServer((CONFIG["host"], CONFIG["port"]), QwenHandler)
    print(f"qwen-web2api v{__version__}")
    print(f"  Listening: http://{CONFIG['host']}:{CONFIG['port']}")
    print(f"  Base URL:  http://localhost:{CONFIG['port']}/v1")
    print(f"  Upstream:  BROWSER BRIDGE -> http://127.0.0.1:"
          f"{CONFIG.get('bridge_port', 8083)}")
    print("             mo https://chat.qwen.ai (da dang nhap) + bat extension")
    print(f"  Auth:      {'enabled' if CONFIG.get('api_keys') else 'disabled (loopback only)'}")
    print(f"  Default:   {CONFIG['default_model']}")
    from .qwen import is_debug
    debug_on = is_debug()
    log_req = bool(CONFIG.get("log_requests"))
    log_file = CONFIG.get("log_file") or "(stderr only)"
    print(f"  Debug:     {'ON' if debug_on else 'off'}  |  "
          f"log_requests={'on' if log_req else 'off'}  |  "
          f"log_file={log_file}")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()
