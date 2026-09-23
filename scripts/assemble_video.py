#!/usr/bin/env python3
"""Draw the demo video frame by frame, then mux it with the narration.

The first cut was twenty-two screenshots, each held still for seven seconds,
with the subtitles as a soft track most players hide. It was accurate and hard
to watch: small text on a large dark frame, nothing moving, and the words on
screen only if the viewer went looking for them.

This cut draws every frame itself (Pillow, piped into ffmpeg):

* **Stills move.** Each dashboard capture is 2x resolution and is slowly
  zoomed from the storyboard's ``zoom[0]`` box to ``zoom[1]``, onto the part of
  the page the sentence is about, so the text on it is readable.
* **Each scene has one card.** The idea the scene exists for - 147 of 600, the
  38 that were already there, 39 wrong, 600 down to 8 - is drawn as a large
  animation, keyed to the moment the voice says the number.
* **Subtitles are burned in** and cut from the synthesiser's own word timings
  (``video_assets/words.json``, from :mod:`generate_audio`), so they are
  always visible and cannot drift from the voice. They are also written as an
  ``.srt`` and muxed as a soft track, for players and platforms that want one.
* A chapter label and a progress bar say where in the three minutes you are.

The shots, their order, their lengths and their words are all
:mod:`storyboard`'s. Frames are rendered in parallel, one contiguous chunk per
worker, and the chunks are concatenated without re-encoding.
"""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from functools import cache, lru_cache
from pathlib import Path

import storyboard
from media import ffmpeg
from PIL import Image, ImageDraw, ImageFont
from storyboard import FIXTURE_FIGURES, NAIVE_WRONG, REVIEWS, SCENES, SHOTS, TOTAL_SECONDS

W, H = 1920, 1080
FPS = 30
#: Seconds each shot dissolves in over the one before it.
CROSSFADE = 0.35

SHOTS_DIR = Path("video_assets/shots")
WORDS = Path("video_assets/words.json")
AUDIO = Path("video_assets/full_narration.wav")
SRT = Path("video_assets/subtitles.srt")
CHUNKS = Path("scratch/video")
OUTPUT = Path("policy_time_machine_demo.mp4")

# The dashboard's own palette, so the cards and the captures look like one thing.
BG = (7, 11, 20)
PANEL = (15, 23, 42)
EDGE = (40, 52, 78)
TEXT = (238, 243, 255)
MUTED = (148, 163, 184)
DIM = (51, 65, 85)
BLUE = (119, 167, 255)
AMBER = (249, 199, 93)
GREY = (100, 116, 139)
GREEN = (61, 220, 151)
RED = (248, 113, 113)


# -- fonts -------------------------------------------------------------------

FONT_CANDIDATES = {
    "regular": ["segoeui.ttf", "Inter-Regular.ttf", "HelveticaNeue.ttc",
                "DejaVuSans.ttf"],
    "semibold": ["seguisb.ttf", "Inter-SemiBold.ttf", "HelveticaNeue.ttc",
                 "DejaVuSans-Bold.ttf"],
    "bold": ["segoeuib.ttf", "Inter-Bold.ttf", "HelveticaNeue.ttc",
             "DejaVuSans-Bold.ttf"],
    "black": ["seguibl.ttf", "Inter-Black.ttf", "segoeuib.ttf", "HelveticaNeue.ttc",
              "DejaVuSans-Bold.ttf"],
    "mono": ["CascadiaMono.ttf", "consola.ttf", "Menlo.ttc", "DejaVuSansMono.ttf"],
}
FONT_DIRS = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
             Path("/System/Library/Fonts"), Path("/Library/Fonts"),
             Path("/usr/share/fonts/truetype/dejavu"), Path.home() / ".fonts"]


@cache
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONT_CANDIDATES[weight]:
        for folder in FONT_DIRS:
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size)


# -- easing and small drawing helpers ------------------------------------------

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ease(x: float) -> float:
    """Ease in-out: slow start, slow finish. For camera moves."""
    x = clamp(x)
    return 0.5 - 0.5 * math.cos(math.pi * x)


def ease_out(x: float) -> float:
    """Fast start, gentle landing. For things arriving on screen."""
    x = clamp(x)
    return 1 - (1 - x) ** 3


def appear(t: float, at: float, length: float = 0.5) -> float:
    """0 before ``at``, easing to 1 over ``length`` seconds."""
    return ease_out((t - at) / length)


def mix(a: tuple, b: tuple, k: float) -> tuple:
    k = clamp(k)
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))


def rgba(color: tuple, alpha: float) -> tuple:
    return (*color[:3], int(255 * clamp(alpha)))


def text(draw: ImageDraw.ImageDraw, xy, words: str, fnt, color, alpha=1.0,
         anchor="mm", rise=0.0, spacing=0):
    """Text that fades in while rising ``rise`` pixels into place."""
    if alpha <= 0:
        return
    x, y = xy
    draw.text((x, y + rise * (1 - alpha)), words, font=fnt,
              fill=rgba(color, alpha), anchor=anchor, spacing=spacing)


def letterspaced(draw, xy, words: str, fnt, color, alpha=1.0, gap=4):
    """Small caps-style eyebrow text, centred on ``xy``."""
    widths = [draw.textlength(ch, font=fnt) for ch in words]
    total = sum(widths) + gap * (len(words) - 1)
    x = xy[0] - total / 2
    for ch, w in zip(words, widths):
        draw.text((x, xy[1]), ch, font=fnt, fill=rgba(color, alpha), anchor="lm")
        x += w + gap


def arrow(draw, start, end, color, alpha=1.0, width=5, head=16):
    if alpha <= 0:
        return
    (x0, y0), (x1, y1) = start, end
    draw.line([start, end], fill=rgba(color, alpha), width=width)
    ang = math.atan2(y1 - y0, x1 - x0)
    left = (x1 - head * math.cos(ang - 0.45), y1 - head * math.sin(ang - 0.45))
    right = (x1 - head * math.cos(ang + 0.45), y1 - head * math.sin(ang + 0.45))
    draw.polygon([end, left, right], fill=rgba(color, alpha))


@lru_cache(maxsize=1)
def backdrop() -> Image.Image:
    """The card background: near-black with a soft blue glow, drawn once."""
    small = Image.new("RGB", (192, 108), BG)
    px = small.load()
    for y in range(108):
        for x in range(192):
            d = math.hypot((x - 70) / 120, (y - 30) / 90)
            k = clamp(1 - d) ** 2 * 0.55
            px[x, y] = mix(BG, (22, 44, 90), k)
    return small.resize((W, H), Image.BICUBIC)


# -- the timeline --------------------------------------------------------------

class Timeline:
    """Where each shot and scene sits on the clock, and when words are said."""

    def __init__(self, words: list[dict]):
        self.starts = storyboard.shot_starts()
        self.words = words
        self.scene_start, cursor = {}, 0.0
        for scene in SCENES:
            self.scene_start[scene["id"]] = cursor
            cursor += scene["duration"]

    def shot_at(self, t: float) -> int:
        for i, shot in enumerate(SHOTS):
            if t < self.starts[shot["id"]] + shot["dur"]:
                return i
        return len(SHOTS) - 1

    def said(self, shot_id: str, word: str, default: float = 1.0) -> float:
        """Seconds into the shot at which ``word`` starts being spoken.

        What the cards key their animation to, so a number lands on screen as
        the voice says it. Falls back to ``default`` if the word is not there,
        which only happens if a line was reworded without its card.
        """
        for w in self.words:
            if w["shot"] == shot_id and w["text"].lower().startswith(word.lower()):
                return w["start"] - self.starts[shot_id]
        return default


# -- stills ------------------------------------------------------------------

@lru_cache(maxsize=4)
def still(shot_id: str) -> Image.Image:
    return Image.open(SHOTS_DIR / f"{shot_id}.png").convert("RGB")


@lru_cache(maxsize=1)
def top_shade() -> Image.Image:
    """A fade from dark to clear across the top of a still, under the label."""
    mask = Image.linear_gradient("L").rotate(180).resize((W, 130))
    return mask.point(lambda v: int(v * 0.85))


def render_still(shot: dict, local: float, shade: float = 1.0) -> Image.Image:
    img = still(shot["id"])
    iw, ih = img.size
    (x0, y0, w0), (x1, y1, w1) = shot["zoom"]
    k = ease(local / shot["dur"])
    x, y, w = x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, w0 + (w1 - w0) * k
    box = (x * iw, y * ih, (x + w) * iw, (y + w) * ih)
    frame_img = img.resize((W, H), Image.BICUBIC, box=box)
    if shade > 0:
        frame_img.paste(BG, (0, 0, W, 130),
                        top_shade().point(lambda v: int(v * shade)))
    return frame_img


# -- cards -------------------------------------------------------------------

def grid_positions(cols, rows, left, top, step, column_major=False):
    out = []
    if column_major:
        for c in range(cols):
            for r in range(rows):
                out.append((left + c * step, top + r * step))
    else:
        for r in range(rows):
            for c in range(cols):
                out.append((left + c * step, top + r * step))
    return out


def dot(draw, xy, radius, color, alpha=1.0):
    x, y = xy
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=rgba(color, alpha))


def card_hook(d, t, tl, shot):
    letterspaced(d, (W / 2, 300), "POLICY TIME MACHINE", font("bold", 30), BLUE,
                 appear(t, 0.1))
    big = font("black", 100)
    text(d, (W / 2, 420), "What if you could try", big, TEXT, appear(t, 0.35), rise=30)
    text(d, (W / 2, 540), "tomorrow's rules on", big, AMBER, appear(t, 0.75), rise=30)
    text(d, (W / 2, 660), "yesterday's decisions?", big, TEXT, appear(t, 1.15), rise=30)
    text(d, (W / 2, 790), "Synthetic demo data  ·  deterministic offline rules",
         font("regular", 30), MUTED, appear(t, 2.0))


SHUFFLE = list(range(600))
random.Random(7).shuffle(SHUFFLE)


def card_reveal(d, t, tl, shot):
    cases, flips = int(FIXTURE_FIGURES["cases"]), int(FIXTURE_FIGURES["flips"])
    t_num = tl.said(shot["id"], FIXTURE_FIGURES["flips"], 0.8)
    t_quarter = tl.said(shot["id"], "Almost", 3.5)
    text(d, (W / 2, 130), f"{cases} past decisions, replayed under the new rules",
         font("semibold", 46), TEXT, appear(t, 0.0), rise=20)

    lit = int(flips * ease_out((t - t_num + 0.2) / 1.6))
    lit_set = set(SHUFFLE[:lit])
    for i, xy in enumerate(grid_positions(30, 20, 190, 260, 27)):
        dot(d, xy, 10, AMBER if i in lit_set else DIM)

    counter = int(flips * ease_out((t - t_num + 0.2) / 1.6)) if t > t_num - 0.2 else 0
    a = appear(t, t_num - 0.3, 0.3)
    text(d, (1390, 420), str(counter), font("black", 260), AMBER, a)
    text(d, (1390, 590), f"out of {cases} get a", font("semibold", 52), TEXT, a)
    text(d, (1390, 655), "different answer", font("semibold", 52), TEXT, a)
    q = appear(t, t_quarter)
    if q > 0:
        d.rounded_rectangle((1180, 720, 1600, 800), radius=40, fill=rgba(PANEL, q),
                            outline=rgba(AMBER, q), width=3)
        text(d, (1390, 760), "≈ 1 in 4", font("bold", 46), AMBER, q)


def card_split(d, t, tl, shot):
    flips = int(FIXTURE_FIGURES["flips"])
    driven = int(FIXTURE_FIGURES["policy_driven"])
    old = int(FIXTURE_FIGURES["deviations"])
    t_split = tl.said(shot["id"], FIXTURE_FIGURES["deviations"], 2.0)
    t_not = tl.said(shot["id"], "didn't", 5.0)
    text(d, (W / 2, 130), f"Where did the {flips} changes come from?",
         font("semibold", 50), TEXT, appear(t, 0.0), rise=20)
    k = ease_out((t - t_split) / 0.9)
    positions = grid_positions(21, 7, 360, 305, 60, column_major=True)
    for i, xy in enumerate(positions[:flips]):
        if i < driven:
            color = mix(AMBER, BLUE, k)
        else:
            # The pre-existing ones drift down and away from the rest.
            color = mix(AMBER, GREY, k)
            xy = (xy[0] + 40 * k, xy[1])
        dot(d, xy, 22, color)
    a = appear(t, t_split + 0.3)
    text(d, (620, 800), str(driven), font("black", 120), BLUE, a, rise=20)
    text(d, (620, 885), "caused by the new rule", font("semibold", 40), TEXT, a)
    b = appear(t, t_split + 0.8)
    text(d, (1420, 800), str(old), font("black", 120), GREY, b, rise=20)
    text(d, (1420, 885), "already broke the old rulebook", font("semibold", 40), TEXT, b)
    c = appear(t, t_not)
    if c > 0:
        d.rounded_rectangle((1210, 205, 1690, 255), radius=25, fill=rgba(PANEL, c),
                            outline=rgba(GREY, c), width=2)
        text(d, (1450, 230), "not the proposal's doing", font("semibold", 30), MUTED, c)


def card_promotion(d, t, tl, shot):
    t_q = tl.said(shot["id"], "Should", 2.5)
    text(d, (W / 2, 150), "Should today's facts rewrite the past?", font("bold", 64),
         TEXT, appear(t, t_q), rise=20)
    y = 560
    draw_len = ease_out(t / 1.2)
    x0, x1 = 300, 1620
    d.line([(x0, y), (x0 + (x1 - x0) * draw_len, y)], fill=rgba(EDGE, 1), width=6)
    marks = [(520, "Two years ago", "Expense claim", "grade 3", BLUE, 0.3),
             (1060, "Last year", "Promoted", "grade 5", AMBER, 0.9),
             (1480, "Today", "The replay", "", GREEN, 1.5)]
    for x, when, what, sub, color, at in marks:
        a = appear(t, at, 0.4)
        dot(d, (x, y), 18 * a + 1, color, a)
        text(d, (x, y + 60), when, font("regular", 34), MUTED, a)
        text(d, (x, y + 115), what, font("bold", 44), TEXT, a)
        if sub:
            text(d, (x, y + 165), sub, font("regular", 34), color, a)
    k = appear(t, t_q + 0.6, 0.9)
    if k > 0:
        # A dashed arc from today back to the claim: the question being asked.
        x_from, x_to, base, height = 1480, 520, 530, 150
        segments = 40
        drawn = int(segments * k)
        points = [(x_from + (x_to - x_from) * n / segments,
                   base - height * math.sin(math.pi * n / segments))
                  for n in range(segments + 1)]
        for n in range(0, drawn, 2):
            d.line([points[n], points[n + 1]], fill=rgba(AMBER, 1), width=5)
        if k >= 1:
            arrow(d, points[-3], (x_to, base + 12), AMBER, width=5, head=20)
        text(d, ((x_from + x_to) / 2, base - height - 55), "?", font("black", 80),
             AMBER, k)


def card_wrong(d, t, tl, shot):
    cases = FIXTURE_FIGURES["cases"]
    text(d, (560, 470), NAIVE_WRONG, font("black", 330), RED, appear(t, 0.0), rise=30)
    a = appear(t, 0.4)
    text(d, (880, 390), "answers come out wrong", font("bold", 64), TEXT, a, anchor="lm")
    text(d, (880, 470), f"of {cases}, when the replay peeks", font("regular", 46), MUTED,
         a, anchor="lm")
    text(d, (880, 530), "at today's facts", font("regular", 46), MUTED, a, anchor="lm")
    k = appear(t, tl.said(shot["id"], "Point", 1.0))
    if k > 0:
        d.rounded_rectangle((360, 700, 1560, 800), radius=50, fill=rgba(PANEL, k),
                            outline=rgba(GREEN, k), width=3)
        dot(d, (420, 750), 26, GREEN, k)
        d.line([(407, 751), (417, 762), (435, 740)], fill=rgba(BG, k), width=6)
        text(d, (470, 750), "Point-in-time replay: only what was known on the day",
             font("semibold", 42), TEXT, k, anchor="lm")


def card_loop(d, t, tl, shot):
    text(d, (W / 2, 150), "The whole machine, in four steps", font("bold", 60), TEXT,
         appear(t, 0.0), rise=20)
    steps = [("Remember", "the old facts,", "as they were", "Remember"),
             ("Replay", "both rulebooks,", "side by side", "Replay"),
             ("Ask a person", "about the cases", "that matter", "Ask"),
             ("Check", "every future rule", "against them", "Then")]
    cw, gap, top, bottom = 370, 60, 360, 690
    left = (W - (4 * cw + 3 * gap)) / 2
    for i, (title, l1, l2, cue) in enumerate(steps):
        on = appear(t, tl.said(shot["id"], cue, 1.5 + 2.5 * i), 0.45)
        x = left + i * (cw + gap)
        fill = mix(PANEL, (24, 40, 74), on)
        edge = mix(EDGE, BLUE, on)
        d.rounded_rectangle((x, top, x + cw, bottom), radius=24, fill=fill,
                            outline=edge, width=4)
        dot(d, (x + cw / 2, top + 70), 34, mix(DIM, BLUE, on))
        text(d, (x + cw / 2, top + 70), str(i + 1), font("bold", 40),
             mix(MUTED, BG, on))
        text(d, (x + cw / 2, top + 165), title, font("bold", 50), mix(MUTED, TEXT, on))
        text(d, (x + cw / 2, top + 240), l1, font("regular", 34), mix(DIM, MUTED, on))
        text(d, (x + cw / 2, top + 285), l2, font("regular", 34), mix(DIM, MUTED, on))
        if i:
            arrow(d, (x - gap + 8, (top + bottom) / 2), (x - 8, (top + bottom) / 2),
                  mix(EDGE, BLUE, on))
    back = appear(t, tl.said(shot["id"], "answers", 11.5) + 0.2, 0.8)
    if back > 0:
        y = bottom + 70
        x_last, x_first = left + 3 * (cw + gap) + cw / 2, left + cw / 2
        d.line([(x_last, bottom + 8), (x_last, y)], fill=rgba(AMBER, back), width=5)
        d.line([(x_last, y), (x_last + (x_first - x_last) * back, y)],
               fill=rgba(AMBER, back), width=5)
        if back > 0.95:
            arrow(d, (x_first, y), (x_first, bottom + 10), AMBER)
        text(d, (W / 2, y + 45), "every answer is remembered for the next proposal",
             font("semibold", 36), AMBER, back)


def card_eight(d, t, tl, shot):
    cases, reviews = int(FIXTURE_FIGURES["cases"]), int(REVIEWS)
    t_eight = tl.said(shot["id"], "eight", 3.5)
    k = ease(clamp((t - t_eight + 0.2) / 1.0))
    chosen = SHUFFLE[:reviews]
    grid = grid_positions(30, 20, 230, 300, 20)
    targets = grid_positions(4, 2, 1270, 400, 130)
    for i, xy in enumerate(grid):
        if i not in chosen:
            dot(d, xy, 6, DIM, 1 - 0.5 * k)
    for n, i in enumerate(chosen):
        (x0, y0), (x1, y1) = grid[i], targets[n]
        xy = (x0 + (x1 - x0) * k, y0 + (y1 - y0) * k)
        dot(d, xy, 6 + 38 * k, mix(DIM, GREEN, clamp(k * 3)))
    a = appear(t, 0.2)
    text(d, (515, 190), str(cases), font("black", 110), MUTED, a)
    text(d, (515, 740), "cases replayed", font("semibold", 42), MUTED, a)
    b = appear(t, t_eight + 0.6)
    text(d, (1465, 190), str(reviews), font("black", 110), GREEN, b)
    text(d, (1465, 740), "for a person to judge", font("semibold", 42), TEXT, b)
    arrow(d, (870, 490), (1150, 490), GREEN, appear(t, t_eight), width=6, head=22)


def card_tradeoff(d, t, tl, shot):
    text(d, (W / 2, 190), "Every option, with its consequences", font("bold", 60), TEXT,
         appear(t, 0.0), rise=20)
    for i, label in enumerate(("£50", "£100", "£150")):
        a = appear(t, 0.4 + 0.3 * i)
        x = W / 2 + (i - 1) * 360
        d.rounded_rectangle((x - 140, 330, x + 140, 470), radius=30,
                            fill=rgba(PANEL, a), outline=rgba(BLUE, a), width=3)
        text(d, (x, 400), label, font("black", 72), BLUE, a)
    k = appear(t, tl.said(shot["id"], "They", 2.0))
    text(d, (W / 2, 610), "Trade-offs, not a recommendation", font("bold", 58), AMBER, k,
         rise=20)
    c = appear(t, tl.said(shot["id"], "The", 4.0))
    text(d, (W / 2, 720), "The choice stays with you.", font("semibold", 48), TEXT, c,
         rise=20)


def card_end(d, t, tl, shot):
    text(d, (W / 2, 400), "Policy Time Machine", font("black", 120), TEXT,
         appear(t, 0.1), rise=30)
    text(d, (W / 2, 520), "Try tomorrow's rules on yesterday's decisions.",
         font("semibold", 52), BLUE, appear(t, 0.7), rise=20)
    a = appear(t, 1.5)
    if a > 0:
        d.rounded_rectangle((W / 2 - 290, 620, W / 2 + 290, 700), radius=16,
                            fill=rgba(PANEL, a), outline=rgba(EDGE, a), width=2)
        text(d, (W / 2, 660), "$ python demo.py --step", font("mono", 40), GREEN, a)


CARDS = {"hook": card_hook, "reveal": card_reveal, "split": card_split,
         "promotion": card_promotion, "wrong": card_wrong, "loop": card_loop,
         "eight": card_eight, "tradeoff": card_tradeoff, "end": card_end}


def render_card(shot: dict, local: float, tl: Timeline) -> Image.Image:
    img = backdrop().copy()
    CARDS[shot["card"]](ImageDraw.Draw(img, "RGBA"), local, tl, shot)
    return img


# -- subtitles -------------------------------------------------------------------

#: The longest subtitle line, in characters. One line at 50px fits ~65; fewer
#: is easier to take in during the second or two each one is on screen.
MAX_CHARS = 50


def _length(group: list[dict]) -> int:
    return len(" ".join(w["text"] for w in group))


def _balanced(phrase: list[dict]) -> list[list[dict]]:
    """A phrase too long for one line, cut into even pieces.

    Greedy filling leaves orphans - "Should that change what they could claim
    two" and then "years ago?" on its own. Instead the phrase is cut into as
    few pieces as fit, at the word boundaries nearest to equal lengths, with a
    comma a preferred place to cut.
    """
    if _length(phrase) <= MAX_CHARS or len(phrase) < 2:
        return [phrase]
    pieces = math.ceil(_length(phrase) / MAX_CHARS)
    target = _length(phrase) / pieces
    best, best_score = 1, float("inf")
    for cut in range(1, len(phrase)):
        score = abs(_length(phrase[:cut]) - target)
        if phrase[cut - 1]["text"].endswith((",", ";", ":")):
            score -= 8
        if _length(phrase[:cut]) <= MAX_CHARS and score < best_score:
            best, best_score = cut, score
    return [phrase[:best]] + _balanced(phrase[best:])


def cues(words: list[dict]) -> list[dict]:
    """The spoken words, grouped into subtitle cues.

    Words are first gathered into phrases, which end at a sentence end, a
    pause, or a change of shot; a phrase too long for one line is then cut
    evenly. Each cue is shown from just before its first word until just after
    its last, or until the next one starts.
    """
    phrases: list[list[dict]] = []
    for w in words:
        if phrases:
            last = phrases[-1]
            ends = last[-1]["text"].endswith((".", "?", "!"))
            if (w["shot"] == last[-1]["shot"] and not ends
                    and w["start"] - last[-1]["end"] < 0.3):
                last.append(w)
                continue
        phrases.append([w])
    # Very short sentences ("Of course not.") read better joined to the next.
    merged: list[list[dict]] = []
    for phrase in phrases:
        if (merged and merged[-1][-1]["shot"] == phrase[0]["shot"]
                and _length(merged[-1]) < 16
                and _length(merged[-1]) + 1 + _length(phrase) <= MAX_CHARS
                and phrase[0]["start"] - merged[-1][-1]["end"] < 0.6):
            merged[-1] = merged[-1] + phrase
        else:
            merged.append(phrase)
    groups = [piece for phrase in merged for piece in _balanced(phrase)]
    out = []
    for i, g in enumerate(groups):
        start = max(0.0, g[0]["start"] - 0.08)
        end = g[-1]["end"] + 0.45
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0]["start"] - 0.1)
        out.append({"start": start, "end": max(end, g[-1]["end"]),
                    "text": " ".join(x["text"] for x in g)})
    return out


def srt(cue_list: list[dict]) -> str:
    def stamp(s: float) -> str:
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}\n"
                     for i, c in enumerate(cue_list, 1))


def draw_subtitle(d: ImageDraw.ImageDraw, line: str, alpha: float):
    """One cue, bottom centre, white on a dark pill; numbers in amber."""
    fnt = font("semibold", 50)
    words = line.split(" ")
    space = d.textlength(" ", font=fnt)
    widths = [d.textlength(w, font=fnt) for w in words]
    total = sum(widths) + space * (len(words) - 1)
    x, y = (W - total) / 2, 975
    d.rounded_rectangle((x - 28, y - 44, x + total + 28, y + 40), radius=18,
                        fill=(4, 7, 14, int(200 * alpha)))
    for word, width in zip(words, widths):
        color = AMBER if any(ch.isdigit() for ch in word) else TEXT
        d.text((x, y), word, font=fnt, fill=rgba(color, alpha), anchor="lm")
        x += width + space


# -- one frame -----------------------------------------------------------------

#: Seconds the chapter label stays up at the start of each scene. It then
#: fades, so it is never parked over the page a still is showing.
LABEL_SECONDS = 4.0


def label_alpha(t: float, tl: Timeline, scene_id: str) -> float:
    into = t - tl.scene_start[scene_id]
    return min(clamp(into / 0.3), clamp((LABEL_SECONDS - into) / 0.5))


def render_shot(i: int, t: float, tl: Timeline) -> Image.Image:
    shot = SHOTS[i]
    local = clamp(t - tl.starts[shot["id"]], 0, shot["dur"])
    if "card" in shot:
        return render_card(shot, local, tl)
    return render_still(shot, local, label_alpha(t, tl, shot["scene"]))


def frame(t: float, tl: Timeline, cue_list: list[dict]) -> Image.Image:
    i = tl.shot_at(t)
    img = render_shot(i, t, tl)
    into = t - tl.starts[SHOTS[i]["id"]]
    if i and into < CROSSFADE:
        img = Image.blend(render_shot(i - 1, t, tl), img, ease(into / CROSSFADE))

    d = ImageDraw.Draw(img, "RGBA")
    # Stills are the dashboard's own dark UI; a soft band behind the chrome
    # keeps the label and subtitles legible over whatever the page shows.
    scene = next(s for s in SCENES if s["id"] == SHOTS[i]["scene"])
    number = SCENES.index(scene) + 1
    label = f"{number}/{len(SCENES)}   {scene['name']}"
    shown = label_alpha(t, tl, scene["id"])
    if shown > 0:
        fnt = font("semibold", 30)
        width = d.textlength(label, font=fnt)
        d.rounded_rectangle((40, 36, 40 + width + 48, 92), radius=28,
                            fill=(4, 7, 14, int(210 * shown)),
                            outline=rgba(EDGE, shown), width=2)
        d.text((64, 64), label, font=fnt, fill=rgba(TEXT, shown), anchor="lm")

    for c in cue_list:
        if c["start"] <= t < c["end"]:
            fade = min(clamp((t - c["start"]) / 0.12), clamp((c["end"] - t) / 0.12))
            draw_subtitle(d, c["text"], fade)
            break

    d.rectangle((0, H - 8, W, H), fill=(15, 23, 42, 255))
    d.rectangle((0, H - 8, W * t / TOTAL_SECONDS, H), fill=(*BLUE, 255))

    # Fade in from black, and out to black at the very end.
    level = min(clamp(t / 0.6), clamp((TOTAL_SECONDS - t) / 1.0))
    if level < 1:
        img = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), img, level)
    return img


# -- encoding ------------------------------------------------------------------

def encode_chunk(args) -> str:
    index, first, last = args
    words = json.loads(WORDS.read_text(encoding="utf-8"))
    tl, cue_list = Timeline(words), cues(words)
    path = CHUNKS / f"chunk_{index:03d}.mp4"
    proc = subprocess.Popen(
        [ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0",
         "-c:v", "libx264", "-preset", "slow", "-crf", "22", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(path)],
        stdin=subprocess.PIPE)
    for n in range(first, last):
        proc.stdin.write(frame(n / FPS, tl, cue_list).tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg failed encoding {path}")
    return str(path)


def main(argv: list[str]) -> int:
    problems = storyboard.check()
    if problems:
        # Checked before a single frame is drawn. A storyboard that does not
        # reconcile produces a video whose narration slides out from under the
        # pictures, and finding that at the end costs the whole render.
        for line in problems:
            print(f"ERROR {line}", file=sys.stderr)
        return 1
    missing = [str(p) for p in (WORDS, AUDIO) if not p.exists()]
    missing += [str(SHOTS_DIR / f"{s['id']}.png") for s in storyboard.captures()
                if not (SHOTS_DIR / f"{s['id']}.png").exists()]
    if missing:
        print("ERROR missing inputs - run build_shots.py and generate_audio.py first: "
              + ", ".join(missing), file=sys.stderr)
        return 1

    words = json.loads(WORDS.read_text(encoding="utf-8"))
    cue_list = cues(words)
    SRT.write_text(srt(cue_list), encoding="utf-8")
    print(f"Wrote {len(cue_list)} subtitle cues to {SRT}")

    if "--preview" in argv:
        # Stills at chosen moments, for checking a layout without a render.
        CHUNKS.mkdir(parents=True, exist_ok=True)
        tl = Timeline(words)
        for stamp in argv[argv.index("--preview") + 1:]:
            frame(float(stamp), tl, cue_list).save(CHUNKS / f"preview_{stamp}.png")
            print(f"  preview at {stamp}s -> {CHUNKS / f'preview_{stamp}.png'}")
        return 0

    CHUNKS.mkdir(parents=True, exist_ok=True)
    total = int(round(TOTAL_SECONDS * FPS))
    workers = max(1, min(os.cpu_count() or 1, 10))
    size = math.ceil(total / workers)
    jobs = [(i, a, min(a + size, total)) for i, a in enumerate(range(0, total, size))]
    print(f"Rendering {total} frames in {len(jobs)} chunk(s)...")
    with ProcessPoolExecutor(workers) as pool:
        parts = list(pool.map(encode_chunk, jobs))

    listing = CHUNKS / "concat.txt"
    listing.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts),
                       encoding="utf-8")
    print("Muxing picture, narration and subtitles...")
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listing),
         "-i", str(AUDIO), "-i", str(SRT),
         "-map", "0:v", "-map", "1:a", "-map", "2:s",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
         "-metadata:s:s:0", "language=eng",
         # The storyboard's total, not a number typed here: a typed total is
         # the copy of the contract that silently truncates the ending.
         "-t", str(TOTAL_SECONDS), "-movflags", "+faststart", str(OUTPUT)],
        check=True)
    size_mb = OUTPUT.stat().st_size / 1024 / 1024
    print(f"\nWrote {OUTPUT} ({TOTAL_SECONDS:.0f}s, {size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
