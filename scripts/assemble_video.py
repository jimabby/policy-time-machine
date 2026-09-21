#!/usr/bin/env python3
import subprocess
from pathlib import Path

SHOTS = [
    # Scene 1: The Bet (25.0s)
    {"id": "s1_shot1", "dur": 8.0},
    {"id": "s1_shot2", "dur": 5.0},
    {"id": "s1_shot3", "dur": 5.0},
    {"id": "s1_shot4", "dur": 7.0},

    # Scene 2: The Plot Twist (30.0s)
    {"id": "s2_shot1", "dur": 7.0},
    {"id": "s2_shot2", "dur": 8.0},
    {"id": "s2_shot3", "dur": 7.0},
    {"id": "s2_shot4", "dur": 8.0},

    # Scene 3: No Spoilers from the Future (25.0s)
    {"id": "s3_shot1", "dur": 8.0},
    {"id": "s3_shot2", "dur": 9.0},
    {"id": "s3_shot3", "dur": 8.0},

    # Scene 4: Open the Machine (30.0s)
    {"id": "s4_shot1", "dur": 8.0},
    {"id": "s4_shot2", "dur": 12.0},
    {"id": "s4_shot3", "dur": 10.0},

    # Scene 5: The Person Gets a Say (30.0s)
    {"id": "s5_shot1", "dur": 10.0},
    {"id": "s5_shot2", "dur": 10.0},
    {"id": "s5_shot3", "dur": 10.0},

    # Scene 6: Let the Room Choose (25.0s)
    {"id": "s6_shot1", "dur": 7.0},
    {"id": "s6_shot2", "dur": 10.0},
    {"id": "s6_shot3", "dur": 8.0},

    # Scene 7: Pay Off the Opening Question (15.0s)
    {"id": "s7_shot1", "dur": 7.0},
    {"id": "s7_shot2", "dur": 8.0},
]

SUBTITLES = """1
00:00:00,500 --> 00:00:04,500
We're thinking about changing our expense rules.

2
00:00:04,500 --> 00:00:10,500
Before we announce anything: how many old decisions do you think would get a different answer?

3
00:00:10,500 --> 00:00:14,000
Ten? Fifty? Half of them?

4
00:00:15,000 --> 00:00:19,500
In this demo, 147 out of 600. Almost one in four.

5
00:00:19,500 --> 00:00:24,500
That small rule change just became a much more interesting conversation.

6
00:00:25,500 --> 00:00:28,500
Which sentence did it?

7
00:00:28,500 --> 00:00:32,500
This receipt clause accounts for 48 changes.

8
00:00:32,500 --> 00:00:38,500
But there's a twist: 38 of the 147 differences also disagree with the old rulebook.

9
00:00:39,500 --> 00:00:43,500
The proposal didn't create those differences.

10
00:00:43,500 --> 00:00:53,500
In the flips table, we can inspect each case's historical facts, candidate clauses, and human rulings.

11
00:00:55,500 --> 00:00:59,500
Imagine someone was promoted last year.

12
00:00:59,500 --> 00:01:05,500
Should today's seniority change what they were entitled to two years ago?

13
00:01:06,500 --> 00:01:10,000
This replay uses what was known on the day.

14
00:01:10,000 --> 00:01:14,500
Using today's facts gets 39 of these 600 cases wrong.

15
00:01:15,500 --> 00:01:21,000
Point in time replay protects you from tomorrow's bias.

16
00:01:21,500 --> 00:01:25,000
Bring back the old facts. Try both rulebooks.

17
00:01:25,000 --> 00:01:28,500
Ask a person about selected changes.

18
00:01:28,500 --> 00:01:34,000
Save their answer so the next proposal has to face it too.

19
00:01:34,500 --> 00:01:38,000
Airflow coordinates those steps.

20
00:01:38,000 --> 00:01:42,500
The machine stores the evidence in one shared memory,

21
00:01:42,500 --> 00:01:48,000
and this screen reads it back so we can discuss it together.

22
00:01:50,500 --> 00:01:54,500
We don't ask someone to read 600 cases.

23
00:01:54,500 --> 00:01:58,000
The demo selects eight.

24
00:01:58,000 --> 00:02:04,500
Each answer becomes an example future rules are checked against.

25
00:02:04,500 --> 00:02:09,500
If a proposal reverses one, the check fails and somebody has to resolve it.

26
00:02:10,500 --> 00:02:15,000
And we still ask whether the old policy already made the same reversal.

27
00:02:15,000 --> 00:02:20,000
A red result needs an explanation, not a convenient scapegoat.

28
00:02:21,000 --> 00:02:26,000
What would you choose: 50, 100, or 150 pounds?

29
00:02:26,000 --> 00:02:30,500
We can compare the consequences before we pick.

30
00:02:31,500 --> 00:02:36,000
These results use the offline rules;

31
00:02:36,000 --> 00:02:41,500
they show trade-offs, not a recommendation.

32
00:02:45,500 --> 00:02:47,500
Would you ship this rule?

33
00:02:47,500 --> 00:02:53,000
Now we can discuss who it affects, what it costs, and which human decisions it must respect.

34
00:02:53,500 --> 00:03:00,000
Try tomorrow's rules on yesterday's decisions, before tomorrow becomes a surprise.
"""

def main():
    clips_dir = Path("video_assets/clips")
    clips_dir.mkdir(parents=True, exist_ok=True)

    shots_dir = Path("video_assets/shots")
    audio_file = Path("video_assets/full_narration.wav")
    output_mp4 = Path("policy_time_machine_demo.mp4")
    srt_file = Path("video_assets/subtitles.srt")

    srt_file.write_text(SUBTITLES.strip() + "\n")
    print("Wrote subtitles to video_assets/subtitles.srt")

    clip_files = []

    print("Step 1: Encoding individual shot video clips...")
    for i, shot in enumerate(SHOTS, 1):
        img_path = shots_dir / f"{shot['id']}.png"
        clip_path = clips_dir / f"{shot['id']}.mp4"
        clip_files.append(clip_path)

        dur = shot["dur"]
        vf_filter = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x080c16"

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-t", str(dur),
            "-i", str(img_path.resolve()),
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-crf", "18",
            str(clip_path.resolve())
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print(f"  [{i}/{len(SHOTS)}] Encoded {clip_path.name} ({dur}s)")

    print("\nStep 2: Concatenating all shot clips...")
    concat_txt = clips_dir / "concat.txt"
    with open(concat_txt, "w") as f:
        for c in clip_files:
            f.write(f"file '{c.resolve()}'\n")

    raw_video = clips_dir / "full_video_track.mp4"
    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_txt.resolve()),
        "-c", "copy",
        str(raw_video.resolve())
    ]
    subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print(f"Generated raw video: {raw_video.name}")

    print("\nStep 3: Muxing with audio and subtitles...")
    cmd_final = [
        "ffmpeg", "-y",
        "-i", str(raw_video.resolve()),
        "-i", str(audio_file.resolve()),
        "-i", str(srt_file.resolve()),
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-c:s", "mov_text",
        "-t", "180.0",
        str(output_mp4.resolve())
    ]
    subprocess.run(cmd_final, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print(f"\nFinal Video Created: {output_mp4.resolve()}")

    res = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(output_mp4.resolve())
    ], stdout=subprocess.PIPE, text=True, check=True)
    dur = float(res.stdout.strip())
    size_mb = output_mp4.stat().st_size / (1024 * 1024)
    print(f"Output verified: Duration = {dur:.2f}s ({dur/60:.2f} min), Size = {size_mb:.2f} MB")

if __name__ == "__main__":
    main()
