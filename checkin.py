#!/usr/bin/env python3
"""AgentRouter check-in entrypoint.

Runtime behavior lives in checkin_core.py. This file only pins the working
directory and headed browser mode so daily automation and interactive add
use the same local environment.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# Keep daily OAuth runs in the same browser mode as the interactive `add` flow.
# This is intentionally fixed rather than exposed as a user setting.
os.environ["CHECKIN_HEADLESS"] = "false"

import checkin_core as core  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(core.main())
