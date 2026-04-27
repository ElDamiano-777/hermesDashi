from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import paths

STRATEGY_FEEDBACK_PATH = paths.YOUTUBE_DATA_DIR / "state" / "strategy_feedback.json"


def load_strategy_feedback() -> dict[str, Any]:
    if not STRATEGY_FEEDBACK_PATH.exists():
        return {"enabled": False}
    try:
        return json.loads(STRATEGY_FEEDBACK_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


def feedback_summary() -> dict[str, Any]:
    data = load_strategy_feedback()
    hypotheses = data.get("hypotheses", [])
    learnings = data.get("research_learnings", [])
    experiments = data.get("experiments", [])
    return {
        "path": str(STRATEGY_FEEDBACK_PATH),
        "exists": Path(STRATEGY_FEEDBACK_PATH).exists(),
        "enabled": bool(data.get("enabled", hypotheses or learnings or experiments)),
        "hypotheses": len(hypotheses) if isinstance(hypotheses, list) else 0,
        "research_learnings": len(learnings) if isinstance(learnings, list) else 0,
        "experiments": len(experiments) if isinstance(experiments, list) else 0,
        "priority_assets": data.get("priority_assets", []),
    }

