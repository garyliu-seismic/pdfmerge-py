"""Make the livedoc_local package (tools/livedoc_local) and this checkout's pdfmergepy importable."""
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[1]
_SRC = _TOOL.parents[1] / "src"
for _p in (str(_SRC), str(_TOOL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
