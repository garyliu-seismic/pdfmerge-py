"""Tests for pdfmergepy.partial — partial PDF sub-region overlay.

Covers:
  1.  parse_partial_pdf_info: basic round-trip
  2.  parse_partial_pdf_info: empty / PDFMerge-only XML
  3.  _set_fit_size FitBoth — no adjustment (already correct ratio)
  4.  _set_fit_size FitBoth — height clip
  5.  _set_fit_size FitBoth — width clip + center alignment
  6.  _set_fit_size FitWidth
  7.  _set_fit_size FitHeight
  8.  _set_fit_size ScaleToFit
  9.  _build_placement_matrix — identity-scale sanity check
  10. _source_rotation_matrix — 270 degrees
  11. apply_partial_pdf — end-to-end: XObject drawn, page count preserved
  12. apply_partial_pdf — multiple z-index layers on one slide
  13. apply_partial_pdf — missing blob is skipped gracefully
  14. apply_partial_pdf — hidden slides are skipped
  15. apply_partial_pdf_from_xml — real XML file round-trip (merge-info2 (2).xml)
  16. CLI partial-merge command smoke test
"""

from __future__ import annotations

import math
from pathlib import Path

import pikepdf
import pytest

from pdfmergepy.partial import (
    PartialPdfInfo,
    _build_placement_matrix,
    _set_fit_size,
    _source_rotation_matrix,
    _compose,
    apply_partial_pdf,
    apply_partial_pdf_from_xml,
    parse_partial_pdf_info,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_blank_pdf(path: Path, pages: int = 1,
                    w: float = 960.0, h: float = 540.0) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(w, h))
    pdf.save(path)


def _make_xml(path: Path, infos: list[dict]) -> None:
    lines = [
        '<?xml version="1.0" encoding="utf-16"?>',
        '<WorkspaceMergeInfo>',
        '  <PDFMerge />',
        '  <partialPDF>',
    ]
    for i in infos:
        lines.append(
            f'    <info blob-id="{i["blob_id"]}"'
            f' content-control="{i.get("cc", "FitBoth")}"'
            f' horizontal-alignment="{i.get("ha", "Left")}"'
            f' vertical-alignment="{i.get("va", "Top")}">'
        )
        lines.append(f'      <ParentType>Slide</ParentType>')
        lines.append(f'      <SlideIndex>{i.get("slide", 1)}</SlideIndex>')
        lines.append(f'      <SlideLocalId>1</SlideLocalId>')
        lines.append(f'      <x>{i.get("x", 0)}</x>')
        lines.append(f'      <y>{i.get("y", 0)}</y>')
        lines.append(f'      <extCX>{i.get("cx", 100)}</extCX>')
        lines.append(f'      <extCY>{i.get("cy", 100)}</extCY>')
        lines.append(f'      <rotation>{i.get("rot", 0)}</rotation>')
        lines.append(f'      <z-index>{i.get("z", 0)}</z-index>')
        lines.append(f'      <image-local-id>1</image-local-id>')
        lines.append(f'      <image-part-id>rId1</image-part-id>')
        lines.append(f'    </info>')
    lines.append('  </partialPDF>')
    lines.append('</WorkspaceMergeInfo>')
    path.write_bytes('\n'.join(lines).encode('utf-16'))


# ---------------------------------------------------------------------------
# 1. parse_partial_pdf_info basic
# ---------------------------------------------------------------------------

def test_parse_basic(tmp_path: Path) -> None:
    xml = tmp_path / "m.xml"
    _make_xml(xml, [{"blob_id": "abc-123", "slide": 2, "x": 10.5, "y": 20.0,
                     "cx": 300, "cy": 200, "cc": "FitBoth",
                     "ha": "Center", "va": "Bottom", "z": 3}])
    infos = parse_partial_pdf_info(xml)
    assert len(infos) == 1
    p = infos[0]
    assert p.blob_id == "abc-123"
    assert p.slide_index == 2
    assert p.x == pytest.approx(10.5)
    assert p.y == pytest.approx(20.0)
    assert p.ext_cx == pytest.approx(300)
    assert p.ext_cy == pytest.approx(200)
    assert p.content_control == "FitBoth"
    assert p.h_align == "Center"
    assert p.v_align == "Bottom"
    assert p.z_index == 3


# ---------------------------------------------------------------------------
# 2. parse — empty / PDFMerge-only
# ---------------------------------------------------------------------------

def test_parse_no_partial_block(tmp_path: Path) -> None:
    xml = tmp_path / "m.xml"
    xml.write_bytes(
        '<?xml version="1.0" encoding="utf-16"?>\n'
        '<WorkspaceMergeInfo><PDFMerge /></WorkspaceMergeInfo>'.encode("utf-16")
    )
    infos = parse_partial_pdf_info(xml)
    assert infos == []


# ---------------------------------------------------------------------------
# 3–8. _set_fit_size
# ---------------------------------------------------------------------------

def _info(cc="FitBoth", ha="Left", va="Top",
          cx=200.0, cy=100.0, x=0.0, y=0.0) -> PartialPdfInfo:
    return PartialPdfInfo(
        blob_id="b", slide_index=1, x=x, y=y,
        ext_cx=cx, ext_cy=cy, rotation=0.0, z_index=0,
        content_control=cc, h_align=ha, v_align=va,
    )


def test_fitboth_no_adjustment() -> None:
    """src ratio == box ratio → no change."""
    w, h, x, y = _set_fit_size(_info(cc="FitBoth", cx=200, cy=100), 400.0, 200.0)
    assert w == pytest.approx(200)
    assert h == pytest.approx(100)


def test_fitboth_height_clip() -> None:
    """box is taller relative to src → height is reduced."""
    p = _info(cc="FitBoth", cx=100, cy=200, ha="Left", va="Top")
    w, h, x, y = _set_fit_size(p, 400.0, 100.0)
    expected_h = 100 * (100.0 / 400.0)
    assert h == pytest.approx(expected_h, rel=1e-4)
    assert w == pytest.approx(100)
    assert y == pytest.approx(0)  # va=Top → no y adjustment


def test_fitboth_width_clip_center() -> None:
    """box is wider → width clips and x is shifted for center alignment."""
    p = _info(cc="FitBoth", cx=400, cy=100, ha="Center", va="Top")
    w, h, x, y = _set_fit_size(p, 200.0, 400.0)
    expected_w = 100.0 * (200.0 / 400.0)
    assert w == pytest.approx(expected_w, rel=1e-4)
    assert x == pytest.approx((400 - expected_w) / 2, rel=1e-4)


def test_fitwidth() -> None:
    p = _info(cc="FitWidth", cx=200, cy=100)
    w, h, x, y = _set_fit_size(p, 400.0, 200.0)
    expected_h = 200.0 * (200.0 / 400.0)
    assert w == pytest.approx(200)
    assert h == pytest.approx(expected_h, rel=1e-4)


def test_fitheight() -> None:
    p = _info(cc="FitHeight", cx=200, cy=100)
    w, h, x, y = _set_fit_size(p, 400.0, 200.0)
    expected_w = 100.0 * (400.0 / 200.0)
    assert h == pytest.approx(100)
    assert w == pytest.approx(expected_w, rel=1e-4)


def test_scaletofit() -> None:
    p = _info(cc="ScaleToFit", cx=300, cy=150)
    w, h, x, y = _set_fit_size(p, 600.0, 300.0)
    assert w == pytest.approx(300)
    assert h == pytest.approx(150)


# ---------------------------------------------------------------------------
# 9. _build_placement_matrix — identity-scale sanity check
# ---------------------------------------------------------------------------

def test_build_matrix_identity_scale() -> None:
    """When bounding box == src size, matrix should be a pure translation."""
    m = _build_placement_matrix(
        x=10.0, y=20.0, width=100.0, height=50.0,
        rotation_deg=0.0,
        src_display_w=100.0, src_display_h=50.0,
        canvas_h=540.0,
        crop_box=None,
    )
    assert m[0] == pytest.approx(1.0, abs=1e-6)   # a
    assert m[1] == pytest.approx(0.0, abs=1e-6)   # b
    assert m[2] == pytest.approx(0.0, abs=1e-6)   # c
    assert m[3] == pytest.approx(1.0, abs=1e-6)   # d
    assert m[4] == pytest.approx(10.0, abs=1e-4)  # e  (ll_x = x)
    assert m[5] == pytest.approx(470.0, abs=1e-4) # f  (ll_y = 540-20-50=470)


# ---------------------------------------------------------------------------
# 9b. _page_display_size — CropBox + Rotate interaction (bug fix regression test)
# ---------------------------------------------------------------------------

def test_page_display_size_cropbox_rotate90(tmp_path: Path) -> None:
    """CropBox [20,30,800,630] on a Rotate=90 page.

    iText7 derives scale from (crop_h, crop_w) = (600, 780) because
    CopyAsFormXObject bakes the rotation.  _page_display_size must return
    the same effective dimensions so _set_fit_size produces matching scale.
    """
    import pikepdf
    src = tmp_path / "src.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(834, 654))   # MediaBox [0,0,834,654]
    pdf.pages[0].obj["/Rotate"]   = pikepdf.Integer(90)
    pdf.pages[0].obj["/CropBox"]  = pikepdf.Array([20, 30, 800, 630])
    pdf.save(src)

    from pdfmergepy.partial import _page_display_size
    with pikepdf.open(src) as p:
        dw, dh, cb = _page_display_size(p.pages[0].obj)

    # crop_w=780, crop_h=600; after Rotate=90 swap -> display=(600, 780)
    assert dw == pytest.approx(600.0, abs=0.1)   # crop_h (swapped)
    assert dh == pytest.approx(780.0, abs=0.1)   # crop_w (swapped)
    assert cb == pytest.approx((20.0, 30.0, 800.0, 630.0), abs=0.1)


def test_page_display_size_cropbox_rotate0(tmp_path: Path) -> None:
    """CropBox on a non-rotated page: effective size = CropBox w x h unchanged."""
    import pikepdf
    src = tmp_path / "src.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(834, 654))
    pdf.pages[0].obj["/CropBox"] = pikepdf.Array([20, 30, 800, 630])
    pdf.save(src)

    from pdfmergepy.partial import _page_display_size
    with pikepdf.open(src) as p:
        dw, dh, cb = _page_display_size(p.pages[0].obj)

    assert dw == pytest.approx(780.0, abs=0.1)   # crop_w
    assert dh == pytest.approx(600.0, abs=0.1)   # crop_h
    assert cb is not None


def test_page_display_size_no_cropbox_rotate90(tmp_path: Path) -> None:
    """No CropBox, Rotate=90: display = (h, w) from MediaBox."""
    import pikepdf
    src = tmp_path / "src.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].obj["/Rotate"] = pikepdf.Integer(90)
    pdf.save(src)

    from pdfmergepy.partial import _page_display_size
    with pikepdf.open(src) as p:
        dw, dh, cb = _page_display_size(p.pages[0].obj)

    assert dw == pytest.approx(792.0, abs=0.1)
    assert dh == pytest.approx(612.0, abs=0.1)
    assert cb is None


def test_scaletofit_cropbox_rotate90_scale(tmp_path: Path) -> None:
    """ScaleToFit with CropBox+Rotate=90: scale must match iText7's 0.858 x 0.495.

    Regression for __temp_blob.xml case:
      blob: MediaBox=834x654, CropBox=[20,30,800,630], Rotate=90
      extCX=514.83, extCY=386.12, content-control=ScaleToFit
      iText7 scale = extCX/600=0.858, extCY/780=0.495
    """
    import pikepdf, re
    src = tmp_path / "src.pdf"
    main = tmp_path / "main.pdf"
    out = tmp_path / "out.pdf"

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(834, 654))
    pdf.pages[0].obj["/Rotate"]  = pikepdf.Integer(90)
    pdf.pages[0].obj["/CropBox"] = pikepdf.Array([20, 30, 800, 630])
    pdf.save(src)
    _make_blank_pdf(main, pages=1, w=960, h=540)

    infos = [PartialPdfInfo(
        blob_id="src", slide_index=1,
        x=367.3803, y=66.5797,
        ext_cx=514.82653543307083, ext_cy=386.1199212598425,
        rotation=0.0, z_index=0,
        content_control="ScaleToFit", h_align="Left", v_align="Top",
    )]
    apply_partial_pdf(infos, main, tmp_path, out)

    # Extract the cm matrix from the output page's content
    with pikepdf.open(out) as pdf:
        pg = pdf.pages[0].obj
        contents = pg["/Contents"]
        if isinstance(contents, pikepdf.Array):
            data = b"".join(s.read_bytes() for s in contents)
        else:
            data = contents.read_bytes()
    matrices = re.findall(
        rb'([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\s+cm',
        data
    )
    assert matrices, "No cm matrix found in output"
    a, b, c, d, e, f = [float(v) for v in matrices[0]]
    # After src-rotation compose: result is a rotation matrix, not identity-scale
    # The scale embedded is sx=0.858, sy=0.495
    # Composed with Rotate=90: [0, -sy, sx, 0, e, f]
    assert abs(a) < 0.01, f"a should be ~0, got {a}"
    assert c == pytest.approx(514.82653543307083 / 600.0, rel=0.01)  # sx = extCX/crop_h
    assert abs(d) < 0.01, f"d should be ~0, got {d}"
    assert abs(b) == pytest.approx(386.1199212598425 / 780.0, rel=0.01)  # sy = extCY/crop_w


# ---------------------------------------------------------------------------
# 10. _source_rotation_matrix
# ---------------------------------------------------------------------------

def test_source_rotation_270() -> None:
    m = _source_rotation_matrix(270, display_w=792.0, display_h=612.0)
    assert m[0] == pytest.approx(0.0)
    assert m[1] == pytest.approx(1.0)
    assert m[2] == pytest.approx(-1.0)
    assert m[3] == pytest.approx(0.0)
    assert m[4] == pytest.approx(612.0)
    assert m[5] == pytest.approx(0.0)


def test_source_rotation_identity() -> None:
    m = _source_rotation_matrix(0, 100.0, 200.0)
    assert m == pytest.approx([1, 0, 0, 1, 0, 0])


# ---------------------------------------------------------------------------
# 11. apply_partial_pdf end-to-end
# ---------------------------------------------------------------------------

def test_apply_partial_pdf_basic(tmp_path: Path) -> None:
    """XObject stream is appended; page count stays the same."""
    main = tmp_path / "main.pdf"
    ext  = tmp_path / "ext.pdf"
    out  = tmp_path / "out.pdf"
    _make_blank_pdf(main, pages=2, w=960, h=540)
    _make_blank_pdf(ext,  pages=1, w=612, h=792)

    infos = [PartialPdfInfo(
        blob_id="ext", slide_index=1,
        x=10.0, y=20.0, ext_cx=300.0, ext_cy=200.0,
        rotation=0.0, z_index=0,
        content_control="FitBoth", h_align="Left", v_align="Top",
    )]
    apply_partial_pdf(infos, main, tmp_path, out)

    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 2
        res = pdf.pages[0].obj.get("/Resources")
        assert res is not None
        xobjs = res.get("/XObject")
        assert xobjs is not None and len(xobjs.keys()) >= 1
        # Page 2 untouched
        res2 = pdf.pages[1].obj.get("/Resources")
        xobjs2 = res2.get("/XObject") if res2 else None
        assert not xobjs2


# ---------------------------------------------------------------------------
# 12. Multiple z-index layers on one slide
# ---------------------------------------------------------------------------

def test_apply_multiple_layers(tmp_path: Path) -> None:
    main = tmp_path / "main.pdf"
    out  = tmp_path / "out.pdf"
    _make_blank_pdf(main, pages=1, w=960, h=540)
    _make_blank_pdf(tmp_path / "blob-a.pdf", pages=1, w=400, h=300)
    _make_blank_pdf(tmp_path / "blob-b.pdf", pages=1, w=300, h=400)

    infos = [
        PartialPdfInfo("blob-a", 1, 0, 0, 200, 150, 0, z_index=0,
                       content_control="FitBoth", h_align="Left", v_align="Top"),
        PartialPdfInfo("blob-b", 1, 500, 0, 200, 150, 0, z_index=1,
                       content_control="FitBoth", h_align="Left", v_align="Top"),
    ]
    apply_partial_pdf(infos, main, tmp_path, out)

    with pikepdf.open(out) as pdf:
        xobjs = pdf.pages[0].obj["/Resources"]["/XObject"]
        assert len(xobjs.keys()) == 2


# ---------------------------------------------------------------------------
# 13. Missing blob is skipped gracefully
# ---------------------------------------------------------------------------

def test_apply_missing_blob(tmp_path: Path) -> None:
    main = tmp_path / "main.pdf"
    out  = tmp_path / "out.pdf"
    _make_blank_pdf(main, pages=1, w=960, h=540)

    infos = [PartialPdfInfo("nonexistent", 1, 0, 0, 200, 100, 0, 0,
                             "FitBoth", "Left", "Top")]
    apply_partial_pdf(infos, main, tmp_path, out)  # must not raise

    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 1
        res = pdf.pages[0].obj.get("/Resources")
        xobjs = res.get("/XObject") if res else None
        assert not xobjs


# ---------------------------------------------------------------------------
# 14. Hidden slides are skipped
# ---------------------------------------------------------------------------

def test_apply_hidden_slide(tmp_path: Path) -> None:
    main = tmp_path / "main.pdf"
    out  = tmp_path / "out.pdf"
    _make_blank_pdf(main, pages=2, w=960, h=540)
    _make_blank_pdf(tmp_path / "blob-x.pdf", pages=1, w=612, h=792)

    infos = [PartialPdfInfo("blob-x", 1, 0, 0, 200, 100, 0, 0,
                             "FitBoth", "Left", "Top")]
    apply_partial_pdf(infos, main, tmp_path, out, hidden_slide_indices={1})

    with pikepdf.open(out) as pdf:
        for page in pdf.pages:
            res = page.obj.get("/Resources")
            xobjs = res.get("/XObject") if res else None
            assert not xobjs


# ---------------------------------------------------------------------------
# 15. Real XML round-trip
# ---------------------------------------------------------------------------

REAL_XML   = Path("C:/test/IText7Test/PdfMerge/merge-info2 (2).xml")
REAL_MAIN  = Path("C:/test/IText7Test/PdfMerge/s1.t1.t2_0.pdf")
REAL_BLOBS = Path("C:/test/IText7Test/PdfMerge/inputs")


@pytest.mark.skipif(
    not (REAL_XML.exists() and REAL_MAIN.exists() and REAL_BLOBS.exists()),
    reason="real test assets not present",
)
def test_real_xml_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "out_partial.pdf"
    n = apply_partial_pdf_from_xml(REAL_XML, REAL_MAIN, REAL_BLOBS, out)
    assert n == 1
    assert out.exists()
    with pikepdf.open(out) as pdf:
        with pikepdf.open(REAL_MAIN) as main:
            assert len(pdf.pages) == len(main.pages)
        res = pdf.pages[0].obj.get("/Resources")
        assert res is not None
        xobjs = res.get("/XObject")
        assert xobjs is not None and len(xobjs.keys()) >= 1


# ---------------------------------------------------------------------------
# 16. CLI smoke test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (REAL_XML.exists() and REAL_MAIN.exists() and REAL_BLOBS.exists()),
    reason="real test assets not present",
)
def test_cli_partial_merge(tmp_path: Path) -> None:
    from pdfmergepy.cli import main as cli_main
    out = tmp_path / "cli_out.pdf"
    rc = cli_main([
        "partial-merge",
        str(REAL_XML),
        "--main", str(REAL_MAIN),
        "--inputs-dir", str(REAL_BLOBS),
        "-o", str(out),
    ])
    assert rc == 0
    assert out.exists()
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) > 0
