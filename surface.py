"""The tool list a model is shown, and how its calls reach the engines.

Athena grew many tools over many sessions. What a model sees is now a small, orthogonal set with short descriptions:

    files      read_file (also folders), write_file, edit_file (text or line range), search_files
    shell      run (any command, inside the sandbox; background and check/stop), delete (always asks the person)
    thinking   plan, ask_user
    the web    web_search, web_fetch, preview, load_skill, get_full_tool_output
    rare ones  make, research, inspect_page, remember, recall, schedule, notify, check ... shown by NAME only (see catalog())

Each surface call is translated to the engine tool that already does the work (plan -> write_plan / update_plan_step, edit_file with
a line range -> replace_lines, make -> generate_pdf / generate_background_music / scaffold ...), so the gates, the progress rules and
the engines themselves keep working untouched. Delegation sessions, bots and rooms keep the old names; this module only shapes what
the main chat and workspace sessions see.

Rare tools carry no schema in the prompt. A model that calls one by name gets it run if its arguments fit, or is shown exactly what
the tool takes (usage) and tries again: the same on-demand loading Claude Code uses, without an extra step to remember. That only
happens on Ollama-style endpoints (verified: a call to an undeclared tool still comes back as a real tool call); hosted APIs get
every schema, since they reject calls to tools that were not declared.
"""
import json
import re

import jsonschema

import mcp_tools

PLAN_ENGINE = {"write_plan", "update_plan_step"}
LEGACY_TO_SURFACE = {
    "write_plan": "plan", "update_plan_step": "step_done", "bash_read_only": "run", "bash_exec": "run", "bash_exec_start": "run",
    "bash_exec_check": "run", "bash_exec_stop": "run", "list_files": "read_file", "replace_lines": "edit_file",
    "lcm_recall_search": "recall", "lcm_recall_expand": "recall", "lcm_recall_range": "recall",
    "generate_pdf": "make", "generate_background_music": "make", "backup_file": "the automatic snapshots", "restore_file": "the undo button",
}


def _fn(name, description, properties, required=()):
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": list(required)}}


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}


def _s(description):
    return {"type": "string", "description": description}


SCHEMAS = {
    "read_file": _fn("read_file", "Read a file, or list a folder. Images and PDFs are shown if you can see. Long files come in pieces of about 200 lines: the answer says which "
                     "lines you have and how to continue (offset), and start_line / end_line read any part you name.",
                     {"path": _s("File or folder, relative to the project."), "offset": {"type": "integer", "description": "Character to continue from."},
                      "start_line": {"type": "integer", "description": "First line to read (1 is the first)."}, "end_line": {"type": "integer", "description": "Last line to read."},
                      "page": {"type": "integer", "description": "Page of a PDF."}}, ["path"]),
    "write_file": _fn("write_file", "Create a file, or replace it with this full content. Set append to add to the end instead, which is how to write a big file in pieces.",
                      {"path": _S, "content": _S, "append": _B}, ["path", "content"]),
    "edit_file": _fn("edit_file", "Change part of a file. Give old_text (exact text to find) and new_text; or start_line and end_line (inclusive) with expected_text, "
                                  "which must match what is there now, and new_text.",
                     {"path": _S, "old_text": _S, "new_text": _S, "start_line": _I, "end_line": _I, "expected_text": _S}, ["path", "new_text"]),
    "search_files": _fn("search_files", "Find text inside the project's files (shows file, line and the line), or with names_only find files by name.",
                        {"pattern": _s("Text or regular expression, or part of a file name."), "path": _s("Folder to search; default is the whole project."),
                         "names_only": _B, "limit": _I}, ["pattern"]),
    "run": _fn("run", "Run a shell command in the project. It runs inside a sandbox, so pipes, python, node, npm, pip, git and the rest all work. "
                      "Each command starts in a fresh container: only the project folder is kept between commands (/tmp is not), so save anything you need again inside the project. "
                      "Commands that delete files ask the person first. Set background to start something long-running; then pass check or stop with its id.",
               {"command": _s("The command line."), "background": _B, "timeout": {"type": "integer", "description": "Seconds before it is stopped (default 120)."},
                "check": {"type": "string", "description": "Id of a background command: show its output."}, "stop": {"type": "string", "description": "Id of a background command: stop it."},
                "reason": _s("Why, in a sentence. Shown to the person if the command needs their approval.")}),
    "delete": _fn("delete", "Delete files or folders. The person is always asked first, and what is deleted goes to a trash folder, not destroyed.",
                  {"paths": {"type": "array", "items": _S, "description": "Files or folders."}, "reason": _s("Why, in a sentence; shown to the person.")}, ["paths"]),
    "plan": _fn("plan", "Write your plan: the ordered list of steps (steps). The person may have to approve it before anything else works. Do not call it again to "
                       "tick a step off (use step_done). To mark a step in_progress or failed, give step and status.",
                {"steps": {"type": "array", "items": _S}, "step": {"type": "integer", "description": "Step number as shown in the plan, starting at 1."},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed"]}, "note": _S,
                 "replace": _s("Only to change an approved plan whose steps really must change: true, with a reason."), "reason": _s("Why the plan has to change (shown to the person).")}),
    "step_done": _fn("step_done", "Tick a step of your plan off as finished. Call it right after you finish that step and before you start the next one; steps go in order.",
                     {"step": {"type": "integer", "description": "The step number, as shown in the plan, starting at 1."}, "note": _s("What was done, in a few words.")}, ["step"]),
    "ask_user": _fn("ask_user", "Ask the person something you cannot continue without: a decision that is theirs, or something you cannot find out yourself. Not for checking in after you finish. Give options when there are clear choices; they can also type their own answer.",
                    {"question": _S, "options": {"type": "array", "items": _S}, "detail": _s("A line of context, if needed.")}, ["question"]),
    "web_search": _fn("web_search", "Search the web. Returns the top results with a readable preview of each.", {"query": _S}, ["query"]),
    "web_fetch": _fn("web_fetch", "Read a web page as text (long pages come in chunks: pass offset). Add screenshot to also see it, or actions to use the page like a person first "
                                 "(\"click Sign in\", \"type Search hello\", \"press Enter\", \"scroll 800\", \"wait 500\"). "
                                 "Add raw to get the page's real source (HTML, CSS or JS) instead of its text; add save_to (a path in the project) to put that source in a file "
                                 "so you can work on it without reading it all.",
                     {"url": _S, "offset": _I, "screenshot": _B, "actions": {"type": "array", "items": _S}, "raw": _B,
                      "save_to": _s("Project path to save the page source to (implies raw).")}, ["url"]),
    "preview": _fn("preview", "Open a page from the project in a real browser, use it, and report what is wrong with it (errors, overflow, contrast, broken images), with a screenshot if you can see. "
                              "You can drive the page like a person: actions run in order after it loads, and the checks and screenshot show the page as it is AFTER them. Use them to play a game, "
                              "press a button, open a menu, fill in and submit a form, or scroll, then see what happened.",
                   {"path": _S, "viewport": {"type": "string", "enum": ["desktop", "mobile", "both"]},
                    "actions": {"type": "array", "items": _S, "description": "Steps to do on the page, in order: \"click #start\" (a selector or visible words), \"press ArrowRight\" or \"press Space\", "
                                                                         "\"hold ArrowRight 600\" (a key held for ms), \"hold mouse #canvas 800\", \"type #name Sam\" (a selector, label or placeholder, then the text; "
                                                                         "put the name in quotes if it has spaces), \"scroll 800\" (or top or bottom), \"wait 500\" (ms). Up to a dozen steps."},
                    "wait_ms": _I}, ["path"]),
    "load_skill": _fn("load_skill", "Load one of the person's saved skills (instructions for a kind of task) by name.", {"name": _S}, ["name"]),
    "get_full_tool_output": _fn("get_full_tool_output", "Get the full text of an earlier result that was shortened.", {"output_id": _S}, ["output_id"]),
    "make": _fn("make", "Make a file from structured content. kind pdf: a resume or document. docx: a Word document. xlsx: an Excel workbook. pptx: a PowerPoint deck. music: instrumental background music. "
                        "scaffold: a starter project (page, site, game, audio). music needs no spec; for the others call with only kind first to see the fields spec takes.",
                {"kind": {"type": "string", "enum": ["pdf", "docx", "xlsx", "pptx", "music", "scaffold"]}, "spec": {"type": "object", "description": "What to make; call with only kind to see the fields."}}, ["kind"]),
    "research": _fn("research", "Answer a question from the web and fact-check it before reporting: pages are read, every claim needs a quote that is really on the page, and sources are compared. Slow and careful.",
                    {"question": _S, "depth": {"type": "string", "enum": ["quick", "standard", "thorough"]}}, ["question"]),
    "inspect_page": _fn("inspect_page", "Look inside a public web page the way browser dev tools do: its structure, stylesheets and CSS variables, or with a CSS selector each match's classes, attributes, text, position and computed styles.",
                        {"url": _S, "selector": _s("CSS selector, e.g. \".product-card h2\"; leave out for the page structure."), "depth": _I, "limit": _I, "html": _B,
                         "styles": {"type": "array", "items": _S}, "actions": {"type": "array", "items": _S}}, ["url"]),
    "remember": _fn("remember", "Save a lasting fact about the person or their work so it is known in every future chat. One short, complete sentence.", {"fact": _S}, ["fact"]),
    "recall": _fn("recall", "Look up the exact earlier wording of THIS conversation, which may now be only summarized. term: search for a word or phrase. node_id: expand a summary. position: first or last messages.",
                  {"term": _S, "node_id": _I, "position": {"type": "string", "enum": ["first", "last"]}, "count": _I}),
    "schedule": _fn("schedule", "Run something later or on a repeat, or list and cancel scheduled tasks. It runs with read-only tools.",
                    {"action": {"type": "string", "enum": ["create", "list", "cancel"]}, "prompt": _s("What to do each time."), "at": _s("2026-10-20 09:30, or 09:30 for the next 9:30."),
                     "in_minutes": _I, "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly"]}, "days": {"type": "array", "items": _S}, "label": _S, "id": _I}),
    "notify": _fn("notify", "Send a message to the person's phone or browser, even if the page is closed. Use it when a long job finishes or you need them.", {"message": _S, "title": _S}, ["message"]),
    "check": _fn("check", "Check the project: run its tests, or only check that its Python files have no syntax errors.", {"what": {"type": "string", "enum": ["tests", "syntax"]}}),
}

CATALOG = {
    "make": "make a PDF, Word, Excel or PowerPoint file, instrumental background music, or a starter project",
    "research": "answer a question from the web and fact-check it before reporting (slow, careful)",
    "inspect_page": "look inside a web page's structure and styles, like browser dev tools",
    "remember": "save a lasting fact about the person or their work",
    "recall": "look up exact earlier wording from this conversation",
    "schedule": "run a task later or on a repeat; list or cancel tasks",
    "notify": "send a message to the person's phone or browser",
    "check": "run the project's tests or check its syntax",
    "find_definition": "find where a symbol is defined (path, line, symbol)",
    "find_references": "find where a symbol is used (path, line, symbol)",
    "type_info": "the type of a symbol (path, line, symbol)",
}
DEFERRABLE = set(CATALOG)


def can_defer(req):
    """Only Ollama-native endpoints are known to turn a call to an undeclared tool into a real tool call."""
    return not getattr(req, "provider", "")


def _present(names, mode, req):
    """Which surface tools exist for this legacy tool list, in the order they are shown."""
    interactive = getattr(req, "interactive", True)
    unrestricted = getattr(req, "allowed_tools", None) is None
    workspace = mode == "workspace"
    files = bool(names & {"read_file", "list_files"})
    out = []
    if files:
        out.append("read_file")
    if files and workspace:
        out.append("search_files")
    if "write_file" in names:
        out.append("write_file")
    if names & {"edit_file", "replace_lines"}:
        out.append("edit_file")
    if "bash_exec" in names:
        out.append("run")
        if interactive and unrestricted:
            out.append("delete")
    if names & PLAN_ENGINE:
        out.append("plan")
    if "update_plan_step" in names:
        out.append("step_done")
    if interactive and unrestricted:
        out.append("ask_user")
    for n in ("web_search", "web_fetch", "preview", "load_skill", "get_full_tool_output", "search_codebase"):
        if n in names:
            out.append(n)
    if names & {"generate_pdf", "generate_background_music", "scaffold"}:
        out.append("make")
    if "web_search" in names and unrestricted:
        out.append("research")
    if "inspect_page" in names:
        out.append("inspect_page")
    if unrestricted:
        out.append("remember")
    if names & {"lcm_recall_search", "lcm_recall_expand", "lcm_recall_range"}:
        out.append("recall")
    if unrestricted and interactive:
        out += ["schedule", "notify"]
    if "bash_exec" in names and workspace:
        out.append("check")
    for n in ("find_definition", "find_references", "type_info"):
        if n in names:
            out.append(n)
    return out


def _schema_for(name, legacy_by_name):
    if name in SCHEMAS:
        return SCHEMAS[name]
    legacy = legacy_by_name.get(name)
    return legacy["function"] if legacy else None


SEEING = {
    True: "You CAN see images: an image (png, jpg, webp, gif) or a PDF read this way is shown to you as a picture, so open it and look.",
    False: "You cannot view images: for an image you only get its type and size.",
}


def _say_what_it_can_see(tools, vision):
    """Tell the model, in the file tool's own description, whether pictures are shown to it. It was reasoning "read_file only reads text"
    and refusing to look at images it could see."""
    if vision is None:
        return tools
    for t in tools:
        fn = t["function"]
        if fn["name"] == "read_file":
            fn["description"] = fn["description"].replace("Images and PDFs are shown if you can see.", SEEING[bool(vision)])
    return tools


def to_surface(legacy_tools, mode, req, defer=None, vision=None):
    """The tool list the model sees, from the (already gate-filtered) legacy one."""
    by_name = {t["function"]["name"]: t for t in legacy_tools}
    names = set(by_name)
    if names and names <= PLAN_ENGINE:                   # a gate has narrowed the turn to planning
        return [{"type": "function", "function": SCHEMAS[n]} for n in ("plan", "step_done") if n == "plan" or "update_plan_step" in names]
    if defer is None:
        defer = can_defer(req)
    shown = _present(names, mode, req)
    mapped = set(LEGACY_TO_SURFACE) | {"read_file", "write_file", "edit_file", "web_search", "web_fetch", "preview", "load_skill", "get_full_tool_output", "scaffold",
                                       "search_codebase", "inspect_page", "find_definition", "find_references", "type_info"}
    tools = []
    for n in shown:
        if defer and n in DEFERRABLE:
            continue
        schema = _schema_for(n, by_name)
        if schema:
            tools.append({"type": "function", "function": dict(schema)})
    for n, t in by_name.items():                         # MCP tools and anything not part of the rebuilt list pass through as they are
        if n not in mapped and mcp_tools.is_mcp_tool(n):
            tools.append(t)
    return _say_what_it_can_see(tools, vision)


def catalog(legacy_tools, mode, req):
    """The prompt section naming the tools that carry no schema. Empty when nothing is deferred."""
    if not can_defer(req):
        return ""
    names = {t["function"]["name"] for t in legacy_tools}
    lines = [f"- {n}: {CATALOG[n]}" for n in _present(names, mode, req) if n in DEFERRABLE]
    if not lines:
        return ""
    return ("## More tools\n"
            "These are not listed above to keep things light, but you can call any of them by name with the right arguments. "
            "If you call one wrongly, the answer shows exactly what it takes.\n" + "\n".join(lines))


# ---------- calls ----------

def _invalid(name, args, problem):
    schema = SCHEMAS.get(name)
    return "_surface_error", {"error": problem, "usage": {"tool": name, **({"takes": schema["parameters"]} if schema else {}), "description": schema["description"] if schema else ""}}


def check_args(name, args):
    """-> an error message when the arguments do not fit the tool's schema, else None."""
    schema = SCHEMAS.get(name)
    if schema is None:
        return None
    try:
        jsonschema.validate(args, schema["parameters"])
    except jsonschema.ValidationError as e:
        where = ".".join(str(p) for p in e.absolute_path)
        return f"{name}: {e.message}" + (f" (at {where})" if where else "")
    return None


_MAKE_ENGINES = {"pdf": "generate_pdf", "music": "generate_background_music", "scaffold": "scaffold", "docx": "generate_docx", "xlsx": "generate_xlsx", "pptx": "generate_pptx"}
_OFFICE_EXAMPLES = {
    "docx": {"title": "Report", "blocks": [{"type": "heading", "text": "Introduction", "level": 1}, {"type": "paragraph", "text": "Some text."}, {"type": "bullets", "items": ["one", "two"]}]},
    "xlsx": {"sheets": [{"name": "Budget", "header": ["Item", "Cost"], "rows": [["Rent", 1200], ["Food", 400]]}]},
    "pptx": {"title": "My deck", "slides": [{"title": "Why this matters", "bullets": ["first point", "second point"], "notes": "what to say"}]},
}


def _kind_of(kind):
    """Small models name the kind loosely ("background_music", "resume"): take the obvious meaning."""
    k = str(kind or "").lower()
    if k in _MAKE_ENGINES:
        return k
    for words, target in ((("docx", "word"), "docx"), (("xlsx", "excel", "spreadsheet", "workbook"), "xlsx"), (("pptx", "powerpoint", "slide", "presentation", "deck"), "pptx"),
                          (("music", "audio", "song", "track", "lofi", "beat"), "music"), (("pdf", "resume", "cv", "document", "report"), "pdf"),
                          (("scaffold", "starter", "template", "page", "site", "game", "project"), "scaffold")):
        if any(w in k for w in words):
            return target
    return None


def _example_spec(parameters):
    props = parameters.get("properties", {})
    ordered = list(parameters.get("required", [])) + [p for p in props if p not in parameters.get("required", [])]
    values = {"integer": 1, "number": 1, "boolean": True, "array": [], "object": {}}
    example = {}
    for name in ordered[:3]:
        prop = props.get(name, {})
        example[name] = (prop.get("enum") or [None])[0] or values.get(prop.get("type"), "example")
    return example


def make_guide(kind):
    import background_music
    import pdf_tools
    import scaffold_tools
    sources = {"pdf": ("generate_pdf", pdf_tools.PDF_TOOL_SCHEMAS), "music": ("generate_background_music", background_music.BACKGROUND_MUSIC_TOOL_SCHEMAS),
               "scaffold": ("scaffold", scaffold_tools.SCAFFOLD_TOOL_SCHEMAS)}
    kind = _kind_of(kind)
    if kind in _OFFICE_EXAMPLES:
        import office_tools
        spec = office_tools.SPEC_SCHEMAS[kind]
        return {"kind": kind, "how_to_call": "Call the make tool itself (not the shell) with kind and spec, where spec is an object with these fields.", "description": spec["description"],
                "spec_fields": spec["parameters"], "example_call": {"kind": kind, "spec": _OFFICE_EXAMPLES[kind]}}
    if kind not in sources:
        return {"error": "kind must be pdf, music, scaffold, docx, xlsx or pptx."}
    engine, schemas = sources[kind]
    fn = next(s["function"] for s in schemas if s["function"]["name"] == engine)
    return {"kind": kind, "how_to_call": "Call the make tool itself (not the shell) with kind and spec, where spec is an object with these fields.", "description": fn["description"],
            "spec_fields": fn["parameters"], "example_call": {"kind": kind, "spec": _example_spec(fn["parameters"])}}


def _make_call(args):
    kind = _kind_of(args.get("kind"))
    if kind is None:
        return "_surface_error", {"error": "make: kind must be pdf, music, scaffold, docx, xlsx or pptx.", "usage": {"tool": "make", "takes": SCHEMAS["make"]["parameters"]}}
    spec = args.get("spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            spec = None
    if not isinstance(spec, dict):
        spec = {k: v for k, v in args.items() if k not in ("kind", "spec")} or None      # fields given loose next to kind count as the spec
    guide = make_guide(kind)
    if spec is None and guide["spec_fields"].get("required"):
        return "_surface_error", guide
    return _MAKE_ENGINES[kind], spec or {}


_LEGACY_SHELL = {"bash_exec", "bash_exec_start", "bash_exec_check", "bash_exec_stop", "bash_read_only"}


def _legacy_shell_to_run(name, args):
    """A model that calls an old shell tool by name (it may remember one from training) gets the new `run`, so the removal check and
    the snapshot apply to it too."""
    import shlex
    import bash_tools
    if name == "bash_exec_check":
        return "run", {"check": str(args.get("process_id", ""))}
    if name == "bash_exec_stop":
        return "run", {"stop": str(args.get("process_id", ""))}
    if name == "bash_read_only":
        command, cmd_args, pipe_to = bash_tools.normalize_read_only_call(args.get("command", ""), args.get("args") or [], args.get("pipe_to"))
        line = shlex.join([command, *[str(a) for a in cmd_args]])
        for stage in (pipe_to or []):
            line += " | " + shlex.join([stage.get("command", ""), *[str(a) for a in stage.get("args", [])]])
        return "run", {"command": line}
    command, cmd_args = bash_tools.normalize_command_line(args.get("command", ""), args.get("args") or [])
    run_args = {"command": shlex.join([command, *[str(a) for a in cmd_args]])}
    if name == "bash_exec_start":
        run_args["background"] = True
    return "run", run_args


def to_engine(name, args, mode=None):
    """(surface name, args) -> (engine name, engine args). Unknown or untranslated names pass through."""
    args = args if isinstance(args, dict) else {}
    if name in _LEGACY_SHELL and mode is not None and mode != "athena_delegation":
        return _legacy_shell_to_run(name, args)
    if name == "make":
        return _make_call(args)
    if name == "update_plan_step" and args.get("index") is None and args.get("step") is not None:      # the old name, with the new parameter name
        return name, {**args, "index": args["step"]}
    problem = check_args(name, args) if name in DEFERRABLE else None
    if problem:
        return _invalid(name, args, problem)
    if name == "edit_file" and (args.get("start_line") is not None or args.get("end_line") is not None):
        return "replace_lines", {"path": args.get("path", ""), "start_line": args.get("start_line"), "end_line": args.get("end_line"),
                                 "new_content": args.get("new_text", ""), "expected_content": args.get("expected_text", "")}
    if name == "step_done":
        step = args.get("step")
        return "update_plan_step", {"index": step - 1 if isinstance(step, int) else step, "status": "done", "note": args.get("note", ""), "_numbering": 1}
    if name == "plan":
        if "steps" in args:
            return "write_plan", {"steps": args["steps"], "_numbering": 1, "replace": args.get("replace") in (True, "true", "True"), "reason": str(args.get("reason") or "")[:300]}
        if args.get("step") is not None or args.get("status"):
            step = args.get("step")
            return "update_plan_step", {"index": step - 1 if isinstance(step, int) else step, "status": args.get("status", ""),
                                        "note": args.get("note", ""), "_numbering": 1}
        return "_surface_error", {"error": "plan: give steps (to write the plan) or step and status (to update one step)."}
    if name == "recall":
        session = {"session_id": ""}
        if args.get("node_id") is not None:
            return "lcm_recall_expand", {**session, "node_id": args["node_id"]}
        if args.get("position") in ("first", "last"):
            return "lcm_recall_range", {**session, "order": "asc" if args["position"] == "first" else "desc", "limit": int(args.get("count") or 5)}
        if args.get("term"):
            return "lcm_recall_search", {**session, "term": args["term"]}
        return "_surface_error", {"error": "recall: give term, node_id, or position (first or last)."}
    return name, args


def engine_call(tool_call, mode=None):
    """A model's tool call -> the call the engine runs, remembering the name the model used."""
    fn = tool_call.get("function", {})
    name = fn.get("name", "")
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        import json
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    engine_name, engine_args = to_engine(name, args, mode)
    return {**tool_call, "function": {**fn, "name": engine_name, "arguments": engine_args}, "_shown": name}


_RENAME = re.compile(r"\b(" + "|".join(sorted(map(re.escape, LEGACY_TO_SURFACE), key=len, reverse=True)) + r")\b")


def rename_text(text):
    """Error messages written for the engine names, shown with the names the model actually has."""
    return _RENAME.sub(lambda m: LEGACY_TO_SURFACE[m.group(1)], text) if isinstance(text, str) else text
