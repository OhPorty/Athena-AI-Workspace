"""A small synthesizer toolkit (numpy). Every sound is a float array between -1 and 1 and every time
is in seconds, so layers can be mixed freely; Track.render does the one final level adjustment.
Write the music in song.py; change this file only to add instruments or effects."""
import wave

import numpy as np

SR = 44100
_rng = np.random.default_rng(1)


def seed(n):
    """Make the noise-based sounds (snare, hats, vinyl) repeatable: the same seed gives the same track."""
    global _rng
    _rng = np.random.default_rng(n)

# ---------- notes and chords ----------
_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
CHORDS = {
    "maj": [0, 4, 7], "min": [0, 3, 7], "maj7": [0, 4, 7, 11], "m7": [0, 3, 7, 10], "7": [0, 4, 7, 10],
    "m9": [0, 3, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "13": [0, 4, 10, 14, 21], "sus2": [0, 2, 7], "sus4": [0, 5, 7],
    "dim": [0, 3, 6], "m11": [0, 3, 7, 10, 14, 17],
}


def note(name):
    """'C4' -> 60, 'F#3' -> 54, 'Bb2' -> 46 (middle C is C4)."""
    n = _NAMES[name[0].upper()]
    for ch in name[1:-1]:
        n += 1 if ch == "#" else -1 if ch == "b" else 0
    return n + 12 * (int(name[-1]) + 1)


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def chord(root, kind="m7"):
    """chord('D3', 'm9') -> list of MIDI notes."""
    base = note(root) if isinstance(root, str) else root
    return [base + i for i in CHORDS[kind]]


def clock(bpm, swing=0.0):
    """-> at(bar, beats): the time in seconds of a position in the song. swing delays the off-beat
    eighths (0 = straight, 0.15 = relaxed lofi shuffle, 0.33 = full triplet feel)."""
    beat = 60.0 / bpm

    def at(bar, beats=0.0):
        eighths = beats * 2
        off = abs(eighths - round(eighths)) < 1e-6 and int(round(eighths)) % 2 == 1
        return (bar * 4 + beats) * beat + (swing * beat if off else 0.0)

    at.beat = beat
    return at


# ---------- filters ----------
def lowpass(x, cutoff):
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spectrum / (1 + (freqs / cutoff) ** 4), len(x))


def highpass(x, cutoff):
    return x - lowpass(x, cutoff)


def _fit(x, peak=1.0):
    return x * (peak / max(np.abs(x).max(), 1e-9))


def _time(dur):
    return np.arange(int(SR * dur)) / SR


def _shape(t, dur, attack, decay, release=0.05):
    return np.minimum(1, t / attack) * np.exp(-t * decay) * np.minimum(1, np.maximum(0, (dur - t) / release))


# ---------- instruments (all return peak <= 1) ----------
def epiano(midi, dur, vel=0.8):
    """Soft electric piano: sine body plus a quick bell-like partial, slow tremolo."""
    t, f = _time(dur), hz(midi)
    bell = np.sin(2 * np.pi * f * t + 2.2 * np.exp(-t * 7) * np.sin(2 * np.pi * f * 14 * t))
    body = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(2 * np.pi * f * 2 * t)
    return _fit((0.8 * body + 0.35 * bell) * _shape(t, dur, 0.008, 2.3, 0.15) * (1 + 0.08 * np.sin(2 * np.pi * 4.2 * t)), vel)


def pad(midis, dur, vel=0.6):
    """Warm, slowly swelling chord from slightly detuned saws."""
    t = _time(dur)
    out = sum(np.sin(2 * np.pi * hz(m) * t) + 0.5 * np.sin(2 * np.pi * hz(m) * 1.004 * t) for m in midis)
    return _fit(lowpass(out, 1800) * np.minimum(1, t / 0.4) * np.minimum(1, np.maximum(0, (dur - t) / 0.5)), vel)


def bass(midi, dur, vel=0.8):
    """Warm bass. The harmonics matter: a bare low sine is nearly inaudible on laptop speakers and earbuds."""
    t, f = _time(dur), hz(midi)
    tone = sum(a * np.sin(2 * np.pi * f * h * t) for h, a in ((1, 1.0), (2, 0.7), (3, 0.45), (4, 0.25), (5, 0.12)))
    return _fit(np.tanh(1.3 * tone) * _shape(t, dur, 0.01, 1.4, 0.08), vel)


def pluck(midi, dur, vel=0.7):
    t, f = _time(dur), hz(midi)
    return _fit((np.sin(2 * np.pi * f * t) + 0.4 * np.sin(2 * np.pi * f * 2 * t)) * _shape(t, dur, 0.003, 6, 0.03), vel)


def piano(midi, dur, vel=0.8):
    """Felt-soft piano: a few harmonics, the upper ones dying faster."""
    t, f = _time(dur), hz(midi)
    body = sum(a * np.sin(2 * np.pi * f * h * t) * np.exp(-t * (1.0 + 1.1 * h)) for h, a in ((1, 1.0), (2, 0.5), (3, 0.3), (4, 0.16), (5, 0.08)))
    return _fit(body * np.minimum(1, t / 0.004) * np.minimum(1, np.maximum(0, (dur - t) / 0.12)), vel)


def guitar(midi, dur, vel=0.7):
    """Nylon-string pluck: bright at the start, mellowing as the upper harmonics fade."""
    t, f = _time(dur), hz(midi)
    body = sum((1.0 / h) * np.sin(2 * np.pi * f * h * t) * np.exp(-t * (2.2 + 1.8 * h)) for h in range(1, 8))
    return _fit(body * np.minimum(1, t / 0.003) * np.minimum(1, np.maximum(0, (dur - t) / 0.08)), vel)


def vibes(midi, dur, vel=0.6):
    """Vibraphone: a pure tone with a bell-like partial and the slow motor tremolo."""
    t, f = _time(dur), hz(midi)
    body = np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * f * 3.99 * t) * np.exp(-t * 6) + 0.1 * np.sin(2 * np.pi * f * 9.9 * t) * np.exp(-t * 14)
    return _fit(body * np.exp(-t * 1.3) * (1 + 0.25 * np.sin(2 * np.pi * 5 * t)) * np.minimum(1, t / 0.005) * np.minimum(1, np.maximum(0, (dur - t) / 0.2)), vel)


def musicbox(midi, dur, vel=0.5):
    """Music box: a high tone with inharmonic bell partials and a quick decay."""
    t, f = _time(dur), hz(midi)
    body = np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 2.76 * t) * np.exp(-t * 4) + 0.25 * np.sin(2 * np.pi * f * 5.4 * t) * np.exp(-t * 9)
    return _fit(body * np.exp(-t * 3.2) * np.minimum(1, t / 0.002) * np.minimum(1, np.maximum(0, (dur - t) / 0.05)), vel)


def lead(midi, dur, vel=0.5, wave="square"):
    t, f = _time(dur), hz(midi)
    shape = {"sine": np.sin(2 * np.pi * f * t), "square": np.sign(np.sin(2 * np.pi * f * t)), "saw": 2 * ((f * t) % 1) - 1}[wave]
    return _fit(shape * _shape(t, dur, 0.01, 1.5, 0.03), vel)


def kick(vel=0.9):
    """Soft kick: a falling sine for the thump plus a short click so it is heard on small speakers too."""
    t = _time(0.4)
    body = np.sin(2 * np.pi * np.cumsum(55 + 140 * np.exp(-t * 30)) / SR) * np.exp(-t * 9)
    click = highpass(_rng.uniform(-1, 1, len(t)), 1500) * np.exp(-t * 400) * 0.5
    return _fit(body + click, vel)


def snare(vel=0.7):
    t = _time(0.3)
    return _fit(0.6 * _rng.uniform(-1, 1, len(t)) * np.exp(-t * 20) + 0.4 * np.sin(2 * np.pi * 185 * t) * np.exp(-t * 25), vel)


def hat(open_=False, vel=0.4):
    t = _time(0.14 if open_ else 0.04)
    return _fit(highpass(_rng.uniform(-1, 1, len(t)), 6000) * np.exp(-t * (30 if open_ else 90)), vel)


def rim(vel=0.5):
    """Side-stick click."""
    t = _time(0.08)
    return _fit(np.sin(2 * np.pi * 1700 * t) * np.exp(-t * 90) + 0.5 * highpass(_rng.uniform(-1, 1, len(t)), 2500) * np.exp(-t * 110), vel)


def shaker(vel=0.3):
    t = _time(0.09)
    return _fit(highpass(_rng.uniform(-1, 1, len(t)), 5000) * np.minimum(1, t / 0.012) * np.exp(-t * 35), vel)


# ---------- texture ----------
def rain(seconds, level=0.05):
    """Steady rain: filtered noise that breathes slowly."""
    n = int(SR * seconds)
    noise = highpass(lowpass(_rng.normal(0, 1, n), 7000), 500)
    t = np.arange(n) / SR
    return np.clip(level * _fit(noise, 1.0) * (0.8 + 0.2 * np.sin(2 * np.pi * 0.07 * t)), -1, 1)


def vinyl(seconds, level=0.03):
    """Vinyl crackle and a little hiss. level is the loudest pop relative to a full-scale signal."""
    n = int(SR * seconds)
    pops = (_rng.random(n) < 0.0004) * _rng.uniform(-1, 1, n)
    hiss = lowpass(_rng.normal(0, 1, n), 5000) * 0.1
    return np.clip(level * (pops + hiss * 0.15), -1, 1)


# ---------- mixing ----------
class Track:
    """The song so far: stereo, float. Add sounds at times, then render once."""

    def __init__(self, seconds, tail=2.0):
        self.left = np.zeros(int(SR * (seconds + tail)), dtype=np.float32)   # float32: a long track is tens of millions of samples
        self.right = np.zeros_like(self.left)

    def add(self, sound, at, gain=1.0, pan=0.0):
        """sound: a -1..1 array. at: seconds. gain: 0..1 level in the mix. pan: -1 left .. 1 right."""
        sound = np.asarray(sound, dtype=float)
        if len(sound) and np.abs(sound).max() > 4:
            raise ValueError("this sound is far outside -1..1 (raw 16-bit units?); scale it to -1..1 before adding it")
        i = max(0, int(at * SR))          # humanised timing can land just before 0; start those at the very beginning
        if i >= len(self.left):
            return
        sound = sound[: len(self.left) - i] * gain
        self.left[i:i + len(sound)] += sound * (1 - max(0, pan))
        self.right[i:i + len(sound)] += sound * (1 + min(0, pan))

    def render(self, path="song.wav", lowpass_hz=None, fade_in=1.0, fade_out=2.5, peak=0.8, wobble=0.0):
        """Filter, soft-clip, set the overall level, fade, and write a 16-bit stereo WAV.
        wobble (seconds, try 0.0004) adds the slow pitch drift of worn tape."""
        left, right = self.left, self.right
        if wobble:
            t = np.arange(len(left)) / SR
            drift = wobble * SR * (np.sin(2 * np.pi * 0.35 * t) + 0.5 * np.sin(2 * np.pi * 1.1 * t + 1.3))
            position = np.arange(len(left)) + drift
            left, right = np.interp(position, np.arange(len(left)), left), np.interp(position, np.arange(len(right)), right)
        if lowpass_hz:
            left, right = lowpass(left, lowpass_hz), lowpass(right, lowpass_hz)
        left, right = np.tanh(left), np.tanh(right)
        scale = peak / max(np.abs(left).max(), np.abs(right).max(), 1e-9)
        t = np.arange(len(left)) / SR
        fade = np.minimum(1, t / max(fade_in, 1e-3)) * np.minimum(1, (len(left) / SR - t) / max(fade_out, 1e-3))
        pcm = (np.stack([left * scale * fade, right * scale * fade], axis=1) * 32767).astype(np.int16)
        with wave.open(path, "wb") as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(SR)
            f.writeframes(pcm.tobytes())
        print(f"wrote {path}: {len(left) / SR:.1f} s, peak {np.abs(pcm).max() / 32767:.2f}")
