#!/usr/bin/env python3
"""
appctl_engine_v2.py - Backward-compatibility shim pointing to appctl_engine.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from appctl_engine import *
from appctl_engine import main

if __name__ == "__main__":
    main()
