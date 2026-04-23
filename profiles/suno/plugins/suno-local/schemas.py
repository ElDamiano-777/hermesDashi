from __future__ import annotations

SUNO_STATUS = {
    "name": "suno_status",
    "description": (
        "Check whether Suno auth/session artifacts are present and whether the current API session is valid. "
        "Use this before generation when you need a quick health check. Also returns best-effort premium tier detection."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

SUNO_SETUP = {
    "name": "suno_setup",
    "description": (
        "Write or update the profile-local Suno plugin runtime configuration without interactive prompts. "
        "Use when credentials or defaults must be configured programmatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "google_email": {"type": "string", "description": "Google account email"},
            "google_password": {"type": "string", "description": "Google account password"},
            "output_dir": {"type": "string", "description": "Default output directory"},
            "cdp_port": {"type": "integer", "description": "Local CDP port for managed browser"},
            "poll_attempts": {"type": "integer", "description": "Default max poll attempts"},
            "poll_interval": {"type": "number", "description": "Default poll interval in seconds"},
        },
    },
}

SUNO_GENERATE = {
    "name": "suno_generate",
    "description": (
        "Generate one Suno song with automatic session check/login and API-first flow. "
        "Use this when the user asks to create a track from lyrics/style prompts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lyrics": {"type": "string", "description": "Lyrics text (required for custom/mumble mode)"},
            "styles": {"type": "string", "description": "Style tags, comma separated"},
            "exclude_styles": {"type": "string", "description": "Negative style tags"},
            "vocal_gender": {"type": "string", "enum": ["m", "f"], "description": "Vocal gender hint"},
            "lyrics_mode": {
                "type": "string",
                "enum": ["custom", "auto", "mumble", "instrumental"],
                "description": "Generation mode",
            },
            "weirdness": {"type": "number", "description": "Weirdness slider in range 0..1"},
            "style_influence": {"type": "number", "description": "Style influence slider in range 0..1"},
            "song_title": {"type": "string", "description": "Song title"},
            "audio_type": {
                "type": "string",
                "enum": ["one_shot", "loop"],
                "description": "Sounds panel type option",
            },
            "bpm": {
                "type": "string",
                "description": "Sounds panel BPM value ('auto' or integer as string)",
            },
            "key_root": {
                "type": "string",
                "enum": ["any", "c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"],
                "description": "Sounds panel key root",
            },
            "key_scale": {
                "type": "string",
                "enum": ["major", "minor"],
                "description": "Sounds panel key scale",
            },
            "inspiration_clip_id": {
                "type": "string",
                "description": "Clip id for 'Use as Inspiration' flow (premium)",
            },
            "inspiration_start_s": {
                "type": "number",
                "description": "Optional inspiration source start second",
            },
            "inspiration_end_s": {
                "type": "number",
                "description": "Optional inspiration source end second",
            },
            "workspace": {"type": "string", "description": "Workspace selector (id/slug/name/default)"},
            "out_dir": {"type": "string", "description": "Override output directory"},
            "poll_attempts": {"type": "integer", "description": "Override poll attempts"},
            "poll_interval": {"type": "number", "description": "Override poll interval seconds"},
            "wait_seconds": {"type": "number", "description": "Login wait timeout in seconds"},
            "force_login": {"type": "boolean", "description": "Force browser login before generation"},
            "disable_p1_fallback": {
                "type": "boolean",
                "description": "Disable browser-backed P1 refresh fallback",
            },
            "debug": {"type": "boolean", "description": "Enable verbose logs"},
        },
    },
}

SUNO_PREVIEW_STATE = {
    "name": "suno_preview_state",
    "description": (
        "Manage the pending Suno Telegram preview state without using terminal commands. "
        "Use this for preview creation, reading, edit-mode changes, applying edited text, and clearing state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "get", "set_edit_mode", "apply_edit", "clear"],
            },
            "target": {"type": "string", "description": "Telegram target id for the pending preview"},
            "artist": {"type": "string", "description": "Artist reference for preview creation"},
            "request": {"type": "string", "description": "Original beat request for preview creation"},
            "model": {"type": "string", "description": "Optional LLM model for style engineering"},
            "field": {"type": "string", "enum": ["style", "lyrics", "both"]},
            "message": {"type": "string", "description": "Edited replacement text from Telegram"},
        },
        "required": ["action", "target"],
    },
}


SUNO_FEATURE_ACTION = {
    "name": "suno_feature_action",
    "description": (
        "Execute one tested Suno frontend feature as an API-backed action. Supports generation variants, "
        "visibility/publish metadata updates, workspace/playlist/trash actions, and premium-guarded flows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "generate_standard",
                    "get_full_song",
                    "extend",
                    "cover",
                    "mashup",
                    "sample",
                    "use_inspiration",
                    "adjust_speed",
                    "crop",
                    "reuse_prompt",
                    "get_stems",
                    "remaster",
                    "create_voice",
                    "download_mp3",
                    "download_wav",
                    "download_video",
                    "list_workspaces",
                    "list_songs",
                    "set_visibility",
                    "publish",
                    "move_to_workspace",
                    "add_to_playlist",
                    "move_to_trash",
                ],
            },
            "clip_id": {"type": "string"},
            "clip_ids": {"type": "array", "items": {"type": "string"}},
            "workspace": {"type": "string"},
            "playlist_name": {"type": "string"},
            "allow_comments": {"type": "boolean"},
            "allow_remixes": {"type": "boolean"},
            "pin_to_profile": {"type": "boolean"},
            "is_public": {"type": "boolean"},
            "speed": {"type": "number"},
            "keep_pitch": {"type": "boolean"},
            "crop_start_s": {"type": "number"},
            "crop_end_s": {"type": "number"},
            "stems_mode": {"type": "string", "enum": ["all_detected", "vocals_instrumental"]},
            "remaster_strength": {"type": "string", "enum": ["subtle", "normal", "high"]},
            "remaster_model": {"type": "string", "enum": ["v5.5", "v5", "v4.5+"]},
            "voice_name": {"type": "string"},
            "voice_styles": {"type": "string"},
            "voice_description": {"type": "string"},
            "voice_public": {"type": "boolean"},
            "voice_start_s": {"type": "number"},
            "voice_end_s": {"type": "number"},
            "lyrics": {"type": "string"},
            "styles": {"type": "string"},
            "exclude_styles": {"type": "string"},
            "song_title": {"type": "string"},
            "lyrics_mode": {"type": "string", "enum": ["custom", "auto", "mumble", "instrumental"]},
            "vocal_gender": {"type": "string", "enum": ["m", "f"]},
            "weirdness": {"type": "number"},
            "style_influence": {"type": "number"},
            "audio_type": {"type": "string", "enum": ["one_shot", "loop"]},
            "bpm": {"type": "string"},
            "key_root": {
                "type": "string",
                "enum": ["any", "c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"],
            },
            "key_scale": {"type": "string", "enum": ["major", "minor"]},
            "inspiration_clip_id": {"type": "string"},
            "inspiration_start_s": {"type": "number"},
            "inspiration_end_s": {"type": "number"},
            "out_dir": {"type": "string"},
            "page": {"type": "integer"},
            "limit": {"type": "integer"},
            "include_trashed": {"type": "boolean"},
            "exclude_shared": {"type": "boolean"},
            "poll_attempts": {"type": "integer"},
            "poll_interval": {"type": "number"},
        },
        "required": ["action"],
    },
}
