"""Text views of the non-text files Athena makes and reads in a workspace.

read_file used to return mojibake for anything that wasn't plain text, so a model that built a
.docx or .xlsx could never check its own work. describe() returns something a model can read
instead: the text of Office files and PDFs, the size of an image, the listing of an archive.
"""
import os
import zipfile

_MAX_CHARS = 2_000_000          # extraction stops here; read_file pages through what was kept
_MAX_SHEET_ROWS = 5000
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff"}


def _docx(path):
    import docx
    document = docx.Document(path)
    lines = [p.text for p in document.paragraphs]
    for t, table in enumerate(document.tables, 1):
        lines.append(f"\n[table {t}]")
        lines += [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
    return "\n".join(lines)


def _xlsx(path):
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=False)
    out = []
    for sheet in book.worksheets:
        out.append(f"[sheet {sheet.title}]")
        for n, row in enumerate(sheet.iter_rows(values_only=True), 1):
            if n > _MAX_SHEET_ROWS:
                out.append(f"... more rows not shown (limit {_MAX_SHEET_ROWS})")
                break
            out.append("\t".join("" if v is None else str(v) for v in row))
    book.close()
    return "\n".join(out)


def _pptx(path):
    from pptx import Presentation
    out = []
    for n, slide in enumerate(Presentation(path).slides, 1):
        out.append(f"[slide {n}]")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                out += [" | ".join(c.text for c in row.cells) for row in shape.table.rows]
    return "\n".join(out)


def _pdf(path):
    import pypdf
    reader = pypdf.PdfReader(path)
    return "\n".join(f"[page {n}]\n{page.extract_text() or ''}" for n, page in enumerate(reader.pages, 1))


def _zip(path):
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        lines = [f"{i.file_size:>10}  {i.filename}" for i in infos[:500]]
    if len(infos) > 500:
        lines.append(f"... and {len(infos) - 500} more entries")
    return f"{len(infos)} entries (size in bytes, name):\n" + "\n".join(lines)


def _db(x):
    import math
    return round(20 * math.log10(max(float(x), 1e-6)), 1)


def _wav(path):
    """What a listener would notice, in numbers: a model can't hear its own output, and a mixdown
    with mismatched levels or a broken filter looks fine to every other check."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(min(frames, rate * 300))      # the first five minutes is plenty to judge
    seconds = frames / rate
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        data = (((b[:, 2] << 24) | (b[:, 1] << 16) | (b[:, 0] << 8)) >> 8) / 8388608
    else:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648
    x = data[: len(data) // channels * channels].reshape(-1, channels).mean(axis=1)
    header = f"WAV audio: {seconds:.1f} s, {'stereo' if channels == 2 else f'{channels} channel(s)'}, {rate} Hz, {width * 8}-bit"
    if len(x) < rate // 2:
        return header + "\nToo short to analyse."
    peak, rms = float(np.abs(x).max()), float(np.sqrt(np.mean(x ** 2)))
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    share = [power[(freqs >= lo) & (freqs < hi)].sum() / max(power.sum(), 1e-20) * 100 for lo, hi in ((0, 250), (250, 2000), (2000, 6000), (6000, rate / 2))]
    window = rate * 5
    levels = [_db(np.sqrt(np.mean(x[i:i + window] ** 2))) for i in range(0, len(x) - window // 2, window)][:12]

    # Notes and chords make a few sharp peaks in the long-run spectrum; noise makes it flat.
    frame = rate
    spectra = [np.abs(np.fft.rfft(x[i:i + frame] * np.hanning(frame))) ** 2 for i in range(0, len(x) - frame, frame // 2)]
    band_power = np.mean(spectra, axis=0)[(np.fft.rfftfreq(frame, 1 / rate) >= 100) & (np.fft.rfftfreq(frame, 1 / rate) < 5000)]
    flatness = float(np.exp(np.mean(np.log(band_power + 1e-20))) / max(np.mean(band_power), 1e-20))
    pitched = flatness < 0.2

    # A beat shows up as the loudness jumping at regular intervals.
    hop = max(1, int(rate * 0.01))
    envelope = np.array([np.sqrt(np.mean(x[i:i + hop * 2] ** 2)) for i in range(0, len(x) - hop * 2, hop)])
    jumps = np.maximum(0, np.diff(envelope))
    jumps = jumps - jumps.mean()
    auto = np.correlate(jumps, jumps, "full")[len(jumps) - 1:]
    auto = auto / (auto[0] + 1e-12)
    lo, hi = int(0.25 / 0.01), min(int(1.0 / 0.01), len(auto) - 1)
    pulse = float(auto[lo:hi].max()) if hi > lo else 0.0
    if envelope.size and envelope.std() < 0.05 * max(envelope.mean(), 1e-9):
        pulse = 0.0                      # the level never moves, so there is nothing to call a beat
    rhythm = "clear regular beat" if pulse >= 0.3 else "some regular pulse" if pulse >= 0.1 else "no regular beat"

    problems = []
    if peak < 0.01:
        problems.append("nearly silent")
    elif _db(rms) < -40:
        problems.append(f"very quiet (average {_db(rms)} dBFS); scale it up before saving")
    if float(np.mean(np.abs(x) >= 0.999)) > 0.005:
        problems.append("clipping (the waveform hits full scale); lower the gain or normalise before saving")
    if peak >= 0.01 and not pitched:
        problems.append(
            f"no pitched notes at all and {share[3]:.0f}% of the energy is above 6 kHz: this is noise or hiss, not music. "
            "If you mixed several layers, print each layer's peak before the final normalise: a layer written in raw "
            "16-bit units mixed with layers in the -1..1 range will drown out everything else"
        )
    lines = [
        header,
        f"Loudness: peak {_db(peak)} dBFS, average {_db(rms)} dBFS",
        f"Energy by frequency: bass (<250 Hz) {share[0]:.0f}%, low-mid (250 Hz-2 kHz) {share[1]:.0f}%, high-mid (2-6 kHz) {share[2]:.0f}%, treble (>6 kHz) {share[3]:.0f}%",
        "Average level per 5 s (dBFS): " + ", ".join(str(v) for v in levels),
        f"Pitched notes: {'yes' if pitched else 'none (noise-like)'}; rhythm: {rhythm}",
    ]
    lines.append("Problems: " + "; ".join(problems) if problems else "Problems: none found. (Only the sound's measurements are checked here; whether it is good music is for a listener to judge.)")
    return "\n".join(lines)


def _image(path):
    from PIL import Image
    with Image.open(path) as im:
        return f"{im.format or 'image'}, {im.width}x{im.height} pixels, mode {im.mode}"


_READERS = {".wav": _wav, ".docx": _docx, ".xlsx": _xlsx, ".xlsm": _xlsx, ".pptx": _pptx, ".pdf": _pdf, ".zip": _zip}


def describe(path):
    """-> (kind, text) for a file that isn't plain text, or None when it is plain text.
    Raises nothing: a damaged file comes back as a description of the damage."""
    ext = os.path.splitext(path)[1].lower()
    reader = _READERS.get(ext) or (_image if ext in _IMAGE_EXTENSIONS else None)
    size = os.path.getsize(path)
    if reader is None:
        with open(path, "rb") as f:
            if b"\0" not in f.read(8192):
                return None
        return "binary", f"Binary file ({size} bytes, {ext or 'no extension'}); there is no text view of it."
    try:
        return ext.lstrip(".") or "binary", reader(path)[:_MAX_CHARS]
    except ImportError as e:
        return "binary", f"Binary {ext} file ({size} bytes); can't read it here: {e}"
    except Exception as e:
        return "binary", f"Could not read this {ext} file ({size} bytes); it may be damaged or not really a {ext} file: {e}"
