#!/usr/bin/env python3
"""
Market Scanner — Crypto Trading Bot Fundament
==============================================
Holt Live-Daten, berechnet Indikatoren, generiert Signale,
verwaltet Paper-Trades und trackt Performance.

Nutzung:
  python3 market_scanner.py scan          # Markt-Scan mit Signalen
  python3 market_scanner.py report        # Tages-Report
  python3 market_scanner.py optimize      # Wöchentliche Optimierung
  python3 market_scanner.py status        # Aktueller Portfolio-Status
"""

import json
import sys
import os
import time
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import ccxt
except ImportError:
    ccxt = None
try:
    import numpy as np
except ImportError:
    np = None
try:
    import pandas as pd
except ImportError:
    pd = None

# ── Konfiguration ────────────────────────────────────────────────────────────

TRADER_HOME = Path(os.getenv("TRADER_HOME", Path(__file__).resolve().parents[1]))
DATA_DIR = TRADER_HOME / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Dateien
TRADES_ACTIVE = DATA_DIR / "trades_active.json"
TRADES_HISTORY = DATA_DIR / "trades_history.json"
SCAN_LOG = DATA_DIR / "scan_log.json"
PERFORMANCE = DATA_DIR / "performance.json"
CONFIG_FILE = DATA_DIR / "scanner_config.json"
HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
PROFILE_RESEARCH_FEEDBACK = DATA_DIR / "strategy_feedback.json"
GLOBAL_RESEARCH_FEEDBACK = HERMES_HOME / "data" / "source-intelligence" / "youtube" / "state" / "strategy_feedback.json"
RESEARCH_FEEDBACK = Path(os.getenv(
    "TRADER_RESEARCH_FEEDBACK",
    str(GLOBAL_RESEARCH_FEEDBACK if GLOBAL_RESEARCH_FEEDBACK.exists() else PROFILE_RESEARCH_FEEDBACK),
))
RUNTIME_STATE = DATA_DIR / "runtime_state.json"
GATEWAY_STATE_FILE = TRADER_HOME / "gateway_state.json"
CRON_JOBS_FILE = TRADER_HOME / "cron" / "jobs.json"
EXPERIMENT_TRACKING = DATA_DIR / "strategy_experiment_tracking.json"
EXPERIMENT_TRIALS = DATA_DIR / "strategy_experiment_trials.json"
OPTIMIZER_RECOMMENDATIONS = DATA_DIR / "strategy_optimizer_recommendations.json"
OPTIMIZER_DECISIONS = DATA_DIR / "strategy_optimizer_decisions.json"
# Hypothesis tracking — separate from raw research feedback.
# Written by the scanner; never by youtube_pipeline.
HYPOTHESIS_TRACKING = DATA_DIR / "strategy_hypothesis_tracking.json"

# Default Config
DEFAULT_CONFIG = {
    "watchlist": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
                  "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT", "POL/USDT"],
    "excluded_assets": [],
    "timeframe": "4h",
    "exchange": "okx",
    "initial_capital": 1000.0,
    "risk_per_trade": 0.02,       # 2%
    "max_positions": 3,
    "atr_stop_multiplier": 1.5,
    "atr_tp1_multiplier": 2.0,
    "atr_tp2_multiplier": 3.0,
    "ema_fast": 21,
    "ema_slow": 55,
    "rsi_period": 14,
    "atr_period": 14,
    "min_signal_strength": 0.3,
    "min_volume_ratio": 1.0,      # Volume muss über 20-Perioden-Durchschnitt sein
    "min_rr_ratio": 1.5,
    "max_exposure_multiple": 2.0,
    "entry_fee_bps": 10,
    "exit_fee_bps": 10,
    "slippage_bps": 5,
    "spread_bps": 3,
    "tp1_take_fraction": 0.5,
    "daily_loss_limit_pct": 5.0,
    "weekly_loss_limit_pct": 10.0,
    "monthly_loss_limit_pct": 15.0,
    "max_portfolio_drawdown_pct": 15.0,
    "crash_daily_drop_pct": 6.0,
    "crash_cumulative_drop_pct": 10.0,
    "crash_lookback_days": 3,
    "crash_volume_multiplier": 1.5,
}


def require_dependencies(*deps):
    missing = []
    for dep in deps:
        if dep == "ccxt" and ccxt is None:
            missing.append("ccxt")
        elif dep == "numpy" and np is None:
            missing.append("numpy")
        elif dep == "pandas" and pd is None:
            missing.append("pandas")
    if missing:
        raise RuntimeError(
            "Missing runtime dependencies: "
            + ", ".join(missing)
            + ". Use the Hermes venv wrapper or install them in the active interpreter."
        )


def load_config():
    """Lade oder erstelle Config."""
    changed = False
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    else:
        cfg = DEFAULT_CONFIG.copy()
        changed = True

    # Merge mit Defaults für neue Keys
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True

    normalized_exclusions = _normalize_excluded_assets(cfg.get("excluded_assets", []))
    if cfg.get("excluded_assets") != normalized_exclusions:
        cfg["excluded_assets"] = normalized_exclusions
        changed = True

    filtered_watchlist = [s for s in cfg.get("watchlist", []) if not is_excluded_asset(cfg, s)]
    if filtered_watchlist != cfg.get("watchlist", []):
        cfg["watchlist"] = filtered_watchlist
        changed = True

    if changed:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)

    return apply_research_overrides(cfg)


def load_json(path, default=None):
    """Lade JSON-Datei oder return Default."""
    if default is None:
        default = []
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    """Speichere JSON-Datei."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_runtime_state():
    raw = load_json(RUNTIME_STATE, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("commands", {})
    raw.setdefault("updated_at", None)
    return raw


def save_runtime_state(state):
    state["updated_at"] = utc_now_iso()
    save_json(RUNTIME_STATE, state)


def mark_command_started(command):
    state = load_runtime_state()
    section = state["commands"].setdefault(command, {})
    section["status"] = "running"
    section["last_started_at"] = utc_now_iso()
    save_runtime_state(state)


def mark_command_finished(command, ok, summary=None, error=None):
    state = load_runtime_state()
    section = state["commands"].setdefault(command, {})
    finished_at = utc_now_iso()
    section["status"] = "ok" if ok else "error"
    section["last_finished_at"] = finished_at
    if ok:
        section["last_success_at"] = finished_at
        section["last_error"] = None
        section["last_error_at"] = None
    else:
        section["last_error"] = error
        section["last_error_at"] = finished_at
    if summary is not None:
        section["last_result"] = summary
    started_at = section.get("last_started_at")
    if started_at:
        try:
            duration = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
            section["last_duration_seconds"] = round(duration.total_seconds(), 2)
        except Exception:
            pass
    save_runtime_state(state)


def load_gateway_state():
    raw = load_json(GATEWAY_STATE_FILE, {})
    return raw if isinstance(raw, dict) else {}


def load_cron_jobs_state():
    raw = load_json(CRON_JOBS_FILE, {})
    if not isinstance(raw, dict):
        return {"jobs": [], "updated_at": None}
    jobs = raw.get("jobs", [])
    return {
        "jobs": jobs if isinstance(jobs, list) else [],
        "updated_at": raw.get("updated_at"),
    }


def build_runtime_snapshot(feedback=None):
    feedback = feedback or normalize_research_feedback(load_research_feedback())
    runtime = load_runtime_state()
    cron_state = load_cron_jobs_state()
    gateway_state = load_gateway_state()
    cron_jobs = cron_state.get("jobs", [])
    enabled_jobs = [j for j in cron_jobs if isinstance(j, dict) and j.get("enabled", True)]
    return {
        "intake_analysis": {
            "last_generated_at": feedback.get("generated_at"),
            "learning_count": len(feedback.get("research_learnings", [])),
            "experiment_count": len(feedback.get("experiments", [])),
            "hypothesis_count": len(feedback.get("hypotheses", [])),
        },
        "commands": runtime.get("commands", {}),
        "cron": {
            "jobs_total": len(cron_jobs),
            "jobs_enabled": len(enabled_jobs),
            "updated_at": cron_state.get("updated_at"),
            "next_runs": [
                {
                    "name": j.get("name"),
                    "next_run_at": j.get("next_run_at"),
                    "last_run_at": j.get("last_run_at"),
                    "last_status": j.get("last_status"),
                }
                for j in enabled_jobs[:6]
            ],
        },
        "gateway": {
            "state": gateway_state.get("gateway_state"),
            "updated_at": gateway_state.get("updated_at"),
            "telegram_state": ((gateway_state.get("platforms") or {}).get("telegram") or {}).get("state"),
        },
    }


def load_performance(config):
    """Lade Performance-State mit stabilen Defaults."""
    perf = load_json(PERFORMANCE, {})
    if not isinstance(perf, dict):
        perf = {}
    initial_capital = config["initial_capital"]
    perf.setdefault("capital", initial_capital)
    perf.setdefault("trades_total", 0)
    perf.setdefault("peak_capital", max(initial_capital, perf["capital"]))
    perf.setdefault("max_drawdown_pct", 0.0)
    return perf


def save_performance(perf):
    save_json(PERFORMANCE, perf)


def apply_capital_delta(config, delta, trade_closed=False):
    """Buche realisierte PnL/Fees ins Portfolio und aktualisiere Drawdown."""
    perf = load_performance(config)
    perf["capital"] = round(perf.get("capital", config["initial_capital"]) + delta, 2)
    if trade_closed:
        perf["trades_total"] = perf.get("trades_total", 0) + 1

    peak_capital = max(perf.get("peak_capital", config["initial_capital"]), perf["capital"])
    perf["peak_capital"] = round(peak_capital, 2)
    if peak_capital > 0:
        current_drawdown = max(0.0, (peak_capital - perf["capital"]) / peak_capital * 100)
        perf["max_drawdown_pct"] = round(max(perf.get("max_drawdown_pct", 0.0), current_drawdown), 2)

    save_performance(perf)
    return perf


def load_research_feedback():
    """Load raw research feedback JSON. Returns empty dict on missing/corrupt file."""
    raw = load_json(RESEARCH_FEEDBACK, {})
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Research Adapter Layer
# ---------------------------------------------------------------------------
# REGRESSION GUARD: youtube_pipeline.py is the single research source of truth.
# Raw strategy_feedback.json must NEVER be read outside this adapter block.
# All research→scanner translation happens here and ONLY here.
# The rest of the scanner reads config['_scanner_hints'] — never the raw JSON.
#
# Design principles:
#   1. Hypotheses are RESEARCH ARTIFACTS, not trading signals.
#      Scanner validates independently; risk guardrails always override research.
#   2. Research may bias candidate ordering only — it does NOT force trades.
#   3. All numeric influence is deterministic and capped.
#   4. No hypothesis field is read outside this block.
#   5. If research is absent, empty, or low-quality → no influence (safe default).
#   6. Research is WEAKER than every scanner guardrail. Risk logic always wins.
# ---------------------------------------------------------------------------

# Canonical set of valid tiers from the youtube_pipeline schema.
# Hypotheses with any other tier value are treated as 'not_actionable'.
_VALID_TIERS = frozenset({
    'potential_strategy', 'testable_hypothesis', 'idea_only', 'not_actionable',
})

# Tier weights for confidence-weighted asset bias aggregation.
# Tiers not in this dict are excluded from asset bias entirely.
_RESEARCH_TIER_WEIGHTS = {
    'potential_strategy': 1.0,
    'testable_hypothesis': 0.6,
    'idea_only': 0.2,
}

# Maximum bias any single asset may receive from research.
# At +0.15 this is a tiebreaker for candidate ORDERING only.
# It does NOT change min_signal_strength or any other numeric threshold.
_MAX_ASSET_BIAS = 0.15

# Hypothesis quality gates — all must pass or hypothesis is excluded.
_MIN_HYPOTHESIS_CONFIDENCE = 0.4
_MAX_HYPOTHESIS_HYPE_RATIO = 0.6
_MAX_HYPOTHESIS_VALIDATION_GAPS = 2


def _coerce_float(value, default, lo=None, hi=None):
    """Safely cast value to float, return default on any failure, optionally clamp."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _normalize_experiment_asset(asset):
    if not isinstance(asset, str):
        return None
    raw = asset.strip().upper()
    if not raw:
        return None
    compact = raw.replace(" ", "")
    if "/" in compact:
        return compact
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if len(compact) > len(quote) and compact.endswith(quote):
            base = compact[:-len(quote)]
            if base:
                return f"{base}/{quote}"
    return compact


def _normalize_excluded_assets(items):
    out = []
    seen = set()
    for item in (items or []):
        if not isinstance(item, str):
            continue
        raw = item.strip().upper()
        normalized = _normalize_experiment_asset(raw) or raw
        base = normalized.split('/')[0] if '/' in normalized else normalized
        for candidate in (raw, normalized, base):
            if candidate and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def is_excluded_asset(config, asset_or_symbol):
    normalized = _normalize_experiment_asset(asset_or_symbol)
    raw = str(asset_or_symbol or '').strip().upper()
    if not normalized and not raw:
        return False
    base = normalized.split('/')[0] if normalized and '/' in normalized else (normalized or raw)
    exclusions = set(_normalize_excluded_assets(config.get('excluded_assets', [])))
    for candidate in (raw, normalized, base):
        if candidate and candidate in exclusions:
            return True
    return False


def _normalize_hypothesis(h):
    """Normalize a single hypothesis dict to a stable internal schema.

    This is the explicit research contract for hypothesis fields.
    All downstream adapter code relies ONLY on the output of this function —
    never on raw field names from youtube_pipeline directly.

    Normalisation rules:
      - Non-dict input               → returns None (caller must skip)
      - tier not in _VALID_TIERS     → 'not_actionable'
      - confidence                   → float, clamped [0.0, 1.0], default 0.0
      - hype_ratio                   → float, clamped [0.0, 1.0], default 1.0 (pessimistic)
      - assets                       → list[str], uppercase tickers, deduplicated
      - validation_gaps / weaknesses
        / overfitting_flags          → list, empty list on missing/wrong type
      - logic_quality                → str, normalised from either 'logic_quality'
                                       or 'logic_complexity' field name

    Fields not used by the adapter are passed through unchanged so callers can
    display them (e.g. title, description, entry_logic) without further parsing.
    """
    if not isinstance(h, dict):
        return None

    tier = h.get('tier', 'not_actionable')
    if tier not in _VALID_TIERS:
        tier = 'not_actionable'

    confidence = _coerce_float(h.get('confidence', 0.0), default=0.0, lo=0.0, hi=1.0)
    # Pessimistic default: unknown hype_ratio is treated as fully promotional.
    hype_ratio = _coerce_float(h.get('hype_ratio', 1.0), default=1.0, lo=0.0, hi=1.0)

    def _to_list(val):
        if isinstance(val, list):
            return val
        return []

    assets_raw = _to_list(h.get('assets'))
    assets = list(dict.fromkeys(
        a.strip().upper() for a in assets_raw if isinstance(a, str) and a.strip()
    ))

    # Accept both field name variants used across pipeline versions.
    logic_quality = h.get('logic_quality') or h.get('logic_complexity') or 'low'

    return {
        # Identity / display (passed through, not interpreted)
        'url': h.get('url', ''),
        'title': h.get('title', ''),
        'channel': h.get('channel', ''),
        'description': h.get('description') or h.get('strategy_description'),
        'entry_logic': h.get('entry_logic'),
        'exit_logic': h.get('exit_logic'),
        # Normalised adapter fields
        'tier': tier,
        'confidence': confidence,
        'hype_ratio': hype_ratio,
        'assets': assets,
        'validation_gaps': _to_list(h.get('validation_gaps')),
        'weaknesses': _to_list(h.get('weaknesses')),
        'overfitting_flags': _to_list(h.get('overfitting_flags')),
        'falsifiability': h.get('falsifiability', 'untestable'),
        'completeness': _coerce_float(h.get('completeness', 0), default=0.0, lo=0.0),
        'logic_quality': logic_quality,
    }


def _filter_valid_hypotheses(hypotheses):
    """Return only hypotheses that pass all quality gates.

    Input is a list of already-normalized hypothesis dicts (from _normalize_hypothesis).
    Exclusion rules (any one fails → excluded):
      - tier == 'not_actionable'   analyzer explicitly rejected this
      - confidence < 0.4           low-quality signal from analyzer
      - overfitting_flags present  curve-fit or look-ahead risk detected
      - hype_ratio > 0.6           >60% promotional content
      - validation_gaps > 2        strategy cannot be evaluated cleanly

    Conservative intent: only hypotheses that survived adversarial review
    AND have meaningful confidence contribute to scanner hints.
    Hypotheses are RESEARCH ARTIFACTS — scanner still validates all signals
    independently before any order is considered.
    """
    valid = []
    for h in (hypotheses or []):
        # Already normalized — all fields have correct types, no float() casts needed.
        if h['tier'] == 'not_actionable':
            continue
        if h['confidence'] < _MIN_HYPOTHESIS_CONFIDENCE:
            continue
        if h['overfitting_flags']:  # non-empty list → exclude
            continue
        if h['hype_ratio'] > _MAX_HYPOTHESIS_HYPE_RATIO:
            continue
        if len(h['validation_gaps']) > _MAX_HYPOTHESIS_VALIDATION_GAPS:
            continue
        valid.append(h)
    return valid


def _asset_bias_from_hypotheses(valid_hypotheses):
    """Compute confidence-weighted asset bias from valid hypotheses.

    Maps bare research tickers ('BTC', 'ETH') → exchange symbols ('BTC/USDT')
    and aggregates confidence * tier_weight across all hypotheses per asset.

    Bias is normalized so the strongest asset gets exactly _MAX_ASSET_BIAS.
    All others scale proportionally below that cap.

    Returns dict: {'BTC/USDT': 0.12, 'ETH/USDT': 0.07, ...}

    NOTE: This produces a candidate ORDERING hint only. It does not change any
    numeric thresholds and does not add symbols to the watchlist.
    """
    raw: dict = {}
    for h in valid_hypotheses:
        tier_w = _RESEARCH_TIER_WEIGHTS.get(h['tier'], 0.0)
        if tier_w == 0.0:
            continue
        weight = h['confidence'] * tier_w
        for asset in h['assets']:
            # Assets already normalized to uppercase by _normalize_hypothesis.
            # Convert to exchange symbol format for scanner compatibility.
            symbol = asset if '/' in asset else f"{asset}/USDT"
            raw[symbol] = raw.get(symbol, 0.0) + weight

    if not raw:
        return {}
    max_w = max(raw.values())
    if max_w <= 0:
        return {}
    return {sym: round(w / max_w * _MAX_ASSET_BIAS, 4) for sym, w in raw.items()}


def _global_caution_factor(all_hypotheses, valid_hypotheses):
    """Compute a [0.0, 1.0] factor that scales all research influence globally.

    If overall research quality is low (few valid hypotheses), research influence
    drops toward zero rather than shaping scanner behavior with weak data.

    Logic:
      - No hypotheses → 0.0 (no influence)
      - valid_ratio = len(valid) / len(all)
      - caution = min(1.0, valid_ratio / 0.30)
        → 0% valid → 0.0, 30%+ valid → 1.0 (linear ramp)

    Conservative intent: bad research = no influence, not bad trades.
    """
    n_all = len(all_hypotheses or [])
    n_valid = len(valid_hypotheses or [])
    if n_all == 0 or n_valid == 0:
        return 0.0
    return round(min(1.0, (n_valid / n_all) / 0.30), 4)


def normalize_research_feedback(raw):
    """Convert raw research JSON into a stable internal structure.

    This is the SINGLE point of contact with the youtube_pipeline output schema.
    No other function in this file reads raw hypothesis field names directly.

    Top-level research contract (youtube_pipeline strategy_feedback.json):
      generated_at      — ISO timestamp of the pipeline run (str | None)
      summary           — aggregate counts and avg_confidence (dict)
      priority_assets   — top assets by hypothesis coverage (list[str])
      hypotheses        — list of hypothesis records (list[dict])

    Each hypothesis is normalized by _normalize_hypothesis() before any
    downstream adapter code accesses it. See that function for the per-field
    contract. Malformed hypotheses are silently dropped.
    """
    if not isinstance(raw, dict):
        return {
            'hypotheses': [],
            'research_learnings': [],
            'experiments': [],
            'priority_assets': [],
            'summary': {},
            'generated_at': None,
        }

    raw_hyps = raw.get('hypotheses')
    raw_hyps = raw_hyps if isinstance(raw_hyps, list) else []
    # Normalize each hypothesis; drop any that fail (None return from _normalize_hypothesis).
    hypotheses = [n for h in raw_hyps for n in [_normalize_hypothesis(h)] if n is not None]

    priority_assets = raw.get('priority_assets')
    priority_assets = [a.strip().upper() for a in priority_assets
                       if isinstance(a, str) and a.strip()] \
        if isinstance(priority_assets, list) else []

    summary = raw.get('summary')
    summary = summary if isinstance(summary, dict) else {}

    raw_learnings = raw.get('research_learnings')
    raw_learnings = raw_learnings if isinstance(raw_learnings, list) else []
    research_learnings = []
    for item in raw_learnings:
        if not isinstance(item, dict):
            continue
        assets = item.get('assets') if isinstance(item.get('assets'), list) else []
        research_learnings.append({
            'title': item.get('title', ''),
            'type': item.get('learning_type', 'research_learning'),
            'summary': item.get('summary', ''),
            'assets': [a.strip().upper() for a in assets if isinstance(a, str) and a.strip()],
            'primary_asset': _normalize_experiment_asset(item.get('primary_asset')),
            'market_scope': str(item.get('market_scope') or 'unknown').strip().lower(),
            'validation_gaps': item.get('validation_gaps', []) if isinstance(item.get('validation_gaps'), list) else [],
            'url': item.get('url', ''),
        })

    raw_experiments = raw.get('experiments')
    raw_experiments = raw_experiments if isinstance(raw_experiments, list) else []
    experiments = []
    for item in raw_experiments:
        if not isinstance(item, dict):
            continue
        asset_raw = item.get('asset')
        asset_candidates = item.get('asset_candidates') if isinstance(item.get('asset_candidates'), list) else []
        experiments.append({
            'id': item.get('experiment_id', ''),
            'type': item.get('type', 'experiment'),
            'asset': _normalize_experiment_asset(asset_raw),
            'asset_raw': asset_raw,
            'asset_candidates': [
                _normalize_experiment_asset(candidate)
                for candidate in asset_candidates
                if _normalize_experiment_asset(candidate)
            ],
            'market_scope': str(item.get('market_scope') or 'unknown').strip().lower(),
            'title': item.get('title', ''),
            'goal': item.get('goal', ''),
            'guardrail': item.get('guardrail', ''),
        })

    return {
        'hypotheses': hypotheses,
        'research_learnings': research_learnings,
        'experiments': experiments,
        'priority_assets': priority_assets,
        'summary': summary,
        'generated_at': raw.get('generated_at'),
    }


def extract_scanner_hints(feedback):
    """Derive deterministic scanner hints from normalized research feedback.

    Returns a single dict consumed by apply_research_overrides() and cmd_scan().
    This is the ONLY place where research fields influence scanner behavior.

    Output contract:
      priority_assets        — bare tickers to promote in watchlist scan order
      asset_bias             — {symbol: float} rank nudge, capped at _MAX_ASSET_BIAS
      valid_hypothesis_count — hypotheses that passed all quality gates
      caution_factor         — scales all research influence down if quality is low
      research_active        — False → skip research influence entirely

    RESEARCH IS NOT EXECUTION: hints produced here bias candidate ordering ONLY.
    They do not modify risk parameters, signal thresholds, or position sizes.
    The scanner validates every signal independently before any order is considered.
    """
    all_hypotheses = feedback.get('hypotheses', [])
    valid = _filter_valid_hypotheses(all_hypotheses)
    caution = _global_caution_factor(all_hypotheses, valid)

    # If caution is zero, research has no meaningful quality → treat as inactive.
    research_active = caution > 0.0 and len(valid) > 0

    raw_bias = _asset_bias_from_hypotheses(valid) if research_active else {}
    # Apply caution factor: weak overall research reduces per-asset bias further.
    analyzer_bias = {sym: round(b * caution, 4) for sym, b in raw_bias.items()}

    # Blend analyzer confidence with empirical performance (60/40 split).
    # Empirical scores are available only after trades have been observed.
    # When no empirical data exists, full weight stays on analyzer confidence.
    # REGRESSION GUARD: this blend affects ordering bias ONLY, not thresholds.
    tracking = load_hypothesis_tracking()
    emp_scores = empirical_asset_scores(tracking)  # {ticker: [0,1]}

    blended_bias: dict = {}
    for sym, abias in analyzer_bias.items():
        ticker = sym.split('/')[0].upper()
        emp = emp_scores.get(ticker)
        if emp is not None:
            # 60% analyzer, 40% empirical.
            # Empirical score is already in [0, 1] — scale directly to _MAX_ASSET_BIAS.
            # Do NOT divide by max(emp_scores) — that relative scaling distorts absolute
            # performance and lets one dominant asset crowd out all others.
            empirical_component = emp * _MAX_ASSET_BIAS * 0.4
            blended = abias * 0.6 + empirical_component
        else:
            blended = abias  # no empirical data yet → use analyzer bias unchanged
        blended_bias[sym] = round(min(blended, _MAX_ASSET_BIAS), 4)

    return {
        'priority_assets': feedback.get('priority_assets', []),
        'asset_bias': blended_bias,
        'valid_hypothesis_count': len(valid),
        'research_learning_count': len(feedback.get('research_learnings', [])),
        'experiment_count': len(feedback.get('experiments', [])),
        'caution_factor': caution,
        'research_active': research_active,
    }


def apply_research_overrides(config):
    """Apply conservative research influence to scanner config.

    REGRESSION GUARD: this function must remain the sole consumer of
    strategy_feedback.json. If you are adding a new code path that reads
    RESEARCH_FEEDBACK directly, stop — call load_research_feedback() here
    and pass hints through config['_scanner_hints'] instead.

    What this function DOES:
      - Reorders the watchlist so research-prioritised assets are scanned first.
      - Stores scanner hints in config['_scanner_hints'] for ranking and display.

    What this function does NOT do:
      - Does NOT change any numeric thresholds (min_signal_strength, min_rr_ratio…).
      - Does NOT modify risk parameters (stop multipliers, exposure caps, etc.).
      - Does NOT add symbols to the watchlist.
      - Does NOT remove symbols from the watchlist.

    Risk logic always wins. Research influence is advisory and capped.
    """
    raw = load_research_feedback()
    feedback = normalize_research_feedback(raw)
    hints = extract_scanner_hints(feedback)
    base_watchlist = list(dict.fromkeys(config.get('watchlist', [])))
    excluded_assets = _normalize_excluded_assets(config.get('excluded_assets', []))

    # Persist new/updated hypothesis records for empirical tracking.
    # Called here so tracking stays in sync whenever config is loaded.
    upsert_hypothesis_records(feedback.get('hypotheses', []))
    experiment_tracking = sync_experiment_tracking(feedback)
    experiment_trials = build_experiment_trial_backlog(
        feedback, experiment_tracking, base_watchlist, excluded_assets=excluded_assets
    )
    save_experiment_trials(experiment_trials)

    # Reorder watchlist: promote research-priority assets to scan first.
    # Only symbols already on the watchlist are moved — no new symbols added.
    if hints['research_active'] and hints['priority_assets']:
        base = list(dict.fromkeys(config.get('watchlist', [])))
        priority_symbols = []
        for ticker in hints['priority_assets']:
            sym = ticker if '/' in ticker else f"{ticker}/USDT"
            if sym in base and sym not in priority_symbols and not is_excluded_asset(config, sym):
                priority_symbols.append(sym)
        if priority_symbols:
            config['watchlist'] = priority_symbols + [s for s in base if s not in priority_symbols]

    ready_trials = [
        trial for trial in experiment_trials.get('trials', [])
        if trial.get('translation_status') == 'scanner_ready' and trial.get('scanner_symbol')
    ]
    ready_symbols = []
    for trial in ready_trials:
        symbol = trial['scanner_symbol']
        if symbol in config['watchlist'] and symbol not in ready_symbols:
            ready_symbols.append(symbol)

    if ready_symbols:
        config['watchlist'] = ready_symbols + [s for s in config['watchlist'] if s not in ready_symbols]

    trial_bias = {}
    for trial in ready_trials:
        symbol = trial['scanner_symbol']
        trial_bias[symbol] = max(trial_bias.get(symbol, 0.0), trial.get('trial_bias', 0.0))

    combined_bias = dict(hints.get('asset_bias', {}))
    for symbol, bias in trial_bias.items():
        combined_bias[symbol] = round(
            min(_MAX_ASSET_BIAS, combined_bias.get(symbol, 0.0) + bias),
            4,
        )
    hints['asset_bias'] = combined_bias
    hints['experiment_trials'] = _experiment_trials_summary(experiment_trials)

    config['_scanner_hints'] = hints
    # Compact display summary for cmd_scan / cmd_report output.
    config['_research_overrides'] = {
        'research_active': hints['research_active'],
        'valid_hypotheses': hints['valid_hypothesis_count'],
        'research_learning_count': hints.get('research_learning_count', 0),
        'experiment_count': hints.get('experiment_count', 0),
        'caution_factor': hints['caution_factor'],
        'asset_bias_symbols': list(hints['asset_bias'].keys()),
        'excluded_assets': excluded_assets,
        'experiment_trial_count': hints['experiment_trials'].get('trial_count', 0),
        'experiment_trial_status_counts': hints['experiment_trials'].get('status_counts', {}),
    } if hints['research_active'] else {}
    return config


# ---------------------------------------------------------------------------
# Hypothesis Tracking
# ---------------------------------------------------------------------------
# Responsibilities:
#   youtube_pipeline  → generates hypothesis records (strategy_feedback.json)
#   THIS BLOCK        → observes trades, scores hypotheses empirically over time
#   market_scanner    → executes trades (never caused by a hypothesis)
#
# A hypothesis is an IDEA UNDER OBSERVATION, not a trigger.
# Trades are softly linked to hypotheses whose assets overlap — this is
# correlation bookkeeping only, NOT causality.
#
# Tracking lifecycle:
#   upsert_hypothesis_records()  — called after each pipeline run
#   link_hypotheses_to_trade()   — called on trade open  (soft association)
#   update_hypotheses_on_close() — called on trade close (empirical feedback)
#   compute_hypothesis_score()   — deterministic scoring, no LLM
#   empirical_asset_scores()     — aggregate per-asset empirical weight
# ---------------------------------------------------------------------------

# Hypothesis aging — stale or untested hypotheses are ignored in scoring.
# They are NOT deleted from file; they simply contribute zero influence.
MAX_HYPOTHESIS_AGE_DAYS = 14
MIN_TESTS_FOR_VALIDITY = 3

import hashlib as _hashlib
import re as _re

_FILLER_WORDS = frozenset({
    'strategy', 'setup', 'best', 'top', 'simple', 'easy', 'ultimate',
    'guide', 'tutorial', 'how', 'to', 'the', 'a', 'an', 'and', 'or',
    'for', 'with', 'using', 'on', 'in', 'of', 'is', 'are', 'my', 'new',
    'trading', 'trade', 'crypto', 'forex', 'market', 'review', 'part',
})


def _normalise_title_tokens(title):
    """Lowercase, strip punctuation, drop filler words, keep first 8 tokens."""
    tokens = _re.sub(r'[^a-z0-9 ]', ' ', title.lower()).split()
    tokens = [t for t in tokens if t not in _FILLER_WORDS and len(t) > 1]
    return ' '.join(tokens[:8])


def _hypothesis_stable_id(h):
    """Stable ID from normalised title tokens + sorted assets.

    Normalisation prevents fragmentation from minor wording differences:
      'RSI divergence BTC strategy' == 'Bitcoin RSI divergence setup'
    Titles are lowercased, punctuation stripped, filler words removed,
    truncated to 8 meaningful tokens. Asset names are sorted and uppercased.
    """
    norm_title = _normalise_title_tokens(h.get('title', ''))
    sorted_assets = ','.join(sorted(a.upper() for a in h.get('assets', []) if a))
    key = f'{norm_title}|{sorted_assets}'.encode()
    return _hashlib.sha1(key).hexdigest()[:16]


def load_hypothesis_tracking():
    """Load tracking state. Returns dict keyed by hypothesis stable_id."""
    raw = load_json(HYPOTHESIS_TRACKING, {})
    return raw if isinstance(raw, dict) else {}


def save_hypothesis_tracking(tracking):
    save_json(HYPOTHESIS_TRACKING, tracking)


def upsert_hypothesis_records(normalized_hypotheses):
    """Merge new hypotheses from a pipeline run into the tracking state.

    Matching: stable_id (sha1 of title + assets). If same id seen again → update
    last_seen / times_seen and refresh metadata from the newer pipeline output.
    New hypotheses get a fresh tracking record with zero empirical counters.

    This is called from apply_research_overrides() so tracking stays in sync
    without requiring a separate explicit call.
    """
    if not normalized_hypotheses:
        return
    tracking = load_hypothesis_tracking()
    now = datetime.now(timezone.utc).isoformat()

    for h in normalized_hypotheses:
        hid = _hypothesis_stable_id(h)
        existing = tracking.get(hid)
        if existing:
            existing['last_seen_timestamp'] = now
            existing['times_seen'] = existing.get('times_seen', 1) + 1
            # Refresh live pipeline fields (confidence/tier may improve over time)
            existing['confidence'] = h['confidence']
            existing['tier'] = h['tier']
            existing['hype_ratio'] = h['hype_ratio']
            existing['overfitting_flags'] = h['overfitting_flags']
        else:
            tracking[hid] = {
                'id': hid,
                'title': h.get('title', ''),
                'assets': h.get('assets', []),
                'timeframe': h.get('timeframe') if h.get('timeframe') else None,
                'confidence': h['confidence'],
                'tier': h['tier'],
                'hype_ratio': h['hype_ratio'],
                'overfitting_flags': h['overfitting_flags'],
                'first_seen_timestamp': now,
                'last_seen_timestamp': now,
                'times_seen': 1,
                # Empirical performance counters (incremented by update_hypotheses_on_close)
                'times_tested': 0,
                'times_profitable': 0,
                'times_unprofitable': 0,
                'cumulative_pnl': 0.0,
                'avg_pnl': 0.0,
                'hypothesis_score': 0.0,
                'last_updated': now,
            }

    save_hypothesis_tracking(tracking)


# ---------------------------------------------------------------------------
# Experiment Tracking
# ---------------------------------------------------------------------------
# Experiments connect research learnings to observed paper trades.
# The pipeline proposes them; the scanner links matching trades and records
# empirical outcomes in a deterministic local state file.
# ---------------------------------------------------------------------------

MIN_EXPERIMENT_TESTS_FOR_DECISION = 3


def load_experiment_tracking():
    raw = load_json(EXPERIMENT_TRACKING, {})
    return raw if isinstance(raw, dict) else {}


def save_experiment_tracking(tracking):
    save_json(EXPERIMENT_TRACKING, tracking)


def load_experiment_trials():
    raw = load_json(EXPERIMENT_TRIALS, {})
    return raw if isinstance(raw, dict) else {}


def save_experiment_trials(snapshot):
    save_json(EXPERIMENT_TRIALS, snapshot)


def load_optimizer_recommendations():
    raw = load_json(OPTIMIZER_RECOMMENDATIONS, {})
    return raw if isinstance(raw, dict) else {}


def save_optimizer_recommendations(snapshot):
    save_json(OPTIMIZER_RECOMMENDATIONS, snapshot)


def load_optimizer_decisions():
    raw = load_json(OPTIMIZER_DECISIONS, {})
    return raw if isinstance(raw, dict) else {}


def save_optimizer_decisions(snapshot):
    save_json(OPTIMIZER_DECISIONS, snapshot)


def _new_experiment_record(exp, now):
    return {
        'id': exp.get('id', ''),
        'title': exp.get('title', ''),
        'type': exp.get('type', 'experiment'),
        'asset': exp.get('asset'),
        'asset_raw': exp.get('asset_raw'),
        'asset_candidates': exp.get('asset_candidates', []),
        'market_scope': exp.get('market_scope', 'unknown'),
        'goal': exp.get('goal', ''),
        'guardrail': exp.get('guardrail', ''),
        'first_seen_timestamp': now,
        'last_seen_timestamp': now,
        'status': 'pending',
        'linked_trade_count': 0,
        'open_trade_count': 0,
        'closed_trade_count': 0,
        'times_profitable': 0,
        'times_unprofitable': 0,
        'cumulative_pnl': 0.0,
        'avg_pnl': 0.0,
        'win_rate': 0.0,
        'gross_profit': 0.0,
        'gross_loss': 0.0,
        'profit_factor': None,
        'total_fees': 0.0,
        'avg_r_multiple': 0.0,
        'expectancy': 0.0,
        'total_hold_hours': 0.0,
        'avg_hold_hours': 0.0,
        'fee_burden_pct': 0.0,
        'min_closed_trades_required': MIN_EXPERIMENT_TESTS_FOR_DECISION,
        'decision_reason': 'Awaiting first linked trade.',
        'last_trade_id': None,
        'last_trade_symbol': None,
        'last_trade_opened_at': None,
        'last_trade_closed_at': None,
        'last_result': None,
        'recent_trade_ids': [],
        'archived_at': None,
    }


def _finalize_experiment_metrics(record):
    closed_count = max(int(record.get('closed_trade_count', 0)), 0)
    wins = max(int(record.get('times_profitable', 0)), 0)
    losses = max(int(record.get('times_unprofitable', 0)), 0)
    cumulative_pnl = _coerce_float(record.get('cumulative_pnl', 0.0), 0.0)
    gross_profit = _coerce_float(record.get('gross_profit', 0.0), 0.0, lo=0.0)
    gross_loss = _coerce_float(record.get('gross_loss', 0.0), 0.0, lo=0.0)
    total_r_multiple = _coerce_float(record.get('total_r_multiple', 0.0), 0.0)
    total_fees = _coerce_float(record.get('total_fees', 0.0), 0.0, lo=0.0)
    total_hold_hours = _coerce_float(record.get('total_hold_hours', 0.0), 0.0, lo=0.0)

    record['avg_pnl'] = round(cumulative_pnl / closed_count, 4) if closed_count > 0 else 0.0
    record['win_rate'] = round(wins / closed_count, 4) if closed_count > 0 else 0.0
    record['avg_r_multiple'] = round(total_r_multiple / closed_count, 4) if closed_count > 0 else 0.0
    record['expectancy'] = round(cumulative_pnl / closed_count, 4) if closed_count > 0 else 0.0
    record['avg_hold_hours'] = round(total_hold_hours / closed_count, 2) if closed_count > 0 else 0.0
    fee_base = gross_profit + gross_loss
    record['fee_burden_pct'] = round((total_fees / fee_base) * 100, 2) if fee_base > 0 else 0.0
    if gross_loss > 0:
        record['profit_factor'] = round(gross_profit / gross_loss, 4)
    elif gross_profit > 0 and losses == 0:
        record['profit_factor'] = 'inf'
    else:
        record['profit_factor'] = None
    return record


def _compute_experiment_status(record, is_current=True):
    if not is_current:
        record['decision_reason'] = 'Experiment no longer present in current research feedback.'
        return 'archived'
    if record.get('open_trade_count', 0) > 0:
        record['decision_reason'] = 'Open experiment-linked trade is still running.'
        return 'running'
    linked = record.get('linked_trade_count', 0)
    closed = record.get('closed_trade_count', 0)
    if linked == 0 and closed == 0:
        record['decision_reason'] = 'No linked trades observed yet.'
        return 'pending'
    min_closed = int(record.get('min_closed_trades_required', MIN_EXPERIMENT_TESTS_FOR_DECISION))
    if closed < min_closed:
        record['decision_reason'] = f'Need at least {min_closed} closed trades; currently {closed}.'
        return 'running'
    avg_pnl = _coerce_float(record.get('avg_pnl', 0.0), 0.0)
    win_rate = _coerce_float(record.get('win_rate', 0.0), 0.0, lo=0.0, hi=1.0)
    expectancy = _coerce_float(record.get('expectancy', avg_pnl), 0.0)
    pf_raw = record.get('profit_factor')
    profit_factor = float('inf') if pf_raw == 'inf' else _coerce_float(pf_raw, 0.0)
    if expectancy > 0 and profit_factor >= 1.05 and win_rate >= 0.45:
        record['decision_reason'] = (
            f'Positive expectancy {expectancy:+.2f}€, profit factor {pf_raw}, '
            f'win rate {win_rate:.0%} across {closed} closed trades.'
        )
        return 'passed'
    record['decision_reason'] = (
        f'Expectancy {expectancy:+.2f}€, profit factor {pf_raw}, '
        f'win rate {win_rate:.0%} across {closed} closed trades.'
    )
    return 'failed'


def link_experiments_to_trade(trade, experiment_tracking=None):
    symbol = str(trade.get('symbol', '')).upper()
    if not symbol:
        return []
    base = symbol.split('/')[0]
    tracking = experiment_tracking or load_experiment_tracking()
    linked = []
    for exp_id, exp in tracking.items():
        asset = str(exp.get('asset') or '').upper()
        if not asset:
            continue
        if asset == symbol or asset == base:
            linked.append(exp_id)
            continue
        if '/' in asset and asset.split('/')[0] == base:
            linked.append(exp_id)
    return linked


def _record_experiment_trade(record, trade, closed=False):
    pnl = _coerce_float(trade.get('pnl_usd', 0.0), 0.0)
    fees = _coerce_float(trade.get('fees_paid', 0.0), 0.0, lo=0.0)
    trade_id = trade.get('id')
    if trade_id:
        recent = list(record.get('recent_trade_ids', []))
        if trade_id not in recent:
            recent.append(trade_id)
        record['recent_trade_ids'] = recent[-10:]

    record['linked_trade_count'] = record.get('linked_trade_count', 0) + 1
    record['last_trade_id'] = trade_id
    record['last_trade_symbol'] = trade.get('symbol')
    record['last_trade_opened_at'] = trade.get('entry_time')

    if closed:
        record['closed_trade_count'] = record.get('closed_trade_count', 0) + 1
        if pnl > 0:
            record['times_profitable'] = record.get('times_profitable', 0) + 1
            record['gross_profit'] = round(record.get('gross_profit', 0.0) + pnl, 4)
        else:
            record['times_unprofitable'] = record.get('times_unprofitable', 0) + 1
            record['gross_loss'] = round(record.get('gross_loss', 0.0) + abs(min(pnl, 0.0)), 4)
        record['cumulative_pnl'] = round(record.get('cumulative_pnl', 0.0) + pnl, 4)
        record['total_fees'] = round(record.get('total_fees', 0.0) + fees, 4)
        record['total_r_multiple'] = round(
            record.get('total_r_multiple', 0.0) + _coerce_float(trade.get('r_multiple', 0.0), 0.0),
            4,
        )
        record['total_hold_hours'] = round(
            record.get('total_hold_hours', 0.0) + _coerce_float(trade.get('hold_duration_hours', 0.0), 0.0, lo=0.0),
            4,
        )
        _finalize_experiment_metrics(record)
        record['last_trade_closed_at'] = trade.get('exit_time')
        record['last_result'] = {
            'trade_id': trade_id,
            'symbol': trade.get('symbol'),
            'exit_reason': trade.get('exit_reason'),
            'pnl_usd': round(pnl, 4),
            'r_multiple': _coerce_float(trade.get('r_multiple', 0.0), 0.0),
            'fees_paid': round(fees, 4),
        }
    else:
        record['open_trade_count'] = record.get('open_trade_count', 0) + 1
        record['last_result'] = {
            'trade_id': trade_id,
            'symbol': trade.get('symbol'),
            'state': 'open',
            'entry_price': trade.get('entry_price'),
        }


def sync_experiment_tracking(feedback):
    tracking = load_experiment_tracking()
    now = utc_now_iso()
    normalized = {}
    current_ids = set()

    for exp in feedback.get('experiments', []):
        exp_id = exp.get('id')
        if not exp_id:
            continue
        current_ids.add(exp_id)
        existing = tracking.get(exp_id)
        record = _new_experiment_record(exp, now)
        if existing:
            record['first_seen_timestamp'] = existing.get('first_seen_timestamp', now)
        normalized[exp_id] = record

    for exp_id, existing in tracking.items():
        if exp_id in normalized:
            continue
        archived = dict(existing)
        archived['status'] = 'archived'
        archived['archived_at'] = archived.get('archived_at') or now
        normalized[exp_id] = archived

    active = load_json(TRADES_ACTIVE, [])
    history = load_json(TRADES_HISTORY, [])
    active_changed = False
    history_changed = False

    for idx, trade in enumerate(active):
        linked = list(dict.fromkeys(
            (trade.get('experiment_ids') or []) + link_experiments_to_trade(trade, normalized)
        ))
        if trade.get('experiment_ids') != linked:
            active[idx]['experiment_ids'] = linked
            active_changed = True
        for exp_id in linked:
            record = normalized.get(exp_id)
            if record:
                _record_experiment_trade(record, trade, closed=False)

    for idx, trade in enumerate(history):
        linked = list(dict.fromkeys(
            (trade.get('experiment_ids') or []) + link_experiments_to_trade(trade, normalized)
        ))
        if trade.get('experiment_ids') != linked:
            history[idx]['experiment_ids'] = linked
            history_changed = True
        for exp_id in linked:
            record = normalized.get(exp_id)
            if record:
                _record_experiment_trade(record, trade, closed=True)

    for exp_id, record in normalized.items():
        is_current = exp_id in current_ids
        record['last_seen_timestamp'] = now if is_current else record.get('last_seen_timestamp', now)
        _finalize_experiment_metrics(record)
        record['status'] = _compute_experiment_status(record, is_current=is_current)
        if record['status'] != 'archived':
            record['archived_at'] = None

    if active_changed:
        save_json(TRADES_ACTIVE, active)
    if history_changed:
        save_json(TRADES_HISTORY, history)
    save_experiment_tracking(normalized)
    return normalized


def _experiment_summary_for_display(tracking, top_n=5):
    items = list(tracking.values())
    status_counts = {}
    for item in items:
        status = item.get('status', 'pending')
        status_counts[status] = status_counts.get(status, 0) + 1

    tested = [item for item in items if item.get('linked_trade_count', 0) > 0]
    ranked = sorted(
        tested,
        key=lambda item: (
            item.get('expectancy', 0.0),
            item.get('avg_r_multiple', 0.0),
            item.get('closed_trade_count', 0),
        ),
        reverse=True,
    )
    flop_candidates = [
        item for item in tested
        if item.get('status') == 'failed' or _coerce_float(item.get('expectancy', 0.0), 0.0) < 0
    ]
    flops = sorted(
        flop_candidates,
        key=lambda item: (
            item.get('expectancy', 0.0),
            item.get('avg_r_multiple', 0.0),
            -item.get('closed_trade_count', 0),
        ),
    )
    return {
        'total': len(items),
        'status_counts': status_counts,
        'top': [
            {
                'id': item.get('id'),
                'title': item.get('title', '')[:60],
                'status': item.get('status'),
                'linked_trades': item.get('linked_trade_count', 0),
                'closed_trades': item.get('closed_trade_count', 0),
                'avg_pnl': item.get('avg_pnl', 0.0),
                'expectancy': item.get('expectancy', 0.0),
                'profit_factor': item.get('profit_factor'),
                'avg_r_multiple': item.get('avg_r_multiple', 0.0),
                'avg_hold_hours': item.get('avg_hold_hours', 0.0),
                'fee_burden_pct': item.get('fee_burden_pct', 0.0),
                'asset': item.get('asset'),
                'decision_reason': item.get('decision_reason'),
            }
            for item in ranked[:top_n]
        ],
        'flop': [
            {
                'id': item.get('id'),
                'title': item.get('title', '')[:60],
                'status': item.get('status'),
                'linked_trades': item.get('linked_trade_count', 0),
                'closed_trades': item.get('closed_trade_count', 0),
                'avg_pnl': item.get('avg_pnl', 0.0),
                'expectancy': item.get('expectancy', 0.0),
                'profit_factor': item.get('profit_factor'),
                'avg_r_multiple': item.get('avg_r_multiple', 0.0),
                'avg_hold_hours': item.get('avg_hold_hours', 0.0),
                'fee_burden_pct': item.get('fee_burden_pct', 0.0),
                'asset': item.get('asset'),
                'decision_reason': item.get('decision_reason'),
            }
            for item in flops[:top_n]
        ],
    }


def _trial_symbol_for_asset(asset):
    normalized = _normalize_experiment_asset(asset)
    if not normalized:
        return None
    if "/" in normalized:
        return normalized
    return f"{normalized}/USDT"


def _select_trial_symbol(asset, asset_candidates, watchlist_set, watchlist_bases):
    for candidate in [asset] + list(asset_candidates or []):
        symbol = _trial_symbol_for_asset(candidate)
        if not symbol:
            continue
        if symbol in watchlist_set:
            return symbol
        base = symbol.split('/')[0].upper()
        if base in watchlist_bases:
            return watchlist_bases[base]
    return None


def build_experiment_trial_backlog(feedback, tracking, watchlist, excluded_assets=None):
    """Translate experiments into concrete scanner trials without mutating rules."""
    base_watchlist = list(dict.fromkeys(watchlist or []))
    watchlist_set = {str(symbol).upper() for symbol in base_watchlist}
    watchlist_bases = {symbol.split('/')[0].upper(): symbol for symbol in watchlist_set}
    excluded_config = {'excluded_assets': list(excluded_assets or [])}
    now = utc_now_iso()
    trials = []

    for exp in feedback.get('experiments', []):
        exp_id = exp.get('id')
        if not exp_id:
            continue
        record = tracking.get(exp_id, {})
        status = record.get('status', 'pending')
        asset = exp.get('asset')
        asset_candidates = exp.get('asset_candidates', [])
        market_scope = str(exp.get('market_scope') or 'unknown').lower()
        candidate_symbol = _select_trial_symbol(asset, asset_candidates, watchlist_set, watchlist_bases)
        base_asset = candidate_symbol.split('/')[0].upper() if candidate_symbol else None
        translation_status = 'blocked'
        trial_type = 'research_gap'
        block_reason = None
        scanner_symbol = None
        trial_bias = 0.0
        next_action = 'Keep observing until a concrete scanner path exists.'

        if status == 'archived':
            translation_status = 'archived'
            trial_type = 'archived'
            block_reason = 'Experiment is no longer active in current research feedback.'
            next_action = 'Ignore unless a newer experiment version reappears.'
        elif market_scope not in {'unknown', 'crypto', 'mixed'}:
            translation_status = 'blocked_market_scope'
            trial_type = 'market_support_gap'
            block_reason = f'Current scanner is crypto-only; market scope {market_scope} is unsupported.'
            next_action = 'Needs a dedicated market adapter before this experiment can run.'
        elif any(is_excluded_asset(excluded_config, candidate) for candidate in [asset] + list(asset_candidates or [])):
            translation_status = 'blocked_asset_policy'
            trial_type = 'asset_policy_block'
            block_reason = f'Asset policy excludes {asset or scanner_symbol or "this experiment"} from trading.'
            next_action = 'Keep this experiment out of active rotation unless the exclusion policy changes.'
        elif asset in {'USDT', 'USDC', 'USD'} and not candidate_symbol:
            translation_status = 'blocked_non_directional'
            trial_type = 'asset_mapping_gap'
            block_reason = f'Asset {asset} is a quote currency, not a directional trade target.'
            next_action = 'Needs manual translation into a concrete tradable base asset.'
        elif not candidate_symbol:
            translation_status = 'blocked_asset_mapping'
            trial_type = 'asset_mapping_gap'
            block_reason = 'Experiment has no tradable asset mapping into the active crypto scanner.'
            next_action = 'Require a concrete crypto asset before this can become a scanner trial.'
        elif candidate_symbol in watchlist_set:
            scanner_symbol = candidate_symbol
            translation_status = 'scanner_ready'
            if status == 'pending':
                trial_type = 'watchlist_priority_trial'
                trial_bias = 0.035
                next_action = 'Prioritize this symbol in scan order until the first linked trade exists.'
            elif status == 'running':
                trial_type = 'active_validation_trial'
                trial_bias = 0.025
                next_action = 'Keep it in rotation until enough closed trades exist for a decision.'
            elif status == 'passed':
                trial_type = 'promotion_candidate'
                trial_bias = 0.02
                next_action = 'Keep trading under current guardrails while preparing a controlled config proposal.'
            elif status == 'failed':
                trial_type = 'deprioritized_trial'
                trial_bias = 0.0
                next_action = 'Do not prioritize this symbol until a new learning reopens the experiment.'
            else:
                trial_type = 'watchlist_trial'
                trial_bias = 0.015
                next_action = 'Keep under observation.'
        elif base_asset in watchlist_bases:
            scanner_symbol = watchlist_bases[base_asset]
            translation_status = 'scanner_ready'
            trial_type = 'watchlist_base_match_trial'
            trial_bias = 0.03 if status in {'pending', 'running'} else 0.015
            next_action = f'Use existing watchlist symbol {scanner_symbol} as the validation path.'
        else:
            translation_status = 'blocked_watchlist_gap'
            trial_type = 'watchlist_scope_gap'
            block_reason = f'{candidate_symbol} is not in the active crypto watchlist.'
            next_action = 'Either extend the watchlist manually or keep this as a research-only experiment.'

        trials.append({
            'id': f'{exp_id}::trial',
            'experiment_id': exp_id,
            'title': exp.get('title', '')[:80],
            'type': exp.get('type', 'experiment'),
            'status': status,
            'asset': asset,
            'asset_candidates': asset_candidates,
            'market_scope': market_scope,
            'scanner_symbol': scanner_symbol,
            'translation_status': translation_status,
            'trial_type': trial_type,
            'trial_bias': round(trial_bias, 4),
            'linked_trade_count': int(record.get('linked_trade_count', 0)),
            'closed_trade_count': int(record.get('closed_trade_count', 0)),
            'decision_reason': record.get('decision_reason') or 'No experiment decision available yet.',
            'block_reason': block_reason,
            'next_action': next_action,
            'generated_at': now,
        })

    status_counts = {}
    ready_symbols = []
    for trial in trials:
        status_key = trial.get('translation_status', 'blocked')
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        if trial.get('translation_status') == 'scanner_ready' and trial.get('scanner_symbol'):
            ready_symbols.append(trial['scanner_symbol'])

    return {
        'generated_at': now,
        'trial_count': len(trials),
        'status_counts': status_counts,
        'scanner_ready_symbols': list(dict.fromkeys(ready_symbols)),
        'trials': trials,
    }


def _experiment_trials_summary(snapshot, top_n=5):
    trials = snapshot.get('trials', []) if isinstance(snapshot, dict) else []
    ranked = sorted(
        trials,
        key=lambda item: (
            1 if item.get('translation_status') == 'scanner_ready' else 0,
            item.get('trial_bias', 0.0),
            item.get('closed_trade_count', 0),
        ),
        reverse=True,
    )
    return {
        'generated_at': snapshot.get('generated_at') if isinstance(snapshot, dict) else None,
        'trial_count': len(trials),
        'status_counts': snapshot.get('status_counts', {}) if isinstance(snapshot, dict) else {},
        'scanner_ready_symbols': snapshot.get('scanner_ready_symbols', []) if isinstance(snapshot, dict) else [],
        'top': ranked[:top_n],
    }


def _trials_by_experiment(snapshot):
    trials = snapshot.get('trials', []) if isinstance(snapshot, dict) else []
    return {
        trial.get('experiment_id'): trial
        for trial in trials
        if isinstance(trial, dict) and trial.get('experiment_id')
    }


def build_optimizer_recommendations(tracking, trial_snapshot, config, trade_count):
    """Return a deterministic recommendation snapshot without mutating config."""
    generated_at = utc_now_iso()
    recommendations = []
    watchlist = {str(symbol).upper() for symbol in config.get('watchlist', [])}
    trials = _trials_by_experiment(trial_snapshot)

    for exp_id, record in tracking.items():
        status = record.get('status', 'pending')
        title = record.get('title', '')[:80]
        asset = record.get('asset')
        trial = trials.get(exp_id, {})
        trial_status = trial.get('translation_status')
        trial_type = trial.get('trial_type')
        market_scope = trial.get('market_scope') or record.get('market_scope') or 'unknown'
        scanner_symbol = trial.get('scanner_symbol')
        closed = int(record.get('closed_trade_count', 0))
        linked = int(record.get('linked_trade_count', 0))
        min_closed = int(record.get('min_closed_trades_required', MIN_EXPERIMENT_TESTS_FOR_DECISION))
        expectancy = _coerce_float(record.get('expectancy', 0.0), 0.0)
        avg_r = _coerce_float(record.get('avg_r_multiple', 0.0), 0.0)
        fee_burden = _coerce_float(record.get('fee_burden_pct', 0.0), 0.0, lo=0.0)
        pf_raw = record.get('profit_factor')
        pf_value = float('inf') if pf_raw == 'inf' else _coerce_float(pf_raw, 0.0)

        action = 'observe_only'
        priority = 40
        promotion_stage = 'research_only'
        execution_lane = 'research'
        reason = record.get('decision_reason') or 'No experiment decision available yet.'
        proposed_changes = []
        next_gate = 'Collect more evidence before any config change.'

        if trial_status == 'blocked_asset_policy':
            action = 'keep_excluded_by_policy'
            priority = 88
            promotion_stage = 'policy_blocked'
            execution_lane = 'asset_policy_blocked'
            reason = trial.get('block_reason') or reason
            next_gate = 'Only reconsider if the asset exclusion policy is deliberately changed.'
        elif trial_status == 'blocked_market_scope':
            action = 'route_to_market_adapter_backlog'
            priority = 70
            promotion_stage = 'research_only'
            execution_lane = 'market_adapter_needed'
            reason = trial.get('block_reason') or reason
            next_gate = 'Requires a dedicated non-crypto adapter before this experiment can advance.'
        elif trial_status in {'blocked_asset_mapping', 'blocked_non_directional'}:
            action = 'resolve_asset_mapping'
            priority = 68
            promotion_stage = 'research_only'
            execution_lane = 'asset_mapping_needed'
            reason = trial.get('block_reason') or reason
            next_gate = 'Needs a concrete tradable crypto asset before scanner validation can start.'
        elif trial_status == 'blocked_watchlist_gap':
            action = 'keep_research_only'
            priority = 62
            promotion_stage = 'research_only'
            execution_lane = 'watchlist_gap'
            reason = trial.get('block_reason') or reason
            next_gate = 'Either add the asset to the watchlist deliberately or leave it in research backlog.'
        elif status == 'passed':
            priority = 95
            promotion_stage = 'passed_validation'
            execution_lane = 'promotion'
            if trade_count < 10:
                action = 'extend_validation_window'
                promotion_stage = 'extended_validation'
                next_gate = 'Wait for at least 10 total closed paper trades before any config proposal.'
            elif closed < max(min_closed + 2, 5):
                action = 'promote_to_extended_papertrade'
                promotion_stage = 'extended_validation'
                next_gate = 'Run a few more controlled paper trades before proposing config edits.'
            else:
                action = 'prepare_controlled_config_candidate'
                promotion_stage = 'config_candidate'
                next_gate = 'Eligible for a manual config proposal with backup and rollback plan.'
                if asset and asset.upper() in watchlist:
                    proposed_changes.append(
                        f'Increase research priority for {asset} while keeping current risk limits unchanged.'
                    )
        elif status == 'failed':
            action = 'deprioritize_or_archive'
            priority = 90
            promotion_stage = 'deprioritized'
            execution_lane = 'deprioritized'
            next_gate = 'Do not promote this experiment until new contradictory evidence appears.'
            if asset and asset.upper() in watchlist:
                proposed_changes.append(
                    f'Deprioritize {asset} in the watchlist until a new validated learning reopens the case.'
                )
        elif status == 'running':
            if record.get('open_trade_count', 0) > 0:
                action = 'wait_for_open_trade_resolution'
                priority = 65
                promotion_stage = 'active_rotation'
                execution_lane = 'scanner_active'
                next_gate = 'Re-evaluate after the open trade closes.'
            else:
                action = 'continue_controlled_papertrading'
                priority = 75
                promotion_stage = 'active_rotation'
                execution_lane = 'scanner_active'
                next_gate = f'Collect {max(min_closed - closed, 0)} more closed trades before deciding.'
        elif status == 'pending':
            if trial_status == 'scanner_ready':
                action = 'activate_scanner_trial'
                priority = 82
                promotion_stage = 'active_rotation'
                execution_lane = 'scanner_ready'
                next_gate = 'Need the first linked trade to move from translation into measured validation.'
            elif record.get('type') == 'research_translation':
                action = 'translate_learning_into_test'
                priority = 80
                promotion_stage = 'trial_translation'
                execution_lane = 'research'
                next_gate = 'Convert this research into a replay/backtest or a watchlist-scoped papertrade trial.'
            else:
                action = 'seek_first_trade_link'
                priority = 60
                promotion_stage = 'trial_translation'
                execution_lane = 'research'
                next_gate = 'Need the first linked trade before any judgement is possible.'
        elif status == 'archived':
            action = 'keep_archived'
            priority = 20
            promotion_stage = 'archived'
            execution_lane = 'archived'
            next_gate = 'Only reopen if the research pipeline emits a new experiment version.'

        recommendations.append({
            'id': f'{exp_id}::{action}',
            'experiment_id': exp_id,
            'title': title,
            'asset': asset,
            'type': record.get('type', 'experiment'),
            'market_scope': market_scope,
            'status': status,
            'trial_status': trial_status,
            'trial_type': trial_type,
            'scanner_symbol': scanner_symbol,
            'execution_lane': execution_lane,
            'promotion_stage': promotion_stage,
            'action': action,
            'priority': priority,
            'reason': reason,
            'next_gate': next_gate,
            'proposed_changes': proposed_changes,
            'evidence': {
                'linked_trades': linked,
                'closed_trades': closed,
                'expectancy': round(expectancy, 4),
                'profit_factor': pf_raw,
                'avg_r_multiple': round(avg_r, 4),
                'fee_burden_pct': round(fee_burden, 2),
            },
        })

        recommendations.sort(
        key=lambda item: (
            item.get('priority', 0),
            item.get('evidence', {}).get('closed_trades', 0),
            item.get('evidence', {}).get('expectancy', 0.0),
        ),
        reverse=True,
    )
    return {
        'generated_at': generated_at,
        'trade_count': int(trade_count),
        'recommendation_count': len(recommendations),
        'experiment_status_counts': _experiment_summary_for_display(tracking, top_n=0)['status_counts'],
        'trial_status_counts': trial_snapshot.get('status_counts', {}) if isinstance(trial_snapshot, dict) else {},
        'recommendations': recommendations,
    }


def build_optimizer_decisions(recommendation_snapshot):
    recommendations = recommendation_snapshot.get('recommendations', []) if isinstance(recommendation_snapshot, dict) else []
    stage_counts = {}
    lane_counts = {}
    decisions = []
    for rec in recommendations:
        stage = rec.get('promotion_stage', 'research_only')
        lane = rec.get('execution_lane', 'research')
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
        decisions.append({
            'experiment_id': rec.get('experiment_id'),
            'title': rec.get('title'),
            'promotion_stage': stage,
            'execution_lane': lane,
            'action': rec.get('action'),
            'priority': rec.get('priority', 0),
            'status': rec.get('status'),
            'trial_status': rec.get('trial_status'),
            'scanner_symbol': rec.get('scanner_symbol'),
            'next_gate': rec.get('next_gate'),
            'reason': rec.get('reason'),
        })
    return {
        'generated_at': recommendation_snapshot.get('generated_at'),
        'trade_count': recommendation_snapshot.get('trade_count', 0),
        'stage_counts': stage_counts,
        'execution_lane_counts': lane_counts,
        'decisions': decisions,
    }


def link_hypotheses_to_trade(trade, tracking):
    """Return list of hypothesis IDs whose assets overlap with the trade symbol.

    This is a SOFT ASSOCIATION — it records which hypotheses were active when
    a trade opened on the same asset. It does NOT imply the hypothesis caused
    the trade. Scanner signal logic is independent.
    """
    symbol = trade.get('symbol', '')
    base = symbol.split('/')[0].upper() if '/' in symbol else symbol.upper()
    linked = []
    for hid, h in tracking.items():
        if base in [a.upper() for a in h.get('assets', [])]:
            linked.append(hid)
    return linked


def update_hypotheses_on_close(closed_trade, tracking):
    """Update empirical counters for all hypotheses linked to a closed trade.

    Called after every trade close. Increments:
      times_tested, times_profitable/unprofitable, cumulative_pnl, avg_pnl.
    Also recomputes hypothesis_score deterministically.

    No LLM, no external calls, no side effects beyond mutating `tracking` in place.
    Caller is responsible for saving tracking after this returns.
    """
    linked_ids = closed_trade.get('hypothesis_ids', [])
    if not linked_ids:
        return

    pnl = float(closed_trade.get('pnl_usd', 0.0))
    now = datetime.now(timezone.utc).isoformat()
    profitable = pnl > 0

    for hid in linked_ids:
        h = tracking.get(hid)
        if not h:
            continue
        h['times_tested'] = h.get('times_tested', 0) + 1
        if profitable:
            h['times_profitable'] = h.get('times_profitable', 0) + 1
        else:
            h['times_unprofitable'] = h.get('times_unprofitable', 0) + 1
        h['cumulative_pnl'] = round(h.get('cumulative_pnl', 0.0) + pnl, 4)
        tested = h['times_tested']
        h['avg_pnl'] = round(h['cumulative_pnl'] / tested, 4) if tested > 0 else 0.0
        h['hypothesis_score'] = compute_hypothesis_score(h)
        h['last_updated'] = now


def compute_hypothesis_score(h):
    """Deterministic hypothesis quality score in [0.0, 1.0].

    Hypotheses are probabilistic ideas, not signals. This score is used only
    to weight ordering bias in extract_scanner_hints() — it never changes
    signal thresholds or risk parameters.

    Inputs (all from the tracking record):
      confidence        — pipeline analyzer confidence [0, 1]
      win_rate          — empirical (times_profitable / times_tested)
      avg_pnl           — empirical average PnL per linked trade
      times_tested      — sample size (low → low empirical weight)
      tier              — pipeline tier
      hype_ratio        — penalises promotional content
      overfitting_flags — heavy penalty when present
      last_updated      — age-based decay applied to final score

    Decay factor: score *= max(0.3, 1 - age_days / 30)
      → halves influence by day 15, floors at 0.3 (never fully zeroed by age alone).

    This score feeds empirical_asset_scores(). It NEVER directly changes
    signal thresholds.
    """
    confidence = _coerce_float(h.get('confidence', 0.0), 0.0, lo=0.0, hi=1.0)
    times_tested = max(int(h.get('times_tested', 0)), 0)
    hype_ratio = _coerce_float(h.get('hype_ratio', 1.0), 1.0, lo=0.0, hi=1.0)

    tier_bonus = {
        'potential_strategy': 0.15,
        'testable_hypothesis': 0.08,
        'idea_only': 0.0,
        'not_actionable': -0.20,
    }.get(h.get('tier', 'not_actionable'), -0.10)

    base = confidence + tier_bonus

    # Empirical blending — tapers in as sample size grows (0 → 0 weight, 10 → full weight)
    empirical_weight = min(times_tested / 10.0, 1.0)
    if times_tested > 0:
        win_rate = h.get('times_profitable', 0) / times_tested
        avg_pnl = _coerce_float(h.get('avg_pnl', 0.0), 0.0)
        normalised_pnl = max(-0.5, min(0.5, avg_pnl / 50.0))
        empirical = win_rate * 0.5 + normalised_pnl * 0.5
    else:
        empirical = 0.0

    score = base * (1.0 - empirical_weight) + empirical * empirical_weight

    # Hard penalties
    score -= hype_ratio * 0.30
    if h.get('overfitting_flags'):
        score -= 0.25

    # Soft age decay: recent hypotheses matter more; old ones fade naturally.
    # Decay is applied AFTER penalties so stale+bad hypotheses reach zero faster.
    try:
        last_updated = datetime.fromisoformat(h.get('last_updated', ''))
        age_days = (datetime.now(timezone.utc) - last_updated.replace(
            tzinfo=timezone.utc if last_updated.tzinfo is None else last_updated.tzinfo
        )).days
    except (ValueError, TypeError):
        age_days = 0
    decay_factor = max(0.3, 1.0 - age_days / 30.0)
    score *= decay_factor

    return round(max(0.0, min(1.0, score)), 4)


def empirical_asset_scores(tracking):
    """Aggregate per-asset empirical score from valid, recent, tested hypotheses.

    Stale hypotheses are silently excluded — they are NOT deleted from file.
    Only empirical data overrides hype; stale ideas contribute nothing.

    Inclusion rules (all must pass):
      - times_tested >= MIN_TESTS_FOR_VALIDITY (default 3)
      - last_seen_timestamp within MAX_HYPOTHESIS_AGE_DAYS (default 14)
      - hypothesis_score > 0 after compute_hypothesis_score() decay

    Multiple hypotheses for the same asset are averaged.
    Returns dict: {base_ticker: float [0, 1]}
    """
    now = datetime.now(timezone.utc)
    asset_scores: dict = {}
    asset_counts: dict = {}
    for h in tracking.values():
        # Minimum test threshold — no influence until statistically meaningful
        if h.get('times_tested', 0) < MIN_TESTS_FOR_VALIDITY:
            continue
        # Aging gate — stale hypotheses contribute nothing
        try:
            last_seen = datetime.fromisoformat(h.get('last_seen_timestamp', ''))
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            if (now - last_seen).days > MAX_HYPOTHESIS_AGE_DAYS:
                continue
        except (ValueError, TypeError):
            continue  # unparseable timestamp → exclude
        score = _coerce_float(h.get('hypothesis_score', 0.0), 0.0, lo=0.0, hi=1.0)
        if score <= 0:
            continue
        for asset in h.get('assets', []):
            if not isinstance(asset, str) or not asset:
                continue
            ticker = asset.upper()
            asset_scores[ticker] = asset_scores.get(ticker, 0.0) + score
            asset_counts[ticker] = asset_counts.get(ticker, 0) + 1
    return {t: round(asset_scores[t] / asset_counts[t], 4) for t in asset_scores}


def timeframe_to_timedelta(timeframe):
    amount = int(timeframe[:-1])
    unit = timeframe[-1]
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    raise ValueError(f"Unbekanntes Timeframe-Format: {timeframe}")


def drop_incomplete_candle(df, timeframe, now=None):
    """Entferne die aktuell noch laufende Candle."""
    if df is None or df.empty:
        return df

    now = now or datetime.now(timezone.utc)
    try:
        candle_end = df.index[-1] + timeframe_to_timedelta(timeframe)
    except ValueError:
        return df

    if candle_end > now:
        return df.iloc[:-1].copy()
    return df


def period_pnl(history, since):
    pnl = 0.0
    for trade in history:
        try:
            if datetime.fromisoformat(trade["exit_time"]) >= since:
                pnl += float(trade.get("pnl_usd", 0.0))
        except Exception:
            continue
    return pnl


def get_trading_guardrails(config, history=None, perf=None):
    """Prüfe Tages-/Wochen-/Monatslimits sowie Max-Drawdown."""
    history = history if history is not None else load_json(TRADES_HISTORY, [])
    perf = perf if perf is not None else load_performance(config)
    now = datetime.now(timezone.utc)
    initial = config["initial_capital"]

    daily_pnl = period_pnl(history, now - timedelta(days=1))
    weekly_pnl = period_pnl(history, now - timedelta(days=7))
    monthly_pnl = period_pnl(history, now - timedelta(days=30))

    peak_capital = perf.get("peak_capital", initial)
    capital = perf.get("capital", initial)
    current_drawdown_pct = ((peak_capital - capital) / peak_capital * 100) if peak_capital > 0 else 0.0

    guardrails = {
        "paused": False,
        "force_close": False,
        "reason": None,
        "daily_pnl_pct": round(daily_pnl / initial * 100, 2),
        "weekly_pnl_pct": round(weekly_pnl / initial * 100, 2),
        "monthly_pnl_pct": round(monthly_pnl / initial * 100, 2),
        "current_drawdown_pct": round(current_drawdown_pct, 2),
        "max_drawdown_pct": round(perf.get("max_drawdown_pct", 0.0), 2),
    }

    if guardrails["monthly_pnl_pct"] <= -config["monthly_loss_limit_pct"]:
        guardrails.update({
            "paused": True,
            "force_close": True,
            "reason": f"Monatslimit gerissen ({guardrails['monthly_pnl_pct']:.1f}%)",
        })
    elif guardrails["current_drawdown_pct"] >= config["max_portfolio_drawdown_pct"]:
        guardrails.update({
            "paused": True,
            "force_close": True,
            "reason": f"Max Drawdown gerissen ({guardrails['current_drawdown_pct']:.1f}%)",
        })
    elif guardrails["weekly_pnl_pct"] <= -config["weekly_loss_limit_pct"]:
        guardrails.update({
            "paused": True,
            "reason": f"Wochenlimit gerissen ({guardrails['weekly_pnl_pct']:.1f}%)",
        })
    elif guardrails["daily_pnl_pct"] <= -config["daily_loss_limit_pct"]:
        guardrails.update({
            "paused": True,
            "reason": f"Tageslimit gerissen ({guardrails['daily_pnl_pct']:.1f}%)",
        })

    return guardrails


def detect_crash_mode(exchange, config):
    """Einfacher Crash-Mode-Filter basierend auf BTC-Tagesbewegung."""
    lookback_days = max(int(config.get("crash_lookback_days", 3)), 2)
    df = fetch_ohlcv(exchange, "BTC/USDT", timeframe="1d", limit=max(lookback_days + 5, 8))
    if df is None or len(df) < lookback_days + 2:
        return {
            "enabled": False,
            "daily_change_pct": None,
            "cumulative_change_pct": None,
            "volume_ratio": None,
        }

    last = df.iloc[-1]
    prev = df.iloc[-2]
    base = df.iloc[-(lookback_days + 1)]
    volume_baseline = df["volume"].iloc[-(lookback_days + 3):-1].mean()
    volume_ratio = (last["volume"] / volume_baseline) if volume_baseline and not pd.isna(volume_baseline) else 1.0
    daily_change_pct = (last["close"] / prev["close"] - 1) * 100
    cumulative_change_pct = (last["close"] / base["close"] - 1) * 100

    crash_enabled = (
        daily_change_pct <= -config["crash_daily_drop_pct"] and volume_ratio >= config["crash_volume_multiplier"]
    ) or cumulative_change_pct <= -config["crash_cumulative_drop_pct"]

    return {
        "enabled": bool(crash_enabled),
        "daily_change_pct": round(daily_change_pct, 2),
        "cumulative_change_pct": round(cumulative_change_pct, 2),
        "volume_ratio": round(volume_ratio, 2),
    }


# ── Exchange & Daten ─────────────────────────────────────────────────────────

def get_exchange(config):
    """Erstelle Exchange-Verbindung."""
    require_dependencies("ccxt")
    exchange_id = config.get("exchange", "okx")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})
    return exchange


def fetch_ohlcv(exchange, symbol, timeframe="4h", limit=200):
    """Hole OHLCV-Daten und konvertiere zu DataFrame."""
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")
        df = drop_incomplete_candle(df, timeframe)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"  ✗ {symbol}: Fehler beim Laden — {e}")
        return None


# ── Indikatoren ──────────────────────────────────────────────────────────────

def compute_indicators(df, config):
    """Berechne alle Trading-Indikatoren."""
    require_dependencies("numpy", "pandas")
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # EMAs
    df["ema_fast"] = c.ewm(span=config["ema_fast"], adjust=False).mean()
    df["ema_slow"] = c.ewm(span=config["ema_slow"], adjust=False).mean()

    # RSI
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).rolling(config["rsi_period"]).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(config["rsi_period"]).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(config["atr_period"]).mean()

    # Volume Ratio (aktuelles Volume vs 20-Perioden Durchschnitt)
    df["vol_avg"] = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_avg"].replace(0, np.nan)

    # Bollinger Bands
    df["bb_mid"] = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["bb_pct"] = (c - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)

    # ADX (simplified — für Marktregime)
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr_smooth = df["atr"].replace(0, np.nan)
    plus_di = 100 * (plus_dm.rolling(14).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr_smooth)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    df["adx"] = dx.rolling(14).mean()

    # Trend-Stärke
    df["trend"] = (df["ema_fast"] - df["ema_slow"]) / df["atr"].replace(0, np.nan)

    return df


# ── Signal-Generierung ───────────────────────────────────────────────────────

def generate_signal(df, config):
    """Generiere Trading-Signal für ein Symbol."""
    require_dependencies("numpy")
    if df is None or len(df) < 60:
        return None

    last = df.iloc[-1]

    # Check: alle Indikatoren verfügbar?
    required = ["ema_fast", "ema_slow", "rsi", "atr", "vol_ratio", "adx", "trend"]
    if any(pd.isna(last.get(r, np.nan)) for r in required):
        return None

    price = last["close"]
    ema_f = last["ema_fast"]
    ema_s = last["ema_slow"]
    rsi = last["rsi"]
    atr = last["atr"]
    vol_ratio = last["vol_ratio"]
    adx = last["adx"]
    trend = last["trend"]
    bb_pct = last.get("bb_pct", 0.5)

    # ── Marktregime ──
    if adx > 25:
        regime = "trending"
    elif adx < 20:
        regime = "ranging"
    else:
        regime = "neutral"

    # ── Signal-Komponenten ──
    # 1. Trend-Richtung (EMA crossover)
    trend_score = np.clip(trend / 2, -1, 1) * 0.35

    # 2. Momentum (RSI)
    momentum_score = ((rsi - 50) / 50) * 0.25

    # 3. Pullback zum EMA (mean reversion an Trend-EMA)
    pullback = (price - ema_f) / atr if atr > 0 else 0
    pullback_score = np.clip(-pullback, -1, 1) * 0.20

    # 4. Bollinger Band Position (oversold/overbought)
    bb_score = (0.5 - bb_pct) * 0.10  # unter 0.5 = bullish, über = bearish

    # 5. Volume Bestätigung
    vol_score = min(vol_ratio / 2, 1.0) * 0.10 * np.sign(trend_score) if vol_ratio > 1 else 0

    # Gesamt-Signal
    raw_signal = trend_score + momentum_score + pullback_score + bb_score + vol_score
    signal = np.clip(raw_signal, -1, 1)

    # ── Richtung bestimmen ──
    if signal > config["min_signal_strength"]:
        direction = "long"
    elif signal < -config["min_signal_strength"]:
        direction = "short"
    else:
        direction = "flat"

    if direction != "flat" and vol_ratio < config.get("min_volume_ratio", 1.0):
        direction = "flat"

    # ── Stop Loss & Take Profit ──
    if direction == "long":
        stop_loss = price - config["atr_stop_multiplier"] * atr
        tp1 = price + config["atr_tp1_multiplier"] * atr
        tp2 = price + config["atr_tp2_multiplier"] * atr
    elif direction == "short":
        stop_loss = price + config["atr_stop_multiplier"] * atr
        tp1 = price - config["atr_tp1_multiplier"] * atr
        tp2 = price - config["atr_tp2_multiplier"] * atr
    else:
        stop_loss = tp1 = tp2 = None

    # Risk/Reward
    if direction != "flat" and stop_loss:
        risk = abs(price - stop_loss)
        reward = abs(tp1 - price) if tp1 else 0
        rr_ratio = reward / risk if risk > 0 else 0
    else:
        risk = reward = rr_ratio = 0

    return {
        "price": round(price, 6),
        "direction": direction,
        "signal_strength": round(abs(signal), 4),
        "raw_signal": round(signal, 4),
        "stop_loss": round(stop_loss, 6) if stop_loss else None,
        "tp1": round(tp1, 6) if tp1 else None,
        "tp2": round(tp2, 6) if tp2 else None,
        "rr_ratio": round(rr_ratio, 2),
        "regime": regime,
        "volume_confirmed": bool(vol_ratio >= config.get("min_volume_ratio", 1.0)),
        "indicators": {
            "ema_fast": round(ema_f, 4),
            "ema_slow": round(ema_s, 4),
            "rsi": round(rsi, 2),
            "atr": round(atr, 6),
            "adx": round(adx, 2),
            "vol_ratio": round(vol_ratio, 2),
            "bb_pct": round(bb_pct, 4),
            "trend": round(trend, 4),
        }
    }


# ── Paper Trading ────────────────────────────────────────────────────────────

def get_portfolio_value(config, active_trades):
    """Berechne aktuellen Portfolio-Wert."""
    return load_performance(config).get("capital", config["initial_capital"])


def entry_cost_rate(config):
    return (config.get("entry_fee_bps", 10) + config.get("slippage_bps", 5) + config.get("spread_bps", 3) / 2) / 10000


def exit_cost_rate(config):
    return (config.get("exit_fee_bps", 10) + config.get("slippage_bps", 5) + config.get("spread_bps", 3) / 2) / 10000


def is_actionable_signal(config, signal):
    if is_excluded_asset(config, signal.get("symbol")):
        return False
    return signal.get("direction") != "flat" and signal.get("rr_ratio", 0) >= config.get("min_rr_ratio", 1.5)


def build_trade_plan(config, capital, active_trades, signal):
    risk_amount = capital * config["risk_per_trade"]
    risk_per_unit = abs(signal["price"] - signal["stop_loss"])
    if risk_per_unit <= 0 or signal.get("direction") == "flat":
        return {"ok": False, "reason": "Ungültiger Stop-Abstand"}

    position_size = risk_amount / risk_per_unit
    position_value = position_size * signal["price"]
    existing_value = sum(t.get("remaining_position_value", t.get("position_value", 0)) for t in active_trades)
    max_exposure = capital * config.get("max_exposure_multiple", 2.0)

    estimated_entry_costs = position_value * entry_cost_rate(config)
    estimated_exit_costs = abs(position_size * (signal.get("tp1") or signal["price"])) * exit_cost_rate(config)
    expected_tp1_profit = abs((signal.get("tp1") or signal["price"]) - signal["price"]) * position_size
    estimated_roundtrip_costs = estimated_entry_costs + estimated_exit_costs
    fee_burden_pct = (estimated_roundtrip_costs / expected_tp1_profit * 100) if expected_tp1_profit > 0 else float("inf")

    return {
        "ok": True,
        "risk_amount": risk_amount,
        "risk_per_unit": risk_per_unit,
        "position_size": position_size,
        "position_value": position_value,
        "existing_value": existing_value,
        "max_exposure": max_exposure,
        "estimated_entry_costs": estimated_entry_costs,
        "estimated_exit_costs": estimated_exit_costs,
        "estimated_roundtrip_costs": estimated_roundtrip_costs,
        "expected_tp1_profit": expected_tp1_profit,
        "fee_burden_pct": fee_burden_pct,
    }


def normalize_trade_state(trade, config):
    normalized = dict(trade)
    normalized.setdefault("remaining_position_size", float(normalized.get("position_size", 0.0)))
    normalized.setdefault("remaining_position_value", float(normalized.get("position_value", 0.0)))
    normalized.setdefault("tp1_take_fraction", config.get("tp1_take_fraction", 0.5))
    normalized.setdefault("realized_exit_costs", 0.0)
    normalized.setdefault("tp1_realized_pnl", 0.0)
    normalized.setdefault("mark_price", normalized.get("entry_price", 0.0))

    if "entry_costs" not in normalized:
        legacy_total_cost = float(normalized.get("fees_paid", 0.0))
        normalized["entry_costs"] = round(legacy_total_cost / 2, 4)
        normalized.setdefault("realized_pnl", round(-normalized["entry_costs"], 4))
        normalized.setdefault("entry_cost_applied", False)
    else:
        normalized.setdefault("realized_pnl", round(-float(normalized.get("entry_costs", 0.0)), 4))
        normalized.setdefault("entry_cost_applied", True)

    normalized["fees_paid"] = round(float(normalized.get("entry_costs", 0.0)) + float(normalized.get("realized_exit_costs", 0.0)), 4)
    normalized["remaining_position_value"] = round(float(normalized["remaining_position_size"]) * float(normalized.get("mark_price", normalized.get("entry_price", 0.0))), 2)
    return normalized


def realize_partial_exit(trade, exit_price, config):
    take_fraction = min(max(float(trade.get("tp1_take_fraction", config.get("tp1_take_fraction", 0.5))), 0.1), 0.9)
    exit_size = float(trade.get("remaining_position_size", 0.0)) * take_fraction
    if exit_size <= 0:
        return 0.0

    if trade["direction"] == "long":
        gross_pnl = (exit_price - trade["entry_price"]) * exit_size
    else:
        gross_pnl = (trade["entry_price"] - exit_price) * exit_size

    exit_value = abs(exit_price * exit_size)
    exit_costs = exit_value * exit_cost_rate(config)
    net_pnl = gross_pnl - exit_costs

    remaining_size = max(float(trade.get("remaining_position_size", 0.0)) - exit_size, 0.0)
    trade["remaining_position_size"] = round(remaining_size, 8)
    trade["remaining_position_value"] = round(remaining_size * exit_price, 2)
    trade["realized_pnl"] = round(float(trade.get("realized_pnl", 0.0)) + net_pnl, 4)
    trade["tp1_realized_pnl"] = round(float(trade.get("tp1_realized_pnl", 0.0)) + net_pnl, 4)
    trade["realized_exit_costs"] = round(float(trade.get("realized_exit_costs", 0.0)) + exit_costs, 4)
    trade["fees_paid"] = round(float(trade.get("entry_costs", 0.0)) + float(trade.get("realized_exit_costs", 0.0)), 4)
    return net_pnl


def close_trade(trade, exit_price, exit_reason, config):
    remaining_size = float(trade.get("remaining_position_size", trade.get("position_size", 0.0)))
    if trade["direction"] == "long":
        gross_remaining = (exit_price - trade["entry_price"]) * remaining_size
    else:
        gross_remaining = (trade["entry_price"] - exit_price) * remaining_size

    exit_value = abs(exit_price * remaining_size)
    exit_costs = exit_value * exit_cost_rate(config)
    pnl_close_leg = gross_remaining - exit_costs
    total_fees = round(float(trade.get("entry_costs", 0.0)) + float(trade.get("realized_exit_costs", 0.0)) + exit_costs, 4)
    total_pnl = round(float(trade.get("realized_pnl", 0.0)) + pnl_close_leg, 4)
    hold_duration = round(
        (datetime.now(timezone.utc) - datetime.fromisoformat(trade["entry_time"])).total_seconds() / 3600,
        1,
    )

    closed_trade = {
        **trade,
        "remaining_position_size": 0.0,
        "remaining_position_value": 0.0,
        "exit_price": round(exit_price, 6),
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "exit_reason": exit_reason,
        "fees_paid": total_fees,
        "pnl_close_leg": round(pnl_close_leg, 4),
        "pnl_usd": total_pnl,
        "pnl_pct": round((total_pnl / trade["position_value"]) * 100, 2) if trade["position_value"] > 0 else 0,
        "r_multiple": round(total_pnl / trade["risk_amount"], 2) if trade["risk_amount"] > 0 else 0,
        "hold_duration_hours": hold_duration,
    }
    return closed_trade, pnl_close_leg


def liquidate_all_trades(exchange, config, reason):
    active = load_json(TRADES_ACTIVE, [])
    if not active:
        return []

    history = load_json(TRADES_HISTORY, [])
    closed = []
    still_active = []

    for raw_trade in active:
        trade = normalize_trade_state(raw_trade, config)
        if not trade.get("entry_cost_applied", True):
            apply_capital_delta(config, -trade["entry_costs"])
            trade["entry_cost_applied"] = True

        try:
            current_price = exchange.fetch_ticker(trade["symbol"])["last"]
        except Exception:
            still_active.append(trade)
            continue

        trade["mark_price"] = current_price
        trade["remaining_position_value"] = round(float(trade.get("remaining_position_size", 0.0)) * current_price, 2)
        closed_trade, pnl_close_leg = close_trade(trade, current_price, reason, config)
        history.append(closed_trade)
        closed.append(closed_trade)
        apply_capital_delta(config, pnl_close_leg, trade_closed=True)
        # Update empirical hypothesis tracking for this closed trade.
        tracking = load_hypothesis_tracking()
        update_hypotheses_on_close(closed_trade, tracking)
        save_hypothesis_tracking(tracking)

    save_json(TRADES_ACTIVE, still_active)
    save_json(TRADES_HISTORY, history)
    sync_experiment_tracking(normalize_research_feedback(load_research_feedback()))
    return closed


def can_open_trade(config, active_trades, signal):
    """Prüfe ob ein neuer Trade eröffnet werden darf."""
    capital = get_portfolio_value(config, active_trades)

    if is_excluded_asset(config, signal.get("symbol")):
        return False, f"Asset policy blockiert {signal.get('symbol')}", None

    # Max Positionen
    if len(active_trades) >= config["max_positions"]:
        return False, "Max Positionen erreicht", None

    # Bereits in diesem Symbol?
    for t in active_trades:
        if t["symbol"] == signal.get("symbol"):
            return False, f"Bereits in {signal['symbol']}", None

    # Risk/Reward Check
    if not is_actionable_signal(config, signal):
        return False, f"R:R zu niedrig ({signal.get('rr_ratio', 0)})", None

    plan = build_trade_plan(config, capital, active_trades, signal)
    if not plan.get("ok"):
        return False, plan.get("reason", "Ungültiger Trade-Plan"), plan

    if plan["existing_value"] + plan["position_value"] > plan["max_exposure"]:
        return False, f"Exposure Limit ({plan['existing_value'] + plan['position_value']:.0f}>{plan['max_exposure']:.0f})", plan

    if plan["fee_burden_pct"] > 10:
        return False, f"Fees zu hoch ({plan['fee_burden_pct']:.1f}% des TP1-Profits)", plan

    if not signal.get("volume_confirmed", True):
        return False, "Volume-Bestätigung fehlt", plan

    return True, "OK", plan


def open_paper_trade(config, symbol, signal, plan=None):
    """Eröffne einen Paper Trade."""
    active = load_json(TRADES_ACTIVE, [])
    capital = get_portfolio_value(config, active)

    plan = plan or build_trade_plan(config, capital, active, signal)
    if not plan.get("ok"):
        return None

    if plan["existing_value"] + plan["position_value"] > plan["max_exposure"]:
        return None

    trade = {
        "id": f"paper_{int(time.time())}_{symbol.replace('/', '_')}",
        "symbol": symbol,
        "direction": signal["direction"],
        "entry_price": signal["price"],
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "position_size": round(plan["position_size"], 8),
        "remaining_position_size": round(plan["position_size"], 8),
        "position_value": round(plan["position_value"], 2),
        "remaining_position_value": round(plan["position_value"], 2),
        "stop_loss": signal["stop_loss"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "atr": signal["indicators"]["atr"],
        "risk_amount": round(plan["risk_amount"], 2),
        "signal_strength": signal["signal_strength"],
        "signal_source": "scanner_v1",
        "tp1_hit": False,
        "tp1_take_fraction": config.get("tp1_take_fraction", 0.5),
        "trailing_stop": None,
        "entry_costs": round(plan["estimated_entry_costs"], 4),
        "entry_cost_applied": True,
        "realized_exit_costs": 0.0,
        "realized_pnl": round(-plan["estimated_entry_costs"], 4),
        "tp1_realized_pnl": 0.0,
        "estimated_roundtrip_costs": round(plan["estimated_roundtrip_costs"], 4),
        "fees_paid": round(plan["estimated_entry_costs"], 4),
        # Soft hypothesis linkage: records which research hypotheses were active
        # for this asset when the trade opened. NOT a causal link. Scanner signal
        # logic is independent — these IDs are for empirical tracking only.
        "hypothesis_ids": link_hypotheses_to_trade(
            {"symbol": symbol}, load_hypothesis_tracking()
        ),
        "experiment_ids": link_experiments_to_trade(
            {"symbol": symbol}, load_experiment_tracking()
        ),
    }

    active.append(trade)
    save_json(TRADES_ACTIVE, active)
    apply_capital_delta(config, -plan["estimated_entry_costs"])
    sync_experiment_tracking(normalize_research_feedback(load_research_feedback()))
    return trade


def check_active_trades(exchange, config):
    """Prüfe aktive Trades gegen aktuelle Preise."""
    active = load_json(TRADES_ACTIVE, [])
    if not active:
        return []

    history = load_json(TRADES_HISTORY, [])
    closed = []
    still_active = []

    for raw_trade in active:
        trade = normalize_trade_state(raw_trade, config)
        if not trade.get("entry_cost_applied", True):
            apply_capital_delta(config, -trade["entry_costs"])
            trade["entry_cost_applied"] = True

        try:
            ticker = exchange.fetch_ticker(trade["symbol"])
            current_price = ticker["last"]
        except Exception:
            still_active.append(trade)
            continue

        trade["mark_price"] = current_price
        trade["remaining_position_value"] = round(float(trade.get("remaining_position_size", 0.0)) * current_price, 2)
        exit_reason = None
        exit_price = current_price

        if trade["direction"] == "long":
            # Check Stop Loss
            effective_stop = trade.get("trailing_stop") or trade["stop_loss"]
            if current_price <= effective_stop:
                exit_reason = "stop_loss"
                exit_price = effective_stop

            # Check TP1
            elif not trade["tp1_hit"] and current_price >= trade["tp1"]:
                trade["tp1_hit"] = True
                trade["trailing_stop"] = max(trade["entry_price"], current_price - trade["atr"])
                partial_pnl = realize_partial_exit(trade, trade["tp1"], config)
                apply_capital_delta(config, partial_pnl)
                partial_pct = int(round(float(trade.get("tp1_take_fraction", config.get("tp1_take_fraction", 0.5))) * 100))
                print(f"  ✓ {trade['symbol']}: TP1 erreicht @ {trade['tp1']:.4f} — {partial_pct}% realisiert ({partial_pnl:+.2f}€), Trailing Stop aktiv")
                still_active.append(trade)
                continue

            # Check TP2
            elif current_price >= trade["tp2"]:
                exit_reason = "tp2"
                exit_price = trade["tp2"]

            # Update Trailing Stop
            elif trade.get("trailing_stop"):
                new_trail = max(trade["entry_price"], current_price - trade["atr"])
                if new_trail > trade["trailing_stop"]:
                    trade["trailing_stop"] = new_trail

            # Time Stop (5 Tage)
            entry_time = datetime.fromisoformat(trade["entry_time"])
            if exit_reason is None and datetime.now(timezone.utc) - entry_time > timedelta(days=5) and not trade["tp1_hit"]:
                exit_reason = "time_stop"

        elif trade["direction"] == "short":
            effective_stop = trade.get("trailing_stop") or trade["stop_loss"]
            if current_price >= effective_stop:
                exit_reason = "stop_loss"
                exit_price = effective_stop
            elif not trade["tp1_hit"] and current_price <= trade["tp1"]:
                trade["tp1_hit"] = True
                trade["trailing_stop"] = min(trade["entry_price"], current_price + trade["atr"])
                partial_pnl = realize_partial_exit(trade, trade["tp1"], config)
                apply_capital_delta(config, partial_pnl)
                partial_pct = int(round(float(trade.get("tp1_take_fraction", config.get("tp1_take_fraction", 0.5))) * 100))
                print(f"  ✓ {trade['symbol']}: TP1 erreicht @ {trade['tp1']:.4f} — {partial_pct}% realisiert ({partial_pnl:+.2f}€), Trailing Stop aktiv")
                still_active.append(trade)
                continue
            elif current_price <= trade["tp2"]:
                exit_reason = "tp2"
                exit_price = trade["tp2"]
            elif trade.get("trailing_stop"):
                new_trail = min(trade["entry_price"], current_price + trade["atr"])
                if new_trail < trade["trailing_stop"]:
                    trade["trailing_stop"] = new_trail
            entry_time = datetime.fromisoformat(trade["entry_time"])
            if exit_reason is None and datetime.now(timezone.utc) - entry_time > timedelta(days=5) and not trade["tp1_hit"]:
                exit_reason = "time_stop"

        if exit_reason:
            closed_trade, pnl_close_leg = close_trade(trade, exit_price, exit_reason, config)
            history.append(closed_trade)
            closed.append(closed_trade)
            apply_capital_delta(config, pnl_close_leg, trade_closed=True)
            # Update empirical hypothesis tracking for this closed trade.
            tracking = load_hypothesis_tracking()
            update_hypotheses_on_close(closed_trade, tracking)
            save_hypothesis_tracking(tracking)
        else:
            still_active.append(trade)

    save_json(TRADES_ACTIVE, still_active)
    save_json(TRADES_HISTORY, history)
    sync_experiment_tracking(normalize_research_feedback(load_research_feedback()))
    return closed


# ── Haupt-Commands ───────────────────────────────────────────────────────────

def cmd_scan():
    """Hauptbefehl: Markt scannen, Signale generieren, Paper-Trades verwalten."""
    require_dependencies("ccxt", "numpy", "pandas")
    config = load_config()
    exchange = get_exchange(config)
    now = datetime.now(timezone.utc)

    print(f"{'═' * 60}")
    print(f"  MARKT-SCAN — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 60}")
    print(f"  Exchange: {config['exchange']}  |  Timeframe: {config['timeframe']}")
    print(f"  Watchlist: {len(config['watchlist'])} Coins")
    if config.get('excluded_assets'):
        print(f"  Excluded: {', '.join(config.get('excluded_assets', []))}")
    trial_summary = (config.get('_scanner_hints') or {}).get('experiment_trials', {})
    if trial_summary.get('trial_count'):
        print(
            f"  Experiment-Trials: {trial_summary['trial_count']} "
            f"| ready={len(trial_summary.get('scanner_ready_symbols', []))} "
            f"| status={trial_summary.get('status_counts', {})}"
        )
    print(f"{'─' * 60}")

    # 1. Aktive Trades prüfen
    active = load_json(TRADES_ACTIVE, [])
    if active:
        print(f"\n  ── AKTIVE TRADES PRÜFEN ({len(active)}) ──")
        closed = check_active_trades(exchange, config)
        for c in closed:
            emoji = "✓" if c["pnl_usd"] > 0 else "✗"
            print(f"  {emoji} {c['symbol']} GESCHLOSSEN: {c['exit_reason']} | "
                  f"P&L: {c['pnl_usd']:+.2f}€ ({c['pnl_pct']:+.1f}%) | "
                  f"R: {c['r_multiple']:+.1f}")
        active = load_json(TRADES_ACTIVE, [])  # Reload after closing

    history = load_json(TRADES_HISTORY, [])
    perf = load_performance(config)
    guardrails = get_trading_guardrails(config, history, perf)
    crash_mode = detect_crash_mode(exchange, config)

    if guardrails["force_close"] and active:
        print(f"\n  ── CIRCUIT BREAKER AKTIV ──")
        print(f"  {guardrails['reason']} — schliesse alle offenen Positionen")
        liquidated = liquidate_all_trades(exchange, config, "circuit_breaker")
        for trade in liquidated:
            print(f"  ✗ {trade['symbol']} liquidiert | P&L: {trade['pnl_usd']:+.2f}€")
        active = load_json(TRADES_ACTIVE, [])
        history = load_json(TRADES_HISTORY, [])
        perf = load_performance(config)
        guardrails = get_trading_guardrails(config, history, perf)

    # 2. Watchlist scannen
    print(f"\n  ── WATCHLIST SCANNEN ──")
    if trial_summary.get('top'):
        print(f"  Aktive Trial-Kandidaten:")
        for trial in trial_summary['top'][:3]:
            line = (
                f"    • [{trial.get('translation_status')}] {trial.get('title', '')[:48]}"
                f" | symbol={trial.get('scanner_symbol') or 'n/a'}"
                f" | bias={trial.get('trial_bias', 0.0):.3f}"
            )
            print(line)
            if trial.get('block_reason'):
                print(f"      {trial['block_reason']}")
            else:
                print(f"      {trial.get('next_action')}")
    signals = []

    for symbol in config["watchlist"]:
        df = fetch_ohlcv(exchange, symbol, config["timeframe"])
        if df is None:
            continue

        df = compute_indicators(df, config)
        signal = generate_signal(df, config)
        if signal is None:
            continue

        signal["symbol"] = symbol
        signals.append(signal)

        # Display
        direction_emoji = {"long": "🟢", "short": "🔴", "flat": "⚪"}
        emoji = direction_emoji.get(signal["direction"], "⚪")
        strength_bar = "█" * int(signal["signal_strength"] * 10) + "░" * (10 - int(signal["signal_strength"] * 10))

        print(f"  {emoji} {symbol:12s} ${signal['price']:>10.4f}  "
              f"{signal['direction']:5s} [{strength_bar}] {signal['signal_strength']:.2f}  "
              f"RSI:{signal['indicators']['rsi']:5.1f}  "
              f"R:R {signal['rr_ratio']:.1f}  "
              f"{signal['regime']}")

    # 3. Paper Trades eröffnen
    actionable = [s for s in signals if is_actionable_signal(config, s)]

    # Apply research asset_bias as a tie-breaker for scan order ONLY.
    # This nudges which candidate is tried first — it does NOT change thresholds,
    # does NOT bypass signal checks, and does NOT force any trade to open.
    # Risk guardrails (can_open_trade) are enforced identically for every candidate.
    hints = config.get('_scanner_hints', {})
    asset_bias = hints.get('asset_bias', {})
    actionable.sort(
        key=lambda x: x["signal_strength"] + asset_bias.get(x.get("symbol", ""), 0.0),
        reverse=True,
    )

    trading_block_reason = None
    if guardrails["paused"]:
        trading_block_reason = guardrails["reason"]
    elif crash_mode["enabled"]:
        trading_block_reason = (
            f"Crash-Mode aktiv (BTC 1D {crash_mode['daily_change_pct']:+.1f}% / "
            f"{crash_mode['cumulative_change_pct']:+.1f}% in {config['crash_lookback_days']}d)"
        )

    if trading_block_reason:
        print(f"\n  ── KEINE NEUEN TRADES ──")
        print(f"  {trading_block_reason}")
    elif actionable:
        print(f"\n  ── NEUE PAPER-TRADES ──")
        for signal in actionable:
            can, reason, plan = can_open_trade(config, active, signal)
            if can:
                trade = open_paper_trade(config, signal["symbol"], signal, plan)
                if trade:
                    print(f"  ✓ ERÖFFNET: {trade['symbol']} {trade['direction'].upper()} "
                          f"@ {trade['entry_price']:.4f} | "
                          f"Size: {trade['position_value']:.2f}€ | "
                          f"SL: {trade['stop_loss']:.4f} | "
                          f"TP1: {trade['tp1']:.4f} | "
                          f"Fees: {trade['entry_costs']:.2f}€")
                    active = load_json(TRADES_ACTIVE, [])
            else:
                print(f"  ─ {signal['symbol']} übersprungen: {reason}")

    # 4. Zusammenfassung
    perf = load_performance(config)
    capital = perf.get("capital", config["initial_capital"])
    total_trades = perf.get("trades_total", 0)
    pnl_total = capital - config["initial_capital"]
    pnl_pct = (pnl_total / config["initial_capital"]) * 100

    print(f"\n{'═' * 60}")
    print(f"  PORTFOLIO: {capital:.2f}€  ({pnl_pct:+.1f}%)")
    print(f"  Offene Pos: {len(active)}  |  Trades total: {total_trades}")
    print(f"  Peak/MaxDD: {perf.get('peak_capital', capital):.2f}€ / {perf.get('max_drawdown_pct', 0.0):.1f}%")
    if config.get("_research_overrides"):
        h = config.get('_scanner_hints', {})
        print(f"  Research: {h.get('valid_hypothesis_count', 0)} valid hypotheses "
              f"| caution={h.get('caution_factor', 0):.2f} "
              f"| bias on: {', '.join(h.get('asset_bias', {}).keys()) or 'none'}")
        if h.get('experiment_trials'):
            t = h['experiment_trials']
            print(
                f"  Trials: {t.get('trial_count', 0)} total "
                f"| ready symbols: {', '.join(t.get('scanner_ready_symbols', [])) or 'none'}"
            )
        if h.get('excluded_assets'):
            print(f"  Excluded Assets: {', '.join(h.get('excluded_assets', []))}")
    if crash_mode["enabled"]:
        print(f"  Status: Crash-Mode aktiv")
    elif guardrails["paused"]:
        print(f"  Status: Trading pausiert ({guardrails['reason']})")
    print(f"{'═' * 60}")

    # Scan-Log speichern
    scan_log = load_json(SCAN_LOG, [])
    scan_log.append({
        "time": now.isoformat(),
        "signals": len(signals),
        "actionable": len(actionable),
        "active_trades": len(active),
        "capital": capital,
        "paused": guardrails["paused"],
        "pause_reason": guardrails["reason"],
        "crash_mode": crash_mode["enabled"],
    })
    # Nur letzte 500 Scans behalten
    save_json(SCAN_LOG, scan_log[-500:])

    return {
        "signals_scanned": len(signals),
        "actionable_signals": len(actionable),
        "active_trades": len(active),
        "capital": capital,
        "paused": guardrails["paused"],
        "pause_reason": guardrails["reason"],
        "crash_mode": crash_mode["enabled"],
        "research_active": bool(config.get("_research_overrides")),
        "experiment_trial_count": trial_summary.get('trial_count', 0),
    }


def cmd_report():
    """Tages-Report."""
    config = load_config()
    feedback = normalize_research_feedback(load_research_feedback())
    now = datetime.now(timezone.utc)

    print(f"\n{'═' * 60}")
    print(f"  TAGES-REPORT — {now.strftime('%Y-%m-%d')}")
    print(f"{'═' * 60}")
    runtime = build_runtime_snapshot(feedback)
    print(f"\n  ── RUNTIME ──")
    print(f"  Research last run: {runtime['intake_analysis']['last_generated_at'] or 'n/a'}")
    print(f"  Scanner last ok:   {(((runtime['commands'].get('scan') or {}).get('last_success_at')) or 'n/a')}")
    print(f"  Report last ok:    {(((runtime['commands'].get('report') or {}).get('last_success_at')) or 'n/a')}")
    print(f"  Gateway/Telegram:  {runtime['gateway']['state'] or 'n/a'} / {runtime['gateway']['telegram_state'] or 'n/a'}")
    print(f"  Cron jobs aktiv:   {runtime['cron']['jobs_enabled']}/{runtime['cron']['jobs_total']}")

    # Performance
    perf = load_performance(config)
    capital = perf.get("capital", config["initial_capital"])
    pnl_total = capital - config["initial_capital"]
    pnl_pct = (pnl_total / config["initial_capital"]) * 100
    guardrails = get_trading_guardrails(config, load_json(TRADES_HISTORY, []), perf)

    print(f"  Portfolio:    {capital:.2f}€ ({pnl_pct:+.1f}%)")
    print(f"  Start:        {config['initial_capital']:.2f}€")
    print(f"  P&L Total:    {pnl_total:+.2f}€")
    print(f"  Peak Capital: {perf.get('peak_capital', capital):.2f}€")
    print(f"  Max DD:       {perf.get('max_drawdown_pct', 0.0):.1f}%")
    if guardrails["paused"]:
        print(f"  Guardrail:    {guardrails['reason']}")
    if config.get("_research_overrides"):
        h = config.get('_scanner_hints', {})
        print(f"  Research:     {h.get('valid_hypothesis_count', 0)} valid hypotheses "
              f"| learnings={h.get('research_learning_count', 0)}"
              f" | experiments={h.get('experiment_count', 0)}"
              f" | caution={h.get('caution_factor', 0):.2f}")

    # Aktive Trades
    active = load_json(TRADES_ACTIVE, [])
    print(f"\n  ── OFFENE POSITIONEN ({len(active)}) ──")
    if active:
        for raw_trade in active:
            t = normalize_trade_state(raw_trade, config)
            print(f"  • {t['symbol']} {t['direction'].upper()} @ {t['entry_price']:.4f}"
                  f" | Size: {t['remaining_position_value']:.2f}€"
                  f" | SL: {t['stop_loss']:.4f} | TP1: {t['tp1']:.4f}"
                  f" | TP1 hit: {'✓' if t.get('tp1_hit') else '✗'}"
                  f" | Realisiert: {t.get('tp1_realized_pnl', 0.0):+.2f}€")
    else:
        print("  (keine)")

    # Trade History (letzte 7 Tage)
    history = load_json(TRADES_HISTORY, [])
    week_ago = now - timedelta(days=7)
    recent = [t for t in history if datetime.fromisoformat(t["exit_time"]) > week_ago]

    print(f"\n  ── LETZTE 7 TAGE ({len(recent)} Trades) ──")
    if recent:
        wins = [t for t in recent if t["pnl_usd"] > 0]
        losses = [t for t in recent if t["pnl_usd"] <= 0]
        total_pnl = sum(t["pnl_usd"] for t in recent)
        win_rate = len(wins) / len(recent) * 100 if recent else 0
        avg_win = statistics.mean([t["pnl_usd"] for t in wins]) if wins else 0
        avg_loss = statistics.mean([t["pnl_usd"] for t in losses]) if losses else 0

        print(f"  Wins/Losses:  {len(wins)}/{len(losses)} ({win_rate:.0f}% Win Rate)")
        print(f"  P&L Woche:    {total_pnl:+.2f}€")
        print(f"  Avg Win:      {avg_win:+.2f}€")
        print(f"  Avg Loss:     {avg_loss:+.2f}€")
        print(f"  Profit Factor: {abs(sum(t['pnl_usd'] for t in wins)) / abs(sum(t['pnl_usd'] for t in losses)):.2f}" if losses and sum(t['pnl_usd'] for t in losses) != 0 else "")

        print(f"\n  Details:")
        for t in sorted(recent, key=lambda x: x["exit_time"], reverse=True):
            emoji = "✓" if t["pnl_usd"] > 0 else "✗"
            print(f"    {emoji} {t['symbol']:12s} {t['direction']:5s} "
                  f"{t['pnl_usd']:+7.2f}€ ({t['pnl_pct']:+5.1f}%) "
                  f"R:{t['r_multiple']:+4.1f}  {t['exit_reason']:10s} "
                  f"{t.get('hold_duration_hours', 0):.0f}h")
    else:
        print("  (keine Trades)")

    # Scan Activity
    scan_log = load_json(SCAN_LOG, [])
    recent_scans = [s for s in scan_log if datetime.fromisoformat(s["time"]) > week_ago]
    print(f"\n  ── SCAN AKTIVITÄT ──")
    print(f"  Scans diese Woche: {len(recent_scans)}")

    print(f"\n  ── RESEARCH-LEARNINGS ──")
    learnings = feedback.get('research_learnings', [])
    experiments = feedback.get('experiments', [])
    print(f"  Learnings aktiv: {len(learnings)}  |  Experimente offen: {len(experiments)}")
    for item in learnings[:3]:
        assets = ','.join(item.get('assets', [])) or 'n/a'
        print(f"    • {item.get('title', '')[:70]} [{item.get('type', 'learning')}] assets={assets}")
    for exp in experiments[:3]:
        print(f"    → Experiment: {exp.get('title', '')[:60]} | asset={exp.get('asset') or 'n/a'}")

    experiment_summary = _experiment_summary_for_display(load_experiment_tracking())
    experiment_trials = _experiment_trials_summary(load_experiment_trials())
    print(f"\n  ── EXPERIMENT-TRACKING ──")
    print(f"  Gesamt: {experiment_summary['total']}  |  Status: {experiment_summary['status_counts']}")
    print(f"  Top-Experimente:")
    for item in experiment_summary['top'][:3]:
        print(
            f"    • [{item['status']}] {item['title']} | trades={item['linked_trades']}"
            f" | closed={item['closed_trades']} | Exp {item['expectancy']:+.2f}€"
            f" | PF {item['profit_factor']} | ØR {item['avg_r_multiple']:+.2f}"
            f" | Hold {item['avg_hold_hours']:.1f}h | Fees {item['fee_burden_pct']:.1f}%"
        )
        if item.get('decision_reason'):
            print(f"      {item['decision_reason']}")
    if experiment_summary['flop']:
        print(f"  Flop-Experimente:")
        for item in experiment_summary['flop'][:3]:
            print(
                f"    • [{item['status']}] {item['title']} | trades={item['linked_trades']}"
                f" | closed={item['closed_trades']} | Exp {item['expectancy']:+.2f}€"
                f" | PF {item['profit_factor']} | ØR {item['avg_r_multiple']:+.2f}"
                f" | Hold {item['avg_hold_hours']:.1f}h | Fees {item['fee_burden_pct']:.1f}%"
            )
            if item.get('decision_reason'):
                print(f"      {item['decision_reason']}")

    print(f"\n  ── EXPERIMENT-TRIALS ──")
    print(f"  Gesamt: {experiment_trials['trial_count']}  |  Status: {experiment_trials['status_counts']}")
    for item in experiment_trials['top'][:5]:
        print(
            f"    • [{item.get('translation_status')}] {item.get('title', '')[:60]}"
            f" | symbol={item.get('scanner_symbol') or 'n/a'}"
            f" | bias={item.get('trial_bias', 0.0):.3f}"
            f" | closed={item.get('closed_trade_count', 0)}"
        )
        if item.get('block_reason'):
            print(f"      {item['block_reason']}")
        else:
            print(f"      {item.get('next_action')}")

    latest_recommendations = load_optimizer_recommendations()
    latest_decisions = load_optimizer_decisions()
    if latest_recommendations.get('recommendations'):
        print(f"\n  ── LETZTE OPTIMIZER-AKTIONEN ──")
        generated_at = latest_recommendations.get('generated_at') or 'n/a'
        print(f"  Snapshot: {generated_at}")
        for rec in latest_recommendations.get('recommendations', [])[:3]:
            print(
                f"    • [{rec.get('priority', 0):02d}] {rec.get('action')} | {rec.get('title', '')[:52]}"
                f" | stage={rec.get('promotion_stage', 'n/a')}"
                f" | status={rec.get('status')} | asset={rec.get('asset') or 'n/a'}"
            )
            if rec.get('reason'):
                print(f"      {rec['reason']}")
            if rec.get('next_gate'):
                print(f"      Next: {rec['next_gate']}")
    if latest_decisions.get('decisions'):
        print(f"\n  ── PROMOTION-STUFEN ──")
        print(f"  Stages: {latest_decisions.get('stage_counts', {})}")
        print(f"  Lanes:  {latest_decisions.get('execution_lane_counts', {})}")

    # Hypothesis tracking summary
    ht = _hypothesis_summary_for_display(load_hypothesis_tracking())
    print(f"\n  ── HYPOTHESEN-TRACKING ──")
    print(f"  Gesamt verfolgt: {ht['total_tracked']}  |  Getestet: {ht['total_tested']}")
    print(f"  Ø Hypothesis Score: {ht['avg_hypothesis_score']:.3f}")
    if ht['top_hypotheses']:
        print(f"  Top Hypothesen:")
        for h in ht['top_hypotheses']:
            wr = f" | WR {h['win_rate']:.0%}" if h['win_rate'] is not None else ''
            print(f"    + [{h['score']:.2f}] {h['title']} ({h['tested']} tests{wr}, ØP&L {h['avg_pnl']:+.2f}€) {h['assets']}")
    if ht['worst_hypotheses']:
        print(f"  Schwache Hypothesen:")
        for h in ht['worst_hypotheses']:
            print(f"    - [{h['score']:.2f}] {h['title']} ({h['tested']} tests, ØP&L {h['avg_pnl']:+.2f}€)")

    print(f"\n{'═' * 60}")
    return {
        "capital": capital,
        "pnl_total": round(pnl_total, 2),
        "active_trades": len(active),
        "recent_trades": len(recent),
        "recent_scans": len(recent_scans),
        "learning_count": len(learnings),
        "experiment_count": len(experiments),
        "experiment_status_counts": experiment_summary['status_counts'],
        "experiment_trial_status_counts": experiment_trials['status_counts'],
    }


def _hypothesis_summary_for_display(tracking, top_n=3):
    """Return a compact summary dict suitable for status/report output."""
    all_h = list(tracking.values())
    tested = [h for h in all_h if h.get('times_tested', 0) > 0]
    scored = sorted(tested, key=lambda h: h.get('hypothesis_score', 0.0), reverse=True)

    return {
        'total_tracked': len(all_h),
        'total_tested': len(tested),
        'avg_hypothesis_score': round(
            sum(h.get('hypothesis_score', 0.0) for h in tested) / len(tested), 4
        ) if tested else 0.0,
        'top_hypotheses': [
            {'title': h['title'][:60], 'score': h.get('hypothesis_score', 0.0),
             'tested': h.get('times_tested', 0), 'win_rate': round(
                 h.get('times_profitable', 0) / h['times_tested'], 2
             ) if h.get('times_tested', 0) > 0 else None,
             'avg_pnl': h.get('avg_pnl', 0.0), 'assets': h.get('assets', [])}
            for h in scored[:top_n]
        ],
        'worst_hypotheses': [
            {'title': h['title'][:60], 'score': h.get('hypothesis_score', 0.0),
             'tested': h.get('times_tested', 0),
             'avg_pnl': h.get('avg_pnl', 0.0), 'assets': h.get('assets', [])}
            for h in reversed(scored[-top_n:]) if scored
        ],
    }


def cmd_status():
    """Schneller Portfolio-Status."""
    config = load_config()
    feedback = normalize_research_feedback(load_research_feedback())
    perf = load_performance(config)
    active = load_json(TRADES_ACTIVE, [])
    history = load_json(TRADES_HISTORY, [])
    capital = perf.get("capital", config["initial_capital"])
    guardrails = get_trading_guardrails(config, history, perf)
    experiment_tracking = load_experiment_tracking()
    experiment_trials = load_experiment_trials()
    optimizer_decisions = load_optimizer_decisions()

    payload = {
        "capital": capital,
        "initial": config["initial_capital"],
        "pnl_total": round(capital - config["initial_capital"], 2),
        "pnl_pct": round((capital - config["initial_capital"]) / config["initial_capital"] * 100, 2),
        "peak_capital": perf.get("peak_capital", capital),
        "max_drawdown_pct": perf.get("max_drawdown_pct", 0.0),
        "active_trades": len(active),
        "total_trades": len(history),
        "guardrails": guardrails,
        "research_hints": config.get("_scanner_hints", {}),
        "research_summary": {
            "learning_count": len(feedback.get("research_learnings", [])),
            "experiment_count": len(feedback.get("experiments", [])),
            "priority_assets": feedback.get("priority_assets", []),
        },
        "hypothesis_tracking": _hypothesis_summary_for_display(load_hypothesis_tracking()),
        "experiment_tracking": _experiment_summary_for_display(experiment_tracking),
        "experiment_trials": _experiment_trials_summary(experiment_trials),
        "optimizer_decisions": {
            "stage_counts": optimizer_decisions.get('stage_counts', {}),
            "execution_lane_counts": optimizer_decisions.get('execution_lane_counts', {}),
        },
        "active_details": [{"symbol": t["symbol"], "direction": t["direction"],
                           "entry": t["entry_price"],
                           "remaining_value": normalize_trade_state(t, config).get("remaining_position_value", t.get("position_value", 0)),
                           "tp1_hit": t.get("tp1_hit", False)}
                          for t in active],
        "runtime": build_runtime_snapshot(feedback),
    }
    print(json.dumps(payload, indent=2))
    return payload


def cmd_optimize():
    """Wöchentliche Strategie-Optimierung."""
    require_dependencies("pandas")
    config = load_config()
    feedback = normalize_research_feedback(load_research_feedback())
    history = load_json(TRADES_HISTORY, [])
    perf = load_performance(config)
    tracking = load_experiment_tracking()
    experiment_summary = _experiment_summary_for_display(tracking)
    trial_snapshot = load_experiment_trials()
    optimizer_snapshot = build_optimizer_recommendations(tracking, trial_snapshot, config, len(history))
    save_optimizer_recommendations(optimizer_snapshot)
    decision_snapshot = build_optimizer_decisions(optimizer_snapshot)
    save_optimizer_decisions(decision_snapshot)

    print(f"\n{'═' * 60}")
    print(f"  WÖCHENTLICHE OPTIMIERUNG")
    print(f"{'═' * 60}")

    print(f"\n  ── EXPERIMENT-STATUS ──")
    print(f"  Status: {experiment_summary['status_counts']}")
    for item in experiment_summary['top'][:5]:
        print(
            f"  • [{item['status']}] {item['title']} | trades={item['linked_trades']}"
            f" | closed={item['closed_trades']} | Exp {item['expectancy']:+.2f}€"
            f" | PF {item['profit_factor']} | ØR {item['avg_r_multiple']:+.2f}"
        )
        if item.get('decision_reason'):
            print(f"    {item['decision_reason']}")

    print(f"\n  ── KONTROLLIERTE AKTIONEN ──")
    for rec in optimizer_snapshot.get('recommendations', [])[:5]:
        evidence = rec.get('evidence', {})
        print(
            f"  • [{rec.get('priority', 0):02d}] {rec.get('action')} | {rec.get('title', '')[:52]}"
            f" | stage={rec.get('promotion_stage', 'n/a')}"
            f" | lane={rec.get('execution_lane', 'n/a')}"
            f" | status={rec.get('status')} | closed={evidence.get('closed_trades', 0)}"
            f" | Exp {evidence.get('expectancy', 0.0):+.2f}€ | PF {evidence.get('profit_factor')}"
        )
        if rec.get('reason'):
            print(f"    {rec['reason']}")
        if rec.get('next_gate'):
            print(f"    Next: {rec['next_gate']}")
        for change in rec.get('proposed_changes', [])[:2]:
            print(f"    Vorschlag: {change}")

    print(f"\n  ── PROMOTION-STUFEN ──")
    print(f"  Stages: {decision_snapshot.get('stage_counts', {})}")
    print(f"  Lanes:  {decision_snapshot.get('execution_lane_counts', {})}")

    if len(history) < 10:
        print(f"  Noch zu wenig Trades ({len(history)}/10) für Optimierung.")
        print(f"  Weiter Paper-Trading sammeln.")
        return {
            "optimized": False,
            "reason": "not_enough_trades",
            "trade_count": len(history),
            "experiment_status_counts": experiment_summary['status_counts'],
            "recommendation_count": optimizer_snapshot.get('recommendation_count', 0),
            "promotion_stage_counts": decision_snapshot.get('stage_counts', {}),
        }

    df = pd.DataFrame(history)

    # Gesamtstatistik
    wins = df[df["pnl_usd"] > 0]
    losses = df[df["pnl_usd"] <= 0]
    total_pnl = df["pnl_usd"].sum()
    win_rate = len(wins) / len(df) * 100

    print(f"\n  ── GESAMTSTATISTIK ({len(df)} Trades) ──")
    print(f"  Win Rate:      {win_rate:.1f}%")
    print(f"  Total P&L:     {total_pnl:+.2f}€")
    print(f"  Avg R-Multiple: {df['r_multiple'].mean():+.2f}")
    print(f"  Max Drawdown:  {perf.get('max_drawdown_pct', 0.0):.1f}%")
    print(f"  Best Trade:    {df['pnl_usd'].max():+.2f}€")
    print(f"  Worst Trade:   {df['pnl_usd'].min():+.2f}€")

    # Analyse nach Exit-Reason
    print(f"\n  ── NACH EXIT-GRUND ──")
    for reason, group in df.groupby("exit_reason"):
        grp_pnl = group["pnl_usd"].sum()
        grp_win = (group["pnl_usd"] > 0).sum()
        print(f"  {reason:12s}: {len(group):3d} trades, {grp_win}/{len(group)} wins, P&L: {grp_pnl:+.2f}€")

    # Analyse nach Symbol
    print(f"\n  ── NACH SYMBOL ──")
    for sym, group in df.groupby("symbol"):
        grp_pnl = group["pnl_usd"].sum()
        grp_win = (group["pnl_usd"] > 0).sum()
        print(f"  {sym:12s}: {len(group):3d} trades, {grp_win}/{len(group)} wins, P&L: {grp_pnl:+.2f}€")

    # Empfehlungen
    print(f"\n  ── EMPFEHLUNGEN ──")

    if win_rate < 40:
        print(f"  ⚠ Win Rate niedrig ({win_rate:.0f}%) — Signal-Schwelle erhöhen?")
        print(f"    Aktuell: min_signal_strength = {config['min_signal_strength']}")
        print(f"    Vorschlag: {min(config['min_signal_strength'] + 0.05, 0.6)}")

    avg_loss_r = losses["r_multiple"].mean() if len(losses) > 0 else 0
    if avg_loss_r < -1.2:
        print(f"  ⚠ Durchschnittlicher Loss zu gross (R={avg_loss_r:.1f}) — Stops enger?")
        print(f"    Aktuell: atr_stop_multiplier = {config['atr_stop_multiplier']}")
        print(f"    Vorschlag: {max(config['atr_stop_multiplier'] - 0.25, 0.75)}")

    time_stops = df[df["exit_reason"] == "time_stop"]
    if len(time_stops) > len(df) * 0.3:
        print(f"  ⚠ Viele Time-Stops ({len(time_stops)}/{len(df)}) — Timeframe überdenken?")

    # Losing Symbols identifizieren
    for sym, group in df.groupby("symbol"):
        if group["pnl_usd"].sum() < -20 and len(group) >= 3:
            print(f"  ⚠ {sym} verliert konstant — von Watchlist entfernen?")

    experiments = feedback.get('experiments', [])
    if experiments:
        print(f"\n  ── RESEARCH-EXPERIMENTE ──")
        for exp in experiments[:5]:
            print(f"  • {exp.get('type')}: {exp.get('title', '')[:70]} | asset={exp.get('asset') or 'n/a'}")
            if exp.get('guardrail'):
                print(f"    Guardrail: {exp.get('guardrail')}")

    print(f"\n{'═' * 60}")
    return {
        "optimized": True,
        "trade_count": len(history),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 2),
        "experiment_count": len(experiments),
        "experiment_status_counts": experiment_summary['status_counts'],
        "recommendation_count": optimizer_snapshot.get('recommendation_count', 0),
        "promotion_stage_counts": decision_snapshot.get('stage_counts', {}),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    mark_command_started(cmd)
    try:
        if cmd == "scan":
            summary = cmd_scan()
        elif cmd == "report":
            summary = cmd_report()
        elif cmd == "status":
            summary = cmd_status()
        elif cmd == "optimize":
            summary = cmd_optimize()
        else:
            print(f"Unbekannter Befehl: {cmd}")
            print("Verfügbar: scan, report, status, optimize")
            sys.exit(1)
        mark_command_finished(cmd, True, summary=summary)
    except RuntimeError as e:
        mark_command_finished(cmd, False, error=str(e))
        print(f"FEHLER: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        mark_command_finished(cmd, False, error=f"{type(e).__name__}: {e}")
        raise
