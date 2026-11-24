"""Tiny in-process background job runner (production: Redis queue + GPU workers)."""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[..., object], *args, on_error: Callable[[str], None] | None = None,
               **meta) -> str:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": kind, "status": "queued", "progress": 0.0, "message": "대기 중",
               "result": None, "error": None, "created_at": time.time(), **meta}
        with self._lock:
            self._jobs[jid] = job

        def progress(frac: float | None = None, message: str | None = None) -> None:
            with self._lock:
                if frac is not None:
                    job["progress"] = round(float(frac), 3)
                if message is not None:
                    job["message"] = message

        def run() -> None:
            job["status"] = "running"
            try:
                job["result"] = fn(*args, progress=progress)
                job.update(status="done", progress=1.0, message="완료")
            except Exception as e:  # report every failure to the UI
                log.error("job %s failed: %s\n%s", jid, e, traceback.format_exc())
                job.update(status="error", error=f"{type(e).__name__}: {e}", message="실패")
                if on_error:
                    on_error(job["error"])

        self._pool.submit(run)
        return jid

    def get(self, jid: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(jid)
            return dict(job) if job else None

    def wait(self, jid: str, timeout: float = 300) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(jid)
            if job and job["status"] in ("done", "error"):
                return job
            time.sleep(0.1)
        raise TimeoutError(jid)
