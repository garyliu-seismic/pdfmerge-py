"""Small pikepdf helpers: page-range parsing and per-page geometry inspection.

Scope note: this only covers plain page-level copy (MediaBox/CropBox/Rotate
carried over as-is by pikepdf's object model). Tag-tree / AcroForm-conflict /
Form-XObject-overlay merging (CTS2.0's PdfMergeTagHelper / CustomCopier /
MergePDFToUseExternalPageSize) are out of scope for this POC — see
project_cts2_itext7_python_merge_feasibility memory for the phased plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pikepdf


@dataclass(frozen=True)
class PageSpec:
    """One input file plus the 1-based page numbers to take from it, in order."""

    path: Path
    pages: tuple[int, ...]


def parse_page_range(spec: str, total_pages: int) -> tuple[int, ...]:
    """Parse '3-7', '2,4,6-8', or '' (all pages) into a tuple of 1-based page numbers."""
    if not spec:
        return tuple(range(1, total_pages + 1))

    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str), int(end_str)
        else:
            start = end = int(part)

        if start < 1 or end < start:
            raise ValueError(f"invalid page range '{part}' (start must be ≥ 1 and ≤ end)")
        if end > total_pages:
            raise ValueError(f"page range '{part}' exceeds document page count ({total_pages})")
        pages.extend(range(start, end + 1))

    if not pages:
        raise ValueError(f"page range '{spec}' resolved to no pages")
    return tuple(pages)


def parse_input_arg(arg: str) -> tuple[Path, str]:
    """Split 'file.pdf' or 'file.pdf:3-7' into (path, range_spec)."""
    # Only treat a trailing ':...' as a page range if it isn't a Windows drive
    # letter (e.g. C:\path\file.pdf) — split on the *last* colon and check the
    # remainder looks like a page-range token (digits/commas/dashes only).
    if ":" in arg:
        head, _, tail = arg.rpartition(":")
        if tail and all(c.isdigit() or c in ",-" for c in tail):
            return Path(head), tail
    return Path(arg), ""


def page_geometry(page: pikepdf.Page) -> dict:
    """Return MediaBox/CropBox/Rotate for one page, for diffing against itext7 output."""
    obj = page.obj
    return {
        "mediabox": [float(v) for v in obj.get("/MediaBox", [])] if "/MediaBox" in obj else None,
        "cropbox": [float(v) for v in obj.get("/CropBox", [])] if "/CropBox" in obj else None,
        "rotate": int(obj.get("/Rotate", 0)),
    }
