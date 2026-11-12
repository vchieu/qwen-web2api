"""Prompt building + tool-call parsing (OpenAI tools -> Qwen Web text protocol).

Trimmed, self-contained version of the gemini-web2api tool layer:
flatten messages -> one prompt with a pinned '# Tool Use' block, then parse
```tool_call fences out of the answer with JSONDecoder.raw_decode.
"""
import json
import re
import uuid

from .config import CONFIG
from .qwen import log

_TRUNCATION_MARKER = "[...truncated...]"


def _stringify_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(c.get("text", ""))
                elif isinstance(c.get("text"), str):
                    out.append(c["text"])
            elif isinstance(c, str):
                out.append(c)
        return "\n".join(p for p in out if p)
    return str(content)


def _fn_of(t):
    return t.get("function", t) if t.get("type") == "function" else t


def tool_names(tools) -> set:
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = _fn_of(t)
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    return names


def tool_parameters(tools) -> dict:
    schemas = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = _fn_of(t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        schemas[fn["name"]] = set(props) if isinstance(props, dict) else set()
    return schemas


def tool_required_params(tools) -> dict:
    required = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = _fn_of(t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or {}
        names = params.get("required") if isinstance(params, dict) else None
        if isinstance(names, list):
            required[fn["name"]] = {n for n in names if isinstance(n, str)}
    return required


def missing_required_params(tool_calls, required) -> list:
    missing = []
    if not required:
        return missing
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        params = required.get(fn.get("name"))
        if not params:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}", strict=False)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(args, dict):
            continue
        missing.extend(f"{fn['name']}.{p}" for p in sorted(params) if p not in args)
    return missing


def is_required_tool_choice(tool_choice) -> bool:
    if tool_choice == "required":
        return True
    if isinstance(tool_choice, dict):
        return bool(tool_choice.get("function") or tool_choice.get("name"))
    return False


def _choice_instruction(tool_choice) -> str:
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return ("\n\nIMPORTANT: You MUST call at least one tool. "
                "Do not respond with text only.")
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        name = fn.get("name") if isinstance(fn, dict) else tool_choice.get("name")
        if name:
            return (f'\n\nIMPORTANT: You MUST call the tool "{name}". '
                    "Do not call other tools.")
    return ""


_TOOL_REMINDER = ("\n\n[Reminder: you have working tools. To know what a file "
                  "contains or what a command produced, call the tool -- do "
                  "not answer from memory and do not claim to have read or "
                  "run anything you have not.]")


def _tool_use_block(tool_defs, constraint) -> str:
    tools_json = json.dumps(tool_defs, ensure_ascii=False)
    return (
        "# Tool Use\n\n"
        "The tools below are connected to the user's machine and execute for "
        "real the moment you call them.\n\n"
        "Call format (use this exact format):\n"
        "```tool_call\n"
        '{"name": "<tool_name>", "arguments": {<parameters>}}\n'
        "```\n\n"
        "Rules:\n"
        "- Each turn do exactly ONE of: (A) you need to read or change "
        "something -> output ONLY tool_call block(s); (B) you already have "
        "everything -> answer as plain text with NO block. Never both, and "
        "never any prose after the block.\n"
        '- Every parameter belongs inside the "arguments" object, keyed '
        'exactly like the tool\'s "parameters" properties -- never beside '
        '"name".\n'
        "- Emit several blocks in one turn when the calls are independent.\n"
        "- Before reviewing or editing code, read the actual files with a "
        "read/search tool first. Listing file names is not reading.\n"
        "- Never write [Tool result for ...] yourself and never invent a "
        "result: emit the block and stop.\n"
        "- Never claim the tools are unavailable or that you lack "
        "permission: they are connected and will run.\n\n"
        f"Available tools:\n{tools_json}{constraint}"
    )


def _join_parts(parts, max_bytes, pinned_head=None) -> str:
    """Join parts; on overflow keep the pinned tool block + newest messages."""
    items = [p for p in parts if p]
    pinned = (pinned_head or "").strip()

    def nb(s):
        return len(s.encode("utf-8"))

    full = "\n\n".join(([pinned] if pinned else []) + items)
    if nb(full) <= max_bytes:
        return full
    log(f"Prompt truncated to {max_bytes} bytes")
    budget = max_bytes - nb(_TRUNCATION_MARKER) - 4
    if pinned:
        cap = max_bytes * 2 // 5
        if nb(pinned) > cap:
            pinned = pinned.encode("utf-8")[:cap].decode("utf-8", "ignore")
        budget -= nb(pinned) + 2
    if budget <= 0:
        return full.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    tail_idx, used = set(), 0
    for pos in range(len(items) - 1, -1, -1):
        cost = nb(items[pos]) + (2 if tail_idx else 0)
        if used + cost > budget:
            break
        tail_idx.add(pos)
        used += cost
    tail = [items[i] for i in sorted(tail_idx)]
    if not tail and items:
        tail = [items[-1].encode("utf-8")[:max(1, budget)].decode("utf-8", "ignore")]
    remaining = budget - used
    head = []
    for pos, part in enumerate(items):
        if pos in tail_idx:
            break
        cost = nb(part) + (2 if head else 0)
        if cost > remaining:
            break
        head.append(part)
        remaining -= cost
    blocks = ([pinned] if pinned else []) + head
    if blocks:
        return "\n\n".join(blocks + [_TRUNCATION_MARKER] + tail)
    return "\n\n".join([_TRUNCATION_MARKER] + tail)


def messages_to_prompt(messages, tools=None, tool_choice=None) -> str:
    max_bytes = int(CONFIG.get("prompt_max_bytes", 262144))
    parts, tool_block = [], None
    if tools and tool_choice != "none":
        defs = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = _fn_of(t)
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            defs.append({"name": fn["name"],
                         "description": fn.get("description", ""),
                         "parameters": fn.get("parameters", {})})
        if defs:
            tool_block = _tool_use_block(defs, _choice_instruction(tool_choice))

    id_to_name = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                if isinstance(tc, dict) and tc.get("id") and fn.get("name"):
                    id_to_name[tc["id"]] = fn["name"]

    combined = set()
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role == "developer":
            role = "system"
        content = _stringify_content(m.get("content"))
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if m.get("tool_calls"):
                blocks = []
                for tc in m["tool_calls"]:
                    fn = (tc or {}).get("function") or {}
                    args = fn.get("arguments") or "{}"
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    try:
                        json.loads(args)
                    except (ValueError, json.JSONDecodeError):
                        args = "{}"
                    blocks.append(
                        f'```tool_call\n{{"id": {json.dumps(tc.get("id", ""))}, '
                        f'"name": {json.dumps(fn.get("name", ""))}, '
                        f'"arguments": {args}}}\n```')
                text = f"[Assistant]: {content}\n" + "\n".join(blocks)
                for j in range(i + 1, len(messages)):
                    nm = messages[j]
                    if not isinstance(nm, dict) or nm.get("role") != "tool":
                        break
                    tcid = nm.get("tool_call_id", "")
                    combined.add(tcid)
                    name = nm.get("name") or id_to_name.get(tcid) or "tool"
                    text += (f"\n[Tool result for {name} (id={tcid})]: "
                             + _stringify_content(nm.get("content")))
                parts.append(text)
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid in combined:
                continue
            name = m.get("name") or id_to_name.get(tcid) or "tool"
            parts.append(f"[Tool result for {name} (id={tcid})]: {content}")
        else:
            parts.append(content)

    if tool_block:
        parts.append(_TOOL_REMINDER)
    return _join_parts(parts, max_bytes, pinned_head=tool_block)


# ------------------------------------------------------------- parsing ----
_FENCE_OPEN = re.compile(
    r"```(tool_call|function_call|tool_code|json)(?![A-Za-z0-9_])[ \t]*\n?")
_FENCE_CLOSE = re.compile(r"\s*```")
_ARGUMENT_KEYS = ("arguments", "args", "input")
_META_KEYS = {"name", "id", "type", "description", "tool", "tool_name"}
_NOTHING = object()


def _iter_blocks(text):
    """Yield (start, end, data, kind); raw_decode so ``` inside strings is safe."""
    dec = json.JSONDecoder(strict=False)
    pos = 0
    while True:
        m = _FENCE_OPEN.search(text, pos)
        if not m:
            return
        kind = m.group(1)
        j = m.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        try:
            data, end = dec.raw_decode(text, j)
        except ValueError:
            close = text.find("```", m.end())
            yield m.start(), (close + 3 if close != -1 else m.end()), _NOTHING, kind
            pos = close if close != -1 else m.end()
            continue
        c = _FENCE_CLOSE.match(text, end)
        if c:
            end = c.end()
        yield m.start(), end, data, kind
        pos = end


def _normalize_name(name, allowed):
    if not isinstance(name, str) or not name:
        return None
    stripped = name
    while True:
        low = stripped.lower()
        if low.startswith("functions.") or low.startswith("default_api."):
            stripped = stripped.split(".", 1)[1]
            if not stripped:
                return None
        else:
            break
    if allowed is None:
        return stripped or None
    if stripped in allowed:
        return stripped
    cands = {a for a in allowed if a.lower() == stripped.lower()}
    if not cands and "." in stripped:
        seg = stripped.split(".")[-1].lower()
        cands = {a for a in allowed if a.split(".")[-1].lower() == seg}
    return next(iter(cands)) if len(cands) == 1 else None


def _extract_args(data, props=None):
    for key in _ARGUMENT_KEYS:
        if key in data:
            v = data[key]
            if isinstance(v, dict):
                if (props is not None and key in props
                        and (set(data) - _META_KEYS - {key})):
                    continue
                return v
            if isinstance(v, str) and v.strip():
                if props is not None and key in props:
                    continue
                return v
    if props is None:
        rest = {k: v for k, v in data.items() if k not in _META_KEYS}
    else:
        rest = {k: v for k, v in data.items()
                if k not in _META_KEYS or k in props}
    if len(rest) == 1:
        k, v = next(iter(rest.items()))
        if k in ("parameters", "params") and isinstance(v, dict):
            return v
    return rest or {}


def _args_json(args):
    if isinstance(args, dict):
        return json.dumps(args, ensure_ascii=False), None
    if isinstance(args, str):
        t = args.strip()
        if not t:
            return "{}", "empty string"
        try:
            p = json.loads(t, strict=False)
            if isinstance(p, dict):
                return json.dumps(p, ensure_ascii=False), None
        except ValueError:
            pass
        return "{}", "not a JSON object"
    if args is None:
        return "{}", "missing"
    return "{}", f"unsupported type {type(args).__name__}"


def parse_tool_calls(text, allowed_names=None, tool_schemas=None):
    """Returns (clean_text, tool_calls, dropped_reasons)."""
    calls, dropped, clean_parts, last_end = [], [], [], 0
    for start, end, data, kind in _iter_blocks(text or ""):
        if kind == "json":
            if (not isinstance(data, dict) or not allowed_names
                    or data.get("name") not in allowed_names):
                continue
            has_wrapper = any(k in data for k in _ARGUMENT_KEYS)
            keys = set(data) - _META_KEYS
            props = (tool_schemas or {}).get(data.get("name"))
            if not has_wrapper and not (keys and props and keys <= props):
                continue
        else:
            line_start = text.rfind("\n", 0, start) + 1
            if text[line_start:start].strip():
                continue  # quoted mid-sentence: explanation, not a call
        parsed, reason = None, ""
        if isinstance(data, dict):
            name = data.get("name")
            resolved = _normalize_name(name, allowed_names) if name else None
            if not name:
                reason = "no tool name"
            elif allowed_names is not None and resolved is None:
                reason = f"undeclared tool '{name}'"
            else:
                name = resolved or name
                props = (tool_schemas or {}).get(name)
                args, degraded = _args_json(_extract_args(data, props))
                if degraded:
                    log(f"tool_call '{name}': arguments unusable ({degraded})")
                parsed = {"id": f"call_{uuid.uuid4().hex[:8]}",
                          "type": "function",
                          "function": {"name": name, "arguments": args}}
        else:
            reason = "body is not valid JSON"
        if parsed is None:
            dropped.append(reason)
            log(f"Dropping tool_call block: {reason}: {text[start:end][:200]}")
            continue
        clean_parts.append(text[last_end:start])
        last_end = end
        calls.append(parsed)
    if calls:
        clean = "".join(clean_parts)
    else:
        clean_parts.append(text[last_end:])
        clean = "".join(clean_parts)
    return clean.strip(), calls, dropped


def looks_like_tool_call(text) -> bool:
    for _s, _e, data, kind in _iter_blocks(text or ""):
        if isinstance(data, dict) and data.get("name"):
            if kind != "json" or any(k in data for k in _ARGUMENT_KEYS):
                return True
    return False


_MISS_RE = re.compile(
    r"\b(?:let me|i'?ll|i will|let's)\s+"
    r"(?:read|check|look|see|inspect|examine|write|create|update|edit|run|fix|implement)\b"
    r"|\b(?:de minh|minh se|toi se)\s+"
    r"(?:doc|kiem tra|xem|viet|tao|sua|chay)\b", re.IGNORECASE)


def looks_like_missed_tool_call(text) -> bool:
    if not text or len(text) >= 1200:
        return False
    if "```tool_call" in text or "```function_call" in text:
        return False
    return bool(_MISS_RE.search(text))


def build_response_format_instruction(response_format) -> str:
    if not isinstance(response_format, dict):
        return ""
    t = response_format.get("type")
    if t == "json_object":
        return ("\n\nIMPORTANT: Respond with a single valid JSON object only. "
                "Do not include prose, explanations or Markdown code fences.")
    if t == "json_schema":
        schema = response_format.get("json_schema", {})
        if isinstance(schema, dict) and "schema" in schema:
            schema = schema["schema"]
        return ("\n\nIMPORTANT: Respond with a single valid JSON value that "
                "strictly conforms to the following JSON Schema. Do not "
                "include prose or Markdown code fences.\nJSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False))
    return ""


def strip_code_fence(text) -> str:
    if not text:
        return text
    stripped = text.strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*[ \t]*\n?(.*?)\n?```$", stripped, re.DOTALL)
    return m.group(1).strip() if m else text