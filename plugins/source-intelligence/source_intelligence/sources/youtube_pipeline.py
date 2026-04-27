#!/usr/bin/env python3
"""
YouTube Source Intelligence Pipeline — migrated from the old trading workspace

Key changes from v1:

REMOVED (too gameable / fundamentally flawed):
  - POSITIVE_TERMS / NEGATIVE_TERMS / STRONG_NEGATIVE_TERMS: keyword counting
    cannot distinguish a scam that says "backtest" from a legitimate strategy.
  - RISK_TERMS / LIQUIDITY_TERMS: same problem — counting mentions ≠ relevance.
  - ASSET_KEYWORDS: mention frequency ≠ trading context. Replaced by LLM extraction.
  - compress_transcript_stage1: discarded everything after char 8000 and lost
    conclusions, caveats, and backtesting stats typically found at the end.
  - evaluate_bullshit_stage2: a single 0-100 "bullshit score" collapses all failure
    modes into one number and incentivises tone-matching over substance detection.
  - evaluate_summary / score_sentence / sentence_split / count_term_hits /
    low_quality_reason: all keyword-dependent fallbacks, removed entirely.
  - build_strategy_feedback (old): emitted trading config overrides derived from
    keyword hit counts. This is not a feedback loop — it's a heuristic pretending
    to be one.
  - scoring_rules / flat score counter in channel state: see channel scoring below.

NEW ARCHITECTURE (4 stages):

  Stage 1 — Structured Extraction (GPT-4.1, temperature=0.1)
    What: extract structured information from the FULL transcript (head+tail split
    if too long), not compress it. Fields: strategy_description, entry_logic,
    exit_logic, assets_mentioned (in trading context), assumptions, evidence
    (numeric, verifiable), missing_info, promotional_signals.
    Why: information architecture, not lossy summarisation.

  Stage 2 — Adversarial Evaluation (GPT-5.4, temperature=0.2)
    What: take the structured extraction and try to break it. Identify weaknesses,
    overfitting/survivorship bias flags, validation gaps, hype_ratio (0-1),
    falsifiability classification, and a 3-way verdict.
    Why: a model looking for flaws catches different things than one scoring quality.

  Stage 3 — Signal Qualification (deterministic, no LLM)
    What: classify content into a tier ladder using Stage 1 + Stage 2 outputs only.
    Tiers: potential_strategy > testable_hypothesis > idea_only > not_actionable.
    Why: deterministic logic on structured data is more reliable than a second LLM
    pass that might hallucinate or be inconsistent.

  Stage 4 — Hypothesis Output (no LLM)
    What: for tiers that are actionable, build a hypothesis record with confidence
    score, known weaknesses, and a backtest_hook placeholder.
    Why: the output of this pipeline should be research questions, not config
    overrides. A consuming system (trader profile) decides what to do with them.

CHANNEL SCORING — Redesigned:
  - Replaces flat integer counter + fixed thresholds with EMA-based weighted score.
  - Each review carries the tier weight (potential_strategy=+3, testable=+2,
    idea_only=0, not_actionable=-1).
  - EMA smoothing (alpha=0.35) means older reviews decay and a channel can recover.
  - Status changes require CHANNEL_MIN_REVIEWS reviews to prevent premature decisions.
  - Blacklist threshold: EMA <= -0.6 (roughly 4 consecutive not_actionable reviews).
  - Favorite threshold: EMA >= 1.5 (roughly 2 consecutive potential_strategy reviews).
"""
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

_ACTIVE_HERMES_HOME = Path(os.environ.get('HERMES_HOME', '').strip() or Path.home() / '.hermes')
HERMES_HOME = _ACTIVE_HERMES_HOME.parent.parent if _ACTIVE_HERMES_HOME.parent.name == 'profiles' else _ACTIVE_HERMES_HOME
ROOT = HERMES_HOME / 'data' / 'source-intelligence' / 'youtube'
CONFIG_DIR = ROOT / 'config'
STATE_DIR = ROOT / 'state'
REPORTS_DIR = ROOT / 'reports'
PROFILES_DIR = HERMES_HOME / 'profiles'
CONFIG_PATH = CONFIG_DIR / 'pipeline.json'
LEGACY_CONFIG_PATH = CONFIG_DIR / 'search_queries.json'
CHANNEL_SCORES_PATH = STATE_DIR / 'channel_scores.json'
FAVORITES_PATH = STATE_DIR / 'favorites.json'
BLACKLIST_PATH = STATE_DIR / 'blacklist.json'
MANUAL_INPUTS_PATH = STATE_DIR / 'manual_inputs.json'
SEEN_VIDEOS_PATH = STATE_DIR / 'seen_videos.json'
RUN_STATE_PATH = STATE_DIR / 'run_state.json'
RUN_DIAGNOSTICS_PATH = STATE_DIR / 'run_diagnostics.json'
RAW_RESPONSES_DIR = STATE_DIR / 'raw_model_responses'
SHARED_STRATEGY_FEEDBACK_PATH = STATE_DIR / 'strategy_feedback.json'

STATE_SCHEMA_VERSION = 2
FEEDBACK_SCHEMA_VERSION = 2

# Process up to this many chars — increase from old [:8000] first-only slice.
# Long transcripts are split head+tail so conclusions are never discarded.
MAX_TRANSCRIPT_CHARS = 12000

# Signal tier weights used for EMA channel scoring.
# Tiers that are not actionable hurt a channel's score; tiers with substance help.
# idea_only is 0 (neutral) — interesting but no track record impact.
SIGNAL_TIER_WEIGHTS = {
    'potential_strategy': 3,   # Entry+exit logic + at least one piece of evidence
    'testable_hypothesis': 2,  # Falsifiable claim with strategy description
    'idea_only': 0,            # Concept present but not actionable yet
    'not_actionable': -1,      # Promotional / vague / incoherent
}

# EMA smoothing factor. alpha=0.35 means ~2-3 reviews for strong signal to dominate.
# Older reviews decay at rate (1 - 0.35)^n — channels can recover or fall.
CHANNEL_EMA_ALPHA = 0.35

# EMA value thresholds for channel status. Computed from CHANNEL_EMA_ALPHA and
# SIGNAL_TIER_WEIGHTS — see compute_channel_ema_score() for derivation rationale.
CHANNEL_FAVORITE_EMA = 1.5    # Requires ~2 consecutive potential_strategy reviews
CHANNEL_BLACKLIST_EMA = -0.6  # Requires ~4 consecutive not_actionable reviews

# Minimum reviews before any status change. Prevents single-video over-reactions.
CHANNEL_MIN_REVIEWS = 4

CHANNEL_URL_RE = re.compile(r'(youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))')
VIDEO_ID_RE = re.compile(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})')

DEFAULT_QUERIES = [
    'ai trading bot',
    'crypto trading bot strategy',
    'algorithmic trading python crypto',
    'claude trading bot',
    'llm trading agent',
]

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    for d in (CONFIG_DIR, STATE_DIR, REPORTS_DIR, RAW_RESPONSES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def trader_feedback_targets():
    targets = []
    if not PROFILES_DIR.exists():
        return targets
    for profile_dir in sorted(PROFILES_DIR.iterdir()):
        if not profile_dir.is_dir():
            continue
        if not (profile_dir / 'scripts' / 'market_scanner.py').exists():
            continue
        targets.append(profile_dir / 'data' / 'strategy_feedback.json')
    return targets


def safe_slug(text, fallback='item', max_len=60):
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return (slug[:max_len] or fallback)


def append_diagnostic(diag, kind, **fields):
    if diag is None:
        return
    diag.setdefault('events', []).append({
        'time': now_iso(),
        'kind': kind,
        **fields,
    })


def persist_raw_model_response(stage, title, raw):
    if not raw:
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = RAW_RESPONSES_DIR / f'{stamp}-{stage}-{safe_slug(title, "untitled")}.txt'
    path.write_text(raw, encoding='utf-8')
    return str(path)


def preferred_python():
    candidates = [
        HERMES_HOME / 'hermes-agent' / 'venv' / 'bin' / 'python3',
        HERMES_HOME / 'hermes-agent' / '.venv' / 'bin' / 'python3',
        Path.home() / 'hermes-agent' / 'venv' / 'bin' / 'python3',
        Path.home() / '.venv' / 'bin' / 'python3',
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which('python3') or sys.executable


def _http_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def parse_env(path: Path):
    data = {}
    if not path.exists():
        return data
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def pipeline_self_check():
    env = parse_env(HERMES_HOME / '.env')
    state_writable = os.access(STATE_DIR, os.W_OK)
    reports_writable = os.access(REPORTS_DIR, os.W_OK)
    transcript_api_ok = True
    transcript_api_error = None
    try:
        import youtube_transcript_api  # noqa: F401
    except Exception as e:
        transcript_api_ok = False
        transcript_api_error = str(e)

    socks_proxy = (env.get('YT_SOCKS5_PROXY') or os.environ.get('YT_SOCKS5_PROXY', '')).strip()
    return {
        'checked_at': now_iso(),
        'python': {
            'current': sys.executable,
            'preferred': preferred_python(),
        },
        'auth': {
            'copilot_token_available': bool(_resolve_copilot_token()),
        },
        'dependencies': {
            'youtube_transcript_api': transcript_api_ok,
            'youtube_transcript_api_error': transcript_api_error,
        },
        'paths': {
            'data_root': str(ROOT),
            'workspace_root': str(ROOT),  # Backward-compatible status key.
            'state_dir': str(STATE_DIR),
            'reports_dir': str(REPORTS_DIR),
            'state_writable': state_writable,
            'reports_writable': reports_writable,
        },
        'network': {
            'yt_socks5_proxy': socks_proxy or None,
            'transcript_transport': 'proxy' if socks_proxy else 'direct',
        },
    }


def parse_json_response(text):
    """Safely extract a JSON object from an LLM response.

    Tries three strategies in order: direct parse, fenced code block, bare object
    scan. Returns None if all fail — callers must handle None gracefully.
    """
    if not text:
        return None
    # 1. Direct parse (model returned only JSON)
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # 2. Fenced code block
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3. Bare object — greedy innermost brace match
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


def http_json(url, headers=None, method='GET', body=None):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if body is not None:
        body = json.dumps(body).encode('utf-8')
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, data=body, timeout=45, context=_http_ssl_context()) as resp:
            return json.loads(resp.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        snippet = raw[:500].replace('\n', ' ').strip()
        raise RuntimeError(f'HTTP {e.code}: {snippet or e.reason}') from e


def _resolve_copilot_token():
    """Resolve a GitHub token for the Copilot API.

    Search order (matches Copilot CLI behaviour):
      1. COPILOT_GITHUB_TOKEN env var
      2. GH_TOKEN env var
      3. GITHUB_TOKEN env var
      4. gh auth token CLI fallback
    """
    env_files = [parse_env(HERMES_HOME / '.env')]
    if PROFILES_DIR.exists():
        env_files.extend(parse_env(path) for path in sorted(PROFILES_DIR.glob('*/.env')))
    for env_var in ('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'):
        val = os.environ.get(env_var, '').strip()
        if val and not val.startswith('ghp_'):  # classic PATs not supported
            return val
        for file_env in env_files:
            val = file_env.get(env_var, '').strip()
            if val and not val.startswith('ghp_'):
                return val
    # Fall back to gh CLI
    import shutil, subprocess
    gh = shutil.which('gh')
    if gh:
        try:
            result = subprocess.run([gh, 'auth', 'token'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                token = result.stdout.strip()
                if not token.startswith('ghp_'):
                    return token
        except Exception:
            pass
    return None


_COPILOT_API_URL = 'https://api.githubcopilot.com/chat/completions'
_COPILOT_HEADERS = {
    'Editor-Version': 'vscode/1.104.1',
    'User-Agent': 'HermesAgent/1.0',
    'Copilot-Integration-Id': 'vscode-chat',
    'Openai-Intent': 'conversation-edits',
    'x-initiator': 'agent',
}


def _copilot_uses_max_completion_tokens(model):
    normalized = (model or '').strip().lower()
    return normalized.startswith('gpt-5')


def _build_copilot_payload(prompt, model, temperature, max_tokens):
    payload = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': temperature,
    }
    token_field = 'max_completion_tokens' if _copilot_uses_max_completion_tokens(model) else 'max_tokens'
    payload[token_field] = max_tokens
    return payload


def call_model_api(prompt, model='gpt-4.1', temperature=0.2, max_tokens=2000):
    """Call GitHub Copilot API and return a structured result dict."""
    token = _resolve_copilot_token()
    if not token:
        error = 'No GitHub token found for Copilot API (set COPILOT_GITHUB_TOKEN, GH_TOKEN or GITHUB_TOKEN)'
        print(error, file=sys.stderr)
        return {'ok': False, 'content': None, 'error': error, 'model': model}

    payload = _build_copilot_payload(prompt, model, temperature, max_tokens)

    headers = dict(_COPILOT_HEADERS)
    headers['Authorization'] = f'Bearer {token}'

    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            data = http_json(
                _COPILOT_API_URL,
                headers=headers,
                method='POST',
                body=payload,
            )
            if 'choices' in data and data['choices']:
                return {
                    'ok': True,
                    'content': data['choices'][0].get('message', {}).get('content', ''),
                    'error': None,
                    'model': model,
                }
            return {'ok': False, 'content': None, 'error': 'Copilot API returned no choices', 'model': model}
        except Exception as e:
            error = f'Copilot API error: {e}'
            if attempt < attempts and 'timed out' in str(e).lower():
                time.sleep(2)
                continue
            print(error, file=sys.stderr)
            return {'ok': False, 'content': None, 'error': error, 'model': model}


_JSON_REPAIR_PROMPT = """\
You repair malformed model output into strict valid JSON.

TARGET_SCHEMA:
{schema}

RAW_MODEL_OUTPUT:
{raw_output}

Rules:
- Respond with JSON only.
- Preserve explicit facts from the raw output.
- Do not invent missing fields. Use null, [] or conservative defaults that fit the schema.
"""


def coerce_json_response(raw, schema, repair_model='gpt-4.1', max_tokens=1200):
    result = parse_json_response(raw)
    if result:
        return result, False, None
    if not raw or not str(raw).strip():
        return None, False, 'empty_response'

    repair_prompt = _JSON_REPAIR_PROMPT.format(
        schema=schema.strip(),
        raw_output=str(raw).strip()[:16000],
    )
    repair = call_model_api(repair_prompt, model=repair_model, temperature=0, max_tokens=max_tokens)
    repaired_raw = repair.get('content')
    repaired_result = parse_json_response(repaired_raw)
    if repair.get('ok') and repaired_result:
        return repaired_result, True, None
    return None, False, repair.get('error') or 'json_repair_failed'


# ---------------------------------------------------------------------------
# STAGE 1: Structured Extraction
# ---------------------------------------------------------------------------
# WHY THE OLD STAGE 1 IS BROKEN:
#   compress_transcript_stage1 processed only text[:8000], discarding the end of
#   the transcript. In YouTube videos, the end often contains the most valuable
#   content: backtest results, actual entry/exit conditions, caveats, disclaimers,
#   and conclusions. Keyword-ranked sentence selection filtered by tone, not information.
#
# NEW APPROACH:
#   Extract a structured schema from the FULL transcript (up to MAX_TRANSCRIPT_CHARS,
#   with head+tail split to preserve both introduction AND conclusions).
#   The model is instructed to EXTRACT ONLY — no opinions, no scoring, no inference.
#   This gives Stage 2 a clean, structured object to reason about adversarially.
# ---------------------------------------------------------------------------

_STAGE1_PROMPT = """\
You are a structured information extractor for trading strategy videos.
Your task: extract information from the transcript below into a strict JSON schema.
Do NOT summarise. Do NOT score. Do NOT add opinions. ONLY extract what is explicitly stated.

TITLE: {title}

TRANSCRIPT:
{transcript}

Output ONLY valid JSON with this exact schema:
{{
  "strategy_description": "<one paragraph describing the trading approach, or null if none>",
  "entry_logic": "<explicit conditions for entering a trade. Must be specific (e.g. 'RSI crosses above 30 AND price above 200 EMA'). null if not stated>",
  "exit_logic": "<explicit conditions for exiting a trade. null if not stated>",
  "assets_mentioned": ["<assets mentioned IN A TRADING CONTEXT — omit casual references like 'I own some BTC'>"],
  "assumptions": ["<assumptions the strategy relies on — e.g. 'trending market', 'low slippage', 'liquid pairs only'>"],
  "evidence": ["<specific verifiable claims: Sharpe ratio, max drawdown %, win rate %, backtest period, dataset name>"],
  "missing_info": ["<important information NOT provided — e.g. 'no backtest shown', 'no risk parameters', 'no live results'>"],
  "promotional_signals": ["<promotional phrases, upsells, affiliate mentions: 'use my referral', 'join my Discord', 'buy my course'>"]
}}

Rules:
- Use null for missing string fields; [] for missing array fields. Never invent.
- entry_logic and exit_logic must be precise enough that a programmer could code them, or null.
- evidence must be numeric or data-backed claims only, never opinions.
"""

_STAGE1_SCHEMA = """\
{
  "strategy_description": "<one paragraph describing the trading approach, or null if none>",
  "entry_logic": "<explicit conditions for entering a trade. Must be specific. null if not stated>",
  "exit_logic": "<explicit conditions for exiting a trade. null if not stated>",
  "assets_mentioned": ["<assets mentioned in a trading context>"],
  "assumptions": ["<assumptions the strategy relies on>"],
  "evidence": ["<specific verifiable claims>"],
  "missing_info": ["<important information not provided>"],
  "promotional_signals": ["<promotional phrases, upsells, affiliate mentions>"]
}
"""


def extract_structured_stage1(text, title, diagnostics=None, source_url=None):
    """Stage 1: Extract structured information from the full transcript.

    If the transcript exceeds MAX_TRANSCRIPT_CHARS, we take the first 60% and
    last 40%. This preserves the introduction (strategy setup) AND the end
    (where conclusions, backtesting results, and caveats usually appear).

    On LLM failure, returns a minimal stub that allows Stage 2 to still run
    and classify the video as 'not_actionable' cleanly rather than crashing.
    """
    if len(text) > MAX_TRANSCRIPT_CHARS:
        head_len = int(MAX_TRANSCRIPT_CHARS * 0.60)
        tail_len = int(MAX_TRANSCRIPT_CHARS * 0.40)
        transcript = text[:head_len] + '\n[...middle section omitted for length...]\n' + text[-tail_len:]
    else:
        transcript = text

    prompt = _STAGE1_PROMPT.format(title=title, transcript=transcript)
    response = call_model_api(prompt, model='gpt-4.1', temperature=0.1, max_tokens=1500)
    raw = response.get('content')
    result, repaired, repair_error = coerce_json_response(raw, _STAGE1_SCHEMA, repair_model='gpt-4.1', max_tokens=1500)

    if not response.get('ok') or not result:
        raw_path = persist_raw_model_response('stage1', title, raw)
        append_diagnostic(
            diagnostics,
            'stage1_failure',
            title=title,
            url=source_url,
            model=response.get('model'),
            error=response.get('error') or repair_error or 'Stage 1 extraction returned non-JSON output',
            raw_response_path=raw_path,
        )
        append_diagnostic(
            diagnostics,
            'llm_parse_failed',
            stage='stage1',
            title=title,
            url=source_url,
            model=response.get('model'),
            error=repair_error or response.get('error') or 'non_json_output',
        )
        return {
            'strategy_description': None,
            'entry_logic': None,
            'exit_logic': None,
            'assets_mentioned': [],
            'assumptions': [],
            'evidence': [],
            'missing_info': [response.get('error') or 'Stage 1 extraction failed — LLM did not return structured output'],
            'promotional_signals': [],
            '_stage1_error': response.get('error') or repair_error or 'llm_parse_failed',
        }

    if repaired:
        append_diagnostic(
            diagnostics,
            'llm_parse_repaired',
            stage='stage1',
            title=title,
            url=source_url,
            model='gpt-4.1',
        )

    # Ensure all expected keys are present (model may omit optional keys)
    defaults = {
        'strategy_description': None,
        'entry_logic': None,
        'exit_logic': None,
        'assets_mentioned': [],
        'assumptions': [],
        'evidence': [],
        'missing_info': [],
        'promotional_signals': [],
    }
    for k, v in defaults.items():
        result.setdefault(k, v)
    return result


# ---------------------------------------------------------------------------
# STAGE 2: Adversarial Evaluation
# ---------------------------------------------------------------------------
# WHY THE OLD STAGE 2 IS BROKEN:
#   evaluate_bullshit_stage2 prompted the model to produce a "bullshit_score"
#   (0-100) and a "real_value_score". Problems:
#   (a) A scam video and a research video can score identically on tone.
#   (b) A single scalar hides which specific failure mode is present.
#   (c) The model was scoring style, not substance or falsifiability.
#   (d) It evaluated compressed content, compounding Stage 1 information loss.
#
# NEW APPROACH:
#   Give the model the STRUCTURED EXTRACTION from Stage 1 and ask it to find
#   flaws — adversarially. This exploits the model's reasoning differently:
#   "find what's wrong" surfaces overfitting, survivorship bias, look-ahead,
#   and missing validation in a way that "rate its quality" does not.
#
#   The output is structured weaknesses + a 3-way verdict, not a score.
# ---------------------------------------------------------------------------

_STAGE2_PROMPT = """\
You are an adversarial quant research reviewer. Your job is to find flaws, not to score.
You receive a structured extraction of a trading video. Identify weaknesses and failure modes.

TITLE: {title}

EXTRACTED STRUCTURE:
{extraction}

Apply adversarial analysis. Output ONLY valid JSON:
{{
  "weaknesses": ["<specific concrete weakness — e.g. 'claims 85% win rate with no dataset, timeframe, or asset specified'>"],
  "overfitting_flags": ["<signs of curve-fitting, look-ahead bias, survivorship bias, tiny sample sizes>"],
  "validation_gaps": ["<explicitly missing validation: no OOS test, no live trading results, no risk metrics>"],
  "hype_ratio": <0.0-1.0, fraction of content that is promotional or hype vs substantive>,
  "falsifiability": "<'falsifiable' | 'partially_testable' | 'untestable'>",
  "adversarial_verdict": "<'reject' | 'scrutinise' | 'worth_exploring'>",
  "rejection_reason": "<required if verdict is 'reject', describing why. null otherwise>"
}}

Verdict definitions:
- 'reject': content is purely promotional OR claims are logically incoherent OR no trading substance detected.
- 'scrutinise': has some substance but significant validation/evidence gaps.
- 'worth_exploring': has specific entry/exit claims, some evidence, and testable logic.

Rules:
- Be specific. "No backtest shown" is not specific enough.
  "Claims 3x annual return with no backtest period, asset, or risk metric" is specific.
- hype_ratio: 1.0 = entirely promotional; 0.0 = zero promotional content.
- 'falsifiable': entry+exit logic is precise enough to backtest today without guesswork.
"""

_STAGE2_SCHEMA = """\
{
  "weaknesses": ["<specific weaknesses in the claims or setup>"],
  "overfitting_flags": ["<specific overfitting or survivorship bias flags>"],
  "validation_gaps": ["<specific missing validation steps or evidence>"],
  "hype_ratio": "<number from 0.0 to 1.0, or null if unclear>",
  "falsifiability": "<falsifiable | partially_testable | untestable>",
  "adversarial_verdict": "<reject | scrutinise | worth_exploring>",
  "rejection_reason": "<string or null>"
}
"""


def evaluate_adversarial_stage2(title, extraction, diagnostics=None, source_url=None):
    """Stage 2: Adversarial evaluation of the Stage 1 structured extraction.

    Works on the structured dict (not raw transcript), so it reasons about
    what was and wasn't extracted — missing_info fields are particularly useful.

    On failure, returns a 'scrutinise' stub (not 'reject') to avoid false blacklisting.
    """
    prompt = _STAGE2_PROMPT.format(
        title=title,
        extraction=json.dumps(extraction, ensure_ascii=False, indent=2)
    )
    response = call_model_api(prompt, model='gpt-5.4', temperature=0.2, max_tokens=1200)
    raw = response.get('content')
    result, repaired, repair_error = coerce_json_response(raw, _STAGE2_SCHEMA, repair_model='gpt-4.1', max_tokens=1200)

    if not response.get('ok') or not result:
        raw_path = persist_raw_model_response('stage2', title, raw)
        append_diagnostic(
            diagnostics,
            'stage2_failure',
            title=title,
            url=source_url,
            model=response.get('model'),
            error=response.get('error') or repair_error or 'Stage 2 adversarial evaluation returned non-JSON output',
            raw_response_path=raw_path,
        )
        append_diagnostic(
            diagnostics,
            'llm_parse_failed',
            stage='stage2',
            title=title,
            url=source_url,
            model=response.get('model'),
            error=repair_error or response.get('error') or 'non_json_output',
        )
        return {
            'weaknesses': [response.get('error') or 'Stage 2 adversarial evaluation failed — LLM did not return structured output'],
            'overfitting_flags': [],
            'validation_gaps': [],
            'hype_ratio': None,
            'falsifiability': 'untestable',
            'adversarial_verdict': 'scrutinise',
            'rejection_reason': None,
            '_stage2_error': response.get('error') or repair_error or 'llm_parse_failed',
        }

    if repaired:
        append_diagnostic(
            diagnostics,
            'llm_parse_repaired',
            stage='stage2',
            title=title,
            url=source_url,
            model='gpt-4.1',
        )

    defaults = {
        'weaknesses': [],
        'overfitting_flags': [],
        'validation_gaps': [],
        'hype_ratio': None,
        'falsifiability': 'untestable',
        'adversarial_verdict': 'scrutinise',
        'rejection_reason': None,
    }
    for k, v in defaults.items():
        result.setdefault(k, v)
    return result


# ---------------------------------------------------------------------------
# STAGE 3 helpers: logic validation, evidence quality, completeness
# ---------------------------------------------------------------------------
# These run on Stage 1 output BEFORE tier classification.
# They catch false positives that survive LLM extraction intact.
# ---------------------------------------------------------------------------

# Patterns that look like logic but are not codable.
# A string matching ANY of these is considered vague.
# Ordered from most to least specific to avoid partial-match false negatives.
_VAGUE_LOGIC_PATTERNS = re.compile(
    r'\b('
    r'when (the )?(market|trend|price action|conditions?|sentiment|momentum|market conditions?) (turn|look|become|get|feel|is|are)\b'
    r'|when (it\'?s? )?(bullish|bearish|good|right|time)\b'
    r'|use (ai|machine learning|ml|deep learning|neural|signals?)\b'
    r'|when (the )?(signal|indicator) (says?|shows?|confirm|confirms?)\b'
    r'|based on (my |our )?(analysis|research|algorithm|model)\b'
    r'|when (the )?(trend|momentum) is (strong|weak|up|down|good|bad)\b'
    r'|at (the )?(right|correct|optimal|perfect) (time|moment|entry)\b'
    r'|when everything (lines up|aligns|looks good)\b'
    r'|enter (on|when|if) (dips?|pullbacks?|breakouts?)\b'  # too vague without numeric condition
    r')',
    re.IGNORECASE
)

# Patterns that indicate codable specificity.
# Logic must match at least ONE of: numeric threshold, named indicator with comparator,
# or explicit logical operator combining two conditions.
_CODABLE_INDICATORS = re.compile(
    r'('
    r'\d+(\.\d+)?'                              # any number (threshold, period, %)
    r'|(?:rsi|ema|sma|macd|atr|vwap|bb|stoch|adx|cci|obv|volume|close|open|high|low)'
    r'\s*(?:above|below|crosses?|>|<|>=|<=|==)\s*(?:\d|[a-z])'  # indicator comparator
    r'|(?:above|below|crosses?|over|under)\s+(?:the\s+)?(?:\d|\w+\s+(?:ema|sma|vwap|band))'
    r'|(?:and|or)\s+(?:rsi|ema|sma|price|volume|atr|close)'    # compound condition
    r'|(?:timeframe|tf|period|candle|bar|hours?|minutes?|daily|weekly|[1-9]\d*[hmd])\b'
    r')',
    re.IGNORECASE
)


def is_codable_logic(text):
    """Return True only if logic string contains specific, programmatically expressible conditions.

    Default: False. Ambiguity is treated as non-codable.
    Rationale: "enter when bullish" and "enter when RSI(14) < 30 AND price > 200 EMA"
    both produce entry_logic != null in Stage 1. Only the second is testable.
    """
    if not text or len(text.strip()) < 10:
        return False
    # If vague language detected AND no codable specifics → not codable.
    # We check vague first because a string can have both ("enter on dip below 200 EMA"):
    # in that case the numeric/indicator pattern overrides the vague match.
    has_vague = bool(_VAGUE_LOGIC_PATTERNS.search(text))
    has_specific = bool(_CODABLE_INDICATORS.search(text))
    if has_vague and not has_specific:
        return False
    # Must have at least one codable signal, even without vague language.
    return has_specific


# Patterns that indicate a real numeric metric in an evidence string.
_EVIDENCE_NUMBER = re.compile(r'\d+(\.\d+)?')
_EVIDENCE_TIMEFRAME = re.compile(
    r'\b(\d{4}|\d+\s*(?:days?|weeks?|months?|years?)|[1-9]\d*[hmd]|daily|weekly|monthly|'
    r'jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|'
    r'sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b',
    re.IGNORECASE
)
_EVIDENCE_ASSET = re.compile(
    r'\b(btc|eth|sol|bnb|xrp|ada|bitcoin|ethereum|solana|s&p|spy|nasdaq|qqq|forex|'
    r'crypto|stock|equity|futures?|[A-Z]{3,5}/[A-Z]{3,5})\b',
    re.IGNORECASE
)
_EVIDENCE_METRIC = re.compile(
    r'\b(sharpe|sortino|calmar|drawdown|win ?rate|profit factor|cagr|return|accuracy|'
    r'backtest|out.of.sample|walk.forward|live|paper trading|r:r|risk.reward|'
    r'max loss|annuali[sz]ed|expectancy)\b',
    re.IGNORECASE
)
# Strings that look numeric but are not evidence ("100x", "1000%", "guaranteed")
_EVIDENCE_HYPE = re.compile(
    r'\b(100x|1000x|guaranteed|never loses?|always wins?|very profitable|huge returns?|'
    r'insane|life.changing|easy money|passive income)\b',
    re.IGNORECASE
)


def validate_evidence(evidence_list):
    """Score each evidence string for concreteness. Return counts of strong vs weak.

    Strong evidence: has a number + at least one of (timeframe, asset, metric type)
    AND does not match hype patterns.
    Weak evidence: has a number but is missing context, or matches hype patterns.
    Anything else is not counted.

    Rationale: "85% winrate" alone is weak (no asset, no timeframe). 
    "85% winrate on BTC/USDT 1h, Jan 2022–Jan 2024" is strong.
    """
    strong = 0
    weak = 0
    for item in (evidence_list or []):
        if not item:
            continue
        if _EVIDENCE_HYPE.search(item):
            # Hype string — don't count even if it has a number
            weak += 1
            continue
        has_number = bool(_EVIDENCE_NUMBER.search(item))
        if not has_number:
            continue  # No number → not evidence
        qualifiers = sum([
            bool(_EVIDENCE_TIMEFRAME.search(item)),
            bool(_EVIDENCE_ASSET.search(item)),
            bool(_EVIDENCE_METRIC.search(item)),
        ])
        if qualifiers >= 2:
            strong += 1
        else:
            weak += 1
    return {'valid_evidence_count': strong, 'weak_evidence_count': weak}


_TIMEFRAME_HINT = re.compile(
    r'\b(\d+[hmd]|hourly|daily|weekly|monthly|[1-9]\d*\s*(?:min|hour|day|week|month)'
    r'|[1-9]\d*H|[1-9]\d*D|timeframe|tf|period)\b',
    re.IGNORECASE
)
_RISK_MGMT_HINT = re.compile(
    r'\b(stop.?loss|sl|take.?profit|tp|position.?siz|risk.?reward|r:r|max.?drawdown|'
    r'atr.?stop|trailing|kelly|fixed.?risk|percent.?risk|\d+\s*%\s*risk)\b',
    re.IGNORECASE
)

# Threshold: a strategy needs >= this completeness to reach potential_strategy tier.
COMPLETENESS_THRESHOLD = 3  # out of 5 dimensions


def compute_completeness(extraction, ev_result):
    """Score how testable a strategy is across 5 binary dimensions.

    Dimensions (each worth 1 point):
      1. codable_entry  — entry_logic passes is_codable_logic()
      2. codable_exit   — exit_logic passes is_codable_logic()
      3. asset_defined  — at least one asset extracted in trading context
      4. timeframe      — timeframe mentioned in entry/exit/strategy text
      5. risk_mgmt      — stop-loss, position sizing, or risk parameter mentioned

    Returns score (int 0-5) and a per-dimension breakdown dict.
    Rationale: a strategy missing 3 of 5 dimensions is not testable regardless
    of how well-written the description is.
    """
    entry = extraction.get('entry_logic') or ''
    exit_ = extraction.get('exit_logic') or ''
    strategy = extraction.get('strategy_description') or ''
    combined = f'{entry} {exit_} {strategy}'

    d_entry   = is_codable_logic(entry)
    d_exit    = is_codable_logic(exit_)
    d_asset   = len(extraction.get('assets_mentioned') or []) > 0
    d_tf      = bool(_TIMEFRAME_HINT.search(combined))
    d_risk    = bool(_RISK_MGMT_HINT.search(combined))

    score = sum([d_entry, d_exit, d_asset, d_tf, d_risk])
    return score, {
        'codable_entry': d_entry,
        'codable_exit': d_exit,
        'asset_defined': d_asset,
        'timeframe_mentioned': d_tf,
        'risk_mgmt_mentioned': d_risk,
    }


# Low/medium/high logic complexity — weak edge indicators vs compound filters.
_LOGIC_COMPOUND = re.compile(
    r'\b(and|or)\b.{3,80}\b(rsi|ema|sma|macd|atr|vwap|adx|volume|close|price|regime)\b',
    re.IGNORECASE
)
_LOGIC_REGIME = re.compile(
    r'\b(regime|trend filter|market state|above.{1,20}200|below.{1,20}200|'
    r'bull market|bear market|ranging|trending)\b',
    re.IGNORECASE
)


def score_logic_complexity(entry_logic, exit_logic):
    """Classify entry+exit logic as low/medium/high complexity.

    high: compound conditions (AND/OR across multiple indicators) AND/OR regime filter
    medium: single named indicator with numeric threshold
    low: everything else (including None)

    Used to slightly nudge confidence — complex logic is harder to overfit to
    YouTube hype but doesn't guarantee edge. Does NOT change tier by itself.
    """
    combined = f'{entry_logic or ""} {exit_logic or ""}'.strip()
    if not combined:
        return 'low'
    has_compound = bool(_LOGIC_COMPOUND.search(combined))
    has_regime = bool(_LOGIC_REGIME.search(combined))
    if has_compound or has_regime:
        return 'high' if (has_compound and has_regime) else 'medium'
    if is_codable_logic(combined):
        return 'medium'
    return 'low'


# ---------------------------------------------------------------------------
# STAGE 3: Signal Qualification (deterministic — no LLM)
# ---------------------------------------------------------------------------
# Changes vs previous version:
#   - entry_logic / exit_logic are run through is_codable_logic(); vague strings
#     are treated as None. This prevents "buy when bullish" from raising tier.
#   - evidence quality is validated; only strong evidence counts toward tier.
#   - completeness score gates potential_strategy (minimum COMPLETENESS_THRESHOLD).
#   - Stage 2 penalties integrated explicitly:
#       * hype_ratio > 0.7 → force not_actionable (was 0.8)
#       * falsifiability == 'untestable' → blocks testable tiers
#       * len(validation_gaps) > 2 → downgrade by one tier
# ---------------------------------------------------------------------------

def qualify_signal(extraction, adversarial):
    """Stage 3: Classify signal tier. Strict by default — ambiguity → lower tier.

    Returns a dict consumed by build_hypothesis() and update_channel_state().
    """
    verdict = adversarial.get('adversarial_verdict', 'scrutinise')
    hype_ratio = adversarial.get('hype_ratio') or 0.0
    falsifiability = adversarial.get('falsifiability', 'untestable')
    validation_gaps = adversarial.get('validation_gaps') or []

    # --- logic codability check (fix #1) ---
    # null-out logic strings that are not actually codable so downstream
    # flags reflect real testability, not presence of any text.
    raw_entry = extraction.get('entry_logic')
    raw_exit  = extraction.get('exit_logic')
    codable_entry = raw_entry if is_codable_logic(raw_entry) else None
    codable_exit  = raw_exit  if is_codable_logic(raw_exit)  else None

    # --- evidence validation (fix #2) ---
    ev = validate_evidence(extraction.get('evidence'))
    has_strong_evidence = ev['valid_evidence_count'] > 0

    # --- completeness (fix #3) — uses codable variants ---
    effective_extraction = dict(extraction)
    effective_extraction['entry_logic'] = codable_entry
    effective_extraction['exit_logic']  = codable_exit
    completeness_score, completeness_dims = compute_completeness(effective_extraction, ev)

    has_strategy  = bool(extraction.get('strategy_description'))
    has_entry_exit = bool(codable_entry) and bool(codable_exit)
    is_rejected    = verdict == 'reject'

    # --- Stage 2 penalties (fix #4) ---
    # Threshold lowered from 0.8 to 0.7 — content that is >70% promotional should
    # never produce a tradeable signal.
    is_hype_dominated = hype_ratio > 0.7
    # 'untestable' falsifiability blocks all testable tiers regardless of other signals.
    is_untestable = falsifiability == 'untestable'
    # >2 validation gaps indicates the strategy cannot be evaluated without major assumptions.
    high_gap_count = len(validation_gaps) > 2

    reasons = []

    # Hard stops — nothing recovers from these
    if is_rejected or is_hype_dominated:
        tier = 'not_actionable'
        reasons.append(f'hard-stop: adversarial_verdict={verdict}, hype_ratio={hype_ratio:.2f}')

    else:
        # --- potential_strategy: strict gate, but gap-count penalty is a downgrade
        #     not a hard block here — see downgrade step below (fix #4 intent: one step down).
        #     Fix #7 is enforced BY the downgrade: high gaps prevent landing at
        #     potential_strategy because potential_strategy → testable_hypothesis.
        if (
            has_entry_exit
            and has_strong_evidence
            and completeness_score >= COMPLETENESS_THRESHOLD
            and not is_untestable
        ):
            tier = 'potential_strategy'
            reasons.append(
                f'codable entry+exit, strong evidence={ev["valid_evidence_count"]}, '
                f'completeness={completeness_score}/5'
            )

        # --- testable_hypothesis: falsifiable claim, not untestable ---
        elif falsifiability == 'falsifiable' and has_strategy:
            tier = 'testable_hypothesis'
            reasons.append('falsifiable strategy with strategy description')

        elif has_strategy:
            tier = 'idea_only'
            reasons.append(
                f'strategy concept only; codable={has_entry_exit}, '
                f'strong_evidence={has_strong_evidence}, completeness={completeness_score}/5'
            )

        else:
            tier = 'not_actionable'
            reasons.append('no describable strategy found')

        # --- tier downgrade for high validation gaps (fix #4) ---
        # Applied AFTER classification as a single downgrade step.
        # idea_only is NOT downgraded — it already signals limited substance,
        # and further downgrading would discard borderline useful content.
        if high_gap_count and tier in ('potential_strategy', 'testable_hypothesis'):
            downgrade_map = {
                'potential_strategy': 'testable_hypothesis',
                'testable_hypothesis': 'idea_only',
            }
            old_tier = tier
            tier = downgrade_map[tier]
            reasons.append(f'downgraded {old_tier}→{tier}: {len(validation_gaps)} validation gaps')

    logic_complexity = score_logic_complexity(codable_entry, codable_exit)

    return {
        'tier': tier,
        'adversarial_verdict': verdict,
        'reasoning': '; '.join(reasons),
        'has_entry_exit': has_entry_exit,
        'codable_entry': codable_entry,
        'codable_exit': codable_exit,
        'has_strong_evidence': has_strong_evidence,
        'evidence_quality': ev,
        'completeness_score': completeness_score,
        'completeness_dims': completeness_dims,
        'logic_complexity': logic_complexity,
        'hype_ratio': hype_ratio,
        'falsifiability': falsifiability,
        'high_gap_count': high_gap_count,
    }


# ---------------------------------------------------------------------------
# STAGE 4: Hypothesis Output
# ---------------------------------------------------------------------------
# This output is for RESEARCH BIAS, not trade execution.
# Hypotheses are NOT trading signals. The scanner must validate independently.
#
# Schema contract (stable, flat, predictable):
#   identity:  hypothesis_id, url, channel
#   research:  title, description, entry_logic, exit_logic, assets, timeframe,
#              confidence, tier, validation_gaps, weaknesses, overfitting_flags,
#              hype_ratio, falsifiability, completeness, logic_quality
#   optional:  assumptions, evidence, evidence_quality, completeness_dims
# ---------------------------------------------------------------------------

# Common asset name aliases → canonical uppercase ticker
_ASSET_ALIASES = {
    'bitcoin': 'BTC', 'ethereum': 'ETH', 'solana': 'SOL',
    'binance coin': 'BNB', 'ripple': 'XRP', 'cardano': 'ADA',
    's&p': 'S&P500', 's&p 500': 'S&P500', 's&p500': 'S&P500',
    'nasdaq': 'NASDAQ', 'gold': 'XAU', 'silver': 'XAG',
    'immunity bio': 'IBRX', 'immunitybio': 'IBRX',
    'taiwan semiconductor': 'TSM', 'taiwan semiconductor (tsmc)': 'TSM',
    'service now': 'NOW', 'space mobile': 'ASTS',
}

_QUOTE_ONLY_ASSETS = {
    'USD', 'USDT', 'USDC', 'BUSD', 'FDUSD', 'DAI', 'TUSD', 'EUR', 'GBP', 'JPY',
}

_CRYPTO_ASSETS = {
    'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'AVAX', 'LINK', 'DOT', 'POL',
    'DOGE', 'MATIC', 'ARB', 'OP', 'LTC', 'TRX', 'ATOM', 'NEAR', 'APT', 'SUI',
}

_EQUITY_INDEX_ASSETS = {
    'STOCKS', 'EQUITIES', 'NASDAQ', 'S&P500', 'SPX', 'DOW', 'QQQ', 'SPY',
}

_METAL_ASSETS = {'XAU', 'XAG'}


def _classify_market_scope(assets):
    normalized = _normalize_assets(assets)
    if not normalized:
        return 'unknown'
    scopes = set()
    for asset in normalized:
        compact = asset.replace(' ', '').upper()
        if compact in _QUOTE_ONLY_ASSETS:
            scopes.add('quote_only')
        elif compact in _CRYPTO_ASSETS or '/' in compact or any(
            compact.endswith(quote) and len(compact) > len(quote)
            for quote in _QUOTE_ONLY_ASSETS
        ):
            scopes.add('crypto')
        elif compact in _EQUITY_INDEX_ASSETS:
            scopes.add('equity')
        elif compact in _METAL_ASSETS:
            scopes.add('macro')
        elif compact.isalpha() and 1 <= len(compact) <= 5:
            scopes.add('equity')
        else:
            scopes.add('unknown')
    if len(scopes) == 1:
        return next(iter(scopes))
    if 'crypto' in scopes:
        return 'crypto'
    if 'equity' in scopes:
        return 'equity'
    if 'macro' in scopes:
        return 'macro'
    if 'quote_only' in scopes:
        return 'quote_only'
    return 'mixed'


def _select_primary_asset(assets):
    normalized = _normalize_assets(assets)
    if not normalized:
        return None
    for asset in normalized:
        compact = asset.replace(' ', '').upper()
        if compact in _CRYPTO_ASSETS or '/' in compact or any(
            compact.endswith(quote) and len(compact) > len(quote)
            for quote in _QUOTE_ONLY_ASSETS
        ):
            return asset
    for asset in normalized:
        if asset in _QUOTE_ONLY_ASSETS:
            continue
        return asset
    return None


def _normalize_assets(assets):
    """Normalize asset names to consistent uppercase tickers. Deduplicates."""
    out = []
    seen = set()
    for a in (assets or []):
        if not a:
            continue
        normalized = _ASSET_ALIASES.get(a.strip().lower(), a.strip()).upper()
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _extract_timeframe_hint(entry_logic, exit_logic, strategy_description):
    """Extract the first timeframe token from logic/strategy strings, or None."""
    combined = f'{entry_logic or ""} {exit_logic or ""} {strategy_description or ""}'
    m = _TIMEFRAME_HINT.search(combined)
    return m.group(0).strip() if m else None


def build_hypothesis(title, url, channel, extraction, adversarial, signal):
    """Stage 4: Build a research hypothesis record. Confidence is pessimistic by default.

    REGRESSION GUARD: output of this function is a RESEARCH ARTIFACT only.
    It is consumed by market_scanner.py's normalize_research_feedback() adapter.
    The scanner validates all signals independently before any order is considered.
    Modifying this schema requires a matching update in _normalize_hypothesis().

    Output contract:
    - This is a RESEARCH RECORD, not a trading signal.
    - entry_logic / exit_logic are codable strings or None (never vague text).
    - confidence is always float in [0.05, 0.95].
    - tier is always one of: potential_strategy | testable_hypothesis | idea_only | not_actionable.
    - All list fields default to [] (never None).
    - 'description' is the canonical field name (mirrors 'strategy_description').

    Hard caps on confidence:
    - no strong evidence → max 0.55
    - non-codable entry/exit → max 0.50
    """
    validation_gaps = adversarial.get('validation_gaps') or []
    weaknesses      = adversarial.get('weaknesses') or []
    gap_count  = len(validation_gaps) + len(weaknesses)
    hype       = adversarial.get('hype_ratio') or 0.0
    ev         = signal.get('evidence_quality', {})
    strong_ev  = ev.get('valid_evidence_count', 0)
    logic_quality = signal.get('logic_complexity', 'low')

    confidence = 0.30  # pessimistic base — content must earn confidence

    # Additive bonuses (only for genuinely strong signals)
    if strong_ev >= 2:
        confidence += 0.20
    elif strong_ev == 1:
        confidence += 0.10
    if signal.get('has_entry_exit'):
        confidence += 0.15
    if signal.get('falsifiability') == 'falsifiable':
        confidence += 0.10
    completeness = signal.get('completeness_score', 0)
    if completeness >= 4:
        confidence += 0.10
    elif completeness >= COMPLETENESS_THRESHOLD:
        confidence += 0.05

    # Logic quality nudge — does NOT gate tier, only nudges confidence slightly
    if logic_quality == 'high':
        confidence += 0.05
    elif logic_quality == 'low':
        confidence -= 0.05

    # Penalties
    confidence -= gap_count * 0.08
    confidence -= hype * 0.40

    # Hard caps
    if strong_ev == 0:
        confidence = min(confidence, 0.55)
    if not signal.get('has_entry_exit'):
        confidence = min(confidence, 0.50)

    confidence = round(max(0.05, min(0.95, confidence)), 2)

    entry_logic = signal.get('codable_entry')
    exit_logic  = signal.get('codable_exit')

    assets = _normalize_assets(extraction.get('assets_mentioned', []))
    primary_asset = _select_primary_asset(assets)
    market_scope = _classify_market_scope(assets)

    return {
        # --- identity ---
        'hypothesis_id': f'{url}::v1',
        'url': url,
        'channel': channel,
        # --- research schema (stable, flat, predictable) ---
        # This output is for research bias, not trade execution.
        # Hypotheses are NOT signals. Scanner must validate independently.
        'title': title,
        'description': extraction.get('strategy_description'),
        'entry_logic': entry_logic,
        'exit_logic': exit_logic,
        'assets': assets,
        'primary_asset': primary_asset,
        'market_scope': market_scope,
        'timeframe': _extract_timeframe_hint(entry_logic, exit_logic, extraction.get('strategy_description')),
        'confidence': confidence,
        'tier': signal['tier'],
        'validation_gaps': validation_gaps,
        'weaknesses': weaknesses,
        'overfitting_flags': adversarial.get('overfitting_flags', []),
        'hype_ratio': round(hype, 3),
        'falsifiability': signal.get('falsifiability', 'untestable'),
        'completeness': completeness,
        'logic_quality': logic_quality,
        # --- optional context (not used for execution decisions) ---
        'assumptions': extraction.get('assumptions', []),
        'evidence': extraction.get('evidence', []),
        'evidence_quality': ev,
        'completeness_dims': signal.get('completeness_dims', {}),
    }


def is_substantive_learning_candidate(extraction, adversarial, signal):
    score = 0
    if extraction.get('strategy_description'):
        score += 2
    if extraction.get('assets_mentioned'):
        score += 1
    if extraction.get('evidence'):
        score += 1
    if extraction.get('assumptions'):
        score += 1
    if signal.get('tier') == 'idea_only':
        score += 1
    if adversarial.get('hype_ratio') is not None and adversarial.get('hype_ratio', 1.0) <= 0.75:
        score += 1
    return score >= 3


def build_research_learning(title, url, channel, extraction, adversarial, signal):
    assets = _normalize_assets(extraction.get('assets_mentioned', []))
    primary_asset = _select_primary_asset(assets)
    market_scope = _classify_market_scope(assets)
    learning_type = 'bot_or_strategy_learning'
    if extraction.get('evidence'):
        learning_type = 'backtest_or_validation_learning'
    elif extraction.get('promotional_signals'):
        learning_type = 'mixed_signal_learning'
    if market_scope not in {'crypto', 'unknown'}:
        learning_type = 'cross_market_learning'

    applicability = []
    if extraction.get('assumptions'):
        applicability.append('assumptions_to_test')
    if adversarial.get('validation_gaps'):
        applicability.append('validation_gap_followup')
    if assets:
        applicability.append('asset_specific')
    if market_scope == 'crypto':
        applicability.append('crypto_scanner_candidate')
    elif market_scope == 'equity':
        applicability.append('equity_scope_learning')
    elif market_scope == 'quote_only':
        applicability.append('asset_mapping_needed')

    return {
        'learning_id': f'{url}::learning::v1',
        'url': url,
        'channel': channel,
        'title': title,
        'learning_type': learning_type,
        'summary': extraction.get('strategy_description') or 'Substantive trading-research content without fully codable rules.',
        'assets': assets,
        'primary_asset': primary_asset,
        'market_scope': market_scope,
        'evidence': extraction.get('evidence', []),
        'assumptions': extraction.get('assumptions', []),
        'missing_info': extraction.get('missing_info', []),
        'validation_gaps': adversarial.get('validation_gaps', []),
        'weaknesses': adversarial.get('weaknesses', []),
        'hype_ratio': round(adversarial.get('hype_ratio') or 0.0, 3),
        'source_tier': signal.get('tier', 'idea_only'),
        'applicability': applicability,
    }


def build_experiments(hypotheses, research_learnings):
    experiments = []
    seen = set()

    for h in hypotheses:
        key = ('hypothesis', h.get('title'), tuple(h.get('assets', [])))
        if key in seen:
            continue
        seen.add(key)
        primary_asset = h.get('primary_asset') or _select_primary_asset(h.get('assets', []))
        market_scope = h.get('market_scope') or _classify_market_scope(h.get('assets', []))
        experiments.append({
            'experiment_id': f"{h['hypothesis_id']}::papertrade",
            'type': 'papertrade_validation' if market_scope == 'crypto' and primary_asset else 'scope_translation',
            'asset': primary_asset,
            'market_scope': market_scope,
            'asset_candidates': _normalize_assets(h.get('assets', [])),
            'title': h.get('title'),
            'goal': (
                'Validate whether the extracted strategy adds edge in paper trading before any config change.'
                if market_scope == 'crypto' and primary_asset
                else 'Translate this non-crypto or under-specified idea into a supported test plan before paper trading.'
            ),
            'guardrail': (
                'No auto-config changes without empirical confirmation and risk guardrail review.'
                if market_scope == 'crypto' and primary_asset
                else 'Do not open scanner-driven paper trades until scope, asset mapping and guardrails are explicit.'
            ),
        })

    for l in research_learnings:
        key = ('learning', l.get('title'), tuple(l.get('assets', [])))
        if key in seen:
            continue
        seen.add(key)
        primary_asset = l.get('primary_asset') or _select_primary_asset(l.get('assets', []))
        market_scope = l.get('market_scope') or _classify_market_scope(l.get('assets', []))
        exp_type = 'research_translation'
        goal = 'Translate the learning into a testable scanner/backtest/papertrade experiment.'
        guardrail = 'Treat as research input only until observed in tracked paper trades.'
        if market_scope != 'crypto':
            exp_type = 'scope_translation'
            goal = 'Translate this learning into a market-specific test plan before scanner or papertrade execution.'
            guardrail = 'Do not force this learning into the crypto scanner without a matching market adapter.'
        elif not primary_asset:
            exp_type = 'asset_translation'
            goal = 'Translate this learning into a concrete tradable asset before scanner/backtest/papertrade execution.'
            guardrail = 'Do not open scanner-driven paper trades until a concrete base asset is defined.'
        experiments.append({
            'experiment_id': f"{l['learning_id']}::translation",
            'type': exp_type,
            'asset': primary_asset,
            'market_scope': market_scope,
            'asset_candidates': _normalize_assets(l.get('assets', [])),
            'title': l.get('title'),
            'goal': goal,
            'guardrail': guardrail,
        })

    return experiments[:8]


def build_strategy_feedback(hypotheses, research_learnings):
    """Assemble the research feedback document from pipeline hypotheses.

    Output intent:
    - This document is for research bias, not trade execution.
    - Hypotheses are NOT signals. Scanner must validate independently.
    - Priority assets reflect contextual frequency across high-confidence
      hypotheses only — not keyword counting.

    Output contract (stable, no optional chaos):
      generated_at    — ISO timestamp of this run
      summary         — aggregate counts and average confidence
      priority_assets — top assets by hypothesis coverage (confidence >= 0.5)
      hypotheses      — list of normalized hypothesis records
    """
    if not hypotheses and not research_learnings:
        return {
            'generated_at': now_iso(),
            'summary': {
                'total': 0,
                'potential_strategy': 0,
                'testable_hypothesis': 0,
                'research_learnings': 0,
                'avg_confidence': 0.0,
            },
            'priority_assets': [],
            'hypotheses': [],
            'research_learnings': [],
            'experiments': [],
            'schema_version': FEEDBACK_SCHEMA_VERSION,
        }

    # Priority assets: LLM-extracted contextual frequency, not keyword counting.
    # Only high-confidence hypotheses contribute to avoid noise.
    asset_mentions: dict = {}
    for collection in (hypotheses, research_learnings):
        for item in collection:
            confidence = item.get('confidence', 0.5)
            if confidence < 0.4:
                continue
            preferred = item.get('primary_asset')
            assets = [preferred] if preferred else _normalize_assets(item.get('assets', []))
            for asset in assets:
                if not asset or asset in _QUOTE_ONLY_ASSETS:
                    continue
                asset_mentions[asset] = asset_mentions.get(asset, 0) + 1

    priority_assets = sorted(asset_mentions, key=lambda a: -asset_mentions[a])[:5]

    tier_counts = {
        'potential_strategy': sum(1 for h in hypotheses if h['tier'] == 'potential_strategy'),
        'testable_hypothesis': sum(1 for h in hypotheses if h['tier'] == 'testable_hypothesis'),
    }

    return {
        'generated_at': now_iso(),
        'summary': {
            'total': len(hypotheses) + len(research_learnings),
            'potential_strategy': tier_counts['potential_strategy'],
            'testable_hypothesis': tier_counts['testable_hypothesis'],
            'research_learnings': len(research_learnings),
            'avg_confidence': round(
                sum(h.get('confidence', 0) for h in hypotheses) / len(hypotheses), 2
            ) if hypotheses else 0.0,
        },
        'priority_assets': priority_assets,
        'hypotheses': hypotheses,
        'research_learnings': research_learnings,
        'experiments': build_experiments(hypotheses, research_learnings),
        'schema_version': FEEDBACK_SCHEMA_VERSION,
    }


def save_strategy_feedback(feedback):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    save_json(SHARED_STRATEGY_FEEDBACK_PATH, feedback)
    for path in trader_feedback_targets():
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json(path, feedback)


# ---------------------------------------------------------------------------
# Channel Scoring — Redesigned
# ---------------------------------------------------------------------------
# WHY THE OLD SYSTEM IS BROKEN:
#   flat score (+1/-1) + static thresholds (score>=3 → favorite, 5 consecutive
#   negatives → blacklist) problems:
#   (a) No decay: 5 early positive reviews permanently protect a channel that
#       has since turned into an affiliate farm.
#   (b) The 5-in-a-row blacklist triggered only on a very specific pattern —
#       4 bad + 1 okay resets it.
#   (c) score == 3 is arbitrary and doesn't consider total review count.
#   (d) Score increments of ±1 ignore that tiers have very different signal value.
#
# NEW APPROACH:
#   Exponential Moving Average (EMA) over weighted tier scores.
#   - Each review contributes its SIGNAL_TIER_WEIGHTS value.
#   - EMA smoothing (alpha=0.35) means recent reviews dominate but old ones decay.
#   - Status changes are gated behind CHANNEL_MIN_REVIEWS to avoid over-reacting.
#   - A blacklisted channel can recover if new reviews push EMA above the threshold.
#   - Favorite thresholds are higher (require sustained quality signal).
# ---------------------------------------------------------------------------

def compute_channel_ema_score(reviews):
    """Compute running EMA score from full review history.

    Processes oldest → newest. alpha=0.35 means the characteristic half-life of
    a review is about ln(0.5)/ln(1-0.35) ≈ 1.8 reviews. Scores converge toward
    the tier weight of the dominant review type.

    Returns float in approximate range [-1.0, +3.0].
    """
    ema = 0.0
    for review in reviews:
        tier = review.get('tier', 'not_actionable')
        weight = SIGNAL_TIER_WEIGHTS.get(tier, 0)
        ema = CHANNEL_EMA_ALPHA * weight + (1.0 - CHANNEL_EMA_ALPHA) * ema
    return round(ema, 4)


def update_channel_state(state, channel_key, signal_qual, source_url, title):
    """Update channel state using EMA-based scoring from signal qualification result.

    `signal_qual` is the result of qualify_signal() — dict with 'tier' and 'adversarial_verdict'.
    Returns list of human-readable change strings.
    """
    channels = state['scores'].setdefault('channels', {})
    entry = channels.setdefault(channel_key, {
        'ema_score': 0.0,
        'reviews': [],
        'status': 'neutral',
        'last_reviewed_at': None,
        'title_samples': [],
    })

    tier = signal_qual.get('tier', 'not_actionable')
    entry['reviews'].append({
        'tier': tier,
        'url': source_url,
        'title': title,
        'reviewed_at': now_iso(),
    })
    entry['reviews'] = entry['reviews'][-30:]  # Keep last 30; EMA handles decay

    if title and title not in entry.get('title_samples', []):
        entry.setdefault('title_samples', []).append(title)
        entry['title_samples'] = entry['title_samples'][-5:]

    entry['last_reviewed_at'] = now_iso()
    entry['ema_score'] = compute_channel_ema_score(entry['reviews'])

    review_count = len(entry['reviews'])
    ema = entry['ema_score']
    favorites = set(state['favorites'].get('channels', []))
    blacklist = set(state['blacklist'].get('channels', []))
    changes = []

    # Gate: don't change status until we have enough reviews to trust the EMA
    if review_count < CHANNEL_MIN_REVIEWS:
        entry['status'] = 'neutral'
        state['favorites']['channels'] = sorted(favorites)
        state['blacklist']['channels'] = sorted(blacklist)
        return changes

    prev_status = entry.get('status', 'neutral')

    if ema <= CHANNEL_BLACKLIST_EMA:
        # EMA deeply negative — channel consistently produces not_actionable content
        entry['status'] = 'blacklisted'
        if channel_key not in blacklist:
            blacklist.add(channel_key)
            changes.append(f'Blacklisted channel (EMA={ema:.3f}): {channel_key}')
        favorites.discard(channel_key)

    elif ema >= CHANNEL_FAVORITE_EMA:
        # EMA strongly positive — channel consistently produces testable/strategy content
        entry['status'] = 'favorite'
        if channel_key not in favorites:
            favorites.add(channel_key)
            changes.append(f'Favorited channel (EMA={ema:.3f}): {channel_key}')
        blacklist.discard(channel_key)

    else:
        # Neutral zone — remove from lists and note recovery if applicable
        if channel_key in blacklist:
            blacklist.discard(channel_key)
            if prev_status == 'blacklisted':
                changes.append(f'Removed from blacklist (EMA recovered to {ema:.3f}): {channel_key}')
        if channel_key in favorites:
            favorites.discard(channel_key)
            if prev_status == 'favorite':
                changes.append(f'Removed from favorites (EMA dropped to {ema:.3f}): {channel_key}')
        entry['status'] = 'neutral'

    state['favorites']['channels'] = sorted(favorites)
    state['blacklist']['channels'] = sorted(blacklist)
    return changes


def search_youtube(query, limit=10):
    """Discover YouTube videos without a third-party search API.

    Durable curation should come from channel RSS/watchlists; topic search is a
    best-effort discovery connector.
    """
    import urllib.request

    search_url = 'https://www.youtube.com/results?' + urlencode({'search_query': query})
    req = urllib.request.Request(search_url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Hermes Source Intelligence',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=_http_ssl_context()) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception:
        return []

    results = []
    seen = set()
    for video_id in re.findall(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', html):
        if video_id in seen:
            continue
        seen.add(video_id)
        results.append({
            'title': '',
            'url': f'https://www.youtube.com/watch?v={video_id}',
            'content': '',
            'channel_hint': '',
        })
        if len(results) >= limit:
            break
    return results


def extract_video_id(url):
    parsed = urlparse(url)
    if parsed.netloc.endswith('youtu.be'):
        vid = parsed.path.strip('/').split('/')[0]
        return vid if len(vid) == 11 else None
    qs = parse_qs(parsed.query)
    if 'v' in qs:
        vid = qs['v'][0]
        return vid if len(vid) == 11 else None
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def canonical_video_url(url):
    vid = extract_video_id(url)
    return f'https://www.youtube.com/watch?v={vid}' if vid else url


def looks_like_video_url(url):
    return '/watch' in url or 'youtu.be/' in url or '/shorts/' in url


def fetch_video_metadata(url):
    if not looks_like_video_url(url):
        return {}
    try:
        params = urlencode({'url': canonical_video_url(url), 'format': 'json'})
        data = http_json(f'https://www.youtube.com/oembed?{params}', headers={'User-Agent': 'Mozilla/5.0'})
        return {
            'title': data.get('title'),
            'author_name': data.get('author_name'),
            'author_url': data.get('author_url'),
        }
    except Exception:
        return {}


def normalize_channel(url):
    m = CHANNEL_URL_RE.search(url)
    if not m:
        return url
    return 'https://' + m.group(1)


def slugify_channel_name(name):
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return slug[:40] or 'unknown-channel'


def _build_transcript_apis():
    from youtube_transcript_api import YouTubeTranscriptApi
    apis = []
    env = parse_env(HERMES_HOME / '.env')
    socks_proxy = (env.get('YT_SOCKS5_PROXY') or os.environ.get('YT_SOCKS5_PROXY', '')).strip()

    if socks_proxy:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            proxy_config = GenericProxyConfig(http_url=socks_proxy, https_url=socks_proxy)
            apis.append(('proxy', YouTubeTranscriptApi(proxy_config=proxy_config), socks_proxy))
        except Exception:
            pass

    apis.append(('direct', YouTubeTranscriptApi(), None))
    return apis


def get_transcript(video_id):
    errors = []
    for transport, api, proxy in _build_transcript_apis():
        try:
            transcript_list = api.list(video_id)
        except Exception as e:
            errors.append(f'{transport}: {e}')
            continue
        transcript = None
        for finder in (
            lambda tl: tl.find_manually_created_transcript(['en', 'de', 'en-US', 'zh-Hans', 'zh-Hant']),
            lambda tl: tl.find_generated_transcript(['en', 'de', 'en-US', 'zh-Hans', 'zh-Hant']),
        ):
            try:
                transcript = finder(transcript_list)
                if transcript:
                    break
            except Exception:
                pass
        if not transcript:
            try:
                transcript = next(iter(transcript_list))
            except Exception as e:
                errors.append(f'{transport}: {e}')
                continue
        try:
            fetched = transcript.fetch()
            text = ' '.join((getattr(snippet, 'text', '') or '') for snippet in fetched)
            lang = getattr(transcript, 'language_code', None)
            transcript_dir = ROOT / 'transcripts'
            transcript_dir.mkdir(exist_ok=True, parents=True)
            transcript_file = transcript_dir / f'{video_id}.txt'
            try:
                with open(transcript_file, 'w', encoding='utf-8') as f:
                    f.write(text)
            except Exception:
                pass
            return text, lang, None
        except Exception as e:
            detail = f'{transport}: {e}'
            if proxy:
                detail += f' [proxy={proxy}]'
            errors.append(detail)
    return None, None, ' | '.join(errors) if errors else 'Transcript fetch failed without detailed error'


def send_transcript_via_telegram(video_id: str, title: str = '') -> None:
    """Send saved transcript to Telegram as a document file.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the Hermes home .env.
    Silently skips if credentials are missing or the transcript file is absent.
    Uses stdlib urllib only — no extra dependencies.
    """
    env = parse_env(HERMES_HOME / '.env')
    bot_token = (env.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN', '')).strip()
    chat_id = (env.get('TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID', '')).strip()
    if not bot_token or not chat_id:
        return

    transcript_file = ROOT / 'transcripts' / f'{video_id}.txt'
    if not transcript_file.exists():
        return

    caption = f'Transcript: {title or video_id}'
    file_bytes = transcript_file.read_bytes()
    boundary = 'HermesTranscriptBoundary'

    parts = []
    for name, value in [('chat_id', chat_id), ('caption', caption)]:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            .encode('utf-8')
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{video_id}.txt"\r\n'
        f'Content-Type: text/plain; charset=utf-8\r\n\r\n'
        .encode('utf-8') + file_bytes + b'\r\n'
    )
    parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(parts)

    import urllib.request as _ureq
    api_url = f'https://api.telegram.org/bot{bot_token}/sendDocument'
    req = _ureq.Request(api_url, data=body, method='POST')
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    try:
        with _ureq.urlopen(req, timeout=30, context=_http_ssl_context()):
            pass
    except Exception as e:
        print(f'[WARN] Telegram transcript send failed for {video_id}: {e}', file=sys.stderr)


def infer_channel(url, title='', metadata=None):
    metadata = metadata or {}
    if metadata.get('author_url'):
        return normalize_channel(metadata['author_url'])
    if metadata.get('author_name'):
        return 'https://www.youtube.com/@' + slugify_channel_name(metadata['author_name'])
    parsed = urlparse(url)
    if '/@' in parsed.path or '/channel/' in parsed.path or '/c/' in parsed.path or '/user/' in parsed.path:
        return normalize_channel(url)
    host = f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme and parsed.netloc else 'https://www.youtube.com'
    slug = title.strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')[:40] or 'unknown-channel'
    return host + '/@' + slug


def _legacy_verdict_to_tier(verdict):
    return {
        'positive': 'testable_hypothesis',
        'neutral': 'idea_only',
        'negative': 'not_actionable',
    }.get((verdict or '').lower(), None)


def migrate_state(state):
    changed = False

    state['scores'].setdefault('schema_version', STATE_SCHEMA_VERSION)
    state['manual'].setdefault('run_full_pipeline', False)
    state['manual'].setdefault('video_urls', [])
    state['manual'].setdefault('channel_urls', [])
    state['manual'].setdefault('notes', 'Add direct YouTube video or channel URLs here for the next manual or scheduled run.')
    state['manual'].setdefault('failed_urls', {})

    channels = state['scores'].setdefault('channels', {})
    for channel_key, entry in channels.items():
        reviews = entry.get('reviews', [])
        migrated_reviews = []
        needs_recompute = 'ema_score' not in entry
        for review in reviews:
            if 'tier' not in review:
                tier = _legacy_verdict_to_tier(review.get('verdict')) or 'idea_only'
                review = dict(review)
                review['tier'] = tier
                needs_recompute = True
                changed = True
            migrated_reviews.append(review)
        entry['reviews'] = migrated_reviews[-30:]
        if needs_recompute:
            entry['ema_score'] = compute_channel_ema_score(entry['reviews'])
            entry.pop('score', None)
            changed = True

    videos = state['seen'].setdefault('videos', {})
    for vid, meta in videos.items():
        if 'signal_tier' not in meta and 'verdict' in meta:
            tier = _legacy_verdict_to_tier(meta.get('verdict'))
            if tier:
                meta['signal_tier'] = tier
                changed = True

    if state['scores'].get('schema_version') != STATE_SCHEMA_VERSION:
        state['scores']['schema_version'] = STATE_SCHEMA_VERSION
        changed = True

    return changed


def persist_state_files(state):
    save_json(CHANNEL_SCORES_PATH, state['scores'])
    save_json(FAVORITES_PATH, state['favorites'])
    save_json(BLACKLIST_PATH, state['blacklist'])
    save_json(MANUAL_INPUTS_PATH, state['manual'])
    save_json(SEEN_VIDEOS_PATH, state['seen'])
    save_json(RUN_STATE_PATH, state['run_state'])


def load_state():
    config = load_json(CONFIG_PATH, None)
    if not isinstance(config, dict):
        config = load_json(LEGACY_CONFIG_PATH, {})
    state = {
        'config': config,
        # scoring_rules removed — EMA parameters are module-level constants (CHANNEL_EMA_ALPHA etc.)
        'scores': load_json(CHANNEL_SCORES_PATH, {'channels': {}}),
        'favorites': load_json(FAVORITES_PATH, {'channels': []}),
        'blacklist': load_json(BLACKLIST_PATH, {'channels': []}),
        'manual': load_json(MANUAL_INPUTS_PATH, {'run_full_pipeline': False, 'video_urls': [], 'channel_urls': [], 'failed_urls': {}}),
        'seen': load_json(SEEN_VIDEOS_PATH, {'videos': {}}),
        'run_state': load_json(RUN_STATE_PATH, {'last_query_index': -1}),
    }
    migrate_state(state)
    return state


def queue_manual_input(kind, url, run_full_pipeline=False, force=False):
    ensure_dirs()
    state = load_state()
    normalized = canonical_video_url(url) if kind == 'video' else normalize_channel(url)
    queue_key = 'video_urls' if kind == 'video' else 'channel_urls'
    queue = state['manual'].setdefault(queue_key, [])
    failed = state['manual'].setdefault('failed_urls', {})

    status = 'queued'
    if normalized in queue:
        status = 'already_queued'
    else:
        seen_videos = state['seen'].get('videos', {})
        if kind == 'video' and not force:
            vid = extract_video_id(normalized)
            if vid and vid in seen_videos:
                status = 'already_seen'
            else:
                queue.append(normalized)
        else:
            queue.append(normalized)
    if run_full_pipeline:
        state['manual']['run_full_pipeline'] = True
    failed.pop(normalized, None)
    persist_state_files(state)
    return {
        'status': status,
        'kind': kind,
        'url': normalized,
        'queue_length': len(queue),
    }


def _mark_manual_failure(state, source, url, error):
    failed = state['manual'].setdefault('failed_urls', {})
    entry = failed.setdefault(url, {'attempts': 0, 'source': source})
    entry['attempts'] = int(entry.get('attempts', 0)) + 1
    entry['last_error'] = error
    entry['last_attempted_at'] = now_iso()


def clear_consumed_manual_inputs(state, consumed):
    state['manual']['run_full_pipeline'] = False
    state['manual']['video_urls'] = [u for u in state['manual'].get('video_urls', []) if u not in consumed.get('video_urls', set())]
    state['manual']['channel_urls'] = [u for u in state['manual'].get('channel_urls', []) if u not in consumed.get('channel_urls', set())]
    failed = state['manual'].setdefault('failed_urls', {})
    for urls in consumed.values():
        for url in urls:
            failed.pop(url, None)


def candidate_urls(state):
    manual = state['manual']
    urls = []
    for u in manual.get('video_urls', []):
        urls.append({'url': u, 'source': 'manual_video'})
    for u in manual.get('channel_urls', []):
        urls.append({'url': u, 'source': 'manual_channel'})
    # Topic discovery uses direct YouTube search, not Tavily or another
    # third-party search API. Channel RSS/watchlists can be added as another
    # source provider without changing the downstream evaluator.
    queries = state['config'].get('queries', []) or DEFAULT_QUERIES
    idx = (state['run_state'].get('last_query_index', -1) + 1) % len(queries)
    query = queries[idx]
    state['run_state']['last_query_index'] = idx
    for item in search_youtube(query, limit=10):
        urls.append({
            'url': item['url'],
            'title': item.get('title', ''),
            'snippet': item.get('snippet', item.get('content', '')),
            'source': f'search:{query}',
        })
    return urls


def resolve_candidate(url, title='', source=''):
    metadata = fetch_video_metadata(url)
    resolved_title = metadata.get('title') or title or 'Untitled YouTube Source'
    channel = normalize_channel(url) if 'manual_channel' in source else infer_channel(url, resolved_title, metadata)
    return metadata, resolved_title, channel


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def report_markdown(hypotheses, research_learnings, changes, strategy_feedback):
    ts = now_iso()
    lines = [
        '# AI Trading YouTube Pipeline Report (4-Stage)',
        '',
        f'- Run timestamp: {ts}',
        '',
    ]

    potential = [h for h in hypotheses if h['tier'] == 'potential_strategy']
    testable = [h for h in hypotheses if h['tier'] == 'testable_hypothesis']

    if potential:
        lines.append('## Potential Strategies')
        for h in potential:
            lines += [
                f"### {h['title']}",
                f"- **Tier**: {h['tier']} | confidence: {h['confidence']}",
                f"- **Strategy**: {h.get('description') or 'n/a'}",
                f"- **Entry**: {h.get('entry_logic') or 'n/a'}",
                f"- **Exit**: {h.get('exit_logic') or 'n/a'}",
                f"- **Evidence**: {', '.join(h.get('evidence', [])) or 'none cited'}",
                f"- **Weaknesses**: {'; '.join(h.get('weaknesses', [])) or 'none identified'}",
                f"- **Assets**: {', '.join(h.get('assets', [])) or 'none'}",
                f"- **Completeness**: {h.get('completeness', 0)}/5 | logic_quality: {h.get('logic_quality', 'n/a')}",
                f"- Source: {h['url']}",
                '',
            ]
    else:
        lines += ['## Potential Strategies', '- None found this run.', '']

    if testable:
        lines.append('## Testable Hypotheses')
        for h in testable:
            lines += [
                f"### {h['title']}",
                f"- **Tier**: {h['tier']} | confidence: {h['confidence']}",
                f"- **Strategy**: {h.get('description') or 'n/a'}",
                f"- **Falsifiability**: {h.get('falsifiability', 'n/a')}",
                f"- **Validation gaps**: {'; '.join(h.get('validation_gaps', [])) or 'none identified'}",
                f"- **Assets**: {', '.join(h.get('assets', [])) or 'none'}",
                f"- Source: {h['url']}",
                '',
            ]
    else:
        lines += ['## Testable Hypotheses', '- None found this run.', '']

    if research_learnings:
        lines.append('## Research Learnings')
        for learning in research_learnings[:8]:
            lines += [
                f"### {learning['title']}",
                f"- **Type**: {learning.get('learning_type', 'research_learning')}",
                f"- **Summary**: {learning.get('summary') or 'n/a'}",
                f"- **Assets**: {', '.join(learning.get('assets', [])) or 'none'}",
                f"- **Validation gaps**: {'; '.join(learning.get('validation_gaps', [])) or 'none identified'}",
                f"- Source: {learning['url']}",
                '',
            ]
    else:
        lines += ['## Research Learnings', '- None found this run.', '']

    lines.append('## Channel Status Changes')
    if changes:
        for c in changes:
            lines.append(f'- {c}')
    else:
        lines.append('- No changes.')
    lines.append('')

    summary = strategy_feedback.get('summary', {})
    lines += [
        '## Pipeline Summary',
        f"- Total hypotheses produced: {summary.get('total', 0)}",
        f"  - potential_strategy: {summary.get('potential_strategy', 0)}",
        f"  - testable_hypothesis: {summary.get('testable_hypothesis', 0)}",
        f"  - research_learnings: {summary.get('research_learnings', 0)}",
        f"  - avg_confidence: {summary.get('avg_confidence', 0)}",
        f"- Priority assets (contextual): {', '.join(strategy_feedback.get('priority_assets', [])) or 'none'}",
        '',
        '## Suggested Experiments',
    ]
    experiments = strategy_feedback.get('experiments', [])
    if experiments:
        for exp in experiments[:6]:
            lines.append(f"- {exp.get('type')}: {exp.get('title')} | asset={exp.get('asset') or 'n/a'}")
    else:
        lines.append('- None.')
    lines += [
        '',
        '## Pipeline Stages',
        '- Stage 1: Structured Extraction (GPT-4.1, full transcript)',
        '- Stage 2: Adversarial Evaluation (GPT-5.4)',
        '- Stage 3: Signal Qualification (deterministic)',
        '- Stage 4: Hypothesis Output (deterministic)',
        '',
    ]
    return '\n'.join(lines)


def save_report(content):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = REPORTS_DIR / f'report-{stamp}.md'
    path.write_text(content)
    return path

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline():
    # This pipeline can run autonomously from configured discovery queries, and
    # can also process manually queued videos/channels.
    ensure_dirs()
    state = load_state()
    hypotheses = []
    research_learnings = []
    changes = []
    diagnostics = {
        'started_at': now_iso(),
        'self_check': pipeline_self_check(),
        'events': [],
    }
    favorites = set(state['favorites'].get('channels', []))
    blacklist = set(state['blacklist'].get('channels', []))
    seen = state['seen'].setdefault('videos', {})
    consumed_manual = {'video_urls': set(), 'channel_urls': set()}

    raw_candidates = candidate_urls(state)
    prioritized = []
    deferred = []
    for c in raw_candidates:
        url = c['url']
        metadata, resolved_title, channel_key = resolve_candidate(
            url, c.get('title', ''), c.get('source', '')
        )
        c['metadata'] = metadata
        c['title'] = resolved_title
        c['channel'] = channel_key
        # Skip blacklisted channels only for automated search results.
        # Manual inputs always proceed regardless of blacklist status.
        if channel_key in blacklist and c.get('source', '').startswith('search:'):
            continue
        # Prioritise known-good channels so their videos are processed first.
        target = prioritized if channel_key in favorites else deferred
        target.append(c)

    candidates = prioritized + deferred

    # Safety fallback: if no usable candidates found, log and exit cleanly.
    # The pipeline NEVER switches to manual mode or asks for user input.
    if not candidates:
        warning = 'No usable video candidates found for this run. Check Tavily API key or network connectivity.'
        print(f'[WARN] {warning}', file=sys.stderr)
        append_diagnostic(diagnostics, 'pipeline_warning', error=warning)
        save_json(RUN_DIAGNOSTICS_PATH, diagnostics)
        save_json(RUN_STATE_PATH, state['run_state'])
        return

    processed = 0
    for c in candidates:
        if processed >= int(state['config'].get('max_videos_per_run', 5)):
            break
        url = c['url']
        title = c.get('title') or 'Untitled YouTube Source'
        metadata = c.get('metadata') or {}
        channel = c.get('channel') or infer_channel(url, title, metadata)
        source = c.get('source', '')
        is_manual_video = source == 'manual_video'
        is_manual_channel = source == 'manual_channel'

        if looks_like_video_url(url):
            vid = extract_video_id(url)
            if not vid:
                append_diagnostic(diagnostics, 'candidate_skipped', title=title, url=url, error='Could not extract video id')
                if is_manual_video:
                    _mark_manual_failure(state, source, url, 'Could not extract video id')
                continue
            if vid in seen:
                prior = seen.get(vid, {})
                if prior.get('analysis_complete', True) or not is_manual_video:
                    if is_manual_video:
                        consumed_manual['video_urls'].add(url)
                    append_diagnostic(diagnostics, 'candidate_deduped', title=title, url=url, video_id=vid)
                    continue
                append_diagnostic(diagnostics, 'candidate_retrying_incomplete', title=title, url=url, video_id=vid)
            import time as _time
            _time.sleep(3)  # Rate-limit: avoid YouTube IP blocks between requests
            text, lang, err = get_transcript(vid)
            if err or not text:
                seen[vid] = {
                    'url': url, 'title': title, 'fetched_at': now_iso(),
                    'has_transcript': False, 'error': err,
                    'analysis_complete': False,
                }
                append_diagnostic(diagnostics, 'transcript_failure', title=title, url=url, video_id=vid, error=err)
                if is_manual_video:
                    _mark_manual_failure(state, source, url, err or 'No transcript text returned')
                continue

            send_transcript_via_telegram(vid, title)

            # STAGE 1: Structured Extraction (full transcript, head+tail split)
            extraction = extract_structured_stage1(text, title, diagnostics=diagnostics, source_url=url)

            # STAGE 2: Adversarial Evaluation (finds flaws, not quality score)
            adversarial = evaluate_adversarial_stage2(title, extraction, diagnostics=diagnostics, source_url=url)

            stage_failed = bool(extraction.get('_stage1_error') or adversarial.get('_stage2_error'))

            # STAGE 3: Signal Qualification (deterministic — no LLM call)
            signal = qualify_signal(extraction, adversarial)

            seen[vid] = {
                'url': url,
                'title': title,
                'channel': channel,
                'author_name': metadata.get('author_name'),
                'fetched_at': now_iso(),
                'has_transcript': True,
                'language': lang,
                'analysis_complete': not stage_failed,
                'signal_tier': signal['tier'],
                'stage1_extraction': extraction,
                'stage2_adversarial': adversarial,
                'stage3_signal': signal,
            }
            processed += 1
            if stage_failed:
                append_diagnostic(diagnostics, 'analysis_incomplete', title=title, url=url, video_id=vid)
                if is_manual_video:
                    _mark_manual_failure(state, source, url, extraction.get('_stage1_error') or adversarial.get('_stage2_error'))
                continue

            # Update channel EMA score based on signal tier only after successful analysis.
            changes.extend(update_channel_state(state, channel, signal, url, title))

            if is_manual_video:
                consumed_manual['video_urls'].add(url)

            # STAGE 4: Build hypothesis for actionable tiers only.
            # idea_only and not_actionable produce no hypothesis record.
            if signal['tier'] in ('testable_hypothesis', 'potential_strategy'):
                hypothesis = build_hypothesis(
                    title, url, channel, extraction, adversarial, signal
                )
                hypotheses.append(hypothesis)
            elif is_substantive_learning_candidate(extraction, adversarial, signal):
                research_learnings.append(
                    build_research_learning(title, url, channel, extraction, adversarial, signal)
                )

        else:
            # Channel URL — update channel entry with neutral signal (idea_only).
            # Not enough information to evaluate without a specific video transcript.
            neutral_signal = {
                'tier': 'idea_only',
                'adversarial_verdict': 'scrutinise',
                'hype_ratio': 0.0,
                'falsifiability': 'untestable',
                'has_entry_exit': False,
                'has_evidence': False,
            }
            changes.extend(update_channel_state(state, channel, neutral_signal, url, title))
            processed += 1
            if is_manual_channel:
                consumed_manual['channel_urls'].add(url)

    clear_consumed_manual_inputs(state, consumed_manual)
    strategy_feedback = build_strategy_feedback(hypotheses, research_learnings)
    report = report_markdown(hypotheses, research_learnings, list(dict.fromkeys(changes)), strategy_feedback)
    report_path = save_report(report)
    strategy_feedback['source_report'] = str(report_path)
    diagnostics['completed_at'] = now_iso()
    diagnostics['summary'] = {
        'processed_candidates': processed,
        'hypotheses': len(hypotheses),
        'research_learnings': len(research_learnings),
    }

    persist_state_files(state)
    save_strategy_feedback(strategy_feedback)
    save_json(RUN_DIAGNOSTICS_PATH, diagnostics)

    print(report)
    print(f'\nReport saved to: {report_path}')


def status():
    ensure_dirs()
    state = load_state()
    strategy_feedback = load_json(SHARED_STRATEGY_FEEDBACK_PATH, {'enabled': False})
    diagnostics = load_json(RUN_DIAGNOSTICS_PATH, {})
    latest = sorted(REPORTS_DIR.glob('report-*.md'))[-1:] if REPORTS_DIR.exists() else []
    out = {
        'self_check': pipeline_self_check(),
        'favorites': state['favorites'].get('channels', []),
        'blacklist': state['blacklist'].get('channels', []),
        'tracked_channels': len(state['scores'].get('channels', {})),
        'seen_videos': len(state['seen'].get('videos', {})),
        'latest_report': str(latest[0]) if latest else None,
        'latest_diagnostics': str(RUN_DIAGNOSTICS_PATH) if RUN_DIAGNOSTICS_PATH.exists() else None,
        'last_diagnostic_summary': diagnostics.get('summary'),
        'pending_manual_inputs': state['manual'],
        'strategy_feedback_active': bool(strategy_feedback.get('hypotheses') or strategy_feedback.get('research_learnings')),
        'strategy_feedback_hypothesis_count': len(strategy_feedback.get('hypotheses', [])),
        'strategy_feedback_learning_count': len(strategy_feedback.get('research_learnings', [])),
        'strategy_feedback_experiment_count': len(strategy_feedback.get('experiments', [])),
        'strategy_feedback_priority_assets': strategy_feedback.get('priority_assets', []),
        'research_consumer_profiles': [path.parents[1].name for path in trader_feedback_targets()],
    }
    persist_state_files(state)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd in {'run', 'learn'}:
        run_pipeline()
    elif cmd == 'status':
        status()
    elif cmd == 'add-video' and len(sys.argv) >= 3:
        print(json.dumps(queue_manual_input('video', sys.argv[2], run_full_pipeline=True), ensure_ascii=False, indent=2))
    elif cmd == 'add-channel' and len(sys.argv) >= 3:
        print(json.dumps(queue_manual_input('channel', sys.argv[2], run_full_pipeline=True), ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 2 and ('youtube.com/' in sys.argv[1] or 'youtu.be/' in sys.argv[1]):
        kind = 'video' if looks_like_video_url(sys.argv[1]) else 'channel'
        print(json.dumps(queue_manual_input(kind, sys.argv[1], run_full_pipeline=True), ensure_ascii=False, indent=2))
    else:
        print('Usage: youtube_pipeline.py [run|learn|status|add-video <url>|add-channel <url>|<youtube-url>]', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
