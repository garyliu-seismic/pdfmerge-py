"""Partial PDF merge — sub-region XObject overlay on an existing PDF page.

Ports ``PartialPdfMerger`` (CTS2.0 iText7) into pikepdf.

Unlike the full-page composite (``composite.merge_from_xml``), the *partial*
merge embeds an external PDF page as a Form XObject into a **bounding-box
sub-region** of an existing slide/page, without replacing the page content.
The placement is driven by the ``<partialPDF><info>`` block in a
``WorkspaceMergeInfo`` XML file.

Schema of the XML block parsed here::

    <WorkspaceMergeInfo>
      <PDFMerge />                <!-- may also be present; ignored here -->
      <partialPDF>
        <info parent-uri="..." blob-id="..." content-control="FitBoth"
              horizontal-alignment="Left" vertical-alignment="Top">
          <ParentType>Slide</ParentType>
          <SlideIndex>1</SlideIndex>      <!-- 1-based slide / page index -->
          <x>10.8</x>                     <!-- upper-left x  (PDF points) -->
          <y>92.95</y>                    <!-- upper-left y  (PDF points, Y-down) -->
          <extCX>684</extCX>              <!-- bounding-box width  (pts) -->
          <extCY>352.29</extCY>           <!-- bounding-box height (pts) -->
          <rotation>0</rotation>          <!-- degrees, CCW -->
          <z-index>0</z-index>
          <image-local-id>8</image-local-id>
          <image-part-id>rId11</image-part-id>
        </info>
        <!-- more <info> elements … -->
      </partialPDF>
    </WorkspaceMergeInfo>

Coordinate notes
----------------
* ``x`` / ``y`` use the **PPTX upper-left-origin, Y-down** convention (already
  expressed in PDF points by the time they reach this file).
* ``CreateFromPPTSize`` converts to **PDF lower-left-origin, Y-up**::

      ll_y = canvas_height − (y + height)
      ur_y = canvas_height − y

* Content-control modes (``SetFitSize`` logic):

  =========== ================================================================
  FitBoth     Uniform-scale the source so its *aspect ratio* fits inside the
              bounding box, adjusting x/y per v/h-alignment.
  FitWidth    Scale to fill the full extCX width; centre vertically.
  FitHeight   Scale to fill the full extCY height; centre horizontally.
  ScaleToFit  Use extCX × extCY as-is (stretch to fill, may distort).
  =========== ================================================================

* Source-rotation correction: when the external PDF page has a non-zero
  ``/Rotate`` **and** a CropBox, a secondary ``GetSourceRotationTransform``
  matrix is composed after the placement matrix (same logic as C#
  ``ComposeTransform``).

References
----------
``src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/PartialPdfMerger.cs``
``src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/PdfPageRectangle.cs``
``src/CTS/Seismic.CTS.Implements.LiveDocs/Domain/PartialPdfMergeInfo.cs``
``src/CTS/Seismic.CTS.Implements.LiveDocs/Domain/ExternalContentPD.cs``
"""

from __future__ import annotations

import contextlib
import logging
import math
import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pikepdf

log = logging.getLogger(__name__)

PRODUCER = "pdfmergepy (pikepdf/QPDF)"

# ---------------------------------------------------------------------------
# Content-control constants  (mirrors ExternalContentPD.cs)
# ---------------------------------------------------------------------------

CC_FIT_BOTH    = "FitBoth"
CC_FIT_WIDTH   = "FitWidth"
CC_FIT_HEIGHT  = "FitHeight"
CC_SCALE_TO_FIT = "ScaleToFit"

HA_LEFT   = "left"
HA_CENTER = "center"
HA_RIGHT  = "right"
VA_TOP    = "top"
VA_CENTER = "center"
VA_BOTTOM = "bottom"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PartialPdfInfo:
    """One ``<info>`` element from ``<partialPDF>``."""

    blob_id: str
    slide_index: int          # 1-based
    x: float                  # PPTX pts, upper-left X
    y: float                  # PPTX pts, upper-left Y (Y-down)
    ext_cx: float             # bounding-box width  (pts)
    ext_cy: float             # bounding-box height (pts)
    rotation: float           # degrees (positive = CCW in PDF)
    z_index: int
    content_control: str      # FitBoth / FitWidth / FitHeight / ScaleToFit
    h_align: str              # Left / Center / Right
    v_align: str              # Top / Center / Bottom
    parent_type: str = "Slide"


# ---------------------------------------------------------------------------
# XML parser
# ---------------------------------------------------------------------------

def _load_xml_root(path: Path):
    """Load WorkspaceMergeInfo XML (UTF-16 BOM or UTF-8) into an ET root."""
    import xml.etree.ElementTree as ET

    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

    text = text.lstrip("\ufeff")
    text = _re.sub(r'encoding=["\']utf-16["\']', 'encoding="utf-8"',
                   text, count=1, flags=_re.IGNORECASE)
    return ET.fromstring(text.encode("utf-8"))


def parse_partial_pdf_info(path: Path) -> list[PartialPdfInfo]:
    """Parse ``<partialPDF><info>`` elements from a WorkspaceMergeInfo XML.

    Returns a list of :class:`PartialPdfInfo` sorted by (slide_index, z_index).
    Returns an empty list if the XML contains no ``<partialPDF>`` block.
    """
    root = _load_xml_root(path)

    items: list[PartialPdfInfo] = []
    for info_el in root.findall(".//partialPDF/info"):
        def _txt(tag: str, default="") -> str:
            el = info_el.find(tag)
            return (el.text or "").strip() if el is not None else default

        def _flt(tag: str, default=0.0) -> float:
            try:
                return float(_txt(tag, str(default)))
            except ValueError:
                return default

        def _int(tag: str, default=0) -> int:
            try:
                return int(_txt(tag, str(default)))
            except ValueError:
                return default

        blob_id = info_el.get("blob-id", "")
        cc      = info_el.get("content-control", CC_FIT_BOTH)
        h_align = info_el.get("horizontal-alignment", "Left")
        v_align = info_el.get("vertical-alignment",   "Top")

        items.append(PartialPdfInfo(
            blob_id        = blob_id,
            slide_index    = _int("SlideIndex", 1),
            x              = _flt("x"),
            y              = _flt("y"),
            ext_cx         = _flt("extCX"),
            ext_cy         = _flt("extCY"),
            rotation       = _flt("rotation", 0.0),
            z_index        = _int("z-index", 0),
            content_control= cc,
            h_align        = h_align,
            v_align        = v_align,
            parent_type    = _txt("ParentType", "Slide"),
        ))

    items.sort(key=lambda i: (i.slide_index, i.z_index))
    return items


# ---------------------------------------------------------------------------
# SetFitSize  (mirrors PartialPdfMerger.SetFitSize)
# ---------------------------------------------------------------------------

def _set_fit_size(
    info: PartialPdfInfo,
    src_display_w: float,
    src_display_h: float,
) -> tuple[float, float, float, float]:
    """Compute the effective (width, height, x, y) of the bounding box.

    Mirrors ``PartialPdfMerger.SetFitSize``.  The *source display size*
    (``src_display_w``, ``src_display_h``) is the external PDF page size
    **after** applying its ``/Rotate`` value (i.e. width/height may be
    swapped for 90°/270° rotated pages).

    Returns ``(width, height, x, y)`` in PPTX pts (Y-down origin).
    """
    width  = info.ext_cx
    height = info.ext_cy
    x      = info.x
    y      = info.y

    pdf_w  = src_display_w
    pdf_h  = src_display_h

    box_w_ratio = info.ext_cx / (info.ext_cx + info.ext_cy)
    box_h_ratio = info.ext_cy / (info.ext_cx + info.ext_cy)
    pdf_w_ratio = pdf_w / (pdf_w + pdf_h)
    pdf_h_ratio = pdf_h / (pdf_w + pdf_h)

    cc = info.content_control

    if cc == CC_FIT_BOTH:
        if box_h_ratio - pdf_h_ratio > 0.01:
            new_h = info.ext_cx * (pdf_h / pdf_w)
            height = new_h
            if info.v_align.lower() == VA_BOTTOM:
                y += info.ext_cy - new_h
            elif info.v_align.lower() == VA_CENTER:
                y += (info.ext_cy - new_h) / 2
        elif box_w_ratio - pdf_w_ratio > 0.01:
            new_w = info.ext_cy * (pdf_w / pdf_h)
            width = new_w
            if info.h_align.lower() == HA_RIGHT:
                x += info.ext_cx - new_w
            elif info.h_align.lower() == HA_CENTER:
                x += (info.ext_cx - new_w) / 2

    elif cc == CC_FIT_HEIGHT:
        new_w = info.ext_cy * (pdf_w / pdf_h)
        width = new_w
        if box_h_ratio - pdf_h_ratio <= 0.01:
            # Image is taller
            x = x + (info.ext_cx - new_w) / 2

    elif cc == CC_FIT_WIDTH:
        new_h = info.ext_cx * (pdf_h / pdf_w)
        height = new_h
        if box_w_ratio - pdf_w_ratio <= 0.01:
            # Image is wider
            y = y + (info.ext_cy - new_h) / 2

    elif cc == CC_SCALE_TO_FIT:
        # Stretch to fill bounding box exactly (may distort)
        width  = info.ext_cx
        height = info.ext_cy

    return width, height, x, y


# ---------------------------------------------------------------------------
# Matrix computation  (mirrors PdfPageRectangle.CreateFromPPTSize + GetMatrix)
# ---------------------------------------------------------------------------

def _build_placement_matrix(
    x: float, y: float,
    width: float, height: float,
    rotation_deg: float,
    src_display_w: float,
    src_display_h: float,
    canvas_h: float,
    crop_box: Optional[tuple[float, float, float, float]],
) -> list[float]:
    """Build the 6-element PDF affine matrix [a,b,c,d,e,f] that places the
    external page (or its CropBox region) into the bounding box on the canvas.

    Mirrors ``PdfPageRectangle.CreateFromPPTSize`` + ``GetMatrix()``.

    Parameters
    ----------
    x, y:
        PPTX upper-left corner (Y-down, pts).
    width, height:
        Effective bounding-box size after SetFitSize (pts).
    rotation_deg:
        Shape rotation from the XML ``<rotation>`` element (degrees; positive
        = CCW in PDF convention after the ``-degree`` sign flip in C#).
    src_display_w, src_display_h:
        Source page display dimensions (after applying /Rotate).
    canvas_h:
        Height of the destination page canvas (pts).
    crop_box:
        ``(left, bottom, right, top)`` CropBox of the source page, or None.

    Returns
    -------
    list[float]
        Six floats [a, b, c, d, e, f] in PDF matrix order.
    """
    # PPTX→PDF coordinate flip
    ll_x = x
    ll_y = canvas_h - (y + height)

    scale_x = width  / src_display_w if src_display_w else 1.0
    scale_y = height / src_display_h if src_display_h else 1.0

    angle_deg = -rotation_deg   # C# negates the degree before passing to AffineTransform.Rotate

    def _mul(A: list, B: list) -> list:
        return [
            A[0]*B[0] + A[2]*B[1],
            A[1]*B[0] + A[3]*B[1],
            A[0]*B[2] + A[2]*B[3],
            A[1]*B[2] + A[3]*B[3],
            A[0]*B[4] + A[2]*B[5] + A[4],
            A[1]*B[4] + A[3]*B[5] + A[5],
        ]

    def _t(tx: float, ty: float) -> list:
        return [1.0, 0.0, 0.0, 1.0, tx, ty]

    def _s(sx: float, sy: float) -> list:
        return [sx, 0.0, 0.0, sy, 0.0, 0.0]

    def _r(deg: float, cx: float, cy: float) -> list:
        rad = math.radians(deg)
        c, s = math.cos(rad), math.sin(rad)
        return [c, s, -s, c,
                cx * (1.0 - c) + cy * s,
                cy * (1.0 - c) - cx * s]

    # T(ll_x, ll_y) · Rot(angle, w/2, h/2) · Scale(sx, sy) · T(-crop_l, -crop_b)
    M = _t(ll_x, ll_y)
    M = _mul(M, _r(angle_deg, width / 2, height / 2))
    M = _mul(M, _s(scale_x, scale_y))
    if crop_box is not None:
        M = _mul(M, _t(-crop_box[0], -crop_box[1]))

    return M


def _source_rotation_matrix(rotation: int, display_w: float, display_h: float) -> list[float]:
    """Return the secondary source-rotation correction matrix.

    Mirrors ``PartialPdfMerger.GetSourceRotationTransform``.
    Only applied when the source page has a non-zero /Rotate AND a CropBox.

    ``display_w`` / ``display_h`` come from ``GetPageSizeWithRotation()``
    (i.e. the dimensions are already swapped for 90°/270°).
    """
    n = ((rotation % 360) + 360) % 360
    w, h = display_w, display_h
    if n == 90:
        return [0.0, -1.0, 1.0, 0.0, 0.0, h]
    if n == 180:
        return [-1.0, 0.0, 0.0, -1.0, w, h]
    if n == 270:
        return [0.0, 1.0, -1.0, 0.0, h, 0.0]
    return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def _compose(outer: list[float], inner: list[float]) -> list[float]:
    """Compose two PDF affine matrices: result = outer · inner.

    Mirrors ``PartialPdfMerger.ComposeTransform``.
    """
    o, i = outer, inner
    return [
        o[0]*i[0] + o[2]*i[1],
        o[1]*i[0] + o[3]*i[1],
        o[0]*i[2] + o[2]*i[3],
        o[1]*i[2] + o[3]*i[3],
        o[0]*i[4] + o[2]*i[5] + o[4],
        o[1]*i[4] + o[3]*i[5] + o[5],
    ]


# ---------------------------------------------------------------------------
# Page geometry helpers
# ---------------------------------------------------------------------------

def _page_display_size(page_obj) -> tuple[float, float, Optional[tuple]]:
    """Return (display_w, display_h, crop_box_or_None) for a pikepdf page.

    display_w/h are the *effective visible* dimensions used for scaling —
    they match what iText7's ``CopyAsFormXObject`` presents as its coordinate
    space:

    * **No CropBox**: MediaBox dimensions after applying ``/Rotate``
      (width/height swapped for 90/270).
    * **With CropBox** (and CropBox differs from MediaBox): CropBox
      dimensions *after* applying ``/Rotate``.  iText7's
      ``CopyAsFormXObject`` internally bakes the ``/Rotate`` transform, so
      the resulting Form XObject has its axes already swapped.  Our
      ``_page_as_form_xobject`` does NOT bake the rotation (it copies the
      raw content stream), but we still need the *same scale factors* that
      iText7 derives — which are based on ``(crop_h, crop_w)`` for
      90/270-degree pages.  The raw ``_source_rotation_matrix`` composed
      afterwards takes care of the axis swap in the content stream.

    crop_box is returned as ``(left, bottom, right, top)`` when a real
    CropBox is present; ``None`` otherwise.
    """
    mb = page_obj.get("/MediaBox")
    if mb is None:
        mb_x0, mb_y0, mb_x1, mb_y1 = 0.0, 0.0, 612.0, 792.0
    else:
        mb_x0, mb_y0, mb_x1, mb_y1 = (float(v) for v in mb)

    rotation = int(page_obj.get("/Rotate", 0))

    cb = page_obj.get("/CropBox")
    crop_box: Optional[tuple] = None
    if cb is not None:
        cb_x0, cb_y0, cb_x1, cb_y1 = (float(v) for v in cb)
        if (abs(cb_x0-mb_x0) > 0.5 or abs(cb_y0-mb_y0) > 0.5 or
                abs(cb_x1-mb_x1) > 0.5 or abs(cb_y1-mb_y1) > 0.5):
            crop_box = (cb_x0, cb_y0, cb_x1, cb_y1)

    if crop_box is not None:
        # Effective visible size = CropBox dimensions after rotation swap.
        # iText7: pageSize = new Rectangle(cropBox.GetWidth(), cropBox.GetHeight())
        # then CopyAsFormXObject bakes /Rotate, so the XObject's own axes
        # are already swapped for 90/270-degree pages.
        crop_w = crop_box[2] - crop_box[0]
        crop_h = crop_box[3] - crop_box[1]
        if rotation in (90, 270):
            display_w, display_h = crop_h, crop_w
        else:
            display_w, display_h = crop_w, crop_h
    else:
        # No CropBox: use MediaBox after rotation swap (unchanged behaviour).
        raw_w = mb_x1 - mb_x0
        raw_h = mb_y1 - mb_y0
        if rotation in (90, 270):
            display_w, display_h = raw_h, raw_w
        else:
            display_w, display_h = raw_w, raw_h

    return display_w, display_h, crop_box


# ---------------------------------------------------------------------------
# Form XObject helpers
# ---------------------------------------------------------------------------

def _page_as_form_xobject(src_page_obj, dst: pikepdf.Pdf) -> pikepdf.Object:
    """Convert *src_page_obj* (from a foreign PDF) into a Form XObject in *dst*."""
    try:
        copied = dst.copy_foreign(src_page_obj)
    except Exception:
        copied = None

    mb_raw = (copied or src_page_obj).get("/MediaBox")
    if mb_raw is not None:
        mediabox = pikepdf.Array([float(v) for v in mb_raw])
    else:
        mediabox = pikepdf.Array([0.0, 0.0, 612.0, 792.0])

    contents = src_page_obj.get("/Contents")
    if contents is None:
        content_bytes = b""
    elif isinstance(contents, pikepdf.Array):
        content_bytes = b"".join(cs.read_bytes() for cs in contents)
    else:
        content_bytes = contents.read_bytes()

    xobj = dst.make_stream(content_bytes)
    xobj.stream_dict["/Type"]    = pikepdf.Name("/XObject")
    xobj.stream_dict["/Subtype"] = pikepdf.Name("/Form")
    xobj.stream_dict["/BBox"]    = mediabox

    if copied is not None and "/Resources" in copied:
        xobj.stream_dict["/Resources"] = copied["/Resources"]
    else:
        xobj.stream_dict["/Resources"] = pikepdf.Dictionary()

    return xobj


def _append_xobject_to_page(
    dst_page_obj,
    dst: pikepdf.Pdf,
    xobj: pikepdf.Object,
    xobj_name: str,
    matrix: list[float],
) -> None:
    """Append a content stream that draws *xobj* with *matrix* onto *dst_page_obj*."""
    if "/Resources" not in dst_page_obj:
        dst_page_obj["/Resources"] = pikepdf.Dictionary()
    res = dst_page_obj["/Resources"]
    if "/XObject" not in res:
        res["/XObject"] = pikepdf.Dictionary()
    res["/XObject"][pikepdf.Name(xobj_name)] = dst.make_indirect(xobj)

    a, b, c, d, e, f = matrix
    m_str = f"{a:.6f} {b:.6f} {c:.6f} {d:.6f} {e:.6f} {f:.6f}"
    draw = f"\nq {m_str} cm\nq\n{xobj_name} Do\nQ\nQ\n".encode()

    new_stream = dst.make_stream(draw)
    existing = dst_page_obj.get("/Contents")
    if existing is None:
        dst_page_obj["/Contents"] = new_stream
    elif isinstance(existing, pikepdf.Array):
        existing.append(new_stream)
    else:
        dst_page_obj["/Contents"] = pikepdf.Array([existing, new_stream])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_partial_pdf(
    partial_infos: list[PartialPdfInfo],
    main_pdf_path: Path,
    inputs_dir: Path,
    output_path: Path,
    *,
    hidden_slide_indices: Optional[set[int]] = None,
) -> None:
    """Overlay partial PDFs onto the pages of *main_pdf_path*.

    For each ``<info>`` entry, the external PDF's first page is embedded as a
    Form XObject and drawn into the bounding box defined by x/y/extCX/extCY on
    the matching slide page.  Multiple entries on the same slide are applied in
    z-index order (lowest first = bottom-most layer).

    The main PDF content is preserved unchanged; only new content streams are
    appended on affected pages.

    Parameters
    ----------
    partial_infos:
        Parsed list from :func:`parse_partial_pdf_info`.
    main_pdf_path:
        Path to the base PDF (output of the standard merge pipeline).
    inputs_dir:
        Directory containing external PDF blobs (``<blob-id>.pdf``).
    output_path:
        Destination path for the result PDF.
    hidden_slide_indices:
        1-based slide indices to skip (matches ``SlideInfo.hidden=true``).
    """
    if not partial_infos:
        log.info("apply_partial_pdf: no partialPDF entries, copying main PDF as-is")
        import shutil
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(main_pdf_path, output_path)
        return

    hidden = hidden_slide_indices or set()

    by_slide: dict[int, list[PartialPdfInfo]] = {}
    for info in partial_infos:
        by_slide.setdefault(info.slide_index, []).append(info)

    def _find_ext_pdf(blob_id: str) -> Optional[Path]:
        target = f"{blob_id}.pdf".lower()
        for p in inputs_dir.iterdir():
            if p.is_file() and p.name.lower() == target:
                return p
        return None

    def _new_page_index(slide_idx: int) -> Optional[int]:
        hidden_before = sum(1 for h in hidden if h < slide_idx)
        if slide_idx in hidden:
            return None
        return slide_idx - hidden_before

    with pikepdf.open(main_pdf_path) as main_pdf, contextlib.ExitStack() as stack:
        with pikepdf.Pdf.new() as dst:
            dst.pages.extend(main_pdf.pages)
            try:
                dst.docinfo.update(main_pdf.docinfo)
            except Exception:
                pass
            dst.docinfo["/Producer"] = PRODUCER

            _open_ext: dict[Path, pikepdf.Pdf] = {}

            def _get_ext(path: Path) -> pikepdf.Pdf:
                if path not in _open_ext:
                    _open_ext[path] = stack.enter_context(pikepdf.open(path))
                return _open_ext[path]

            xobj_counter = 0

            for slide_idx in sorted(by_slide.keys()):
                new_idx = _new_page_index(slide_idx)
                if new_idx is None:
                    log.debug("apply_partial_pdf: slide %d is hidden, skipping", slide_idx)
                    continue
                if new_idx < 1 or new_idx > len(dst.pages):
                    log.warning("apply_partial_pdf: slide %d → page %d out of range (%d pages)",
                                slide_idx, new_idx, len(dst.pages))
                    continue

                dst_page_obj = dst.pages[new_idx - 1].obj
                canvas_mb = dst_page_obj.get("/MediaBox")
                canvas_h = float(canvas_mb[3]) - float(canvas_mb[1]) if canvas_mb else 792.0

                for info in sorted(by_slide[slide_idx], key=lambda i: i.z_index):
                    ext_path = _find_ext_pdf(info.blob_id)
                    if ext_path is None:
                        log.warning("apply_partial_pdf: blob not found: %s.pdf in %s",
                                    info.blob_id, inputs_dir)
                        continue

                    ext_pdf = _get_ext(ext_path)
                    if len(ext_pdf.pages) == 0:
                        log.warning("apply_partial_pdf: external PDF %s has no pages", ext_path)
                        continue

                    src_page_obj = ext_pdf.pages[0].obj
                    display_w, display_h, crop_box = _page_display_size(src_page_obj)
                    src_rotation = int(src_page_obj.get("/Rotate", 0))

                    eff_w, eff_h, eff_x, eff_y = _set_fit_size(info, display_w, display_h)

                    matrix = _build_placement_matrix(
                        eff_x, eff_y, eff_w, eff_h,
                        info.rotation,
                        display_w, display_h,
                        canvas_h,
                        crop_box,
                    )

                    if crop_box is not None and src_rotation % 360 != 0:
                        rot_m = _source_rotation_matrix(src_rotation, display_w, display_h)
                        matrix = _compose(matrix, rot_m)

                    xobj = _page_as_form_xobject(src_page_obj, dst)
                    xobj_name = f"/PXobj{xobj_counter}"
                    xobj_counter += 1

                    _append_xobject_to_page(dst_page_obj, dst, xobj, xobj_name, matrix)

                    log.info(
                        "apply_partial_pdf: slide=%d page=%d blob=%s xobj=%s "
                        "matrix=[%.4f %.4f %.4f %.4f %.4f %.4f]",
                        slide_idx, new_idx, info.blob_id, xobj_name, *matrix,
                    )

            output_path.parent.mkdir(parents=True, exist_ok=True)
            dst.save(output_path, min_version="1.7")

    log.info("apply_partial_pdf: wrote %s", output_path)


# ---------------------------------------------------------------------------
# CLI-callable convenience wrapper
# ---------------------------------------------------------------------------

def apply_partial_pdf_from_xml(
    xml_path: Path,
    main_pdf_path: Path,
    inputs_dir: Path,
    output_path: Path,
) -> int:
    """Parse *xml_path* and call :func:`apply_partial_pdf`.

    Returns the number of partial-PDF entries applied.
    """
    infos = parse_partial_pdf_info(xml_path)
    if not infos:
        log.warning("apply_partial_pdf_from_xml: no <partialPDF> entries in %s", xml_path)
    apply_partial_pdf(infos, main_pdf_path, inputs_dir, output_path)
    return len(infos)
