"""Face detectors.

Default is OpenCV's bundled Haar cascade (works offline, no model download).
For production use a DNN detector: pass a YuNet ONNX model path
(face_detection_yunet_2023mar.onnx from the OpenCV model zoo) for much better
recall on profiles, small faces and low light.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import logging

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Face:
    x: float  # box in *source* pixel coordinates
    y: float
    w: float
    h: float
    score: float = 1.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h


class FaceDetector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> list[Face]: ...


class HaarFaceDetector:
    def __init__(self, min_rel_size: float = 0.06) -> None:
        self.min_rel_size = min_rel_size
        self.available = hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data")
        if not self.available:  # e.g. some OpenCV 5.x builds; keep working without face tracking
            log.warning("OpenCV has no CascadeClassifier (install opencv-python-headless<5); faces won't be tracked")
            return
        base = cv2.data.haarcascades
        self.frontal = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
        self.profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")

    def detect(self, frame_bgr: np.ndarray) -> list[Face]:
        if not self.available:
            return []
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        h = gray.shape[0]
        min_px = max(20, int(h * self.min_rel_size))
        boxes = list(self.frontal.detectMultiScale(gray, 1.1, 5, minSize=(min_px, min_px)))
        if not boxes:  # retry with contrast equalisation for dark / flat footage
            boxes = list(self.frontal.detectMultiScale(cv2.equalizeHist(gray), 1.1, 5, minSize=(min_px, min_px)))
        if not boxes:
            boxes = list(self.profile.detectMultiScale(gray, 1.1, 5, minSize=(min_px, min_px)))
            if not boxes:  # profile cascade only finds left-facing; mirror for right-facing
                flipped = self.profile.detectMultiScale(cv2.flip(gray, 1), 1.1, 5, minSize=(min_px, min_px))
                boxes = [(gray.shape[1] - x - w, y, w, h_) for x, y, w, h_ in flipped]
        return _nms([Face(float(x), float(y), float(w), float(hh)) for x, y, w, hh in boxes])


class YuNetFaceDetector:
    def __init__(self, model_path: str, score_threshold: float = 0.7) -> None:
        self.model_path = model_path
        self.score_threshold = score_threshold
        self._det = None
        self._size = None

    def detect(self, frame_bgr: np.ndarray) -> list[Face]:
        h, w = frame_bgr.shape[:2]
        if self._det is None or self._size != (w, h):
            self._det = cv2.FaceDetectorYN.create(self.model_path, "", (w, h), self.score_threshold)
            self._size = (w, h)
        _, faces = self._det.detect(frame_bgr)
        if faces is None:
            return []
        return [Face(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])) for f in faces]


def _nms(faces: list[Face], thresh: float = 0.3) -> list[Face]:
    faces = sorted(faces, key=lambda f: f.area, reverse=True)
    kept: list[Face] = []
    for f in faces:
        ok = True
        for k in kept:
            ix = max(0.0, min(f.x + f.w, k.x + k.w) - max(f.x, k.x))
            iy = max(0.0, min(f.y + f.h, k.y + k.h) - max(f.y, k.y))
            if ix * iy / min(f.area, k.area) > thresh:
                ok = False
                break
        if ok:
            kept.append(f)
    return kept
