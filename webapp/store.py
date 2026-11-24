"""File-based project storage (prototype; the production plan moves this to Postgres + S3).

data/projects/<id>/
  project.json        metadata, clips, per-clip edits, render status
  source.<ext>        uploaded video
  transcript.json     word-level transcript (edits to words are saved here)
  renders/            rendered shorts (+ sidecar json)
  cache/              thumbnails, style previews
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from highlight_engine.models import Transcript

DEFAULT_EDITS = {
    "preset": "legend",
    "style_overrides": {},
    "layout": "auto",
    "shot_overrides": [],
    "show_title": True,
}


class NotFound(Exception):
    pass


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "projects").mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._global = threading.Lock()
        self._transcripts: dict[str, tuple[float, Transcript]] = {}

    # ------------------------------------------------------------ basics
    def lock(self, pid: str) -> threading.RLock:
        with self._global:
            return self._locks.setdefault(pid, threading.RLock())

    def dir(self, pid: str) -> Path:
        if not pid.isalnum():
            raise NotFound(pid)
        d = self.root / "projects" / pid
        if not (d / "project.json").exists():
            raise NotFound(pid)
        return d

    def get(self, pid: str) -> dict:
        return json.loads((self.dir(pid) / "project.json").read_text(encoding="utf-8"))

    def save(self, project: dict) -> None:
        d = self.root / "projects" / project["id"]
        tmp = d / "project.json.tmp"
        tmp.write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(d / "project.json")

    def update(self, pid: str, fn) -> dict:
        """Read-modify-write a project under its lock."""
        with self.lock(pid):
            project = self.get(pid)
            fn(project)
            project["updated_at"] = time.time()
            self.save(project)
            return project

    def list(self) -> list[dict]:
        out = []
        for d in (self.root / "projects").iterdir():
            f = d / "project.json"
            if f.exists():
                p = json.loads(f.read_text(encoding="utf-8"))
                out.append({k: p.get(k) for k in ("id", "name", "status", "error", "duration", "created_at")}
                           | {"clip_count": len(p.get("clips", []))})
        return sorted(out, key=lambda p: p["created_at"] or 0, reverse=True)

    def create(self, name: str, video_path: Path, *, move: bool = False, config: dict | None = None) -> dict:
        pid = uuid.uuid4().hex[:10]
        d = self.root / "projects" / pid
        (d / "renders").mkdir(parents=True)
        (d / "cache").mkdir()
        dest = d / f"source{video_path.suffix.lower() or '.mp4'}"
        (shutil.move if move else shutil.copyfile)(str(video_path), str(dest))
        project = {
            "id": pid, "name": name, "video": dest.name, "transcript": None,
            "status": "created", "error": None, "job_id": None, "config": config or {},
            "duration": None, "language": None, "clips": [],
            "created_at": time.time(), "updated_at": time.time(),
        }
        self.save(project)
        return project

    def delete(self, pid: str) -> None:
        shutil.rmtree(self.dir(pid))

    def video_path(self, pid: str) -> Path:
        return self.dir(pid) / self.get(pid)["video"]

    # ------------------------------------------------------------ transcript
    def transcript(self, pid: str) -> Transcript:
        path = self.dir(pid) / "transcript.json"
        if not path.exists():
            raise NotFound(f"{pid} has no transcript yet")
        mtime = path.stat().st_mtime
        cached = self._transcripts.get(pid)
        if cached and cached[0] == mtime:
            return cached[1]
        t = Transcript.load(path)
        self._transcripts[pid] = (mtime, t)
        return t

    def words_in(self, pid: str, start: float, end: float) -> list[dict]:
        return [
            {"i": i, "text": w.text, "start": w.start, "end": w.end}
            for i, w in enumerate(self.transcript(pid).words)
            if w.end > start and w.start < end
        ]

    def update_word(self, pid: str, index: int, text: str) -> dict:
        with self.lock(pid):
            t = self.transcript(pid)
            if not 0 <= index < len(t.words):
                raise NotFound(f"word {index}")
            t.words[index].text = text
            t.save(self.dir(pid) / "transcript.json")
            self._transcripts.pop(pid, None)
            w = t.words[index]
            return {"i": index, "text": w.text, "start": w.start, "end": w.end}

    # ------------------------------------------------------------ clips
    def set_clips(self, pid: str, result: dict) -> None:
        def fn(p: dict) -> None:
            p["duration"] = result["duration"]
            p["language"] = result["language"]
            p["clips"] = []
            for c in result["clips"]:
                c = {k: v for k, v in c.items() if k not in ("words", "text")}
                c["id"] = f"c{c['rank']}"
                c["original"] = {"start": c["start"], "end": c["end"]}
                c["edits"] = json.loads(json.dumps(DEFAULT_EDITS))
                c["render"] = None
                p["clips"].append(c)
        self.update(pid, fn)

    @staticmethod
    def find_clip(project: dict, cid: str) -> dict:
        for c in project["clips"]:
            if c["id"] == cid:
                return c
        raise NotFound(cid)

    def clip_view(self, pid: str, clip: dict) -> dict:
        """Clip with its current words/text derived from the (editable) transcript."""
        words = self.words_in(pid, clip["start"], clip["end"])
        return {**clip, "words": words, "text": " ".join(w["text"] for w in words)}
