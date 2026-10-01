"""Page-level PDF merge — plain page copy/concatenation via pikepdf.

Mirrors the "plain copy" half of CTS2.0's CustomMerger.Merge (PdfMerger.cs):
copy a page range from a source PDF into the destination, preserving
MediaBox/CropBox/Rotate *and* AcroForm widget fields (via
``acroform.add_pages_range_with_forms``, which wraps pikepdf's
``add_pages_from(forms='preserve')`` — the Python equivalent of iText7's
``PdfPageFormCopier``).

Out of scope: tag-tree/PDF-UA structure merging (PdfMergeTagHelper) and Form
XObject overlay compositing (MergePDFToUseExternalPageSize / 
MergePDFToFitMainPdfSize) — those live in composite.py.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf

from pdfmergepy.acroform import add_pages_range_with_forms
from pdfmergepy.pdfutil import PageSpec
from pdfmergepy.tagtree import fix_link_annots, merge_page_tags, set_pdfua_metadata
from pdfmergepy._repairs import apply_post_merge_repairs


PRODUCER = "pdfmergepy (pikepdf/QPDF)"


def merge_files(specs: list[PageSpec], output: Path) -> None:
    """Concatenate page ranges from one or more PDFs, preserving AcroForm fields.

    Uses ``add_pages_from(forms='preserve')`` instead of ``pages.append`` so
    that widget fields are copied into the destination ``/AcroForm`` with
    conflict renaming — matching iText7's ``PdfPageFormCopier`` behaviour.
    """
    if not specs:
        raise ValueError("no input files given")

    with pikepdf.Pdf.new() as dst:
        _pdfua_set = False
        _first_src: "pikepdf.Pdf | None" = None  # kept open for XMP repair below
        for spec in specs:
            with pikepdf.open(spec.path) as src:
                # Propagate PDF/UA metadata from the first tagged source
                if not _pdfua_set:
                    set_pdfua_metadata(src, dst)
                    _pdfua_set = True

                # Convert 1-based page numbers to 0-based indices for pikepdf
                page_indices = [p - 1 for p in spec.pages]
                # Record dst page count BEFORE appending so we can locate new pages
                dst_base = len(dst.pages)
                add_pages_range_with_forms(dst, src, page_indices, src_path=spec.path)

                # Merge tag tree for each copied page (src still open → copy_foreign safe)
                for n, src_idx in enumerate(page_indices):
                    dst_page_obj = dst.pages[dst_base + n].obj
                    merge_page_tags(
                        src, dst, dst_page_obj,
                        src_page_index=src_idx,
                        src_path=spec.path,
                    )

        # Match itext7's PdfMerger.cs, which explicitly declares 1.7
        # (WriterProperties().SetPdfVersion(PdfVersion.PDF_1_7)) rather than
        # leaving the header at whatever QPDF's default baseline is.
        dst.docinfo["/Producer"] = PRODUCER

        # Matterhorn 28-001: wrap any Link annotations that are still orphaned
        # (no /StructParent) inside <Link> struct elements with OBJR references.
        fix_link_annots(dst)

        # Post-merge repairs: XMP pdfuaid:part (06-001) + TH /Scope (14-003).
        # Re-open the first source to copy title/lang/dates into the XMP stream.
        try:
            with pikepdf.open(specs[0].path) as first_src:
                apply_post_merge_repairs(dst, src=first_src)
        except Exception:
            apply_post_merge_repairs(dst, src=None)

        output.parent.mkdir(parents=True, exist_ok=True)
        dst.save(output, min_version="1.7")
