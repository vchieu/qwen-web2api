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
    p.add_argument("--token-file")
    p.add_argument("--proxy")
    p.add_argument("--bridge", action="store_true",
                   help="route upstream calls through the browser extension")
    p.add_argument("--no-bridge", action="store_true",
                   help="call chat.qwen.ai directly from Python (blocked by baxia)")
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
        if a.token_file:
            CONFIG["token_file"] = a.token_file
        if a.proxy:
            CONFIG["proxy"] = a.proxy
        if a.bridge:
            CONFIG["bridge_enabled"] = True
        if a.no_bridge:
            CONFIG["bridge_enabled"] = False

    _guard_bind(CONFIG["host"], a.allow_insecure)

    bridge = None
    if CONFIG.get("bridge_enabled", True):
        from . import bridge as _bridge
        _bridge.install()
        _bridge.start_server()
        bridge = _bridge

    srv = ThreadedServer((CONFIG["host"], CONFIG["port"]), QwenHandler)
    print(f"qwen-web2api v{__version__}")
    print(f"  Listening: http://{CONFIG['host']}:{CONFIG['port']}")
    print(f"  Base URL:  http://localhost:{CONFIG['port']}/v1")
    if bridge:
        print(f"  Upstream:  BROWSER BRIDGE -> http://127.0.0.1:"
              f"{CONFIG.get('bridge_port', 8083)}")
        print("             mo https://chat.qwen.ai (da dang nhap) + bat extension")
    else:
        from .qwen import load_token
        tok, _cookie = load_token()
        print(f"  Upstream:  direct HTTP (token {'yes' if tok else 'MISSING'})")
    print(f"  Auth:      {'enabled' if CONFIG.get('api_keys') else 'disabled (loopback only)'}")
    print(f"  Default:   {CONFIG['default_model']}")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()