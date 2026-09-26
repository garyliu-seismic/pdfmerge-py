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
    invert_matrix,
    multiply_matrices,
    get_raw_page_size,
    get_page_size_with_rotation,
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
