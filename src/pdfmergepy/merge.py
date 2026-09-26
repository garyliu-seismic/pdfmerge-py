"""Page-level PDF merge (POC scope) — plain page copy/concatenation via pikepdf.

Mirrors the "plain copy" half of CTS2.0's CustomMerger.Merge (PdfMerger.cs):
copy a page range from a source PDF into the destination, preserving
MediaBox/CropBox/Rotate. Deliberately excludes everything PdfMerger.cs adds on
top of that — tag-tree/PDF-UA structure merging (PdfMergeTagHelper), AcroForm
field-conflict resolution (CustomCopier), and Form XObject overlay compositing
(MergePDFToUseExternalPageSize/MergePDFToFitMainPdfSize). Those are later POC
phases per project_cts2_itext7_python_merge_feasibility memory.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf

from pdfmergepy.pdfutil import PageSpec


PRODUCER = "pdfmergepy (pikepdf/QPDF)"


def merge_files(specs: list[PageSpec], output: Path) -> None:
    if not specs:
        raise ValueError("no input files given")

    with pikepdf.Pdf.new() as dst:
        for spec in specs:
            with pikepdf.open(spec.path) as src:
                for page_num in spec.pages:
                    dst.pages.append(src.pages[page_num - 1])

        # Match itext7's PdfMerger.cs, which explicitly declares 1.7
        # (WriterProperties().SetPdfVersion(PdfVersion.PDF_1_7)) rather than
        # leaving the header at whatever QPDF's default baseline is.
        dst.docinfo["/Producer"] = PRODUCER

        output.parent.mkdir(parents=True, exist_ok=True)
        dst.save(output, min_version="1.7")
