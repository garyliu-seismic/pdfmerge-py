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
| Fonts: `list-fonts` / `embed-fonts` / `remove-fonts` (pikepdf + fontTools) | ✅ Done | `fonts.py`, `fontmap.py`, `fontembed.py` |
| Partial PDF overlay (`<partialPDF>`) | ✅ Done | `partial.py`, `partial-merge` CLI |
| Page ranges `1,3,6-8,10-` (port of `EnumAllPageSections`) | ✅ Done | `pageranges.py` |
| `select` / `delete` pages (tags, forms, outlines pruned) | ✅ Done | `pageops.py` + `prune.py` |
| `split` into `0000.pdf, 0001.pdf, ...` | ✅ Done | `pageops.split_pdf` |
| `append` whole PDFs (reuses merge engine) / `page-count` | ✅ Done | `pageops.py` |
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

# Fonts: inventory (JSON), embed full fonts into non-embedded fonts, remove embedded font files
pdfmergepy list-fonts in.pdf --missing-only
pdfmergepy embed-fonts in.pdf -o out.pdf --font-dir ./fonts            # uses ./fonts/font-map.json if present
pdfmergepy embed-fonts in.pdf -o out.pdf --font-map font-map.json --font Helvetica=arial.ttf --report -
pdfmergepy remove-fonts in.pdf -o out.pdf --min-size 5242880 --subset no
pdfmergepy remove-fonts in.pdf -o out.pdf --large-pdf-rule             # CTS2.0 rule: PDF >= 10 MB, fonts >= 5 MB
# Overlay <partialPDF> sub-regions onto a base PDF
pdfmergepy partial-merge merge.xml --main main.pdf --inputs-dir ./blobs -o out.pdf

# Page operations (ports of CTS2.0 `pdf select|delete|split`, PdfManager.GetPageCount)
pdfmergepy page-count in.pdf
pdfmergepy select in.pdf --pages "1,3,6-8,10-" -o out.pdf      # keep these pages
pdfmergepy delete in.pdf --pages "1,3,6-8,10-" -o out.pdf      # drop these pages
pdfmergepy split  in.pdf --pages 3,6 -o outdir/                # 0000.pdf (3 pp), 0001.pdf (6 pp), 0002.pdf (rest)
pdfmergepy append a.pdf b.pdf c.pdf -o out.pdf                 # same engine as `merge`, whole documents
# encrypted input: add --password (select/delete/split/page-count)
```

Page expressions follow `PdfUtility.EnumAllPageSections`: single `5`, range `3-7`,
open end `10-`, open start `-6`; out-of-range / duplicate / reversed (`7-3`)
items are harmless and output keeps the original page order; `0`, `-`, text and
values > int32 are errors. `select`/`delete` that would produce an empty document
are an error (iText cannot write a 0-page file either). Output is PDF 1.7 and keeps
/Lang, /Title, DisplayDocTitle; tagged sources also keep /MarkInfo and get XMP
`pdfuaid:part=1`.

select / delete / split keep the whole source, delete the unwanted pages and then
**prune** the tag tree (StructElems, `/Pg`, `/K`, ParentTree), AcroForm fields,
outlines, named destinations, link/open-action destinations and page labels
(`prune.py`) — so PDF/UA tags survive instead of dangling.


`embed-fonts` edits font dictionaries in place (no orphans), keeps /Encoding /Widths /ToUnicode, never touches
content streams or tags, and refuses (with a warning) Type0 fonts whose CIDs do not match the supplied font glyph ids.
Flags: `--strict`, `--ignore-license`, `--allow-unverified-cid`, `--remap-cid-via-tounicode`, `--require-all`, `--password`.
See STATUS.md (font section) for design notes and known gaps.

## Tests

```sh
python -m pytest tests/ --ignore=tests/_proto_tagtree.py
# 326 passed  (unit + integration + real-data regression; ~4 min with the real data present)

# Real-data regression (requires C:\test\IText7Test\PdfMerge\inputs\ and pdftagvalicate)
python -m pytest tests/test_regression_inputs.py -v
# 82 passed  (41 blobs x Mode A + Mode B)
python -m pytest tests/test_regression_pageops.py -v
# 41 passed  (select/delete/split/append per blob; validator Fail ids must be a subset of the source's)
# tests/conftest.py puts src/ on sys.path + PYTHONPATH, so `pip install -e .` is not required

# Font tests (test fonts are generated by fontTools at test time; pypdfium2 render checks run when installed)
python -m pytest tests/test_fonts.py tests/test_regression_fonts.py -v
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
pdfutil.py        utilities     →  merge's `file.pdf:1-3` range parsing, geometry inspection
pageranges.py     page ranges   →  PdfUtility.EnumAllPageSections / PageSection (select/delete/split)
prune.py          page removal  →  delete pages + prune StructTree/ParentTree/AcroForm/outlines/dests/labels
pageops.py        page ops      →  select / delete / split / append (merge engine) / page-count
cli.py            CLI           →  argparse (merge / info / merge-xml / partial-merge /
                                   split / select / delete / append / page-count /
                                   list-fonts / embed-fonts / remove-fonts)
fonts.py          fonts         →  font discovery, list-fonts, remove-fonts
fontmap.py        font matching →  font-map.json loader + PostScript/Family+Style matching
fontembed.py      embedding     →  in-place full-font embedding, CID/GID verification
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
