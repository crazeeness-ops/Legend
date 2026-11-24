"""16:9 -> 9:16 reframing: shot detection, face tracking, layout choice, smooth crop path.

Layouts
- track: one 9:16 window that follows the main face (talking head, 1 speaker in frame)
- split: two people far apart -> left person on top half, right person on bottom half
- fit:   no reliable face (slides, screen recordings, b-roll) -> whole frame over a blurred background
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import numpy as np

from .detect import Face, FaceDetector
from .media import VideoInfo, read_frames

OUT_W, OUT_H = 1080, 1920
SAMPLE_FPS = 5.0
ANALYSIS_WIDTH = 640
CUT_MIN_DIFF = 10.0  # mean abs diff (0-255) between consecutive thumbnails
CUT_RATIO = 5.0  # ...and this many times the recent typical frame-to-frame change


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    def to_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]


@dataclass
class ShotPath:
    start: float  # clip-relative seconds
    end: float
    layout: str = "track"  # each shot gets its own layout (a clip can mix close-ups, two-shots and slides)
    times: list[float] = field(default_factory=list)
    centers: list[float] = field(default_factory=list)  # track: crop centre x per keyframe
    split: tuple[Rect, Rect] | None = None  # split: (top, bottom) source rects


@dataclass
class ReframePlan:
    src_w: int
    src_h: int
    crop_w: int
    shots: list[ShotPath]
    face_ratio: float
    two_face_ratio: float

    @property
    def layout(self) -> str:
        """Dominant layout by duration (for summaries)."""
        totals: dict[str, float] = {}
        for s in self.shots:
            totals[s.layout] = totals.get(s.layout, 0.0) + (s.end - s.start)
        return max(totals, key=totals.get) if totals else "fit"

    def shot_at(self, t: float) -> ShotPath:
        starts = [s.start for s in self.shots]
        return self.shots[max(0, bisect.bisect_right(starts, t) - 1)]

    def crop_rect(self, t: float, shot: ShotPath | None = None) -> Rect:
        """Track layout: the 9:16 source window at clip time t (piecewise-linear within a shot)."""
        shot = shot or self.shot_at(t)
        cx = float(np.interp(t, shot.times, shot.centers)) if shot.times else self.src_w / 2
        x = int(round(cx - self.crop_w / 2))
        x = max(0, min(self.src_w - self.crop_w, x))
        return Rect(x, 0, self.crop_w, self.src_h)

    def caption_y_at(self, t: float) -> int:
        """Vertical centre for captions in the 1080x1920 output, following the shot's layout."""
        layout = self.shot_at(t).layout
        if layout == "split":
            return OUT_H // 2  # on the seam between the two people
        if layout == "fit":
            fg_h = min(OUT_H, int(OUT_W * self.src_h / self.src_w))
            return min(OUT_H - 380, OUT_H // 2 + fg_h // 2 + 170)  # just below the video
        return int(OUT_H * 0.70)

    def to_dict(self) -> dict:
        return {
            "layout": self.layout,
            "source": [self.src_w, self.src_h],
            "crop_w": self.crop_w,
            "face_ratio": round(self.face_ratio, 3),
            "two_face_ratio": round(self.two_face_ratio, 3),
            "shots": [
                {
                    "start": round(s.start, 3), "end": round(s.end, 3), "layout": s.layout,
                    "keyframes": [[round(t, 3), round(c, 1)] for t, c in zip(s.times, s.centers)],
                    "split": [r.to_list() for r in s.split] if s.split else None,
                }
                for s in self.shots
            ],
        }


# ---------------------------------------------------------------- analysis

def detect_cuts(thumbs: list[np.ndarray], fps: float, window: int = 25) -> list[float]:
    """Hard cuts: a frame difference far above both an absolute floor and the recent motion level."""
    diffs = [
        float(np.mean(np.abs(thumbs[i].astype(np.int16) - thumbs[i - 1].astype(np.int16))))
        for i in range(1, len(thumbs))
    ]
    cuts = []
    for i, d in enumerate(diffs):
        recent = diffs[max(0, i - window) : i] or [0.0]
        if d > max(CUT_MIN_DIFF, CUT_RATIO * float(np.median(recent))):
            cuts.append((i + 1) / fps)
    return cuts


def shots_from_cuts(cuts: list[float], duration: float, min_len: float = 0.5) -> list[tuple[float, float]]:
    bounds = [0.0]
    for c in sorted(cuts):
        if c - bounds[-1] >= min_len and duration - c >= min_len:
            bounds.append(c)
    bounds.append(duration)
    return list(zip(bounds[:-1], bounds[1:]))


def choose_layout(samples: list[list[Face]], src_w: int, crop_w: int, src_h: int) -> tuple[str, float, float]:
    n = max(1, len(samples))
    face_ratio = sum(1 for f in samples if f) / n
    two = 0
    for faces in samples:
        if len(faces) >= 2:
            a, b = sorted(faces, key=lambda f: f.area, reverse=True)[:2]
            if b.area >= 0.35 * a.area and abs(a.cx - b.cx) > crop_w * 0.8:
                two += 1
    two_ratio = two / n
    if src_w / src_h <= OUT_W / OUT_H + 0.02:
        return "fit", face_ratio, two_ratio  # already vertical
    if face_ratio < 0.35:
        return "fit", face_ratio, two_ratio
    if two_ratio >= 0.6:
        return "split", face_ratio, two_ratio
    return "track", face_ratio, two_ratio


def track_shot(
    times: list[float], samples: list[list[Face]], s_start: float, s_end: float, src_w: int, crop_w: int,
    fallback_center: float, *, deadzone_rel: float = 0.06, alpha: float = 0.3,
) -> ShotPath:
    """Smooth crop path following the main face within one shot.

    ``times``/``samples`` are the detections inside the shot.
    """
    lo, hi = crop_w / 2, src_w - crop_w / 2
    deadzone = deadzone_rel * src_w
    raw: list[float | None] = []
    prev: float | None = None
    for faces in samples:
        if not faces:
            raw.append(None)
            continue
        if prev is None:
            pick = max(faces, key=lambda f: f.area)
        else:  # stick with the same person unless someone is much bigger
            pick = max(faces, key=lambda f: f.area * (1.6 if abs(f.cx - prev) < 0.15 * src_w else 1.0))
        prev = pick.cx
        raw.append(pick.cx)

    known = [c for c in raw if c is not None]
    fallback = float(np.median(known)) if known else fallback_center
    filled = [c if c is not None else fallback for c in raw] or [fallback]
    t_list = list(times) or [s_start]

    if max(filled) - min(filled) < deadzone:
        centers = [float(np.median(filled))] * len(filled)  # static shot: lock the camera
    else:
        centers, cur = [], filled[0]
        for target in filled:
            if abs(target - cur) > deadzone / 2:
                cur += alpha * (target - cur)
            centers.append(cur)
    centers = [float(min(hi, max(lo, c))) for c in centers]
    # anchor the path to the shot edges so interpolation never leaks across a cut
    return ShotPath(s_start, s_end, "track", [s_start] + t_list + [s_end], [centers[0]] + centers + [centers[-1]])


def split_rects_for(faces_pool: list[list[Face]], src_w: int, src_h: int) -> tuple[Rect, Rect] | None:
    lefts, rights = [], []
    for faces in faces_pool:
        if len(faces) >= 2:
            a, b = sorted(sorted(faces, key=lambda f: f.area, reverse=True)[:2], key=lambda f: f.cx)
            lefts.append(a)
            rights.append(b)
    if not lefts:
        return None

    def rect(fs: list[Face]) -> Rect:
        cx = float(np.median([f.cx for f in fs]))
        cy = float(np.median([f.cy for f in fs]))
        fh = float(np.median([f.h for f in fs]))
        h = int(min(src_h, max(fh * 4.0, src_h * 0.55)))
        w = int(min(src_w / 2, h * OUT_W / (OUT_H / 2)))
        h = int(w * (OUT_H / 2) / OUT_W)
        x = int(min(src_w - w, max(0, cx - w / 2)))
        y = int(min(src_h - h, max(0, cy - h * 0.5)))  # face centred, clear of the title bar
        return Rect(x, y, w, h)

    return rect(lefts), rect(rights)


def build_plan(
    times: list[float], samples: list[list[Face]], shots: list[tuple[float, float]],
    src_w: int, src_h: int, *, layout: str = "auto",
) -> ReframePlan:
    """Pure planning step (no I/O) so it can be unit-tested with synthetic detections."""
    crop_w = min(src_w, int(round(src_h * OUT_W / OUT_H / 2)) * 2)
    clip_layout, face_ratio, two_ratio = choose_layout(samples, src_w, crop_w, src_h)
    clip_split = split_rects_for(samples, src_w, src_h)
    paths: list[ShotPath] = []
    last_center = src_w / 2
    prev_layout = clip_layout
    for s0, s1 in shots:
        idx = [i for i, t in enumerate(times) if s0 <= t < s1]
        pool = [samples[i] for i in idx]
        if layout != "auto":
            lay = layout
        elif pool:
            lay = choose_layout(pool, src_w, crop_w, src_h)[0]
        else:
            lay = prev_layout  # shot too short to sample: keep the previous framing
        if lay == "split":
            rects = split_rects_for(pool, src_w, src_h) or clip_split
            if rects is not None:
                paths.append(ShotPath(s0, s1, "split", split=rects))
                prev_layout = lay
                continue
            lay = "track"
        if lay == "fit":
            paths.append(ShotPath(s0, s1, "fit"))
        else:
            path = track_shot([times[i] for i in idx], pool, s0, s1, src_w, crop_w, last_center)
            last_center = path.centers[-1]
            paths.append(path)
        prev_layout = lay
    return ReframePlan(src_w, src_h, crop_w, paths, face_ratio, two_ratio)


def analyze(
    video: str, start: float, duration: float, info: VideoInfo, detector: FaceDetector,
    *, layout: str = "auto", sample_fps: float = SAMPLE_FPS,
) -> ReframePlan:
    src_w, src_h = info.width, info.height
    thumbs = list(read_frames(video, start, duration, 64, 36, gray=True))
    shots = shots_from_cuts(detect_cuts(thumbs, info.fps), duration)

    aw = ANALYSIS_WIDTH if src_w > ANALYSIS_WIDTH else src_w
    ah = int(round(src_h * aw / src_w / 2)) * 2
    scale = src_w / aw
    times, samples = [], []
    for i, frame in enumerate(read_frames(video, start, duration, aw, ah, fps=sample_fps)):
        faces = [Face(f.x * scale, f.y * scale, f.w * scale, f.h * scale, f.score) for f in detector.detect(frame)]
        times.append(i / sample_fps)
        samples.append(faces)
    return build_plan(times, samples, shots, src_w, src_h, layout=layout)
