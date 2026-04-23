from __future__ import annotations

import argparse
import json
from typing import Any

from . import schemas, tools


def _register_tool(ctx: Any, name: str, schema: dict[str, Any], handler: Any) -> None:
    try:
        ctx.register_tool(name=name, toolset="suno", schema=schema, handler=handler)
    except TypeError:
        ctx.register_tool(name, schema, handler)


def _render_json(data: str) -> str:
    try:
        return json.dumps(json.loads(data), ensure_ascii=True, indent=2)
    except Exception:
        return data

def _setup_cli(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="suno_subcommand")

    subs.add_parser("status", help="Check Suno session/auth status")

    p_setup = subs.add_parser("setup", help="Update ~/.hermes/plugins/suno/.env values")
    p_setup.add_argument("--email", default="", help="Google account email")
    p_setup.add_argument("--password", default="", help="Google account password")
    p_setup.add_argument("--output-dir", default="", help="Default output directory")
    p_setup.add_argument("--cdp-port", type=int, default=None, help="Managed browser CDP port")
    p_setup.add_argument("--poll-attempts", type=int, default=None, help="Default poll attempts")
    p_setup.add_argument("--poll-interval", type=float, default=None, help="Default poll interval")

    p_generate = subs.add_parser("generate", help="Generate one Suno song")
    p_generate.add_argument("--lyrics", default="")
    p_generate.add_argument("--styles", default="")
    p_generate.add_argument("--exclude-styles", default="")
    p_generate.add_argument("--vocal-gender", choices=["m", "f"], default="m")
    p_generate.add_argument(
        "--lyrics-mode",
        choices=["custom", "auto", "mumble", "instrumental"],
        default="custom",
    )
    p_generate.add_argument("--weirdness", type=float, default=0.5)
    p_generate.add_argument("--style-influence", type=float, default=0.5)
    p_generate.add_argument("--song-title", default="Skill Song")
    p_generate.add_argument("--audio-type", choices=["", "one_shot", "loop"], default="")
    p_generate.add_argument("--bpm", default="")
    p_generate.add_argument(
        "--key-root",
        choices=["", "any", "c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"],
        default="",
    )
    p_generate.add_argument("--key-scale", choices=["", "major", "minor"], default="")
    p_generate.add_argument("--inspiration-clip-id", default="")
    p_generate.add_argument("--inspiration-start-s", type=float, default=None)
    p_generate.add_argument("--inspiration-end-s", type=float, default=None)
    p_generate.add_argument("--workspace", default="")
    p_generate.add_argument("--out-dir", default="")
    p_generate.add_argument("--poll-attempts", type=int, default=None)
    p_generate.add_argument("--poll-interval", type=float, default=None)
    p_generate.add_argument("--wait-seconds", type=float, default=180.0)
    p_generate.add_argument("--force-login", action="store_true")
    p_generate.add_argument("--disable-p1-fallback", action="store_true")
    p_generate.add_argument("--debug", action="store_true")


def _handle_cli(args: argparse.Namespace) -> None:
    sub = getattr(args, "suno_subcommand", None)

    if sub == "status":
        print(_render_json(tools.suno_status({})))
        return

    if sub == "setup":
        payload = {
            "google_email": getattr(args, "email", ""),
            "google_password": getattr(args, "password", ""),
            "output_dir": getattr(args, "output_dir", ""),
            "cdp_port": getattr(args, "cdp_port", None),
            "poll_attempts": getattr(args, "poll_attempts", None),
            "poll_interval": getattr(args, "poll_interval", None),
        }
        print(_render_json(tools.suno_setup(payload)))
        return

    if sub == "generate":
        payload = {
            "lyrics": getattr(args, "lyrics", ""),
            "styles": getattr(args, "styles", ""),
            "exclude_styles": getattr(args, "exclude_styles", ""),
            "vocal_gender": getattr(args, "vocal_gender", "m"),
            "lyrics_mode": getattr(args, "lyrics_mode", "custom"),
            "weirdness": getattr(args, "weirdness", 0.5),
            "style_influence": getattr(args, "style_influence", 0.5),
            "song_title": getattr(args, "song_title", "Skill Song"),
            "audio_type": getattr(args, "audio_type", ""),
            "bpm": getattr(args, "bpm", ""),
            "key_root": getattr(args, "key_root", ""),
            "key_scale": getattr(args, "key_scale", ""),
            "inspiration_clip_id": getattr(args, "inspiration_clip_id", ""),
            "inspiration_start_s": getattr(args, "inspiration_start_s", None),
            "inspiration_end_s": getattr(args, "inspiration_end_s", None),
            "workspace": getattr(args, "workspace", ""),
            "out_dir": getattr(args, "out_dir", ""),
            "poll_attempts": getattr(args, "poll_attempts", None),
            "poll_interval": getattr(args, "poll_interval", None),
            "wait_seconds": getattr(args, "wait_seconds", 180.0),
            "force_login": getattr(args, "force_login", False),
            "disable_p1_fallback": getattr(args, "disable_p1_fallback", False),
            "debug": getattr(args, "debug", False),
        }
        print(_render_json(tools.suno_generate(payload)))
        return

    print("Usage: hermes suno <status|setup|generate> [options]")

def register(ctx: Any) -> None:
    _register_tool(ctx, "suno_status", schemas.SUNO_STATUS, tools.suno_status)
    _register_tool(ctx, "suno_setup", schemas.SUNO_SETUP, tools.suno_setup)
    _register_tool(ctx, "suno_generate", schemas.SUNO_GENERATE, tools.suno_generate)
    _register_tool(ctx, "suno_feature_action", schemas.SUNO_FEATURE_ACTION, tools.suno_feature_action)

    ctx.register_cli_command(
        name="suno",
        help="Manage Suno plugin",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
    )
