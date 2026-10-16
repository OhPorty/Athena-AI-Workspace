"""generate_background_music: a simple instrumental track for video, stream or study backgrounds.

The model can steer it with a few plain choices (style, instrument, tempo, chords ...); this module
owns the sound, timing, arrangement and mixing, so a small model never writes signal-processing code.
Whatever the model leaves open is chosen from the seed -- the key, the progression, the instrument,
the chord rhythm, the bass line, the drum groove, the melody and the texture -- so two calls differ
in more than tempo and tone. The instruments come from the numpy toolkit the `audio` scaffold ships
(scaffolds/audio/synth.py), loaded from there so there is one copy. Output is a WAV in the workspace
plus a measured report (binary_files).
"""
import math
import os
import re
import shutil
import tempfile
import threading
import time
import types

import numpy as np

import backup_tools
import binary_files
import file_tools
import generated_files

_SYNTH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scaffolds", "audio", "synth.py")
_synth = None
_render_lock = threading.Lock()          # one render at a time: a long track holds a few hundred MB
_layer_hook = None                       # tests set this to see each layer (name, sound, time, gain)

MAX_MINUTES = 8
_BRIGHTNESS = {"dark": 2500, "warm": 3800, "bright": 6500}
_ALIASES = {"": "maj", "m": "min", "min": "min", "maj": "maj", "M7": "maj7", "maj7": "maj7", "min7": "m7", "m7": "m7",
            "7": "7", "M9": "maj9", "maj9": "maj9", "min9": "m9", "m9": "m9", "13": "13", "m11": "m11", "min11": "m11",
            "sus2": "sus2", "sus4": "sus4", "dim": "dim"}
_CHORD = re.compile(r"^([A-Ga-g][#b]?)(maj9|maj7|min11|min9|min7|maj|min|m11|m9|m7|M9|M7|sus2|sus4|dim|13|7|m|)$")

# ---------- the musical choices ----------
KEYS = ("epiano", "piano", "guitar", "vibes", "musicbox", "pad")
COMPS = ("rolled", "pairs", "sustain", "offbeat", "syncopated", "arpeggio")
BASSES = ("rootfifth", "pedal", "walking", "syncopated", "octave", "none")
GROOVES = ("boombap", "halftime", "sparse", "shuffle", "none")
TEXTURES = ("vinyl", "rain", "tape", "none")
LEADS = ("vibes", "musicbox", "epiano", "guitar", "piano")

# Each style is a set of leanings, not a fixed arrangement: every field is a pool that the seed picks from.
STYLES = {
    "chill": {"keys": ("epiano", "piano"), "comp": ("rolled", "pairs", "syncopated"), "bass": ("rootfifth", "pedal"), "groove": ("boombap", "halftime"),
              "melody": ("sparse", "motif"), "texture": ("vinyl", "vinyl", "tape"), "bpm": (68, 84), "swing": (0.12, 0.2), "brightness": ("warm", "dark"), "minor": 0.35},
    "jazzy": {"keys": ("piano", "epiano", "vibes"), "comp": ("syncopated", "offbeat", "rolled"), "bass": ("walking", "syncopated"), "groove": ("shuffle", "boombap"),
              "melody": ("motif",), "texture": ("vinyl", "none"), "bpm": (82, 98), "swing": (0.2, 0.3), "brightness": ("warm", "bright"), "minor": 0.3},
    "dreamy": {"keys": ("pad", "epiano", "vibes"), "comp": ("sustain", "pairs"), "bass": ("pedal", "rootfifth"), "groove": ("sparse", "none", "halftime"),
               "melody": ("sparse",), "texture": ("rain", "vinyl", "tape"), "bpm": (58, 72), "swing": (0.05, 0.15), "brightness": ("dark", "warm"), "minor": 0.5},
    "acoustic": {"keys": ("guitar",), "comp": ("arpeggio", "rolled", "offbeat"), "bass": ("rootfifth", "octave"), "groove": ("halftime", "sparse"),
                 "melody": ("motif", "sparse"), "texture": ("vinyl", "tape", "none"), "bpm": (66, 86), "swing": (0.08, 0.18), "brightness": ("warm",), "minor": 0.4},
    "upbeat": {"keys": ("piano", "epiano", "guitar"), "comp": ("offbeat", "syncopated", "arpeggio"), "bass": ("octave", "syncopated", "rootfifth"), "groove": ("boombap", "shuffle"),
               "melody": ("motif",), "texture": ("vinyl", "none"), "bpm": (88, 106), "swing": (0.08, 0.18), "brightness": ("bright", "warm"), "minor": 0.2},
    "minimal": {"keys": ("piano", "musicbox", "vibes"), "comp": ("pairs", "sustain"), "bass": ("pedal", "none"), "groove": ("none", "sparse"),
                "melody": ("sparse", "sparse", "motif"), "texture": ("tape", "rain", "vinyl"), "bpm": (60, 78), "swing": (0.05, 0.15), "brightness": ("warm", "dark"), "minor": 0.45},
}

# Roman-numeral progressions as (semitones above the key, chord kind); one is picked and transposed to a random key.
_MAJOR_PROGRESSIONS = [
    [(2, "m9"), (7, "13"), (0, "maj9"), (9, "m9")], [(0, "maj9"), (9, "m9"), (2, "m9"), (7, "13")],
    [(5, "maj9"), (4, "m7"), (2, "m9"), (0, "maj9")], [(0, "maj7"), (5, "maj7"), (4, "m7"), (9, "m7")],
    [(9, "m9"), (5, "maj9"), (0, "maj9"), (7, "13")], [(0, "maj9"), (4, "m7"), (5, "maj9"), (7, "7")],
    [(2, "m7"), (7, "7"), (0, "maj7")], [(0, "maj7"), (9, "m7"), (5, "maj7"), (7, "sus4")],
]
_MINOR_PROGRESSIONS = [
    [(0, "m9"), (8, "maj9"), (3, "maj7"), (10, "13")], [(0, "m7"), (5, "m9"), (10, "7"), (3, "maj9")],
    [(0, "m9"), (10, "maj7"), (8, "maj9"), (7, "7")], [(0, "m11"), (5, "m7"), (7, "m7"), (0, "m9")],
    [(0, "m9"), (3, "maj7"), (8, "maj7"), (10, "7")],
]
_KEY_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")

# Mix levels, set by what is heard on small speakers (equal-loudness weighted), not by raw energy:
# a low bass or kick holds a lot of energy and almost no audible loudness.
GAINS = {"chords": 0.16, "bass": 0.64, "melody": 0.19, "kick": 1.15, "snare": 0.62, "hat": 1.25, "rim": 0.7, "shaker": 1.2}
# Instruments differ in how loud they sound at equal peak; these bring them to the electric piano's level.
_LOUDNESS = {"epiano": 1.0, "piano": 1.05, "guitar": 1.5, "vibes": 1.4, "musicbox": 2.2, "pad": 2.6}

BACKGROUND_MUSIC_TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "generate_background_music",
        "description": (
            "Make a simple instrumental background track (chords, bass, optional drums and melody, soft texture) for a video, "
            "stream or study session, saved as a WAV in the workspace. Anything you leave out is chosen for you and differs each "
            "call, so set only what the request calls for. A plain synthesized loop: not a realistic band, no vocals. "
            "Returns the file, the choices made and a measured report."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "minutes": {"type": "number", "description": f"0.25 to {MAX_MINUTES}, default 3"},
                "style": {"type": "string", "enum": list(STYLES)},
                "keys": {"type": "string", "enum": list(KEYS), "description": "chord instrument"},
                "comp": {"type": "string", "enum": list(COMPS), "description": "how chords are played"},
                "bass": {"type": "string", "enum": list(BASSES)},
                "groove": {"type": "string", "enum": list(GROOVES), "description": "drum feel"},
                "texture": {"type": "string", "enum": list(TEXTURES)},
                "brightness": {"type": "string", "enum": list(_BRIGHTNESS)},
                "melody": {"type": "boolean"},
                "drums": {"type": "boolean"},
                "bpm": {"type": "integer", "description": "50-140"},
                "chords": {"type": "array", "items": {"type": "string"}, "description": "repeating, e.g. [\"Dm9\",\"G13\",\"Cmaj9\"]; kinds maj min 7 maj7 m7 maj9 m9 13 m11 sus2 sus4 dim"},
                "bars_per_chord": {"type": "integer", "enum": [1, 2, 4]},
                "swing": {"type": "number", "description": "0-0.33"},
                "vinyl": {"type": "number", "description": "texture amount 0-1"},
                "seed": {"type": "integer", "description": "same seed and settings repeat the track"},
            },
        },
    },
}]


def _load_synth():
    global _synth
    if _synth is None:
        module = types.ModuleType("athena_synth")
        module.__file__ = _SYNTH_PATH
        with open(_SYNTH_PATH, encoding="utf-8") as f:       # exec the source: importing would write a __pycache__ into the scaffold folder
            exec(compile(f.read(), _SYNTH_PATH, "exec"), module.__dict__)
        _synth = module
    return _synth


def parse_chord(name):
    """'Dm9' -> ('D', 'm9'), 'Bbmaj7' -> ('Bb', 'maj7'); raises ValueError naming what is supported."""
    match = _CHORD.match(str(name).strip())
    if not match or match.group(2) not in _ALIASES:
        raise ValueError(f"'{name}' is not a chord I know. Use a root A-G (with # or b) plus one of: maj, min, 7, maj7, m7, maj9, m9, 13, m11, sus2, sus4, dim (for example Dm9, G13, Bbmaj7).")
    root = match.group(1)
    return root[0].upper() + root[1:], _ALIASES[match.group(2)]


def _number(args, key, default, low, high, cast=float):
    value = args.get(key)
    if value is None:
        return default
    try:
        value = cast(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


def _choice(args, key, allowed):
    value = args.get(key)
    if value is None or value == "":
        return None
    value = str(value).strip().lower()
    if value not in allowed:
        raise ValueError(f"{key} must be one of: {', '.join(allowed)}")
    return value


def random_progression(rng, minor):
    pool = _MINOR_PROGRESSIONS if minor else _MAJOR_PROGRESSIONS
    steps = pool[int(rng.integers(len(pool)))]
    key = int(rng.integers(12))
    return [_KEY_NAMES[(key + offset) % 12] + kind for offset, kind in steps]


def _settings(args):
    raw_seed = args.get("seed")
    seed = int(raw_seed) if isinstance(raw_seed, (int, float)) and not isinstance(raw_seed, bool) else int(time.time() * 1000) % 1_000_000
    rng = np.random.default_rng(seed)
    pick = lambda pool: pool[int(rng.integers(len(pool)))]
    style = _choice(args, "style", list(STYLES)) or pick(list(STYLES))
    lean = STYLES[style]
    s = {"seed": seed, "style": style}
    s["keys"] = _choice(args, "keys", KEYS) or pick(lean["keys"])
    s["comp"] = _choice(args, "comp", COMPS) or pick(lean["comp"])
    s["comp_b"] = pick([c for c in lean["comp"] if c != s["comp"]] or [s["comp"]])         # the second phrase of the form
    s["bass"] = _choice(args, "bass", BASSES) or pick(lean["bass"])
    s["bass_b"] = pick([b for b in lean["bass"] if b != s["bass"]] or [s["bass"]])
    s["groove"] = "none" if args.get("drums") is False else (_choice(args, "groove", GROOVES) or pick(lean["groove"]))
    s["melody_style"] = "none" if args.get("melody") is False else pick(lean["melody"])
    s["lead"] = pick([v for v in LEADS if v != s["keys"]])
    s["texture"] = _choice(args, "texture", TEXTURES) or pick(lean["texture"])
    s["brightness"] = _choice(args, "brightness", list(_BRIGHTNESS)) or pick(lean["brightness"])
    s["bpm"] = _number(args, "bpm", int(rng.integers(lean["bpm"][0], lean["bpm"][1] + 1)), 50, 140, int)
    s["swing"] = _number(args, "swing", round(float(rng.uniform(*lean["swing"])), 2), 0, 0.33)
    s["vinyl"] = _number(args, "vinyl", round(float(rng.uniform(0.3, 0.7)), 2), 0, 1)
    if "minutes" not in args:                                    # a length in seconds is a common way to ask for one
        for alias in ("duration_seconds", "seconds", "length_seconds"):
            if isinstance(args.get(alias), (int, float)) and not isinstance(args.get(alias), bool):
                args = {**args, "minutes": args[alias] / 60}
                break
    s["minutes"] = _number(args, "minutes", 3.0, 0.25, MAX_MINUTES)
    s["bars_per_chord"] = _number(args, "bars_per_chord", int(pick((2, 2, 4, 1))), 1, 4, int)
    if s["bars_per_chord"] not in (1, 2, 4):
        raise ValueError("bars_per_chord must be 1, 2 or 4")
    chords = args.get("chords")
    if chords is None or chords == []:
        chords = random_progression(rng, minor=bool(rng.random() < lean["minor"]))
    if not isinstance(chords, list) or not 1 <= len(chords) <= 16:
        raise ValueError("chords must be a list of 1 to 16 chord names")
    s["chord_names"] = [str(c).strip() for c in chords]
    s["chords"] = [parse_chord(c) for c in chords]
    return s


# ---------- patterns ----------
# comp: (beat in the bar, length in beats, strum seconds), played with every note of the chord
_COMP_HITS = {
    "rolled": [(0, 2.2, 0.018), (2.5, 1.4, 0.018)],
    "pairs": [(0, 1.9, 0.02), (2, 1.9, 0.02)],
    "sustain": [(0, 3.9, 0.03)],
    "offbeat": [(1.5, 0.8, 0.01), (3.5, 0.8, 0.01)],
    "syncopated": [(0, 1.4, 0.015), (1.5, 1.0, 0.015), (2.5, 1.4, 0.015)],
}
# bass: (beat, length, which note): r root, 3 third, 5 fifth, 8 octave, a approach to the next chord's root
_BASS_HITS = {
    "rootfifth": [(0, 1.8, "r"), (2.5, 0.9, "5")],
    "pedal": [(0, 3.9, "r")],
    "walking": [(0, 0.95, "r"), (1, 0.95, "3"), (2, 0.95, "5"), (3, 0.95, "a")],
    "syncopated": [(0, 1.0, "r"), (1.5, 0.5, "r"), (2.5, 0.9, "5"), (3.5, 0.45, "a")],
    "octave": [(b / 2, 0.42, "r" if b % 2 == 0 else "8") for b in range(8)],
}
# drums on a 16-step bar; "kick2" and "ghost" are the optional extras
_GROOVES = {
    "boombap": {"kick": [0, 10], "kick2": [7], "snare": [4, 12], "hat": list(range(0, 16, 2)), "open": [14]},
    "halftime": {"kick": [0, 11], "snare": [8], "hat": list(range(0, 16, 2)), "open": [15]},
    "sparse": {"kick": [0, 8], "rim": [12], "shaker": [2, 6, 10, 14]},
    "shuffle": {"kick": [0, 6, 10], "snare": [4, 12], "ghost": [7, 15], "hat": list(range(0, 16, 2)), "shaker": [3, 11]},
}
_MINOR_KINDS = {"min", "m7", "m9", "m11", "dim"}
_PENTATONIC = {True: [0, 3, 5, 7, 10], False: [0, 2, 4, 7, 9]}


def _make_motif(rng, scale_len):
    """A short phrase: onsets on the half-beat grid, pitches wandering by step through the scale."""
    count = int(rng.integers(3, 6))
    onsets = sorted(rng.choice([0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5], count, replace=False).tolist())
    index = int(rng.integers(scale_len // 2 - 1, scale_len // 2 + 2))
    motif = []
    for i, beat in enumerate(onsets):
        length = (onsets[i + 1] - beat) if i + 1 < len(onsets) else 4 - beat
        motif.append((beat, min(1.6, max(0.4, length * 0.9)), index))
        index = int(np.clip(index + rng.choice([-2, -1, -1, 0, 1, 1, 2]), 0, scale_len - 1))
    return motif


def _bar_count(s):
    return int(math.ceil(s["minutes"] * 60 / (60.0 / s["bpm"] * 4)))


def _put(song, layer, sound, at, gain, pan=0.0):
    at = max(0.0, at)
    if _layer_hook:
        _layer_hook(layer, sound, at, gain)
    song.add(sound, at, gain=gain, pan=pan)


def _voice(synth, name, midi, dur, vel=None):
    fn = getattr(synth, name)
    sound = fn(midi, dur) if vel is None else fn(midi, dur, vel)
    return sound * _LOUDNESS[name]


def _arrange(song, s, synth):
    """Lay the whole track out. Chords and bass run throughout (the bass joins after a short intro), drums
    come in after the intro, the melody once the track is under way, and every fourth eight-bar phrase
    strips back to the chords so a long track breathes. Alternate phrases use the second comp and bass."""
    rng = np.random.default_rng(s["seed"] + 1)
    at = synth.clock(s["bpm"], s["swing"])
    bars = _bar_count(s)
    first_minor = s["chords"][0][1] in _MINOR_KINDS
    tonic = s["chords"][0][0]
    scale_offsets = _PENTATONIC[first_minor]
    scale = [synth.note(tonic + "4") + 12 + 12 * octave + off for octave in range(2) for off in scale_offsets]
    motif_a = _make_motif(rng, len(scale))
    motif_b = [(b, l, int(np.clip(i + int(rng.choice([-2, 2])), 0, len(scale) - 1))) if k >= len(motif_a) - 2 else (b, l, i) for k, (b, l, i) in enumerate(motif_a)]
    intro_bars = int(rng.choice([2, 4]))
    groove = _GROOVES.get(s["groove"])
    for bar in range(bars):
        root, kind = s["chords"][(bar // s["bars_per_chord"]) % len(s["chords"])]
        next_root, _ = s["chords"][((bar + 1) // s["bars_per_chord"]) % len(s["chords"])]
        notes = synth.chord(root + "3", kind)
        phrase, in_phrase = divmod(bar, 8)
        breakdown = phrase % 4 == 3
        comp = s["comp"] if phrase % 2 == 0 else s["comp_b"]
        bass_pattern = s["bass"] if (phrase // 2) % 2 == 0 else s["bass_b"]
        if breakdown:
            comp = "sustain" if comp != "arpeggio" else comp

        # chords
        keys = s["keys"]
        if comp == "arpeggio" and keys != "pad":
            order = sorted(notes) + sorted(notes)[-2:0:-1]
            for i in range(8):
                midi = order[i % len(order)] + 12
                _put(song, "chords", _voice(synth, keys, midi, 0.9 * at.beat), at(bar, i / 2) + rng.normal(0, 0.004), GAINS["chords"] * 1.2, pan=(i % 4 - 1.5) * 0.12)
        else:
            for beat, length, strum in _COMP_HITS["sustain" if comp == "arpeggio" else comp]:
                when = at(bar, beat)
                if keys == "pad":
                    _put(song, "chords", synth.pad([m + 12 for m in notes], length * at.beat + 0.3) * _LOUDNESS["pad"], when, GAINS["chords"] * 1.1)
                    continue
                for k, midi in enumerate(notes):
                    _put(song, "chords", _voice(synth, keys, midi + 12, length * at.beat), when + k * strum + rng.normal(0, 0.004), GAINS["chords"], pan=(k - len(notes) / 2) * 0.12)

        # bass
        if bar >= (intro_bars // 2) and bass_pattern != "none":
            base = synth.note(root + "3") - 12
            intervals = {"r": 0, "3": notes[1] - notes[0], "5": 7, "8": 12}
            approach = synth.note(next_root + "3") - 12 + int(rng.choice([-1, 1]))
            for beat, length, degree in _BASS_HITS[bass_pattern]:
                midi = approach if degree == "a" else base + intervals[degree]
                _put(song, "bass", synth.bass(midi, length * at.beat), at(bar, beat), GAINS["bass"] * (0.85 if degree != "r" else 1.0))

        # melody
        if s["melody_style"] != "none" and phrase >= 1 and not breakdown:
            lead = s["lead"]
            if s["melody_style"] == "sparse" and bar % 2 == 1:
                for beat in sorted(rng.choice([0, 0.5, 1, 1.5, 2, 3], 3, replace=False)):
                    _put(song, "melody", _voice(synth, lead, int(rng.choice(notes)) + 24, 0.9 * at.beat), at(bar, float(beat)), GAINS["melody"], pan=0.25)
            elif s["melody_style"] == "motif" and bar % 4 in (0, 2):
                for beat, length, index in (motif_a if bar % 4 == 0 else motif_b):
                    _put(song, "melody", _voice(synth, lead, scale[index], length * at.beat), at(bar, beat), GAINS["melody"], pan=0.25)

        # drums
        if groove and bar >= intro_bars and not breakdown:
            for step in groove.get("kick", []) + ([groove["kick2"][0]] if groove.get("kick2") and rng.random() < 0.3 else []):
                _put(song, "kick", synth.kick(), at(bar, step / 4) + rng.normal(0, 0.004), GAINS["kick"])
            for step in groove.get("snare", []):
                _put(song, "snare", synth.snare(), at(bar, step / 4) + rng.normal(0, 0.006), GAINS["snare"])
            for step in groove.get("ghost", []):
                _put(song, "snare", synth.snare(), at(bar, step / 4) + rng.normal(0, 0.006), GAINS["snare"] * 0.35)
            for step in groove.get("rim", []):
                _put(song, "rim", synth.rim(), at(bar, step / 4), GAINS["rim"])
            for step in groove.get("hat", []):
                loud = 1.0 if step % 4 == 0 else 0.7
                _put(song, "hat", synth.hat(open_=step in groove.get("open", [])), at(bar, step / 4) + rng.normal(0, 0.003), GAINS["hat"] * loud, pan=0.15 if step % 4 == 0 else -0.15)
            for step in groove.get("shaker", []):
                _put(song, "shaker", synth.shaker(), at(bar, step / 4), GAINS["shaker"], pan=-0.2)
            if in_phrase == 7 and "snare" in groove:              # a small fill into the next phrase
                for k, step in enumerate((12, 13, 14, 15)):
                    _put(song, "snare", synth.snare(), at(bar, step / 4), GAINS["snare"] * (0.3 + 0.12 * k))
    return at(bars)


def _stem(name):
    stem = re.sub(r"\.wav$", "", os.path.basename(str(name or "")).strip(), flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9 _-]", "", stem).strip().replace(" ", "_") or "background"


def generate_background_music(args, workspace):
    """With a workspace the track is saved there. Without one (a plain chat) it is made in a scratch folder and handed back as a
    download link, the same way a PDF is."""
    scratch = None if workspace else tempfile.mkdtemp(prefix="athena_music_")
    try:
        return _generate(args if isinstance(args, dict) else {}, workspace, scratch)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def _generate(args, workspace, scratch):
    try:
        s = _settings(args)
        rel = _stem(args.get("filename")) + ".wav"
        path = os.path.join(scratch, rel) if scratch else file_tools.resolve_workspace_path(workspace, rel)
    except ValueError as e:
        return {"error": str(e)}
    synth = _load_synth()
    started = time.time()
    with _render_lock:
        try:
            synth.seed(s["seed"])
            song = synth.Track(seconds=_bar_count(s) * 4 * 60.0 / s["bpm"], tail=2.0)
            length = _arrange(song, s, synth)
            amount, texture = s["vinyl"], s["texture"]
            if texture == "vinyl" and amount > 0:
                song.add(synth.vinyl(length + 2, level=0.06 * amount), 0)
            elif texture == "rain" and amount > 0:
                song.add(synth.rain(length + 2, level=0.35 * amount), 0)
            elif texture == "tape" and amount > 0:
                song.add(synth.vinyl(length + 2, level=0.02 * amount), 0)
            if os.path.exists(path):
                backup_tools.backup_file(path)
            song.render(path, lowpass_hz=_BRIGHTNESS[s["brightness"]], fade_in=1.5, fade_out=4.0, wobble=0.0005 * amount * 2 if texture == "tape" else 0.0)
        except OSError as e:
            return {"error": f"Could not save {rel}: {e}"}
        finally:
            song = None
    _, report = binary_files.describe(path)
    delivered = {"path": rel}
    if scratch:
        with open(path, "rb") as f:
            data = f.read()
        delivered = {"filename": rel, "url": generated_files.url_for(generated_files.store(data, rel)), "size": len(data)}
    return {
        **delivered, "minutes": round(length / 60, 2), "bpm": s["bpm"], "chords": s["chord_names"], "bars_per_chord": s["bars_per_chord"],
        "style": s["style"], "keys": s["keys"], "comp": s["comp"], "bass": s["bass"], "groove": s["groove"],
        "melody": s["melody_style"] != "none", "texture": s["texture"], "brightness": s["brightness"], "swing": s["swing"], "seed": s["seed"],
        "seconds_to_make": round(time.time() - started, 1), "report": report,
    }
