#!/usr/bin/env python3
"""Synthesise the narration, one wav per scene, then one for the whole video.

**macOS only, and deliberately so.** ``say`` is the synthesiser and it ships
with the system; the ``[[slnc N]]`` pauses in the narration are its markup too.
:mod:`build_shots` used to read as though it were macOS-only as well, which was
an accident and is fixed; this one is a real constraint, so it is stated here
rather than discovered as a FileNotFoundError.

The text and the per-scene durations are :mod:`storyboard`'s. They were a
second copy here, next to a third copy of the shot timings in
:mod:`assemble_video`, all three describing one contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import storyboard
from storyboard import SCENES

#: ffmpeg's ``atempo`` takes 0.5-2.0 per instance. A scene whose synthesised
#: narration overruns its slot by more than double is a storyboard problem
#: rather than something to fix with a filter, and passing the filter a value
#: it rejects fails with ffmpeg's own message about a slot nobody has mentioned.
MAX_TEMPO = 2.0


def get_audio_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def main() -> int:
    problems = storyboard.check()
    if problems:
        for line in problems:
            print(f"ERROR {line}", file=sys.stderr)
        return 1
    for tool in ("say", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"ERROR {tool!r} is not on PATH. This script is macOS-only: the "
                  f"narration is synthesised with the system `say` voice.",
                  file=sys.stderr)
            return 1

    os.makedirs("scratch/audio", exist_ok=True)
    os.makedirs("video_assets", exist_ok=True)

    total_target = sum(s["duration"] for s in SCENES)
    print(f"Total target video duration: {total_target}s ({total_target/60:.2f} mins)")

    scene_wavs = []

    for i, seg in enumerate(SCENES, 1):
        raw_aiff = f"scratch/audio/{seg['id']}_raw.aiff"
        final_wav = f"scratch/audio/{seg['id']}_timed.wav"

        # Synthesize with say using Daniel
        # -r rate: standard is ~175. We can test around 165 for very clear narration
        cmd = ["say", "-v", "Daniel", "-r", "165", "-o", raw_aiff, seg["narration"]]
        subprocess.run(cmd, check=True)

        raw_dur = get_audio_duration(raw_aiff)
        target_dur = seg["duration"]
        print(f"Scene {i} ({seg['name']}): raw duration {raw_dur:.2f}s -> target {target_dur:.2f}s")

        # Pad with silence or slight tempo adjust to exactly match target duration
        if raw_dur < target_dur:
            pad_needed = target_dur - raw_dur
            # apad to pad silence at the end
            cmd = [
                "ffmpeg", "-y", "-i", raw_aiff,
                "-af", f"apad=pad_dur={pad_needed}",
                "-ar", "44100", "-ac", "2",
                "-t", str(target_dur),
                final_wav
            ]
        else:
            # Need slight speedup (atempo)
            tempo = raw_dur / (target_dur - 0.5)
            if tempo > MAX_TEMPO:
                # Refused rather than clamped. Clamping would produce a scene
                # that overruns its slot and pushes every later scene out of
                # step with its pictures - silently, which is the one failure
                # this pipeline is now arranged to make noisy. Shorten the
                # narration or lengthen the scene in storyboard.py.
                print(f"ERROR scene {seg['id']} synthesises to {raw_dur:.2f}s against a "
                      f"{target_dur:.2f}s slot, which needs a tempo of {tempo:.2f} - past "
                      f"the {MAX_TEMPO} ffmpeg's atempo accepts, and past what stays "
                      f"listenable. Shorten the narration or lengthen the scene.",
                      file=sys.stderr)
                return 1
            cmd = [
                "ffmpeg", "-y", "-i", raw_aiff,
                "-af", f"atempo={tempo},apad=whole_dur={target_dur}",
                "-ar", "44100", "-ac", "2",
                "-t", str(target_dur),
                final_wav
            ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        final_dur = get_audio_duration(final_wav)
        print(f"  -> Generated {final_wav}: {final_dur:.2f}s")
        scene_wavs.append(final_wav)

    # Concatenate all into full_audio.wav
    concat_list_file = "scratch/audio/concat_list.txt"
    with open(concat_list_file, "w", encoding="utf-8") as f:
        for w in scene_wavs:
            f.write(f"file '{os.path.abspath(w)}'\n")

    full_wav = "video_assets/full_narration.wav"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list_file, "-c", "copy", full_wav
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    total_dur = get_audio_duration(full_wav)
    print(f"\nSuccessfully generated full narration: {full_wav} (Duration: {total_dur:.2f}s / {total_dur/60:.2f} min)")

if __name__ == "__main__":
    main()
