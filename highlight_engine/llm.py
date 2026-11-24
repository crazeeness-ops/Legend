"""Claude-based highlight selection.

The transcript is sent as numbered sentences. Claude answers with sentence-id
ranges (never raw timestamps), so every clip boundary is guaranteed to land on
a sentence boundary that post-processing can then fine-tune.
"""

from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor

import anthropic

from .models import Candidate, Sentence

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
SCORE_KEYS = ("hook", "completeness", "emotion", "value", "trend")

SYSTEM_PROMPT = """You are a senior short-form video editor who turns long YouTube videos \
(podcasts, interviews, lectures, talk shows) into viral vertical shorts for YouTube Shorts, \
TikTok and Instagram Reels.

You receive a transcript as numbered sentences:
[S<id>] <start>-<end> <speaker> | <text>

Pick the segments that work as standalone shorts. A great clip:
1. Hooks in the first 3 seconds: a bold claim, a question, a surprising number, conflict, \
or an emotional moment. The first sentence must make a scroller stop.
2. Is self-contained: a viewer with zero context understands it. Avoid clips that depend on \
earlier context ("as I said before", unexplained "that", "he") unless the clip itself resolves it.
3. Has a complete arc and a satisfying ending: a punchline, conclusion, answer or insight. \
Never end mid-thought.
4. Delivers clear value or emotion: a practical tip, a strong opinion, a funny story, a \
reveal, a relatable struggle.

Rules:
- start_sentence and end_sentence are sentence ids from the transcript (inclusive range).
- The clip duration (end time of end_sentence minus start time of start_sentence) must fall \
inside the requested duration range. Check the timestamps.
- Clips must not overlap each other.
- Write title and hook in the same language as the transcript. title: a short, scroll-stopping \
on-screen title (max ~40 characters, no hashtags). hook: the opening line or idea that grabs attention.
- keywords: 1-3 words from the clip to emphasize in animated captions.
- Score each clip honestly on 0-10 integers; do not inflate. 5 = average, 8+ = genuinely \
strong. hook = strength of the first 3 seconds; completeness = standalone understandability \
and clean ending; emotion = energy, humor, tension; value = usefulness or insight; \
trend = fit with broadly popular short-form topics.
- Quality over quantity: return fewer clips rather than weak ones."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sentence": {"type": "integer"},
                    "end_sentence": {"type": "integer"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "reason": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    **{f"{k}_score": {"type": "integer"} for k in SCORE_KEYS},
                },
                "required": [
                    "start_sentence", "end_sentence", "title", "hook", "reason", "keywords",
                    *[f"{k}_score" for k in SCORE_KEYS],
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def _ts(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:04.1f}"


def format_transcript(sentences: list[Sentence]) -> str:
    return "\n".join(
        f"[S{s.id}] {_ts(s.start)}-{_ts(s.end)} {s.speaker or 'SPK'} | {s.text}" for s in sentences
    )


def chunk_sentences(
    sentences: list[Sentence], chunk_seconds: float = 1200.0, overlap_seconds: float = 90.0
) -> list[list[Sentence]]:
    """Split long transcripts into overlapping windows so each call stays focused."""
    if not sentences:
        return []
    chunks: list[list[Sentence]] = []
    i = 0
    while i < len(sentences):
        start_t = sentences[i].start
        j = i
        while j < len(sentences) and sentences[j].end - start_t <= chunk_seconds:
            j += 1
        j = max(j, i + 1)
        chunks.append(sentences[i:j])
        if j >= len(sentences):
            break
        # next chunk starts overlap_seconds before this chunk's end
        back_t = sentences[j - 1].end - overlap_seconds
        k = j
        while k > i + 1 and sentences[k - 1].start > back_t:
            k -= 1
        i = k
    return chunks


class ClaudeSelector:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_workers: int = 4,
        chunk_seconds: float = 1200.0,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_workers = max_workers
        self.chunk_seconds = chunk_seconds

    def select(
        self,
        sentences: list[Sentence],
        *,
        min_len: float,
        max_len: float,
        target_count: int,
        language: str = "ko",
        user_prompt: str | None = None,
    ) -> list[Candidate]:
        chunks = chunk_sentences(sentences, self.chunk_seconds)
        if not chunks:
            return []
        total = max(sentences[-1].end - sentences[0].start, 1.0)

        def run(chunk: list[Sentence]) -> list[Candidate]:
            span = chunk[-1].end - chunk[0].start
            n = min(15, math.ceil(target_count * 1.5 * span / total) + 2)
            return self._call(chunk, n, min_len, max_len, language, user_prompt)

        results: list[Candidate] = []
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for fut in [pool.submit(run, c) for c in chunks]:
                try:
                    results.extend(fut.result())
                except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as e:
                    log.warning("chunk failed: %s", e)
                    errors.append(e)
        if errors and len(errors) == len(chunks):
            raise errors[0]
        return results

    def _call(
        self,
        chunk: list[Sentence],
        n: int,
        min_len: float,
        max_len: float,
        language: str,
        user_prompt: str | None,
    ) -> list[Candidate]:
        request = (
            f"Transcript language: {language}\n"
            f"Requested clip duration: {min_len:.0f}-{max_len:.0f} seconds\n"
            f"Return up to {n} clips, best first.\n"
        )
        if user_prompt:
            request += (
                "The creator asked specifically for: "
                f"<creator_request>{user_prompt}</creator_request>\n"
                "Only return clips that match this request.\n"
            )
        request += f"\n<transcript>\n{format_transcript(chunk)}\n</transcript>"

        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": request}],
        )

        if response.stop_reason == "refusal":
            log.warning("model declined chunk S%d-S%d", chunk[0].id, chunk[-1].id)
            return []
        if response.stop_reason == "max_tokens":
            raise ValueError("response truncated (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ValueError("no text block in response")
        return parse_candidates(json.loads(text), chunk)


def parse_candidates(data: dict, chunk: list[Sentence]) -> list[Candidate]:
    """Validate model output against the chunk's sentence ids."""
    lo, hi = chunk[0].id, chunk[-1].id
    out: list[Candidate] = []
    for c in data.get("clips", []):
        s, e = int(c["start_sentence"]), int(c["end_sentence"])
        if s > e:
            s, e = e, s
        if s < lo or e > hi:
            log.debug("dropping out-of-range clip S%d-S%d", s, e)
            continue
        out.append(
            Candidate(
                start_sid=s,
                end_sid=e,
                title=c["title"].strip(),
                hook=c["hook"].strip(),
                reason=c["reason"].strip(),
                scores={k: float(min(10, max(0, c[f"{k}_score"]))) for k in SCORE_KEYS},
                keywords=[k for k in c.get("keywords", []) if k][:3],
                source="llm",
            )
        )
    return out
