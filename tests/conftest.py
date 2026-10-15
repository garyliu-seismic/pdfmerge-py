"""Make the worktree's ``src/`` importable in-process *and* in subprocesses
(``python -m pdfmergepy`` is spawned by the regression tests), and the tests
directory importable (shared helpers such as ``fontfactory``), so the suite
runs without ``pip install -e .``."""
import os
import sys
from pathlib import Path

_TESTS = str(Path(__file__).resolve().parent)
_SRC = str(Path(__file__).resolve().parent.parent / "src")
for _p in (_SRC, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_pp = os.environ.get("PYTHONPATH", "")
if _SRC not in _pp.split(os.pathsep):
    os.environ["PYTHONPATH"] = _SRC + (os.pathsep + _pp if _pp else "")
