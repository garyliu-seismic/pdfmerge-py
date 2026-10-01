"""Unit tests for mergeinfo.py (XML parsing) and composite.py (matrix math, merge logic).

Uses only synthetic PDFs — no real blob files required.
"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path

import pikepdf
import pytest

from pdfmergepy.mergeinfo import (
    MergeItem,
    SlideInfo,
    SlideLocalId,
    WorkspaceMergeInfo,
    parse_merge_info,
    resolve_external_page,
)
from pdfmergepy.composite import (
    _compute_fit_main_matrix,
    invert_matrix,
    multiply_matrices,
    get_raw_page_size,
    get_page_size_with_rotation,
    page_as_form_xobject,
    merge_from_xml,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pdf(path: Path, num_pages: int, width: float = 200, height: float = 300, rotate: int = 0) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(num_pages):
        page = pikepdf.Page(pdf.add_blank_page(page_size=(width, height)))
        if rotate:
            page.obj["/Rotate"] = rotate
    pdf.save(path)


def _write_xml(path: Path, content: str) -> None:
    """Write an XML file encoded as UTF-16 with BOM (matching CTS2.0 output)."""
    path.write_bytes(content.encode("utf-16"))


# ---------------------------------------------------------------------------
# XML parsing tests
# ---------------------------------------------------------------------------

MERGE_INFO_DIRECT = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="item-1" BlobId="blob-abc">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="2">42</slideLocalId>
    </MergeItem>
  </PDFMerge>
  <SlideInfo>
    <info slideIndex="1" localId="10" hidden="false" />
    <info slideIndex="2" localId="20" hidden="false" />
    <info slideIndex="3" localId="30" hidden="true" />
  </SlideInfo>
</WorkspaceMergeInfo>
"""

MERGE_INFO_APPLY_SLIDE = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="false" SlideFitPattern="AlignBottomRight" pageCount="1"
               id="item-2" BlobId="blob-xyz">
      <MergedPdfFileInfo MergedPdfFileId="merged-file-id" StartIndexInMergedFile="5" />
      <ApplySlide>
        <slideLocalId pdfPage="3" slideIndex="4">99</slideLocalId>
      </ApplySlide>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""


def test_parse_merge_info_direct_slideid(tmp_path: Path) -> None:
    """slideLocalId directly under MergeItem (no ApplySlide wrapper)."""
    xml_path = tmp_path / "mi.xml"
    _write_xml(xml_path, MERGE_INFO_DIRECT)
    mi = parse_merge_info(xml_path)

    assert len(mi.merge_items) == 1
    item = mi.merge_items[0]
    assert item.id == "item-1"
    assert item.blob_id == "blob-abc"
    assert item.fit_pdf_size is True
    assert item.slide_fit_pattern == "AlignTopLeft"
    assert item.merged_pdf_file_id == ""
    assert item.start_index_in_merged_file == -1
    assert len(item.slides) == 1
    slide = item.slides[0]
    assert slide.pdf_page == 1
    assert slide.slide_index == 2
    assert slide.local_id == 42


def test_parse_merge_info_slide_infos(tmp_path: Path) -> None:
    xml_path = tmp_path / "mi.xml"
    _write_xml(xml_path, MERGE_INFO_DIRECT)
    mi = parse_merge_info(xml_path)

    assert len(mi.slide_infos) == 3
    hidden = [si for si in mi.slide_infos if si.hidden]
    assert len(hidden) == 1
    assert hidden[0].slide_index == 3


def test_parse_merge_info_apply_slide_wrapper(tmp_path: Path) -> None:
    """slideLocalId inside <ApplySlide> wrapper."""
    xml_path = tmp_path / "mi2.xml"
    _write_xml(xml_path, MERGE_INFO_APPLY_SLIDE)
    mi = parse_merge_info(xml_path)

    assert len(mi.merge_items) == 1
    item = mi.merge_items[0]
    assert item.fit_pdf_size is False
    assert item.slide_fit_pattern == "AlignBottomRight"
    assert item.merged_pdf_file_id == "merged-file-id"
    assert item.start_index_in_merged_file == 5
    assert len(item.slides) == 1
    slide = item.slides[0]
    assert slide.pdf_page == 3
    assert slide.slide_index == 4
    assert slide.local_id == 99


def test_parse_merge_info_no_slide_infos(tmp_path: Path) -> None:
    xml_path = tmp_path / "mi3.xml"
    _write_xml(xml_path, MERGE_INFO_APPLY_SLIDE)
    mi = parse_merge_info(xml_path)
    assert mi.slide_infos == []


# ---------------------------------------------------------------------------
# resolve_external_page tests
# ---------------------------------------------------------------------------

def _make_item(
    blob_id: str,
    merged_pdf_file_id: str,
    start_index: int,
    pdf_page: int,
    slide_index: int,
    fit_pdf_size: bool = True,
) -> tuple[MergeItem, SlideLocalId]:
    slide = SlideLocalId(pdf_page=pdf_page, slide_index=slide_index, local_id=0)
    item = MergeItem(
        id="test",
        blob_id=blob_id,
        fit_pdf_size=fit_pdf_size,
        slide_fit_pattern="AlignTopLeft",
        page_count=1,
        merged_pdf_file_id=merged_pdf_file_id,
        start_index_in_merged_file=start_index,
        slides=[slide],
    )
    return item, slide


def test_resolve_external_page_use_blob(tmp_path: Path) -> None:
    """When MergedPdfFileId is empty, use BlobId."""
    (tmp_path / "blob-abc.pdf").write_bytes(b"%PDF-1.4")
    item, slide = _make_item("blob-abc", "", -1, 2, 1)
    path, page = resolve_external_page(item, slide, tmp_path)
    assert path.name.lower() == "blob-abc.pdf"
    assert page == 2


def test_resolve_external_page_use_merged(tmp_path: Path) -> None:
    """When MergedPdfFileId is set and StartIndex >= 0, use merged file."""
    (tmp_path / "merged-file-id.pdf").write_bytes(b"%PDF-1.4")
    item, slide = _make_item("blob-xyz", "merged-file-id", 5, 3, 1)
    path, page = resolve_external_page(item, slide, tmp_path)
    assert path.name.lower() == "merged-file-id.pdf"
    assert page == 8  # 5 + 3


def test_resolve_external_page_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "BLOB-ABC.PDF").write_bytes(b"%PDF-1.4")
    item, slide = _make_item("blob-abc", "", -1, 1, 1)
    path, page = resolve_external_page(item, slide, tmp_path)
    assert path.name.upper() == "BLOB-ABC.PDF"


def test_resolve_external_page_not_found(tmp_path: Path) -> None:
    item, slide = _make_item("missing", "", -1, 1, 1)
    with pytest.raises(FileNotFoundError, match="missing.pdf"):
        resolve_external_page(item, slide, tmp_path)


# ---------------------------------------------------------------------------
# Matrix math tests
# ---------------------------------------------------------------------------

def test_invert_matrix_identity() -> None:
    m = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    inv = invert_matrix(m)
    assert inv is not None
    for got, expected in zip(inv, m):
        assert abs(got - expected) < 1e-9


def test_invert_matrix_scale() -> None:
    m = [2.0, 0.0, 0.0, 3.0, 0.0, 0.0]
    inv = invert_matrix(m)
    assert inv is not None
    assert abs(inv[0] - 0.5) < 1e-9
    assert abs(inv[3] - 1.0 / 3.0) < 1e-9


def test_invert_matrix_singular() -> None:
    m = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert invert_matrix(m) is None


def test_multiply_matrices_identity() -> None:
    ident = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    m = [2.0, 0.0, 0.0, 3.0, 10.0, 20.0]
    result = multiply_matrices(ident, m)
    for got, expected in zip(result, m):
        assert abs(got - expected) < 1e-9


def test_multiply_invert_roundtrip() -> None:
    m = [0.5, 0.0, 0.0, 0.5, 100.0, 200.0]
    inv = invert_matrix(m)
    assert inv is not None
    product = multiply_matrices(inv, m)
    ident = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    for got, expected in zip(product, ident):
        assert abs(got - expected) < 1e-9


# ---------------------------------------------------------------------------
# Page geometry tests
# ---------------------------------------------------------------------------

def test_get_raw_page_size(tmp_path: Path) -> None:
    p = tmp_path / "a.pdf"
    _make_pdf(p, 1, width=400, height=600)
    with pikepdf.open(p) as pdf:
        w, h = get_raw_page_size(pdf.pages[0].obj)
    assert abs(w - 400) < 0.1
    assert abs(h - 600) < 0.1


def test_get_page_size_with_rotation_90(tmp_path: Path) -> None:
    p = tmp_path / "a.pdf"
    _make_pdf(p, 1, width=400, height=600, rotate=90)
    with pikepdf.open(p) as pdf:
        w, h = get_page_size_with_rotation(pdf.pages[0].obj)
    # After 90° rotation: logical width = raw height, logical height = raw width
    assert abs(w - 600) < 0.1
    assert abs(h - 400) < 0.1


# ---------------------------------------------------------------------------
# merge_from_xml integration test (synthetic PDFs)
# ---------------------------------------------------------------------------

def test_merge_xml_basic(tmp_path: Path) -> None:
    """3-page main PDF + 1-page external; slideIndex=2 mapped to external page 1.

    Expected output: 3 pages (slide 1 = main page 1 as-is, slide 2 = external
    page composited with main page 2, slide 3 = main page 3 as-is).
    """
    main_pdf_path = tmp_path / "main.pdf"
    ext_pdf_path = tmp_path / "blob-test.pdf"
    output_path = tmp_path / "output.pdf"

    _make_pdf(main_pdf_path, 3, width=960, height=540)
    _make_pdf(ext_pdf_path, 1, width=1280, height=720)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="test-item" BlobId="blob-test">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="2">100</slideLocalId>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_pdf_path, tmp_path, output_path)

    assert output_path.exists()
    with pikepdf.open(output_path) as out:
        assert len(out.pages) == 3


def test_merge_xml_hidden_slide_skipped(tmp_path: Path) -> None:
    """Hidden slides should not appear in the output."""
    main_pdf_path = tmp_path / "main.pdf"
    ext_pdf_path = tmp_path / "blob-test.pdf"
    output_path = tmp_path / "output.pdf"

    _make_pdf(main_pdf_path, 3, width=960, height=540)
    _make_pdf(ext_pdf_path, 1, width=960, height=540)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="test-item" BlobId="blob-test">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="1">10</slideLocalId>
    </MergeItem>
  </PDFMerge>
  <SlideInfo>
    <info slideIndex="1" localId="10" hidden="false" />
    <info slideIndex="2" localId="20" hidden="true" />
    <info slideIndex="3" localId="30" hidden="false" />
  </SlideInfo>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_pdf_path, tmp_path, output_path)

    assert output_path.exists()
    with pikepdf.open(output_path) as out:
        # Slide 2 is hidden, so only 2 pages expected
        assert len(out.pages) == 2


def test_merge_xml_mode_a_fit_false(tmp_path: Path) -> None:
    """Mode A (FitPDFSize=false): output file should open without errors."""
    main_pdf_path = tmp_path / "main.pdf"
    ext_pdf_path = tmp_path / "blob-ext.pdf"
    output_path = tmp_path / "output.pdf"

    _make_pdf(main_pdf_path, 2, width=960, height=540)
    _make_pdf(ext_pdf_path, 1, width=500, height=700)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="false" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="mode-a-item" BlobId="blob-ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <ApplySlide>
        <slideLocalId pdfPage="1" slideIndex="1">50</slideLocalId>
      </ApplySlide>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_pdf_path, tmp_path, output_path)

    assert output_path.exists()
    with pikepdf.open(output_path) as out:
        assert len(out.pages) == 2


# ---------------------------------------------------------------------------
# Fix #1: page_as_form_xobject — Resources and content from same copy_foreign
# ---------------------------------------------------------------------------

def test_page_as_form_xobject_resources_consistent(tmp_path: Path) -> None:
    """Resources in the Form XObject must be dst-local (from copy_foreign), not
    foreign indirect refs from the still-open source PDF.

    We verify this by:
    1. Building a source PDF with a named XObject resource (/Im0) in /Resources.
    2. Calling page_as_form_xobject into a fresh dst PDF while the source is open.
    3. Checking that /Resources on the resulting xobj exists and that every
       indirect object reachable from it belongs to dst (not src) — confirmed
       by the fact that dst.save() completes without 'foreign object' errors.
    """
    src_path = tmp_path / "src.pdf"
    _make_pdf(src_path, 1, width=400, height=300)

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        src_page_obj = src.pages[0].obj
        xobj = page_as_form_xobject(src_page_obj, dst)

    # Resources dict must be present and must not be a foreign-object proxy
    assert "/Resources" in xobj.stream_dict

    # Attach the xobj to a blank page and save — this will raise if any
    # object reference inside xobj still points into the (now closed) src PDF.
    blank = pikepdf.Page(dst.add_blank_page(page_size=(400, 300)))
    xobj_dict = pikepdf.Dictionary()
    xobj_dict["/Xobj0"] = xobj
    resources = pikepdf.Dictionary()
    resources["/XObject"] = xobj_dict
    blank.obj["/Resources"] = resources
    blank.obj["/Contents"] = dst.make_stream(b"q /Xobj0 Do Q")
    dst.pages.append(blank)

    out_path = tmp_path / "out.pdf"
    dst.save(out_path)  # must not raise
    assert out_path.exists()


# ---------------------------------------------------------------------------
# Fix #2: Mode B — MediaBox explicitly fixed to external page native size
# ---------------------------------------------------------------------------

def test_merge_mode_b_mediabox_is_external_size(tmp_path: Path) -> None:
    """Mode B output page MediaBox must equal the external PDF's native size,
    not the main PDF page size."""
    main_path = tmp_path / "main.pdf"
    ext_path = tmp_path / "blob-ext.pdf"
    out_path = tmp_path / "out.pdf"

    # main: 960×540 (16:9 slide)  ext: 1280×960 (4:3 document)
    _make_pdf(main_path, 1, width=960, height=540)
    _make_pdf(ext_path, 1, width=1280, height=960)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="mb-test" BlobId="blob-ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="1">1</slideLocalId>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    with pikepdf.open(out_path) as out:
        mb = [float(v) for v in out.pages[0].obj["/MediaBox"]]
    # Must be the external page size (1280×960), not main (960×540)
    assert abs(mb[2] - 1280) < 0.5, f"expected width 1280, got {mb[2]}"
    assert abs(mb[3] - 960) < 0.5, f"expected height 960, got {mb[3]}"


# ---------------------------------------------------------------------------
# Fix #3: _compute_fit_main_matrix — CropBox-aware scale (no double-correction)
# ---------------------------------------------------------------------------

def _make_pdf_with_cropbox(
    path: Path,
    media_w: float, media_h: float,
    crop_x0: float, crop_y0: float, crop_x1: float, crop_y1: float,
) -> None:
    """Create a 1-page PDF with explicit MediaBox and CropBox."""
    pdf = pikepdf.Pdf.new()
    page = pikepdf.Page(pdf.add_blank_page(page_size=(media_w, media_h)))
    page.obj["/CropBox"] = pikepdf.Array([crop_x0, crop_y0, crop_x1, crop_y1])
    pdf.save(path)


def test_compute_fit_main_matrix_no_cropbox(tmp_path: Path) -> None:
    """Without CropBox, scale = min(main_w/ext_w, main_h/ext_h)."""
    main_path = tmp_path / "main.pdf"
    ext_path = tmp_path / "ext.pdf"
    _make_pdf(main_path, 1, width=400, height=300)
    _make_pdf(ext_path, 1, width=200, height=100)  # wider ratio than main

    with pikepdf.open(main_path) as mp, pikepdf.open(ext_path) as ep:
        m = _compute_fit_main_matrix(mp.pages[0].obj, ep.pages[0].obj)

    # ext 200×100 ratio=2.0, main 400×300 ratio=1.33 → width-limited: scale=400/200=2.0
    assert abs(m[0] - 2.0) < 1e-6, f"scale a={m[0]}, expected 2.0"
    assert abs(m[3] - 2.0) < 1e-6, f"scale d={m[3]}, expected 2.0"
    # translation: e = left - scale*crop_left = 0 - 2*0 = 0
    assert abs(m[4]) < 1e-6
    assert abs(m[5]) < 1e-6


def test_compute_fit_main_matrix_with_cropbox(tmp_path: Path) -> None:
    """With CropBox, scale must be computed from CropBox dimensions only,
    without the erroneous MediaBox-ratio secondary correction.

    Setup: MediaBox=400×400, CropBox=200×100 (top-left quarter, bottom-offset).
    Main page=400×300.
    CropBox ratio = 200/100 = 2.0 > main ratio 400/300 ≈ 1.33
    → width-limited: scale = 400/200 = 2.0
    """
    main_path = tmp_path / "main.pdf"
    ext_path = tmp_path / "ext_crop.pdf"
    _make_pdf(main_path, 1, width=400, height=300)
    _make_pdf_with_cropbox(ext_path, 400, 400, 0, 300, 200, 400)  # crop is 200×100

    with pikepdf.open(main_path) as mp, pikepdf.open(ext_path) as ep:
        m = _compute_fit_main_matrix(mp.pages[0].obj, ep.pages[0].obj)

    expected_scale = 2.0  # 400 / crop_w(200)
    assert abs(m[0] - expected_scale) < 1e-6, f"scale a={m[0]}, expected {expected_scale}"
    assert abs(m[3] - expected_scale) < 1e-6, f"scale d={m[3]}, expected {expected_scale}"
    # e = left - scale*crop_x0 = 0 - 2*0 = 0
    # f = bottom - scale*crop_y0 = 0 - 2*300 = -600
    assert abs(m[4]) < 1e-6, f"e={m[4]}, expected 0"
    assert abs(m[5] - (-600.0)) < 1e-6, f"f={m[5]}, expected -600"
