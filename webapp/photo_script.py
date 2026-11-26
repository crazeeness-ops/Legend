"""Templates and scripts for photo shorts.

The script is written by Claude from the photos themselves (vision) plus the details the user typed;
without an API key a template script is filled in from those details.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import cv2

from highlight_engine.photos import estimate_duration, load_photo

log = logging.getLogger(__name__)
MODEL = "claude-opus-5-5"

TEMPLATES: dict[str, dict] = {
    "restaurant": {
        "label": "맛집·카페 소개", "icon": "🍰", "preset": "legend_sticker",
        "fields": [("name", "가게 이름", "크로플하우스 성수점"), ("menu", "대표 메뉴·가격", "버터 크로플 6,500원"),
                   ("place", "위치", "서울 성동구 연무장길 12"), ("hours", "영업시간", "매일 11:00 - 21:00"),
                   ("point", "자랑할 점", "겉바속촉, 주말엔 웨이팅")],
        "tone": "친근하고 군침 돌게, 직접 가본 사람이 추천하듯. 메뉴의 식감·맛·분위기를 구체적으로.",
        "card": ("name", "menu", "place", "hours"),
        "fallback": ["{name}, 여기 진짜예요", "{point}", "대표 메뉴는 {menu}", "분위기까지 완벽", "{place}에서 만나요"],
    },
    "product": {
        "label": "상품 홍보", "icon": "🛍️", "preset": "bold",
        "fields": [("name", "상품명", "무선 미니 가습기"), ("price", "가격·혜택", "29,000원 · 오늘만 무료배송"),
                   ("point", "핵심 장점", "조용하고 12시간 지속"), ("where", "구매처", "스마트스토어 '레전드샵'")],
        "tone": "짧고 힘 있게, 문제→해결→혜택 순서. 과장 광고나 사실이 아닌 효능 주장 금지.",
        "card": ("name", "price", "where"),
        "fallback": ["이거 하나면 해결돼요", "{name}", "{point}", "{price}", "{where}에서 확인하세요"],
    },
    "travel": {
        "label": "여행·일상 브이로그", "icon": "✈️", "preset": "legend",
        "fields": [("place", "장소", "제주 애월"), ("when", "언제·누구와", "2박 3일 친구랑"),
                   ("feeling", "한 줄 소감", "바다 보다가 하루가 다 갔어요")],
        "tone": "감성적이고 1인칭 브이로그 말투. 장면의 빛·소리·기분을 짧게.",
        "card": ("place", "when"),
        "fallback": ["{place}, {when}", "{feeling}", "여기는 꼭 가보세요", "다음 여행지는 어디로 갈까요?"],
    },
    "realestate": {
        "label": "매물·인테리어 소개", "icon": "🏠", "preset": "news",
        "fields": [("area", "지역·단지", "마포구 공덕동 ○○아파트"), ("size", "면적·구조", "84㎡ · 방 3 욕실 2"),
                   ("price", "가격·조건", "전세 6억 (협의 가능)"), ("point", "특징", "올수리, 남향, 역세권 5분"),
                   ("contact", "문의처", "레전드공인중개사 02-000-0000")],
        "tone": "신뢰감 있고 정보 위주. 공간마다 장점을 사실대로. 과장·허위 표현 금지(표시광고법).",
        "card": ("area", "size", "price", "contact"),
        "fallback": ["{area} 매물 소개", "{size}", "{point}", "{price}", "문의: {contact}"],
    },
    "free": {
        "label": "자유 주제", "icon": "✨", "preset": "legend",
        "fields": [("topic", "주제", "우리 집 강아지의 하루"), ("message", "전하고 싶은 말", "")],
        "tone": "주제에 맞는 자연스러운 말투. 첫 문장은 궁금증을 일으키게.",
        "card": (),
        "fallback": ["{topic}", "{message}"],
    },
}

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "slides": {"type": "array", "items": {
            "type": "object",
            "properties": {"photo": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["photo", "text"], "additionalProperties": False}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "slides", "keywords", "hashtags"],
    "additionalProperties": False,
}

SYSTEM = """You write scripts for vertical short videos (YouTube Shorts / Reels) made from a user's photos.
You will see the photos numbered from 0. Write in Korean unless told otherwise.

Rules:
- Pick the photo order that tells the best story; the FIRST slide must hook viewers in the first second
  (a question, a surprising fact, a strong claim that the photos support).
- One short caption per slide: 6-22 Korean characters, spoken style, no hashtags, no emojis.
- Describe only what is visible or what the user told you. Never invent prices, places, awards or health claims.
- Use every photo at most once; you may skip blurry or off-topic photos.
- The last slide should end with a natural call to action (visit, comment, follow) fitting the template.
- title: a scroll-stopping YouTube title (max 40 chars). keywords: 1-3 words from the captions to highlight.
- hashtags: 3-5 relevant hashtags (with #)."""


def card_for(template: str, fields: dict) -> dict | None:
    keys = TEMPLATES[template]["card"]
    vals = [str(fields.get(k, "")).strip() for k in keys]
    vals = [v for v in vals if v]
    if not vals:
        return None
    return {"title": vals[0], **{f"line{i}": v for i, v in enumerate(vals[1:4], 1)}}


def fallback_script(template: str, fields: dict, n_photos: int) -> dict:
    """Template captions filled with the user's details (no AI)."""
    t = TEMPLATES[template]
    lines = []
    for pattern in t["fallback"]:
        try:
            line = pattern.format(**{k: str(fields.get(k, "")).strip() for k, _, _ in t["fields"]})
        except (KeyError, IndexError):
            continue
        if line.strip(" ,·") and "{}" not in line and not line.startswith(","):
            lines.append(line.strip())
    slides = [{"photo": i, "text": lines[i] if i < len(lines) else ""} for i in range(n_photos)]
    first = str(fields.get(t["fields"][0][0], "")).strip()
    return {"title": first or t["label"], "slides": slides, "keywords": [first] if first else [],
            "hashtags": ["#Shorts"], "source": "template"}


def _image_block(path: str) -> dict:
    img = load_photo(path, max_side=768)  # plenty for understanding the scene, keeps tokens low
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.tobytes()).decode()}}


def ai_script(template: str, fields: dict, photos: list[str], notes: str = "", *, client=None) -> dict:
    """Claude looks at the photos and writes the script; falls back to the template on any API problem."""
    t = TEMPLATES[template]
    try:
        import anthropic

        client = client or anthropic.Anthropic()
        details = "\n".join(f"- {label}: {fields.get(k, '')}" for k, label, _ in t["fields"] if str(fields.get(k, "")).strip())
        content: list[dict] = []
        for i, p in enumerate(photos):
            content.append({"type": "text", "text": f"사진 {i}"})
            content.append(_image_block(p))
        content.append({"type": "text", "text": (
            f"템플릿: {t['label']}\n말투: {t['tone']}\n사용자가 알려준 정보:\n{details or '(없음)'}\n"
            f"추가 요청: {notes or '(없음)'}\n사진은 {len(photos)}장이에요. 숏츠 대본을 써 주세요.")})
        response = client.beta.messages.create(
            model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM, messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"stop_reason={response.stop_reason}")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        seen, slides = set(), []
        for s in data["slides"]:
            i = int(s["photo"])
            if 0 <= i < len(photos) and i not in seen:
                seen.add(i)
                slides.append({"photo": i, "text": s["text"].strip()[:60]})
        if not slides:
            raise ValueError("empty script")
        return {"title": data["title"].strip()[:60], "slides": slides, "keywords": data["keywords"][:3],
                "hashtags": [h if h.startswith("#") else "#" + h for h in data["hashtags"]][:5], "source": "ai"}
    except Exception as e:  # noqa: BLE001 - never block the user on the AI
        log.warning("AI photo script unavailable, using template: %s", e)
        out = fallback_script(template, fields, len(photos))
        out["warning"] = "AI 대본을 쓸 수 없어 템플릿 문구로 채웠어요 (ANTHROPIC_API_KEY 확인)"
        return out


def with_durations(script: dict) -> dict:
    for s in script["slides"]:
        s.setdefault("duration", estimate_duration(s["text"]))
    return script


def template_catalog() -> list[dict]:
    return [{"id": k, "label": v["label"], "icon": v["icon"], "preset": v["preset"],
             "fields": [{"key": a, "label": b, "placeholder": c} for a, b, c in v["fields"]]} for k, v in TEMPLATES.items()]


__all__ = ["TEMPLATES", "ai_script", "fallback_script", "card_for", "with_durations", "template_catalog", "Path"]
