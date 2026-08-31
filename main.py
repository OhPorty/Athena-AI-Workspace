"""
Athena — a lean, LCM-backed chat/agent workspace with voice support.
"""
import os
import io
import json
import tempfile
import httpx
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LCM_URL = os.environ.get("ATHENA_LCM_URL", "http://localhost:8420")
OLLAMA_URL = os.environ.get("ATHENA_OLLAMA_URL", "http://localhost:11434/api/chat")
DEFAULT_MODEL = os.environ.get("ATHENA_DEFAULT_MODEL", "gpt-oss-20b-32k:latest")
WHISPER_MODEL_SIZE = os.environ.get("ATHENA_WHISPER_MODEL", "large-v3-turbo")
PIPER_VOICE_PATH = os.environ.get("ATHENA_PIPER_VOICE_PATH", "/home/ohporty/athena/voices/en_US-lessac-medium.onnx")

app = FastAPI(title="Athena")
app.mount("/static", StaticFiles(directory="static"), name="static")

BASE_SYSTEM_PROMPT = (
    "You are Athena, a helpful assistant. Be direct and concise."
)

AGENT_SYSTEM_SUFFIX = (
    " You have access to tools listed below when relevant -- use them "
    "rather than guessing when you need real information. When presenting "
    "structured or tabular data (schedules, forecasts, comparisons, lists "
    "of items with multiple fields each), format it as a real markdown "
    "table with | column | headers | -- not a run-on list of colon-separated "
    "values."
)

# Direct port of Odysseus's proven _pick_dynamic_ctx formula and step
# list (src/llm_core.py) -- same steps, same len(text)//3 + 16000
# headroom, same ceiling default. Not reinvented -- Odysseus's version
# is confirmed working in production; this mirrors it exactly rather
# than trusting a from-scratch reimplementation that kept landing at
# the wrong number in testing.
_CTX_STEPS = [4096, 16384, 32768, 65536, 131072]
MAX_CTX_DEFAULT = int(os.environ.get("ATHENA_MAX_CTX", "65536"))  # matches Odysseus's OLLAMA_NUM_CTX_OVERRIDE

def estimate_tokens(messages: list, tools: list = None) -> int:
    total_chars = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
    if tools:
        total_chars += len(json.dumps(tools))
    return max(1, total_chars // 4)

def pick_dynamic_ctx(messages: list, tools: list = None, max_ctx: int = MAX_CTX_DEFAULT) -> int:
    combined_text = ""
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            combined_text += content
    if tools:
        combined_text += json.dumps(tools)
    needed = len(combined_text) // 3 + 16000
    for step in _CTX_STEPS:
        if step >= needed and step <= max_ctx:
            return step
    return max_ctx


class ChatIn(BaseModel):
    session_id: str
    message: str
    model: str = DEFAULT_MODEL
    max_ctx: int = 0  # 0 = dynamic sizing; any other value is an explicit override
    workspace: str = ""  # empty = no workspace bound; file tools stay disabled
    endpoint_url: str = ""  # empty = use the default OLLAMA_URL
    search_url: str = ""  # SearXNG base URL; empty = web_search/web_fetch tools unavailable


class TtsIn(BaseModel):
    text: str


def get_lcm_context(session_id: str) -> list:
    try:
        resp = httpx.get(f"{LCM_URL}/context/{session_id}", timeout=30)
        print(f"[DEBUG] LCM context status={resp.status_code} body={resp.text[:300]!r}", flush=True)
        if resp.status_code == 200:
            return resp.json().get("context", [])
    except Exception as e:
        print(f"[DEBUG] LCM context EXCEPTION: {e!r}", flush=True)
    return []


def get_lcm_tools() -> list:
    try:
        resp = httpx.get(f"{LCM_URL}/tools/schema", timeout=2)
        if resp.status_code == 200:
            return resp.json().get("tools", [])
    except Exception:
        pass
    return []


def send_to_lcm(session_id: str, role: str, content: str):
    try:
        httpx.post(f"{LCM_URL}/message", json={
            "session_id": session_id,
            "role": role,
            "content": content,
            "service": "athena",
        }, timeout=3)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


class DetectModelsIn(BaseModel):
    url: str

@app.post("/api/detect-models")
def detect_models(req: DetectModelsIn):
    base = req.url.rstrip("/")
    try:
        resp = httpx.get(f"{base}/api/tags", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            names = [m.get("name") for m in data.get("models", []) if m.get("name")]
            return {"models": names}
        return {"error": f"Endpoint responded with HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"Could not reach endpoint: {e}"}


@app.get("/api/workspace/browse")
def browse_workspace(path: str = ""):
    """List subdirectories at a given path, for the workspace picker.
    Defaults to the user's home directory. Only lists directories --
    this is for choosing a workspace ROOT, not general file browsing
    (that's what the model's own list_files tool is for, once a
    workspace is actually bound)."""
    target = os.path.realpath(path) if path else os.path.expanduser("~")
    if not os.path.isdir(target):
        return {"error": f"Not a directory: {target}"}
    try:
        dirs = sorted([
            name for name in os.listdir(target)
            if os.path.isdir(os.path.join(target, name)) and not name.startswith(".")
        ], key=str.lower)
    except PermissionError:
        return {"error": f"Permission denied: {target}"}
    parent = os.path.dirname(target) if target != "/" else None
    return {"path": target, "parent": parent, "directories": dirs}


class MkdirIn(BaseModel):
    path: str
    name: str

@app.post("/api/workspace/mkdir")
def mkdir_workspace(req: MkdirIn):
    """Create a new directory inside the given path, for the workspace
    picker's 'New Folder' action."""
    if not req.name or "/" in req.name or req.name in (".", ".."):
        return {"error": "Invalid folder name."}
    base = os.path.realpath(req.path)
    if not os.path.isdir(base):
        return {"error": f"Not a directory: {base}"}
    new_dir = os.path.join(base, req.name)
    try:
        os.makedirs(new_dir, exist_ok=False)
        return {"path": new_dir}
    except FileExistsError:
        return {"error": "A folder with that name already exists."}
    except Exception as e:
        return {"error": f"Could not create folder: {e}"}

WEB_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and automatically fetch readable content from the top results -- returns full page text, not just snippets, so you usually don't need a separate web_fetch call after this. Use for anything current/external not already in context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract readable text from a specific URL. Use after web_search to read a promising result, or when given a URL directly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]

def _web_search(search_url: str, query: str):
    """Search then auto-fetch the top results concurrently, keeping only
    the ones that actually parsed to real content -- ports Odysseus's
    proven approach (services/search/core.py comprehensive_web_search):
    the model never has to decide "try another URL", since failed/blocked
    fetches are silently filtered out before it ever sees the response.
    This is what fixed Odysseus's own indecisive-looping problem."""
    try:
        resp = httpx.get(f"{search_url.rstrip('/')}/search", params={"q": query, "format": "json"}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"SearXNG responded with HTTP {resp.status_code}"}
        results = resp.json().get("results", [])[:5]
        if not results:
            return {"error": "No search results found."}

        import concurrent.futures
        fetched = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_result = {
                executor.submit(_web_fetch, r.get("url", "")): r
                for r in results if r.get("url")
            }
            for future in concurrent.futures.as_completed(future_to_result):
                r = future_to_result[future]
                try:
                    fetch_result = future.result()
                    if "content" in fetch_result and fetch_result["content"]:
                        fetched.append({
                            "title": r.get("title"),
                            "url": r.get("url"),
                            "content": fetch_result["content"][:2500],
                        })
                except Exception:
                    pass  # a single failed fetch shouldn't fail the whole search

        if not fetched:
            # Every candidate failed to fetch -- fall back to snippets alone
            # rather than returning nothing at all.
            return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")} for r in results]

        return fetched
    except Exception as e:
        return {"error": f"Search failed: {e}"}

def _web_fetch(url: str):
    """Fetch and extract clean readable text -- uses BeautifulSoup for
    real HTML parsing (same technique Odysseus uses), not a regex
    tag-strip. A regex-only approach leaves nav/ad/script text mixed
    into the output, which was confusing local models into treating
    real content as unreliable. Structural elements (lists, headings)
    get separators so the model can still parse a readable shape."""
    try:
        from bs4 import BeautifulSoup
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AthenaBot/1.0)"
        })
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text = "\n".join(lines)

        if not text:
            return {"error": f"web_fetch: {url}: no readable text content (page may need JS)"}

        header = f"# {title}\nSource: {url}\n\n" if title else f"Source: {url}\n\n"
        output = header + text
        return {"url": url, "content": output[:6000]}
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}


FILE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a path within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to workspace root. Use '.' for the root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to workspace root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file, or overwrite an existing one entirely. Use edit_file instead if you only need to change part of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of text in an existing file. old_text must match uniquely -- include enough surrounding context if the text could appear more than once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace root."},
                    "old_text": {"type": "string", "description": "Exact text to find and replace. Must appear exactly once in the file."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
]

def _resolve_workspace_path(workspace: str, rel_path: str) -> str:
    """Resolve a model-supplied relative path against the workspace root,
    refusing to ever resolve outside it. This is the actual safety
    boundary for file tools -- without it, a path like '../../etc/passwd'
    or an absolute path would let the model read/write anywhere on the
    filesystem the Athena process has access to, not just the intended
    workspace directory."""
    workspace_root = os.path.realpath(workspace)
    candidate = os.path.realpath(os.path.join(workspace_root, rel_path or "."))
    if candidate != workspace_root and not candidate.startswith(workspace_root + os.sep):
        raise ValueError(f"Path '{rel_path}' resolves outside the workspace, refusing.")
    return candidate

def _list_files(workspace: str, rel_path: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isdir(target):
            return {"error": f"Not a directory: {rel_path}"}
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
        return {"path": rel_path, "entries": entries}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"list_files failed: {e}"}

def _read_file(workspace: str, rel_path: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": rel_path, "content": content[:20000]}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"read_file failed: {e}"}

def _write_file(workspace: str, rel_path: str, file_content: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(file_content)
        return {"path": rel_path, "written": True, "bytes": len(file_content)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"write_file failed: {e}"}

def _edit_file(workspace: str, rel_path: str, old_text: str, new_text: str):
    try:
        target = _resolve_workspace_path(workspace, rel_path)
        if not os.path.isfile(target):
            return {"error": f"Not a file: {rel_path}"}
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_text)
        if count == 0:
            return {"error": "old_text not found in file"}
        if count > 1:
            return {"error": f"old_text appears {count} times -- must be unique, add more context"}
        content = content.replace(old_text, new_text)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return {"path": rel_path, "edited": True}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"edit_file failed: {e}"}


@app.post("/api/chat")
def chat_stream(req: ChatIn):
    print(f"[DEBUG] model={req.model!r} endpoint_url={req.endpoint_url!r}", flush=True)
    send_to_lcm(req.session_id, "user", req.message)

    context = get_lcm_context(req.session_id)

    # Perpetual agent mode -- one tool set, always available. LCM's recall
    # tools are always on; file write/edit tools (once the Pi bridge is
    # wired in) will only be included here when req.workspace is non-empty,
    # so the model can never edit files on a session with no workspace
    # bound to it, regardless of how it reads an ambiguous prompt.
    system_prompt = BASE_SYSTEM_PROMPT + AGENT_SYSTEM_SUFFIX
    tools = get_lcm_tools()
    if req.search_url:
        tools = tools + WEB_TOOL_SCHEMAS
    if req.workspace:
        tools = tools + FILE_TOOL_SCHEMAS

    _raw_messages = [{"role": "system", "content": system_prompt}] + context
    # Some chat templates (e.g. qwen3.5's) require every system-role
    # message to be grouped at the very start of the conversation --
    # LCM returns one separate system message per summary node, which
    # otherwise ends up interleaved with real user/assistant turns and
    # breaks that requirement. Merge them into a single leading system
    # message, preserving order, then keep everything else as-is.
    _system_parts = [m["content"] for m in _raw_messages if m.get("role") == "system"]
    _non_system = [m for m in _raw_messages if m.get("role") != "system"]
    messages = [{"role": "system", "content": "\n\n".join(_system_parts)}] + _non_system
    effective_max = req.max_ctx if req.max_ctx > 0 else MAX_CTX_DEFAULT
    ctx_size = pick_dynamic_ctx(messages, tools, max_ctx=effective_max)
    if req.max_ctx > 0:
        ctx_size = req.max_ctx
    prompt_tokens = estimate_tokens(messages, tools)

    def _execute_tool_call(tool_call):
        """Dispatch a single tool call. Right now only LCM's own tools
        (lcm_recall_search, lcm_recall_expand) exist -- forward those to
        LCM's generic /tools/call endpoint. Anything else currently has
        nowhere to go and returns a clear error rather than failing silently."""
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name == "web_search":
            return _web_search(req.search_url, args.get("query", ""))
        if name == "web_fetch":
            return _web_fetch(args.get("url", ""))
        if name == "list_files":
            return _list_files(req.workspace, args.get("path", "."))
        if name == "read_file":
            return _read_file(req.workspace, args.get("path", ""))
        if name == "write_file":
            return _write_file(req.workspace, args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            return _edit_file(req.workspace, args.get("path", ""), args.get("old_text", ""), args.get("new_text", ""))

        args["session_id"] = req.session_id
        try:
            resp = httpx.post(f"{LCM_URL}/tools/call", json={"name": name, "arguments": args}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result")
            return {"error": f"Tool call failed: HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": f"Tool call failed: {e}"}

    def generate():
        full_reply = ""
        _target_url = (req.endpoint_url.rstrip("/") + "/api/chat") if req.endpoint_url else OLLAMA_URL
        _messages = list(messages)
        print(f"[DEBUG] _messages roles={[m.get("role") for m in _messages]!r}", flush=True)
        MAX_ROUNDS = 5

        for _round in range(MAX_ROUNDS):
            round_reply = ""
            round_tool_calls = []

            with httpx.stream("POST", _target_url, json={
                "model": req.model,
                "messages": _messages,
                "tools": tools if tools else None,
                "options": {"num_ctx": ctx_size},
                "stream": True,
            }, timeout=180) as resp:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message", {})
                    print(f"[DEBUG] chunk={chunk}", flush=True)
                    thinking_delta = msg.get("thinking", "")
                    if thinking_delta:
                        yield f"data: {json.dumps({'thinking': thinking_delta})}\n\n"
                    delta = msg.get("content", "")
                    if delta:
                        round_reply += delta
                        full_reply += delta
                        yield f"data: {json.dumps({'delta': delta})}\n\n"
                    if msg.get("tool_calls"):
                        round_tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        break

            if not round_tool_calls:
                yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens})}\n\n"
                break

            # Model wants to call tool(s) -- execute each, tell the
            # frontend what's happening, then loop back with results
            # appended so the model can use them for its next turn.
            _messages.append({"role": "assistant", "content": round_reply, "tool_calls": round_tool_calls})
            for tc in round_tool_calls:
                tool_name = tc.get("function", {}).get("name", "unknown")
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"
                result = _execute_tool_call(tc)
                yield f"data: {json.dumps({'type': 'tool_output', 'tool': tool_name, 'output': result})}\n\n"
                _messages.append({"role": "tool", "content": json.dumps(result)})
        else:
            fallback_msg = "I wasn't able to settle on an answer after several tool calls -- the search results may be inconsistent or the page I need isn't easily fetchable. Try rephrasing, or ask me to check a specific source directly."
            full_reply = fallback_msg
            yield f"data: {json.dumps({'delta': fallback_msg})}\n\n"
            yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'note': 'max tool rounds reached'})}\n\n"

        print(f"[DEBUG] full_reply length={len(full_reply)!r} content={full_reply[:200]!r}", flush=True)
        send_to_lcm(req.session_id, "assistant", full_reply)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Voice: faster-whisper (local, free) for STT, Piper (local, free, ONNX) for TTS
# ---------------------------------------------------------------------------
_whisper_model = None
_piper_voice = None

def _get_piper_voice():
    global _piper_voice
    if _piper_voice is None:
        from piper import PiperVoice
        if not os.path.exists(PIPER_VOICE_PATH):
            raise FileNotFoundError(
                f"Piper voice model not found at {PIPER_VOICE_PATH}. "
                f"Download one from https://github.com/rhasspy/piper/releases "
                f"or https://huggingface.co/rhasspy/piper-voices "
                f"(need both the .onnx file and its matching .onnx.json config)."
            )
        _piper_voice = PiperVoice.load(PIPER_VOICE_PATH)
    return _piper_voice

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="float16")
    return _whisper_model


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    try:
        model = _get_whisper_model()
    except Exception as e:
        return {"error": f"Whisper model failed to load: {e}"}

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        segments, _info = model.transcribe(tmp_path, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments)
        return {"text": text}
    except Exception as e:
        return {"error": f"Transcription failed: {e}"}
    finally:
        os.unlink(tmp_path)


@app.post("/api/tts")
async def tts(req: TtsIn):
    import wave
    try:
        voice = _get_piper_voice()
    except Exception as e:
        return {"error": f"Piper TTS unavailable: {e}"}

    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize(req.text, wav_file)
    except Exception as e:
        return {"error": f"TTS synthesis failed: {e}"}

    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")


@app.get("/api/history/{session_id}")
def get_history(session_id: str):
    try:
        resp = httpx.get(f"{LCM_URL}/messages/{session_id}", timeout=3)
        if resp.status_code == 200:
            msgs = resp.json()
            return [{"role": m["role"], "content": m["content"]} for m in msgs]
    except Exception:
        pass
    return []

@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("ATHENA_PORT", "9500"))
    uvicorn.run(app, host="0.0.0.0", port=port)
