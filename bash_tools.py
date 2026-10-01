import glob
import os
import time
import subprocess
import threading

# Read-only shell access. Structured, not a raw command string: the
# model supplies a bare command name plus a list of arguments, which
# are passed directly to subprocess.run with shell=False -- no shell
# is ever invoked at all, so shell metacharacters (;, &&, |, $(...),
# backticks, redirection) have no special meaning whatsoever if they
# appear in an argument; they're just literal text passed to the
# allowlisted command itself. This is a structural guarantee against
# command injection, not a blocklist of dangerous patterns to detect --
# there's no shell present for an injection to exploit in the first
# place. On top of that: only specific, genuinely read-only commands
# are allowed at all, and specific dangerous flags are rejected even
# for allowed commands (sed's -i, find's -delete/-exec), since those
# would give write access through a "read" tool otherwise. This tool
# never requires a workspace and is never confined to one -- it can
# read anywhere this process has filesystem access, matching the
# read-anywhere/write-only-in-workspace split this design is built on.
BASH_ALLOWED_COMMANDS = {
    "ls", "cat", "grep", "find", "head", "tail", "wc", "pwd",
    "sed", "stat", "diff", "sort", "uniq", "file", "tree", "du", "date",
}
BASH_DANGEROUS_FLAGS = {
    "sed": {"-i", "--in-place"},
    "find": {"-delete", "-exec", "-execdir", "-fprintf", "-fprint", "-fprint0", "-fls"},
}
_RAW_OUTPUT_CAP = 50000
_READONLY_TIMEOUT_SECONDS = 60

# Plain GNU grep -r has no idea what a vendor/build directory or a binary
# blob is -- unlike ripgrep, it'll happily churn through .git internals,
# node_modules, and match inside binary DB files, burying real hits in
# noise. Auto-inject sensible default excludes for a recursive grep, but
# only when the model hasn't already narrowed things down itself --
# respect explicit --exclude-dir/--include/--binary-files/-a/-I flags
# rather than overriding them.
_GREP_DEFAULT_EXCLUDE_DIRS = [
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target",
]


def _grep_is_recursive(args: list) -> bool:
    for a in args:
        if a == "--recursive":
            return True
        # combined short flags (e.g. -rn, -Hnr) count too, not just a bare -r/-R
        if a.startswith("-") and not a.startswith("--") and ("r" in a[1:] or "R" in a[1:]):
            return True
    return False


def _grep_has_own_filter(args: list) -> bool:
    for a in args:
        if a.startswith(("--exclude", "--include", "--binary-files")) or a in ("-a", "--text"):
            return True
        # combined short flags (e.g. -rI, -ai) count too, not just bare -I/-a
        if a.startswith("-") and not a.startswith("--") and ("I" in a[1:] or "a" in a[1:]):
            return True
    return False


def _add_grep_default_excludes(args: list) -> list:
    if not _grep_is_recursive(args) or _grep_has_own_filter(args):
        return args
    extra = [f"--exclude-dir={d}" for d in _GREP_DEFAULT_EXCLUDE_DIRS]
    extra.append("--binary-files=without-match")
    return extra + args


# With no real shell (shell=False), there's no automatic glob expansion --
# a model writing "grep pattern *.py" or "sed -n 1,5p src/**/*.ts" the way
# it would in a real shell gets back a literal, confusing "No such file or
# directory" for "*.py" instead of a search across every .py file, because
# nothing ever turned that glob into a list of real paths. That made grep
# and sed effectively unusable for their most common invocation shape.
# Expand globs here in Python instead: pure filesystem listing, no code
# execution, so it adds no injection surface. Only an argument that
# actually matches >=1 real path gets replaced -- an unmatched glob is left
# as the literal string, same as bash's own default (non-nullglob)
# behavior, so a regex/script argument that happens to contain *, ?, or []
# (e.g. grep's own pattern, or sed's script) is untouched unless it
# coincidentally names real files on disk. find is excluded entirely: its
# -name/-path/-iname patterns are matched internally by find itself and
# must reach it as the literal, unexpanded string.
def _expand_globs(command: str, args: list, cwd: str) -> list:
    if command == "find":
        return list(args)
    base = cwd or "."
    expanded = []
    for arg in args:
        if glob.has_magic(arg):
            try:
                matches = sorted(glob.glob(arg, root_dir=base, recursive=True))
            except (OSError, ValueError):
                matches = []
            if matches:
                expanded.extend(matches)
                continue
        expanded.append(arg)
    return expanded


MAX_PIPELINE_STAGES = 5


def _validate_readonly_stage(command: str, args: list):
    """The complete set of per-command safety checks a single bash stage
    needs -- bare-name shape, allowlist membership, dangerous-flag
    rejection, shell-operator-as-literal-text rejection. Shared between a
    lone command and every stage of a pipe_to chain: a pipeline stage is
    exactly as capable of misuse as a standalone call, so it gets exactly
    the same guarantees, never a weaker set just because it's downstream."""
    if not command:
        return {"error": "Missing 'command'. Example: to run grep -n pattern file.txt, set command to 'grep' (just the program name) and args to ['-n', 'pattern', 'file.txt'] (a list of separate arguments)."}
    if not isinstance(command, str) or " " in command or command.startswith("[") or command.startswith('"'):
        return {
            "error": "'" + str(command) + "' looks like a full command line or a JSON array, not a bare program name. "
            "command must be ONLY the program name by itself, e.g. 'grep' -- never the whole command line, "
            "and never the command name repeated inside args. Everything after the program name goes in "
            "args as separate list items instead: to run grep -n pattern file.txt, use "
            "command='grep' and args=['-n', 'pattern', 'file.txt']."
        }
    if command not in BASH_ALLOWED_COMMANDS:
        return {"error": "Command '" + command + "' is not allowed. Allowed commands: " + ", ".join(sorted(BASH_ALLOWED_COMMANDS))}
    if not isinstance(args, list):
        return {"error": "'args' must be a list of strings."}
    dangerous = BASH_DANGEROUS_FLAGS.get(command, set())
    shell_operators = ("|", ">", "<", "&", ";", "$(", "`", "&&", "||")
    for arg in args:
        if not isinstance(arg, str):
            return {"error": "All arguments must be strings."}
        if any(op in arg for op in shell_operators):
            return {
                "error": "Argument '" + arg + "' contains a shell operator (pipe, redirect, chaining, or substitution). "
                "There is no shell here at all -- this tool runs the program directly, so operators like |, >, 2>/dev/null, "
                "&&, or $(...) have no special meaning and can't do what they'd do in a real shell; they'd just be passed "
                "as literal, meaningless text to the program. To actually pipe one command's output into another, use "
                "this tool's own pipe_to parameter instead, e.g. command='grep', args=['-rn', 'foo', '.'], "
                "pipe_to=[{'command': 'wc', 'args': ['-l']}]."
            }
        if arg in dangerous or any(arg.startswith(d) for d in dangerous):
            return {"error": "Argument '" + arg + "' is not allowed for '" + command + "' -- this tool is strictly read-only, no in-place edits or deletions."}
    return None


def _cap_output(output: str) -> str:
    # A hard, silent cutoff here would be unrecoverable -- unlike the
    # separate context-compression layer (tool_output.compress_tool_result),
    # which stores the full result for later retrieval, whatever gets cut
    # off at this point is simply gone. So the cap is generous, and when
    # it's actually hit, that fact is reported explicitly (with an
    # actionable suggestion) rather than the output just quietly ending.
    if len(output) > _RAW_OUTPUT_CAP:
        return (
            output[:_RAW_OUTPUT_CAP]
            + f"\n\n... [TRUNCATED: output exceeded {_RAW_OUTPUT_CAP} characters -- this is not "
            "the complete result. Narrow the search instead of trusting what's shown here: use "
            "grep -l to list matching filenames only, -m to cap matches per file, -c to just count, "
            "or a more specific pattern/path.]"
        )
    return output


def _prepare_stage_args(command: str, args: list, cwd: str) -> list:
    args = _expand_globs(command, args, cwd)
    if command == "grep":
        args = _add_grep_default_excludes(args)
    return args


def _run_single(command: str, args: list, cwd: str):
    try:
        result = subprocess.run(
            [command] + list(args),
            capture_output=True,
            text=True,
            timeout=_READONLY_TIMEOUT_SECONDS,
            shell=False,
            cwd=cwd if cwd else None,
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        return {"command": command, "args": args, "output": _cap_output(output), "exit_code": result.returncode}
    except FileNotFoundError:
        return {"error": "Command '" + command + "' not found on this system."}
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {_READONLY_TIMEOUT_SECONDS} seconds."}
    except Exception as e:
        return {"error": f"bash execution failed: {e}"}


# Real OS-level piping between allowlisted commands, with no shell ever
# involved: each stage is spawned with subprocess.Popen and the next
# stage's stdin is wired directly to the previous stage's stdout file
# descriptor. This is NOT shell=True with a string for the shell to parse
# -- there is still no shell reading these arguments, so none of the
# injection surface that ban is about ever reappears; it's just Python
# connecting two file descriptors together, the same primitive a shell
# itself uses under the hood. Because the pipe is a real OS pipe, normal
# Unix semantics apply for free: a downstream stage that exits early (e.g.
# "head -1") causes the upstream stage to get EPIPE/SIGPIPE on its next
# write and stop, exactly like a real shell pipeline -- nothing extra
# needed to short-circuit it.
def _run_pipeline(stages: list, cwd: str):
    procs = []
    for command, args in stages:
        try:
            proc = subprocess.Popen(
                [command] + list(args),
                stdin=procs[-1].stdout if procs else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                cwd=cwd if cwd else None,
            )
        except FileNotFoundError:
            for p in procs:
                p.kill()
            return {"error": "Command '" + command + "' not found on this system."}
        except Exception as e:
            for p in procs:
                p.kill()
            return {"error": f"bash execution failed: {e}"}
        if procs:
            procs[-1].stdout.close()  # this fd's only other reader is `proc` now; let SIGPIPE propagate correctly
        procs.append(proc)

    # Every stage but the last has its stderr drained on its own thread --
    # otherwise a chatty upstream stage could fill its stderr pipe and
    # deadlock while the main thread is only reading the last stage's
    # output via communicate().
    stderrs = [None] * len(procs)

    def _drain_stderr(i, proc):
        try:
            stderrs[i] = proc.stderr.read()
        except Exception:
            stderrs[i] = ""

    threads = [threading.Thread(target=_drain_stderr, args=(i, p), daemon=True) for i, p in enumerate(procs[:-1])]
    for t in threads:
        t.start()

    last = procs[-1]
    try:
        final_output, final_stderr = last.communicate(timeout=_READONLY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        for p in procs:
            p.kill()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        return {"error": f"Pipeline timed out after {_READONLY_TIMEOUT_SECONDS} seconds."}
    stderrs[-1] = final_stderr

    for t in threads:
        t.join(timeout=_READONLY_TIMEOUT_SECONDS)
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()

    output = final_output or ""
    if stderrs[-1]:
        output += "\n[stderr]\n" + stderrs[-1]
    output = _cap_output(output)

    pipeline_info = []
    for (command, args), proc, stderr in zip(stages, procs, stderrs):
        entry = {"command": command, "args": args, "exit_code": proc.returncode}
        if proc is not last and stderr and stderr.strip():
            entry["stderr"] = stderr[:1000]
        pipeline_info.append(entry)

    return {"pipeline": pipeline_info, "output": output, "exit_code": last.returncode}


def execute_readonly_bash(command: str, args: list, cwd: str = "", pipe_to: list = None):
    err = _validate_readonly_stage(command, args)
    if err:
        return err
    stages = [(command, list(args))]

    if pipe_to:
        if not isinstance(pipe_to, list):
            return {"error": "'pipe_to' must be a list of stages, each like {'command': 'wc', 'args': ['-l']}."}
        if len(pipe_to) > MAX_PIPELINE_STAGES - 1:
            return {"error": f"Too many piped stages -- at most {MAX_PIPELINE_STAGES} commands total in one pipeline (including the first). Make separate calls instead."}
        for i, stage in enumerate(pipe_to):
            if not isinstance(stage, dict):
                return {"error": f"pipe_to[{i}] must be an object with 'command' and 'args', e.g. {{'command': 'wc', 'args': ['-l']}}."}
            stage_command = stage.get("command", "")
            stage_args = stage.get("args", [])
            stage_err = _validate_readonly_stage(stage_command, stage_args)
            if stage_err:
                stage_err["error"] = f"pipe_to[{i}] ('{stage_command}'): " + stage_err["error"]
                return stage_err
            stages.append((stage_command, list(stage_args)))

    prepared = [(cmd, _prepare_stage_args(cmd, a, cwd)) for cmd, a in stages]

    if len(prepared) == 1:
        cmd, a = prepared[0]
        return _run_single(cmd, a, cwd)
    return _run_pipeline(prepared, cwd)


def _rm_is_recursive(args: list) -> bool:
    for a in args:
        if a == "--recursive":
            return True
        if a.startswith("-") and not a.startswith("--") and "r" in a[1:].lower():
            return True
    return False


def _rm_targets_broad(args: list) -> bool:
    positional = [a for a in args if not a.startswith("-")]
    broad = {".", "/", "*", "..", ""}
    return (not positional) or any(p in broad for p in positional)


# Write-capable shell access, confined to a single workspace. Like the
# read-only bash_read_only tool, this never invokes a real shell (shell=False),
# so shell metacharacters in an argument are inert literal text, not
# injection surface. Confinement here is structurally weaker than the
# file tools' resolve_workspace_path though: cwd is fixed to the
# workspace root, but individual arguments aren't path-validated (no
# reliable way to tell "this argument is a path" from "this argument
# is just a string" across an arbitrary allowlisted command). The
# actual safety boundary is: a small allowlist of genuinely useful
# commands, cwd pinned to the workspace, and explicit rejection of the
# two clearly destructive patterns (broad recursive rm, force-push) --
# not a guarantee that no path outside the workspace can ever be named.
BASH_EXEC_ALLOWED_COMMANDS = {
    "npm", "npx", "yarn", "pip", "pip3", "python3", "node", "pytest",
    "git", "make", "mkdir", "touch", "mv", "cp", "rm",
}
BASH_EXEC_TIMEOUT_SECONDS = 300


def _validate_bash_exec_call(command: str, args: list):
    """Shared safety validation for both the blocking and background
    forms of bash_exec -- same allowlist, same shell-operator block,
    same rm/force-push refusals, so a background command gets exactly
    the same structural guarantees a blocking one does. Returns an
    error dict if invalid, or None if the call is safe to run."""
    if not command:
        return {"error": "Missing 'command'. Example: to run npm install, set command to 'npm' (just the program name) and args to ['install'] (a list of separate arguments)."}
    if not isinstance(command, str) or " " in command or command.startswith("[") or command.startswith('"'):
        return {
            "error": "'" + str(command) + "' looks like a full command line or a JSON array, not a bare program name. "
            "command must be ONLY the program name by itself, e.g. 'npm' -- never the whole command line, "
            "and never the command name repeated inside args. Everything after the program name goes in "
            "args as separate list items instead: to run npm install --save-dev foo, use "
            "command='npm' and args=['install', '--save-dev', 'foo']."
        }
    if command not in BASH_EXEC_ALLOWED_COMMANDS:
        return {"error": "Command '" + command + "' is not allowed. Allowed commands: " + ", ".join(sorted(BASH_EXEC_ALLOWED_COMMANDS))}
    shell_operators = ("|", ">", "<", "&", ";", "$(", "`", "&&", "||")
    for arg in args:
        if not isinstance(arg, str):
            return {"error": "All arguments must be strings."}
        if any(op in arg for op in shell_operators):
            return {
                "error": "Argument '" + arg + "' contains a shell operator (pipe, redirect, chaining, or substitution). "
                "There is no shell here at all -- this tool runs the program directly, so operators like |, >, 2>/dev/null, "
                "&&, or $(...) have no special meaning and can't do what they'd do in a real shell; they'd just be passed "
                "as literal, meaningless text to the program. Make separate bash_exec calls instead."
            }
    if command == "rm" and _rm_is_recursive(args) and _rm_targets_broad(args):
        return {"error": "Refusing: recursive rm with a broad or missing target (e.g. '.', '/', '*', '..', or no target at all). Name specific files or directories instead."}
    if command == "git" and args and args[0] == "push":
        force_flags = {"-f", "--force", "--force-with-lease"}
        if any(a in force_flags or a.startswith("--force") for a in args):
            return {"error": "Refusing: force-push is blocked. Run a normal 'git push' instead."}
    return None


ATHENA_SANDBOX_IMAGE = "athena-sandbox:latest"


def _docker_sandbox_available():
    """Checked live on every call rather than cached -- a stale cached
    'yes' would be exactly the false-confidence failure mode a sandbox
    can't afford: if Docker or the image genuinely isn't there right
    now, bash_exec must say so plainly and refuse, never silently run
    unsandboxed."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", ATHENA_SANDBOX_IMAGE],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _build_docker_sandbox_cmd(command, args, workspace):
    """Every bash_exec command runs inside this container, not
    directly on the host -- allowlisting/pattern-blocking in
    _validate_bash_exec_call decides WHETHER a command runs; this
    decides what it can actually reach once it does. Hardened per
    DeepSeek Harness's own real-world Docker practice: dropped
    capabilities, no privilege escalation, read-only root filesystem
    (only the mounted workspace and /tmp are writable), no Docker
    socket, no credential paths, nothing beyond the workspace itself
    visible. Network stays available (install-type commands need it);
    filesystem containment is the actual protection here."""
    real_workspace = os.path.realpath(workspace)
    uid, gid = os.getuid(), os.getgid()
    return [
        "docker", "run", "--rm",
        "--network", "bridge",
        "-v", f"{real_workspace}:{real_workspace}",
        "-w", real_workspace,
        "--user", f"{uid}:{gid}",
        "-e", "HOME=/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--read-only",
        "--tmpfs", "/tmp",
        "--memory", "512m",
        "--cpus", "1",
        "--pids-limit", "100",
        ATHENA_SANDBOX_IMAGE,
        command,
    ] + list(args)


def execute_write_bash(command: str, args: list, workspace: str):
    if not workspace:
        return {"error": "bash_exec requires an active workspace."}
    err = _validate_bash_exec_call(command, args)
    if err:
        return err
    if not _docker_sandbox_available():
        return {"error": "The sandbox container isn't available right now (Docker or the athena-sandbox image is missing) -- refusing to run this command unsandboxed rather than silently skipping the isolation it's supposed to have."}
    try:
        result = subprocess.run(
            _build_docker_sandbox_cmd(command, args, workspace),
            capture_output=True,
            text=True,
            timeout=BASH_EXEC_TIMEOUT_SECONDS,
            shell=False,
        )
        output = result.stdout
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        return {"command": command, "args": args, "output": output[:10000], "exit_code": result.returncode}
    except FileNotFoundError:
        return {"error": "Command '" + command + "' not found on this system."}
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {BASH_EXEC_TIMEOUT_SECONDS} seconds."}
    except Exception as e:
        return {"error": f"bash execution failed: {e}"}


# Background command execution -- same allowlist/safety validation as
# the blocking bash_exec above, but returns immediately with a handle
# instead of waiting for the command to finish. Built as the deliberate
# alternative to a real PTY: gives the actual capability a PTY exists
# for (long-running processes, checking on progress) without losing
# the structural safety guarantee (shell=False, no real shell ever
# involved) that a true interactive terminal can't preserve.
_bg_processes = {}
_bg_processes_lock = threading.Lock()
_bg_process_counter = [0]


def start_background_bash(command: str, args: list, workspace: str):
    if not workspace:
        return {"error": "bash_exec requires an active workspace."}
    err = _validate_bash_exec_call(command, args)
    if err:
        return err
    if not _docker_sandbox_available():
        return {"error": "The sandbox container isn't available right now (Docker or the athena-sandbox image is missing) -- refusing to run this command unsandboxed rather than silently skipping the isolation it's supposed to have."}
    try:
        proc = subprocess.Popen(
            _build_docker_sandbox_cmd(command, args, workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=False,
        )
    except FileNotFoundError:
        return {"error": "Command '" + command + "' not found on this system."}
    except Exception as e:
        return {"error": f"Failed to start background process: {e}"}

    with _bg_processes_lock:
        _bg_process_counter[0] += 1
        process_id = str(_bg_process_counter[0])
        entry = {"proc": proc, "command": command, "args": args, "output": "", "output_lock": threading.Lock(), "started_at": time.time()}
        _bg_processes[process_id] = entry

    def _drain():
        try:
            for line in proc.stdout:
                with entry["output_lock"]:
                    entry["output"] += line
                    if len(entry["output"]) > 50000:
                        entry["output"] = entry["output"][-50000:]
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return {"process_id": process_id, "command": command, "args": args, "status": "started"}


def check_background_bash(process_id: str):
    entry = _bg_processes.get(str(process_id))
    if not entry:
        return {"error": f"No background process with id '{process_id}'. It may have already been stopped, or the id is wrong -- check with a process_id returned by a prior background-start call."}
    proc = entry["proc"]
    with entry["output_lock"]:
        output = entry["output"]
    exit_code = proc.poll()
    return {
        "process_id": process_id, "command": entry["command"], "args": entry["args"],
        "running": exit_code is None, "exit_code": exit_code, "output": output[-10000:],
    }


def stop_background_bash(process_id: str, force: bool = False):
    entry = _bg_processes.get(str(process_id))
    if not entry:
        return {"error": f"No background process with id '{process_id}'."}
    proc = entry["proc"]
    if proc.poll() is not None:
        return {"error": f"Process '{process_id}' has already exited (code {proc.poll()})."}
    try:
        proc.kill() if force else proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception as e:
        return {"error": f"Failed to stop process: {e}"}
    return {"process_id": process_id, "stopped": True, "exit_code": proc.poll()}


BASH_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash_read_only",
            "description": "Run a read-only shell command -- for searching/reading only; it can never write, modify, install, or execute anything. For write-capable operations (npm/pip install, git commit, mkdir, mv, rm, etc.) use the separate bash_exec tool instead, not this one. Only these commands are allowed: ls, cat, grep, find, head, tail, wc, pwd, sed, stat, diff, sort, uniq, file, tree, du, date. Literal shell chaining, pipes, or redirection typed inside an argument string (|, >, <, &&, ;, $(...)) do NOT work -- there's no shell here to interpret them, they'd just be inert text. To actually pipe this command's output into another allowlisted command, use the separate pipe_to parameter instead (e.g. grep piped into wc -l, or into sort, or into a second grep -v to exclude noise) -- this really does connect real OS pipes between real processes, it's just done without ever invoking a shell, so the same safety guarantees apply to every stage. Glob patterns like '*.py' or 'src/**/*.ts' in an argument ARE expanded against real files before the command runs, even without a real shell -- so 'grep -rn foo *.py' and 'sed -n 1,5p src/**/*.ts' work as expected; a glob that matches nothing is passed through unchanged (this only matters for find, whose own -name/-path patterns are always literal since find matches them internally). A recursive grep (-r/-R/--recursive) automatically skips .git, node_modules, __pycache__, venv/.venv, dist, build, .next, target, and binary files by default, unless you pass your own --exclude-dir/--include/--binary-files/-a/-I -- in which case your flags are used as-is instead. sed's -i flag and find's -delete/-exec flags are blocked; this tool can never write or modify anything, on any file, regardless of workspace. Works anywhere on the filesystem this process can read, not limited to any workspace. Commands (and the whole pipeline, if piped) time out after 60 seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bare command name, e.g. 'grep' or 'ls'. No path, no shell operators."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments to the command, each as a separate array element (e.g. [\"-n\", \"pattern\", \"file.txt\"] for grep -n pattern file.txt).",
                    },
                    "pipe_to": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string", "description": "The bare command name for this stage, e.g. 'wc' or 'sort'. Same allowlist as the first command."},
                                "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments for this stage, e.g. [\"-l\"] for wc -l."},
                            },
                            "required": ["command", "args"],
                        },
                        "description": "Optional: additional stages to pipe this command's stdout into, in order, e.g. [{\"command\": \"wc\", \"args\": [\"-l\"]}] to count matches, or [{\"command\": \"sort\", \"args\": []}, {\"command\": \"uniq\", \"args\": [\"-c\"]}] to tally them. Each stage goes through the exact same allowlist and safety checks as the first command. Up to 4 stages here (5 total including the first).",
                    },
                },
                "required": ["command", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec",
            "description": "Run a write-capable shell command, confined to the current workspace (cwd is pinned to the workspace root). Not for searching or reading -- use bash_read_only for grep/sed/find/cat/etc. instead. Only these commands are allowed: npm, npx, yarn, pip, pip3, python3, node, pytest, git, make, mkdir, touch, mv, cp, rm. No shell chaining, pipes, or redirection -- provide the command and its arguments as a separate list, not as one combined string. Recursive rm with a broad target (., /, *, .., or no target) and git push --force are refused. Runs inside an isolated sandbox container: network access works (git clone/push over HTTPS, npm/pip installs), but nothing outside the mounted workspace is visible or writable -- no host SSH keys, no credential files, no other directories. If a command fails specifically because it can't find or write to something outside the workspace, that's this containment working as intended, not a bug to work around. Requires an active workspace; there is no bash_exec without one. Timeout is 300 seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bare command name, e.g. 'npm' or 'git'. No path, no shell operators."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Arguments to the command, each as a separate array element (e.g. [\"install\"] for npm install, or [\"commit\", \"-m\", \"fix bug\"] for git commit -m \"fix bug\").",
                    },
                },
                "required": ["command", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec_start",
            "description": "Start a bash_exec command in the background instead of waiting for it to finish -- for anything long-running (a dev server, a build watcher) or where you need to check progress partway through. Same allowlist and safety rules as bash_exec. Returns a process_id immediately; use bash_exec_check to see output so far, and bash_exec_stop to end it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bare command name, e.g. 'npm'."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments to the command, each as a separate array element."},
                },
                "required": ["command", "args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec_check",
            "description": "Check a background command started with bash_exec_start -- returns its output so far and whether it's still running.",
            "parameters": {
                "type": "object",
                "properties": {"process_id": {"type": "string", "description": "The process_id returned by bash_exec_start."}},
                "required": ["process_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_exec_stop",
            "description": "Stop a background command started with bash_exec_start. Tries a graceful stop first unless force is true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {"type": "string", "description": "The process_id returned by bash_exec_start."},
                    "force": {"type": "boolean", "description": "If true, kill immediately instead of asking it to stop gracefully first."},
                },
                "required": ["process_id"],
            },
        },
    },
]
