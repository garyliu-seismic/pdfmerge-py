"""AcroForm-aware page copy helpers.

Provides a thin wrapper around pikepdf's ``Pdf.add_pages_from()`` (introduced
in pikepdf 8.x) that replicates the behaviour of iText7's
``PdfPageFormCopier`` as used in CTS2.0's ``CustomMerger.Merge()``:

  * Copies pages AND their associated AcroForm widget fields into the
    destination PDF.
  * Handles field-name conflicts by renaming duplicates (``name`` → ``name+1``
    etc.), exactly as iText7's ``PdfPageFormCopier`` does.
  * Merges ``/AcroForm/DR`` (Default Resources — fonts, XObjects used for
    field appearance rendering) so field appearances can be re-generated.

Scope note
----------
CTS2.0's ``CustomMerger`` only applies ``PdfPageFormCopier`` on the *plain
copy* path (pages that are directly merged without Form-XObject compositing).
For the composite paths (``MergePDFToUseExternalPageSize`` /
``MergePDFToFitMainPdfSize``) the ``CopyAnnotations`` calls are commented out
in the C# source, so widget fields from *external* PDFs are NOT carried over
in iText7 either.  This module deliberately mirrors that limitation: composite
pages use ``add_pages_from`` for the external PDF only (so its AcroForm fields
come through), while the main-PDF overlay is added as a Form XObject (which
carries no AcroForm entries).

Reference
---------
iText7 open-source (AGPL): ``iText.Forms.PdfPageFormCopier``
pikepdf API: ``Pdf.add_pages_from(src, pages=..., forms='preserve')``
    returns ``PageCopyResult(fields_added, renamed_fields, …)``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import pikepdf

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PageCopyResult:
    """Summary of one add_pages_from call — mirrors pikepdf's own namedtuple
    but adds a ``src_path`` field for logging."""

    def __init__(self, raw: pikepdf.PageCopyResult, src_path: Path | str = "") -> None:
        self._raw = raw
        self.src_path = str(src_path)
        self.pages_added: int = raw.pages_added
        self.fields_added: int = raw.fields_added
        self.renamed_fields: dict[str, str] = dict(raw.renamed_fields)
        self.partial_fields: list = list(raw.partial_fields)

    def __repr__(self) -> str:
        return (
            f"PageCopyResult(src={self.src_path!r}, pages={self.pages_added}, "
            f"fields={self.fields_added}, renamed={self.renamed_fields})"
        )

    def log_if_notable(self) -> None:
        """Emit log warnings for any field renames or partial fields."""
        if self.renamed_fields:
            log.warning(
                "AcroForm field name conflicts in %s — renamed: %s",
                self.src_path,
                self.renamed_fields,
            )
        if self.partial_fields:
            log.warning(
                "AcroForm partial fields (not fully copied) in %s: %s",
                self.src_path,
                self.partial_fields,
            )


def add_page_with_forms(
    dst: pikepdf.Pdf,
    src: pikepdf.Pdf,
    page_index: int,
    src_path: Path | str = "",
) -> PageCopyResult:
    """Copy page ``page_index`` (0-based) from *src* into *dst*, preserving
    AcroForm widget fields and merging /AcroForm/DR.

    This is a direct replacement for::

        dst.pages.append(pikepdf.Page(src.pages[page_index].obj))

    …when the source page may carry fillable-form fields.  It delegates to
    ``pikepdf.Pdf.add_pages_from(src, pages=[page_index], forms='preserve')``,
    which internally replicates the iText7 ``PdfPageFormCopier`` logic:

    1. Deep-copies the page's Widget annotations into dst.
    2. Registers each widget's parent field in dst's ``/AcroForm/Fields``.
    3. Renames conflicting field names (``T`` key) using a ``+N`` suffix.
    4. Merges ``/AcroForm/DR`` font/resource sub-dictionaries.

    Parameters
    ----------
    dst:
        Destination PDF (open, writable).
    src:
        Source PDF (open, readable).  Must remain open for the duration of
        this call.
    page_index:
        0-based page index in *src*.
    src_path:
        Optional path string used only for log messages.

    Returns
    -------
    PageCopyResult
        Summary including ``fields_added`` and ``renamed_fields``.
    """
    raw = dst.add_pages_from(src, pages=[page_index], forms="preserve")
    result = PageCopyResult(raw, src_path=src_path)
    result.log_if_notable()
    return result


def add_pages_range_with_forms(
    dst: pikepdf.Pdf,
    src: pikepdf.Pdf,
    page_indices: Sequence[int],
    src_path: Path | str = "",
) -> PageCopyResult:
    """Copy multiple pages (0-based indices) from *src* into *dst*,
    preserving AcroForm fields.

    Convenience wrapper for multi-page plain-copy merges (the ``merge_files``
    path in ``merge.py``).
    """
    raw = dst.add_pages_from(src, pages=list(page_indices), forms="preserve")
    result = PageCopyResult(raw, src_path=src_path)
    result.log_if_notable()
    return result
