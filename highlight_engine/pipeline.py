"""End-to-end: video (or transcript) -> ranked clip candidates."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import Candidate, Sentence, Transcript
from .postprocess import finalize
from .segment import split_sentences
from .signals import annotate_sentences, load_audio_rms

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
    whisper_model: str = "large-v3"


def run(
    source: str,
    selector: Selector,
    config: EngineConfig | None = None,
    *,
    audio: str | None = None,
    transcript_out: str | None = None,
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

        transcript = transcribe(source, language=cfg.language, model_size=cfg.whisper_model)
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
    timings["signals"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    candidates = selector.select(
        sentences,
        min_len=cfg.min_len,
        max_len=cfg.max_len,
        target_count=cfg.top_n,
        language=cfg.language or transcript.language,
        user_prompt=cfg.user_prompt,
    )
    timings["select"] = time.perf_counter() - t2

    clips = finalize(
        candidates, sentences,
        min_len=cfg.min_len, max_len=cfg.max_len, top_n=cfg.top_n,
        total_duration=transcript.duration,
    )
    timings["total"] = time.perf_counter() - t0

    return {
        "source": Path(source).name,
        "language": transcript.language,
        "duration": round(transcript.duration, 2),
        "sentence_count": len(sentences),
        "candidate_count": len(candidates),
        "config": {"min_len": cfg.min_len, "max_len": cfg.max_len, "top_n": cfg.top_n,
                   "user_prompt": cfg.user_prompt},
        "timings_sec": {k: round(v, 2) for k, v in timings.items()},
        "clips": [c.to_dict() for c in clips],
    }
