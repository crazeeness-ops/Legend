"""Turn word-level timestamps into sentence units.

Clips are always cut on sentence boundaries, so sentence quality directly
determines whether a clip ends mid-thought.
"""

from __future__ import annotations

from .models import TERMINAL_PUNCT, Sentence, Word


def split_sentences(
    words: list[Word],
    *,
    pause_split: float = 0.8,
    min_len_for_pause_split: float = 1.5,
    max_len: float = 20.0,
) -> list[Sentence]:
    """Group words into sentences.

    A sentence ends at terminal punctuation, a speaker change, a long pause
    (only once the sentence is at least ``min_len_for_pause_split`` long), or
    when it reaches ``max_len`` seconds (unpunctuated run-on speech).
    """
    sentences: list[Sentence] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(w.text.strip() for w in current if w.text.strip())
        if text:
            sentences.append(
                Sentence(
                    id=len(sentences),
                    start=current[0].start,
                    end=current[-1].end,
                    text=text,
                    speaker=current[0].speaker,
                )
            )
        current.clear()

    for word in words:
        if current:
            prev = current[-1]
            gap = word.start - prev.end
            span = prev.end - current[0].start
            speaker_change = bool(word.speaker and prev.speaker and word.speaker != prev.speaker)
            if speaker_change or (gap >= pause_split and span >= min_len_for_pause_split) or span >= max_len:
                flush()
        current.append(word)
        if word.text.strip().endswith(TERMINAL_PUNCT):
            flush()
    flush()

    for i, s in enumerate(sentences):
        s.gap_before = max(0.0, s.start - sentences[i - 1].end) if i > 0 else s.start
        s.gap_after = max(0.0, sentences[i + 1].start - s.end) if i + 1 < len(sentences) else 0.0
    return sentences
