"""Legend signature caption style: "레전드 마커".

- Clean white bold words with a soft shadow (no hard outline).
- The spoken word gets a brand-coloured highlighter stroke behind it: a slightly
  slanted, slightly tilted parallelogram that wipes in left-to-right in ~110 ms,
  with a darker offset "sticker" shadow. Tilt alternates per word so it feels hand-drawn.
- Keywords are gold. Each caption page rises in from below.
- The title is a rounded translucent card with a brand-colour stripe on the left.

Shapes are ASS vector drawings positioned from text measurements (textmetrics), so
they land exactly behind the words libass renders.
"""

from __future__ import annotations

from collections.abc import Callable

from .textmetrics import em_ratio, text_width

OUT_W = 1080
MAX_LINE_W = 920  # 1080 minus 80 px side margins
TILTS = (-2.0, 1.4, -1.0, 2.2, -1.6, 0.8)
WIPE_MS = 110
RISE_PX = 18


def _hex_bgr(rgb: str) -> str:
    rgb = rgb.lstrip("#")
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&".upper()


def _poly(points: list[tuple[float, float]]) -> str:
    (x0, y0), *rest = points
    return f"m {x0:.0f} {y0:.0f} " + " ".join(f"l {x:.0f} {y:.0f}" for x, y in rest)


def rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> str:
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    return (
        f"m {x0 + r:.0f} {y0:.0f} l {x1 - r:.0f} {y0:.0f} b {x1:.0f} {y0:.0f} {x1:.0f} {y0:.0f} {x1:.0f} {y0 + r:.0f} "
        f"l {x1:.0f} {y1 - r:.0f} b {x1:.0f} {y1:.0f} {x1:.0f} {y1:.0f} {x1 - r:.0f} {y1:.0f} "
        f"l {x0 + r:.0f} {y1:.0f} b {x0:.0f} {y1:.0f} {x0:.0f} {y1:.0f} {x0:.0f} {y1 - r:.0f} "
        f"l {x0:.0f} {y0 + r:.0f} b {x0:.0f} {y0:.0f} {x0:.0f} {y0:.0f} {x0 + r:.0f} {y0:.0f}"
    )


def split_lines(widths: list[float], space: float, max_w: float = MAX_LINE_W) -> list[list[int]]:
    """One line if it fits, else the most balanced two-line split."""
    total = sum(widths) + space * (len(widths) - 1)
    if total <= max_w or len(widths) == 1:
        return [list(range(len(widths)))]
    best, best_k = None, 1
    for k in range(1, len(widths)):
        a = sum(widths[:k]) + space * (k - 1)
        b = sum(widths[k:]) + space * (len(widths) - k - 1)
        score = max(a, b)
        if best is None or score < best:
            best, best_k = score, k
    return [list(range(best_k)), list(range(best_k, len(widths)))]


def _layout(page: list[dict], st, yc: float, text_of, space: float, line_h: float):
    """Measure words, split into 1-2 balanced lines centred on yc, return word boxes."""
    texts = [text_of(w) for w in page]
    widths = [text_width(t, st.font, st.bold, st.size) for t in texts]
    lines = split_lines(widths, space)
    boxes: dict[int, tuple[float, float, float]] = {}  # word -> (x0, x1, line_y)
    line_ys: list[float] = []
    for li, idx in enumerate(lines):
        y = yc + (li - (len(lines) - 1) / 2) * line_h
        line_ys.append(y)
        lw = sum(widths[i] for i in idx) + space * (len(idx) - 1)
        x = OUT_W / 2 - lw / 2
        for i in idx:
            boxes[i] = (x, x + widths[i], y)
            x += widths[i] + space
    return texts, lines, boxes, line_ys


def signature_events(
    pages: list[list[dict]],
    st,
    *,
    duration: float,
    ypos: Callable[[float], int],
    keywords: list[str],
    is_keyword: Callable[[str, list[str]], bool],
    text_of: Callable[[dict], str],
    ass_time: Callable[[float], str],
) -> list[str]:
    events: list[str] = []
    em = st.size * em_ratio(st.font, st.bold)
    space = text_width(" ", st.font, st.bold, st.size)
    line_h = st.size * 0.98
    marker = _hex_bgr(st.word_box_color)
    marker_shadow = "&H000000&"
    gold = _hex_bgr(st.keyword_color) if st.keyword_color else None
    white = _hex_bgr(st.color)

    def emit(layer: int, s: float, e: float, style: str, text: str) -> None:
        if e - s >= 0.01:
            events.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{style},,0,0,0,,{text}")

    for pi, page in enumerate(pages):
        page_start = max(0.0, page[0]["start"])
        next_start = pages[pi + 1][0]["start"] if pi + 1 < len(pages) else duration
        page_end = min(duration, next_start, page[-1]["end"] + 0.6)
        if page_end <= page_start:
            continue

        texts, lines, boxes, line_ys = _layout(page, st, ypos(page_start), text_of, space, line_h)

        def line_text(idx: list[int], cur: int) -> str:
            parts = []
            for i in idx:
                # gold keywords, except on the highlighter where white reads better
                if gold and i != cur and is_keyword(page[i]["text"], keywords):
                    parts.append(f"{{\\c{gold}}}{texts[i]}{{\\c{white}}}")
                else:
                    parts.append(texts[i])
            return " ".join(parts)

        # highlighter behind the spoken word
        for i, w in enumerate(page):
            s = page_start if i == 0 else max(page_start, w["start"])
            e = page_end if i == len(page) - 1 else max(s, page[i + 1]["start"])
            x0, x1, y = boxes[i]
            if i == 0:
                y_anim = f"\\move(0,{RISE_PX},0,0,0,120)"  # follow the page rise
            else:
                y_anim = "\\pos(0,0)"
            pad_x, h = em * 0.2, em * 1.12
            top, bot = y - h * 0.52, y + h * 0.48
            skew = h * 0.16
            shape = [(x0 - pad_x + skew, top), (x1 + pad_x + skew, top), (x1 + pad_x - skew, bot), (x0 - pad_x - skew, bot)]
            tilt = TILTS[(pi * 3 + i) % len(TILTS)]
            cx = (x0 + x1) / 2
            clip0 = f"\\clip({x0 - pad_x - skew - 6:.0f},{top - 40:.0f},{x0 - pad_x - skew - 5:.0f},{bot + 40:.0f})"
            clip1 = f"\\clip({x0 - pad_x - skew - 6:.0f},{top - 40:.0f},{x1 + pad_x + skew + 12:.0f},{bot + 40:.0f})"
            common = f"\\an7{y_anim}\\org({cx:.0f},{y:.0f})\\frz{tilt:g}\\bord0\\shad0\\p1"
            off = max(4, em * 0.07)
            shadow = [(px + off, py + off) for px, py in shape]
            emit(1, s, e, "Caption", f"{{{common}\\c{marker_shadow}\\alpha&H90&{clip0}\\t(0,{WIPE_MS},{clip1})}}{_poly(shadow)}")
            emit(2, s, e, "Caption", f"{{{common}\\c{marker}\\alpha&H00&{clip0}\\t(0,{WIPE_MS},{clip1})}}{_poly(shape)}")
            for li, idx in enumerate(lines):
                ly = line_ys[li]
                anim = (f"\\move({OUT_W // 2},{ly + RISE_PX:.0f},{OUT_W // 2},{ly:.0f},0,120)\\fad(70,0)" if i == 0
                        else f"\\pos({OUT_W // 2},{ly:.0f})")
                emit(3, s, e, "Caption", f"{{\\an5{anim}\\blur{st.blur:g}}}{line_text(idx, i)}")
    return events


def signature_title(title: str, st, *, duration: float, y_top: int, ass_time: Callable[[float], str],
                    escape: Callable[[str], str]) -> list[str]:
    """Rounded translucent title card with a brand stripe, wrapped to at most two balanced lines."""
    words = escape(title).split()
    if not words:
        return []
    size = st.title_size
    widths = [text_width(w, st.font, True, size) for w in words]
    space = text_width(" ", st.font, True, size)
    lines = split_lines(widths, space, max_w=820)
    line_ws = [sum(widths[i] for i in idx) + space * (len(idx) - 1) for idx in lines]
    line_h = size * 1.02
    pad_x, pad_y, stripe = 34, 20, 12
    card_w = max(line_ws) + pad_x * 2 + stripe
    card_h = line_h * len(lines) + pad_y * 2
    x0 = OUT_W / 2 - card_w / 2
    x1, y0, y1 = x0 + card_w, y_top, y_top + card_h
    t0, t1 = ass_time(0), ass_time(duration)
    ev = [
        f"Dialogue: 4,{t0},{t1},Caption,,0,0,0,,{{\\an7\\pos(0,0)\\bord0\\shad0\\p1\\c&H141414&\\alpha&H38&"
        f"\\fad(150,0)}}{rounded_rect(x0, y0, x1, y1, 22)}",
        f"Dialogue: 5,{t0},{t1},Caption,,0,0,0,,{{\\an7\\pos(0,0)\\bord0\\shad0\\p1\\c{_hex_bgr(st.title_box_color)}"
        f"\\fad(150,0)\\clip({x0:.0f},{y0:.0f},{x0 + stripe:.0f},{y1:.0f})}}{rounded_rect(x0, y0, x1, y1, 22)}",
    ]
    text_cx = x0 + stripe + (card_w - stripe) / 2
    for li, idx in enumerate(lines):
        y = y0 + pad_y + line_h * (li + 0.5)
        ev.append(f"Dialogue: 6,{t0},{t1},Caption,,0,0,0,,{{\\an5\\pos({text_cx:.0f},{y:.0f})\\bord0\\shad0\\blur0\\b1"
                  f"\\c{_hex_bgr(st.title_color)}\\fs{size}\\fad(150,0)}}{' '.join(words[i] for i in idx)}")
    return ev


# =========================================================================== "레전드 스티커"
STICKER_TILTS = (-3.5, 2.8, -2.2, 3.6, -3.0, 2.0)
POP = "\\fscx55\\fscy55\\t(0,90,\\fscx114\\fscy114)\\t(90,170,\\fscx100\\fscy100)"


def sticker_events(
    pages: list[list[dict]],
    st,
    *,
    duration: float,
    ypos: Callable[[float], int],
    keywords: list[str],
    is_keyword: Callable[[str, list[str]], bool],
    text_of: Callable[[dict], str],
    ass_time: Callable[[float], str],
) -> list[str]:
    """The spoken word pops onto a tilted white sticker with an offset brand-colour shadow.

    The word is drawn as its own event (so it can scale and rotate with its sticker) while
    the same word in the page line is made transparent, keeping the rest of the line still.
    """
    events: list[str] = []
    em = st.size * em_ratio(st.font, st.bold)
    gap = "  "  # double space: room for the sticker padding without touching the neighbours
    space = text_width(gap, st.font, st.bold, st.size)
    line_h = st.size * 1.02
    fill = _hex_bgr(st.word_box_color)
    accent = _hex_bgr(st.highlight or "FF3D6E")
    ink = "&H141414&"
    kw = _hex_bgr(st.keyword_color) if st.keyword_color else None
    white = _hex_bgr(st.color)

    def emit(layer: int, s: float, e: float, text: str) -> None:
        if e - s >= 0.01:
            events.append(f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},Caption,,0,0,0,,{text}")

    for pi, page in enumerate(pages):
        page_start = max(0.0, page[0]["start"])
        next_start = pages[pi + 1][0]["start"] if pi + 1 < len(pages) else duration
        page_end = min(duration, next_start, page[-1]["end"] + 0.6)
        if page_end <= page_start:
            continue
        texts, lines, boxes, line_ys = _layout(page, st, ypos(page_start), text_of, space, line_h)

        for i, w in enumerate(page):
            s = page_start if i == 0 else max(page_start, w["start"])
            e = page_end if i == len(page) - 1 else max(s, page[i + 1]["start"])
            # the page line, with the spoken word left as a transparent gap
            for li, idx in enumerate(lines):
                parts = []
                for j in idx:
                    if j == i:
                        parts.append(f"{{\\alpha&HFF&}}{texts[j]}{{\\alpha&H00&}}")
                    elif kw and is_keyword(page[j]["text"], keywords):
                        parts.append(f"{{\\c{kw}}}{texts[j]}{{\\c{white}}}")
                    else:
                        parts.append(texts[j])
                fade = "\\fad(80,0)" if i == 0 else ""
                emit(3, s, e, f"{{\\an5\\pos({OUT_W // 2},{line_ys[li]:.0f}){fade}}}" + gap.join(parts))

            # sticker + shadow + the word itself, popping together around the word centre
            x0, x1, y = boxes[i]
            cx = (x0 + x1) / 2
            pad_x, h = em * 0.22, em * 1.28
            wbox = (x1 - x0) + pad_x * 2
            r = h * 0.28
            tilt = STICKER_TILTS[(pi * 3 + i) % len(STICKER_TILTS)]
            sticker = rounded_rect(0, 0, wbox, h, r)
            cy = y - h * 0.02
            off = max(6, em * 0.09)
            common = f"\\an5\\bord0\\shad0\\frz{tilt:g}{POP}"
            emit(4, s, e, f"{{{common}\\pos({cx + off:.0f},{cy + off:.0f})\\c{accent}\\p1}}{sticker}")
            emit(5, s, e, f"{{{common}\\pos({cx:.0f},{cy:.0f})\\c{fill}\\p1}}{sticker}")
            emit(6, s, e, f"{{{common}\\pos({cx:.0f},{y:.0f})\\c{ink}\\blur0}}{texts[i]}")
    return events


def sticker_title(title: str, st, *, duration: float, y_top: int, ass_time: Callable[[float], str],
                  escape: Callable[[str], str]) -> list[str]:
    """Tilted white sticker card with a brand-colour offset shadow."""
    words = escape(title).split()
    if not words:
        return []
    size = st.title_size
    widths = [text_width(w, st.font, True, size) for w in words]
    space = text_width(" ", st.font, True, size)
    lines = split_lines(widths, space, max_w=800)
    line_ws = [sum(widths[i] for i in idx) + space * (len(idx) - 1) for idx in lines]
    line_h = size * 1.02
    card_w, card_h = max(line_ws) + 64, line_h * len(lines) + 36
    cx, cy = OUT_W / 2, y_top + card_h / 2
    t0, t1 = ass_time(0), ass_time(duration)
    card = rounded_rect(0, 0, card_w, card_h, 26)
    rot = "\\frz-2"
    ev = [
        f"Dialogue: 7,{t0},{t1},Caption,,0,0,0,,{{\\an5\\pos({cx + 9:.0f},{cy + 9:.0f})\\org({cx:.0f},{cy:.0f}){rot}"
        f"\\bord0\\shad0\\p1\\c{_hex_bgr(st.title_box_color)}\\fad(150,0)}}{card}",
        f"Dialogue: 8,{t0},{t1},Caption,,0,0,0,,{{\\an5\\pos({cx:.0f},{cy:.0f}){rot}\\bord0\\shad0\\p1"
        f"\\c&HFFFFFF&\\fad(150,0)}}{card}",
    ]
    for li, idx in enumerate(lines):
        y = cy + (li - (len(lines) - 1) / 2) * line_h
        ev.append(f"Dialogue: 9,{t0},{t1},Caption,,0,0,0,,{{\\an5\\pos({cx:.0f},{y:.0f})\\org({cx:.0f},{cy:.0f}){rot}"
                  f"\\bord0\\shad0\\blur0\\b1\\c{_hex_bgr(st.title_color)}\\fs{size}\\fad(150,0)}}"
                  f"{' '.join(words[i] for i in idx)}")
    return ev
