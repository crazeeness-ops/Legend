"""Cut preview clips with ffmpeg (no reframing yet; that's the next pipeline stage)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import Clip


def export_clips(source: str, clips: list[Clip], out_dir: str) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for clip in clips:
        dest = out / f"clip_{clip.rank:02d}_{clip.score}.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y",
             "-ss", f"{clip.start:.2f}", "-i", source, "-t", f"{clip.end - clip.start:.2f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)],
            check=True,
        )
        paths.append(dest)
    return paths
