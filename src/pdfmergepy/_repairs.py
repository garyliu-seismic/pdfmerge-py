"""Post-merge repair passes applied to the destination PDF before saving.

Mirrors the repair passes that CTS2.0 / itext7 runs after merging:
  - TH /Scope  (Matterhorn 14-003): add /Scope attribute to <TH> cells missing one.

These are delegated to ``pdftagvalicate``'s repair modules when available.
If ``pdftagvalicate`` is not installed, the repairs are silently skipped
(the merge still succeeds, just without the repairs).
"""

from __future__ import annotations

import logging

import pikepdf

log = logging.getLogger(__name__)


def apply_post_merge_repairs(dst: pikepdf.Pdf) -> None:
    """Run all post-merge repair passes on *dst* (in-place, before save).

    Currently applies:
    - **TH /Scope** (14-003): adds ``/Scope /Column`` or ``/Row`` to ``<TH>``
      cells that have no ``/Scope`` attribute, matching itext7's auto-repair.

    Requires ``pdftagvalicate`` to be installed.  If it is absent the function
    returns silently — the merge output is still written, just without repairs.
    """
    _repair_th_scope(dst)


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
