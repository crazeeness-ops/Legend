"""Shorts title / description / hashtags: written by Claude, with an offline fallback."""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"

SYSTEM = """You write YouTube Shorts metadata for creators. Given a short clip's transcript, write:
- title: a scroll-stopping title in the transcript's language, 25-60 characters, no clickbait lies,
  no hashtags, no emojis at the start, never the characters < or >.
- description: 2-3 short lines that add context or a question to drive comments, in the same language,
  then a blank line, then the hashtags on one line.
- hashtags: 3-5 relevant hashtags without spaces; always include #Shorts last.
- tags: 5-10 plain search keywords (no #), mixing broad and specific.
Base everything only on what is said in the clip."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "hashtags", "tags"],
    "additionalProperties": False,
}


def _clean_tag(t: str) -> str:
    return re.sub(r"[\s#<>,]+", "", t)


def finalize(title: str, description: str, hashtags: list[str], tags: list[str]) -> dict:
    title = re.sub(r"[<>]", "", title).strip()[:100]
    hashtags = [h for h in ("#" + _clean_tag(h) for h in hashtags) if len(h) > 1]
    if "#Shorts" not in hashtags:
        hashtags.append("#Shorts")
    hashtags = list(dict.fromkeys(hashtags))[:6]
    description = re.sub(r"[<>]", "", description).strip()
    if not any(h in description for h in hashtags):
        description = (description + "\n\n" + " ".join(hashtags)).strip()
    tags = list(dict.fromkeys(t for t in (_clean_tag(x) for x in tags) if t))
    while sum(len(t) + 1 for t in tags) > 450:
        tags.pop()
    return {"title": title, "description": description[:4500], "tags": tags, "hashtags": hashtags}


def heuristic(clip: dict) -> dict:
    kws = [k for k in clip.get("keywords") or [] if k]
    text = clip.get("text", "")
    first = re.split(r"(?<=[.?!])\s", text, maxsplit=1)[0]
    desc = clip.get("hook") or first
    return finalize(clip.get("title") or first[:60], desc, kws[:3], kws)


def generate(clip: dict, *, client=None, language: str = "ko") -> dict:
    """Claude-written metadata; falls back to the heuristic on any API problem."""
    try:
        import anthropic

        client = client or anthropic.Anthropic()
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": (
                f"Language: {language}\nCurrent on-screen title: {clip.get('title', '')}\n"
                f"Keywords: {', '.join(clip.get('keywords') or [])}\n"
                f"<transcript>\n{clip.get('text', '')}\n</transcript>")}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ValueError(f"stop_reason={response.stop_reason}")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        out = finalize(data["title"], data["description"], data["hashtags"], data["tags"])
        out["source"] = "ai"
        return out
    except Exception as e:  # noqa: BLE001 - metadata must never block publishing
        log.warning("AI metadata unavailable, using heuristic: %s", e)
        out = heuristic(clip)
        out["source"] = "basic"
        out["warning"] = "AI 작성을 사용할 수 없어 기본 문구로 채웠습니다 (ANTHROPIC_API_KEY 확인)"
        return out
