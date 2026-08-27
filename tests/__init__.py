"""Test package for ibus_voice_ime.

Adds the project ``src/`` directory to ``sys.path`` so that test modules can
import the ``ibus_voice_ime`` package regardless of the current working
directory (e.g. under ``python -m unittest discover -s tests``).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
