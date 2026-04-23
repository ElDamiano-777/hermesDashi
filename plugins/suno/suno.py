#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
if __package__:
    from .core.workflow import SunoPaths, SunoWorkflow
else:
    if str(PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_ROOT))
    from core.workflow import SunoPaths, SunoWorkflow

PLUGIN_DIR = Path(__file__).resolve().parent
ENV_PATH = PLUGIN_DIR / ".env"
PROFILE_ROOT = PLUGIN_DIR / "profile"
CHROME_PROFILE_DIR = PROFILE_ROOT / "chrome"
ARTIFACT_DIR = PROFILE_ROOT / "artifacts"


def _embedded_capture_template() -> dict[str, Any]:
    # Built-in baseline capture so the skill can run without external capture files.
    body: dict[str, Any] = {
        "artist_clip_id": None,
        "artist_end_s": None,
        "artist_start_s": None,
        "continue_at": None,
        "continue_clip_id": None,
        "continued_aligned_prompt": None,
        "cover_clip_id": None,
        "cover_end_s": None,
        "cover_start_s": None,
        "generation_type": "TEXT",
        "make_instrumental": False,
        "metadata": {
            "web_client_pathname": "/create",
            "is_max_mode": False,
            "is_mumble": False,
            "create_mode": "custom",
            "user_tier": "",
            "create_session_token": "",
            "disable_volume_normalization": False,
            "vocal_gender": "m",
            "control_sliders": {
                "weirdness_constraint": 0.5,
                "style_weight": 0.5,
            },
        },
        "mv": "chirp-v3-5",
        "negative_tags": "",
        "override_fields": [],
        "persona_id": None,
        "prompt": "",
        "tags": "",
        "title": "",
        "token": None,
        "transaction_uuid": "",
        "user_uploaded_images_b64": [],
    }

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://suno.com",
        "referer": "https://suno.com/create",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "browser-token": "{\"token\":\"\"}",
        "device-id": "",
    }

    return {
        "request": {
            "method": "POST",
            "url": "https://studio-api-prod.suno.com/api/generate/v2-web/",
            "headers": headers,
            "post_data": json.dumps(body, ensure_ascii=True, separators=(",", ":")),
        }
    }


def _ensure_embedded_capture_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _embedded_capture_template()
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(values.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_command() -> int:
    current = _load_env(ENV_PATH)

    email_default = current.get("SUNO_GOOGLE_EMAIL", "")
    pass_default = current.get("SUNO_GOOGLE_PASSWORD", "")
    out_default = current.get("SUNO_OUTPUT_DIR", "outputs/suno_plugin")
    port_default = current.get("SUNO_CDP_PORT", "9222")
    poll_attempts_default = current.get("SUNO_POLL_ATTEMPTS", "60")
    poll_interval_default = current.get("SUNO_POLL_INTERVAL", "4.0")

    # Do not display stored credentials in setup prompts.
    email = input("Google Email (without @gmail.com): ").strip() or email_default
    password = getpass.getpass("Google Password (hidden): ").strip() or pass_default
    output_dir = input(f"Output directory [{out_default}]: ").strip() or out_default
    cdp_port = input(f"CDP port [{port_default}]: ").strip() or port_default
    poll_attempts = input(f"Poll attempts [{poll_attempts_default}]: ").strip() or poll_attempts_default
    poll_interval = input(f"Poll interval seconds [{poll_interval_default}]: ").strip() or poll_interval_default

    values = dict(current)
    values["SUNO_GOOGLE_EMAIL"] = email
    values["SUNO_GOOGLE_PASSWORD"] = password
    values["SUNO_OUTPUT_DIR"] = output_dir
    values.pop("SUNO_MANUAL_CAPTURE", None)
    values["SUNO_CDP_PORT"] = cdp_port
    values["SUNO_POLL_ATTEMPTS"] = poll_attempts
    values["SUNO_POLL_INTERVAL"] = poll_interval

    _write_env(ENV_PATH, values)
    print(f"[suno] setup saved: {ENV_PATH}")
    return 0


def _setup_help() -> str:
    return (
        "usage: suno.py setup\n\n"
        "Interactive setup writes ~/.hermes/plugins/suno/.env with:\n"
        "- SUNO_GOOGLE_EMAIL\n"
        "- SUNO_GOOGLE_PASSWORD\n"
        "- SUNO_OUTPUT_DIR\n"
        "- SUNO_CDP_PORT\n"
        "- SUNO_POLL_ATTEMPTS\n"
        "- SUNO_POLL_INTERVAL\n"
    )


def _build_paths(env: dict[str, str]) -> SunoPaths:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    capture_path = ARTIFACT_DIR / "embedded_capture.json"
    _ensure_embedded_capture_file(capture_path)

    return SunoPaths(
        storage_state=ARTIFACT_DIR / "suno_storage_state.json",
        auth_bundle=ARTIFACT_DIR / "suno_auth_bundle.json",
        manual_capture=capture_path,
        bearer_token=ARTIFACT_DIR / "suno_bearer_token.txt",
        api_headers=ARTIFACT_DIR / "suno_api_headers.json",
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


def _collect_strings(node: Any, out: list[str], depth: int = 0) -> None:
    if depth > 5:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                out.append(key)
            _collect_strings(value, out, depth + 1)
        return
    if isinstance(node, list):
        for item in node[:300]:
            _collect_strings(item, out, depth + 1)
        return
    if isinstance(node, str):
        out.append(node)


def _find_bool_flags(node: Any, flags: dict[str, bool], depth: int = 0) -> None:
    if depth > 5 or not isinstance(node, dict):
        return
    for key, value in node.items():
        k = str(key).strip().lower()
        if isinstance(value, bool):
            flags[k] = value
        if isinstance(value, dict):
            _find_bool_flags(value, flags, depth + 1)
        elif isinstance(value, list):
            for item in value[:100]:
                if isinstance(item, dict):
                    _find_bool_flags(item, flags, depth + 1)


def _detect_premium_account(wf: SunoWorkflow) -> dict[str, Any]:
    """Best-effort premium detection from billing endpoint.

    Returns keys: ok, is_premium, tier_hint, reason.
    is_premium is True/False/None (None means unknown).
    """
    if not wf.paths.storage_state.exists():
        return {
            "ok": False,
            "is_premium": None,
            "tier_hint": "",
            "reason": f"missing storage state: {wf.paths.storage_state}",
        }

    try:
        session = wf._cookie_session_from_storage(wf.paths.storage_state)
    except Exception as exc:
        return {"ok": False, "is_premium": None, "tier_hint": "", "reason": f"invalid storage state: {exc}"}

    headers = wf._load_runtime_headers()
    if "authorization" not in headers:
        try:
            token = wf._pick_working_token(session, headers)
            headers["authorization"] = f"Bearer {token}"
        except Exception as exc:
            return {"ok": False, "is_premium": None, "tier_hint": "", "reason": f"missing bearer token: {exc}"}

    try:
        resp = session.get("https://studio-api-prod.suno.com/api/billing/info/", headers=headers, timeout=30)
    except Exception as exc:
        return {"ok": False, "is_premium": None, "tier_hint": "", "reason": f"billing request failed: {exc}"}

    if resp.status_code != 200:
        return {
            "ok": False,
            "is_premium": None,
            "tier_hint": "",
            "reason": f"billing info status={resp.status_code}",
        }

    try:
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "is_premium": None, "tier_hint": "", "reason": f"invalid billing json: {exc}"}

    flags: dict[str, bool] = {}
    _find_bool_flags(data, flags)
    for key in ("is_premium", "premium", "is_pro", "pro", "is_paid", "paid", "is_subscribed", "subscribed"):
        if key in flags:
            val = bool(flags[key])
            return {
                "ok": True,
                "is_premium": val,
                "tier_hint": "boolean_flag",
                "reason": f"detected via {key}",
            }

    strings: list[str] = []
    _collect_strings(data, strings)
    blob = " ".join(strings).lower()

    premium_hit = bool(re.search(r"\b(premium|pro|premier|plus|paid|creator\s*pro)\b", blob))
    free_hit = bool(re.search(r"\b(free|basic|starter)\b", blob))

    if premium_hit and not free_hit:
        return {"ok": True, "is_premium": True, "tier_hint": "string_match", "reason": "premium markers detected"}
    if free_hit and not premium_hit:
        return {"ok": True, "is_premium": False, "tier_hint": "string_match", "reason": "free markers detected"}

    return {"ok": True, "is_premium": None, "tier_hint": "", "reason": "tier markers ambiguous"}


def _chrome_bin() -> str:
    candidates = ["google-chrome", "chromium", "chromium-browser"]
    for name in candidates:
        if shutil.which(name):
            return name
    raise RuntimeError("No Chrome/Chromium binary found in PATH.")


def _cleanup_profile_locks(profile_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        p = profile_dir / name
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
            except Exception:
                pass


def _wait_cdp_ready(port: int, timeout_s: float = 8.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.08)
    return False


def _start_managed_browser(port: int) -> subprocess.Popen[str]:
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_profile_locks(CHROME_PROFILE_DIR)

    cmd = [
        _chrome_bin(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-extensions",
        "--disable-search-engine-choice-screen",
        "--disable-features=SigninIntercept,ChromeWhatsNewUI",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "https://suno.com",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--no-sandbox")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    if not _wait_cdp_ready(port, timeout_s=10.0):
        _stop_managed_browser(proc)
        raise RuntimeError(f"Managed browser CDP endpoint not ready on port {port}")
    return proc


def _stop_managed_browser(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.send_signal(signal.SIGKILL)
        except Exception:
            pass


def _build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suno",
        description="Suno skill runner with auto session/login flow and clean browser lifecycle.",
    )
    parser.add_argument("--lyrics", default="", help="Song lyrics (optional for auto/instrumental)")
    parser.add_argument("--styles", default="", help="Styles/tags (optional)")
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
    parser.add_argument("--song-title", default="Skill Song", help="Song title")
    parser.add_argument("--audio-type", default="", choices=["", "one_shot", "loop"], help="Sounds panel type")
    parser.add_argument("--bpm", default="", help="Sounds panel BPM ('auto' or integer)")
    parser.add_argument(
        "--key-root",
        default="",
        choices=["", "any", "c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"],
        help="Sounds panel key root",
    )
    parser.add_argument("--key-scale", default="", choices=["", "major", "minor"], help="Sounds panel key scale")
    parser.add_argument("--inspiration-clip-id", default="", help="Use as Inspiration source clip id (premium)")
    parser.add_argument("--inspiration-start-s", type=float, default=None, help="Inspiration source start second")
    parser.add_argument("--inspiration-end-s", type=float, default=None, help="Inspiration source end second")
    parser.add_argument(
        "--workspace",
        default="",
        help="Target workspace/project (id, slug, name, or 'default')",
    )
    parser.add_argument("--out-dir", default="", help="Output path override")
    parser.add_argument("--poll-attempts", type=int, default=None, help="Polling attempts (default from SUNO_POLL_ATTEMPTS)")
    parser.add_argument("--poll-interval", type=float, default=None, help="Polling interval (default from SUNO_POLL_INTERVAL)")
    parser.add_argument("--wait-seconds", type=float, default=180.0, help="Login wait timeout")
    parser.add_argument("--force-login", action="store_true", help="Always login before generation")
    parser.add_argument("--disable-p1-fallback", action="store_true", help="Disable P1 refresh via browser")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
    return parser


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _log(message: str) -> None:
    print(f"[suno] {message}")


def _run_wrapped(debug: bool, fn: Any, *args: Any, passthrough: bool = False, **kwargs: Any) -> Any:
    if debug or passthrough:
        return fn(*args, **kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*args, **kwargs)


def _persist_p1_into_capture(capture_file: Path, p1_token: str) -> None:
    if not p1_token.startswith("P1_"):
        return
    try:
        raw = json.loads(capture_file.read_text(encoding="utf-8"))
        request_obj = raw.get("request")
        if not isinstance(request_obj, dict):
            return
        post_data = request_obj.get("post_data")
        if not isinstance(post_data, str) or not post_data.strip():
            return
        body = json.loads(post_data)
        if not isinstance(body, dict):
            return
        body["token"] = p1_token
        request_obj["post_data"] = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        capture_file.write_text(json.dumps(raw, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        return


def _generate_command(argv: list[str]) -> int:
    args = _build_generate_parser().parse_args(argv)
    if not args.debug:
        os.environ.setdefault("NODE_NO_WARNINGS", "1")
        os.environ.setdefault("NODE_OPTIONS", "--no-deprecation --no-warnings")
    env = _load_env(ENV_PATH)

    lyrics_mode = str(args.lyrics_mode).strip().lower()
    lyrics_text = str(args.lyrics or "").strip()
    styles_text = str(args.styles or "").strip()
    exclude_styles_text = str(args.exclude_styles or "").strip()
    workspace_selector = str(args.workspace or "").strip()
    audio_type = str(args.audio_type or "").strip().lower()
    bpm_value = str(args.bpm or "").strip().lower()
    key_root = str(args.key_root or "").strip().lower()
    key_scale = str(args.key_scale or "").strip().lower()
    inspiration_clip_id = str(args.inspiration_clip_id or "").strip()

    requested_premium_features: list[str] = []
    if inspiration_clip_id:
        requested_premium_features.append("use_as_inspiration")

    if bpm_value and bpm_value != "auto":
        try:
            bpm_int = int(bpm_value)
        except Exception:
            _log("Ungueltiger BPM-Wert. Nutze 'auto' oder eine Ganzzahl.")
            return 1
        if bpm_int < 30 or bpm_int > 300:
            _log("Ungueltiger BPM-Bereich. Erlaubt: 30..300 oder 'auto'.")
            return 1

    # Mode-specific requirement: custom and mumble generation need explicit lyrics text.
    if lyrics_mode in {"custom", "mumble"} and not lyrics_text:
        _log("Fehlende Lyrics: In den Modi 'custom' und 'mumble' ist --lyrics erforderlich.")
        _log("Nutze fuer lyrics-freie Runs z. B. --lyrics-mode auto oder --lyrics-mode instrumental.")
        return 1

    if not env.get("SUNO_GOOGLE_EMAIL") or not env.get("SUNO_GOOGLE_PASSWORD"):
        _log("Fehlende Zugangsdaten. Bitte zuerst: hermes suno setup")
        return 1

    paths = _build_paths(env)
    wf = SunoWorkflow(paths)

    out_value = args.out_dir.strip() or env.get("SUNO_OUTPUT_DIR", "outputs/suno_plugin")
    out_dir = Path(out_value)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir

    poll_attempts_raw = env.get("SUNO_POLL_ATTEMPTS")
    # Backward-compatible typo fallback: some configs used ATTEMPS (missing 'T').
    if poll_attempts_raw is None and env.get("SUNO_POLL_ATTEMPS") is not None:
        poll_attempts_raw = env.get("SUNO_POLL_ATTEMPS")
        _log("Hinweis: Verwende SUNO_POLL_ATTEMPS als Fallback. Bitte auf SUNO_POLL_ATTEMPTS umstellen.")
    try:
        env_poll_attempts = max(1, int(str(poll_attempts_raw if poll_attempts_raw is not None else "60").strip() or "60"))
    except Exception:
        env_poll_attempts = 60
    effective_poll_attempts = max(1, int(args.poll_attempts)) if args.poll_attempts is not None else env_poll_attempts
    if args.poll_attempts is not None:
        _log(f"Polling attempts: CLI-Override {effective_poll_attempts} (ENV waere {env_poll_attempts}).")
    try:
        env_poll_interval = max(0.5, float(str(env.get("SUNO_POLL_INTERVAL", "4.0")).strip() or "4.0"))
    except Exception:
        env_poll_interval = 4.0
    effective_poll_interval = max(0.5, float(args.poll_interval)) if args.poll_interval is not None else env_poll_interval

    port = int(env.get("SUNO_CDP_PORT", "9222"))
    cdp_url = f"http://127.0.0.1:{port}"

    session_ok, reason = _has_valid_api_session(wf)
    if session_ok:
        _log("Session gueltig, starte direkte Generierung.")
    else:
        _log(f"Session ungueltig ({reason}), hole neue Anmeldung.")

    _log(
        "Generiere Song mit Parametern: "
        f"title='{args.song_title}', styles='{styles_text}', exclude='{exclude_styles_text}', "
        f"gender='{args.vocal_gender}', mode='{lyrics_mode}', weirdness={_clamp01(args.weirdness):.2f}, "
        f"style_influence={_clamp01(args.style_influence):.2f}, workspace='{workspace_selector or 'default'}'"
    )

    managed_browser: subprocess.Popen[str] | None = None
    use_browser_for_generate = bool(args.force_login and not session_ok)
    try:
        if args.force_login or not session_ok:
            _log("Logge mich bei Google ein...")
            managed_browser = _start_managed_browser(port)
            login_result = _run_wrapped(
                args.debug,
                asyncio.run,
                wf.login_via_cdp(
                    cdp_url=cdp_url,
                    wait_seconds=max(args.wait_seconds, 30.0),
                    target_url="https://suno.com",
                    close_browser_after_login=False,
                ),
            )
            if args.debug:
                _log(f"login result: {login_result}")
            if not login_result.get("ok"):
                login_error = str(login_result.get("error", ""))
                login_info = str(login_result.get("info", ""))
                if login_error == "authorization_denied" or "auth_denied:" in login_info or "access_denied" in login_info:
                    _log("Autorisierung fehlgeschlagen (Google-Login/2FA wurde abgelehnt).")
                    _log("Bitte versuche den Login erneut und bestaetige die Google-Autorisierung.")
                    return 1
                _log("Login fehlgeschlagen.")
                return 1
            _log("Login erfolgreich.")

            premium_info = _detect_premium_account(wf)
            if premium_info.get("is_premium") is True:
                _log("Abo-Status: Premium erkannt.")
            elif premium_info.get("is_premium") is False:
                _log("Abo-Status: Free erkannt.")
            else:
                _log(f"Abo-Status: unklar ({premium_info.get('reason', 'unknown')}).")

            # Desired mode: fetch fresh P1 while browser is open, then close browser before API generate.
            try:
                _log("Hole frischen P1-Token...")
                p1_token = _run_wrapped(
                    args.debug,
                    asyncio.run,
                    wf._harvest_p1_token_via_cdp(
                        cdp_url,
                        {
                            "lyrics": lyrics_text,
                            "styles": styles_text,
                            "exclude_styles": exclude_styles_text,
                            "song_title": args.song_title,
                        },
                    ),
                )
                if isinstance(p1_token, str) and p1_token.startswith("P1_"):
                    _persist_p1_into_capture(paths.manual_capture, p1_token)
                    _log("Frischer P1-Token gespeichert.")
            except Exception as exc:
                if args.debug:
                    _log(f"Konnte P1 nicht vorab holen: {exc}")

            _stop_managed_browser(managed_browser)
            managed_browser = None
            use_browser_for_generate = False
            _log("Browser nach Login geschlossen. Starte API-only Generierung.")

        premium_info = _detect_premium_account(wf)
        if premium_info.get("is_premium") is True:
            _log("Premium-Check: Account ist Premium.")
        elif premium_info.get("is_premium") is False:
            _log("Premium-Check: Account ist Free.")
        else:
            _log(f"Premium-Check: Status unklar ({premium_info.get('reason', 'unknown')}).")

        if requested_premium_features and premium_info.get("is_premium") is not True:
            _log(
                "Abbruch: Angeforderte Premium-Funktion(en) sind fuer diesen Account nicht verifizierbar. "
                f"Features={','.join(requested_premium_features)}"
            )
            _log("Hinweis: Bitte Premium aktivieren oder Premium-Funktionen aus dem Aufruf entfernen.")
            return 1

        params: dict[str, Any] = {
            "song_title": args.song_title,
            "lyrics": lyrics_text,
            "styles": styles_text,
            "exclude_styles": exclude_styles_text,
            "gender": args.vocal_gender,
            "lyrics_mode": lyrics_mode,
            "weirdness": _clamp01(args.weirdness),
            "style_influence": _clamp01(args.style_influence),
            "workspace": workspace_selector,
            "audio_type": audio_type,
            "bpm": bpm_value,
            "key_root": key_root,
            "key_scale": key_scale,
            "inspiration_clip_id": inspiration_clip_id,
            "inspiration_start_s": args.inspiration_start_s,
            "inspiration_end_s": args.inspiration_end_s,
        }

        # First try without browser dependency when session is already valid.
        try:
            _log("Starte API-Generierung...")
            _run_wrapped(
                args.debug,
                wf.generate_without_browser,
                passthrough=True,
                out_dir=out_dir,
                songs_count=1,
                poll_attempts=effective_poll_attempts,
                poll_interval=effective_poll_interval,
                browser_cdp_url=cdp_url if use_browser_for_generate else "",
                auto_refresh_p1=not args.disable_p1_fallback,
                song_params=params,
            )
            _log(f"Song(s) erfolgreich erstellt. Ausgabe: {out_dir}")
            return 0
        except Exception as exc:
            msg = str(exc)
            retryable = (
                "captcha token required" in msg
                or "Token validation failed" in msg
                or "status=422" in msg
                or "Failed to harvest fresh P1 token via CDP" in msg
            )
            if args.disable_p1_fallback or not retryable:
                _log(f"Fehler bei der Generierung: {exc}")
                raise

            if managed_browser is None:
                _log("Captcha/P1 erfordert Browser-Unterstuetzung, starte Browser...")
                managed_browser = _start_managed_browser(port)

            # Fast path: retry once immediately with browser-backed P1 refresh, no extra login cycle.
            try:
                _log("Erster Versuch fehlgeschlagen, starte Sofort-Retry mit Browser-P1...")
                _run_wrapped(
                    args.debug,
                    wf.generate_without_browser,
                    passthrough=True,
                    out_dir=out_dir,
                    songs_count=1,
                    poll_attempts=effective_poll_attempts,
                    poll_interval=effective_poll_interval,
                    browser_cdp_url=cdp_url,
                    auto_refresh_p1=True,
                    song_params=params,
                )
                _log(f"Song(s) erfolgreich erstellt. Ausgabe: {out_dir}")
                return 0
            except Exception as exc2:
                if args.debug:
                    _log(f"fast retry failed: {exc2}")

            # Last resort: refresh browser-side session once, then final retry.
            _log("Erneuere Browser-Session fuer P1...")
            login_result = _run_wrapped(
                args.debug,
                asyncio.run,
                wf.login_via_cdp(
                    cdp_url=cdp_url,
                    wait_seconds=max(args.wait_seconds, 30.0),
                    target_url="https://suno.com",
                    close_browser_after_login=False,
                ),
            )
            if args.debug:
                _log(f"fallback login result: {login_result}")
            if not login_result.get("ok"):
                login_error = str(login_result.get("error", ""))
                login_info = str(login_result.get("info", ""))
                if login_error == "authorization_denied" or "auth_denied:" in login_info or "access_denied" in login_info:
                    _log("Fallback-Login fehlgeschlagen: Google-Autorisierung wurde abgelehnt.")
                    _log("Bitte versuche den Login erneut und bestaetige die Google-Autorisierung.")
                    return 1
                _log("Fallback-Login fehlgeschlagen.")
                return 1

            _run_wrapped(
                args.debug,
                wf.generate_without_browser,
                passthrough=True,
                out_dir=out_dir,
                songs_count=1,
                poll_attempts=effective_poll_attempts,
                poll_interval=effective_poll_interval,
                browser_cdp_url=cdp_url,
                auto_refresh_p1=True,
                song_params=params,
            )
            _log(f"Song(s) erfolgreich erstellt. Ausgabe: {out_dir}")
            return 0
    finally:
        if managed_browser is not None:
            _log("Schliesse Browser und raeume auf...")
        _stop_managed_browser(managed_browser)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "setup":
        if len(argv) > 1 and argv[1] in {"-h", "--help"}:
            print(_setup_help())
            return 0
        return _setup_command()
    try:
        return _generate_command(argv)
    except KeyboardInterrupt:
        _log("Abgebrochen durch Benutzer.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
