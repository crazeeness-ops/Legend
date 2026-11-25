"""Animated captions as an ASS subtitle file (rendered by libass inside ffmpeg).

A caption look = a ``CaptionStyle``: one of the built-in ``PRESETS``, optionally
overridden with brand settings (font, colours, size, position) or loaded from a
JSON style file. Animation modes:

- karaoke: page of words, the spoken word changes colour (and can scale up)
- box:     a coloured box sits behind the spoken word
- reveal:  words appear one by one as they are spoken (typewriter)
- single:  one word at a time, big, popping in
- sweep:   colour fills each word left-to-right while it is spoken
- static:  plain page of words, no per-word effect
- signature: Legend's own look — a hand-drawn highlighter wipes in behind the
             spoken word (see ``signature.py``)
- sticker:   Legend's second signature — the spoken word pops onto a tilted white sticker
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
STRIP_TRAILING = ".,。、"
BREAK_AFTER = (".", "?", "!", "。", "？", "！", ",")
MODES = ("signature", "sticker", "karaoke", "box", "reveal", "single", "sweep", "static")
# a Korean-capable font that ships with each OS (override with LEGEND_CAPTION_FONT)
DEFAULT_FONT = os.environ.get("LEGEND_CAPTION_FONT") or (
    "Malgun Gothic" if sys.platform == "win32" else "Apple SD Gothic Neo" if sys.platform == "darwin" else "Noto Sans CJK KR")
POSITIONS = {"top": 560, "middle": OUT_H // 2, "bottom": 1560}  # "auto" follows the video layout


@dataclass(frozen=True)
class CaptionStyle:
    label: str = "기본"
    description: str = ""
    mode: str = "karaoke"
    font: str = DEFAULT_FONT
    size: int = 80
    bold: bool = True
    uppercase: bool = False
    color: str = "FFFFFF"  # RGB hex
    highlight: str | None = "FFE600"  # spoken-word colour (karaoke/reveal) or fill colour (sweep)
    highlight_scale: int = 100  # % scale on the spoken word
    word_box_color: str = "E8364A"  # box mode
    keyword_color: str | None = "00E5FF"
    outline: float = 6
    outline_color: str = "000000"
    shadow: float = 2
    blur: float = 0  # >0 softens the outline into a glow
    box: bool = False  # translucent box behind the whole line
    box_color: str = "000000"
    box_alpha: int = 0x30  # 0 = opaque, 255 = transparent
    max_chars: int = 14  # per caption page, spaces excluded
    max_words: int = 4
    pop_in: bool = True
    position: str = "auto"  # auto | top | middle | bottom
    title_size: int = 62
    title_color: str = "FFFFFF"
    title_box_color: str = "E8364A"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown caption mode {self.mode!r}; use one of {MODES}")
        if self.position != "auto" and self.position not in POSITIONS:
            raise ValueError(f"unknown caption position {self.position!r}")
        for name in ("color", "word_box_color", "outline_color", "box_color", "title_color", "title_box_color",
                     "highlight", "keyword_color"):
            value = getattr(self, name)
            if value is not None and not _is_hex(value):
                raise ValueError(f"{name} must be an RGB hex colour like 'FFE600', got {value!r}")


def _is_hex(v: str) -> bool:
    v = v.lstrip("#")
    return len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v)


PRESETS: dict[str, CaptionStyle] = {
    "legend": CaptionStyle(label="레전드 마커", description="시그니처 · 말하는 단어 뒤로 형광펜이 손으로 긋듯 칠해짐",
                           mode="signature", size=86, color="FFFFFF", highlight=None, word_box_color="FF3D6E",
                           keyword_color="FFC94D", outline=2.5, outline_color="101010", shadow=3, blur=1.2,
                           max_chars=13, max_words=4, pop_in=False, title_size=56, title_box_color="FF3D6E"),
    "legend_sticker": CaptionStyle(label="레전드 스티커", description="시그니처 · 말하는 단어가 기울어진 흰 스티커로 톡 튀어나옴",
                                   mode="sticker", size=86, color="FFFFFF", highlight="FF3D6E", word_box_color="FFFFFF",
                                   keyword_color="FF5C8A", outline=6, outline_color="000000", shadow=0, blur=0,
                                   max_chars=13, max_words=4, pop_in=False, title_size=56, title_color="141414",
                                   title_box_color="FF3D6E"),
    "basic": CaptionStyle(label="기본", description="흰 글씨 + 검은 외곽선, 말하는 단어는 노란색"),
    "bold": CaptionStyle(label="굵은 노랑", description="크고 굵은 노란 글씨, 말하는 단어는 흰색으로 커짐",
                         size=96, color="FFE600", highlight="FFFFFF", highlight_scale=112, outline=8,
                         max_chars=10, max_words=3),
    "box": CaptionStyle(label="반투명 박스", description="반투명 검은 배경 박스, 말하는 단어는 민트색",
                        size=72, box=True, outline=14, outline_color="111111", shadow=0, highlight="38E1A0"),
    "highlight_box": CaptionStyle(label="단어 박스", description="말하는 단어 뒤에 빨간 박스가 따라다님",
                                  mode="box", size=82, highlight=None, word_box_color="E8364A", shadow=0),
    "one_word": CaptionStyle(label="한 단어씩", description="한 단어씩 화면 가운데에 크게 팝",
                             mode="single", size=124, outline=9, max_words=1, highlight=None,
                             keyword_color="FFE600"),
    "reveal": CaptionStyle(label="타자기", description="말하는 대로 단어가 하나씩 나타남",
                           mode="reveal", size=80, highlight=None, max_chars=16, max_words=5, pop_in=False),
    "karaoke": CaptionStyle(label="노래방", description="말하는 동안 왼쪽부터 노란색으로 채워짐",
                            mode="sweep", size=84, keyword_color=None, pop_in=False),
    "emphasis": CaptionStyle(label="키워드 강조", description="핵심 키워드는 핑크, 말하는 단어는 초록색으로 팝",
                             size=84, highlight="7CFF4F", highlight_scale=118, keyword_color="FF4FA3"),
    "neon": CaptionStyle(label="네온", description="하늘색 네온 글로우",
                         size=84, outline=7, outline_color="00C8FF", blur=5, shadow=0,
                         highlight="FFFFFF", highlight_scale=110, color="D9F6FF", keyword_color="FF6AD5"),
    "news": CaptionStyle(label="방송 자막", description="화면 아래 반투명 바, 차분한 뉴스 스타일",
                         mode="static", size=58, box=True, box_alpha=0x50, outline=18, shadow=0,
                         highlight=None, keyword_color=None, max_chars=22, max_words=8, pop_in=False,
                         position="bottom"),
    "minimal": CaptionStyle(label="미니멀", description="작은 흰 글씨, 효과 없음",
                            mode="static", size=60, bold=False, highlight=None, keyword_color=None,
                            outline=3, shadow=0, max_chars=18, max_words=6, pop_in=False),
}


# ------------------------------------------------------------------ style resolution

def load_style_file(path: str | Path) -> CaptionStyle:
    """Load a JSON style: {"base": "bold", "font": "Pretendard", "highlight": "FF4FA3", ...}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    base = data.pop("base", "basic")
    if base not in PRESETS:
        raise ValueError(f"unknown base preset {base!r}")
    return with_brand(base, **data)


def with_brand(style: CaptionStyle | str, **overrides) -> CaptionStyle:
    """Apply overrides (font, colours, size, position...) on top of a preset."""
    base = PRESETS[style] if isinstance(style, str) else style
    valid = {f.name for f in fields(CaptionStyle)}
    unknown = set(overrides) - valid
    if unknown:
        raise ValueError(f"unknown caption style field(s): {', '.join(sorted(unknown))}")
    return replace(base, **{k: v for k, v in overrides.items() if v is not None})


def preset_catalog() -> list[dict]:
    """Preset list for UIs (style picker)."""
    return [{"id": k, **asdict(v)} for k, v in PRESETS.items()]


# ------------------------------------------------------------------ ASS helpers

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
    primary = ass_color(st.highlight if st.mode == "sweep" and st.highlight else st.color)
    secondary = ass_color(st.color)  # sweep: colour before the fill reaches it
    bold = -1 if st.bold else 0
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
           "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding")
    cap = (f"Style: Caption,{st.font},{st.size},{primary},{secondary},{outline_col},{back},"
           f"{bold},0,0,0,100,100,0,0,{border_style},{st.outline},{st.shadow},5,80,80,0,1")
    pad = max(8, int(st.size * 0.14))
    wbox = (f"Style: WordBox,{st.font},{st.size},{ass_color(st.color, 0xFF)},{ass_color(st.color, 0xFF)},"
            f"{ass_color(st.word_box_color)},{ass_color(st.word_box_color)},{bold},0,0,0,100,100,0,0,3,{pad},0,5,80,80,0,1")
    title = (f"Style: Title,{st.font},{st.title_size},{ass_color(st.title_color)},{ass_color(st.title_color)},"
             f"{ass_color(st.title_box_color)},{ass_color(st.title_box_color)},-1,0,0,0,100,100,0,0,3,16,0,8,90,90,0,1")
    return f"[V4+ Styles]\n{fmt}\n{cap}\n{wbox}\n{title}\n"


# ------------------------------------------------------------------ builder

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
    max_words = 1 if st.mode == "single" else st.max_words
    pages = paginate(rel, st.max_chars, max_words)

    def ypos(t: float) -> int:
        if st.position != "auto":
            return POSITIONS[st.position]
        return caption_y(t) if callable(caption_y) else caption_y

    def prefix(t: float, first: bool) -> str:
        tags = f"\\an5\\pos({OUT_W // 2},{ypos(t)})"
        if st.blur:
            tags += f"\\blur{st.blur:g}"
        if st.pop_in and first:
            tags += "\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)"
        return tags

    def text_of(w: dict) -> str:
        return escape(w["text"].upper() if st.uppercase else w["text"])

    def markup(w: dict, current: bool, hidden: bool = False) -> str:
        """One word with its override tags for the karaoke/box/reveal/static family."""
        tags = []
        if hidden:
            tags.append("\\alpha&HFF&")
        elif current and st.highlight and st.mode in ("karaoke", "reveal"):
            tags.append(f"\\c{ass_color(st.highlight)}")
        elif st.keyword_color and _is_keyword(w["text"], keywords):
            tags.append(f"\\c{ass_color(st.keyword_color)}")
        if current and st.highlight_scale != 100:
            tags.append(f"\\fscx{st.highlight_scale}\\fscy{st.highlight_scale}")
        if not tags:
            return text_of(w)
        return "{" + "".join(tags) + "}" + text_of(w) + "{\\r}"

    def box_markup(w: dict, current: bool) -> str:
        """Box layer: same layout as the text, glyphs invisible, box only behind the spoken word."""
        scale = f"\\fscx{st.highlight_scale}\\fscy{st.highlight_scale}" if current and st.highlight_scale != 100 else ""
        alpha = "\\3a&H00&\\4a&H00&" if current else "\\3a&HFF&\\4a&HFF&"
        return "{" + alpha + scale + "}" + text_of(w)

    events: list[str] = []

    def emit(layer: int, s: float, e: float, style_name: str, text: str) -> None:
        if e - s >= 0.01:
            events.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{style_name},,0,0,0,,{text}")

    for pi, page in enumerate(pages):
        page_start = max(0.0, page[0]["start"])
        next_start = pages[pi + 1][0]["start"] if pi + 1 < len(pages) else duration
        page_end = min(duration, next_start, page[-1]["end"] + 0.6)
        if page_end <= page_start:
            continue

        if st.mode in ("signature", "sticker"):
            break  # laid out separately below (needs text measurement across the whole page)

        if st.mode in ("static", "single"):
            body = " ".join(markup(w, False) for w in page)
            emit(1, page_start, page_end, "Caption", "{" + prefix(page_start, True) + "}" + body)
            continue

        if st.mode == "sweep":
            parts = []
            for i, w in enumerate(page):
                end = page[i + 1]["start"] if i + 1 < len(page) else w["end"]
                cs = max(1, int(round((end - w["start"]) * 100)))
                parts.append(f"{{\\kf{cs}}}{text_of(w)}")
            emit(1, page_start, page_end, "Caption", "{" + prefix(page_start, True) + "}" + " ".join(parts))
            continue

        # per-word events: karaoke, box, reveal
        for i, w in enumerate(page):
            s = page_start if i == 0 else max(page_start, w["start"])
            e = page_end if i == len(page) - 1 else max(s, page[i + 1]["start"])
            pre = "{" + prefix(s, i == 0) + "}"
            hidden = (lambda j: j > i) if st.mode == "reveal" else (lambda j: False)
            emit(1, s, e, "Caption", pre + " ".join(markup(x, j == i, hidden(j)) for j, x in enumerate(page)))
            if st.mode == "box":
                emit(0, s, e, "WordBox", pre + " ".join(box_markup(x, j == i) for j, x in enumerate(page)))

    if st.mode in ("signature", "sticker"):
        from .signature import signature_events, signature_title, sticker_events, sticker_title

        build, build_title = ((signature_events, signature_title) if st.mode == "signature"
                              else (sticker_events, sticker_title))
        events = build(pages, st, duration=duration, ypos=ypos, keywords=keywords,
                       is_keyword=_is_keyword, text_of=text_of, ass_time=ass_time)
        if title:
            events = build_title(title, st, duration=duration, y_top=title_y - 10, ass_time=ass_time,
                                 escape=escape) + events
        title = None

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
