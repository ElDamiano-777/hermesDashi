from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
TOKEN_KEY_HINTS = {
    "token",
    "access_token",
    "id_token",
    "refresh_token",
    "authorization",
    "auth",
    "session",
    "jwt",
}


def _looks_sensitive_key(name: str) -> bool:
    n = name.lower()
    return any(hint in n for hint in TOKEN_KEY_HINTS)


def _extract_candidates_from_mapping(mapping: dict[str, Any], source: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for key, value in mapping.items():
        if not isinstance(value, str):
            continue
        val = value.strip()
        if not val:
            continue
        if JWT_RE.fullmatch(val) or _looks_sensitive_key(key):
            out.append({"source": source, "key": key, "value": val})
    return out


def build_auth_bundle(storage_state: dict[str, Any]) -> dict[str, Any]:
    cookies = storage_state.get("cookies", [])
    origins = storage_state.get("origins", [])

    candidates: list[dict[str, str]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", ""))
        value = cookie.get("value")
        if isinstance(value, str) and (JWT_RE.fullmatch(value) or _looks_sensitive_key(name)):
            candidates.append({"source": "cookie", "key": name, "value": value})

    for origin in origins:
        if not isinstance(origin, dict):
            continue
        origin_url = str(origin.get("origin", ""))
        local_storage = origin.get("localStorage", [])
        if isinstance(local_storage, list):
            mapping: dict[str, Any] = {}
            for item in local_storage:
                if isinstance(item, dict):
                    mapping[str(item.get("name", ""))] = item.get("value")
            candidates.extend(_extract_candidates_from_mapping(mapping, f"localStorage:{origin_url}"))

    dedup: dict[tuple[str, str], dict[str, str]] = {}
    for item in candidates:
        k = (item.get("key", ""), item.get("value", ""))
        if k not in dedup:
            dedup[k] = item

    all_candidates = list(dedup.values())
    preferred = None
    if all_candidates:
        jwt_candidates = [c for c in all_candidates if JWT_RE.fullmatch(c["value"])]
        preferred = (jwt_candidates or all_candidates)[0]

    suggested_headers: dict[str, str] = {}
    if preferred:
        val = preferred["value"]
        if not val.lower().startswith("bearer "):
            val = f"Bearer {val}"
        suggested_headers["Authorization"] = val

    return {
        "cookies": cookies,
        "origins": origins,
        "token_candidates": all_candidates,
        "preferred_token": preferred,
        "suggested_headers": suggested_headers,
    }


def write_auth_bundle(storage_state_path: Path, bundle_path: Path) -> dict[str, Any]:
    data = json.loads(storage_state_path.read_text(encoding="utf-8"))
    bundle = build_auth_bundle(data)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=True), encoding="utf-8")
    return bundle
