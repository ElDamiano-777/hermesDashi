from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .workflow import SunoPaths, SunoWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stable Suno flow: CDP login + browserless API generation with manual capture template."
    )
    parser.add_argument("--browser-cdp-url", default="", help="CDP URL, e.g. http://127.0.0.1:9222")
    parser.add_argument("--target", default="https://suno.com/create", help="Login start URL")
    parser.add_argument("--wait-seconds", type=float, default=600.0, help="Login wait window in seconds")

    parser.add_argument("--save-auth-state", default="auth/suno_storage_state.json", help="Output storage_state path")
    parser.add_argument("--save-auth-bundle", default="auth/suno_auth_bundle.json", help="Output auth bundle path")
    parser.add_argument(
        "--manual-capture",
        default="outputs/live_capture_generate_from_ui.json",
        help="Successful manual generate capture JSON",
    )
    parser.add_argument("--out-dir", default="outputs/playwright_api", help="Output directory")

    parser.add_argument("--login-only", action="store_true", help="Only run CDP login and save auth artifacts")
    parser.add_argument("--api-only", action="store_true", help="Only run browserless API generation")
    parser.add_argument("--songs-count", type=int, default=1, help="Number of songs to generate in api-only mode")
    parser.add_argument("--poll-attempts", type=int, default=20, help="Max polling attempts")
    parser.add_argument("--poll-interval", type=float, default=6.0, help="Polling interval in seconds")
    parser.add_argument(
        "--disable-p1-fallback",
        action="store_true",
        help="Disable automatic P1 token refresh via CDP when captcha is required",
    )
    parser.add_argument(
        "--close-browser-after-login",
        action="store_true",
        help="Close the CDP browser automatically after successful login",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    paths = SunoPaths(
        storage_state=Path(args.save_auth_state),
        auth_bundle=Path(args.save_auth_bundle),
        manual_capture=Path(args.manual_capture),
        bearer_token=Path("auth/suno_bearer_token.txt"),
        api_headers=Path("auth/suno_api_headers.json"),
    )
    wf = SunoWorkflow(paths)

    if args.api_only:
        wf.generate_without_browser(
            out_dir=Path(args.out_dir),
            songs_count=max(args.songs_count, 1),
            poll_attempts=max(args.poll_attempts, 1),
            poll_interval=max(args.poll_interval, 0.5),
            browser_cdp_url=args.browser_cdp_url,
            auto_refresh_p1=not args.disable_p1_fallback,
        )
        return 0

    if not args.browser_cdp_url:
        raise SystemExit("--browser-cdp-url is required for login mode")

    result = asyncio.run(
        wf.login_via_cdp(
            cdp_url=args.browser_cdp_url,
            wait_seconds=args.wait_seconds,
            target_url=args.target,
            close_browser_after_login=args.close_browser_after_login,
        )
    )
    print(f"[suno] login result: {result}")
    if not result.get("ok"):
        return 1

    if args.login_only:
        return 0

    wf.generate_without_browser(
        out_dir=Path(args.out_dir),
        songs_count=max(args.songs_count, 1),
        poll_attempts=max(args.poll_attempts, 1),
        poll_interval=max(args.poll_interval, 0.5),
        browser_cdp_url=args.browser_cdp_url,
        auto_refresh_p1=not args.disable_p1_fallback,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
