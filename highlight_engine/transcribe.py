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


GPU_ERRORS = ("cublas", "cudnn", "cuda", "cufft", "nvrtc", "libcudart", ".dll")


def _is_gpu_problem(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in GPU_ERRORS)


def transcribe(
    path: str,
    *,
    language: str | None = None,
    model_size: str = "large-v3",
    device: str | None = None,
    compute_type: str = "auto",  # CTranslate2 picks the fastest supported type (int8 on most CPUs)
) -> Transcript:
    """Transcribe with word timestamps.

    ``device`` defaults to ``LEGEND_WHISPER_DEVICE`` or "auto" (GPU when CTranslate2 sees one). A PC with
    an NVIDIA card but without the CUDA runtime libraries (cuBLAS/cuDNN) fails on the GPU — often only
    once decoding starts — so we retry on the CPU instead of failing the upload.
    """
    import os

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pip install faster-whisper  (or pass a transcript .json)") from e

    device = device or os.environ.get("LEGEND_WHISPER_DEVICE", "auto")
    audio = load_audio(path)

    def run(dev: str, ctype: str) -> Transcript:
        model = WhisperModel(model_size, device=dev, compute_type=ctype)
        segments, info = model.transcribe(
            audio,
            language=language,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,  # reduces hallucinated repetition on long audio
        )
        words: list[Word] = []
        for seg in segments:  # decoding happens lazily here, so GPU errors surface here too
            for w in seg.words or []:
                text = w.word.strip()
                if text:
                    words.append(Word(text=text, start=float(w.start), end=float(w.end), prob=float(w.probability)))
        return Transcript(language=info.language, duration=float(info.duration), words=words)

    if device == "cpu":
        return run("cpu", "int8" if compute_type == "auto" else compute_type)
    try:
        return run(device, compute_type)
    except (RuntimeError, OSError, ValueError) as e:
        if not _is_gpu_problem(e):
            raise
        import logging

        logging.getLogger(__name__).warning("GPU transcription unavailable (%s); falling back to CPU", e)
        return run("cpu", "int8")
