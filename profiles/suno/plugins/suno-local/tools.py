from __future__ import annotations

import io
import importlib.util
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from . import suno
from .core.workflow import SunoWorkflow


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True)


def _read_summary(out_dir: str | Path | None) -> dict[str, Any] | None:
    if not out_dir:
        return None
    try:
        summary_path = Path(out_dir) / "summary.json"
        if not summary_path.exists():
            return None
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run_suno_main(argv: list[str]) -> dict[str, Any]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 1
    error = ""
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            exit_code = int(suno.main(argv))
    except Exception as exc:
        error = str(exc)
    return {
        "ok": exit_code == 0 and not error,
        "exit_code": exit_code,
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "error": error,
    }


def _profile_home() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        return Path(hermes_home)
    return Path.home() / ".hermes"


def _load_module_from_path(module_name: str, module_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_preview_state_module() -> Any:
    skill_dir = _profile_home() / "skills" / "suno-create"
    script_path = skill_dir / "preview_state.py"
    style_engineer_path = skill_dir / "style_engineer.py"
    if not script_path.exists():
        raise FileNotFoundError(f"preview_state.py not found: {script_path}")
    if not style_engineer_path.exists():
        raise FileNotFoundError(f"style_engineer.py not found: {style_engineer_path}")
    skill_dir_str = str(skill_dir)
    if skill_dir_str not in sys.path:
        sys.path.insert(0, skill_dir_str)
    # Force a fresh import from the active profile skill directory on every call.
    # The agent process is long-lived, so relying on sys.modules can leave stale
    # templates in memory even after the file changed on disk.
    _load_module_from_path("style_engineer", style_engineer_path)
    return _load_module_from_path("hermes_suno_preview_state", script_path)


def suno_status(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    try:
        env_values = suno._load_env(suno.ENV_PATH)
        paths = suno._build_paths(env_values)
        wf = SunoWorkflow(paths)
        valid, reason = suno._has_valid_api_session(wf)
        premium = suno._detect_premium_account(wf)
        return _json(
            {
                "ok": True,
                "session_valid": bool(valid),
                "reason": reason,
                "premium": premium,
                "env_path": str(suno.ENV_PATH),
                "has_env": suno.ENV_PATH.exists(),
                "storage_state": str(paths.storage_state),
                "auth_bundle": str(paths.auth_bundle),
                "manual_capture": str(paths.manual_capture),
            }
        )
    except Exception as exc:
        return _json({"ok": False, "error": f"status failed: {exc}"})


def suno_setup(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    try:
        current = suno._load_env(suno.ENV_PATH)
        updates: dict[str, str] = {}

        if isinstance(args.get("google_email"), str) and args["google_email"].strip():
            updates["SUNO_GOOGLE_EMAIL"] = args["google_email"].strip()
        if isinstance(args.get("google_password"), str) and args["google_password"].strip():
            updates["SUNO_GOOGLE_PASSWORD"] = args["google_password"].strip()
        if isinstance(args.get("output_dir"), str) and args["output_dir"].strip():
            updates["SUNO_OUTPUT_DIR"] = args["output_dir"].strip()
        if args.get("cdp_port") is not None:
            updates["SUNO_CDP_PORT"] = str(int(args["cdp_port"]))
        if args.get("poll_attempts") is not None:
            updates["SUNO_POLL_ATTEMPTS"] = str(max(1, int(args["poll_attempts"])))
        if args.get("poll_interval") is not None:
            updates["SUNO_POLL_INTERVAL"] = str(max(0.5, float(args["poll_interval"])))

        merged = dict(current)
        merged.update(updates)
        suno._write_env(suno.ENV_PATH, merged)

        return _json(
            {
                "ok": True,
                "env_path": str(suno.ENV_PATH),
                "updated": sorted(updates.keys()),
            }
        )
    except Exception as exc:
        return _json({"ok": False, "error": f"setup failed: {exc}"})


def suno_generate(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    try:
        argv: list[str] = []

        def add_str(name: str, flag: str) -> None:
            value = args.get(name)
            if isinstance(value, str) and value.strip():
                argv.extend([flag, value.strip()])

        def add_num(name: str, flag: str) -> None:
            value = args.get(name)
            if value is not None:
                argv.extend([flag, str(value)])

        def add_bool(name: str, flag: str) -> None:
            if bool(args.get(name)):
                argv.append(flag)

        add_str("lyrics", "--lyrics")
        add_str("styles", "--styles")
        add_str("exclude_styles", "--exclude-styles")
        add_str("vocal_gender", "--vocal-gender")
        add_str("lyrics_mode", "--lyrics-mode")
        add_num("weirdness", "--weirdness")
        add_num("style_influence", "--style-influence")
        add_str("song_title", "--song-title")
        add_str("audio_type", "--audio-type")
        add_str("bpm", "--bpm")
        add_str("key_root", "--key-root")
        add_str("key_scale", "--key-scale")
        add_str("inspiration_clip_id", "--inspiration-clip-id")
        add_num("inspiration_start_s", "--inspiration-start-s")
        add_num("inspiration_end_s", "--inspiration-end-s")
        add_str("workspace", "--workspace")
        add_str("out_dir", "--out-dir")
        add_num("poll_attempts", "--poll-attempts")
        add_num("poll_interval", "--poll-interval")
        add_num("wait_seconds", "--wait-seconds")
        add_bool("force_login", "--force-login")
        add_bool("disable_p1_fallback", "--disable-p1-fallback")
        add_bool("debug", "--debug")

        result = _run_suno_main(argv)
        if result["ok"]:
            out_dir = args.get("out_dir") or "outputs/suno_plugin"
            summary = _read_summary(out_dir)
            return _json(
                {
                    "ok": True,
                    "exit_code": result["exit_code"],
                    "out_dir": str(Path(out_dir)),
                    "summary": summary,
                    "stdout": result["stdout"],
                }
            )
        return _json(
            {
                "ok": False,
                "exit_code": result["exit_code"],
                "error": result["error"] or "generation failed",
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
        )
    except Exception as exc:
        return _json({"ok": False, "error": f"generate failed: {exc}"})


def suno_preview_state(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    try:
        module = _load_preview_state_module()
        action = str(args.get("action", "")).strip().lower()
        target = str(args.get("target", "")).strip()
        if not action:
            return _json({"ok": False, "error": "action is required"})
        if not target:
            return _json({"ok": False, "error": "target is required"})

        if action == "create":
            artist = str(args.get("artist", "")).strip()
            request = str(args.get("request", "")).strip()
            model = str(args.get("model", "")).strip() or module.DEFAULT_MODEL
            if not artist or not request:
                return _json({"ok": False, "error": "artist and request are required for create"})
            payload = module._create_preview(target, artist, request, model)
        elif action == "get":
            payload = module._read_state(target)
        elif action == "set_edit_mode":
            field = str(args.get("field", "")).strip().lower()
            if field not in module.VALID_EDIT_FIELDS:
                return _json({"ok": False, "error": f"invalid field: {field}"})
            payload = module._read_state(target)
            payload["edit_mode"] = field
            payload = module._write_state(target, payload)
        elif action == "apply_edit":
            message = str(args.get("message", ""))
            payload = module._apply_edit(target, message)
        elif action == "clear":
            payload = module._clear_state(target)
        else:
            return _json({"ok": False, "error": f"unsupported action: {action}"})
        if isinstance(payload, dict) and "ok" not in payload:
            payload = {"ok": True, **payload}
        return _json(payload)
    except Exception as exc:
        return _json({"ok": False, "error": f"preview_state failed: {exc}"})


def suno_feature_action(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    try:
        action = str(args.get("action", "")).strip().lower()
        if not action:
            return _json({"ok": False, "error": "action is required"})

        env_values = suno._load_env(suno.ENV_PATH)
        paths = suno._build_paths(env_values)
        wf = SunoWorkflow(paths)

        premium = suno._detect_premium_account(wf)
        premium_actions = {"use_inspiration", "get_stems", "create_voice", "download_wav"}
        if action in premium_actions and premium.get("is_premium") is not True:
            return _json(
                {
                    "ok": False,
                    "error": "premium_required",
                    "action": action,
                    "premium": premium,
                    "hint": "This action requires a premium account.",
                }
            )

        if action in {"generate_standard", "get_full_song", "extend", "cover", "mashup", "sample", "use_inspiration", "remaster", "reuse_prompt"}:
            song_params: dict[str, Any] = {
                "song_title": args.get("song_title") or "Skill Song",
                "lyrics": args.get("lyrics") or "",
                "styles": args.get("styles") or "",
                "exclude_styles": args.get("exclude_styles") or "",
                "gender": args.get("vocal_gender") or "m",
                "lyrics_mode": args.get("lyrics_mode") or "custom",
                "weirdness": args.get("weirdness") if args.get("weirdness") is not None else 0.5,
                "style_influence": args.get("style_influence") if args.get("style_influence") is not None else 0.5,
                "workspace": args.get("workspace") or "",
            }

            for k in ("audio_type", "bpm", "key_root", "key_scale", "inspiration_clip_id", "inspiration_start_s", "inspiration_end_s"):
                if args.get(k) is not None:
                    song_params[k] = args.get(k)

            clip_id = str(args.get("clip_id") or "").strip()
            if action in {"get_full_song", "extend"} and clip_id:
                song_params["continue_clip_id"] = clip_id
            if action == "cover" and clip_id:
                song_params["cover_clip_id"] = clip_id
            if action in {"mashup", "sample"} and clip_id:
                song_params["artist_clip_id"] = clip_id
            if action == "use_inspiration" and not song_params.get("inspiration_clip_id") and clip_id:
                song_params["inspiration_clip_id"] = clip_id
            if action == "remaster" and clip_id:
                song_params["cover_clip_id"] = clip_id
                if args.get("remaster_strength") is not None:
                    song_params["remaster_strength"] = args.get("remaster_strength")
                if args.get("remaster_model") is not None:
                    song_params["remaster_model"] = args.get("remaster_model")
            if action == "reuse_prompt" and clip_id:
                source = wf.get_songs_by_ids([clip_id])
                clips = source.get("clips") if isinstance(source, dict) else []
                src = clips[0] if isinstance(clips, list) and clips else {}
                if isinstance(src, dict):
                    song_params["lyrics"] = str(src.get("prompt") or song_params.get("lyrics") or "")
                    song_params["styles"] = str(src.get("tags") or song_params.get("styles") or "")
                    song_params["exclude_styles"] = str(src.get("negative_tags") or song_params.get("exclude_styles") or "")
                    song_params["song_title"] = str(src.get("title") or song_params.get("song_title") or "Skill Song")

            out_dir = args.get("out_dir") or "outputs/suno_plugin"
            summary = wf.generate_without_browser(
                out_dir=Path(out_dir),
                songs_count=1,
                poll_attempts=int(args.get("poll_attempts") or 20),
                poll_interval=float(args.get("poll_interval") or 6.0),
                browser_cdp_url="",
                auto_refresh_p1=True,
                song_params=song_params,
            )
            return _json({"ok": True, "action": action, "premium": premium, "summary": summary})

        if action == "adjust_speed":
            clip_id = str(args.get("clip_id") or "").strip()
            speed = float(args.get("speed") if args.get("speed") is not None else 1.0)
            keep_pitch = args.get("keep_pitch")
            result = wf.adjust_speed(clip_id, speed, keep_pitch if isinstance(keep_pitch, bool) else None)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "crop":
            clip_id = str(args.get("clip_id") or "").strip()
            start_s = float(args.get("crop_start_s") if args.get("crop_start_s") is not None else 0.0)
            end_s = float(args.get("crop_end_s") if args.get("crop_end_s") is not None else 0.0)
            result = wf.crop_clip(clip_id, start_s, end_s)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "get_stems":
            clip_id = str(args.get("clip_id") or "").strip()
            mode = str(args.get("stems_mode") or "all_detected")
            result = wf.get_stems(clip_id, mode)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "create_voice":
            clip_id = str(args.get("clip_id") or "").strip()
            name = str(args.get("voice_name") or "").strip()
            result = wf.create_voice(
                clip_id=clip_id,
                name=name,
                styles=str(args.get("voice_styles") or ""),
                description=str(args.get("voice_description") or ""),
                is_public=bool(args.get("voice_public", False)),
                start_s=float(args.get("voice_start_s")) if args.get("voice_start_s") is not None else None,
                end_s=float(args.get("voice_end_s")) if args.get("voice_end_s") is not None else None,
            )
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action in {"download_mp3", "download_wav", "download_video"}:
            clip_id = str(args.get("clip_id") or "").strip()
            kind = "mp3" if action == "download_mp3" else ("wav" if action == "download_wav" else "video")
            out_dir = Path(args.get("out_dir") or "outputs/suno_plugin/downloads")
            result = wf.download_clip_asset(clip_id, kind, out_dir)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "list_workspaces":
            result = wf.list_workspaces(
                page=int(args.get("page") or 1),
                include_trashed=bool(args.get("include_trashed", False)),
                exclude_shared=bool(args.get("exclude_shared", False)),
            )
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "list_songs":
            result = wf.list_songs(
                page=int(args.get("page") or 1),
                include_trashed=bool(args.get("include_trashed", False)),
                exclude_shared=bool(args.get("exclude_shared", False)),
                workspace_selector=str(args.get("workspace") or ""),
                limit=int(args.get("limit") or 50),
            )
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "set_visibility":
            clip_id = str(args.get("clip_id") or "").strip()
            updates: dict[str, Any] = {}
            if args.get("allow_comments") is not None:
                val = bool(args.get("allow_comments"))
                updates["allow_comments"] = val
                updates["comments_enabled"] = val
            if args.get("allow_remixes") is not None:
                val = bool(args.get("allow_remixes"))
                updates["allow_remixes"] = val
                updates["remix_enabled"] = val
            if args.get("pin_to_profile") is not None:
                val = bool(args.get("pin_to_profile"))
                updates["pin_to_profile"] = val
                updates["is_pinned"] = val
            result = wf.set_clip_metadata(clip_id, updates)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "publish":
            clip_id = str(args.get("clip_id") or "").strip()
            is_public = bool(args.get("is_public", True))
            updates = {"is_public": is_public, "published": is_public}
            result = wf.set_clip_metadata(clip_id, updates)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "move_to_workspace":
            ids = args.get("clip_ids") if isinstance(args.get("clip_ids"), list) else []
            if not ids and isinstance(args.get("clip_id"), str):
                ids = [args.get("clip_id")]
            workspace = str(args.get("workspace") or "").strip()
            result = wf.move_to_workspace(ids, workspace)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "add_to_playlist":
            playlist_name = str(args.get("playlist_name") or "").strip()
            result = wf.create_playlist(playlist_name)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        if action == "move_to_trash":
            ids = args.get("clip_ids") if isinstance(args.get("clip_ids"), list) else []
            if not ids and isinstance(args.get("clip_id"), str):
                ids = [args.get("clip_id")]
            result = wf.move_to_trash(ids)
            return _json({"ok": bool(result.get("ok")), "action": action, "premium": premium, "result": result})

        return _json({"ok": False, "error": f"unsupported action: {action}"})
    except Exception as exc:
        return _json({"ok": False, "error": f"feature action failed: {exc}"})
