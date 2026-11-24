"""Measure caption text the way libass lays it out, so we can draw shapes behind exact words.

libass sizes a font so that ascender + descender equals the ASS font size; we
open the same font file (resolved through fontconfig, like libass) with Pillow
at the equivalent em size and measure advances.
"""

from __future__ import annotations

import subprocess
import unicodedata
from functools import lru_cache

try:
    from PIL import ImageFont
except ImportError:  # pragma: no cover
    ImageFont = None


@lru_cache(maxsize=32)
def font_file(family: str, bold: bool) -> tuple[str, int] | None:
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}|%{index}", f"{family}:{'bold' if bold else 'regular'}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        path, _, index = out.partition("|")
        return (path, int(index or 0)) if path else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@lru_cache(maxsize=64)
def _font(path: str, index: int, em_px: int):
    return ImageFont.truetype(path, em_px, index=index)


@lru_cache(maxsize=32)
def em_ratio(family: str, bold: bool) -> float:
    """em size / ASS size for this font (≈0.69 for Noto Sans CJK)."""
    ff = font_file(family, bold)
    if ff and ImageFont:
        try:
            asc, desc = _font(ff[0], ff[1], 1000).getmetrics()
            return 1000 / (asc + desc)
        except OSError:
            pass
    return 1 / 1.45


def _approx_width(text: str, em: float) -> float:
    w = 0.0
    for ch in text:
        if ch == " ":
            w += 0.23
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 0.92
        else:
            w += 0.58
    return w * em


def text_width(text: str, family: str, bold: bool, ass_size: float) -> float:
    """Advance width in script pixels of ``text`` rendered at ASS font size ``ass_size``."""
    em = ass_size * em_ratio(family, bold)
    ff = font_file(family, bold)
    if ff and ImageFont:
        try:
            return _font(ff[0], ff[1], 200).getlength(text) * em / 200
        except OSError:
            pass
    return _approx_width(text, em)
