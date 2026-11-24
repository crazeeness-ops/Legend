"""Core data types shared by every stage of the highlight engine."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

TERMINAL_PUNCT = (".", "?", "!", "。", "？", "！", "…")


@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: str | None = None
    prob: float | None = None


@dataclass
class Transcript:
    language: str
    duration: float
    words: list[Word]

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "duration": self.duration,
            "words": [asdict(w) for w in self.words],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        return cls(
            language=data.get("language", "ko"),
            duration=float(data["duration"]),
            words=[Word(**w) for w in data["words"]],
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class Sentence:
    id: int
    start: float
    end: float
    text: str
    speaker: str | None = None
    gap_before: float = 0.0  # silence before this sentence (s)
    gap_after: float = 0.0  # silence after this sentence (s)
    energy_z: float = 0.0  # loudness relative to the whole video (z-score)
    rate_z: float = 0.0  # speaking rate relative to the whole video (z-score)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def terminal(self) -> bool:
        """True if the sentence ends a complete thought (ends with terminal punctuation)."""
        return self.text.rstrip().endswith(TERMINAL_PUNCT)


@dataclass
class Candidate:
    """A raw highlight proposal from a selector, expressed in sentence ids."""

    start_sid: int
    end_sid: int
    title: str
    hook: str
    reason: str
    scores: dict[str, float]  # each 0-10: hook, completeness, emotion, value, trend
    keywords: list[str] = field(default_factory=list)
    source: str = "llm"


@dataclass
class Clip:
    """A final, boundary-corrected clip ready for reframing/rendering."""

    rank: int
    start: float
    end: float
    start_sid: int
    end_sid: int
    title: str
    hook: str
    reason: str
    score: int  # 0-99
    score_detail: dict[str, float]
    keywords: list[str]
    text: str

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d
