"""PDF tag-tree (StructTree) merge helpers — plain-copy + XObject migration (Phase 1+2).

Ports the CTS2.0 ``PdfMergeTagHelper`` logic for the *plain-copy* path
(``CustomMerger.Merge`` → ``CopyPagesTo``) into pikepdf object-model
operations.

What this module does
---------------------
When merging a tagged source PDF into a destination PDF we must:

1. **Detect** whether the source is tagged (``/StructTreeRoot`` present).
2. **Initialise** the destination's ``/StructTreeRoot`` and ``/MarkInfo`` if
   they don't exist yet.
3. **Merge the RoleMap** — copy any custom role names from source to dest
   without overwriting existing dest roles (mirrors
   ``PdfMergeTagHelper.MergeRoleMap``).
4. **Get-or-create a single ``<Document>`` element** at the root level —
   PDF/UA-1 requires exactly one ``<Document>`` directly under the
   ``StructTreeRoot``; all per-source slide containers live as its kids
   (mirrors ``WrapNewKidsUnderSect``).
5. **Deep-copy** each top-level kid of the source's ``<Document>`` element
   (typically ``<Slide>`` or ``<Sect>`` containers) into the destination
   using ``pikepdf.Pdf.copy_foreign()``, then **fix up** every ``/Pg``
   reference in the copied subtree to point to the correct destination page
   (``copy_foreign`` drops foreign page references because they can't be
   resolved in the target document).
6. **Update the ``/ParentTree``** — allocate a fresh slot number for the
   new destination page, assign ``/StructParents`` on the page dict, and
   write the slot's entry (array of owning struct elements) into the dest
   ``/ParentTree`` number tree.
7. **Propagate PDF/UA metadata** (``/Lang``, ``/Title``,
   ``/ViewerPreferences``) from the main source PDF on first call (mirrors
   ``PdfMerger.SetPDFUATag``).

What this module also does (Phase 2)
-------------------------------------
- ``migrate_tags_for_xobject``: migrate the tag tree of a source page that has
  been embedded as a Form XObject into the destination document.  Ports
  ``PdfMergeTagHelper.MigrateTagsForXObject`` (line 886).
- ``fix_link_annots``: wrap orphaned Link annotations in ``<Link>`` struct
  elements with OBJR back-references (Matterhorn 28-001).

What this module does NOT do (Phase 3, deferred)
-------------------------------------------------
- ``FixInvalidTBodies``, ``SanitizeDocumentK``, ``AlterChartAsFigure``:
  post-merge repair passes used by CTS2.0 to clean up PAC validation errors.
  Deferred pending PAC/pdftagvalicate feedback from real merge runs.

References
----------
CTS2.0 source:
  ``src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/PdfMergeTagHelper.cs``
  ``src/CTS/Seismic.CTS.Implements.LiveDocs/IText7/PdfMerger.cs``
    – ``CustomMerger.Merge()``
    – ``PdfMerger.SetPDFUATag()``
    – ``PdfMerger.MergeNew()`` (main loop)
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

import pikepdf

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def merge_page_tags(
    src: pikepdf.Pdf,
    dst: pikepdf.Pdf,
    dst_page_obj: pikepdf.Object,
    *,
    src_page_index: int = 0,
    src_path: "Path | str" = "",
) -> bool:
    """Merge the tag-tree contribution of one source page into *dst*.

    Must be called **after** the source page has already been appended to
    *dst* (via ``add_pages_from`` or equivalent), so that *dst_page_obj* is
    already a valid indirect object in *dst*.

    Parameters
    ----------
    src:
        Open source PDF.  Must remain open for the duration of this call.
    dst:
        Destination PDF (open, writable).
    dst_page_obj:
        The page dictionary in *dst* that corresponds to the copied source
        page (i.e. ``dst.pages[-1].obj`` immediately after the copy).
    src_page_index:
        0-based index of the source page being copied.  Used to filter which
        Slide-level struct elements belong to this specific page (a multi-page
        source PDF has one Slide per page in its Document.K array).
    src_path:
        Optional path string used only for log messages.

    Returns
    -------
    bool
        ``True`` if the source was tagged and migration was performed,
        ``False`` if the source had no ``/StructTreeRoot`` (no-op).
    """
    if not _is_tagged(src):
        return False

    src_root = src.Root["/StructTreeRoot"]
    dst_root = _ensure_dst_struct_root(dst)

    # 1. Merge /RoleMap
    _merge_rolemap(src_root, dst_root)

    # 2. Ensure single <Document> kid under dst root
    dst_doc = _get_or_create_document(dst, dst_root)

    # 3. Allocate a ParentTree slot for this page (sets /StructParents on page)
    slot = _alloc_parent_tree_slot(dst_root, dst_page_obj)

    # 4. Migrate MCR wiring via /ParentTree lookup + ancestor-chain mirroring.
    #    This is the plain-copy counterpart of migrate_tags_for_xobject:
    #    - Reads src ParentTree[src_slot] to find MCID -> StructElem mapping.
    #    - Mirrors each ancestor chain into dst (creating fresh StructElems
    #      with /Pg = dst_page_obj), avoiding duplicates via parent_map.
    #    - Appends plain MCR dicts {/Type/MCR, /Pg, /MCID} to each dst elem.
    #    - Writes the dense dst ParentTree array at the allocated slot.
    src_page_obj = src.pages[src_page_index].obj if src_page_index < len(src.pages) else None
    migrated = _migrate_mcrs_for_page(
        src_root, src_page_obj, dst_root, dst, dst_page_obj, dst_doc, slot,
        src_path=src_path,
    )

    if not migrated:
        log.debug("tagtree: source %s page %d had no MCRs to migrate", src_path, src_page_index)
        return False

    log.debug(
        "tagtree: migrated MCRs at slot %d from %s page %d",
        slot, src_path, src_page_index,
    )
    return True


def set_pdfua_metadata(src: pikepdf.Pdf, dst: pikepdf.Pdf) -> None:
    """Propagate PDF/UA metadata from *src* to *dst* (once, on first call).

    Mirrors ``PdfMerger.SetPDFUATag()``:
    - Marks dst as tagged.
    - Copies ``/Lang`` from src Catalog.
    - Copies ``/Title`` from src document info.
    - Sets ``/ViewerPreferences /DisplayDocTitle true``.
    """
    if _is_tagged(dst):
        return  # already initialised
    _ensure_dst_struct_root(dst)

    try:
        src_lang = src.Root.get("/Lang")
        if src_lang is not None:
            # /Lang is a string — copy its value as a fresh pikepdf.String
            dst.Root["/Lang"] = pikepdf.String(str(src_lang))

        title = src.docinfo.get("/Title")
        if title:
            dst.docinfo["/Title"] = pikepdf.String(str(title))

        vp = dst.Root.get("/ViewerPreferences")
        if vp is None:
            vp = pikepdf.Dictionary()
            dst.Root["/ViewerPreferences"] = vp
        vp["/DisplayDocTitle"] = pikepdf.Boolean(True)

    except Exception as exc:
        log.warning("tagtree: set_pdfua_metadata failed: %s", exc)


_PDFUAID_NS = "http://www.aiim.org/pdfua/ns/id/"
_DC_NS      = "http://purl.org/dc/elements/1.1/"
_XMP_NS     = "http://ns.adobe.com/xap/1.0/"
_PDF_NS     = "http://ns.adobe.com/pdf/1.3/"

PRODUCER    = "pdfmergepy (pikepdf/QPDF)"


def write_xmp_metadata(src: pikepdf.Pdf, dst: pikepdf.Pdf) -> None:
    """Write a conforming XMP /Metadata stream to *dst*, including the
    ``pdfuaid:part = '1'`` declaration required by Matterhorn 06-001 / PDF/UA-1.

    Mirrors the metadata that itext7 writes via ``SetPDFUATag`` +
    ``PdfDocumentInfo``, but adds the ``pdfuaid`` namespace block that itext7
    omits by default (causing its own 06-001 failure).

    Fields written
    --------------
    - ``pdfuaid:part``  = ``'1'``       (PDF/UA-1 conformance identifier)
    - ``dc:title``      from *src* docinfo ``/Title`` (if present)
    - ``dc:language``   from *src* Root ``/Lang``     (if present)
    - ``dc:format``     = ``'application/pdf'``
    - ``xmp:CreateDate`` preserved from *src* XMP    (if present)
    - ``xmp:ModifyDate`` = UTC now
    - ``pdf:Producer``  = ``'pdfmergepy (pikepdf/QPDF)'``

    Safe to call when *dst* has no prior ``/Metadata`` stream; pikepdf creates
    one automatically via ``open_metadata()``.
    """
    # Collect values from source
    src_title: str | None = None
    src_lang:  str | None = None
    src_create_date: str | None = None
    try:
        title_val = src.docinfo.get("/Title")
        if title_val:
            src_title = str(title_val)
        lang_val = src.Root.get("/Lang")
        if lang_val:
            src_lang = str(lang_val)
        with src.open_metadata(set_pikepdf_as_editor=False) as src_meta:
            src_create_date = src_meta.get("xmp:CreateDate")
    except Exception as exc:
        log.debug("write_xmp_metadata: could not read src metadata: %s", exc)

    now_iso = (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )

    try:
        with dst.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta.register_xml_namespace(_PDFUAID_NS, "pdfuaid")
            meta.register_xml_namespace(_DC_NS, "dc")
            meta.register_xml_namespace(_XMP_NS, "xmp")
            meta.register_xml_namespace(_PDF_NS, "pdf")

            # PDF/UA-1 conformance identifier  (Matterhorn 06-001)
            meta["pdfuaid:part"] = "1"

            # Document format
            meta["dc:format"] = "application/pdf"

            # Title
            if src_title:
                meta["dc:title"] = src_title

            # Language (dc:language is an unordered Bag — pass as list)
            if src_lang:
                meta["dc:language"] = [src_lang]

            # Timestamps
            if src_create_date:
                meta["xmp:CreateDate"] = src_create_date
            meta["xmp:ModifyDate"] = now_iso

            # Producer
            meta["pdf:Producer"] = PRODUCER

        log.debug("write_xmp_metadata: wrote pdfuaid:part=1 to dst")
    except Exception as exc:
        log.warning("write_xmp_metadata failed: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_tagged(pdf: pikepdf.Pdf) -> bool:
    return "/StructTreeRoot" in pdf.Root


def _ensure_dst_struct_root(dst: pikepdf.Pdf) -> pikepdf.Object:
    """Initialise /StructTreeRoot and /MarkInfo in dst if absent."""
    if "/StructTreeRoot" not in dst.Root:
        sr = dst.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/StructTreeRoot"),
        ))
        sr["/RoleMap"] = pikepdf.Dictionary()
        sr["/ParentTreeNextKey"] = pikepdf.Integer(0)
        pt = pikepdf.Dictionary()
        pt["/Nums"] = pikepdf.Array()
        sr["/ParentTree"] = pt
        dst.Root["/StructTreeRoot"] = sr

        mi = pikepdf.Dictionary()
        mi["/Marked"] = pikepdf.Boolean(True)
        dst.Root["/MarkInfo"] = mi

    return dst.Root["/StructTreeRoot"]


def _merge_rolemap(src_root: pikepdf.Object, dst_root: pikepdf.Object) -> None:
    """Copy /RoleMap entries from src to dst, preserving existing dst entries.

    Mirrors ``PdfMergeTagHelper.MergeRoleMap()``.
    """
    src_rm = src_root.get("/RoleMap")
    if not src_rm:
        return
    dst_rm = dst_root.get("/RoleMap")
    if dst_rm is None:
        dst_root["/RoleMap"] = pikepdf.Dictionary()
        dst_rm = dst_root["/RoleMap"]
    for key in src_rm.keys():
        if key not in dst_rm:
            try:
                dst_rm[key] = src_rm[key]
            except Exception:
                pass  # skip uncopiable entries


def _find_document_elem(struct_root: pikepdf.Object) -> Optional[pikepdf.Object]:
    """Return the <Document> struct element that is a direct kid of the root."""
    k = struct_root.get("/K")
    if k is None:
        return None
    kids = list(k) if isinstance(k, pikepdf.Array) else [k]
    for kid in kids:
        if hasattr(kid, "get") and kid.get("/S") == pikepdf.Name("/Document"):
            return kid
    return None


def _get_or_create_document(dst: pikepdf.Pdf, dst_root: pikepdf.Object) -> pikepdf.Object:
    """Return the single <Document> kid of dst_root, creating it if absent.

    PDF/UA-1 §7.1: the structure tree must have exactly one <Document>
    element directly under the StructTreeRoot.  All merged slide containers
    live as kids of this element.

    Mirrors the intent of ``WrapNewKidsUnderSect`` in CTS2.0 which ensures
    every merged source lands under one persistent <Document>.
    """
    existing = _find_document_elem(dst_root)
    if existing is not None:
        return existing

    doc = dst.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"),
        S=pikepdf.Name("/Document"),
        P=dst_root,
    ))
    doc["/K"] = pikepdf.Array()

    k = dst_root.get("/K")
    if k is None:
        dst_root["/K"] = pikepdf.Array([doc])
    elif isinstance(k, pikepdf.Array):
        k.append(doc)
    else:
        dst_root["/K"] = pikepdf.Array([k, doc])

    return doc


def _alloc_parent_tree_slot(dst_root: pikepdf.Object, dst_page_obj: pikepdf.Object) -> int:
    """Allocate the next /ParentTree slot number and assign it to the page.

    Sets ``/StructParents`` on *dst_page_obj* and increments
    ``/ParentTreeNextKey`` in *dst_root*.

    Returns the allocated slot number (0-based).
    """
    slot = int(dst_root.get("/ParentTreeNextKey", 0))
    dst_root["/ParentTreeNextKey"] = pikepdf.Integer(slot + 1)
    dst_page_obj["/StructParents"] = pikepdf.Integer(slot)
    return slot


def _refs_page(elem: pikepdf.Object, src_page_obj: pikepdf.Object) -> bool:
    """Return True if *elem* (a StructElem dict) references *src_page_obj* via /Pg.

    Comparison is by object-generation number pair (objgen) so it is stable
    across indirect-reference resolution.
    """
    pg = elem.get("/Pg")
    if pg is None:
        return False
    try:
        return pg.objgen == src_page_obj.objgen
    except Exception:
        return False


def _copy_slide_kids(
    src_doc: pikepdf.Object,
    dst_doc: pikepdf.Object,
    dst: pikepdf.Pdf,
    dst_page_obj: pikepdf.Object,
    *,
    filter_src_page: Optional[pikepdf.Object] = None,
) -> list[pikepdf.Object]:
    """Deep-copy kids of *src_doc* that belong to *filter_src_page* into *dst_doc*.

    When *filter_src_page* is not ``None``, only kids whose ``/Pg`` matches
    that page dict are copied.  This prevents a multi-page source PDF from
    injecting all its Slide elements on every single page merge call.

    After ``copy_foreign``:
    - Sets ``/P`` on each copied kid to *dst_doc*.
    - Recursively updates all ``/Pg`` references in the copied subtree to
      *dst_page_obj*.  (``copy_foreign`` cannot resolve foreign page
      references, so it drops them; we must restore them manually.)

    Returns the list of newly copied top-level kids.
    """
    src_kids_raw = src_doc.get("/K")
    if src_kids_raw is None:
        return []
    src_kids = list(src_kids_raw) if isinstance(src_kids_raw, pikepdf.Array) else [src_kids_raw]

    copied: list[pikepdf.Object] = []
    for src_kid in src_kids:
        # Filter: only copy kids that reference the requested source page
        if filter_src_page is not None and not _refs_page(src_kid, filter_src_page):
            continue
        try:
            c = dst.copy_foreign(src_kid)
        except Exception as exc:
            log.warning("tagtree: copy_foreign failed for a slide kid: %s", exc)
            continue
        c["/P"] = dst_doc
        _update_pg_recursive(c, dst_page_obj)
        dst_doc["/K"].append(c)
        copied.append(c)

    return copied


# StructElem roles that are page-spanning (no /Pg) vs page-specific (need /Pg).
# PDF spec §14.7.2: /Pg is required on StructElems that reference a specific page,
# but optional on container elements like <Document> that span the whole file.
_DOCUMENT_LEVEL_ROLES = frozenset([
    pikepdf.Name("/Document"),
    pikepdf.Name("/Part"),
])


def _update_pg_recursive(elem: pikepdf.Object, dst_page_obj: pikepdf.Object) -> None:
    """Recursively set ``/Pg`` on every page-specific StructElem and MCR.

    ``copy_foreign`` drops indirect references that come from a foreign
    document (the source PDF's page dict is such a reference), so ``/Pg``
    values are ``None`` after copying.  We restore them here by walking the
    entire subtree.

    Document-level container elements (``<Document>``, ``<Part>``) are
    intentionally skipped: they span the entire document and must not have
    a ``/Pg`` pointing to a single page.
    """
    if not hasattr(elem, "get"):
        return

    role = elem.get("/S")
    is_doc_level = role in _DOCUMENT_LEVEL_ROLES

    # Set /Pg on page-specific StructElems and MCR dicts.
    if not is_doc_level:
        if role is not None or elem.get("/Type") == pikepdf.Name("/MCR"):
            elem["/Pg"] = dst_page_obj

    k = elem.get("/K")
    if k is None:
        return
    if isinstance(k, pikepdf.Array):
        for child in k:
            _update_pg_recursive(child, dst_page_obj)
    elif isinstance(k, pikepdf.Dictionary):
        # Inline MCR dict (not indirect)
        if k.get("/Type") == pikepdf.Name("/MCR"):
            k["/Pg"] = dst_page_obj
    # Stream MCR or ObjRef — not currently handled (rare in plain-copy path)


def _collect_leaf_elems(elems: list[pikepdf.Object]) -> list[pikepdf.Object]:
    """Return all leaf StructElems (those whose /K is an MCR dict or absent).

    These are the elements that own the page's marked-content sequences and
    must be listed in the /ParentTree slot for the page.

    Mirrors the logic in ``ParentTreeHandler.CreateParentTreeEntryForPage``
    in iText7: for each MCR on the page, the parent struct element is the
    entry.
    """
    result: list[pikepdf.Object] = []

    def _walk(elem: pikepdf.Object) -> None:
        if not hasattr(elem, "get"):
            return
        k = elem.get("/K")
        if k is None:
            # Leaf with no kids — treat as a leaf
            result.append(elem)
            return
        if isinstance(k, pikepdf.Dictionary):
            # Inline MCR dict or single child dict
            if k.get("/Type") == pikepdf.Name("/MCR"):
                result.append(elem)
                return
            # Non-MCR inline dict — recurse
            _walk(k)
            return
        if isinstance(k, pikepdf.Array):
            all_mcr = all(
                isinstance(child, pikepdf.Dictionary)
                and child.get("/Type") == pikepdf.Name("/MCR")
                for child in k
                if hasattr(child, "get")
            )
            if all_mcr and len(list(k)) > 0:
                result.append(elem)
                return
            for child in k:
                _walk(child)

    for e in elems:
        _walk(e)
    return result


def _write_parent_tree_slot(
    dst_root: pikepdf.Object,
    slot: int,
    leaf_elems: list[pikepdf.Object],
) -> None:
    """Append ``slot → [leaf_elems]`` to the /ParentTree number tree.

    The /ParentTree is a PDF number tree (ISO 32000 §7.9.7).  For the
    flat (non-intermediate-node) variant we store it as a direct ``/Nums``
    array: ``[key0, value0, key1, value1, ...]``.
    """
    pt = dst_root.get("/ParentTree")
    if pt is None:
        pt = pikepdf.Dictionary()
        pt["/Nums"] = pikepdf.Array()
        dst_root["/ParentTree"] = pt

    nums = pt.get("/Nums")
    if nums is None:
        pt["/Nums"] = pikepdf.Array()
        nums = pt["/Nums"]

    nums.append(pikepdf.Integer(slot))
    nums.append(pikepdf.Array(leaf_elems) if leaf_elems else pikepdf.Array())


# ---------------------------------------------------------------------------
# Plain-copy MCR wiring (companion to migrate_tags_for_xobject for plain pages)
# ---------------------------------------------------------------------------

def _migrate_mcrs_for_page(
    src_root: pikepdf.Object,
    src_page_obj: "pikepdf.Object | None",
    dst_root: pikepdf.Object,
    dst: pikepdf.Pdf,
    dst_page_obj: pikepdf.Object,
    dest_attach: pikepdf.Object,
    dst_slot: int,
    *,
    src_path: "Path | str" = "",
) -> bool:
    """Wire MCR dicts for a plain-copied page into the destination struct tree.

    Mirrors the MCR-migration logic of :func:`migrate_tags_for_xobject` but
    for the **plain-copy path** where content lives directly on the page
    (no Form XObject):

    1. Read ``src /ParentTree[src_slot]`` to get the dense array of
       StructElems indexed by MCID for this page.
    2. For each StructElem, mirror its ancestor chain into *dst* via
       :func:`_ensure_ancestor_chain` (creating fresh dst elements with
       ``/Pg = dst_page_obj``).
    3. Append a plain MCR dict ``{/Type /MCR, /Pg dst_page, /MCID n}`` to
       each mirrored dst StructElem's ``/K`` array.
    4. Write the dense dst ParentTree array at *dst_slot*.

    Returns ``True`` if at least one MCR was wired.
    """
    if src_page_obj is None:
        return False

    # Collect MCID -> src StructElem from src ParentTree
    mcid_to_src_elem: dict[int, pikepdf.Object] = {}
    _collect_page_mcrs(src_root, src_page_obj, mcid_to_src_elem)
    if not mcid_to_src_elem:
        return False

    max_mcid = max(mcid_to_src_elem.keys())

    # Mirror ancestor chains
    parent_map: dict[int, pikepdf.Object] = {}  # src objgen -> dst StructElem
    mcid_to_dst_elem: dict[int, pikepdf.Object] = {}
    for mcid, src_elem in mcid_to_src_elem.items():
        try:
            dst_elem = _ensure_ancestor_chain(
                src_elem, src_root, dst_root, dst, dst_page_obj,
                dest_attach, parent_map,
            )
        except Exception as exc:
            log.warning("_migrate_mcrs_for_page: ancestor chain failed mcid=%d: %s", mcid, exc)
            continue
        if dst_elem is not None:
            mcid_to_dst_elem[mcid] = dst_elem

    if not mcid_to_dst_elem:
        return False

    # Build dense ParentTree array
    _PDF_NULL = pikepdf.Object.parse(b"null")
    arr = pikepdf.Array()
    for i in range(max_mcid + 1):
        elem = mcid_to_dst_elem.get(i)
        arr.append(elem if elem is not None else _PDF_NULL)

    # Write to /ParentTree at the pre-allocated slot
    _write_xobject_parent_table(dst_root, dst_slot, arr)

    # Append plain MCR dicts to dst StructElems.
    # Make each MCR dict an indirect object so pikepdf's walk_struct_tree
    # can use its stable objgen (instead of id()) for cycle detection.
    for mcid, dst_elem in mcid_to_dst_elem.items():
        mcr = dst.make_indirect(pikepdf.Dictionary())
        mcr["/Type"] = pikepdf.Name("/MCR")
        mcr["/Pg"] = dst_page_obj
        mcr["/MCID"] = pikepdf.Integer(mcid)
        _append_kid(dst_elem, mcr)

    log.debug(
        "_migrate_mcrs_for_page: slot=%d mcrs=%d src=%s",
        dst_slot, len(mcid_to_dst_elem), src_path,
    )
    return True


# ---------------------------------------------------------------------------
# Phase 2: XObject tag-tree migration (MigrateTagsForXObject port)
# ---------------------------------------------------------------------------

import re as _re


def migrate_tags_for_xobject(
    src: pikepdf.Pdf,
    src_page_index: int,
    xobj_stream: pikepdf.Object,
    dst: pikepdf.Pdf,
    dst_page_obj: pikepdf.Object,
    *,
    src_path: "Path | str" = "",
) -> bool:
    """Migrate the tag tree of a source page that has been embedded as a
    Form XObject into *dst*.

    Ports ``PdfMergeTagHelper.MigrateTagsForXObject`` (line 886).

    Must be called **after** ``page_as_form_xobject`` has returned *xobj_stream*
    and **after** *xobj_stream* has been registered in the destination page's
    ``/Resources/XObject`` dict so it is an indirect object.

    Steps
    -----
    1. Walk the source ``/ParentTree`` to collect MCID → source StructElem pairs
       for *src_page_index*.
    2. Scan the source page content streams with a regex for ``/MCID n`` tokens
       (also scans the XObject stream bytes as fallback).
    3. Build a dense parent-array (length = maxMCID+1, ``pikepdf.Null`` for gaps)
       and write it into the destination ``/ParentTree`` at a fresh slot, then
       set ``/StructParents`` on *xobj_stream*.
    4. For every *real* MCID (from Step 1), mirror the source ancestor chain into
       the destination struct tree (creating fresh StructElem copies), then append
       an MCR dict ``{/Type/MCR, /Pg dst_page, /Stm xobj, /MCID n}`` to the
       mirrored parent element.

    Returns
    -------
    bool
        ``True`` if at least one MCR was migrated; ``False`` when the source is
        not tagged or has no MCRs for this page (caller should treat the XObject
        content as artifact).
    """
    if not _is_tagged(src):
        return False

    src_root = src.Root.get("/StructTreeRoot")
    if src_root is None:
        return False

    dst_root = _ensure_dst_struct_root(dst)
    _merge_rolemap(src_root, dst_root)

    # ── Step 1: source MCR walk ──────────────────────────────────────────────
    # Build mcid → src StructElem dict for the requested page.
    src_page_obj = src.pages[src_page_index].obj if src_page_index < len(src.pages) else None
    mcid_to_src_elem: dict[int, pikepdf.Object] = {}
    if src_page_obj is not None:
        _collect_page_mcrs(src_root, src_page_obj, mcid_to_src_elem)

    log.debug(
        "migrate_tags_for_xobject Step1: src=%s page=%d mcrs=%d",
        src_path, src_page_index, len(mcid_to_src_elem),
    )

    # ── Step 2: extract every MCID present in the content stream ─────────────
    mcids_in_content: set[int] = set()
    if src_page_obj is not None:
        _extract_mcids_from_page(src_page_obj, mcids_in_content)
    if not mcids_in_content:
        # Fallback: scan the already-built XObject stream bytes
        _extract_mcids_from_stream(xobj_stream, mcids_in_content)

    max_mcid = max(
        (max(mcids_in_content, default=-1), max(mcid_to_src_elem.keys(), default=-1))
    )
    if max_mcid < 0:
        log.debug("migrate_tags_for_xobject: no MCIDs found in %s page %d", src_path, src_page_index)
        return False

    # ── Step 3: allocate /StructParents slot and write parent table ───────────
    # Remove any stale /StructParents that copy_foreign may have copied from the
    # source page dict (would collide with a destination page's slot).
    if "/StructParents" in xobj_stream.stream_dict:
        del xobj_stream.stream_dict["/StructParents"]

    # Resolve destAttachPoint lazily
    dest_attach = _resolve_dest_attach(dst_root, dst, dst_page_obj)

    # Mirror ancestor chains for every real MCID; collect dest StructElem per mcid
    parent_map: dict[int, pikepdf.Object] = {}  # src_dict.objgen → dst StructElem
    mcid_to_dst_elem: dict[int, pikepdf.Object] = {}
    for mcid, src_elem in mcid_to_src_elem.items():
        try:
            dst_elem = _ensure_ancestor_chain(src_elem, src_root, dst_root, dst, dst_page_obj,
                                              dest_attach, parent_map)
        except Exception as exc:
            log.warning("migrate_tags_for_xobject: ancestor chain failed mcid=%d: %s", mcid, exc)
            continue
        if dst_elem is not None:
            mcid_to_dst_elem[mcid] = dst_elem

    # Build dense parent array (PDF null for gaps / unregistered MCIDs)
    _PDF_NULL = pikepdf.Object.parse(b"null")
    arr = pikepdf.Array()
    for i in range(max_mcid + 1):
        elem = mcid_to_dst_elem.get(i)
        arr.append(elem if elem is not None else _PDF_NULL)

    # Write to /ParentTree
    slot = _alloc_parent_tree_slot_for_xobj(dst_root)
    xobj_stream.stream_dict["/StructParents"] = pikepdf.Integer(slot)
    _write_xobject_parent_table(dst_root, slot, arr)

    log.debug(
        "migrate_tags_for_xobject Step3: slot=%d arr_len=%d real_mcids=%d",
        slot, len(arr), len(mcid_to_dst_elem),
    )

    if not mcid_to_dst_elem:
        return False

    # ── Step 4: append MCR dicts to dst StructElems ───────────────────────────
    # xobj_stream must be indirect so its ref is stable for /Stm.
    if getattr(xobj_stream, "objgen", (0, 0)) == (0, 0):
        xobj_stream = dst.make_indirect(xobj_stream)  # noqa: PLW2901

    for mcid, dst_elem in mcid_to_dst_elem.items():
        mcr = dst.make_indirect(pikepdf.Dictionary())
        mcr["/Type"] = pikepdf.Name("/MCR")
        mcr["/Pg"] = dst_page_obj
        mcr["/Stm"] = xobj_stream
        mcr["/MCID"] = pikepdf.Integer(mcid)
        _append_kid(dst_elem, mcr)

    log.debug(
        "migrate_tags_for_xobject Step4: appended %d MCR dicts src=%s",
        len(mcid_to_dst_elem), src_path,
    )
    return True


# ── Phase-2 internal helpers ─────────────────────────────────────────────────

def _collect_page_mcrs(
    src_root: pikepdf.Object,
    src_page_obj: pikepdf.Object,
    out: "dict[int, pikepdf.Object]",
) -> None:
    """Walk src /ParentTree to find every MCR that references *src_page_obj*.

    Populates *out* with ``{mcid: owning_struct_elem}``.

    Strategy: the /ParentTree slot for this page is indexed by the page's
    ``/StructParents`` integer.  That slot is a flat array of StructElems
    (one per MCID, in order).  We iterate the array and use the index as
    the MCID.
    """
    sp = src_page_obj.get("/StructParents")
    if sp is None:
        # /StructParents absent: fall back to linear struct-tree walk
        _walk_struct_for_page_mcrs(src_root, src_page_obj, out)
        return

    slot = int(sp)
    pt = src_root.get("/ParentTree")
    if pt is None:
        return
    nums = list(pt.get("/Nums", []))
    for i in range(0, len(nums), 2):
        try:
            if int(nums[i]) != slot:
                continue
            val = nums[i + 1]
            if isinstance(val, pikepdf.Array):
                for mcid, elem in enumerate(val):
                    if hasattr(elem, "get") and elem.get("/S") is not None:
                        out[mcid] = elem
            return
        except Exception:
            continue


def _walk_struct_for_page_mcrs(
    node: pikepdf.Object,
    src_page_obj: pikepdf.Object,
    out: "dict[int, pikepdf.Object]",
    _visited: "set | None" = None,
) -> None:
    """Fallback linear walk when /StructParents is absent."""
    if _visited is None:
        _visited = set()
    if not hasattr(node, "get"):
        return
    key = getattr(node, "objgen", id(node))
    if key in _visited:
        return
    _visited.add(key)

    k = node.get("/K")
    kids: list = []
    if isinstance(k, pikepdf.Array):
        kids = list(k)
    elif k is not None:
        kids = [k]

    for kid in kids:
        if isinstance(kid, pikepdf.Dictionary):
            typ = kid.get("/Type")
            if typ == pikepdf.Name("/MCR"):
                pg = kid.get("/Pg")
                mcid_val = kid.get("/MCID")
                if pg is not None and mcid_val is not None:
                    try:
                        if pg.objgen == src_page_obj.objgen:
                            out[int(mcid_val)] = node  # parent of the MCR
                    except Exception:
                        pass
            else:
                _walk_struct_for_page_mcrs(kid, src_page_obj, out, _visited)
        elif hasattr(kid, "get"):
            _walk_struct_for_page_mcrs(kid, src_page_obj, out, _visited)


_MCID_RE = _re.compile(rb"/MCID\s+(\d+)")


def _extract_mcids_from_page(page_obj: pikepdf.Object, out: "set[int]") -> None:
    """Scan all content streams of *page_obj* for ``/MCID n`` tokens."""
    contents = page_obj.get("/Contents")
    if contents is None:
        return
    streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    for cs in streams:
        try:
            data = cs.read_bytes()
            for m in _MCID_RE.finditer(data):
                out.add(int(m.group(1)))
        except Exception:
            pass


def _extract_mcids_from_stream(xobj: pikepdf.Object, out: "set[int]") -> None:
    """Scan an XObject stream's bytes for ``/MCID n`` tokens."""
    try:
        data = xobj.read_bytes()
        for m in _MCID_RE.finditer(data):
            out.add(int(m.group(1)))
    except Exception:
        pass


def _alloc_parent_tree_slot_for_xobj(dst_root: pikepdf.Object) -> int:
    """Allocate a fresh /ParentTree slot number for an XObject, strictly above
    the current /ParentTreeNextKey (which equals the number of pages already
    processed).  This guarantees no collision with any page slot.
    """
    pt = dst_root.get("/ParentTree")
    nums = list(pt.get("/Nums", [])) if pt else []
    max_existing = -1
    for i in range(0, len(nums), 2):
        try:
            max_existing = max(max_existing, int(nums[i]))
        except Exception:
            pass
    hint = int(dst_root.get("/ParentTreeNextKey", 0))
    slot = max(hint, max_existing + 1)
    # Don't advance /ParentTreeNextKey here — fix_link_annots and
    # _write_parent_tree_slot do that separately; we keep the slot private.
    return slot


def _write_xobject_parent_table(
    dst_root: pikepdf.Object,
    slot: int,
    arr: pikepdf.Array,
) -> None:
    """Append ``slot → arr`` to the flat /ParentTree /Nums array."""
    pt = dst_root.get("/ParentTree")
    if pt is None:
        pt = pikepdf.Dictionary()
        pt["/Nums"] = pikepdf.Array()
        dst_root["/ParentTree"] = pt
    nums = pt.get("/Nums")
    if nums is None:
        nums = pikepdf.Array()
        pt["/Nums"] = nums
    nums.append(pikepdf.Integer(slot))
    nums.append(arr)
    # Advance NextKey past this slot
    dst_root["/ParentTreeNextKey"] = pikepdf.Integer(
        max(int(dst_root.get("/ParentTreeNextKey", 0)), slot + 1)
    )


def _resolve_dest_attach(
    dst_root: pikepdf.Object,
    dst: pikepdf.Pdf,
    dst_page_obj: pikepdf.Object,
) -> pikepdf.Object:
    """Return the destination StructElem under which migrated ancestors attach.

    Resolution order (mirrors ``ResolveDestAttachPoint``):
    1. StructElem that owns *dst_page_obj* in the dst struct tree.
    2. The ``<Document>`` kid of dst_root.
    3. Freshly-created ``<Sect>`` appended to dst_root.
    """
    # Try to find the StructElem that owns dst_page_obj
    owner = _find_page_owner(dst_root, dst_page_obj)
    if owner is not None:
        return owner
    # Fallback: <Document> kid
    doc = _find_document_elem(dst_root)
    if doc is not None:
        return doc
    # Last resort: fresh <Sect>
    sect = dst.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"),
        S=pikepdf.Name("/Sect"),
        P=dst_root,
    ))
    sect["/K"] = pikepdf.Array()
    k = dst_root.get("/K")
    if k is None:
        dst_root["/K"] = pikepdf.Array([sect])
    elif isinstance(k, pikepdf.Array):
        k.append(sect)
    else:
        dst_root["/K"] = pikepdf.Array([k, sect])
    return sect


def _find_page_owner(
    node: pikepdf.Object,
    target_page: pikepdf.Object,
    _visited: "set | None" = None,
) -> "pikepdf.Object | None":
    """Return the StructElem whose /Pg == target_page, or None."""
    if _visited is None:
        _visited = set()
    if not hasattr(node, "get"):
        return None
    key = getattr(node, "objgen", id(node))
    if key in _visited:
        return None
    _visited.add(key)

    pg = node.get("/Pg")
    if pg is not None:
        try:
            if pg.objgen == target_page.objgen:
                return node
        except Exception:
            pass

    k = node.get("/K")
    kids = list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
    for kid in kids:
        if hasattr(kid, "get"):
            result = _find_page_owner(kid, target_page, _visited)
            if result is not None:
                return result
    return None


def _ensure_ancestor_chain(
    src_elem: pikepdf.Object,
    src_root: pikepdf.Object,
    dst_root: pikepdf.Object,
    dst: pikepdf.Pdf,
    dst_page_obj: pikepdf.Object,
    dest_attach: pikepdf.Object,
    parent_map: "dict[int, pikepdf.Object]",
) -> "pikepdf.Object | None":
    """Mirror *src_elem* and all its ancestors (up to but not including
    ``<Document>`` / StructTreeRoot) into *dst*, caching results in
    *parent_map* (keyed by src objgen) to avoid duplicate mirrors.

    Returns the destination StructElem that corresponds to *src_elem*,
    or ``None`` if the element cannot be mirrored.

    Ports ``PdfMergeTagHelper.EnsureAncestorChain`` (line 1117).
    """
    if not hasattr(src_elem, "get"):
        return None

    src_key = getattr(src_elem, "objgen", None)
    if src_key is None:
        src_key = id(src_elem)
    if src_key in parent_map:
        return parent_map[src_key]

    # Determine parent of src_elem
    src_parent = src_elem.get("/P")
    is_top_level = False
    if src_parent is None:
        is_top_level = True
    else:
        p_s = src_parent.get("/S") if hasattr(src_parent, "get") else None
        p_type = src_parent.get("/Type") if hasattr(src_parent, "get") else None
        if p_s == pikepdf.Name("/Document") or p_type == pikepdf.Name("/StructTreeRoot"):
            is_top_level = True

    if is_top_level:
        dst_parent = dest_attach
    else:
        dst_parent = _ensure_ancestor_chain(
            src_parent, src_root, dst_root, dst, dst_page_obj, dest_attach, parent_map
        )
        if dst_parent is None:
            dst_parent = dest_attach

    role = src_elem.get("/S")
    if role is None:
        return None

    # Build a fresh dst StructElem, copying non-structural attributes
    new_elem = dst.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"),
        S=role,
        Pg=dst_page_obj,
        P=dst_parent,
    ))
    new_elem["/K"] = pikepdf.Array()

    # Copy non-structural attributes (e.g. /Alt, /ActualText, /A, /Lang).
    # All pikepdf Object values -- even direct (non-indirect) ones -- belong to
    # their source Pdf and cannot be assigned directly into a different Pdf.
    # Strategy:
    #   1. copy_foreign() for indirect objects (works for dicts, arrays, streams).
    #   2. For direct objects (og==(0,0)): use Object.parse on the unparse bytes,
    #      which produces a fresh owner-independent value. Fall back to str()-based
    #      reconstruction for strings, then to copy_foreign as last resort.
    _SKIP_KEYS = {"/K", "/P", "/Pg", "/S", "/Type"}
    for key in src_elem.keys():
        if key in _SKIP_KEYS:
            continue
        try:
            val = src_elem[key]
            og = val.objgen
            if og != (0, 0):
                # Indirect object: copy_foreign deep-copies it into dst
                new_elem[key] = dst.copy_foreign(val)
            else:
                # Direct object: re-parse from bytes (owner-neutral)
                try:
                    new_elem[key] = pikepdf.Object.parse(val.unparse())
                except Exception:
                    # Last-resort fallbacks for common types
                    try:
                        new_elem[key] = pikepdf.String(str(val))  # strings
                    except Exception:
                        pass
        except Exception:
            pass  # skip truly uncopyable entries

    # Attach under dst_parent
    _append_kid(dst_parent, new_elem)
    parent_map[src_key] = new_elem
    return new_elem


def _append_kid(parent: pikepdf.Object, kid: pikepdf.Object) -> None:
    """Append *kid* to the /K array of *parent* (creating /K if absent)."""
    k = parent.get("/K")
    if k is None:
        parent["/K"] = pikepdf.Array([kid])
    elif isinstance(k, pikepdf.Array):
        k.append(kid)
    else:
        parent["/K"] = pikepdf.Array([k, kid])


# ---------------------------------------------------------------------------
# Link annotation repair (Matterhorn 28-001)
# ---------------------------------------------------------------------------

def fix_link_annots(dst: pikepdf.Pdf) -> int:
    """Wrap every orphaned Link annotation in a ``<Link>`` struct element.

    An annotation is *orphaned* when it has no ``/StructParent`` (i.e. it is
    not yet referenced from the struct tree via an OBJR).  For each such
    annotation this function:

    1. Creates a ``<Link>`` struct element with an OBJR kid pointing at the
       annotation.
    2. Appends the ``<Link>`` element under the document-level ``<Document>``
       element (creating the struct tree if the PDF is not yet tagged).
    3. Allocates the next ``/ParentTree`` slot, writes the slot entry, and
       sets ``/StructParent`` on the annotation.

    Returns the number of annotations fixed (0 = nothing to do).

    Mirrors the repair performed by ``CTS2.0 PdfMergeHelper.CopyAnnotations``
    (the plain-copy path) and the ``pdftagvalicate`` link-nesting repair,
    adapted to run inside the merge pipeline rather than as a post-repair.
    """
    dst_root = _ensure_dst_struct_root(dst)
    dst_doc = _get_or_create_document(dst, dst_root)

    # Build set of annotation obj-numbers already referenced via OBJR from
    # any <Link> struct element, so we skip them.
    already_tagged: set[int] = set()
    _collect_objr_refs(dst_root, already_tagged)

    # Prepare ParentTree for new entries
    pt = dst_root.get("/ParentTree")
    if pt is None:
        pt = pikepdf.Dictionary()
        pt["/Nums"] = pikepdf.Array()
        dst_root["/ParentTree"] = pt
    nums = pt.get("/Nums")
    if nums is None:
        nums = pikepdf.Array()
        pt["/Nums"] = nums

    next_key = _next_parent_tree_key(nums, dst_root)
    fixed = 0

    for page in dst.pages:
        page_obj = page.obj
        annots = page_obj.get("/Annots")
        if annots is None:
            continue

        for annot in list(annots):  # snapshot — we may mutate /StructParent
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            if annot.get("/Subtype") != pikepdf.Name("/Link"):
                continue

            # Ensure the annotation is an indirect object so OBJR can point at it
            if getattr(annot, "objgen", (0, 0)) == (0, 0):
                annot = dst.make_indirect(annot)  # noqa: PLW2901

            obj_num = annot.objgen[0]
            if obj_num in already_tagged:
                continue

            # Build OBJR dict:  {/Type /OBJR, /Pg page_obj, /Obj annot}
            objr = pikepdf.Dictionary()
            objr["/Type"] = pikepdf.Name("/OBJR")
            objr["/Pg"] = page_obj
            objr["/Obj"] = annot

            # Create <Link> struct element
            link_elem = dst.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name("/StructElem"),
                S=pikepdf.Name("/Link"),
                Pg=page_obj,
                P=dst_doc,
                K=objr,
            ))

            # Attach under <Document>
            doc_k = dst_doc.get("/K")
            if doc_k is None:
                dst_doc["/K"] = pikepdf.Array([link_elem])
            elif isinstance(doc_k, pikepdf.Array):
                doc_k.append(link_elem)
            else:
                dst_doc["/K"] = pikepdf.Array([doc_k, link_elem])

            # ParentTree slot -> link_elem (single struct elem, not an array)
            annot["/StructParent"] = pikepdf.Integer(next_key)
            nums.append(pikepdf.Integer(next_key))
            nums.append(link_elem)
            next_key += 1
            fixed += 1

    dst_root["/ParentTreeNextKey"] = pikepdf.Integer(next_key)
    if fixed:
        log.debug("fix_link_annots: wrapped %d Link annotation(s)", fixed)
    return fixed


def _collect_objr_refs(node: pikepdf.Object, out: set[int],
                       _parent_is_link: bool = False,
                       _visited: "set | None" = None) -> None:
    """Walk the struct tree and collect obj-numbers of annotations already
    referenced via OBJR from a ``<Link>`` element."""
    if _visited is None:
        _visited = set()
    if not isinstance(node, pikepdf.Dictionary):
        return
    key = getattr(node, "objgen", (0, 0))
    key = key if key != (0, 0) else id(node)
    if key in _visited:
        return
    _visited.add(key)

    is_link = (node.get("/S") == pikepdf.Name("/Link"))

    k = node.get("/K")
    kids: list = []
    if k is None:
        kids = []
    elif isinstance(k, pikepdf.Array):
        kids = list(k)
    elif isinstance(k, pikepdf.Dictionary):
        kids = [k]

    for kid in kids:
        if isinstance(kid, pikepdf.Dictionary):
            if (is_link or _parent_is_link) and kid.get("/Type") == pikepdf.Name("/OBJR"):
                annot_ref = kid.get("/Obj")
                if annot_ref is not None:
                    og = getattr(annot_ref, "objgen", (0, 0))
                    if og != (0, 0):
                        out.add(og[0])
            _collect_objr_refs(kid, out, is_link, _visited)


def _next_parent_tree_key(nums: pikepdf.Array, dst_root: pikepdf.Object) -> int:
    """Return the next available ParentTree integer key.

    Takes the larger of the ``/ParentTreeNextKey`` hint and
    ``max(existing keys) + 1`` to avoid collisions.
    """
    max_key = -1
    for i in range(0, len(nums), 2):
        try:
            max_key = max(max_key, int(nums[i]))
        except Exception:
            pass
    actual_next = max_key + 1
    hint = dst_root.get("/ParentTreeNextKey")
    if hint is not None:
        return max(int(hint), actual_next)
    return actual_next
