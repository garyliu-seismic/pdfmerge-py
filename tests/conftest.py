"""Make ``src`` and the tests directory importable without an editable install."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "src", Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
