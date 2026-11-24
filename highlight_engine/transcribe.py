"""Speech-to-text with word timestamps (faster-whisper)."""

from __future__ import annotations

from .models import Transcript, Word


def transcribe(
    path: str,
    *,
    language: str | None = None,
    model_size: str = "large-v3",
    device: str = "auto",
    compute_type: str = "default",
) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pip install faster-whisper  (or pass a transcript .json)") from e

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        path,
        language=language,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,  # reduces hallucinated repetition on long audio
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(text=text, start=float(w.start), end=float(w.end), prob=float(w.probability)))
    return Transcript(language=info.language, duration=float(info.duration), words=words)
