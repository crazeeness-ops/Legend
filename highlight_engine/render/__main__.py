"""CLI: render vertical shorts, list caption styles, or preview them.

  python -m highlight_engine.render clips.json video.mp4 --out-dir shorts/ --preset highlight_box
  python -m highlight_engine.render --list-presets [--json]
  python -m highlight_engine.render --preview styles.png [--preview-video video.mp4]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .captions import POSITIONS, PRESETS, CaptionStyle, load_style_file, preset_catalog, with_brand
from .compose import render_short
from .detect import HaarFaceDetector, YuNetFaceDetector


def add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--preset", choices=list(PRESETS), default="legend",
                   help="caption style (see --list-presets)")
    p.add_argument("--style-file", help="JSON caption style, e.g. {\"base\": \"bold\", \"font\": \"Pretendard\"}")
    p.add_argument("--font", help="caption font family (overrides the preset)")
    p.add_argument("--caption-size", type=int, help="caption font size in px at 1080x1920")
    p.add_argument("--caption-color", help="text colour, RGB hex like FFFFFF")
    p.add_argument("--highlight-color", help="spoken-word / fill colour, RGB hex")
    p.add_argument("--caption-position", choices=["auto", *POSITIONS], help="caption vertical position")
    p.add_argument("--layout", choices=["auto", "track", "split", "fit"], default="auto")
    p.add_argument("--no-title", action="store_true", help="do not show the clip title on top")
    p.add_argument("--yunet", help="path to YuNet ONNX face model (better than the default Haar)")
    p.add_argument("--fonts-dir", help="directory with brand fonts")


def resolve_style(args: argparse.Namespace) -> CaptionStyle:
    base = load_style_file(args.style_file) if getattr(args, "style_file", None) else PRESETS[args.preset]
    return with_brand(
        base,
        font=getattr(args, "font", None),
        size=getattr(args, "caption_size", None),
        color=getattr(args, "caption_color", None),
        highlight=getattr(args, "highlight_color", None),
        position=getattr(args, "caption_position", None),
    )


def render_all(video: str, clips: list[dict], out_dir: str, args: argparse.Namespace) -> list[dict]:
    detector = YuNetFaceDetector(args.yunet) if getattr(args, "yunet", None) else HaarFaceDetector()
    style = resolve_style(args)
    results = []
    for clip in clips:
        out = Path(out_dir) / f"short_{clip['rank']:02d}.mp4"
        meta = render_short(
            video, clip, str(out), style=style, layout=args.layout,
            show_title=not args.no_title, detector=detector, fonts_dir=args.fonts_dir,
        )
        print(f"rendered {out}  layout={meta['reframe']['layout']}  style={style.label}  "
              f"{meta['timings_sec']['total']}s", file=sys.stderr)
        results.append(meta)
    return results


def print_presets(as_json: bool) -> None:
    if as_json:
        print(json.dumps(preset_catalog(), ensure_ascii=False, indent=2))
        return
    for key, st in PRESETS.items():
        print(f"{key:<14} {st.label:<8} [{st.mode}]  {st.description}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="highlight_engine.render", description="Render vertical shorts from engine output.")
    p.add_argument("clips", nargs="?", help="result JSON from `python -m highlight_engine`")
    p.add_argument("video", nargs="?", help="source video")
    p.add_argument("--out-dir", default="shorts")
    p.add_argument("--ranks", help="comma-separated ranks to render (default: all)")
    p.add_argument("--list-presets", action="store_true", help="list caption styles and exit")
    p.add_argument("--json", action="store_true", help="with --list-presets: machine-readable catalog for UIs")
    p.add_argument("--preview", metavar="OUT.png", help="render a gallery of all caption styles and exit")
    p.add_argument("--preview-video", help="use a frame of this video as the gallery background")
    p.add_argument("--preview-at", type=float, default=1.0, help="frame time for --preview-video")
    add_render_args(p)
    args = p.parse_args(argv)

    if args.list_presets:
        print_presets(args.json)
        return 0
    if args.preview:
        from .preview import preview_gallery

        styles = dict(PRESETS)
        if args.style_file or args.font or args.caption_color or args.highlight_color or args.caption_size:
            styles = {"custom": resolve_style(args), **styles}
        print(f"wrote {preview_gallery(args.preview, styles, video=args.preview_video, at=args.preview_at)}")
        return 0
    if not (args.clips and args.video):
        p.error("clips and video are required (or use --list-presets / --preview)")

    clips = json.loads(Path(args.clips).read_text(encoding="utf-8"))["clips"]
    if args.ranks:
        wanted = {int(r) for r in args.ranks.split(",")}
        clips = [c for c in clips if c["rank"] in wanted]
    render_all(args.video, clips, args.out_dir, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
