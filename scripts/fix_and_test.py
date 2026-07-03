#!/usr/bin/env python3
"""Apply LangGraph-only routing patch and run pytest."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

subprocess.check_call([sys.executable, str(ROOT / "scripts" / "_apply_langgraph_only.py")])
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT))
