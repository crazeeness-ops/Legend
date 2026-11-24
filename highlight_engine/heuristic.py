"""Offline baseline selector (no API calls).

Useful for local testing without credentials and as a quality baseline the
LLM selector must beat on the evaluation set.
"""

from __future__ import annotations

import re

from .models import Candidate, Sentence

QUESTION = re.compile(r"[?？]")
EXCLAIM = re.compile(r"[!！]")
NUMBER = re.compile(r"\d")
HOOK_WORDS = (
    "비밀", "진짜", "절대", "충격", "실수", "이유", "방법", "꿀팁", "솔직히", "처음", "최고", "최악",
    "secret", "never", "mistake", "why", "how", "truth", "best", "worst",
)


def _text_cues(s: Sentence) -> float:
    t = s.text.lower()
    score = 0.0
    score += 1.5 if QUESTION.search(t) else 0.0
    score += 1.0 if EXCLAIM.search(t) else 0.0
    score += 0.8 if NUMBER.search(t) else 0.0
    score += 1.0 * sum(1 for w in HOOK_WORDS if w in t)
    return score


class HeuristicSelector:
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
        terms = [w for w in (user_prompt or "").lower().split() if len(w) > 1]
        windows: list[tuple[float, int, int]] = []
        for i, first in enumerate(sentences):
            for j in range(i, len(sentences)):
                dur = sentences[j].end - first.start
                if dur > max_len:
                    break
                if dur < min_len or not sentences[j].terminal:
                    continue
                span = sentences[i : j + 1]
                if terms and not any(t in " ".join(s.text.lower() for s in span) for t in terms):
                    continue
                hook = _text_cues(first) * 2 + max(first.energy_z, 0)
                body = sum(_text_cues(s) for s in span) / len(span)
                energy = sum(s.energy_z * s.duration for s in span) / max(dur, 1e-6)
                windows.append((hook + body + energy, i, j))

        windows.sort(reverse=True)
        picked: list[Candidate] = []
        used: list[tuple[int, int]] = []
        for score, i, j in windows:
            if any(not (j < a or i > b) for a, b in used):
                continue
            used.append((i, j))
            first = sentences[i]
            norm = max(0.0, min(10.0, 4 + score))
            picked.append(
                Candidate(
                    start_sid=i,
                    end_sid=j,
                    title=first.text[:40],
                    hook=first.text,
                    reason="heuristic: text cues + delivery energy",
                    scores={"hook": norm, "completeness": 6.0, "emotion": norm, "value": 5.0, "trend": 5.0},
                    source="heuristic",
                )
            )
            if len(picked) >= target_count * 2:
                break
        return picked
