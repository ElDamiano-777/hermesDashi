from __future__ import annotations

import os
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_ACTIVE_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
HERMES_HOME = (
    _ACTIVE_HERMES_HOME.parent.parent
    if _ACTIVE_HERMES_HOME.parent.name == "profiles"
    else _ACTIVE_HERMES_HOME
)
DATA_DIR = HERMES_HOME / "data" / "source-intelligence"
YOUTUBE_DATA_DIR = DATA_DIR / "youtube"
NORMALIZED_SOURCES_DIR = DATA_DIR / "sources"
REPORTS_DIR = DATA_DIR / "reports"


def ensure_base_dirs() -> None:
    for path in (
        DATA_DIR,
        YOUTUBE_DATA_DIR,
        NORMALIZED_SOURCES_DIR,
        REPORTS_DIR,
        YOUTUBE_DATA_DIR / "config",
        YOUTUBE_DATA_DIR / "state",
        YOUTUBE_DATA_DIR / "reports",
        YOUTUBE_DATA_DIR / "transcripts",
    ):
        path.mkdir(parents=True, exist_ok=True)
