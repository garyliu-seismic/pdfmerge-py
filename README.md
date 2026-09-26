# pdfmergepy

Page-level PDF merge CLI, built on `pikepdf` (QPDF). This is the POC-phase
deliverable from the CTS2.0 itext7→Python merge feasibility evaluation — it
covers plain page copy/concatenation only (MediaBox/CropBox/Rotate preserved
as pikepdf carries the page object over as-is).

**Out of scope (later phases, not implemented here):** tag-tree/PDF-UA
structure merging, AcroForm field-conflict resolution, and Form XObject
overlay compositing (rotation-aware affine matrix math for external-PDF +
slide compositing). See CTS2.0's `PdfMergeTagHelper.cs` / `CustomCopier.cs` /
`PdfMerger.MergePDFToUseExternalPageSize`. Use this tool to establish a
page-copy fidelity/perf baseline vs itext7 before tackling those.

## Install

```sh
pip install -e .
```

## Usage

```sh
# Merge full documents
pdfmergepy merge a.pdf b.pdf -o out.pdf

# Merge specific page ranges (1-based, comma-separated, dash ranges)
pdfmergepy merge a.pdf:1-3 b.pdf:2,4-5 -o out.pdf

# Inspect page geometry (for diffing against itext7's output) as JSON
pdfmergepy info out.pdf
```

## Comparing against CTS2.0 (itext7)

1. Run the same page range through CTS2.0's `CustomMerger.Merge` (plain-copy
   path, no external-PDF overlay) and this tool.
2. Diff `pdfmergepy info` output for both against each other (page count,
   MediaBox, CropBox, Rotate).
3. Structural/OOXML-level correctness of the merged file can be checked with
   the sibling `pdftagvalicate` tool (`pdftagvalicate --validate`).
