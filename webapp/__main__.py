"""Run the editor.

  python -m webapp serve [--data data] [--port 8000]
  python -m webapp import VIDEO [--transcript T.json] [--selector heuristic] [--name 이름]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="webapp")
    p.add_argument("--data", default="data", help="data directory (projects, renders)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the editor web server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--google-client-secret", help="OAuth client JSON from Google Cloud (YouTube upload)")
    s.add_argument("--public-url", help="URL users open, for the OAuth redirect (default http://HOST:PORT)")

    i = sub.add_parser("import", help="create a project from a local video (and optional transcript)")
    i.add_argument("video")
    i.add_argument("--transcript", help="transcript .json (skips speech recognition)")
    i.add_argument("--name")
    i.add_argument("--selector", choices=["claude", "heuristic"], default="claude")
    i.add_argument("--length", choices=["short", "medium", "long"], default="medium")
    i.add_argument("--top", type=int, default=10)
    i.add_argument("--prompt", default="")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    from .server import create_app

    if args.cmd == "serve":
        app = create_app(args.data, google_client_secret=args.google_client_secret,
                         public_url=args.public_url or f"http://{args.host}:{args.port}")
    else:
        app = create_app(args.data)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    video = Path(args.video)
    project = app.state.store.create(args.name or video.stem, video)
    cfg = {"length": args.length, "top": args.top, "prompt": args.prompt, "selector": args.selector}
    jid = app.state.start_analysis(project["id"], args.transcript, cfg)
    job = app.state.jobs.wait(jid, timeout=3600)
    if job["status"] != "done":
        print(f"analysis failed: {job['error']}", file=sys.stderr)
        return 1
    print(f"project {project['id']} ready: {job['result']['clips']} clips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
