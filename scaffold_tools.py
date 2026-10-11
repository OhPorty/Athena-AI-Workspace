"""scaffold: copy a ready-made, working starter project into the workspace.

A model can't read files outside its workspace, and retyping a 300-line starter at local-model
speeds costs minutes -- so starters ship in scaffolds/<kind>/ and this tool copies them in.
Add a starter by adding a folder with a scaffold.json ({"description", "entry", "notes"}).
"""
import html
import json
import os
import re
import shutil

import file_tools

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scaffolds")
_SKIP = {"scaffold.json"}
_BLOCKS = {
    "css": re.compile(r"/\* THEME:START \*/.*?/\* THEME:END \*/", re.S),
    "js": re.compile(r"// THEME:START.*?// THEME:END", re.S),
}


def _kinds():
    out = {}
    if not os.path.isdir(_ROOT):
        return out
    for name in sorted(os.listdir(_ROOT)):
        meta_path = os.path.join(_ROOT, name, "scaffold.json")
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                out[name] = json.load(f)
    return out


def _schema():
    kinds = _kinds()
    listing = "; ".join(f"{name}: {meta['description']}" for name, meta in kinds.items())
    all_themes = sorted({t for meta in kinds.values() for t in meta.get("themes", {})})
    return [{
        "type": "function",
        "function": {
            "name": "scaffold",
            "description": (
                "Copy working starter code into the workspace so you don't retype plumbing. A starter is structure and "
                "behavior only: the design, content and rules are yours to write for what you are building, and you can "
                "also skip it and write the files yourself. Starters: " + listing + ". site and game start with a neutral "
                "look; theme is an optional ready-made palette if one happens to suit. Refuses to overwrite existing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(kinds)},
                    "theme": {"type": "string", "enum": all_themes, "description": "Optional palette for site or game; leave it out for the neutral look."},
                    "path": {"type": "string", "description": "Folder inside the workspace to copy into. Default: the workspace root."},
                },
                "required": ["kind"],
            },
        },
    }]


SCAFFOLD_TOOL_SCHEMAS = _schema()


def _apply_theme(dest, files, meta, theme):
    """Rewrite the marked theme block in the copied CSS/JS and fill {{PLACEHOLDER}} text in every copied file."""
    spec = meta["themes"][theme]
    targets = meta.get("theme_files", {})
    values = {k: html.escape(str(v), quote=True) for k, v in spec.get("text", {}).items()}
    for rel in files:
        path = os.path.join(dest, rel)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if rel == targets.get("css"):
            tokens = {**meta.get("base_css", {}), **spec.get("css", {})}
            block = "/* THEME:START */\n:root {\n" + "".join(f"  {k}: {v};\n" for k, v in tokens.items()) + "}\n"
            if spec.get("extra_css"):
                block += spec["extra_css"] + "\n"
            text = _BLOCKS["css"].sub(lambda m: block + "/* THEME:END */", text, count=1)
        if rel == targets.get("js") and spec.get("js"):
            text = _BLOCKS["js"].sub(lambda m: "// THEME:START\nconst THEME = " + json.dumps(spec["js"], indent=2) + ";\n// THEME:END", text, count=1)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def scaffold(workspace, args):
    if not workspace:
        return {"error": "scaffold needs an active workspace."}
    args = args if isinstance(args, dict) else {}
    kinds = _kinds()
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in kinds:
        return {"error": f"kind must be one of: {', '.join(kinds)}"}
    themes = kinds[kind].get("themes") or {}
    theme = str(args.get("theme") or "").strip().lower()
    if themes and theme and theme not in themes:
        return {"error": f"theme must be one of: {', '.join(themes)} (or omit it for the neutral look)"}
    if themes and not theme:
        theme = kinds[kind]["default_theme"]
    try:
        dest = file_tools.resolve_workspace_path(workspace, str(args.get("path") or "."))
    except ValueError as e:
        return {"error": str(e)}
    src = os.path.join(_ROOT, kind)
    files = [os.path.relpath(os.path.join(d, f), src) for d, dirs, fs in os.walk(src) for f in fs
             if f not in _SKIP and "__pycache__" not in d and not f.endswith(".pyc")]
    clashes = sorted(f for f in files if os.path.exists(os.path.join(dest, f)))
    if clashes:
        return {"error": f"Refusing to overwrite existing files: {', '.join(clashes)}. Pick an empty folder with the path argument."}
    for rel in files:
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(src, rel), target)
    if themes:
        _apply_theme(dest, files, kinds[kind], theme)
    base = os.path.relpath(dest, os.path.realpath(workspace))
    prefix = "" if base == "." else base.rstrip("/") + "/"
    result = {
        "kind": kind,
        "created": sorted(prefix + f for f in files),
        "open": prefix + kinds[kind]["entry"],
        "notes": kinds[kind]["notes"],
    }
    if themes:
        result["theme"] = theme
        result["theme_description"] = themes[theme]["description"]
    return result
