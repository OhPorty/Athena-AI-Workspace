import json
import types

import pytest

import background_music as bm
import backup_tools
import main


def make(ws, **args):
    return bm.generate_background_music({"minutes": 0.3, "filename": "t", **args}, str(ws))


@pytest.mark.parametrize("name,expected", [
    ("Dm9", ("D", "m9")), ("G13", ("G", "13")), ("Cmaj9", ("C", "maj9")), ("Bbmaj7", ("Bb", "maj7")),
    ("F#m7", ("F#", "m7")), ("c", ("C", "maj")), ("Am", ("A", "min")), ("Esus4", ("E", "sus4")), ("Gdim", ("G", "dim")),
])
def test_chord_names_are_understood(name, expected):
    assert bm.parse_chord(name) == expected


@pytest.mark.parametrize("name", ["H7", "Dm99", "banana", "", "C#m7b5"])
def test_unknown_chords_are_refused_with_what_is_supported(name):
    with pytest.raises(ValueError, match="not a chord I know"):
        bm.parse_chord(name)


def test_a_track_is_made_with_the_requested_length_and_a_clean_report(tmp_path):
    out = make(tmp_path, minutes=0.4, bpm=80, chords=["Dm9", "G13"], seed=5)
    assert out["path"] == "t.wav" and (tmp_path / "t.wav").is_file() and out["chords"] == ["Dm9", "G13"] and out["bpm"] == 80
    assert 22 <= float(out["report"].split("WAV audio: ")[1].split(" s")[0]) <= 28      # 0.4 min = 24 s, plus a short tail
    assert "Pitched notes: yes" in out["report"] and "Problems: none found" in out["report"]


def test_the_same_settings_and_seed_give_the_same_file_and_a_new_seed_a_different_one(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    for d in (a, b, c):
        d.mkdir()
    make(a, seed=1); make(b, seed=1); make(c, seed=2)
    assert (a / "t.wav").read_bytes() == (b / "t.wav").read_bytes() != (c / "t.wav").read_bytes()


def test_without_a_workspace_the_track_comes_back_as_a_download_link(tmp_path, monkeypatch):
    import generated_files
    monkeypatch.setattr(generated_files, "ROOT", str(tmp_path / "gen"))
    out = bm.generate_background_music({"minutes": 0.25, "seed": 3, "filename": "chill"}, "")
    assert "error" not in out and out["filename"] == "chill.wav" and out["url"].startswith("/api/generated-files/") and out["size"] > 1000
    stored = generated_files.resolve(out["url"].rsplit("/", 1)[-1])
    assert stored and open(stored, "rb").read(4) == b"RIFF" and "path" not in out
    import tempfile, glob, os
    assert not glob.glob(os.path.join(tempfile.gettempdir(), "athena_music_*"))              # the scratch folder is cleaned up


def test_a_length_in_seconds_is_understood(tmp_path):
    out = bm.generate_background_music({"duration_seconds": 30, "seed": 4}, str(tmp_path))
    assert 28 <= float(out["report"].split("WAV audio: ")[1].split(" s")[0]) <= 36


def test_cannot_write_outside_the_workspace(tmp_path):
    out = bm.generate_background_music({"minutes": 0.25, "filename": "../../etc/evil.wav"}, str(tmp_path))
    assert out["path"] == "evil.wav" and (tmp_path / "evil.wav").is_file()      # the path part is dropped, never followed


def test_remaking_a_track_keeps_a_restorable_copy_of_the_old_one(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_tools, "_BACKUP_ROOT", str(tmp_path / "backups"))
    ws = tmp_path / "ws"
    ws.mkdir()
    make(ws, seed=1)
    make(ws, seed=2)
    assert len(backup_tools._read_backup_manifest()) == 1


def test_the_tool_is_offered_in_workspace_modes_and_counts_as_progress():
    names = lambda tools: {t["function"]["name"] for t in tools}
    Req = lambda: types.SimpleNamespace(search_url="", allowed_tools=None)
    assert "generate_background_music" in names(main._get_mode_tools_legacy("workspace", Req(), build_task=True))
    assert "generate_background_music" in names(main._get_mode_tools_legacy("workspace", Req()))
    assert "generate_background_music" not in names(main._get_mode_tools_legacy("casual", Req()))
    assert main._result_is_progress("generate_background_music", {"path": "t.wav"})


def test_dispatch_runs_it_in_the_turns_workspace(tmp_path):
    ctx = main.ToolContext(session_id="s1", workspace=str(tmp_path), search_url="", bot=None, session_key=None,
                           req=None, plan_state=None, passthrough_session_id=None, require_athena_bots_session=False)
    out = main.dispatch_tool("generate_background_music", {"minutes": 0.25, "filename": "d"}, ctx)
    assert out["path"] == "d.wav" and (tmp_path / "d.wav").is_file()


def _a_weight(x, sr=44100):
    import numpy as np
    spec, f2 = np.fft.rfft(x), np.maximum(np.fft.rfftfreq(len(x), 1 / sr), 1e-3) ** 2
    ra = (12194 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194 ** 2))
    return np.fft.irfft(spec * ra, len(x))


# ---------- variety: every call should be a different piece, not the same one in another key ----------

def _signature(r):
    return (r["style"], r["keys"], r["comp"], r["bass"], r["groove"], r["texture"], r["melody"])


def test_with_nothing_specified_each_seed_makes_a_genuinely_different_track(tmp_path):
    results = []
    for seed in range(12):
        (tmp_path / str(seed)).mkdir()
        results.append(make(tmp_path / str(seed), seed=seed, minutes=0.25))
    assert len({_signature(r) for r in results}) >= 10                    # not just a new key and tempo
    assert len({tuple(r["chords"]) for r in results}) >= 10
    assert len({r["keys"] for r in results}) >= 4 and len({r["groove"] for r in results}) >= 3 and len({r["bass"] for r in results}) >= 4
    assert len({r["style"] for r in results}) >= 4
    assert len({round(r["bpm"], -1) for r in results}) >= 3
    sounds = {(tmp_path / str(i) / "t.wav").read_bytes() for i in range(12)}
    assert len(sounds) == 12


def test_two_calls_with_identical_settings_but_no_seed_still_differ(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    import time
    first = make(a, minutes=0.25)
    time.sleep(0.01)
    second = make(b, minutes=0.25)
    assert first["seed"] != second["seed"] or _signature(first) != _signature(second) or first["chords"] != second["chords"]


def test_what_the_model_chooses_is_used_and_the_rest_is_still_chosen_for_it(tmp_path):
    out = make(tmp_path, seed=7, minutes=0.25, style="jazzy", keys="vibes", comp="arpeggio", bass="walking", groove="shuffle", texture="rain", brightness="dark", bpm=90)
    assert (out["style"], out["keys"], out["comp"], out["bass"], out["groove"], out["texture"], out["brightness"], out["bpm"]) == \
        ("jazzy", "vibes", "arpeggio", "walking", "shuffle", "rain", "dark", 90)
    assert out["chords"] and out["swing"] > 0            # unspecified things were still decided
    assert make(tmp_path, seed=7, minutes=0.25, drums=False)["groove"] == "none"
    assert make(tmp_path, seed=7, minutes=0.25, melody=False)["melody"] is False


@pytest.mark.parametrize("field,values", [("keys", bm.KEYS), ("comp", bm.COMPS), ("bass", bm.BASSES), ("groove", bm.GROOVES), ("texture", bm.TEXTURES), ("style", list(bm.STYLES))])
def test_a_bad_choice_is_refused_naming_the_options(tmp_path, field, values):
    out = make(tmp_path, **{field: "kazoo"})
    assert field in out["error"] and values[0] in out["error"]


def test_every_instrument_pattern_groove_and_texture_renders_a_clean_track(tmp_path):
    longest = max(len(bm.KEYS), len(bm.COMPS), len(bm.BASSES), len(bm.GROOVES), len(bm.TEXTURES), len(bm.STYLES))
    for i in range(longest):
        d = tmp_path / str(i)
        d.mkdir()
        pick = lambda pool: pool[i % len(pool)]
        out = make(d, seed=i, minutes=0.3, keys=pick(bm.KEYS), comp=pick(bm.COMPS), bass=pick(bm.BASSES), groove=pick(bm.GROOVES),
                   texture=pick(bm.TEXTURES), style=pick(list(bm.STYLES)))
        assert "error" not in out, out
        assert "Pitched notes: yes" in out["report"] and "Problems: none found" in out["report"], (i, out["report"])


def test_random_progressions_are_valid_and_cover_many_keys():
    import numpy as np
    seen_roots = set()
    for seed in range(60):
        rng = np.random.default_rng(seed)
        chords = bm.random_progression(rng, minor=seed % 3 == 0)
        assert 3 <= len(chords) <= 4 and all(bm.parse_chord(c) for c in chords)
        seen_roots.add(bm.parse_chord(chords[0])[0])
    assert len(seen_roots) >= 8


def test_notes_with_timing_jitter_before_time_zero_are_kept_not_dropped():
    import numpy as np
    track = bm._load_synth().Track(2)
    track.add(np.ones(100) * 0.5, -0.01)
    assert float(np.abs(track.left[:100]).sum()) > 0


def test_the_schema_stays_small_and_describes_the_tool_plainly():
    schema = bm.BACKGROUND_MUSIC_TOOL_SCHEMAS
    assert len(json.dumps(schema)) // 4 < 520
    desc = schema[0]["function"]["description"].lower()
    assert "background track" in desc and "no vocals" in desc and schema[0]["function"]["parameters"].get("required") is None


# ---------- balance: bass and drums must be heard on small speakers, whatever the instrument ----------

def _a_weighted_level(layer, sr=44100):
    import numpy as np
    spec, f2 = np.fft.rfft(layer), np.maximum(np.fft.rfftfreq(len(layer), 1 / sr), 1e-3) ** 2
    ra = (12194 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194 ** 2))
    weighted = np.fft.irfft(spec * ra, len(layer))
    win = int(sr * 0.05)
    frames = np.sqrt((weighted[: len(weighted) // win * win].reshape(-1, win) ** 2).mean(axis=1))
    return 20 * np.log10(np.percentile(frames[frames > frames.max() * 0.05], 90) + 1e-9)


def test_bass_and_drums_stay_audible_next_to_the_chords_for_every_instrument(tmp_path):
    """Raw energy once made the bass look loud while a listener on a laptop could hardly hear it. Each layer is
    captured alone and compared, equal-loudness weighted, with the chords; the reference is the balance that
    was listened to and accepted (bass about 2 dB under the chords, kick about level, hats about 5 dB under)."""
    import numpy as np
    wanted = {"bass": -2.1, "kick": -0.5, "snare": -1.5, "hat": -5.2}
    off_by = {name: [] for name in wanted}
    try:
        for i, keys in enumerate(bm.KEYS):
            layers = {}

            def hook(layer, sound, at, gain, layers=layers):
                buf = layers.setdefault(layer, np.zeros(44100 * 40, dtype=np.float32))
                start = int(at * 44100)
                piece = (np.asarray(sound) * gain)[: len(buf) - start]
                buf[start:start + len(piece)] += piece

            bm._layer_hook = hook
            d = tmp_path / keys
            d.mkdir()
            make(d, seed=i + 1, minutes=0.6, keys=keys, comp="rolled", bass="rootfifth", groove="boombap", texture="none", style="chill")
            chords = _a_weighted_level(layers["chords"])
            for name in wanted:
                off_by[name].append(_a_weighted_level(layers[name]) - chords - wanted[name])
    finally:
        bm._layer_hook = None
    for name, deviations in off_by.items():
        assert max(abs(x) for x in deviations) < 5, (name, [round(x, 1) for x in deviations])


def test_a_bad_request_without_a_workspace_leaves_no_scratch_folder_behind(tmp_path, monkeypatch):
    import generated_files, glob, tempfile, os
    monkeypatch.setattr(generated_files, "ROOT", str(tmp_path / "gen"))
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "athena_music_*")))
    assert "error" in bm.generate_background_music({"bars_per_chord": 3}, "")
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "athena_music_*"))) == before
