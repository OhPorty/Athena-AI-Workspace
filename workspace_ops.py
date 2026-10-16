"""Running commands, removing things and searching, inside a workspace, safely.

`run` executes any shell command inside the hardened sandbox container (read-only root, only the workspace writable, no secrets,
capabilities dropped), so the command list is no longer the safety boundary: the container is. What the container cannot make
safe is deleting a person's own work, so every removal step is recognised here and goes to the person for approval first. A
removal that is approved still moves files to .athena/trash instead of destroying them, and the turn's snapshot can undo the rest.
"""
import difflib
import fnmatch
import glob
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid

import bash_tools
import safety

_MAX_OUTPUT = 10000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
_NOISE_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", ".next", "target", ".athena"}


# ---------- recognising removal ----------

_REMOVAL_WORDS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put", "truncate"}
_WRAPPERS = {"sudo", "env", "command", "time", "nohup", "exec", "xargs", "nice", "stdbuf", "timeout", "doas", "builtin"}
_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&"}
_INLINE_REMOVAL = {
    "python": re.compile(r"\bos\.(remove|unlink|rmdir|removedirs)\b|\bshutil\.rmtree\b|\brmtree\b|\.unlink\s*\(|\.rmdir\s*\("),
    "node": re.compile(r"\bfs\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync|promises\.(rm|unlink|rmdir))\b|\brimraf\b|\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\s*\("),
    "perl": re.compile(r"\bunlink\b|\brmtree\b"),
    "ruby": re.compile(r"FileUtils\.(rm|rm_rf|rm_r|remove)|File\.delete|File\.unlink"),
    "sh": re.compile(r"(^|[\s;&|(`])(rm|rmdir|unlink|shred)(\s|$)|find\b[^\n]*-delete|git\s+(clean|rm)\b"),
}
_SCRIPT_KINDS = {".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node", ".rb": "ruby", ".pl": "perl", ".sh": "sh", ".bash": "sh"}
_INTERPRETERS = {"python": "python", "python3": "python", "node": "node", "nodejs": "node", "perl": "perl", "ruby": "ruby", "sh": "sh", "bash": "sh", "dash": "sh", "zsh": "sh"}
_BACKTICK = re.compile(r"`([^`]*)`")


def _tokenize(command):
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _segments(command):
    """The simple commands in a shell line, each as its list of words (quotes already resolved)."""
    segments, current = [], []
    for token in _tokenize(command):
        if token in _SEPARATORS or (set(token) <= set(";&|()") and token):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_wrappers(words):
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif os.path.basename(w) in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.match(r"^[0-9.]+[smhd]?$", words[i])):
                i += 1
        else:
            break
    return words[i:]


def _script_files(words, workspace):
    found = []
    for w in words:
        ext = os.path.splitext(w)[1].lower()
        if ext in _SCRIPT_KINDS and workspace:
            path = os.path.join(workspace, w) if not os.path.isabs(w) else w
            if os.path.isfile(path) and os.path.getsize(path) < 200_000:
                found.append((w, path, _SCRIPT_KINDS[ext]))
    return found


def _segment_reasons(words, workspace, depth=0):
    words = _strip_wrappers(words)
    if not words:
        return []
    verb = os.path.basename(words[0])
    args = words[1:]
    reasons = []
    if verb in _REMOVAL_WORDS:
        reasons.append(f"`{verb}` removes files")
    elif verb == "find" and ("-delete" in args or any(a in ("-exec", "-execdir", "-ok") and i + 1 < len(args) and os.path.basename(args[i + 1]) in _REMOVAL_WORDS for i, a in enumerate(args))):
        reasons.append("`find` is set to delete what it finds")
    elif verb == "git" and args:
        sub = args[0]
        if sub in ("clean", "rm") or (sub == "reset" and "--hard" in args) or sub == "restore" or (sub == "checkout" and ("--" in args or "." in args)) \
                or (sub == "stash" and len(args) > 1 and args[1] in ("drop", "clear")) or (sub == "branch" and any(a in ("-D", "-d", "--delete") for a in args)):
            reasons.append(f"`git {sub}` discards work")
    elif verb == "rsync" and any(a.startswith("--delete") for a in args):
        reasons.append("`rsync --delete` removes files at the destination")
    elif verb in _INTERPRETERS:
        kind = _INTERPRETERS[verb]
        for flag in ("-c", "-e", "-p", "--eval", "--print"):
            if flag in args and args.index(flag) + 1 < len(args):
                code = args[args.index(flag) + 1]
                if _INLINE_REMOVAL[kind].search(code):
                    reasons.append(f"the {verb} code you gave it deletes files")
                if kind == "sh" and depth < 2:
                    for inner in _segments(code):
                        reasons += _segment_reasons(inner, workspace, depth + 1)
        for name, path, script_kind in _script_files(args, workspace):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    if _INLINE_REMOVAL[script_kind].search(f.read()):
                        reasons.append(f"{name} contains code that deletes files")
            except OSError:
                pass
    else:
        for name, path, script_kind in _script_files([verb] + args, workspace):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    if _INLINE_REMOVAL[script_kind].search(f.read()):
                        reasons.append(f"{name} contains code that deletes files")
            except OSError:
                pass
    return reasons


def removal_reasons(command, workspace=""):
    """Why this shell line counts as a removal step (empty when it does not)."""
    reasons = []
    try:
        for words in _segments(command):
            reasons += _segment_reasons(words, workspace)
    except ValueError:                                  # unbalanced quotes: look at the raw text instead
        if _INLINE_REMOVAL["sh"].search(command):
            reasons.append("the command removes files")
    for inner in _BACKTICK.findall(command):
        if _INLINE_REMOVAL["sh"].search(inner):
            reasons.append("a command inside backticks removes files")
    if "$(" in command and _INLINE_REMOVAL["sh"].search(command[command.index("$("):]):
        reasons.append("a command substitution removes files")
    seen, unique = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


_READ_ONLY_WORDS = {"ls", "cat", "grep", "egrep", "fgrep", "rg", "head", "tail", "wc", "pwd", "stat", "diff", "file", "tree", "du", "date",
                    "echo", "which", "whoami", "uname", "id", "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath", "printf", "true", "type"}
_GIT_READ_ONLY = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "remote", "config"}


def is_read_only(command):
    """True when every part of the command only looks at things (so no snapshot is needed before it)."""
    if re.search(r"(^|[^-])>|>>|\btee\b|\bsed\b[^|;&]*\s-i|\bfind\b[^|;&]*(-delete|-exec|-fprint)", command):
        return False
    try:
        segments = _segments(command)
    except ValueError:
        return False
    for words in segments:
        words = _strip_wrappers(words)
        if not words:
            continue
        verb = os.path.basename(words[0])
        if verb == "git":
            if len(words) < 2 or words[1] not in _GIT_READ_ONLY:
                return False
        elif verb == "find":
            continue
        elif verb not in _READ_ONLY_WORDS:
            return False
    return bool(segments)


def _dir_summary(path):
    files, size = 0, 0
    for root, _, names in os.walk(path):
        for n in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return files, size


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def describe_target(root, path):
    rel = os.path.relpath(path, root)
    if os.path.isdir(path):
        files, size = _dir_summary(path)
        return f"folder {rel}/ ({files} file{'s' if files != 1 else ''}, {human_size(size)})"
    try:
        return f"file {rel} ({human_size(os.path.getsize(path))})"
    except OSError:
        return f"{rel}"


def removal_targets(command, workspace):
    """For a plain rm/rmdir/unlink with literal names: what it would remove, as readable lines. None when that cannot be known."""
    if not workspace or any(ch in command for ch in "$`"):
        return None
    root = os.path.realpath(workspace)
    lines = []
    try:
        segments = _segments(command)
    except ValueError:
        return None
    for words in segments:
        words = _strip_wrappers(words)
        if not words or os.path.basename(words[0]) not in ("rm", "rmdir", "unlink"):
            continue
        for arg in words[1:]:
            if arg.startswith("-"):
                continue
            matches = glob.glob(os.path.join(root, arg)) or [os.path.join(root, arg)]
            for m in matches[:40]:
                real = os.path.realpath(m)
                if (real == root or real.startswith(root + os.sep)) and os.path.exists(real):
                    lines.append(describe_target(root, real))
    return lines[:40]


# ---------- running ----------

def _docker_ready():
    return bash_tools._docker_sandbox_available()


NO_SANDBOX = ("The sandbox container isn't available right now (Docker or the athena-sandbox image is missing). Commands only run inside it, "
              "so nothing was run. Build it once with `docker build -t athena-sandbox:latest sandbox/` from Athena's folder.")


def _cap(text):
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT - 1500] + f"\n... [{len(text) - _MAX_OUTPUT + 1500} characters omitted] ...\n" + text[-1500:]


def run_command(command, workspace, timeout=DEFAULT_TIMEOUT):
    if not workspace:
        return {"error": "run needs an active workspace."}
    if not _docker_ready():
        return {"error": NO_SANDBOX}
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    name = f"athena-run-{uuid.uuid4().hex[:10]}"
    cmd = bash_tools._build_docker_sandbox_cmd("sh", ["-c", command], workspace, name=name)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=20)
        return {"error": f"The command was still running after {timeout} seconds and was stopped. For something long-running, run it with background set to true."}
    except Exception as e:
        return {"error": f"Could not run the command: {e}"}
    output = result.stdout + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
    reply = {"command": command, "output": _cap(output), "exit_code": result.returncode}
    if result.returncode != 0:
        failing = bash_tools.failing_code_context(output, workspace)
        if failing:
            reply["failing_code"] = failing
    return reply


def start_background(command, workspace):
    if not workspace:
        return {"error": "run needs an active workspace."}
    if not _docker_ready():
        return {"error": NO_SANDBOX}
    name = f"athena-bg-{uuid.uuid4().hex[:10]}"
    try:
        proc = subprocess.Popen(bash_tools._build_docker_sandbox_cmd("sh", ["-c", command], workspace, name=name),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, shell=False)
    except Exception as e:
        return {"error": f"Could not start the command: {e}"}
    with bash_tools._bg_processes_lock:
        bash_tools._bg_process_counter[0] += 1
        process_id = str(bash_tools._bg_process_counter[0])
        entry = {"proc": proc, "command": "sh", "args": ["-c", command], "output": "", "output_lock": threading.Lock(), "started_at": time.time(), "container": name}
        bash_tools._bg_processes[process_id] = entry

    def drain():
        try:
            for line in proc.stdout:
                with entry["output_lock"]:
                    entry["output"] = (entry["output"] + line)[-50000:]
        except Exception:
            pass

    threading.Thread(target=drain, daemon=True).start()
    return {"process_id": process_id, "command": command, "status": "started"}


def check_process(process_id):
    result = bash_tools.check_background_bash(process_id)
    if "args" in result:
        result.pop("args", None)
        result["command"] = result.get("command") if result.get("command") != "sh" else "(background command)"
    return result


def stop_process(process_id):
    entry = bash_tools._bg_processes.get(str(process_id))
    result = bash_tools.stop_background_bash(process_id, True)
    if entry and entry.get("container"):
        subprocess.run(["docker", "kill", entry["container"]], capture_output=True, timeout=20)
    return result


# ---------- deleting (always through the person) ----------

def plan_delete(paths, workspace):
    """-> {"items": [...readable lines...], "resolved": [absolute paths]} or {"error"}."""
    if not workspace:
        return {"error": "delete needs an active workspace."}
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        return {"error": "paths must be a list of file or folder names to delete."}
    root = os.path.realpath(workspace)
    resolved, items = [], []
    for p in paths[:50]:
        full = os.path.realpath(os.path.join(root, p))
        if full == root or not full.startswith(root + os.sep):
            return {"error": f"'{p}' is the workspace itself or outside it, so it cannot be deleted."}
        if os.path.relpath(full, root).split(os.sep)[0] == ".athena":
            return {"error": "Athena's own .athena folder cannot be deleted."}
        problem = safety.check_model_path(full, write=True)
        if problem:
            return {"error": problem}
        if not os.path.exists(full):
            return {"error": f"'{p}' does not exist."}
        resolved.append(full)
        items.append(describe_target(root, full))
    return {"items": items, "resolved": resolved}


def do_delete(resolved, workspace):
    """Move to .athena/trash (never destroy). -> what was moved and where."""
    root = os.path.realpath(workspace)
    trash = os.path.join(root, ".athena", "trash", time.strftime("%Y%m%d-%H%M%S"))
    moved = []
    for full in resolved:
        target = os.path.join(trash, os.path.relpath(full, root))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(full, target)
        moved.append(os.path.relpath(full, root))
    return {"deleted": moved, "kept_copy_in": os.path.relpath(trash, root)}


# ---------- searching ----------

def search_files(workspace, pattern, path=".", names_only=False, limit=40):
    if not workspace:
        return {"error": "search_files needs an active workspace."}
    root = os.path.realpath(workspace)
    base = os.path.realpath(os.path.join(root, path or "."))
    if base != root and not base.startswith(root + os.sep):
        return {"error": f"Path '{path}' is outside the workspace."}
    if not pattern:
        return {"error": "Give something to search for."}
    hits, scanned, truncated = [], 0, False
    regex = None
    if not names_only:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in sorted(dirs) if d not in _NOISE_DIRS and safety.classify(os.path.join(current, d)) != "secret"]
        for name in sorted(files):
            full = os.path.join(current, name)
            if safety.classify(full) == "secret":
                continue
            rel = os.path.relpath(full, root)
            if names_only:
                if fnmatch.fnmatch(name.lower(), pattern.lower()) or pattern.lower() in name.lower():
                    hits.append({"path": rel})
            else:
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, "r", encoding="utf-8") as f:
                        for number, line in enumerate(f, 1):
                            if regex.search(line):
                                hits.append({"path": rel, "line": number, "text": line.rstrip()[:200]})
                                if len(hits) >= limit:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
                scanned += 1
            if len(hits) >= limit:
                truncated = True
                break
        if truncated:
            break
    return {"pattern": pattern, "matches": hits, "truncated": truncated, **({"note": f"Showing the first {limit}; narrow the search for more."} if truncated else {})}


# ---------- what a command or a write changed (shown to the person as file name and +/-) ----------

_DIFF_MAX_FILE = 256 * 1024
_DIFF_MAX_CACHE = 16 * 1024 * 1024
_DIFF_MAX_FILES = 3000
_DIFF_LINE_LIMIT = 5000


def read_text(path, limit=1024 * 1024):
    """The text of a file for comparing, or None when it is missing, binary or large."""
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def line_counts(old, new):
    """(lines added, lines removed) going from old to new text."""
    a, b = (old or "").splitlines(), (new or "").splitlines()
    if len(a) > _DIFF_LINE_LIMIT or len(b) > _DIFF_LINE_LIMIT:
        return max(len(b) - len(a), 0), max(len(a) - len(b), 0)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def watch(workspace):
    """The workspace's files as they are now: {relative path: [size, mtime, text or None]}. None when there are too many to follow."""
    root = os.path.realpath(workspace)
    state, cached = {}, 0
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _NOISE_DIRS and safety.classify(os.path.join(current, d)) != "secret"]
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path) or safety.classify(path) == "secret":
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            text = None
            if st.st_size <= _DIFF_MAX_FILE and cached < _DIFF_MAX_CACHE:
                text = read_text(path, _DIFF_MAX_FILE)
                cached += st.st_size if text is not None else 0
            state[os.path.relpath(path, root)] = [st.st_size, st.st_mtime_ns, text]
            if len(state) > _DIFF_MAX_FILES:
                return None
    return state


def changes_since(before, workspace):
    """Files created, edited or deleted since watch() took `before`: [{"path", "status", "added", "removed"}]."""
    if before is None:
        return []
    after = watch(workspace)
    if after is None:
        return []
    root = os.path.realpath(workspace)
    changed = []
    for rel, (size, mtime, text) in after.items():
        old = before.get(rel)
        if old is None:
            new_text = text if text is not None else read_text(os.path.join(root, rel), _DIFF_MAX_FILE)
            changed.append({"path": rel, "status": "created", "added": len((new_text or "").splitlines()) if new_text is not None else None, "removed": 0})
        elif (old[0], old[1]) != (size, mtime):
            if old[2] is not None and text is not None:
                added, removed = line_counts(old[2], text)
                if added == 0 and removed == 0:
                    continue
            else:
                added = removed = None
            changed.append({"path": rel, "status": "edited", "added": added, "removed": removed})
    for rel, (size, mtime, text) in before.items():
        if rel not in after:
            changed.append({"path": rel, "status": "deleted", "added": 0, "removed": len(text.splitlines()) if text is not None else None})
    return sorted(changed, key=lambda c: c["path"])[:30]
