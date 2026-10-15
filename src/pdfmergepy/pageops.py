"""Whole-document page operations: split / select / delete / append / page-count.

Ports CTS2.0 ``PdfSplitter`` / ``PdfSelector`` (+ the ``pdf split|select|delete``
converters) and ``PdfManager.GetPageCount``.  ``append`` is the generic
``PdfMerger`` append, which is exactly what ``merge.merge_files`` already is
(tag tree via ``tagtree.merge_page_tags``, AcroForm via ``acroform``), so it is
reused rather than duplicated.

select / delete / split keep the whole source document, delete the unwanted
pages and prune tags / forms / outlines (see ``prune.py``); pages are never
re-copied, so content, resources and the tag tree stay intact.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pikepdf

from pdfmergepy.merge import PRODUCER, merge_files
from pdfmergepy.pageranges import (
    parse_split_counts,
    remaining_pages,
    selected_pages,
)
from pdfmergepy.pdfutil import PageSpec
from pdfmergepy.prune import keep_only_pages
from pdfmergepy.tagtree import write_xmp_metadata

log = logging.getLogger(__name__)


class PdfInputError(ValueError):
    """The input PDF cannot be processed (encrypted, malformed, missing...)."""


def open_pdf(path: "str | Path", password: str = "") -> pikepdf.Pdf:
    """Open *path* lazily, translating pikepdf failures into :class:`PdfInputError`."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"input file not found: {path}")
    try:
        return pikepdf.open(path, password=password)
    except pikepdf.PasswordError as exc:
        raise PdfInputError(
            f"{path.name}: PDF is encrypted and requires a password (or the password is wrong)"
        ) from exc
    except pikepdf.PdfError as exc:
        raise PdfInputError(f"{path.name}: not a valid / readable PDF ({exc})") from exc


def page_count(path: "str | Path", password: str = "") -> int:
    """Port of ``PdfManager.GetPageCount``."""
    with open_pdf(path, password) as pdf:
        return len(pdf.pages)


def _finalize_pdfua(pdf: pikepdf.Pdf) -> None:
    """Mirror ``PdfSplitter.SetPDFUATag`` on the pruned copy.

    /Lang and /Title already survive (we keep the source catalog / docinfo).
    Adds DisplayDocTitle=true; for tagged sources also /MarkInfo /Marked and
    XMP ``pdfuaid:part=1``.  Untagged sources stay untagged (deliberate
    deviation: C# ``SetTagged`` would emit an empty struct tree, and claiming
    PDF/UA for untagged content is false).
    """
    vp = pdf.Root.get("/ViewerPreferences")
    if vp is None:
        vp = pikepdf.Dictionary()
        pdf.Root["/ViewerPreferences"] = vp
    vp["/DisplayDocTitle"] = True
    pdf.docinfo["/Producer"] = PRODUCER
    if "/StructTreeRoot" in pdf.Root:
        mi = pdf.Root.get("/MarkInfo")
        if mi is None:
            mi = pikepdf.Dictionary()
            pdf.Root["/MarkInfo"] = mi
        mi["/Marked"] = True
        write_xmp_metadata(pdf, pdf)


def _save(pdf: pikepdf.Pdf, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.remove_unreferenced_resources()
    pdf.save(output, force_version="1.7")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def extract_pages(input_path: "str | Path", pages_1based: list[int], output: "str | Path",
                  password: str = "") -> int:
    """Write the given 1-based pages (original order) of *input_path* to *output*."""
    input_path, output = Path(input_path), Path(output)
    if _same_file(input_path, output):
        raise ValueError("output path must differ from input path")
    with open_pdf(input_path, password) as pdf:
        total = len(pdf.pages)
        keep = sorted({p for p in pages_1based if 1 <= p <= total})
        if not keep:
            raise ValueError("page selection resulted in an empty document (no pages to write)")
        keep_only_pages(pdf, [p - 1 for p in keep])
        _finalize_pdfua(pdf)
        _save(pdf, output)
    return len(keep)


def select_pages(input_path, pages_expr: str, output, password: str = "") -> int:
    """``pdf select``: keep pages matching *pages_expr* (e.g. ``1,3,6-8,10-``)."""
    total = page_count(input_path, password)
    return extract_pages(input_path, selected_pages(pages_expr, total), output, password)


def delete_pages(input_path, pages_expr: str, output, password: str = "") -> int:
    """``pdf delete``: drop pages matching *pages_expr*, keep the rest."""
    total = page_count(input_path, password)
    return extract_pages(input_path, remaining_pages(pages_expr, total), output, password)


def split_pdf(input_path, pages_expr: str, output_dir, password: str = "") -> list[Path]:
    """``pdf split``: chunk sizes ``"3,6"`` -> 3 pages, 6 pages, remainder.

    Files are ``0000.pdf``, ``0001.pdf``... in *output_dir*.  The remainder goes
    into the last file; chunks stop as soon as no pages are left (so
    ``"3,6"`` on a 5-page document yields 0000.pdf=3 pages, 0001.pdf=2 pages).
    """
    counts = parse_split_counts(pages_expr)
    total = page_count(input_path, password)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    cursor = 0
    index = 0
    while index < len(counts) and cursor < total:
        files.append(_split_one(input_path, cursor, counts[index], index, out_dir, password))
        cursor = min(total, cursor + counts[index])
        index += 1
    if cursor < total:
        files.append(_split_one(input_path, cursor, total - cursor, index, out_dir, password))
    return files


def _split_one(input_path, start0: int, count: int, index: int, out_dir: Path, password: str) -> Path:
    out = out_dir / f"{index:04d}.pdf"
    pages = list(range(start0 + 1, start0 + count + 1))
    extract_pages(input_path, pages, out, password)
    return out


def append_files(inputs: "list[str | Path]", output: "str | Path") -> int:
    """``append``: concatenate whole documents (tags + AcroForm preserved).

    Delegates to :func:`pdfmergepy.merge.merge_files`; returns total pages.
    """
    if not inputs:
        raise ValueError("no input files given")
    specs = []
    for p in inputs:
        with open_pdf(p) as pdf:
            n = len(pdf.pages)
        specs.append(PageSpec(path=Path(p), pages=tuple(range(1, n + 1))))
    output = Path(output)
    if any(_same_file(s.path, output) for s in specs):
        raise ValueError("output path must differ from every input path")
    try:
        merge_files(specs, output)
    except pikepdf.PdfError as exc:
        raise PdfInputError(f"append failed: {exc}") from exc
    return sum(len(s.pages) for s in specs)
