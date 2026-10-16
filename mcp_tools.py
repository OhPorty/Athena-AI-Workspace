"""MCP tools as Athena's models see them.

A server is off-limits to models until its `model_access` is switched on (Settings -> MCP), and even then
only the tools the person left enabled are offered, so the tool list a slow local model has to read each
round stays exactly as small as they chose. Tools appear as mcp__<server>__<tool>.

Calls come back as ordinary results: an MCP tool's own failure, a timeout or an unreachable server is
{"error": "..."} like any other tool's, never a halt. Images from a tool ride along as _images, the same way
screenshots do, and are shown only to models that can see.
"""
import json
import re

import settings
from mcp_manager import mcp_manager, normalize_config

PREFIX = "mcp__"
_DESCRIPTION_LIMIT = 400


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "server"


def _tool_part(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(name))[:64] or "tool"


def _configured_servers():
    out = []
    for raw in settings.load_settings().get("mcp_servers", []):
        try:
            out.append(normalize_config(raw))
        except ValueError:
            continue
    return out


def exposed_tools():
    """-> [(function_name, server_config, mcp_tool)] for every tool a model may be offered right now."""
    exposed, taken = [], set()
    for cfg in _configured_servers():
        if not cfg["model_access"]:
            continue
        live = mcp_manager.get_config(cfg["name"])
        for tool in mcp_manager.list_tools(cfg["name"]):
            if cfg["enabled_tools"] is not None and tool.name not in cfg["enabled_tools"]:
                continue
            fn = f"{PREFIX}{_slug(cfg['name'])}__{_tool_part(tool.name)}"
            while fn in taken:                                      # two tools that sanitize to the same name
                fn += "_"
            taken.add(fn)
            exposed.append((fn, live or cfg, tool))
    return exposed


def _parameters(tool):
    schema = dict(getattr(tool, "input_schema", None) or {})
    schema.pop("$schema", None)
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": schema.get("properties", {})}
    schema.setdefault("properties", {})
    return schema


def schemas():
    out = []
    for fn, cfg, tool in exposed_tools():
        text = " ".join(str(tool.description or tool.name).split())[:_DESCRIPTION_LIMIT]
        out.append({"type": "function", "function": {
            "name": fn, "description": f"{text} (from the {cfg['name']} MCP server)", "parameters": _parameters(tool)}})
    return out


def is_mcp_tool(name):
    return isinstance(name, str) and name.startswith(PREFIX)


def _result_to_dict(result):
    texts, images = [], []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
        elif kind == "image":
            images.append(block.data)
        elif kind == "audio":
            texts.append("[the tool returned audio, which can't be played here]")
        elif kind == "resource":
            inner = getattr(block, "resource", None)
            texts.append(getattr(inner, "text", None) or f"[resource {getattr(inner, 'uri', '')}]")
        else:
            texts.append(f"[{kind or 'unknown'} content]")
    text = "\n".join(t for t in texts if t).strip()
    if getattr(result, "is_error", False):
        return {"error": text or "The tool reported an error without saying why."}
    out = {}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        out = dict(structured) if "result" not in structured or len(structured) > 1 else {"result": structured["result"]}
    elif text:
        try:
            parsed = json.loads(text)
            out = parsed if isinstance(parsed, dict) else {"result": parsed}
        except ValueError:
            out = {"result": text}
    else:
        out = {"result": "(the tool returned nothing)"} if not images else {}
    if images:
        out["_images"] = images
        out["images"] = f"{len(images)} image(s) returned"
    return out


def call(function_name, arguments):
    match = next(((cfg, tool) for fn, cfg, tool in exposed_tools() if fn == function_name), None)
    if match is None:
        return {"error": f"'{function_name}' is not an available MCP tool. A server's tools are offered only while it is connected and switched on for models."}
    cfg, tool = match
    args = arguments if isinstance(arguments, dict) else {}
    try:
        result = mcp_manager.call_tool(cfg["name"], tool.name, args)
    except TimeoutError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"The {cfg['name']} MCP server could not run {tool.name}: {str(e)[:300]}"}
    return _result_to_dict(result)
