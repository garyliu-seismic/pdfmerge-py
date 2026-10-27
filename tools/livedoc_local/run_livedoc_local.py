#!/usr/bin/env python3
"""Launcher: `python tools/livedoc_local/run_livedoc_local.py <cmd> ...` from anywhere.

Puts tools/livedoc_local on sys.path (for the livedoc_local package). The pdfmergepy package is
imported from THIS checkout's src/ by the stages themselves (never the pip-editable install).
Do not use `python -I`: it ignores PYTHONPATH / script-dir path entries.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from livedoc_local.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
