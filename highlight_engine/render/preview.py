"""Style gallery: one still per caption preset, tiled into a single image for choosing a look."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .media import filter_path
from .captions import OUT_H, OUT_W, PRESETS, CaptionStyle, build_ass

SAMPLE_WORDS = [
    ("직장인이", 0.0, 0.4), ("부업으로", 0.4, 0.8), ("월", 0.8, 1.0), ("100만", 1.0, 1.4),
    ("원", 1.4, 1.6), ("진짜", 1.6, 2.0), ("가능해요?", 2.0, 2.5),
]
SNAPSHOT_AT = 1.2  # "100만" is being spoken


def _background(video: str | None, at: float) -> np.ndarray:
    if video:
        from .compose import _fit_with_blur

        png = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(at), "-i", video, "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, check=True,
        ).stdout
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            return _fit_with_blur(img)
    y = np.linspace(0, 1, OUT_H, dtype=np.float32)[:, None, None]
    top, bottom = np.array([94, 58, 40], np.float32), np.array([40, 30, 22], np.float32)
    return np.broadcast_to(top * (1 - y) + bottom * y, (OUT_H, OUT_W, 3)).astype(np.uint8).copy()


def render_still(style: CaptionStyle, background: np.ndarray, *, title: str | None = None) -> np.ndarray:
    words = [{"text": t, "start": s, "end": e} for t, s, e in SAMPLE_WORDS]
    ass = build_ass(words, clip_start=0.0, duration=3.0, style=style, keywords=["100만"], title=title)
    with tempfile.TemporaryDirectory() as tmp:
        bg, sub, out = Path(tmp) / "bg.png", Path(tmp) / "s.ass", Path(tmp) / "out.png"
        cv2.imwrite(str(bg), background)
        sub.write_text(ass, encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-t", "3", "-r", "25", "-i", str(bg),
             "-vf", f"ass=filename='{filter_path(sub)}'", "-ss", str(SNAPSHOT_AT), "-frames:v", "1", "-update", "1", str(out)],
            check=True,
        )
        return cv2.imread(str(out))


def preview_gallery(
    out_path: str,
    styles: dict[str, CaptionStyle] | None = None,
    *,
    video: str | None = None,
    at: float = 1.0,
    cols: int = 4,
    tile_w: int = 360,
) -> Path:
    styles = styles or PRESETS
    bg = _background(video, at)
    tile_h = int(tile_w * OUT_H / OUT_W)
    label_h = 40
    tiles = []
    for name, st in styles.items():
        img = cv2.resize(render_still(st, bg), (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        tile = np.full((tile_h + label_h, tile_w, 3), 24, np.uint8)
        tile[:tile_h] = img
        cv2.putText(tile, name, (12, tile_h + 29), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(tile)
    rows = -(-len(tiles) // cols)
    blank = np.full_like(tiles[0], 24)
    tiles += [blank] * (rows * cols - len(tiles))
    grid = np.vstack([np.hstack(tiles[r * cols : (r + 1) * cols]) for r in range(rows)])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), grid)
    return out
