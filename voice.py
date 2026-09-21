import os
import io
import tempfile

from fastapi import APIRouter, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Voice: faster-whisper (local, free) for STT, Piper (local, free, ONNX) for TTS
# ---------------------------------------------------------------------------
WHISPER_MODEL_SIZE = os.environ.get("ATHENA_WHISPER_MODEL", "large-v3-turbo")
PIPER_VOICE_PATH = os.environ.get("ATHENA_PIPER_VOICE_PATH", "/home/ohporty/athena/voices/en_US-lessac-medium.onnx")

router = APIRouter()

_whisper_model = None
_piper_voice = None


class TtsIn(BaseModel):
    text: str


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


@router.post("/api/transcribe")
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


@router.post("/api/tts")
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
