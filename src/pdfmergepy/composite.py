"""Compositing engine for WorkspaceMergeInfo-driven PDF merging.

Ports CTS2.0's PdfMerger.cs MergeNew logic:
  - Mode A (FitPDFSize=false): external page scaled/fitted to main page size
  - Mode B (FitPDFSize=true):  external page at native size, main overlay on top

Both modes use Form XObject compositing via pikepdf content-stream manipulation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import pikepdf

from pdfmergepy.acroform import add_page_with_forms
from pdfmergepy.tagtree import (
    fix_link_annots,
    merge_page_tags,
    migrate_tags_for_xobject,
    set_pdfua_metadata,
)
from pdfmergepy._repairs import apply_post_merge_repairs
from pdfmergepy.mergeinfo import (
    MergeItem,
    SlideLocalId,
    WorkspaceMergeInfo,
    resolve_external_page,
)


# ---------------------------------------------------------------------------
# Affine matrix helpers
# ---------------------------------------------------------------------------

Matrix6 = list[float]  # [a, b, c, d, e, f]


def invert_matrix(m: Matrix6) -> Optional[Matrix6]:
    """Return the inverse of a 2D affine matrix [a,b,c,d,e,f], or None if singular."""
    det = m[0] * m[3] - m[1] * m[2]
    if abs(det) < 1e-9:
        return None
    inv = 1.0 / det
    return [
        m[3] * inv,
        -m[1] * inv,
        -m[2] * inv,
        m[0] * inv,
        (m[2] * m[5] - m[3] * m[4]) * inv,
        (m[1] * m[4] - m[0] * m[5]) * inv,
    ]


def multiply_matrices(A: Matrix6, B: Matrix6) -> Matrix6:
    """Multiply two 2D affine matrices: result = A * B."""
    return [
        A[0] * B[0] + A[2] * B[1],
        A[1] * B[0] + A[3] * B[1],
        A[0] * B[2] + A[2] * B[3],
        A[1] * B[2] + A[3] * B[3],
        A[0] * B[4] + A[2] * B[5] + A[4],
        A[1] * B[4] + A[3] * B[5] + A[5],
    ]


def fmt_matrix(m: Matrix6) -> str:
    """Format matrix as PDF content-stream token sequence."""
    return " ".join(f"{v:.6f}" for v in m)


# ---------------------------------------------------------------------------
# Page geometry helpers
# ---------------------------------------------------------------------------

def _get_mediabox(page_obj) -> tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) from /MediaBox, defaulting to US Letter."""
    mb = page_obj.get("/MediaBox")
    if mb is None:
        return 0.0, 0.0, 612.0, 792.0
    return tuple(float(v) for v in mb)  # type: ignore[return-value]


def _get_cropbox(page_obj) -> Optional[tuple[float, float, float, float]]:
    """Return (x0, y0, x1, y1) from /CropBox if present, else None."""
    cb = page_obj.get("/CropBox")
    if cb is None:
        return None
    return tuple(float(v) for v in cb)  # type: ignore[return-value]


def get_raw_page_size(page_obj) -> tuple[float, float]:
    """Return (width, height) from /MediaBox without rotation."""
    x0, y0, x1, y1 = _get_mediabox(page_obj)
    return x1 - x0, y1 - y0


def get_page_size_with_rotation(page_obj) -> tuple[float, float]:
    """Return (width, height) after applying /Rotate."""
    w, h = get_raw_page_size(page_obj)
    rotation = int(page_obj.get("/Rotate", 0))
    if rotation in (90, 270):
        return h, w
    return w, h


# ---------------------------------------------------------------------------
# Content-stream helpers
# ---------------------------------------------------------------------------

def _read_page_content_bytes(page_obj) -> bytes:
    """Read all content stream bytes for a page."""
    contents = page_obj.get("/Contents")
    if contents is None:
        return b""
    if isinstance(contents, pikepdf.Array):
        return b"".join(cs.read_bytes() for cs in contents)
    return contents.read_bytes()


def prepend_content(page_obj, dst_pdf: pikepdf.Pdf, content_bytes: bytes) -> None:
    """Prepend a new content stream to the page.

    Fragility note (畸形 PDF 风险): the prepended bytes are expected to open
    a graphics-state save (`q … cm q`) that the matching append_content closes
    (`Q Q`).  If the *existing* page content stream already contains unbalanced
    q/Q operators (a non-conforming PDF), the graphics-state stack will be
    corrupted for subsequent content.  CTS2.0's CustomCopier guards against
    this by normalising the stream before wrapping; that defence is not
    implemented here.  Flag for the regression-test phase: exercise with any
    historical problem samples that have known q/Q imbalances.
    """
    new_stream = dst_pdf.make_stream(content_bytes)
    existing = page_obj.get("/Contents")
    if existing is None:
        page_obj["/Contents"] = new_stream
    elif isinstance(existing, pikepdf.Array):
        page_obj["/Contents"] = pikepdf.Array([new_stream] + list(existing))
    else:
        page_obj["/Contents"] = pikepdf.Array([new_stream, existing])


def append_content(page_obj, dst_pdf: pikepdf.Pdf, content_bytes: bytes) -> None:
    """Append a new content stream to the page."""
    new_stream = dst_pdf.make_stream(content_bytes)
    existing = page_obj.get("/Contents")
    if existing is None:
        page_obj["/Contents"] = new_stream
    elif isinstance(existing, pikepdf.Array):
        page_obj["/Contents"] = pikepdf.Array(list(existing) + [new_stream])
    else:
        page_obj["/Contents"] = pikepdf.Array([existing, new_stream])


def add_xobject_to_resources(
    page_obj,
    dst_pdf: pikepdf.Pdf,
    xobj_name: str,
    xobj_stream,
) -> None:
    """Register xobj_stream as /Resources/XObject/<xobj_name> on the page."""
    if "/Resources" not in page_obj:
        page_obj["/Resources"] = pikepdf.Dictionary()
    resources = page_obj["/Resources"]
    # pikepdf automatically dereferences indirect objects on access, so
    # resources is always a Dictionary here (no pikepdf.Reference check needed)
    if "/XObject" not in resources:
        resources["/XObject"] = pikepdf.Dictionary()
    xobjects = resources["/XObject"]
    xobjects[pikepdf.Name(xobj_name)] = xobj_stream


# ---------------------------------------------------------------------------
# Form XObject construction
# ---------------------------------------------------------------------------

def page_as_form_xobject(src_page_obj, dst_pdf: pikepdf.Pdf) -> pikepdf.Object:
    """Copy src_page_obj (a pikepdf dict) into dst_pdf as a Form XObject.

    src_page_obj is the raw page dictionary (already unwrapped from pikepdf.Page
    via .obj).  The source PDF must still be open when this is called.

    Correctness requirement: the content stream and /Resources dict placed in
    the Form XObject MUST come from the same copy_foreign() traversal.  If we
    read content_bytes from the original src and Resources from a copy_foreign
    copy, the resource names in the content stream (/F1, /Im0, …) still match
    — names are stable — but every indirect object reference inside Resources
    has been remapped to dst_pdf's object numbering by copy_foreign.  Taking
    Resources from the copy and content bytes from the original is therefore
    safe **as long as** the content stream itself contains only name tokens
    (not raw object references), which is the PDF spec guarantee for page
    content streams.  The subtlety is that sub-resources inside Resources
    (fonts, images, nested XObjects) that are indirect must also be copied;
    copy_foreign handles this recursively.

    Strategy:
    1. copy_foreign the entire page dict into dst_pdf.  This deep-copies
       Resources (and all referenced fonts/images/XObjects) with consistent
       dst-local object numbers.
    2. Read content bytes from the *original* src_page_obj (safe: bytes are
       value-copied, no indirect refs in content streams).
    3. Build a new stream in dst_pdf using those bytes + the copied Resources.
    """
    # Step 1: deep-copy the page object tree into dst so all Resources
    # (fonts, images, nested XObjects) get consistent dst-local object numbers.
    try:
        copied_obj = dst_pdf.copy_foreign(src_page_obj)
    except Exception:
        copied_obj = None

    # Step 2: MediaBox for /BBox — read from the copy (already dst-local),
    # fall back to the original if copy_foreign failed.
    if copied_obj is not None:
        mb_raw = copied_obj.get("/MediaBox")
    else:
        mb_raw = src_page_obj.get("/MediaBox")

    if mb_raw is not None:
        mediabox = pikepdf.Array([float(v) for v in mb_raw])
    else:
        w, h = get_raw_page_size(src_page_obj)
        mediabox = pikepdf.Array([0.0, 0.0, w, h])

    # Step 3: read content bytes from the original.  Content streams contain
    # only name-token references to Resources (e.g. /F1 Tf), never raw indirect
    # object refs, so reading from src is correct — the names match the
    # already-copied Resources dict in copied_obj.
    content_bytes = _read_page_content_bytes(src_page_obj)

    # Step 4: build the Form XObject stream entirely within dst_pdf.
    xobj = dst_pdf.make_stream(content_bytes)
    xobj.stream_dict["/Type"] = pikepdf.Name("/XObject")
    xobj.stream_dict["/Subtype"] = pikepdf.Name("/Form")
    xobj.stream_dict["/BBox"] = mediabox

    # Use Resources from the copy_foreign result so every indirect sub-object
    # (font descriptors, image streams, etc.) is dst-local and valid.
    if copied_obj is not None and "/Resources" in copied_obj:
        xobj.stream_dict["/Resources"] = copied_obj["/Resources"]
    else:
        xobj.stream_dict["/Resources"] = pikepdf.Dictionary()

    return xobj


# ---------------------------------------------------------------------------
# Base-CTM detection (Mode B, step 4)
# ---------------------------------------------------------------------------

_CTM_RE = re.compile(
    rb"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"  # a
    rb"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"   # b
    rb"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"   # c
    rb"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"   # d
    rb"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"   # e
    rb"\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"   # f
    rb"\s+cm\b"
)


def _detect_base_ctm(page_obj) -> Optional[Matrix6]:
    """Return the unguarded base CTM from the first content stream, or None.

    Scans the first 200 bytes of the first content stream.  If the stream
    starts with '6 numbers cm' without a preceding 'q', that CTM is returned
    so the caller can neutralise it.
    """
    contents = page_obj.get("/Contents")
    if contents is None:
        return None

    # Get the first stream only
    if isinstance(contents, pikepdf.Array):
        if not contents:
            return None
        first_stream = contents[0]
    else:
        first_stream = contents

    try:
        head = first_stream.read_bytes()[:200]
    except Exception:
        return None

    m = _CTM_RE.match(head)
    if m is None:
        return None

    return [float(m.group(i)) for i in range(1, 7)]


# ---------------------------------------------------------------------------
# Mode B: FitPDFSize=true  (external native size, main as XObject on top)
# ---------------------------------------------------------------------------

def _build_rotation_aware_matrix(
    main_page_obj,
    ext_page_obj,
    align_bottom_right: bool,
) -> Matrix6:
    """Port of CTS2.0 BuildRotationAwareMatrix."""
    sw, sh = get_page_size_with_rotation(main_page_obj)

    mb_x0, mb_y0, mb_x1, mb_y1 = _get_mediabox(ext_page_obj)
    cb = _get_cropbox(ext_page_obj)
    rotation = int(ext_page_obj.get("/Rotate", 0))

    # Determine visibleBox: use CropBox if it's smaller and offset from MediaBox
    if cb is not None:
        cb_x0, cb_y0, cb_x1, cb_y1 = cb
        cb_w = cb_x1 - cb_x0
        cb_h = cb_y1 - cb_y0
        mb_w = mb_x1 - mb_x0
        mb_h = mb_y1 - mb_y0
        # Use CropBox if it is strictly smaller than or offset from MediaBox
        if cb_w < mb_w or cb_h < mb_h or cb_x0 != mb_x0 or cb_y0 != mb_y0:
            ox, oy = cb_x0, cb_y0
            vw, vh = cb_w, cb_h
        else:
            ox, oy = mb_x0, mb_y0
            vw, vh = mb_w, mb_h
    else:
        ox, oy = mb_x0, mb_y0
        vw, vh = mb_x1 - mb_x0, mb_y1 - mb_y0

    quarter_turn = rotation in (90, 270)
    display_w = vh if quarter_turn else vw
    display_h = vw if quarter_turn else vh

    s = min(display_w / sw, display_h / sh) if sw > 0 and sh > 0 else 1.0
    placed_w = sw * s
    placed_h = sh * s
    slack_x = max(0.0, display_w - placed_w)
    slack_y = max(0.0, display_h - placed_h)
    offset_x = slack_x if align_bottom_right else 0.0
    offset_y = slack_y if align_bottom_right else 0.0

    if rotation == 90:
        a, b, c, d = 0.0, s, -s, 0.0
        e = ox + sh * s + offset_y
        f = oy + offset_x
    elif rotation == 180:
        a, b, c, d = -s, 0.0, 0.0, -s
        e = ox + vw - offset_x
        f = oy + sh * s + offset_y
    elif rotation == 270:
        a, b, c, d = 0.0, -s, s, 0.0
        e = ox + vw - sh * s - offset_y
        f = oy + vh - offset_x
    else:  # 0
        a, b, c, d = s, 0.0, 0.0, s
        e = ox + offset_x
        f = oy + vh - offset_y - sh * s

    return [a, b, c, d, e, f]


def _merge_fit_pdf_size(
    main_page_obj,
    ext_page_obj,
    dst: pikepdf.Pdf,
    item: MergeItem,
    ext_src: pikepdf.Pdf,
    ext_page_index: int,
    ext_path: "Path | str" = "",
    main_src: "pikepdf.Pdf | None" = None,
    main_page_index: int = 0,
) -> None:
    """Mode B: append external page natively, draw main page as XObject on top.

    ``ext_src`` and ``ext_page_index`` are required so that
    ``add_page_with_forms`` can carry AcroForm widget fields from the external
    PDF into *dst* (mirrors iText7's ``PdfPageFormCopier`` on the external-page
    path; note that CTS2.0 had this call commented out, but we implement it
    here for correctness).
    """
    align_bottom_right = item.slide_fit_pattern == "AlignBottomRight"

    # Step 1: copy external page into dst, preserving AcroForm fields.
    add_page_with_forms(dst, ext_src, ext_page_index, src_path=ext_path)
    new_page_obj = dst.pages[-1].obj

    # Step 2: explicitly fix the MediaBox on the new page to the external page's
    # native dimensions.  pikepdf.Page(ext_page_obj) copies the object but the
    # resulting indirect reference in dst may not yet have a settled MediaBox if
    # ext_page_obj came from a foreign Pdf still open elsewhere.  Writing it
    # explicitly here also makes the Mode B semantic unambiguous in tests.
    ext_mb_raw = ext_page_obj.get("/MediaBox")
    if ext_mb_raw is not None:
        new_page_obj["/MediaBox"] = pikepdf.Array([float(v) for v in ext_mb_raw])

    # Step 3: compute placement matrix
    matrix = _build_rotation_aware_matrix(main_page_obj, ext_page_obj, align_bottom_right)

    # Step 4: detect and neutralise unguarded base CTM
    base_ctm = _detect_base_ctm(new_page_obj)
    if base_ctm is not None:
        inv = invert_matrix(base_ctm)
        if inv is not None:
            matrix = multiply_matrices(inv, matrix)

    # Step 5: create Form XObject from main page
    xobj = page_as_form_xobject(main_page_obj, dst)

    # Step 6: add overlay content stream
    overlay = (
        f"\nq {fmt_matrix(matrix)} cm\nq\n/Xobj0 Do\nQ\nQ\n"
    ).encode()
    append_content(new_page_obj, dst, overlay)

    # Step 7: register XObject in resources
    add_xobject_to_resources(new_page_obj, dst, "/Xobj0", xobj)

    # Step 8 (Phase 2): migrate main-page tag tree into dst via XObject MCR dicts.
    if main_src is not None:
        try:
            migrate_tags_for_xobject(
                main_src, main_page_index, xobj, dst, new_page_obj,
            )
        except Exception as _exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "migrate_tags_for_xobject (Mode B) failed: %s", _exc
            )


# ---------------------------------------------------------------------------
# Mode A: FitPDFSize=false  (external scaled to fit main page size)
# ---------------------------------------------------------------------------

def _compute_fit_main_matrix(
    main_page_obj,
    ext_page_obj,
) -> Matrix6:
    """Port of CTS2.0 GetTransformMatrixFitMainPdfSize.

    The scale is computed so that the *visible* region of the external page
    (CropBox when present, otherwise MediaBox) fits inside the main page
    dimensions.  Using CropBox directly for the aspect-ratio test avoids the
    previous double-correction bug where the scale was first derived from the
    full MediaBox and then divided by the crop/media ratio — which compounded
    the scaling in the wrong direction when CropBox != MediaBox.
    """
    # Main page dimensions (after rotation)
    main_w, main_h = get_page_size_with_rotation(main_page_obj)

    ext_mb_x0, ext_mb_y0, ext_mb_x1, ext_mb_y1 = _get_mediabox(ext_page_obj)

    ext_cb = _get_cropbox(ext_page_obj)
    if ext_cb is not None:
        cb_x0, cb_y0, cb_x1, cb_y1 = ext_cb
        crop_left, crop_bottom = cb_x0, cb_y0
        crop_w = cb_x1 - cb_x0
        crop_h = cb_y1 - cb_y0
    else:
        crop_left, crop_bottom = ext_mb_x0, ext_mb_y0
        crop_w = ext_mb_x1 - ext_mb_x0
        crop_h = ext_mb_y1 - ext_mb_y0

    # Page layout origin (lower-left of the main page within its MediaBox)
    main_mb_x0, main_mb_y0, _, _ = _get_mediabox(main_page_obj)
    left = main_mb_x0
    bottom = main_mb_y0

    # Scale so the visible region (crop_w × crop_h) fits inside main_w × main_h.
    # This directly mirrors CTS2.0 GetTransformMatrixFitMainPdfSize which uses
    # the CropBox (or MediaBox when CropBox absent) for the aspect-ratio test —
    # no secondary MediaBox-to-CropBox correction is needed or correct.
    if crop_w > 0 and crop_h > 0:
        src_ratio = crop_w / crop_h
        overlay_ratio = main_w / main_h if main_h > 0 else 1.0
        if overlay_ratio < src_ratio:
            scale = main_w / crop_w
        else:
            scale = main_h / crop_h
    else:
        scale = 1.0

    # Final matrix maps visible-region origin (crop_left, crop_bottom) to
    # main-page origin (left, bottom) with uniform scale.
    # M = T(left, bottom) · S(scale) · T(-crop_left, -crop_bottom)
    #   = [scale, 0, 0, scale, left - scale*crop_left, bottom - scale*crop_bottom]
    a = scale
    b = 0.0
    c = 0.0
    d = scale
    e = left - scale * crop_left
    f = bottom - scale * crop_bottom

    return [a, b, c, d, e, f]


def _merge_fit_main_size(
    main_page_obj,
    ext_page_obj,
    dst: pikepdf.Pdf,
    item: MergeItem,
    ext_src: pikepdf.Pdf,
    ext_page_index: int,
    ext_path: "Path | str" = "",
    main_src: "pikepdf.Pdf | None" = None,
    main_page_index: int = 0,
) -> None:
    """Mode A: scale external page to fit main page size, overlay main on top.

    ``ext_src`` and ``ext_page_index`` are required so that
    ``add_page_with_forms`` can carry AcroForm widget fields from the external
    PDF into *dst*.
    """
    # Step 1: copy external page into dst, preserving AcroForm fields.
    add_page_with_forms(dst, ext_src, ext_page_index, src_path=ext_path)
    new_page_obj = dst.pages[-1].obj

    # Step 2: set MediaBox = main page MediaBox
    rotation = int(ext_page_obj.get("/Rotate", 0))
    if rotation == 0:
        main_mb = main_page_obj.get("/MediaBox")
        if main_mb is not None:
            # Reconstruct as fresh Array to avoid foreign-object copy issues
            new_page_obj["/MediaBox"] = pikepdf.Array([float(v) for v in main_mb])

    # Step 3-4: compute transform matrix
    matrix = _compute_fit_main_matrix(main_page_obj, ext_page_obj)

    # Step 5-6: wrap existing content with transform, then add closing
    prepend_bytes = f"q {fmt_matrix(matrix)} cm q\n".encode()
    append_bytes = b"\nQ\nQ\n"
    prepend_content(new_page_obj, dst, prepend_bytes)
    append_content(new_page_obj, dst, append_bytes)

    # Step 7: overlay main page as Form XObject
    xobj = page_as_form_xobject(main_page_obj, dst)
    overlay = b"\nq 1 0 0 1 0 0 cm\nq\n/MainXobj0 Do\nQ\nQ\n"
    append_content(new_page_obj, dst, overlay)
    add_xobject_to_resources(new_page_obj, dst, "/MainXobj0", xobj)

    # Step 8 (Phase 2): migrate main-page tag tree into dst via XObject MCR dicts.
    if main_src is not None:
        try:
            migrate_tags_for_xobject(
                main_src, main_page_index, xobj, dst, new_page_obj,
            )
        except Exception as _exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "migrate_tags_for_xobject (Mode A) failed: %s", _exc
            )


# ---------------------------------------------------------------------------
# Top-level merge entry point
# ---------------------------------------------------------------------------

def merge_from_xml(
    merge_info: WorkspaceMergeInfo,
    main_pdf_path: Path,
    inputs_dir: Path,
    output_path: Path,
) -> None:
    """Merge PDFs according to a WorkspaceMergeInfo, porting CTS2.0 MergeNew.

    Iterates main PDF pages (slide_index = 1..N):
    - Hidden slides (SlideInfo.hidden=true) are skipped.
    - Slides with a MergeItem → Mode A or B compositing.
    - Slides without a MergeItem → copy main page as-is.

    External PDFs are opened once per unique file path and kept open for the
    duration of the merge loop, then closed together via contextlib.ExitStack.
    This avoids re-opening the same blob file for every slide that references
    it (a common pattern when one external PDF spans many slides).
    """
    import contextlib

    # Build hidden-slide set
    hidden_slides: set[int] = {
        si.slide_index for si in merge_info.slide_infos if si.hidden
    }

    # Build slide_index → (MergeItem, SlideLocalId) map
    slide_map: dict[int, tuple[MergeItem, SlideLocalId]] = {}
    for item in merge_info.merge_items:
        for slide in item.slides:
            slide_map[slide.slide_index] = (item, slide)

    with pikepdf.open(main_pdf_path) as main_pdf:
        n_main = len(main_pdf.pages)

        with pikepdf.Pdf.new() as dst, contextlib.ExitStack() as ext_stack:
            # Propagate PDF/UA metadata from main PDF (SetPDFUATag equivalent)
            set_pdfua_metadata(main_pdf, dst)
            # Cache of path → open pikepdf.Pdf, opened lazily on first use.
            # ExitStack closes all of them when the `with` block exits.
            _open_ext: dict[Path, pikepdf.Pdf] = {}

            def _get_ext_pdf(path: Path) -> pikepdf.Pdf:
                if path not in _open_ext:
                    _open_ext[path] = ext_stack.enter_context(pikepdf.open(path))
                return _open_ext[path]

            for slide_idx in range(1, n_main + 1):
                if slide_idx in hidden_slides:
                    continue

                main_page = main_pdf.pages[slide_idx - 1]

                if slide_idx in slide_map:
                    item, slide_local_id = slide_map[slide_idx]
                    ext_path, ext_page_num = resolve_external_page(
                        item, slide_local_id, inputs_dir
                    )

                    ext_pdf = _get_ext_pdf(ext_path)
                    if ext_page_num < 1 or ext_page_num > len(ext_pdf.pages):
                        raise ValueError(
                            f"External page {ext_page_num} out of range "
                            f"in {ext_path} (has {len(ext_pdf.pages)} pages)"
                        )
                    ext_page = ext_pdf.pages[ext_page_num - 1]
                    # ext_page_num is 1-based; add_page_with_forms needs 0-based
                    ext_page_index = ext_page_num - 1

                    main_page_index = slide_idx - 1
                    if item.fit_pdf_size:
                        _merge_fit_pdf_size(
                            main_page.obj, ext_page.obj, dst, item,
                            ext_src=ext_pdf,
                            ext_page_index=ext_page_index,
                            ext_path=ext_path,
                            main_src=main_pdf,
                            main_page_index=main_page_index,
                        )
                    else:
                        _merge_fit_main_size(
                            main_page.obj, ext_page.obj, dst, item,
                            ext_src=ext_pdf,
                            ext_page_index=ext_page_index,
                            ext_path=ext_path,
                            main_src=main_pdf,
                            main_page_index=main_page_index,
                        )
                else:
                    # Plain copy: AcroForm fields + tag tree from main PDF.
                    add_page_with_forms(
                        dst, main_pdf, slide_idx - 1, src_path=main_pdf_path
                    )
                    # Tag-tree merge: main_pdf still open → copy_foreign safe
                    merge_page_tags(
                        main_pdf, dst, dst.pages[-1].obj,
                        src_page_index=slide_idx - 1,
                        src_path=main_pdf_path,
                    )

            dst.docinfo["/Producer"] = "pdfmergepy (pikepdf/QPDF)"

            # Matterhorn 28-001: wrap any Link annotations still without
            # /StructParent inside <Link> struct elements with OBJR references.
            fix_link_annots(dst)

            # Post-merge repairs (TH /Scope, etc.)
            apply_post_merge_repairs(dst)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            dst.save(output_path, min_version="1.7")
