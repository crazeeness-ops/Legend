"""ffmpeg/ffprobe helpers: probing and frame-accurate decoding into numpy."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Iterator

import numpy as np


def filter_path(path) -> str:
    """A file path safe inside an ffmpeg filter argument (quote it with single quotes).

    ``C:\\Users\\me\\a.ass`` → ``C\\:/Users/me/a.ass`` — backslashes become slashes and the drive colon is escaped.
    """
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool


def probe(path: str) -> VideoInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, check=True, text=True,
    ).stdout
    data = json.loads(out)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    num, _, den = v.get("avg_frame_rate", "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(num) else 30.0
    # honour rotation metadata (phone videos)
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", 0) or 0)
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) in (90, 270):
        w, h = h, w
    return VideoInfo(
        width=w, height=h, fps=fps,
        duration=float(data["format"].get("duration", v.get("duration", 0))),
        has_audio=any(s["codec_type"] == "audio" for s in data["streams"]),
    )


def read_frames(
    path: str, start: float, duration: float, width: int, height: int, *,
    fps: float | None = None, gray: bool = False,
) -> Iterator[np.ndarray]:
    """Decode frames of [start, start+duration) scaled to width x height.

    ``-ss`` before ``-i`` with decoding is frame-accurate in modern ffmpeg, and the
    same seek is used for the audio track, so picture and sound stay in sync.
    """
    filters = []
    if fps:
        filters.append(f"fps={fps}")
    filters.append(f"scale={width}:{height}")
    pix = "gray" if gray else "bgr24"
    channels = 1 if gray else 3
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", path, "-t", f"{duration:.3f}",
         "-vf", ",".join(filters), "-f", "rawvideo", "-pix_fmt", pix, "-"],
        stdout=subprocess.PIPE,
    )
    size = width * height * channels
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            frame = np.frombuffer(buf, np.uint8)
            yield frame.reshape(height, width) if gray else frame.reshape(height, width, 3)
    finally:
        proc.stdout.close()
        proc.wait()
