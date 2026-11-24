"""CLI: python -m highlight_engine <video|transcript.json> [options]"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .pipeline import LENGTH_PRESETS, EngineConfig, run


def parse_length(value: str) -> tuple[float, float]:
    if value in LENGTH_PRESETS:
        return LENGTH_PRESETS[value]
    lo, _, hi = value.partition("-")
    try:
        a, b = float(lo), float(hi)
    except ValueError:
        raise argparse.ArgumentTypeError("use short|medium|long or MIN-MAX seconds, e.g. 30-60")
    if not 0 < a < b:
        raise argparse.ArgumentTypeError("MIN must be < MAX")
    return a, b


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="highlight_engine", description="Find short-form highlights in a long video.")
    p.add_argument("source", help="video/audio file, or transcript .json")
    p.add_argument("--audio", help="media file for loudness signals when source is a transcript .json")
    p.add_argument("--length", type=parse_length, default=LENGTH_PRESETS["medium"],
                   help="short|medium|long or MIN-MAX seconds (default: medium = 30-60)")
    p.add_argument("--top", type=int, default=10, help="number of clips to return")
    p.add_argument("--lang", default=None, help="transcript language (default: auto)")
    p.add_argument("--prompt", default=None, help='what to look for, e.g. "가격 얘기만"')
    p.add_argument("--selector", choices=["claude", "heuristic"], default="claude")
    p.add_argument("--model", default=None, help="Claude model id (default: claude-opus-5-5)")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--whisper-model", default="large-v3")
    p.add_argument("--save-transcript", help="write the transcript json here (video input only)")
    p.add_argument("--out", help="write result json here (default: stdout)")
    p.add_argument("--export-dir", help="also cut preview mp4 clips into this directory")
    p.add_argument("--render-dir", help="also render vertical 9:16 shorts with captions into this directory")
    p.add_argument("-v", "--verbose", action="store_true")
    from .render.__main__ import add_render_args

    add_render_args(p.add_argument_group("render options (with --render-dir)"))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if args.selector == "claude":
        from .llm import DEFAULT_MODEL, ClaudeSelector

        selector = ClaudeSelector(model=args.model or DEFAULT_MODEL, effort=args.effort)
    else:
        from .heuristic import HeuristicSelector

        selector = HeuristicSelector()

    cfg = EngineConfig(min_len=args.length[0], max_len=args.length[1], top_n=args.top,
                       language=args.lang, user_prompt=args.prompt, whisper_model=args.whisper_model)
    result = run(args.source, selector, cfg, audio=args.audio, transcript_out=args.save_transcript)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)

    media = args.audio if args.source.endswith(".json") else args.source
    if (args.export_dir or args.render_dir) and not media:
        print("--export-dir/--render-dir need the media file (use --audio with a transcript)", file=sys.stderr)
        return 2
    if args.render_dir:
        from .render.__main__ import render_all

        render_all(media, result["clips"], args.render_dir, args)
    if args.export_dir:
        from .export import export_clips
        from .models import Clip

        clips = [Clip(**{k: v for k, v in c.items() if k != "duration"}) for c in result["clips"]]
        for path in export_clips(media, clips, args.export_dir):
            print(f"exported {path}", file=sys.stderr)

    for c in result["clips"]:
        print(f"#{c['rank']:>2} [{c['score']:>2}] {c['start']:7.1f}-{c['end']:7.1f}s ({c['duration']:.0f}s) {c['title']}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
