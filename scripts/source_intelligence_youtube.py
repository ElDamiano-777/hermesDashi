#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
PREFERRED_PYTHON = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python3"
if (
    PREFERRED_PYTHON.exists()
    and Path(sys.executable).resolve() != PREFERRED_PYTHON.resolve()
    and os.environ.get("SOURCE_INTELLIGENCE_NO_REEXEC") != "1"
):
    env = dict(os.environ)
    env["SOURCE_INTELLIGENCE_NO_REEXEC"] = "1"
    os.execve(str(PREFERRED_PYTHON), [str(PREFERRED_PYTHON), __file__, *sys.argv[1:]], env)

PLUGIN_DIR = HERMES_HOME / "plugins" / "source-intelligence"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from source_intelligence.sources import youtube_pipeline  # noqa: E402


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in {"run", "learn"}:
        youtube_pipeline.run_pipeline()
        return 0
    if cmd == "status":
        youtube_pipeline.status()
        return 0
    if cmd in {"add-video", "add-channel"}:
        if len(sys.argv) < 3:
            print(f"Usage: source_intelligence_youtube.py {cmd} <youtube-url>", file=sys.stderr)
            return 1
        kind = "video" if cmd == "add-video" else "channel"
        print(json.dumps(
            youtube_pipeline.queue_manual_input(kind, sys.argv[2], run_full_pipeline=True),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    if "youtube.com/" in cmd or "youtu.be/" in cmd:
        kind = "video" if youtube_pipeline.looks_like_video_url(cmd) else "channel"
        print(json.dumps(
            youtube_pipeline.queue_manual_input(kind, cmd, run_full_pipeline=True),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    print("Usage: source_intelligence_youtube.py [run|status|add-video <url>|add-channel <url>|<youtube-url>]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
