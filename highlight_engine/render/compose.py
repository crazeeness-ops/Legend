"""Render a vertical 1080x1920 short: reframed video + burned-in animated captions + original audio."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from .captions import PRESETS, CaptionStyle, build_ass
from .detect import FaceDetector, HaarFaceDetector
from .media import filter_path, probe, read_frames
from .reframe import OUT_H, OUT_W, ReframePlan, Rect, analyze, apply_overrides


def _crop_resize(frame: np.ndarray, r: Rect, w: int, h: int) -> np.ndarray:
    crop = frame[r.y : r.y + r.h, r.x : r.x + r.w]
    interp = cv2.INTER_AREA if crop.shape[1] > w else cv2.INTER_CUBIC
    return cv2.resize(crop, (w, h), interpolation=interp)


def _fit_with_blur(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    # background: cover-scale, heavy blur done at low resolution for speed, darkened
    small_w = OUT_W // 8
    small_h = OUT_H // 8
    s = max(small_w / w, small_h / h)
    bg = cv2.resize(frame, (max(small_w, int(w * s) + 1), max(small_h, int(h * s) + 1)), interpolation=cv2.INTER_AREA)
    y0 = (bg.shape[0] - small_h) // 2
    x0 = (bg.shape[1] - small_w) // 2
    bg = cv2.GaussianBlur(bg[y0 : y0 + small_h, x0 : x0 + small_w], (0, 0), 6)
    bg = cv2.resize(bg, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
    bg = (bg.astype(np.float32) * 0.55).astype(np.uint8)
    # foreground: whole frame fitted to the width
    fg_h = min(OUT_H, int(round(OUT_W * h / w / 2)) * 2)
    fg_w = OUT_W if fg_h < OUT_H else int(round(OUT_H * w / h / 2)) * 2
    fg = cv2.resize(frame, (fg_w, fg_h), interpolation=cv2.INTER_AREA if w > fg_w else cv2.INTER_CUBIC)
    y = (OUT_H - fg_h) // 2
    x = (OUT_W - fg_w) // 2
    bg[y : y + fg_h, x : x + fg_w] = fg
    return bg


def compose_frame(frame: np.ndarray, plan: ReframePlan, t: float) -> np.ndarray:
    shot = plan.shot_at(t)
    if shot.layout == "track":
        return _crop_resize(frame, plan.crop_rect(t, shot), OUT_W, OUT_H)
    if shot.layout == "split" and shot.split:
        top_r, bottom_r = shot.split
        half = OUT_H // 2
        out = np.vstack([_crop_resize(frame, top_r, OUT_W, half), _crop_resize(frame, bottom_r, OUT_W, half)])
        out[half - 3 : half + 3] = 0  # thin divider
        return out
    return _fit_with_blur(frame)


def render_short(
    video: str,
    clip: dict,
    out_path: str,
    *,
    style: CaptionStyle | str = "legend",
    layout: str = "auto",
    show_title: bool = True,
    detector: FaceDetector | None = None,
    crf: int = 20,
    x264_preset: str = "veryfast",
    fonts_dir: str | None = None,
    plan: ReframePlan | None = None,
    shot_overrides: list[dict] | None = None,
    progress: Callable[[float], None] | None = None,
) -> dict:
    """Render one clip (a dict from the engine's output JSON) to a vertical MP4.

    Writes ``<out>.json`` next to the video with the reframe plan (layout, crop
    keyframes) so an editor UI can show and adjust it. Pass ``plan`` to reuse an
    analysis, and ``shot_overrides`` to apply manual per-shot layout/crop edits.
    """
    t0 = time.perf_counter()
    info = probe(video)
    start = float(clip["start"])
    duration = float(clip["end"]) - start
    if plan is None:
        plan = analyze(video, start, duration, info, detector or HaarFaceDetector(), layout=layout)
    if shot_overrides:
        apply_overrides(plan, shot_overrides)
    t_analyze = time.perf_counter() - t0

    ass = build_ass(
        clip.get("words", []), clip_start=start, duration=duration, style=style,
        caption_y=plan.caption_y_at, title=clip.get("title") if show_title else None,
        keywords=clip.get("keywords", []),
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        ass_path = Path(tmp) / "captions.ass"
        ass_path.write_text(ass, encoding="utf-8")
        ass_filter = f"ass=filename='{filter_path(ass_path)}'" + (f":fontsdir='{filter_path(fonts_dir)}'" if fonts_dir else "")
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", f"{info.fps:.6f}", "-i", "-",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", video,
            "-filter_complex", f"[0:v]{ass_filter}[v]", "-map", "[v]", "-map", "1:a?",
            "-c:v", "libx264", "-preset", x264_preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        frames = 0
        try:
            for frame in read_frames(video, start, duration, info.width, info.height):
                proc.stdin.write(compose_frame(frame, plan, frames / info.fps).tobytes())
                frames += 1
                if progress and frames % 15 == 0:
                    progress(min(0.99, frames / max(1.0, duration * info.fps)))
        finally:
            proc.stdin.close()
            code = proc.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg failed with exit code {code}")

    st = PRESETS[style] if isinstance(style, str) else style
    meta = {
        "clip_rank": clip.get("rank"),
        "caption_style": asdict(st),
        "title": clip.get("title"),
        "output": out.name,
        "frames": frames,
        "timings_sec": {"analyze": round(t_analyze, 2), "total": round(time.perf_counter() - t0, 2)},
        "reframe": plan.to_dict(),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta
