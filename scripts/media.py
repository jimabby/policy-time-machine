"""The two tools the video scripts shell out to, found rather than assumed.

ffmpeg is taken from PATH when it is there and otherwise from the
``imageio-ffmpeg`` wheel, which bundles a static build for every platform - so
the pipeline runs on a Windows machine with nothing installed but pip, which is
the machine the first cut could not be rebuilt on.
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np

#: Every audio buffer in the pipeline is mono float32 at this rate.
RATE = 48000


def ffmpeg() -> str:
    """The ffmpeg executable, or a refusal that says how to get one."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        raise SystemExit("ffmpeg is not on PATH. Install it, or `pip install "
                         "imageio-ffmpeg`, which bundles one.") from None
    return imageio_ffmpeg.get_ffmpeg_exe()


def decode(data: bytes) -> np.ndarray:
    """Any audio ffmpeg reads, as mono float32 samples at :data:`RATE`."""
    out = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1",
         "-ar", str(RATE), "pipe:1"],
        input=data, capture_output=True, check=True)
    return np.frombuffer(out.stdout, dtype=np.float32).copy()
