"""Post-merge repair passes applied to the destination PDF before saving.

Mirrors the repair passes that CTS2.0 / itext7 runs after merging:
  - XMP pdfuaid:part (Matterhorn 06-001): write PDF/UA-1 conformance identifier.
  - TH /Scope        (Matterhorn 14-003): add /Scope attribute to <TH> cells.

The TH /Scope repair is delegated to ``pdftagvalicate``'s repair module when
available; if ``pdftagvalicate`` is not installed it is silently skipped.
The XMP repair uses only pikepdf (always available).
"""

from __future__ import annotations

import logging

import pikepdf

log = logging.getLogger(__name__)


def apply_post_merge_repairs(dst: pikepdf.Pdf, src: "pikepdf.Pdf | None" = None) -> None:
    """Run all post-merge repair passes on *dst* (in-place, before save).

    Parameters
    ----------
    dst:
        The destination PDF being assembled (open, writable).
    src:
        The primary source PDF (the main/template PDF), used to copy title,
        language, and CreateDate into the XMP stream.  ``None`` is accepted
        gracefully (XMP will still be written with pdfuaid:part=1 and
        current timestamp).

    Currently applies:
    1. **XMP pdfuaid:part** (06-001): write ``pdfuaid:part = '1'`` plus
       dc:title, dc:language, xmp timestamps, and pdf:Producer.
    2. **TH /Scope** (14-003): add ``/Scope /Column`` or ``/Row`` to ``<TH>``
       cells missing one.
    """
    _repair_xmp(dst, src)
    _repair_th_scope(dst)


def _repair_xmp(dst: pikepdf.Pdf, src: "pikepdf.Pdf | None") -> None:
    """Write XMP /Metadata stream with pdfuaid:part=1 (Matterhorn 06-001)."""
    try:
        from pdfmergepy.tagtree import write_xmp_metadata
        # write_xmp_metadata needs a src to copy title/lang/dates from.
        # If src is None, pass dst itself (reads empty values gracefully).
        write_xmp_metadata(src if src is not None else dst, dst)
    except Exception as exc:
        log.warning("post-merge XMP repair failed: %s", exc)


def _repair_th_scope(dst: pikepdf.Pdf) -> None:
    """Add /Scope to <TH> cells missing one (Matterhorn 14-003)."""
    try:
        from pdftagvalicate.th_scope_repair import fix as _fix_th_scope
        result = _fix_th_scope(dst)
        if result.fixed:
            log.debug("post-merge repair: added /Scope to %d <TH> cell(s)", result.fixed)
    except ImportError:
        log.debug("pdftagvalicate not installed — skipping TH /Scope repair")
    except Exception as exc:
        log.warning("post-merge TH /Scope repair failed: %s", exc)
