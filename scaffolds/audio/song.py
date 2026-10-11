"""The music. This example is a slow lofi loop; replace the tempo, chords, patterns and instruments
with whatever the piece needs (the same toolkit does chiptune, ambient, game sound effects, jingles).
Run:  python3 song.py   -> writes song.wav"""
import numpy as np

from synth import *

BPM, BARS, SWING = 76, 16, 0.15
PROGRESSION = [("D3", "m9"), ("G2", "13"), ("C3", "maj9"), ("A2", "m9")]   # one chord per two bars
rng = np.random.default_rng(7)

at = clock(BPM, SWING)
song = Track(seconds=at(BARS))

for bar in range(BARS):
    root, kind = PROGRESSION[(bar // 2) % len(PROGRESSION)]
    notes = chord(root, kind)

    # chords, two hits per bar, strummed a little late and uneven
    for beats, length in ((0, 2.2), (2.5, 1.4)):
        for k, m in enumerate(notes):
            song.add(epiano(m + 12, length * at.beat), at(bar, beats) + k * 0.018 + rng.normal(0, 0.004), gain=0.3, pan=(k - 2) * 0.12)

    # bass: the root on the first beat, the fifth on the "and" of three
    song.add(bass(note(root) - 12, 1.8 * at.beat), at(bar, 0), gain=0.7)
    song.add(bass(note(root) - 5, 0.9 * at.beat), at(bar, 2.5), gain=0.6)

    # a sparse melody every other bar, from the chord's own notes
    if bar % 2 == 1:
        for beats in sorted(rng.choice([0, 0.5, 1, 1.5, 2, 3], 3, replace=False)):
            song.add(epiano(int(rng.choice(notes)) + 24, 0.9 * at.beat, 0.7), at(bar, float(beats)), gain=0.3, pan=0.25)

    # drums: kick on 1 and 3, snare on 2 and 4, swung hats
    for beats in (0, 2):
        song.add(kick(), at(bar, beats), gain=0.8)
    for beats in (1, 3):
        song.add(snare(), at(bar, beats) + rng.normal(0, 0.006), gain=0.5)
    for beats in (0, 1, 2, 3):
        song.add(hat(), at(bar, beats), gain=0.5, pan=0.15)
        song.add(hat(open_=(beats == 3)), at(bar, beats + 0.5), gain=0.35, pan=-0.15)

song.add(vinyl(at(BARS) + 2), 0, gain=1.0)
song.render("song.wav", lowpass_hz=3800)
