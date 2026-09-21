#!/usr/bin/env python3
import subprocess
import os
import sys

# Segments with target durations in seconds
SEGMENTS = [
    {
        "id": "scene1",
        "name": "The Bet",
        "duration": 25.0,
        "text": "We're thinking about changing our expense rules. Before we announce anything: how many old decisions do you think would get a different answer? Ten? Fifty? Half of them? [[slnc 1200]] In this demo, 147 out of 600. Almost one in four. That small rule change just became a much more interesting conversation."
    },
    {
        "id": "scene2",
        "name": "The Plot Twist",
        "duration": 30.0,
        "text": "Which sentence did it? This receipt clause accounts for 48 changes. But there's a twist: 38 of the 147 differences also disagree with the old rulebook. [[slnc 1000]] The proposal didn't create those differences. In the flips table, we can inspect each case's historical facts, candidate clauses, and human rulings."
    },
    {
        "id": "scene3",
        "name": "No Spoilers from the Future",
        "duration": 25.0,
        "text": "Imagine someone was promoted last year. Should today's seniority change what they were entitled to two years ago? [[slnc 1000]] This replay uses what was known on the day. Using today's facts gets 39 of these 600 cases wrong. [[slnc 1500]] Point in time replay protects you from tomorrow's bias."
    },
    {
        "id": "scene4",
        "name": "Open the Machine",
        "duration": 30.0,
        "text": "Bring back the old facts. Try both rulebooks. Ask a person about selected changes. Save their answer so the next proposal has to face it too. [[slnc 1000]] Airflow coordinates those steps. The machine stores the evidence in one shared memory, and this screen reads it back so we can discuss it together."
    },
    {
        "id": "scene5",
        "name": "The Person Gets a Say",
        "duration": 30.0,
        "text": "We don't ask someone to read 600 cases. The demo selects eight. Each answer becomes an example future rules are checked against. If a proposal reverses one, the check fails and somebody has to resolve it. [[slnc 1200]] And we still ask whether the old policy already made the same reversal. A red result needs an explanation, not a convenient scapegoat."
    },
    {
        "id": "scene6",
        "name": "Let the Room Choose",
        "duration": 25.0,
        "text": "What would you choose: 50, 100, or 150 pounds? We can compare the consequences before we pick. [[slnc 1200]] These results use the offline rules; they show trade-offs, not a recommendation."
    },
    {
        "id": "scene7",
        "name": "Pay Off the Opening Question",
        "duration": 15.0,
        "text": "Would you ship this rule? Now we can discuss who it affects, what it costs, and which human decisions it must respect. [[slnc 600]] Try tomorrow's rules on yesterday's decisions, before tomorrow becomes a surprise."
    }
]

def get_audio_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return float(res.stdout.strip())

def main():
    os.makedirs("scratch/audio", exist_ok=True)
    os.makedirs("video_assets", exist_ok=True)
    
    total_target = sum(s["duration"] for s in SEGMENTS)
    print(f"Total target video duration: {total_target}s ({total_target/60:.2f} mins)")
    
    scene_wavs = []
    
    for i, seg in enumerate(SEGMENTS, 1):
        raw_aiff = f"scratch/audio/{seg['id']}_raw.aiff"
        final_wav = f"scratch/audio/{seg['id']}_timed.wav"
        
        # Synthesize with say using Daniel
        # -r rate: standard is ~175. We can test around 165 for very clear narration
        cmd = ["say", "-v", "Daniel", "-r", "165", "-o", raw_aiff, seg["text"]]
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
    with open(concat_list_file, "w") as f:
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
