"""Boundary correction, de-duplication and final scoring.

This is where "never cut mid-sentence" is enforced, independently of how good
the selector's proposals were.
"""

from __future__ import annotations

from .models import Candidate, Clip, Sentence
from .signals import signal_score

SCORE_WEIGHTS = {"hook": 0.30, "completeness": 0.25, "value": 0.20, "emotion": 0.15, "trend": 0.10}
DANGLING_STARTS = (
    "그래서", "그리고", "근데", "그런데", "그러니까", "아까", "그게", "그건", "그거",
    "and ", "so ", "but ", "because ", "that's why", "which ",
)
PAD_BEFORE = 0.20
PAD_AFTER = 0.40


def fit_bounds(
    s: int, e: int, sentences: list[Sentence], min_len: float, max_len: float
) -> tuple[int, int] | None:
    """Adjust a sentence range to satisfy the duration range and end on a complete thought.

    Prefers extending/trimming the *end* so the chosen hook stays at the start.
    Returns None if no valid range exists.
    """
    n = len(sentences)

    def dur(a: int, b: int) -> float:
        return sentences[b].end - sentences[a].start

    # 1) finish the thought: extend to the next terminal sentence if it fits
    k = e
    while not sentences[k].terminal and k + 1 < n and dur(s, k + 1) <= max_len:
        k += 1
    if sentences[k].terminal:
        e = k

    # 2) too short: grow end first, then start
    while dur(s, e) < min_len:
        if e + 1 < n and dur(s, e + 1) <= max_len:
            e += 1
        elif s - 1 >= 0 and dur(s - 1, e) <= max_len:
            s -= 1
        else:
            return None

    # 3) too long: drop trailing sentences
    while dur(s, e) > max_len:
        if e == s:
            return None
        e -= 1
    if dur(s, e) < min_len:
        return None

    # 4) make sure we end on a terminal sentence if any valid one exists
    if not sentences[e].terminal:
        for k in range(e - 1, s - 1, -1):
            if sentences[k].terminal and dur(s, k) >= min_len:
                e = k
                break
        else:
            for k in range(e + 1, n):
                if dur(s, k) > max_len:
                    break
                if sentences[k].terminal:
                    e = k
                    break
    return s, e


def _iou(a: Clip, b: Clip) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    union = max(a.end, b.end) - min(a.start, b.start)
    return inter / union if union > 0 else 0.0


def llm_score(scores: dict[str, float]) -> float:
    """Weighted 0-100 from 0-10 sub-scores."""
    return 10 * sum(w * scores.get(k, 5.0) for k, w in SCORE_WEIGHTS.items())


def finalize(
    candidates: list[Candidate],
    sentences: list[Sentence],
    *,
    min_len: float,
    max_len: float,
    top_n: int,
    total_duration: float | None = None,
    llm_weight: float = 0.7,
    overlap_threshold: float = 0.3,
) -> list[Clip]:
    by_id = {s.id: idx for idx, s in enumerate(sentences)}
    total = total_duration if total_duration is not None else (sentences[-1].end if sentences else 0.0)
    clips: list[Clip] = []

    for c in candidates:
        if c.start_sid not in by_id or c.end_sid not in by_id:
            continue
        fitted = fit_bounds(by_id[c.start_sid], by_id[c.end_sid], sentences, min_len, max_len)
        if fitted is None:
            continue
        s, e = fitted
        span = sentences[s : e + 1]
        first, last = span[0], span[-1]

        scores = dict(c.scores)
        if first.text.lower().startswith(DANGLING_STARTS):
            scores["completeness"] = max(0.0, scores.get("completeness", 5.0) - 2)
        if not last.terminal:
            scores["completeness"] = max(0.0, scores.get("completeness", 5.0) - 2)

        sig = signal_score(span)
        final = llm_weight * llm_score(scores) + (1 - llm_weight) * sig
        clips.append(
            Clip(
                rank=0,
                start=round(max(0.0, first.start - min(PAD_BEFORE, first.gap_before * 0.5)), 2),
                end=round(min(total or last.end, last.end + min(PAD_AFTER, last.gap_after * 0.5)), 2),
                start_sid=first.id,
                end_sid=last.id,
                title=c.title,
                hook=c.hook,
                reason=c.reason,
                score=int(round(max(0.0, min(99.0, final)))),
                score_detail={**{k: round(v, 1) for k, v in scores.items()}, "signal": round(sig, 1)},
                keywords=c.keywords,
                text=" ".join(x.text for x in span),
                words=[{"text": w.text, "start": w.start, "end": w.end} for x in span for w in x.words],
            )
        )

    clips.sort(key=lambda c: c.score, reverse=True)
    kept: list[Clip] = []
    for clip in clips:
        if all(_iou(clip, k) < overlap_threshold for k in kept):
            kept.append(clip)
        if len(kept) >= top_n:
            break
    for i, clip in enumerate(kept, 1):
        clip.rank = i
    return kept
