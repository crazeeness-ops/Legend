"""CLI: python -m highlight_engine.render clips.json video.mp4 --out-dir shorts/"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .captions import PRESETS
from .compose import render_short
from .detect import HaarFaceDetector, YuNetFaceDetector


def add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--preset", choices=sorted(PRESETS), default="basic", help="caption style")
    p.add_argument("--layout", choices=["auto", "track", "split", "fit"], default="auto")
    p.add_argument("--no-title", action="store_true", help="do not show the clip title on top")
    p.add_argument("--yunet", help="path to YuNet ONNX face model (better than the default Haar)")
    p.add_argument("--fonts-dir", help="directory with brand fonts")


def render_all(video: str, clips: list[dict], out_dir: str, args: argparse.Namespace) -> list[dict]:
    detector = YuNetFaceDetector(args.yunet) if getattr(args, "yunet", None) else HaarFaceDetector()
    results = []
    for clip in clips:
        out = Path(out_dir) / f"short_{clip['rank']:02d}.mp4"
        meta = render_short(
            video, clip, str(out), style=args.preset, layout=args.layout,
            show_title=not args.no_title, detector=detector, fonts_dir=args.fonts_dir,
        )
        print(f"rendered {out}  layout={meta['reframe']['layout']}  {meta['timings_sec']['total']}s", file=sys.stderr)
        results.append(meta)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="highlight_engine.render", description="Render vertical shorts from engine output.")
    p.add_argument("clips", help="result JSON from `python -m highlight_engine`")
    p.add_argument("video", help="source video")
    p.add_argument("--out-dir", default="shorts")
    p.add_argument("--ranks", help="comma-separated ranks to render (default: all)")
    add_render_args(p)
    args = p.parse_args(argv)

    clips = json.loads(Path(args.clips).read_text(encoding="utf-8"))["clips"]
    if args.ranks:
        wanted = {int(r) for r in args.ranks.split(",")}
        clips = [c for c in clips if c["rank"] in wanted]
    render_all(args.video, clips, args.out_dir, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
