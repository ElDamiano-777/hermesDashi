#!/usr/bin/env python3
"""Compatibility wrapper for the migrated source-intelligence YouTube connector."""
from __future__ import annotations

import runpy
from pathlib import Path

TARGET = Path.home() / ".hermes" / "scripts" / "source_intelligence_youtube.py"
runpy.run_path(str(TARGET), run_name="__main__")

