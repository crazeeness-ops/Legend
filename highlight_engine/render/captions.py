"""Animated word-by-word captions as an ASS subtitle file (rendered by libass in ffmpeg)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

OUT_W, OUT_H = 1080, 1920
STRIP_TRAILING = ".,。、"
BREAK_AFTER = (".", "?", "!", "。", "？", "！", ",")


@dataclass(frozen=True)
class CaptionStyle:
    font: str = "Noto Sans CJK KR"
    size: int = 80
    bold: bool = True
    color: str = "FFFFFF"  # RGB hex
    highlight: str | None = "FFE600"  # current word colour (None = no karaoke)
    highlight_scale: int = 100  # % scale on the current word
    keyword_color: str | None = "00E5FF"
    outline: float = 6
    outline_color: str = "000000"
    shadow: float = 2
    box: bool = False  # opaque box behind the line
    box_color: str = "000000"
    box_alpha: int = 0x30  # 0 = opaque, 255 = transparent
    max_chars: int = 14  # per caption page, spaces excluded
    max_words: int = 4
    pop_in: bool = True
    title_size: int = 62
    title_color: str = "FFFFFF"
    title_box_color: str = "E8364A"


PRESETS: dict[str, CaptionStyle] = {
    "basic": CaptionStyle(),
    "bold": CaptionStyle(size=96, color="FFE600", highlight="FFFFFF", highlight_scale=112, outline=8, max_chars=10, max_words=3),
    "box": CaptionStyle(size=72, box=True, outline=14, outline_color="111111", shadow=0, highlight="38E1A0"),
    "emphasis": CaptionStyle(size=84, highlight="7CFF4F", highlight_scale=118, keyword_color="FF4FA3"),
    "minimal": CaptionStyle(size=60, bold=False, highlight=None, keyword_color=None, outline=3, shadow=0, max_chars=18, max_words=6, pop_in=False),
}


def ass_color(rgb: str, alpha: int = 0) -> str:
    rgb = rgb.lstrip("#")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def clean_word(text: str) -> str:
    return text.strip().rstrip(STRIP_TRAILING)


def paginate(words: list[dict], max_chars: int, max_words: int, max_gap: float = 0.7) -> list[list[dict]]:
    """Group words into short caption pages that read at a glance."""
    pages: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for w in words:
        text = clean_word(w["text"])
        if not text:
            continue
        n = len(text.replace(" ", ""))
        if cur and (
            chars + n > max_chars
            or len(cur) >= max_words
            or w["start"] - cur[-1]["end"] > max_gap
            or cur[-1]["raw"].rstrip().endswith(BREAK_AFTER)
        ):
            pages.append(cur)
            cur, chars = [], 0
        cur.append({"text": text, "raw": w["text"], "start": w["start"], "end": w["end"]})
        chars += n
    if cur:
        pages.append(cur)
    return pages


def _is_keyword(word: str, keywords: list[str]) -> bool:
    w = word.lower()
    return any(k and (k.lower() in w or (len(w) >= 2 and w in k.lower())) for k in keywords)


def _styles(st: CaptionStyle) -> str:
    border_style = 3 if st.box else 1
    back = ass_color(st.box_color, st.box_alpha) if st.box else ass_color("000000", 0x80)
    outline_col = ass_color(st.box_color, st.box_alpha) if st.box else ass_color(st.outline_color)
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
           "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding")
    cap = (f"Style: Caption,{st.font},{st.size},{ass_color(st.color)},{ass_color(st.color)},{outline_col},{back},"
           f"{-1 if st.bold else 0},0,0,0,100,100,0,0,{border_style},{st.outline},{st.shadow},5,80,80,0,1")
    title = (f"Style: Title,{st.font},{st.title_size},{ass_color(st.title_color)},{ass_color(st.title_color)},"
             f"{ass_color(st.title_box_color)},{ass_color(st.title_box_color)},-1,0,0,0,100,100,0,0,3,16,0,8,90,90,0,1")
    return f"[V4+ Styles]\n{fmt}\n{cap}\n{title}\n"


def build_ass(
    words: list[dict],
    *,
    clip_start: float,
    duration: float,
    style: CaptionStyle | str = "basic",
    caption_y: int | Callable[[float], int] = int(OUT_H * 0.70),
    title: str | None = None,
    title_y: int = 230,
    keywords: list[str] | None = None,
) -> str:
    st = PRESETS[style] if isinstance(style, str) else style
    keywords = keywords or []
    rel = [
        {**w, "start": w["start"] - clip_start, "end": w["end"] - clip_start}
        for w in words
        if w["end"] > clip_start and w["start"] < clip_start + duration
    ]
    pages = paginate(rel, st.max_chars, st.max_words)

    events: list[str] = []

    def pos(t: float) -> str:
        y = caption_y(t) if callable(caption_y) else caption_y
        return f"\\an5\\pos({OUT_W // 2},{y})"

    def word_markup(w: dict, current: bool) -> str:
        text = escape(w["text"])
        tags = []
        if current and st.highlight:
            tags.append(f"\\c{ass_color(st.highlight)}")
            if st.highlight_scale != 100:
                tags.append(f"\\fscx{st.highlight_scale}\\fscy{st.highlight_scale}")
        elif st.keyword_color and _is_keyword(w["text"], keywords):
            tags.append(f"\\c{ass_color(st.keyword_color)}")
        if not tags:
            return text
        return "{" + "".join(tags) + "}" + text + "{\\r}"

    for pi, page in enumerate(pages):
        page_start = max(0.0, page[0]["start"])
        next_start = pages[pi + 1][0]["start"] if pi + 1 < len(pages) else duration
        page_end = min(duration, next_start, page[-1]["end"] + 0.6)
        if page_end <= page_start:
            continue
        if st.highlight:
            spans = []
            for i, w in enumerate(page):
                s = page_start if i == 0 else max(page_start, w["start"])
                e = page_end if i == len(page) - 1 else max(s, page[i + 1]["start"])
                spans.append((s, e, i))
        else:
            spans = [(page_start, page_end, -1)]
        for k, (s, e, cur) in enumerate(spans):
            if e - s < 0.01:
                continue
            anim = ""
            if st.pop_in and k == 0:
                anim = "\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)"
            body = " ".join(word_markup(w, i == cur) for i, w in enumerate(page))
            events.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Caption,,0,0,0,,{{{pos(s)}{anim}}}{body}")

    if title:
        events.insert(0, f"Dialogue: 2,{ass_time(0)},{ass_time(duration)},Title,,0,0,0,,"
                         f"{{\\an8\\pos({OUT_W // 2},{title_y})}}{escape(title)}")

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {OUT_W}\nPlayResY: {OUT_H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
    )
    return (header + _styles(st) + "\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            + "\n".join(events) + "\n")


def with_brand(style: CaptionStyle | str, **overrides) -> CaptionStyle:
    """Apply brand-kit overrides (font, colours) on top of a preset."""
    base = PRESETS[style] if isinstance(style, str) else style
    return replace(base, **{k: v for k, v in overrides.items() if v is not None})
