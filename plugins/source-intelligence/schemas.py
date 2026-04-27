from __future__ import annotations

SOURCE_INTELLIGENCE_STATUS = {
    "name": "source_intelligence_status",
    "description": (
        "Inspect the global source-intelligence system: data paths, YouTube pipeline health, "
        "tracked channels, queued inputs, and projected trading feedback counts."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

SOURCE_INTELLIGENCE_YOUTUBE = {
    "name": "source_intelligence_youtube",
    "description": (
        "Operate the YouTube source connector. Use status for health, run to process queued/autonomous "
        "research, and queue to add a YouTube video or channel as source input."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "run", "queue"],
                "description": "Operation to perform.",
            },
            "url": {
                "type": "string",
                "description": "YouTube video/channel URL for action=queue.",
            },
            "kind": {
                "type": "string",
                "enum": ["auto", "video", "channel"],
                "description": "Source type for queued YouTube URLs.",
            },
            "run": {
                "type": "boolean",
                "description": "Whether to run the full pipeline immediately after queueing.",
            },
            "force": {
                "type": "boolean",
                "description": "Allow requeueing an already seen manual input.",
            },
        },
        "required": ["action"],
    },
}

SOURCE_INTELLIGENCE_INGEST = {
    "name": "source_intelligence_ingest",
    "description": (
        "Store a normalized source document for future extraction. This is domain-neutral and supports "
        "raw text/transcripts from YouTube, podcasts, articles, PDFs, social posts, or manual notes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_type": {
                "type": "string",
                "description": "Source type, e.g. youtube_video, podcast, blog, paper, x_thread, text.",
            },
            "url": {"type": "string", "description": "Canonical source URL if available."},
            "title": {"type": "string", "description": "Human-readable source title."},
            "domain": {
                "type": "string",
                "description": "Optional downstream domain tag, e.g. trading, content_strategy, product.",
            },
            "text": {"type": "string", "description": "Raw body text or transcript."},
            "metadata": {
                "type": "object",
                "description": "Optional arbitrary source metadata.",
                "additionalProperties": True,
            },
        },
        "required": ["source_type", "text"],
    },
}

