# pdfmergepy

Python PDF merge CLI, built on **pikepdf** (QPDF, MPL-2.0).
POC-phase Python rewrite of CTS2.0's itext7-based PDF merge pipeline.

## Feature Status

| Feature | Status | Notes |
|---------|--------|-------|
| Plain page concat (MediaBox/CropBox/Rotate) | ✅ Done | `merge` CLI |
| Form XObject compositing — Mode A (FitPDFSize=false) | ✅ Done | `composite.py` |
| Form XObject compositing — Mode B (FitPDFSize=true) | ✅ Done | `composite.py` |
| AcroForm field copy + conflict rename + DR merge | ✅ Done | `acroform.py` |
| Tag tree — plain-copy path (ParentTree + MCR wiring) | ✅ Done | `tagtree.merge_page_tags` |
| Tag tree — Form XObject path (MigrateTagsForXObject) | ✅ Done | `tagtree.migrate_tags_for_xobject` |
| Link annotation nesting in `<Link>` struct elem | ✅ Done | `tagtree.fix_link_annots` |
| PDF/UA metadata propagation (/Lang, /Title, XMP) | ✅ Done | `tagtree.set_pdfua_metadata` |
| Post-merge TH /Scope repair (Matterhorn 14-003) | ✅ Done | `_repairs.apply_post_merge_repairs` |
| Regression test — all 41 real blob inputs × Mode A+B | ✅ Done | `tests/test_regression_inputs.py` |
| Post-merge repairs (FixInvalidTBodies, AlterChartAsFigure) | 🔲 Planned | lower priority |

## Validate results vs itext7 (real merge, 17-slide deck)

| Check | pdfmergepy | itext7 ref |
|-------|-----------|-----------|
| 09-004 All pages tagged | ✅ **Pass (17/17)** | ❌ Fail (15/17) |
| 28-001 Link annotations nested | ✅ Pass | ✅ Pass |
| 14-003 TH /Scope | ✅ Pass | ✅ Pass |
| 13-004 Figure /Alt | ❌ Fail* | ❌ Fail |
| 31-001 All fonts embedded | ✅ Pass | ❌ Fail (4 Arial) |
| 06-003 /Title set | ✅ Pass | ❌ Fail |
| 09-006 Untagged paint ops | ❌ Fail (6) | ❌ Fail (1362) |
| 06-001 XMP pdfuaid:part | ❌ Fail† | ❌ Fail† |

\* Source PDF defect (16 logo images on page 2 have no /Alt text) — faithfully mirrored.
† XMP pdfuaid:part serialiser not yet integrated.

## Install

```sh
pip install -e .
# optional: pdftagvalicate for post-merge TH /Scope repair
pip install -e ../pdftagvalicate
```

## Usage

```sh
# Merge full documents (plain concat)
pdfmergepy merge a.pdf b.pdf -o out.pdf

# Merge specific page ranges (1-based, comma-separated, dash ranges)
pdfmergepy merge a.pdf:1-3 b.pdf:2,4-5 -o out.pdf

# Inspect page geometry as JSON (for diffing against itext7 output)
pdfmergepy info out.pdf

# WorkspaceMergeInfo XML-driven merge (CTS2.0 format, Mode A/B compositing)
pdfmergepy merge-xml merge.xml --main main.pdf --inputs-dir ./blobs -o out.pdf
```

## Tests

```sh
python -m pytest tests/ --ignore=tests/_proto_tagtree.py
# 60 passed  (unit + integration)

# Real-data regression (requires C:\test\IText7Test\PdfMerge\inputs\)
python -m pytest tests/test_regression_inputs.py -v
# 82 passed  (41 blobs × Mode A + Mode B)
```

## Architecture

```
merge.py          plain concat  →  add_pages_range_with_forms + merge_page_tags
composite.py      Mode A/B      →  page_as_form_xobject + affine matrix
                                   + migrate_tags_for_xobject
acroform.py       AcroForm      →  pikepdf.Pdf.add_pages_from(forms='preserve')
tagtree.py        StructTree    →  merge_page_tags (plain-copy, ParentTree+MCR)
                                   migrate_tags_for_xobject (XObject /Stm MCR)
                                   fix_link_annots (Matterhorn 28-001)
_repairs.py       post-merge    →  pdftagvalicate TH /Scope repair
mergeinfo.py      XML parsing   →  WorkspaceMergeInfo / MergeItem / SlideLocalId
pdfutil.py        utilities     →  page range parsing, geometry inspection
cli.py            CLI           →  argparse (merge / info / merge-xml)
```

## Comparing against CTS2.0 (itext7)

1. Run the same inputs through CTS2.0's `CustomMerger.Merge` and this tool.
2. Diff `pdfmergepy info` JSON output (page count, MediaBox, CropBox, Rotate).
3. Validate PDF/UA with `pdftagvalicate --validate --json`.

## Source references

CTS2.0 itext7 implementation:
- `PdfMerger.cs` — `CustomMerger.Merge`, `MergeNew`, `SetPDFUATag`
- `PdfMergeTagHelper.cs` (2608 lines) — `MigrateTagsForXObject` (line 886),
  `EnsureAncestorChain` (line 1117), `WriteXObjectParentTable` (line 1216),
  `ExtractMcidsFromPageContent` (line 1055)
- `CustomCopier.cs` — `PdfPageFormCopier` equivalent
