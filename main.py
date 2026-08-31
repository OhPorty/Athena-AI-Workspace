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
    "rather than guessing when you need real information."
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


class TtsIn(BaseModel):
    text: str


def get_lcm_context(session_id: str) -> list:
    try:
        resp = httpx.get(f"{LCM_URL}/context/{session_id}", timeout=3)
        if resp.status_code == 200:
            return resp.json().get("context", [])
    except Exception:
        pass
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

@app.post("/api/chat")
def chat_stream(req: ChatIn):
    send_to_lcm(req.session_id, "user", req.message)

    context = get_lcm_context(req.session_id)

    # Perpetual agent mode -- one tool set, always available. LCM's recall
    # tools are always on; file write/edit tools (once the Pi bridge is
    # wired in) will only be included here when req.workspace is non-empty,
    # so the model can never edit files on a session with no workspace
    # bound to it, regardless of how it reads an ambiguous prompt.
    system_prompt = BASE_SYSTEM_PROMPT + AGENT_SYSTEM_SUFFIX
    tools = get_lcm_tools()
    # TODO once Pi bridge exists: if req.workspace: tools += get_file_tools()

    messages = [{"role": "system", "content": system_prompt}] + context
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
            yield f"data: {json.dumps({'done': True, 'ctx_used': ctx_size, 'prompt_tokens': prompt_tokens, 'note': 'max tool rounds reached'})}\n\n"

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
