#!/usr/bin/env python3
"""Deprecated — redirects to LangGraph-only patch."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
subprocess.check_call([sys.executable, str(ROOT / "scripts" / "_apply_langgraph_only.py")])
