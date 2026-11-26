"""End-to-end: video (or transcript) -> ranked clip candidates."""

from __future__ import annotations

import logging
import time
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import Candidate, Clip, Sentence, Transcript
from .postprocess import finalize
from .segment import split_sentences
from .signals import annotate_retention, annotate_sentences, load_audio_rms

log = logging.getLogger(__name__)

LENGTH_PRESETS = {"short": (15.0, 30.0), "medium": (30.0, 60.0), "long": (60.0, 90.0)}


class Selector(Protocol):
    def select(
        self,
        sentences: list[Sentence],
        *,
        min_len: float,
        max_len: float,
        target_count: int,
        language: str = "ko",
        user_prompt: str | None = None,
    ) -> list[Candidate]: ...


@dataclass
class EngineConfig:
    min_len: float = 30.0
    max_len: float = 60.0
    top_n: int = 10
    language: str | None = None  # None = auto-detect
    user_prompt: str | None = None
    whisper_model: str = field(default_factory=lambda: os.environ.get("LEGEND_WHISPER_MODEL", "large-v3"))


def run(
    source: str,
    selector: Selector,
    config: EngineConfig | None = None,
    *,
    audio: str | None = None,
    transcript_out: str | None = None,
    retention: list[tuple[float, float]] | None = None,
    progress=None,  # callable(fraction 0-1, message): transcription is most of the time
) -> dict:
    """Run the engine.

    ``source`` is a media file, or a transcript ``.json`` (then ``audio`` may
    optionally point at the media for loudness signals).
    """
    cfg = config or EngineConfig()
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    if source.endswith(".json"):
        transcript = Transcript.load(source)
        media = audio
    else:
        from .transcribe import transcribe

        transcript = transcribe(source, language=cfg.language, model_size=cfg.whisper_model, progress=progress)
        media = source
        if transcript_out:
            transcript.save(transcript_out)
    timings["transcribe"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    sentences = split_sentences(transcript.words)
    rms = None
    if media:
        try:
            rms = load_audio_rms(media)
        except Exception as e:  # signals are optional
            log.warning("audio analysis skipped: %s", e)
    annotate_sentences(sentences, rms)
    annotate_retention(sentences, retention, transcript.duration)
    timings["signals"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    min_len, max_len = fit_lengths(cfg.min_len, cfg.max_len, transcript.duration)
    candidates = selector.select(
        sentences,
        min_len=min_len,
        max_len=max_len,
        target_count=cfg.top_n,
        language=cfg.language or transcript.language,
        user_prompt=cfg.user_prompt,
    )
    timings["select"] = time.perf_counter() - t2

    clips = finalize(
        candidates, sentences,
        min_len=min_len, max_len=max_len, top_n=cfg.top_n,
        total_duration=transcript.duration,
        llm_weight=0.6 if retention else 0.7,  # real audience data earns a bigger say
    )
    if not clips and 0 < transcript.duration <= max(max_len, cfg.max_len):
        clips = [whole_clip(sentences, transcript.duration)]
    timings["total"] = time.perf_counter() - t0

    return {
        "source": Path(source).name,
        "language": transcript.language,
        "duration": round(transcript.duration, 2),
        "sentence_count": len(sentences),
        "candidate_count": len(candidates),
        "config": {"min_len": min_len, "max_len": max_len, "top_n": cfg.top_n,
                   "user_prompt": cfg.user_prompt, "retention": bool(retention)},
        "timings_sec": {k: round(v, 2) for k, v in timings.items()},
        "clips": [c.to_dict() for c in clips],
    }


def fit_lengths(min_len: float, max_len: float, duration: float) -> tuple[float, float]:
    """Shrink the requested clip length for videos too short to hold it (e.g. a 20 s phone clip)."""
    if duration <= 0 or duration >= min_len * 1.5:
        return min_len, max_len
    new_max = min(max_len, duration)
    return round(max(3.0, min(min_len, duration * 0.4)), 2), round(new_max, 2)


def whole_clip(sentences: list[Sentence], duration: float) -> Clip:
    """Last resort for short or speechless videos: the whole video becomes one short."""
    words = [{"text": w.text, "start": w.start, "end": w.end} for x in sentences for w in x.words]
    return Clip(rank=1, start=0.0, end=round(duration, 2),
                start_sid=sentences[0].id if sentences else 0, end_sid=sentences[-1].id if sentences else 0,
                title=(sentences[0].text[:40] if sentences else "전체 영상"), hook="", reason="짧은 영상이라 전체를 한 클립으로 만들었어요",
                score=50, score_detail={}, keywords=[], text=" ".join(x.text for x in sentences), words=words)
