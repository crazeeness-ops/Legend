"""Speech-to-text with word timestamps (faster-whisper)."""

from __future__ import annotations

import subprocess

import numpy as np

from .models import Transcript, Word

SAMPLE_RATE = 16000


def load_audio(path: str) -> np.ndarray:
    """Decode to 16 kHz mono float32 with ffmpeg.

    We hand Whisper the samples ourselves instead of letting faster-whisper decode the file: its
    decoder calls PyAV with an argument newer PyAV releases removed (``metadata_errors``), which
    breaks transcription with ``TypeError`` depending on which versions pip picks.
    """
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"오디오를 읽지 못했어요: {proc.stderr.decode(errors='ignore')[-300:]}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("영상에 소리가 없어요. 말소리가 있는 영상을 올려 주세요")
    return audio


def transcribe(
    path: str,
    *,
    language: str | None = None,
    model_size: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",  # CTranslate2 picks the fastest supported type (int8 on most CPUs)
) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pip install faster-whisper  (or pass a transcript .json)") from e

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        load_audio(path),
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
