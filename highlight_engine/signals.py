"""Non-LLM signals: loudness and speaking rate.

These capture *how* something is said (excitement, emphasis) which a text
transcript alone misses. They contribute a minority share of the final score.
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np

from .models import Sentence

SAMPLE_RATE = 16000


def load_audio_rms(path: str, window: float = 0.5) -> np.ndarray:
    """Return loudness (dBFS) per ``window`` seconds, decoded with ffmpeg."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for audio analysis")
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    )
    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(SAMPLE_RATE * window)
    n = len(samples) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = samples[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def _zscore(values: np.ndarray) -> np.ndarray:
    std = values.std()
    if std < 1e-6:
        return np.zeros_like(values)
    return (values - values.mean()) / std


def annotate_sentences(sentences: list[Sentence], rms_db: np.ndarray | None = None, window: float = 0.5) -> None:
    """Fill ``energy_z`` and ``rate_z`` on each sentence in place."""
    if not sentences:
        return
    rates = np.array([len(s.text.replace(" ", "")) / max(s.duration, 0.3) for s in sentences], dtype=np.float32)
    for s, z in zip(sentences, _zscore(rates)):
        s.rate_z = float(np.clip(z, -3, 3))

    if rms_db is None or len(rms_db) == 0:
        return
    energies = []
    for s in sentences:
        a = int(s.start / window)
        b = max(a + 1, int(np.ceil(s.end / window)))
        seg = rms_db[a:b]
        energies.append(float(seg.mean()) if len(seg) else float(rms_db.mean()))
    for s, z in zip(sentences, _zscore(np.array(energies, dtype=np.float32))):
        s.energy_z = float(np.clip(z, -3, 3))


def signal_score(span: list[Sentence]) -> float:
    """0-100 score for a clip from its audio/delivery signals (50 = average)."""
    if not span:
        return 50.0
    energy = np.array([s.energy_z for s in span])
    rate = np.array([s.rate_z for s in span])
    weights = np.array([s.duration for s in span]) + 1e-6
    retention = np.array([s.retention_z for s in span])
    score = (
        50
        + 12 * float(np.average(energy, weights=weights))
        + 8 * float(energy.max())
        + 6 * float(np.average(rate, weights=weights))
        + 10 * float(np.average(retention, weights=weights))
        + 8 * float(retention.max())
    )
    return float(np.clip(score, 0, 100))


def retention_residuals(curve: list[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    """Detrended audience retention.

    YouTube's ``audienceWatchRatio`` by ``elapsedVideoTimeRatio`` always decays as viewers
    leave, so raw values would just favour the start of the video. We fit the decay
    (linear in log space) and keep what's left: bumps where people stayed or rewatched.
    Returns (x positions 0-1, z-scored residuals).
    """
    pts = sorted((float(x), float(y)) for x, y in curve if y is not None and y > 0)
    if len(pts) < 5:
        return np.zeros(0), np.zeros(0)
    x = np.array([p[0] for p in pts])
    y = np.log(np.array([p[1] for p in pts]))
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    return x, _zscore(resid)


def annotate_retention(sentences: list[Sentence], curve: list[tuple[float, float]] | None, duration: float) -> None:
    if not curve or not sentences or duration <= 0:
        return
    xs, zs = retention_residuals(curve)
    if not len(xs):
        return
    for s in sentences:
        a, b = s.start / duration, s.end / duration
        inside = zs[(xs >= a) & (xs <= b)]
        val = float(inside.max()) if len(inside) else float(np.interp((a + b) / 2, xs, zs))
        s.retention_z = float(np.clip(val, -3, 3))
