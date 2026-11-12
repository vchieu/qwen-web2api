"""Model aliases for Qwen Chat.

Ids verified live from GET /api/v2/models (Nov 2026):
  qwen3.7-plus, qwen3.8-max, qwen4.0-max-preview
Unknown ids pass through unchanged; GET /v1/models merges the live list.
"""

MODELS = {
    # --- current web lineup ---
    "qwen3.7-plus": {
        "upstream": "qwen3.7-plus",
        "desc": "Qwen3.7-Plus (web default, multimodal, auto thinking/search)",
    },
    "qwen3.7-plus-thinking": {
        "upstream": "qwen3.7-plus", "think": True,
        "desc": "Qwen3.7-Plus with thinking forced on",
    },
    "qwen3.8-max": {
        "upstream": "qwen3.8-max",
        "desc": "Qwen3.8-Max (flagship, vision + reasoning)",
    },
    "qwen3.8-max-thinking": {
        "upstream": "qwen3.8-max", "think": True,
        "desc": "Qwen3.8-Max with thinking forced on",
    },
    "qwen4.0-max-preview": {
        "upstream": "qwen4.0-max-preview",
        "desc": "Qwen4-Max-Preview (agentic flagship preview)",
    },
    "qwen4-max-preview": {
        "upstream": "qwen4.0-max-preview",
        "desc": "Alias for qwen4.0-max-preview",
    },
    "qwen4.0-max-preview-thinking": {
        "upstream": "qwen4.0-max-preview", "think": True,
        "desc": "Qwen4-Max-Preview with thinking forced on",
    },

    # --- legacy aliases (may be retired upstream) ---
    "qwen3-max": {"upstream": "qwen3-max", "desc": "Legacy Qwen3 Max"},
    "qwen3-plus": {"upstream": "qwen-plus-latest", "desc": "Qwen Plus"},
    "qwen-plus": {"upstream": "qwen-plus-latest", "desc": "Alias for qwen3-plus"},
    "qwen3-turbo": {"upstream": "qwen-turbo-latest", "desc": "Qwen Turbo"},
    "qwen-turbo": {"upstream": "qwen-turbo-latest", "desc": "Alias for qwen3-turbo"},
    "qwen3-coder-plus": {"upstream": "qwen3-coder-plus", "desc": "Qwen3 Coder Plus"},
    "qwen3-coder-flash": {"upstream": "qwen3-coder-flash", "desc": "Qwen3 Coder Flash"},
    "qwen3-235b-a22b": {"upstream": "qwen3-235b-a22b", "think": True,
                        "desc": "Qwen3 235B A22B"},
    "qwq-32b": {"upstream": "qwq-32b", "think": True, "desc": "QwQ 32B reasoning"},
    "qwen3-vl-plus": {"upstream": "qwen3-vl-plus", "desc": "Qwen3 VL Plus"},
    "qwen3-omni-flash": {"upstream": "qwen3-omni-flash", "desc": "Qwen3 Omni Flash"},
}


def resolve_model(model_name, default="qwen3.7-plus", thinking_default=False):
    """Resolve a requested model id -> (name, upstream_id, think, error).

    Unknown ids pass through. "@think=1"/"@think=0" forces thinking on/off.
    """
    think_override = None
    if not isinstance(model_name, str) or not model_name:
        model_name = default
    if "@think=" in model_name:
        model_name, s = model_name.rsplit("@think=", 1)
        s = s.strip().lower()
        if s in ("1", "true", "on", "yes"):
            think_override = True
        elif s in ("0", "false", "off", "no"):
            think_override = False
        else:
            return None, None, None, f"Invalid think level: {s}"
    cfg = MODELS.get(model_name)
    if cfg is None:
        think = think_override if think_override is not None else thinking_default
        return model_name, model_name, think, None
    think = think_override if think_override is not None else cfg.get("think", thinking_default)
    return model_name, cfg.get("upstream", model_name), think, None