from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path
from typing import Any

from .workflow import SunoPaths, SunoWorkflow


def _find_env_path() -> Path:
    cwd = Path.cwd()
    candidates = [
        cwd / ".env",
        cwd / "share" / "suno_library" / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def _load_env(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _write_env(env_path: Path, values: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(values.items())]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_path(base_dir: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base_dir / p)


def _resolve_manual_capture(base_dir: Path, configured: str) -> Path:
    candidates = [
        configured,
        "outputs/live_capture_generate_from_ui_fresh.json",
        "outputs/live_capture_generate_from_ui.json",
        "../../outputs/live_capture_generate_from_ui_fresh.json",
        "../../outputs/live_capture_generate_from_ui.json",
    ]
    for item in candidates:
        p = _resolve_path(base_dir, item)
        if p.exists():
            return p
    return _resolve_path(base_dir, configured)


def _build_paths(base_dir: Path, env: dict[str, str]) -> SunoPaths:
    storage_state = os.environ.get("SUNO_STORAGE_STATE", "").strip() or env.get("SUNO_STORAGE_STATE", "auth/suno_storage_state.json")
    auth_bundle = os.environ.get("SUNO_AUTH_BUNDLE", "").strip() or env.get("SUNO_AUTH_BUNDLE", "auth/suno_auth_bundle.json")
    manual_capture_cfg = (
        os.environ.get("SUNO_MANUAL_CAPTURE", "").strip()
        or env.get("SUNO_MANUAL_CAPTURE", "outputs/live_capture_generate_from_ui_fresh.json")
    )
    return SunoPaths(
        storage_state=_resolve_path(base_dir, storage_state),
        auth_bundle=_resolve_path(base_dir, auth_bundle),
        manual_capture=_resolve_manual_capture(base_dir, manual_capture_cfg),
        bearer_token=_resolve_path(base_dir, "auth/suno_bearer_token.txt"),
        api_headers=_resolve_path(base_dir, "auth/suno_api_headers.json"),
    )


def _has_valid_api_session(wf: SunoWorkflow) -> tuple[bool, str]:
    if not wf.paths.storage_state.exists():
        return False, f"missing storage state: {wf.paths.storage_state}"

    try:
        session = wf._cookie_session_from_storage(wf.paths.storage_state)
    except Exception as exc:
        return False, f"invalid storage state: {exc}"

    headers = wf._load_runtime_headers()
    if "authorization" not in headers:
        try:
            token = wf._pick_working_token(session, headers)
            headers["authorization"] = f"Bearer {token}"
        except Exception:
            return False, "no working bearer token"

    try:
        resp = session.get(
            "https://studio-api-prod.suno.com/api/user/get_user_session_id/",
            headers=headers,
            timeout=20,
        )
    except Exception as exc:
        return False, f"session check request failed: {exc}"

    if resp.status_code == 200:
        return True, "session check status=200"
    return False, f"session check status={resp.status_code}"


def _setup_command(env_path: Path) -> int:
    current = _load_env(env_path)

    default_email = current.get("SUNO_GOOGLE_EMAIL", "")
    default_cdp = current.get("SUNO_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    default_output = current.get("SUNO_OUTPUT_DIR", "outputs/suno_cli")

    # Do not expose stored email in interactive prompt.
    email = input("Google Email: ").strip() or default_email
    password = getpass.getpass("Google Password (hidden): ").strip() or current.get("SUNO_GOOGLE_PASSWORD", "")
    output_dir = input(f"Output directory [{default_output}]: ").strip() or default_output
    cdp_url = input(f"Visible CDP URL [{default_cdp}]: ").strip() or default_cdp

    updates = dict(current)
    updates["SUNO_GOOGLE_EMAIL"] = email
    updates["SUNO_GOOGLE_PASSWORD"] = password
    updates["SUNO_OUTPUT_DIR"] = output_dir
    updates["SUNO_BROWSER_CDP_URL"] = cdp_url
    updates.setdefault("SUNO_STORAGE_STATE", "auth/suno_storage_state.json")
    updates.setdefault("SUNO_AUTH_BUNDLE", "auth/suno_auth_bundle.json")
    updates.setdefault("SUNO_MANUAL_CAPTURE", "../../outputs/live_capture_generate_from_ui_fresh.json")

    _write_env(env_path, updates)
    print(f"[suno] setup saved: {env_path}")
    return 0


def _setup_help() -> str:
    return (
        "usage: suno setup\n\n"
        "Interactive setup for .env values:\n"
        "- SUNO_GOOGLE_EMAIL\n"
        "- SUNO_GOOGLE_PASSWORD\n"
        "- SUNO_OUTPUT_DIR\n"
        "- SUNO_BROWSER_CDP_URL\n"
    )


def _build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suno",
        description="Generate music via Suno API with automatic login/token handling.",
    )
    parser.add_argument("--lyrics", required=True, help="Song lyrics")
    parser.add_argument("--styles", required=True, help="Styles/tags, e.g. 'trap, dark pop'")
    parser.add_argument("--exclude-styles", default="", help="Exclude styles")
    parser.add_argument("--vocal-gender", default="m", choices=["m", "f"], help="Vocal gender")
    parser.add_argument(
        "--lyrics-mode",
        default="custom",
        choices=["custom", "auto", "mumble", "instrumental"],
        help="Lyrics mode",
    )
    parser.add_argument("--weirdness", type=float, default=0.5, help="Weirdness slider (0..1)")
    parser.add_argument("--style-influence", type=float, default=0.5, help="Style influence slider (0..1)")
    parser.add_argument("--song-title", default="", help="Song title")
    parser.add_argument("--songs-count", type=int, default=1, help="Number of songs")
    parser.add_argument("--out-dir", default="", help="Override output directory")
    parser.add_argument("--cdp-url", default="", help="Visible CDP URL override")
    parser.add_argument("--wait-seconds", type=float, default=600.0, help="Login wait timeout")
    parser.add_argument("--poll-attempts", type=int, default=20, help="Polling attempts")
    parser.add_argument("--poll-interval", type=float, default=6.0, help="Polling interval")
    parser.add_argument("--force-login", action="store_true", help="Always login before generation")
    parser.add_argument("--disable-p1-fallback", action="store_true", help="Disable P1 refresh via CDP")
    return parser


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _generate_command(argv: list[str], env_path: Path) -> int:
    parser = _build_generate_parser()
    args = parser.parse_args(argv)

    env = _load_env(env_path)
    base_dir = env_path.parent
    paths = _build_paths(base_dir, env)
    wf = SunoWorkflow(paths)

    cdp_url = args.cdp_url.strip() or env.get("SUNO_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    out_dir_value = args.out_dir.strip() or env.get("SUNO_OUTPUT_DIR", "outputs/suno_cli")
    out_dir = _resolve_path(base_dir, out_dir_value)

    session_ok, reason = _has_valid_api_session(wf)
    print(f"[suno] session-valid={session_ok} reason={reason}")

    if args.force_login or not session_ok:
        if not cdp_url:
            raise SystemExit("No CDP URL configured. Use 'suno setup' or --cdp-url.")
        login_result = asyncio.run(
            wf.login_via_cdp(
                cdp_url=cdp_url,
                wait_seconds=max(args.wait_seconds, 30.0),
                target_url="https://suno.com/create",
                close_browser_after_login=False if not args.disable_p1_fallback else True,
            )
        )
        print(f"[suno] login result: {login_result}")
        if not login_result.get("ok"):
            return 1

    params: dict[str, Any] = {
        "song_title": args.song_title.strip() or "CLI Song",
        "lyrics": args.lyrics,
        "styles": args.styles,
        "exclude_styles": args.exclude_styles,
        "gender": args.vocal_gender,
        "lyrics_mode": args.lyrics_mode,
        "weirdness": _clamp01(args.weirdness),
        "style_influence": _clamp01(args.style_influence),
    }

    wf.generate_without_browser(
        out_dir=out_dir,
        songs_count=max(args.songs_count, 1),
        poll_attempts=max(args.poll_attempts, 1),
        poll_interval=max(args.poll_interval, 0.5),
        browser_cdp_url=cdp_url,
        auto_refresh_p1=not args.disable_p1_fallback,
        song_params=params,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    env_path = _find_env_path()
    argv = list(argv if argv is not None else os.sys.argv[1:])

    if argv and argv[0] == "setup":
        if len(argv) > 1 and argv[1] in {"-h", "--help"}:
            print(_setup_help())
            return 0
        return _setup_command(env_path)

    return _generate_command(argv, env_path)


if __name__ == "__main__":
    raise SystemExit(main())
