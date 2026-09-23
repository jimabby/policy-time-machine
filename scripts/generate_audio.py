#!/usr/bin/env python3
"""Synthesise the narration: one line per shot, laid on the video's clock.

The first cut used macOS ``say`` and then ran each scene's audio through
ffmpeg's ``atempo`` until it filled the scene - a robotic voice, time-stretched,
under hand-typed subtitle timings that drifted from what was actually said. It
was hard to hear and harder to follow.

Now the voice is a neural one (Microsoft's, through ``edge-tts``) at its own
natural pace, and nothing stretches it. Each shot's line is synthesised on its
own and placed :data:`storyboard.LEAD_IN` seconds after that shot starts, so the
picture always changes just before the sentence about it. A line that does not
fit its shot is refused, with the overrun named, rather than squeezed: shorten
the line or lengthen the shot in ``storyboard.py``.

The synthesiser also reports when each word is spoken. Those timings are
written beside the audio (``video_assets/words.json``) and are what the
subtitles are cut from, so a subtitle cannot run ahead of or behind the voice.

Needs network access (the voice is a web service) and ``pip install edge-tts
imageio-ffmpeg``. Synthesised lines are cached under ``scratch/audio`` by their
text, so rebuilding after changing one line re-synthesises only that line.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import storyboard
from media import RATE, decode, ffmpeg
from storyboard import LEAD_IN, PAUSE, SHOTS, TAIL, TOTAL_SECONDS, VOICE

CACHE = Path("scratch/audio")
OUT_WAV = Path("video_assets/full_narration.wav")
OUT_WORDS = Path("video_assets/words.json")

#: Integrated loudness of the finished narration, in LUFS. -16 is the level
#: web video players and podcasts are mastered to: loud enough to hear over a
#: laptop fan without anyone reaching for the volume.
LOUDNESS = -16


async def _synthesise(text: str) -> tuple[bytes, list[dict]]:
    import edge_tts

    audio = bytearray()
    words: list[dict] = []
    stream = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    async for chunk in stream.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / 1e7
            words.append({"start": start, "end": start + chunk["duration"] / 1e7,
                          "text": chunk["text"]})
    return bytes(audio), words


def synthesise(text: str) -> tuple[np.ndarray, list[dict]]:
    """One phrase as samples, and when each of its words is spoken. Cached."""
    key = hashlib.sha256(f"{VOICE}\n{text}".encode()).hexdigest()[:20]
    mp3, meta = CACHE / f"{key}.mp3", CACHE / f"{key}.json"
    if not (mp3.exists() and meta.exists()):
        audio, words = asyncio.run(_synthesise(text))
        if not audio:
            raise SystemExit(f"the voice service returned no audio for {text!r}")
        mp3.write_bytes(audio)
        meta.write_text(json.dumps(words), encoding="utf-8")
    return decode(mp3.read_bytes()), json.loads(meta.read_text(encoding="utf-8"))


def attach_punctuation(text: str, words: list[dict]) -> list[dict]:
    """The synthesiser's words, as they are written in the line.

    It reports "147" for "147." and "Point-in-time" for "Point-in-time", which
    is right for timing and wrong for a subtitle. Each reported word is found
    in the line from where the last one ended, and widened to the whitespace
    around it so the punctuation comes along.
    """
    out, cursor = [], 0
    for word in words:
        at = text.find(word["text"], cursor)
        if at < 0:  # The service normalised something; keep its spelling.
            out.append(word)
            continue
        end = at + len(word["text"])
        while end < len(text) and not text[end].isspace():
            end += 1
        start = at
        while start > cursor and not text[start - 1].isspace():
            start -= 1
        out.append({**word, "text": text[start:end]})
        cursor = end
    return out


def shot_line(shot: dict) -> tuple[np.ndarray, list[dict]]:
    """A shot's whole line, its pauses included, starting at time zero."""
    pieces: list[np.ndarray] = []
    words: list[dict] = []
    cursor = 0.0
    parts = PAUSE.split(shot["say"])
    # split() alternates text, pause, text, pause, ...
    for i, part in enumerate(parts):
        if i % 2:
            silence = np.zeros(int(float(part) * RATE), dtype=np.float32)
            pieces.append(silence)
            cursor += len(silence) / RATE
            continue
        text = " ".join(part.split())
        if not text:
            continue
        samples, timed = synthesise(text)
        for word in attach_punctuation(text, timed):
            words.append({**word, "start": word["start"] + cursor,
                          "end": word["end"] + cursor})
        pieces.append(samples)
        cursor += len(samples) / RATE
    line = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    # Trim the synthesiser's trailing silence, so the fit check below is
    # measured against the last word rather than the padding after it.
    loud = np.nonzero(np.abs(line) > 1e-3)[0]
    if len(loud):
        line = line[: loud[-1] + int(0.05 * RATE)]
    return line, words


def main() -> int:
    problems = storyboard.check()
    if problems:
        for line in problems:
            print(f"ERROR {line}", file=sys.stderr)
        return 1
    CACHE.mkdir(parents=True, exist_ok=True)
    OUT_WAV.parent.mkdir(parents=True, exist_ok=True)

    track = np.zeros(int(TOTAL_SECONDS * RATE), dtype=np.float32)
    all_words: list[dict] = []
    starts = storyboard.shot_starts()
    overruns: list[str] = []
    for shot in SHOTS:
        line, words = shot_line(shot)
        length = len(line) / RATE
        room = shot["dur"] - LEAD_IN - TAIL
        print(f"{shot['id']:<10} {length:5.2f}s spoken in a {room:5.2f}s window")
        if length > room:
            overruns.append(f"{shot['id']} says {length:.2f}s of narration in a "
                            f"{shot['dur']}s shot with room for {room:.2f}s")
            continue
        at = starts[shot["id"]] + LEAD_IN
        begin = int(at * RATE)
        track[begin:begin + len(line)] += line
        all_words += [{**w, "start": round(w["start"] + at, 3),
                       "end": round(w["end"] + at, 3), "shot": shot["id"]}
                      for w in words]
    if overruns:
        # Refused rather than squeezed: speeding a line up to fit is what made
        # the first cut hard to follow.
        for line in overruns:
            print(f"ERROR {line}. Shorten the line or lengthen the shot.",
                  file=sys.stderr)
        return 1

    raw = CACHE / "narration_raw.wav"
    with wave.open(str(raw), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((np.clip(track, -1, 1) * 32767).astype("<i2").tobytes())
    # A gentle high-pass to take out rumble, then loudness normalisation, so
    # the voice is at a level people can hear without turning it up.
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-i", str(raw),
         "-af", f"highpass=f=70,loudnorm=I={LOUDNESS}:TP=-1.5:LRA=11",
         "-ar", str(RATE), "-ac", "2", str(OUT_WAV)], check=True)
    OUT_WORDS.write_text(json.dumps(all_words, indent=1), encoding="utf-8")
    print(f"\nWrote {OUT_WAV} ({TOTAL_SECONDS:.1f}s) and {len(all_words)} word "
          f"timings to {OUT_WORDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
