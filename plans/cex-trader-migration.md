# CEX Trader Migration Plan

Status: implemented, pending live Telegram/Cron verification  
Owner: Hermes main agent  
Target profile: `cex-trader`

## Goals

- [x] Migrate the old `cex-trader` profile into the current Hermes setup cleanly.
- [x] Keep source discovery, transcript extraction, LLM evaluation, and trading execution concerns separated.
- [x] Preserve the daily autonomous learning loop: discover AI-trading YouTube videos, extract transcripts, evaluate learnings, and feed usable hypotheses into paper-trading logic.
- [x] Connect the CEX trading bot to Telegram so the user can chat with it in its dedicated topic.
- [x] Restrict the Hermes main agent to only the allowed Telegram topics.
- [x] Switch Hermes approval behavior to safer auto approval after confirming the current config schema.

## Telegram Routing Requirements

- [x] CEX trading bot must answer in the CEX execution topic:
  - Telegram link: `https://t.me/c/3969468087/1564/1988`
  - Hermes target: `-1003969468087:1564`
- [ ] CEX trading bot should read/use the shared research topic when configured:
  - Expected Hermes target: `-1003969468087:58`
- [x] Hermes main agent must answer only in:
  - General topic / reset context: `-1003969468087`
  - Main-agent own topic: `https://t.me/c/3969468087/57/1979`
  - Hermes target for own topic: `-1003969468087:57`
- [x] Hermes main agent must not answer in trading execution topics.
- [x] Bot tokens must stay in `.env` or secret storage only. Do not write full Telegram bot tokens into markdown, git, logs, or config comments.
- [x] The main-agent bot token provided in chat must be treated as secret material and referenced only as `TELEGRAM_BOT_TOKEN`.

## Source Intelligence Architecture

- [x] Keep `source-intelligence` as a global Hermes plugin/capability, not profile-local workspace code.
- [x] Keep persistent source data under `data/source-intelligence/`.
- [x] Keep YouTube as one source connector, not the whole product boundary.
- [ ] Use the existing Hermes `youtube-content` skill pattern for transcript fetching where useful.
- [ ] Use the existing Hermes `blogwatcher`/RSS skill pattern as a reference for future feed/channel monitoring.
- [x] Remove Tavily as a required dependency.
- [x] Preserve topic-based YouTube discovery without Tavily by using one or both:
  - [x] direct YouTube search connector for query discovery
  - [ ] channel RSS/watchlist connector for stable curated sources
- [x] Make the discovery provider configurable:
  - [x] `youtube_search`
  - [ ] `channel_watchlist`
  - [ ] future: `youtube_api`
  - [ ] future: `rss`
- [x] Configure daily discovery to process 4 videos per run by default.
- [x] Keep transcript extraction and LLM evaluation independent from trading.
- [x] Project only evaluated trading-relevant hypotheses into trading state.

## Trading Profile Migration

- [x] Use old profile source: `hermes-old-setup/.hermes/profiles/cex-trader`.
- [x] Create target profile: `profiles/cex-trader`.
- [x] Copy durable profile files:
  - [x] `SOUL.md`
  - [x] `memories/`
  - [x] `data/`
  - [x] `scripts/market_scanner.py`
  - [x] relevant wrapper scripts after path cleanup
  - [x] `channel_directory.json`
- [x] Do not copy generated/runtime artifacts:
  - [x] `__pycache__/`
  - [x] `.tick.lock`
  - [x] `.update_check`
  - [x] stale `gateway.pid`
  - [x] old logs unless archived intentionally
- [x] Build a new Linux-compatible `profiles/cex-trader/config.yaml`.
- [x] Replace old Mac paths like `/Users/damian/.hermes/...` with `/home/dashi/.hermes/...`.
- [x] Configure model/provider consistently with current profiles.
- [x] Keep CEX-specific risk state and paper-trading state profile-local.
- [x] Keep source intelligence state global.

## Cron And Automation

- [x] Migrate daily source-intelligence job:
  - [x] schedule: configurable, default 1x/day
  - [x] max videos per run: 4
  - [x] output: global source-intelligence state plus trading projection
- [x] Migrate CEX market scan job:
  - [x] schedule: configurable, old default was every 4h
  - [x] delivery: `telegram:-1003969468087:1564`
- [x] Migrate CEX daily report job:
  - [x] schedule: configurable, old default was 21:00
  - [x] delivery: `telegram:-1003969468087:1564`
- [x] Migrate CEX weekly optimization job:
  - [x] schedule: configurable, old default was Sunday 10:00
  - [x] delivery: `telegram:-1003969468087:1564`
- [x] Keep cron jobs disabled until manual smoke tests pass.
- [ ] Enable cron jobs only after Telegram delivery is verified.

## Approval Mode

- [x] Inspect current Hermes config schema for valid approval modes.
- [x] Change approval mode from `manual` to the correct safer auto-approval setting.
- [x] Confirm cron approval behavior separately if Hermes has a distinct `approvals.cron_mode`.
- [x] Verify the agent still blocks destructive or sensitive actions if required by Hermes policy.

## Verification Checklist

- [x] Markdown plan exists at `plans/cex-trader-migration.md`.
- [x] Plan does not contain full Telegram bot tokens.
- [x] `source-intelligence` status command runs.
- [x] YouTube discovery works without `TAVILY_API_KEY`.
- [ ] Transcript extraction works for a known YouTube video.
- [ ] LLM evaluation produces `strategy_feedback.json`.
- [x] CEX scanner can consume projected source-intelligence feedback.
- [x] `profiles/cex-trader` starts without path errors.
- [x] `hermes -p cex-trader gateway status` is healthy.
- [ ] Message sent in topic `-1003969468087:1564` reaches the CEX trading bot.
- [ ] Hermes main agent ignores topic `-1003969468087:1564`.
- [ ] Hermes main agent answers in `-1003969468087` general context.
- [ ] Hermes main agent answers in `-1003969468087:57`.
- [ ] Cron dry run produces expected output without Telegram delivery.
- [ ] Cron live run delivers to `telegram:-1003969468087:1564`.
- [x] Auto approval mode is active and confirmed via config/status output.
