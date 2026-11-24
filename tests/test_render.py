import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from highlight_engine.render.captions import ass_color, ass_time, build_ass, paginate, with_brand  # noqa: E402
from highlight_engine.render.detect import Face  # noqa: E402
from highlight_engine.render.reframe import (  # noqa: E402
    OUT_H, build_plan, choose_layout, detect_cuts, shots_from_cuts, track_shot,
)

W, H = 1920, 1080
CROP_W = 608


def words(*specs):
    return [{"text": t, "start": s, "end": e} for t, s, e in specs]


# ---------- captions ----------

def test_ass_helpers():
    assert ass_color("FFE600") == "&H0000E6FF"
    assert ass_time(3661.239) == "1:01:01.24"


def test_paginate_breaks_on_length_gap_and_punctuation():
    ws = words(("직장인이", 0, .5), ("부업으로", .5, 1), ("월", 1, 1.2), ("100만", 1.2, 1.6), ("원.", 1.6, 2),
               ("진짜", 3.5, 3.8), ("가능해요?", 3.8, 4.3))
    pages = paginate(ws, max_chars=8, max_words=4)
    assert [[w["text"] for w in p] for p in pages] == [["직장인이", "부업으로"], ["월", "100만", "원"], ["진짜", "가능해요?"]]


def test_build_ass_karaoke_keywords_title_and_positions():
    ws = words(("월", 10.0, 10.3), ("100만", 10.3, 10.8), ("원", 10.8, 11.0), ("{위험}", 11.0, 11.5))
    ass = build_ass(ws, clip_start=10.0, duration=3.0, style="basic", title="부업의 진실",
                    keywords=["100만"], caption_y=lambda t: 960 if t >= 1.0 else 1344)
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue: 1")]
    assert len(lines) == 4  # one event per spoken word
    assert lines[0].startswith("Dialogue: 1,0:00:00.00,0:00:00.30")  # times are clip-relative
    assert "\\c&H0000E6FF" in lines[0]  # current word highlighted
    assert "\\c&H00FFE500" in lines[0]  # keyword colour on a non-current word
    assert "pos(540,1344)" in lines[0] and "pos(540,960)" in lines[3]  # follows the shot layout
    assert "(위험)" in lines[3] and "{위험}" not in ass  # braces can't inject ASS tags
    assert "Dialogue: 2,0:00:00.00,0:00:03.00,Title" in ass and "부업의 진실" in ass


def test_minimal_preset_one_event_per_page_and_brand_override():
    ws = words(("하나", 0, .4), ("둘", .4, .8), ("셋", .8, 1.2))
    ass = build_ass(ws, clip_start=0, duration=2, style="minimal")
    assert len([l for l in ass.splitlines() if l.startswith("Dialogue: 1")]) == 1
    branded = with_brand("basic", font="Pretendard", highlight="FF0000")
    assert "Style: Caption,Pretendard" in build_ass(ws, clip_start=0, duration=2, style=branded)


# ---------- reframe planning ----------

def face(cx, w=150, cy=400):
    return Face(cx - w / 2, cy - w / 2, w, w)


def test_cut_detection_adapts_to_motion():
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, (36, 64), dtype=np.uint8)
    other = rng.integers(0, 255, (36, 64), dtype=np.uint8)
    thumbs = [base] * 30 + [other] * 30
    assert detect_cuts(thumbs, fps=30) == [1.0]
    assert shots_from_cuts([1.0, 1.2], 3.0) == [(0.0, 1.0), (1.0, 3.0)]  # too-short shot merged


def test_choose_layout():
    assert choose_layout([[face(900)]] * 10, W, CROP_W, H)[0] == "track"
    assert choose_layout([[face(400), face(1500)]] * 10, W, CROP_W, H)[0] == "split"
    assert choose_layout([[]] * 8 + [[face(900)]] * 2, W, CROP_W, H)[0] == "fit"
    assert choose_layout([[face(500)]] * 10, 1080, CROP_W, 1920)[0] == "fit"  # already vertical


def test_track_locks_on_jitter_and_follows_movement():
    times = [i / 5 for i in range(25)]
    jitter = [[face(1000 + (8 if i % 2 else -8))] for i in range(25)]
    p = track_shot(times, jitter, 0, 5, W, CROP_W, W / 2)
    assert len(set(p.centers)) == 1 and abs(p.centers[0] - 1000) <= 8  # static camera

    moving = [[face(500 + 40 * i)] for i in range(25)]
    p = track_shot(times, moving, 0, 5, W, CROP_W, W / 2)
    assert p.centers[-1] > p.centers[1] + 400  # followed the subject
    assert all(CROP_W / 2 <= c <= W - CROP_W / 2 for c in p.centers)  # never leaves the frame


def test_plan_switches_layout_per_shot_and_never_interpolates_across_cuts():
    times = [i / 5 for i in range(50)]
    samples = [[face(400)] if t < 4 else [face(400), face(1500)] if t < 7 else [] for t in times]
    plan = build_plan(times, samples, [(0, 4), (4, 7), (7, 10)], W, H)
    assert [s.layout for s in plan.shots] == ["track", "split", "fit"]
    r = plan.crop_rect(3.9)
    assert r.x <= 400 <= r.x + r.w and r.w == CROP_W and r.h == H
    assert plan.caption_y_at(5) == OUT_H // 2 and plan.caption_y_at(1) == int(OUT_H * 0.7)
    top, bottom = plan.shots[1].split
    assert top.x < 400 < top.x + top.w and bottom.x < 1500 < bottom.x + bottom.w


# ---------- full render ----------

needs_media = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or pytest.importorskip("skimage", reason="scikit-image needed") is None,
    reason="ffmpeg/scikit-image missing",
)


@needs_media
def test_render_short_end_to_end(tmp_path):
    import cv2
    from make_demo_video import main as make_video

    from highlight_engine.render import render_short

    video = tmp_path / "demo.mp4"
    make_video(str(video), duration=9.0)  # 0-6s host close-up (face left), 6-9s guest (face right)
    clip = {"rank": 1, "start": 1.0, "end": 8.5, "title": "테스트 제목", "keywords": ["부업"],
            "words": words(("부업으로", 1.2, 1.8), ("돈", 1.9, 2.2), ("벌기.", 2.2, 2.8), ("진짜", 6.5, 7.0))}
    out = tmp_path / "short.mp4"
    meta = render_short(str(video), clip, str(out), style="bold")

    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)], capture_output=True, text=True,
    ).stdout)["streams"]
    v = next(s for s in info if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert abs(float(v["duration"]) - 7.5) < 0.2
    assert any(s["codec_type"] == "audio" for s in info)

    shots = meta["reframe"]["shots"]
    assert [round(s["start"]) for s in shots] == [0, 5]  # cut at source 6.0s
    assert all(s["layout"] == "track" for s in shots)
    assert out.with_suffix(".json").exists()

    # the face must be horizontally centred-ish in the vertical output, before and after the cut
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    for t in (2.0, 6.5):
        frame_png = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                        "-vf", "scale=540:960", str(frame_png)], check=True)
        gray = cv2.cvtColor(cv2.imread(str(frame_png)), cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
        assert len(faces), f"no face at {t}s"
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        assert abs((x + w / 2) - 270) < 110, f"face off-centre at {t}s"
