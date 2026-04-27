#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "").strip() or Path.home() / ".hermes")
PROFILE_HOME = HERMES_HOME / "profiles" / "cex-trader"
PYTHON = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python3"
SCRIPT = PROFILE_HOME / "scripts" / "market_scanner.py"

env = dict(os.environ)
env["TRADER_HOME"] = str(PROFILE_HOME)
result = subprocess.run(
    [str(PYTHON if PYTHON.exists() else sys.executable), str(SCRIPT), "scan"],
    capture_output=True,
    text=True,
    timeout=180,
    env=env,
)
if result.stdout:
    print(result.stdout.strip())
if result.stderr:
    print(result.stderr.strip(), file=sys.stderr)
raise SystemExit(result.returncode)

