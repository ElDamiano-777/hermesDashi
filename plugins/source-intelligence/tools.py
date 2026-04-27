from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .source_intelligence import paths
from .source_intelligence.domains import trading
from .source_intelligence.sources import youtube_pipeline


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True)


def _capture(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            result = fn(*args, **kwargs)
        return {
            "ok": True,
            "result": result,
            "stdout": out_buf.getvalue(),
            "stderr": err_buf.getvalue(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "stdout": out_buf.getvalue(),
            "stderr": err_buf.getvalue(),
        }


def _read_status_from_stdout(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("stdout", "").strip()
    if not raw:
        return payload
    try:
        parsed = json.loads(raw)
    except Exception:
        return payload
    return {**payload, "status": parsed}


def source_intelligence_status(args: dict[str, Any], **kwargs: Any) -> str:
    del args, kwargs
    paths.ensure_base_dirs()
    youtube = _read_status_from_stdout(_capture(youtube_pipeline.status))
    return _json({
        "ok": bool(youtube.get("ok")),
        "hermes_home": str(paths.HERMES_HOME),
        "plugin_dir": str(paths.PLUGIN_DIR),
        "data_dir": str(paths.DATA_DIR),
        "youtube_data_dir": str(paths.YOUTUBE_DATA_DIR),
        "normalized_sources_dir": str(paths.NORMALIZED_SOURCES_DIR),
        "trading_feedback": trading.feedback_summary(),
        "youtube": youtube,
    })


def source_intelligence_youtube(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    action = str(args.get("action", "status")).strip().lower()
    paths.ensure_base_dirs()

    if action == "status":
        return _json(_read_status_from_stdout(_capture(youtube_pipeline.status)))

    if action == "run":
        return _json(_capture(youtube_pipeline.run_pipeline))

    if action == "queue":
        url = str(args.get("url", "")).strip()
        if not url:
            return _json({"ok": False, "error": "url is required for action=queue"})
        kind = str(args.get("kind", "auto")).strip().lower()
        if kind == "auto":
            kind = "video" if youtube_pipeline.looks_like_video_url(url) else "channel"
        if kind not in {"video", "channel"}:
            return _json({"ok": False, "error": f"invalid kind: {kind}"})
        run = bool(args.get("run", True))
        force = bool(args.get("force", False))
        return _json(_capture(
            youtube_pipeline.queue_manual_input,
            kind,
            url,
            run_full_pipeline=run,
            force=force,
        ))

    return _json({"ok": False, "error": f"unsupported action: {action}"})


def _slug(value: str, fallback: str = "source") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or fallback


def source_intelligence_ingest(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    paths.ensure_base_dirs()
    source_type = str(args.get("source_type", "text")).strip() or "text"
    text = str(args.get("text", ""))
    if not text.strip():
        return _json({"ok": False, "error": "text is required"})

    url = str(args.get("url", "")).strip()
    title = str(args.get("title", "")).strip()
    domain = str(args.get("domain", "")).strip()
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}

    digest = hashlib.sha256((url or title or text[:200]).encode("utf-8")).hexdigest()[:16]
    name = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{_slug(title or source_type)}-{digest}.json"
    path = paths.NORMALIZED_SOURCES_DIR / name
    payload = {
        "schema_version": 1,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "source_type": source_type,
        "url": url,
        "title": title,
        "domain": domain,
        "text": text,
        "metadata": metadata,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _json({"ok": True, "path": str(path), "bytes": path.stat().st_size})

