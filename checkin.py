#!/usr/bin/env python3
"""AgentRouter check-in launcher.

Always execute the core script from the repository directory so local browser
profiles and state files are stable regardless of the caller's working directory.
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

# GitHub OAuth session stability is much better when the browser mode stays
# consistent with the interactive `add` flow. Keep daily runs headed as well.
os.environ["CHECKIN_HEADLESS"] = "false"

runpy.run_path(str(BASE_DIR / "checkin_core.py"), run_name="__main__")
