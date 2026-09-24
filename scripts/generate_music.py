#!/usr/bin/env python3
"""Synthesise the demo video's background music: a quiet, slow ambient bed.

Generated rather than downloaded, so there is no licence to track and nothing
to fetch - the track is this file, and rebuilding it is deterministic. It is
deliberately plain: soft pads on a four-chord loop, a low root under them and a
sparse bell on the chord tones, all well below the voice. It is ducked further
under the narration when :mod:`assemble_video` mixes the two.

    python scripts/generate_music.py        # writes video_assets/music.wav
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from media import RATE
from storyboard import TOTAL_SECONDS

OUT = Path("video_assets/music.wav")

#: Seconds each chord is held. Slow on purpose: a bed that changes under every
#: sentence competes with it.
CHORD_SECONDS = 5.0

#: Fmaj7, Am7, Cmaj7, G6 - calm, unresolved, and in no hurry to arrive.
#: MIDI note numbers, pad voicing around middle C.
CHORDS = [
    [53, 57, 60, 64],
    [57, 60, 64, 67],
    [48, 55, 59, 64],
    [55, 59, 62, 64],
]


def hz(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def tone(freq: float, t: np.ndarray, detune: float = 0.0) -> np.ndarray:
    """A soft tone: the fundamental and two quiet overtones, no edge to it."""
    f = freq * (1 + detune)
    return (np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
            + 0.06 * np.sin(6 * np.pi * f * t))


def envelope(n: int, attack: float, release: float) -> np.ndarray:
    """Rise over ``attack`` seconds, fall over ``release``, flat between."""
    env = np.ones(n, dtype=np.float64)
    a, r = int(attack * RATE), int(release * RATE)
    env[:a] = np.linspace(0, 1, a) ** 2
    env[n - r:] *= np.linspace(1, 0, r) ** 2
    return env


def render(seconds: float) -> np.ndarray:
    n = int(seconds * RATE)
    left, right = np.zeros(n), np.zeros(n)
    rng = np.random.default_rng(7)
    overlap = 1.5
    step = int(CHORD_SECONDS * RATE)
    for index, start in enumerate(range(0, n, step)):
        chord = CHORDS[index % len(CHORDS)]
        length = min(step + int(overlap * RATE), n - start)
        t = np.arange(length) / RATE
        env = envelope(length, attack=1.6, release=min(2.2, length / RATE / 2))
        for note in chord:
            # Two slightly detuned voices per note, one per side: the width
            # comes from the beating between them, not from panning.
            left[start:start + length] += 0.10 * env * tone(hz(note), t, -0.0015)
            right[start:start + length] += 0.10 * env * tone(hz(note), t, +0.0015)
        bass = 0.16 * env * np.sin(2 * np.pi * hz(chord[0] - 12) * t)
        left[start:start + length] += bass
        right[start:start + length] += bass
        # A few bell notes per chord, an octave up, each decaying quickly.
        for k in range(4):
            at = int((0.6 + k * 1.1 + rng.uniform(-0.1, 0.1)) * RATE)
            if at >= length:
                break
            bell_len = min(int(1.4 * RATE), length - at)
            bt = np.arange(bell_len) / RATE
            note = chord[rng.integers(len(chord))] + 12
            bell = 0.035 * np.exp(-bt * 3.2) * np.sin(2 * np.pi * hz(note) * bt)
            pan = 0.5 + 0.35 * (k % 2 * 2 - 1)
            left[start + at:start + at + bell_len] += bell * (1 - pan)
            right[start + at:start + at + bell_len] += bell * pan
    fade = envelope(n, attack=2.5, release=5.0)
    stereo = np.stack([left * fade, right * fade], axis=1)
    return stereo / np.max(np.abs(stereo)) * 0.5


def write(samples: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


if __name__ == "__main__":
    write(render(TOTAL_SECONDS), OUT)
    print(f"Wrote {OUT} ({TOTAL_SECONDS:.0f}s)")
