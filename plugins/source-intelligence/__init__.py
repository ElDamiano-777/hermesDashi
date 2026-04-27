from __future__ import annotations

import argparse
import json
from typing import Any

from . import schemas, tools


def _register_tool(ctx: Any, name: str, schema: dict[str, Any], handler: Any) -> None:
    try:
        ctx.register_tool(name=name, toolset="source-intelligence", schema=schema, handler=handler)
    except TypeError:
        ctx.register_tool(name, schema, handler)


def _render_json(data: str) -> str:
    try:
        return json.dumps(json.loads(data), ensure_ascii=True, indent=2)
    except Exception:
        return data


def _setup_cli(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="source_intelligence_subcommand")
    subs.add_parser("status", help="Show source intelligence status")
    subs.add_parser("run-youtube", help="Run the YouTube source pipeline")

    p_youtube = subs.add_parser("youtube", help="Queue a YouTube video or channel")
    p_youtube.add_argument("url")
    p_youtube.add_argument("--kind", choices=["auto", "video", "channel"], default="auto")
    p_youtube.add_argument("--no-run", action="store_true", help="Queue without running the full pipeline")

    p_ingest = subs.add_parser("ingest", help="Store a normalized source document")
    p_ingest.add_argument("--type", default="text", dest="source_type")
    p_ingest.add_argument("--url", default="")
    p_ingest.add_argument("--title", default="")
    p_ingest.add_argument("--domain", default="")
    p_ingest.add_argument("--text", required=True)


def _handle_cli(args: argparse.Namespace) -> None:
    sub = getattr(args, "source_intelligence_subcommand", None)

    if sub == "status":
        print(_render_json(tools.source_intelligence_status({})))
        return

    if sub == "run-youtube":
        print(_render_json(tools.source_intelligence_youtube({"action": "run"})))
        return

    if sub == "youtube":
        print(_render_json(tools.source_intelligence_youtube({
            "action": "queue",
            "url": getattr(args, "url", ""),
            "kind": getattr(args, "kind", "auto"),
            "run": not bool(getattr(args, "no_run", False)),
        })))
        return

    if sub == "ingest":
        print(_render_json(tools.source_intelligence_ingest({
            "source_type": getattr(args, "source_type", "text"),
            "url": getattr(args, "url", ""),
            "title": getattr(args, "title", ""),
            "domain": getattr(args, "domain", ""),
            "text": getattr(args, "text", ""),
        })))
        return

    print("Usage: hermes source-intelligence <status|run-youtube|youtube|ingest> [options]")


def register(ctx: Any) -> None:
    _register_tool(
        ctx,
        "source_intelligence_status",
        schemas.SOURCE_INTELLIGENCE_STATUS,
        tools.source_intelligence_status,
    )
    _register_tool(
        ctx,
        "source_intelligence_youtube",
        schemas.SOURCE_INTELLIGENCE_YOUTUBE,
        tools.source_intelligence_youtube,
    )
    _register_tool(
        ctx,
        "source_intelligence_ingest",
        schemas.SOURCE_INTELLIGENCE_INGEST,
        tools.source_intelligence_ingest,
    )
    ctx.register_cli_command(
        name="source-intelligence",
        help="Manage source intelligence ingestion and pipelines",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
    )

