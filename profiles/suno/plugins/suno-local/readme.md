# Suno Hermes Plugin

Hermes plugin for Suno song generation. In this repo it lives as a local directory plugin under `~/.hermes/plugins/suno`.

## Naming

- Python package: `suno_plugin`
- Hermes plugin name: `suno`
- Tool prefix: `suno_`

## What it provides

- Tools:
  - `suno_status`
  - `suno_setup`
  - `suno_generate`
  - `suno_feature_action`
- Hermes CLI subcommand:
  - `hermes suno status|setup|generate`
- Bundled plugin skill:
  - `suno:suno`

Additional behavior:

- `suno_status` reports best-effort premium detection.
- `suno_generate` supports Sounds controls (`audio_type`, `bpm`, `key_root`, `key_scale`).
- Premium feature guard: premium-only options (e.g. `inspiration_clip_id`) are blocked for non-premium/unverifiable accounts.

Feature-action coverage (single entrypoint):

- `generate_standard`, `get_full_song`, `extend`, `cover`, `mashup`, `sample`, `use_inspiration`
- `adjust_speed`, `crop`, `reuse_prompt`, `get_stems`, `remaster`, `create_voice`
- `download_mp3`, `download_wav`, `download_video`
- `set_visibility`, `publish`, `move_to_workspace`, `add_to_playlist`, `move_to_trash`

## Install

Hermes discovers this plugin automatically when it exists at `~/.hermes/plugins/suno/`.

If you want to distribute it as a pip package instead, the same code can also be exposed through the `hermes_agent.plugins` entry point.

## Configure

Use Hermes CLI setup command:

```bash
hermes suno setup \
  --email your.name@gmail.com \
  --password 'your-password' \
  --output-dir outputs/suno_plugin \
  --cdp-port 9222 \
  --poll-attempts 60 \
  --poll-interval 4.0
```

Or configure through tool calls with `suno_setup`.

## Status check

```bash
hermes suno status
```

## Generate

```bash
hermes suno generate \
  --lyrics 'Mitternacht im Neonregen' \
  --styles 'synthwave, dark pop' \
  --exclude-styles 'country' \
  --vocal-gender f \
  --lyrics-mode custom \
  --weirdness 0.62 \
  --style-influence 0.74 \
  --song-title 'Neon Drift'

# Sounds options from Create sidebar
hermes suno generate \
  --lyrics-mode auto \
  --audio-type one_shot \
  --bpm auto \
  --key-root any \
  --song-title 'One Shot Test'

# Premium: Use as Inspiration
hermes suno generate \
  --lyrics 'Remix this base idea' \
  --styles 'electronic, dark pop' \
  --inspiration-clip-id '<CLIP_ID>' \
  --inspiration-start-s 8 \
  --inspiration-end-s 24
```

## Action matrix (`suno_feature_action`)

| Action | Required parameters | Premium |
|---|---|---|
| `generate_standard` | none (defaults work) | No |
| `get_full_song` | `clip_id` | No |
| `extend` | `clip_id` | No |
| `cover` | `clip_id` | No |
| `mashup` | `clip_id` | No |
| `sample` | `clip_id` | No |
| `use_inspiration` | `inspiration_clip_id` or `clip_id` | Yes |
| `adjust_speed` | `clip_id`, `speed` | No |
| `crop` | `clip_id`, `crop_start_s`, `crop_end_s` | No |
| `reuse_prompt` | `clip_id` | No |
| `get_stems` | `clip_id` (`stems_mode` optional) | Yes |
| `remaster` | `clip_id` (`remaster_strength`/`remaster_model` optional) | No |
| `create_voice` | `clip_id`, `voice_name` | Yes |
| `download_mp3` | `clip_id` | No |
| `download_wav` | `clip_id` | Yes |
| `download_video` | `clip_id` | No |
| `list_workspaces` | none (`page` optional) | No |
| `list_songs` | none (`workspace`, `page`, `limit` optional) | No |
| `set_visibility` | `clip_id`, at least one of `allow_comments`/`allow_remixes`/`pin_to_profile` | No |
| `publish` | `clip_id` (`is_public` optional) | No |
| `move_to_workspace` | `workspace`, `clip_id` or `clip_ids` | No |
| `add_to_playlist` | `playlist_name` | No |
| `move_to_trash` | `clip_id` or `clip_ids` | No |

## Runtime verification (2026-04-18)

Post-refresh, non-destructive live checks:

- Auth/session refresh successful (`session_status=200`)
- `GET /api/project/default`: `200`
- `GET /api/project/me`: `200`
- `download_mp3` smoke with current candidate id: `404 asset_url_not_found`

Evidence report:

- `outputs/runtime_verification_20260418.json`

Current conclusion: read-path is verified live; download success still requires a clip that exposes a resolvable asset URL for this account/workspace.

## Runtime paths

- Plugin config: `~/.hermes/plugins/suno/.env`
- Browser profile: `~/.hermes/plugins/suno/profile/chrome/`
- Artifacts: `~/.hermes/plugins/suno/profile/artifacts/`

Generated auth files are reused in-place:

- `~/.hermes/plugins/suno/profile/artifacts/suno_storage_state.json`
- `~/.hermes/plugins/suno/profile/artifacts/suno_auth_bundle.json`
- `~/.hermes/plugins/suno/profile/artifacts/suno_bearer_token.txt`
- `~/.hermes/plugins/suno/profile/artifacts/suno_api_headers.json`
- `~/.hermes/plugins/suno/profile/artifacts/embedded_capture.json`

## Team distribution without GitHub

1. Build a wheel:

```bash
python -m build
```

2. Share `dist/*.whl` with your colleague (cloud folder, USB, internal share).

3. Install on colleague machine:

```bash
pip install /path/to/suno_plugin-<version>-py3-none-any.whl
```

4. Verify in Hermes:

```bash
hermes plugins list
```
