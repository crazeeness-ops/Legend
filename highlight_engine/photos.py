"""Photo → vertical short: Ken Burns motion, crossfades, animated captions, end card, narration + music.

    slides = [Slide("a.jpg", "성수동 줄 서는 크로플", 3.5), ...]
    render_slideshow(slides, "out.mp4", style="legend_sticker", end_card={...}, narration=[...], music="bgm.mp3")
"""

from __future__ import annotations

import math
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .render.captions import PRESETS, CaptionStyle, ass_color, ass_time, build_ass, escape
from .render.media import filter_path

OUT_W, OUT_H, FPS = 1080, 1920, 30
XFADE = 0.4  # crossfade between slides (s)
ZOOM = 0.10  # Ken Burns zoom over a slide
AUDIO_SR = 44100
END_CARD_SEC = 3.0
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}

try:  # iPhone photos (HEIC); optional so a missing wheel never blocks installation
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # pragma: no cover
    HEIC_SUPPORTED = False


@dataclass
class Slide:
    path: str
    text: str = ""
    duration: float = 3.0
    narration: np.ndarray | None = field(default=None, repr=False)  # mono float32 @ AUDIO_SR


# ------------------------------------------------------------------ images

def load_photo(path: str, max_side: int = 2400) -> np.ndarray:
    """BGR image with EXIF rotation applied (phones store portrait shots rotated)."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def normalize_photo(src: str, dest: str, thumb: str | None = None) -> tuple[int, int]:
    """Store an upload as a sane JPEG (+ optional 360px thumbnail). Returns (width, height)."""
    img = load_photo(src)
    cv2.imwrite(dest, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if thumb:
        h, w = img.shape[:2]
        s = 360 / max(h, w)
        cv2.imwrite(thumb, cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA),
                    [cv2.IMWRITE_JPEG_QUALITY, 85])
    return img.shape[1], img.shape[0]


def _blurred_bg(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    sw, sh = OUT_W // 8, OUT_H // 8
    s = max(sw / w, sh / h)
    small = cv2.resize(img, (max(sw, int(w * s) + 1), max(sh, int(h * s) + 1)), interpolation=cv2.INTER_AREA)
    y0, x0 = (small.shape[0] - sh) // 2, (small.shape[1] - sw) // 2
    bg = cv2.GaussianBlur(small[y0:y0 + sh, x0:x0 + sw], (0, 0), 5)
    return (cv2.resize(bg, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR).astype(np.float32) * 0.5).astype(np.uint8)


class SlideFrames:
    """Pre-scales one photo so each frame is a cheap affine warp."""

    def __init__(self, img: np.ndarray, index: int) -> None:
        h, w = img.shape[:2]
        self.portrait = w / h <= 0.8  # close enough to 9:16 to fill the screen
        self.direction = (-1, 1, 0, 1, -1, 0)[index % 6]  # pan left / right / none, alternating
        self.zoom_in = index % 2 == 0
        if self.portrait:  # cover the screen
            s = max(OUT_W / w, OUT_H / h) * (1 + ZOOM + 0.04)
            self.base = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            self.bg = None
        else:  # whole photo over a blurred copy of itself
            s = OUT_W / w * (1 + ZOOM)
            self.base = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            self.bg = _blurred_bg(img)

    def frame(self, p: float) -> np.ndarray:
        """p = 0..1 progress through the slide."""
        e = p * p * (3 - 2 * p)  # ease in/out
        z = (1 + ZOOM * e) if self.zoom_in else (1 + ZOOM * (1 - e))
        bh, bw = self.base.shape[:2]
        if self.portrait:
            scale = z / (1 + ZOOM + 0.04)
            cx = bw / 2 + self.direction * (e - 0.5) * (bw * scale - OUT_W) * 0.8 / scale
            cy = bh / 2
            m = np.float32([[scale, 0, OUT_W / 2 - cx * scale], [0, scale, OUT_H / 2 - cy * scale]])
            return cv2.warpAffine(self.base, m, (OUT_W, OUT_H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        scale = z / (1 + ZOOM)
        out = self.bg.copy()
        fw, fh = int(bw * scale), int(bh * scale)
        fg = cv2.resize(self.base, (fw, fh), interpolation=cv2.INTER_AREA)
        x = (OUT_W - fw) // 2 + int(self.direction * (e - 0.5) * 40)
        y = int(OUT_H * 0.40 - fh / 2)
        xs, ys = max(0, -x), max(0, -y)
        xd, yd = max(0, x), max(0, y)
        cw, ch = min(fw - xs, OUT_W - xd), min(fh - ys, OUT_H - yd)
        if cw > 0 and ch > 0:
            out[yd:yd + ch, xd:xd + cw] = fg[ys:ys + ch, xs:xs + cw]
        return out


# ------------------------------------------------------------------ timing

def timeline(slides: list[Slide], end_card: bool) -> tuple[list[tuple[float, float]], float]:
    spans, t = [], 0.0
    for s in slides:
        spans.append((t, t + s.duration))
        t += s.duration
    return spans, t + (END_CARD_SEC if end_card else 0.0)


def _balanced_breaks(toks: list[str], max_chars: int, max_words: int) -> set[int]:
    """Indices after which a caption page ends, so pages are evenly sized (no lonely last word)."""
    chars = sum(len(t) for t in toks)
    pages = max(math.ceil(chars / max_chars), math.ceil(len(toks) / max_words), 1)
    if pages == 1:
        return set()
    target, acc, breaks = chars / pages, 0, set()
    for i, t in enumerate(toks[:-1]):
        acc += len(t)
        if acc >= target * (len(breaks) + 1) - len(t) / 2 and len(breaks) < pages - 1:
            breaks.add(i)
    return breaks


def caption_words(slides: list[Slide], spans: list[tuple[float, float]], lead: float = 0.25,
                  max_chars: int = 13, max_words: int = 4) -> list[dict]:
    """Word timings: spread over the narration if there is one, else over the slide; longer words get more time."""
    words = []
    for s, (a, b) in zip(slides, spans):
        toks = s.text.split()
        if not toks:
            continue
        start = a + lead
        end = start + len(s.narration) / AUDIO_SR if s.narration is not None else b - 0.35
        end = max(start + 0.4, min(end, b - 0.1))
        weights = [len(t) + 1 for t in toks]
        total, t = sum(weights), start
        breaks = _balanced_breaks(toks, max_chars, max_words)
        for j, (tok, wgt) in enumerate(zip(toks, weights)):
            if j in breaks and not tok.endswith((",", ".", "?", "!")):
                tok += ","  # page break (commas at line ends aren't shown)
            d = (end - start) * wgt / total
            if j == len(toks) - 1 and not tok.endswith((".", "?", "!", "…", "~")):
                tok += "."  # ends the caption page with the slide (the period itself isn't shown)
            words.append({"text": tok, "start": round(t, 3), "end": round(t + d, 3)})
            t += d
    return words


def end_card_events(card: dict, style: CaptionStyle, start: float, end: float) -> list[str]:
    """Info card on the last screen: big name, then up to 3 detail lines."""
    lines = [str(card.get(k, "")).strip() for k in ("line1", "line2", "line3") if str(card.get(k, "")).strip()]
    title = str(card.get("title", "")).strip()
    if not title and not lines:
        return []
    accent = ass_color(style.title_box_color if style.title_box_color else "FF3D6E")
    s0, s1 = ass_time(start), ass_time(end)
    box_h = 200 + 90 * len(lines)
    y0 = OUT_H // 2 - box_h // 2
    card_shape = f"m 120 {y0} l 960 {y0} l 960 {y0 + box_h} l 120 {y0 + box_h}"
    ev = [f"Dialogue: 7,{s0},{s1},Caption,,0,0,0,,{{\\an7\\pos(0,0)\\bord0\\shad0\\p1\\c&H141414&\\alpha&H30&\\fad(250,0)}}{card_shape}",
          f"Dialogue: 8,{s0},{s1},Caption,,0,0,0,,{{\\an7\\pos(0,0)\\bord0\\shad0\\p1\\c{accent}\\fad(250,0)}}"
          f"m 120 {y0} l 960 {y0} l 960 {y0 + 14} l 120 {y0 + 14}"]
    if title:
        ev.append(f"Dialogue: 9,{s0},{s1},Caption,,0,0,0,,{{\\an5\\pos(540,{y0 + 110})\\fs78\\b1\\bord0\\shad0\\blur0"
                  f"\\c&HFFFFFF&\\fad(250,0)}}{escape(title)}")
    for i, line in enumerate(lines):
        ev.append(f"Dialogue: 9,{s0},{s1},Caption,,0,0,0,,{{\\an5\\pos(540,{y0 + 200 + 90 * i})\\fs50\\b0\\bord0\\shad0"
                  f"\\blur0\\c&HDDDDDD&\\fad(350,0)}}{escape(line)}")
    return ev


# ------------------------------------------------------------------ audio

def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_SR)
        w.writeframes(pcm.tobytes())


def narration_track(slides: list[Slide], spans: list[tuple[float, float]], total: float, lead: float = 0.25) -> np.ndarray | None:
    if not any(s.narration is not None for s in slides):
        return None
    out = np.zeros(int(total * AUDIO_SR) + AUDIO_SR, dtype=np.float32)
    for s, (a, _) in zip(slides, spans):
        if s.narration is not None:
            i = int((a + lead) * AUDIO_SR)
            seg = s.narration[: len(out) - i]
            out[i:i + len(seg)] += seg
    return out


def decode_audio(data: bytes | str) -> np.ndarray:
    """Any audio (bytes or path) → mono float32 @ AUDIO_SR via ffmpeg."""
    src = ["-i", data] if isinstance(data, str) else ["-i", "pipe:0"]
    proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *src, "-vn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "f32le", "-"],
                          input=None if isinstance(data, str) else data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"오디오를 읽지 못했어요: {proc.stderr.decode(errors='ignore')[-200:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


# ------------------------------------------------------------------ render

def render_slideshow(
    slides: list[Slide],
    out_path: str,
    *,
    style: CaptionStyle | str = "legend",
    title: str | None = None,
    keywords: list[str] | None = None,
    end_card: dict | None = None,
    music: str | None = None,
    music_volume: float = 0.3,
    progress: Callable[[float], None] | None = None,
) -> dict:
    if not slides:
        raise ValueError("사진이 한 장 이상 필요해요")
    st = PRESETS[style] if isinstance(style, str) else style
    spans, total = timeline(slides, bool(end_card))
    words = caption_words(slides, spans, max_chars=st.max_chars, max_words=1 if st.mode == "single" else st.max_words)
    ass = build_ass(words, clip_start=0.0, duration=total, style=st, caption_y=int(OUT_H * 0.74), title=title,
                    keywords=keywords or [])
    if end_card:
        ass = ass.rstrip("\n") + "\n" + "\n".join(end_card_events(end_card, st, spans[-1][1], total)) + "\n"

    frames = [SlideFrames(load_photo(s.path), i) for i, s in enumerate(slides)]
    last_bg = _blurred_bg(load_photo(slides[-1].path)) if end_card else None
    n_frames = int(round(total * FPS))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        sub = Path(tmp) / "captions.ass"
        sub.write_text(ass, encoding="utf-8")
        inputs, filters, audio_map = [], [], None
        voice = narration_track(slides, spans, total)
        if voice is not None:
            _write_wav(Path(tmp) / "voice.wav", voice)
            inputs += ["-i", str(Path(tmp) / "voice.wav")]
        if music:
            inputs += ["-stream_loop", "-1", "-i", music]
        fade_out = max(0.0, total - 1.5)
        if voice is not None and music:
            vol = music_volume * 0.45  # duck the music under the voice
            filters.append(f"[2:a]volume={vol:.3f},afade=t=out:st={fade_out:.2f}:d=1.5[m];"
                           f"[1:a][m]amix=inputs=2:duration=first:normalize=0[a]")
            audio_map = "[a]"
        elif voice is not None:
            audio_map = "1:a"
        elif music:
            filters.append(f"[1:a]volume={music_volume:.3f},afade=t=out:st={fade_out:.2f}:d=1.5[a]")
            audio_map = "[a]"
        else:  # silent track: platforms expect audio
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_SR}:cl=mono"]
            audio_map = "1:a"
        fc = f"[0:v]ass=filename='{filter_path(sub)}'[v]" + ("".join(";" + f for f in filters))
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-i", "-",
               *inputs, "-filter_complex", fc, "-map", "[v]", "-map", audio_map,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            for i in range(n_frames):
                t = i / FPS
                proc.stdin.write(_compose(t, slides, spans, frames, last_bg).tobytes())
                if progress and i % 15 == 0:
                    progress(i / n_frames)
        finally:
            proc.stdin.close()
            code = proc.wait()
        if code != 0:
            raise RuntimeError(f"영상 만들기에 실패했어요 (ffmpeg {code})")
    if progress:
        progress(1.0)
    return {"duration": round(total, 2), "slides": len(slides), "narration": voice is not None, "music": bool(music)}


def _compose(t: float, slides: list[Slide], spans: list[tuple[float, float]], frames: list[SlideFrames],
             last_bg: np.ndarray | None) -> np.ndarray:
    if t >= spans[-1][1]:  # end card: blurred last photo, the card itself comes from the subtitles
        return last_bg if last_bg is not None else frames[-1].frame(1.0)
    i = next(k for k, (a, b) in enumerate(spans) if a <= t < b)
    a, b = spans[i]
    cur = frames[i].frame((t - a) / (b - a))
    nxt_start = b - XFADE
    if t >= nxt_start:
        alpha = (t - nxt_start) / XFADE
        if i + 1 < len(frames):
            nxt = frames[i + 1].frame(0.0)
        elif last_bg is not None:
            nxt = last_bg
        else:
            return cur
        return cv2.addWeighted(cur, 1 - alpha, nxt, alpha, 0)
    return cur


def estimate_duration(text: str, base: float = 2.2, per_char: float = 0.11) -> float:
    """Reading time for a caption when there's no narration."""
    return round(min(8.0, max(base, 1.0 + per_char * len(text.replace(" ", "")))), 1)


def ease(x: float) -> float:  # kept for tests / previews
    return x * x * (3 - 2 * x) if 0 <= x <= 1 else float(x > 1)


__all__ = ["Slide", "render_slideshow", "normalize_photo", "load_photo", "decode_audio", "estimate_duration",
           "IMAGE_EXT", "HEIC_SUPPORTED", "math"]
