"""Synthesise a 16:9 'podcast' video for testing the reframer (needs scikit-image for the face image).

Shots cycle through: host close-up (face left, drifting), guest close-up (face right),
two-shot (both faces far apart) and a slide with no faces. Audio is a tone whose
loudness varies per shot.

Usage: python examples/make_demo_video.py out.mp4 [duration_sec]
"""

from __future__ import annotations

import subprocess
import sys

import cv2
import numpy as np


def face_image(size: int) -> np.ndarray:
    from skimage import data

    img = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    return cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)


SHOTS = ("host", "guest", "two", "slide")


def shot_at(t: float, shot_len: float = 6.0) -> str:
    return SHOTS[int(t // shot_len) % len(SHOTS)]


def make_frame(t: float, w: int = 1280, h: int = 720, faces: dict | None = None) -> np.ndarray:
    kind = shot_at(t)
    frame = np.full((h, w, 3), (52, 44, 38), np.uint8)
    cv2.rectangle(frame, (0, int(h * 0.75)), (w, h), (70, 60, 55), -1)  # desk
    big, small = faces["big"], faces["small"]
    if kind == "host":
        x = int(180 + 40 * np.sin(t * 0.8))
        frame[120 : 120 + big.shape[0], x : x + big.shape[1]] = big
    elif kind == "guest":
        x = 760
        frame[110 : 110 + big.shape[0], x : x + big.shape[1]] = big
    elif kind == "two":
        frame[200 : 200 + small.shape[0], 90 : 90 + small.shape[1]] = small
        frame[200 : 200 + small.shape[0], w - 90 - small.shape[1] : w - 90] = small
    else:
        frame[:] = (235, 235, 235)
        cv2.putText(frame, "MONTHLY SIDE INCOME", (120, 200), cv2.FONT_HERSHEY_DUPLEX, 2.2, (40, 40, 40), 4)
        for i, v in enumerate((120, 260, 420, 610)):
            cv2.rectangle(frame, (180 + i * 230, 620 - v), (330 + i * 230, 620), (60, 120, 230), -1)
    return frame


def main(out: str, duration: float = 30.0, fps: int = 25) -> None:
    faces = {"big": face_image(400), "small": face_image(330)}
    w, h = 1280, 720
    vol = "if(lt(mod(t,24),6),0.9,if(lt(mod(t,24),12),0.5,if(lt(mod(t,24),18),0.7,0.2)))"
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps),
         "-i", "-", "-f", "lavfi", "-i", f"sine=frequency=180:duration={duration}",
         "-af", f"volume='{vol}':eval=frame", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out],
        stdin=subprocess.PIPE,
    )
    for i in range(int(duration * fps)):
        proc.stdin.write(make_frame(i / fps, w, h, faces).tobytes())
    proc.stdin.close()
    proc.wait()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo.mp4", float(sys.argv[2]) if len(sys.argv) > 2 else 30.0)
