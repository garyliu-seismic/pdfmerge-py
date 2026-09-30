"""Tests for tagtree.py — plain-copy path StructTree merge.

All PDFs are constructed programmatically using pikepdf.
Covers:
  1. Non-tagged source is a no-op
  2. Single tagged source: StructTreeRoot created, Document/Slide/ParentTree correct
  3. Two tagged sources merged: single Document with two Slide kids, two PT slots
  4. RoleMap merged from source without overwriting existing dst entries
  5. ParentTree slot numbers are unique and ascending
  6. /Pg references on all StructElems point to the correct dst page
  7. merge_files end-to-end with tagged PDFs
  8. merge_from_xml plain-copy path with tagged main PDF
  9. set_pdfua_metadata propagates /Lang, /Title, DisplayDocTitle
 10. Non-tagged source alongside tagged source
 11. fix_link_annots: orphaned Link annots gain <Link>+OBJR+/StructParent
 12. fix_link_annots: already-tagged annots are skipped
 13. fix_link_annots: merge_files end-to-end wraps Link annots
 14. fix_link_annots: no Link annots → zero fixed, no crash
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdfmergepy.tagtree import (
    fix_link_annots,
    merge_page_tags,
    migrate_tags_for_xobject,
    set_pdfua_metadata,
    _is_tagged,
    _find_document_elem,
    _merge_rolemap,
)
from pdfmergepy.merge import merge_files
from pdfmergepy.mergeinfo import parse_merge_info
from pdfmergepy.composite import merge_from_xml
from pdfmergepy.pdfutil import PageSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tagged_pdf(
    path: Path,
    num_pages: int = 1,
    width: float = 612,
    height: float = 792,
    lang: str = "en-US",
    title: str = "Test Doc",
    extra_roles: dict | None = None,
) -> None:
    """Create a minimal valid tagged PDF with one <Slide> per page."""
    pdf = pikepdf.Pdf.new()

    # PDF/UA metadata
    pdf.Root["/Lang"] = pikepdf.String(lang)
    pdf.docinfo["/Title"] = pikepdf.String(title)
    mi = pikepdf.Dictionary()
    mi["/Marked"] = pikepdf.Boolean(True)
    pdf.Root["/MarkInfo"] = mi

    # StructTreeRoot
    sr = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/StructTreeRoot")))
    rm = pikepdf.Dictionary()
    rm["/Slide"] = pikepdf.Name("/Sect")
    if extra_roles:
        for k, v in extra_roles.items():
            rm[k] = pikepdf.Name(v)
    sr["/RoleMap"] = rm
    pt = pikepdf.Dictionary()
    pt["/Nums"] = pikepdf.Array()
    sr["/ParentTree"] = pt
    sr["/ParentTreeNextKey"] = pikepdf.Integer(0)

    doc_elem = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Document"), P=sr))
    doc_elem["/K"] = pikepdf.Array()
    sr["/K"] = pikepdf.Array([doc_elem])
    pdf.Root["/StructTreeRoot"] = sr

    for page_num in range(num_pages):
        page = pdf.add_blank_page(page_size=(width, height))

        slide = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Slide"),
            P=doc_elem, Pg=page.obj))
        para = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/P"),
            P=slide, Pg=page.obj))
        mcr = pikepdf.Dictionary()
        mcr["/Type"] = pikepdf.Name("/MCR")
        mcr["/Pg"] = page.obj
        mcr["/MCID"] = pikepdf.Integer(0)
        para["/K"] = mcr
        slide["/K"] = pikepdf.Array([para])
        doc_elem["/K"].append(slide)

        slot = page_num
        pt["/Nums"].append(pikepdf.Integer(slot))
        pt["/Nums"].append(pikepdf.Array([para]))
        sr["/ParentTreeNextKey"] = pikepdf.Integer(page_num + 1)
        page.obj["/StructParents"] = pikepdf.Integer(slot)

        page.obj["/Contents"] = pdf.make_stream(
            f"/P <</MCID 0>> BDC (Page {page_num}) Tj EMC".encode()
        )

    pdf.save(path)


def _make_plain_pdf(path: Path, num_pages: int = 1,
                    width: float = 612, height: float = 792) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(num_pages):
        pdf.add_blank_page(page_size=(width, height))
    pdf.save(path)


def _write_xml(path: Path, content: str) -> None:
    path.write_bytes(content.encode("utf-16"))


def _get_struct_root(pdf_path: Path) -> pikepdf.Object | None:
    with pikepdf.open(pdf_path) as pdf:
        return pdf.Root.get("/StructTreeRoot")


def _count_document_slide_kids(pdf_path: Path) -> int:
    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return 0
        doc = _find_document_elem(sr)
        if doc is None:
            return 0
        return len(list(doc.get("/K", [])))


def _parent_tree_slots(pdf_path: Path) -> list[int]:
    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return []
        pt = sr.get("/ParentTree")
        if pt is None:
            return []
        nums = list(pt.get("/Nums", []))
        return [int(nums[i]) for i in range(0, len(nums), 2)]


# Roles that are document-spanning and do NOT need /Pg (PDF spec §14.7.2)
_DOC_LEVEL_ROLES = {pikepdf.Name("/Document"), pikepdf.Name("/Part")}


def _all_pg_present(pdf_path: Path) -> bool:
    """Return True if every page-specific StructElem has /Pg set.

    Document-level containers (<Document>, <Part>) are intentionally skipped
    because they span the whole file and must NOT have a single-page /Pg.
    """
    def _check(elem) -> bool:
        if not hasattr(elem, "get"):
            return True
        role = elem.get("/S")
        if role is not None and role not in _DOC_LEVEL_ROLES:
            if elem.get("/Pg") is None:
                return False
        k = elem.get("/K")
        if k is None:
            return True
        if isinstance(k, pikepdf.Array):
            return all(_check(c) for c in k)
        if isinstance(k, pikepdf.Dictionary):
            return True  # MCR dict — /Pg optional per spec
        return True

    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return True
        k = sr.get("/K")
        if k is None:
            return True
        kids = list(k) if isinstance(k, pikepdf.Array) else [k]
        return all(_check(d) for d in kids)


# ---------------------------------------------------------------------------
# 1. Non-tagged source is no-op
# ---------------------------------------------------------------------------

def test_merge_page_tags_non_tagged_noop(tmp_path: Path) -> None:
    plain = tmp_path / "plain.pdf"
    _make_plain_pdf(plain)

    dst = pikepdf.Pdf.new()
    with pikepdf.open(plain) as src:
        dst.add_pages_from(src, forms="preserve")
        dst_page = dst.pages[-1].obj
        result = merge_page_tags(src, dst, dst_page)

    assert result is False
    assert "/StructTreeRoot" not in dst.Root


# ---------------------------------------------------------------------------
# 2. Single tagged source: correct tree structure
# ---------------------------------------------------------------------------

def test_merge_page_tags_single_tagged(tmp_path: Path) -> None:
    tagged = tmp_path / "tagged.pdf"
    _make_tagged_pdf(tagged)
    out = tmp_path / "out.pdf"

    dst = pikepdf.Pdf.new()
    with pikepdf.open(tagged) as src:
        dst.add_pages_from(src, forms="preserve")
        dst_page = dst.pages[-1].obj
        result = merge_page_tags(src, dst, dst_page)

    assert result is True
    dst.save(out)

    with pikepdf.open(out) as v:
        sr = v.Root["/StructTreeRoot"]
        assert sr is not None
        assert v.Root.get("/MarkInfo") is not None

        # Single <Document> kid
        doc = _find_document_elem(sr)
        assert doc is not None, "No <Document> found under StructTreeRoot"

        # One <Slide> under Document
        slide_kids = list(doc.get("/K", []))
        assert len(slide_kids) == 1
        assert slide_kids[0].get("/S") == pikepdf.Name("/Slide")

        # /Pg on Slide points to the (only) page
        pg = slide_kids[0].get("/Pg")
        assert pg is not None, "Slide /Pg is missing"

        # ParentTree has one slot
        pt = sr["/ParentTree"]
        nums = list(pt["/Nums"])
        assert len(nums) == 2  # [slot0, [elems]]
        assert int(nums[0]) == 0


# ---------------------------------------------------------------------------
# 3. Two tagged sources: two Slide kids, two ParentTree slots
# ---------------------------------------------------------------------------

def test_merge_page_tags_two_sources(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"
    _make_tagged_pdf(a)
    _make_tagged_pdf(b)

    dst = pikepdf.Pdf.new()
    for src_path in [a, b]:
        with pikepdf.open(src_path) as src:
            dst.add_pages_from(src, forms="preserve")
            merge_page_tags(src, dst, dst.pages[-1].obj, src_path=src_path)
    dst.save(out)

    # Two slide kids under one Document
    assert _count_document_slide_kids(out) == 2

    # Two distinct ParentTree slots
    slots = _parent_tree_slots(out)
    assert slots == [0, 1], f"Expected slots [0,1], got {slots}"

    # All /Pg references intact
    assert _all_pg_present(out)


# ---------------------------------------------------------------------------
# 4. RoleMap merging — new roles added, existing preserved
# ---------------------------------------------------------------------------

def test_rolemap_merged_without_overwrite(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"
    # a has /MyRole → /Sect; b has /OtherRole → /P and also /MyRole → /Div
    _make_tagged_pdf(a, extra_roles={"/MyRole": "/Sect"})
    _make_tagged_pdf(b, extra_roles={"/OtherRole": "/P", "/MyRole": "/Div"})

    dst = pikepdf.Pdf.new()
    for src_path in [a, b]:
        with pikepdf.open(src_path) as src:
            dst.add_pages_from(src, forms="preserve")
            merge_page_tags(src, dst, dst.pages[-1].obj)
    dst.save(out)

    with pikepdf.open(out) as v:
        sr = v.Root["/StructTreeRoot"]
        rm = sr.get("/RoleMap")
        assert rm is not None
        # /MyRole from a takes precedence (first writer wins)
        assert str(rm.get("/MyRole")) == "/Sect"
        # /OtherRole from b was added
        assert "/OtherRole" in rm


# ---------------------------------------------------------------------------
# 5. ParentTree slots are unique and ascending
# ---------------------------------------------------------------------------

def test_parent_tree_slots_unique_ascending(tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    dst = pikepdf.Pdf.new()
    for i in range(4):
        p = tmp_path / f"s{i}.pdf"
        _make_tagged_pdf(p)
        with pikepdf.open(p) as src:
            dst.add_pages_from(src, forms="preserve")
            merge_page_tags(src, dst, dst.pages[-1].obj)
    dst.save(out)

    slots = _parent_tree_slots(out)
    assert slots == list(range(4)), f"Expected [0,1,2,3], got {slots}"


# ---------------------------------------------------------------------------
# 6. /Pg references correct for all StructElems
# ---------------------------------------------------------------------------

def test_pg_references_all_present(tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    dst = pikepdf.Pdf.new()
    for _ in range(3):
        p = tmp_path / f"p{_}.pdf"
        _make_tagged_pdf(p)
        with pikepdf.open(p) as src:
            dst.add_pages_from(src, forms="preserve")
            merge_page_tags(src, dst, dst.pages[-1].obj)
    dst.save(out)
    assert _all_pg_present(out), "Some StructElem /Pg references are missing"


# ---------------------------------------------------------------------------
# 7. merge_files end-to-end with tagged PDFs
# ---------------------------------------------------------------------------

def test_merge_files_tagged_end_to_end(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"
    _make_tagged_pdf(a, title="Doc A")
    _make_tagged_pdf(b, title="Doc B")

    merge_files(
        [PageSpec(path=a, pages=(1,)), PageSpec(path=b, pages=(1,))],
        out,
    )

    with pikepdf.open(out) as v:
        assert len(v.pages) == 2
        sr = v.Root.get("/StructTreeRoot")
        assert sr is not None, "No StructTreeRoot in merged output"
        assert v.Root.get("/MarkInfo") is not None

    assert _count_document_slide_kids(out) == 2
    assert _parent_tree_slots(out) == [0, 1]
    assert _all_pg_present(out)


# ---------------------------------------------------------------------------
# 8. merge_from_xml plain-copy path with tagged main PDF
# ---------------------------------------------------------------------------

def test_merge_xml_plain_copy_tagged(tmp_path: Path) -> None:
    main_path = tmp_path / "main.pdf"
    out_path = tmp_path / "out.pdf"
    _make_tagged_pdf(main_path, num_pages=2)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge />
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    with pikepdf.open(out_path) as v:
        assert len(v.pages) == 2
        sr = v.Root.get("/StructTreeRoot")
        assert sr is not None

    assert _count_document_slide_kids(out_path) == 2
    assert _parent_tree_slots(out_path) == [0, 1]
    assert _all_pg_present(out_path)


# ---------------------------------------------------------------------------
# 9. set_pdfua_metadata propagates /Lang, /Title, DisplayDocTitle
# ---------------------------------------------------------------------------

def test_set_pdfua_metadata(tmp_path: Path) -> None:
    src_path = tmp_path / "src.pdf"
    _make_tagged_pdf(src_path, lang="fr-FR", title="Mon Document")

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        set_pdfua_metadata(src, dst)

    assert str(dst.Root.get("/Lang", "")) == "fr-FR"
    assert str(dst.docinfo.get("/Title", "")) == "Mon Document"
    vp = dst.Root.get("/ViewerPreferences")
    assert vp is not None
    assert vp.get("/DisplayDocTitle") == pikepdf.Boolean(True)


def test_set_pdfua_metadata_idempotent(tmp_path: Path) -> None:
    """Calling set_pdfua_metadata twice must not overwrite the first /Lang."""
    src1 = tmp_path / "src1.pdf"
    src2 = tmp_path / "src2.pdf"
    _make_tagged_pdf(src1, lang="en-US")
    _make_tagged_pdf(src2, lang="de-DE")

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src1) as s1:
        set_pdfua_metadata(s1, dst)
    with pikepdf.open(src2) as s2:
        set_pdfua_metadata(s2, dst)

    # First call wins
    assert str(dst.Root.get("/Lang", "")) == "en-US"


# ---------------------------------------------------------------------------
# 10. Non-tagged alongside tagged source
# ---------------------------------------------------------------------------

def test_merge_tagged_and_plain_mixed(tmp_path: Path) -> None:
    tagged = tmp_path / "tagged.pdf"
    plain = tmp_path / "plain.pdf"
    out = tmp_path / "out.pdf"
    _make_tagged_pdf(tagged)
    _make_plain_pdf(plain)

    dst = pikepdf.Pdf.new()
    for src_path in [tagged, plain]:
        with pikepdf.open(src_path) as src:
            dst.add_pages_from(src, forms="preserve")
            merge_page_tags(src, dst, dst.pages[-1].obj)
    dst.save(out)

    # Only the tagged page contributes a Slide kid
    assert _count_document_slide_kids(out) == 1
    # Only one ParentTree slot
    assert _parent_tree_slots(out) == [0]
    assert _all_pg_present(out)


# ---------------------------------------------------------------------------
# Helpers for fix_link_annots tests
# ---------------------------------------------------------------------------

def _make_pdf_with_link_annots(
    path: Path,
    n_links: int = 2,
    with_struct_parent: bool = False,
) -> None:
    """PDF with one page and *n_links* Link annotations (no /StructParent by default)."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    annots: list[pikepdf.Object] = []
    for i in range(n_links):
        a = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/Annot"),
            Subtype=pikepdf.Name("/Link"),
            Rect=pikepdf.Array([72, 700 - i * 30, 300, 720 - i * 30]),
            A=pikepdf.Dictionary(
                Type=pikepdf.Name("/Action"),
                S=pikepdf.Name("/URI"),
                URI=pikepdf.String(f"https://example.com/{i}"),
            ),
        ))
        if with_struct_parent:
            a["/StructParent"] = pikepdf.Integer(i)
        annots.append(a)
    page.obj["/Annots"] = pikepdf.Array(annots)
    pdf.save(path)


def _count_link_struct_elems(pdf_path: Path) -> int:
    """Count <Link> struct elements in the struct tree of *pdf_path*."""
    count = 0
    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return 0
        def walk(node):
            nonlocal count
            if not hasattr(node, "get"):
                return
            if node.get("/S") == pikepdf.Name("/Link"):
                count += 1
            k = node.get("/K")
            if k is None:
                return
            for child in (k if isinstance(k, pikepdf.Array) else [k]):
                walk(child)
        k = sr.get("/K")
        for child in (k if isinstance(k, pikepdf.Array) else [k]):
            walk(child)
    return count


def _link_annots_have_struct_parent(pdf_path: Path) -> bool:
    """Return True if every Link annotation has a /StructParent key."""
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            for a in page.obj.get("/Annots", []):
                if a.get("/Subtype") == pikepdf.Name("/Link"):
                    if a.get("/StructParent") is None:
                        return False
    return True


def _objr_annot_obj_numbers(pdf_path: Path) -> set[int]:
    """Return obj-numbers of annotations referenced via OBJR from <Link> elems."""
    out: set[int] = set()
    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return out
        def walk(node, parent_is_link=False):
            if not hasattr(node, "get"):
                return
            is_link = node.get("/S") == pikepdf.Name("/Link")
            k = node.get("/K")
            kids = list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
            for child in kids:
                if isinstance(child, pikepdf.Dictionary):
                    if (is_link or parent_is_link) and child.get("/Type") == pikepdf.Name("/OBJR"):
                        obj = child.get("/Obj")
                        if obj is not None:
                            og = getattr(obj, "objgen", (0, 0))
                            if og != (0, 0):
                                out.add(og[0])
                    walk(child, is_link)
        k = sr.get("/K")
        for child in (list(k) if isinstance(k, pikepdf.Array) else [k]):
            walk(child)
    return out


# ---------------------------------------------------------------------------
# 11. fix_link_annots: orphaned Link annots gain <Link>+OBJR+/StructParent
# ---------------------------------------------------------------------------

def test_fix_link_annots_basic(tmp_path: Path) -> None:
    """fix_link_annots must create one <Link> struct elem per orphaned annot,
    each with an OBJR kid and /StructParent set on the annotation."""
    src_path = tmp_path / "src.pdf"
    out_path = tmp_path / "out.pdf"
    _make_pdf_with_link_annots(src_path, n_links=2)

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        dst.add_pages_from(src, forms="preserve")
    n_fixed = fix_link_annots(dst)
    dst.save(out_path)

    assert n_fixed == 2, f"Expected 2 fixed, got {n_fixed}"
    assert _count_link_struct_elems(out_path) == 2
    assert _link_annots_have_struct_parent(out_path)
    assert len(_objr_annot_obj_numbers(out_path)) == 2


# ---------------------------------------------------------------------------
# 12. fix_link_annots: already-tagged annots are not duplicated
# ---------------------------------------------------------------------------

def test_fix_link_annots_already_tagged_skipped(tmp_path: Path) -> None:
    """Annotations that already have an OBJR in the struct tree must not get
    a second <Link> wrapper."""
    src_path = tmp_path / "src.pdf"
    out_path = tmp_path / "out.pdf"
    _make_pdf_with_link_annots(src_path, n_links=1)

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        dst.add_pages_from(src, forms="preserve")

    # First call: wraps the one annot
    n1 = fix_link_annots(dst)
    # Second call: annot is now tagged → skipped
    n2 = fix_link_annots(dst)
    dst.save(out_path)

    assert n1 == 1
    assert n2 == 0
    # Only one <Link> elem despite calling twice
    assert _count_link_struct_elems(out_path) == 1


# ---------------------------------------------------------------------------
# 13. merge_files end-to-end: Link annots wrapped automatically
# ---------------------------------------------------------------------------

def test_merge_files_link_annots_wrapped(tmp_path: Path) -> None:
    """merge_files must call fix_link_annots so the output passes 28-001."""
    src_path = tmp_path / "src.pdf"
    out_path = tmp_path / "out.pdf"
    _make_pdf_with_link_annots(src_path, n_links=3)

    merge_files([PageSpec(path=src_path, pages=(1,))], out_path)

    assert _count_link_struct_elems(out_path) == 3
    assert _link_annots_have_struct_parent(out_path)
    objr_nums = _objr_annot_obj_numbers(out_path)
    assert len(objr_nums) == 3


# ---------------------------------------------------------------------------
# 14. fix_link_annots: no Link annots → zero fixed, no crash
# ---------------------------------------------------------------------------

def test_fix_link_annots_no_links(tmp_path: Path) -> None:
    """A PDF with no Link annotations must not be modified; returns 0."""
    src_path = tmp_path / "plain.pdf"
    _make_plain_pdf(src_path)

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        dst.add_pages_from(src, forms="preserve")

    n_fixed = fix_link_annots(dst)
    assert n_fixed == 0


# ---------------------------------------------------------------------------
# Phase 2: migrate_tags_for_xobject
# ---------------------------------------------------------------------------

def _make_tagged_pdf_with_content(path: Path, mcids: list[int] = (0, 1)) -> None:
    """Tagged PDF with explicit MCID tokens in the content stream and
    matching MCR entries in the ParentTree."""
    pdf = pikepdf.Pdf.new()
    pdf.Root["/Lang"] = pikepdf.String("en-US")
    pdf.docinfo["/Title"] = pikepdf.String("Test")
    mi = pikepdf.Dictionary()
    mi["/Marked"] = pikepdf.Boolean(True)
    pdf.Root["/MarkInfo"] = mi

    sr = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/StructTreeRoot")))
    rm = pikepdf.Dictionary()
    rm["/Slide"] = pikepdf.Name("/Sect")
    sr["/RoleMap"] = rm
    pt = pikepdf.Dictionary()
    pt["/Nums"] = pikepdf.Array()
    sr["/ParentTree"] = pt
    sr["/ParentTreeNextKey"] = pikepdf.Integer(0)

    doc_elem = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Document"), P=sr))
    doc_elem["/K"] = pikepdf.Array()
    sr["/K"] = pikepdf.Array([doc_elem])
    pdf.Root["/StructTreeRoot"] = sr

    page = pdf.add_blank_page(page_size=(612, 792))

    # Build a Slide -> one Span per MCID
    slide = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Slide"),
        P=doc_elem, Pg=page.obj))
    slide_kids = pikepdf.Array()
    parent_tree_arr = pikepdf.Array()
    for mcid in mcids:
        span = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Span"),
            P=slide, Pg=page.obj))
        mcr = pikepdf.Dictionary()
        mcr["/Type"] = pikepdf.Name("/MCR")
        mcr["/Pg"] = page.obj
        mcr["/MCID"] = pikepdf.Integer(mcid)
        span["/K"] = mcr
        slide_kids.append(span)
        # ParentTree entry: mcid index -> span
        parent_tree_arr.append(span)
    slide["/K"] = slide_kids
    doc_elem["/K"].append(slide)

    # Build content stream with MCID markers
    content_parts = []
    for mcid in mcids:
        content_parts.append(f"/Slide <</MCID {mcid}>> BDC (text) Tj EMC".encode())
    content = b"\n".join(content_parts)
    page.obj["/Contents"] = pdf.make_stream(content)

    # Slot 0 in ParentTree
    pt["/Nums"].append(pikepdf.Integer(0))
    pt["/Nums"].append(parent_tree_arr)
    sr["/ParentTreeNextKey"] = pikepdf.Integer(1)
    page.obj["/StructParents"] = pikepdf.Integer(0)

    pdf.save(path)


def _has_mcr_with_stm(pdf_path: Path) -> bool:
    """Return True if any MCR dict in the struct tree has a /Stm key (XObject ref)."""
    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return False
        def walk(node) -> bool:
            if not hasattr(node, "get"):
                return False
            typ = node.get("/Type")
            if typ == pikepdf.Name("/MCR") and node.get("/Stm") is not None:
                return True
            k = node.get("/K")
            if k is None:
                return False
            kids = list(k) if isinstance(k, pikepdf.Array) else [k]
            return any(walk(c) for c in kids)
        k = sr.get("/K")
        kids = list(k) if isinstance(k, pikepdf.Array) else [k]
        return any(walk(c) for c in kids)


def _xobj_struct_parents_slot(pdf_path: Path, xobj_name: str = "/Xobj0") -> int:
    """Return the /StructParents slot number from a named XObject, or -1."""
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            res = page.obj.get("/Resources")
            if res is None:
                continue
            xobjs = res.get("/XObject")
            if xobjs is None:
                continue
            for name in xobjs.keys():
                xobj = xobjs[name]
                if hasattr(xobj, "stream_dict"):
                    sp = xobj.stream_dict.get("/StructParents")
                    if sp is not None:
                        return int(sp)
    return -1


def _parent_tree_has_slot(pdf_path: Path, slot: int) -> bool:
    """Return True if the /ParentTree has an entry for *slot*."""
    with pikepdf.open(pdf_path) as pdf:
        sr = pdf.Root.get("/StructTreeRoot")
        if sr is None:
            return False
        pt = sr.get("/ParentTree")
        if pt is None:
            return False
        nums = list(pt.get("/Nums", []))
        for i in range(0, len(nums), 2):
            try:
                if int(nums[i]) == slot:
                    return True
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# 15. migrate_tags_for_xobject: non-tagged source is no-op
# ---------------------------------------------------------------------------

def test_migrate_tags_xobj_non_tagged(tmp_path: Path) -> None:
    """migrate_tags_for_xobject must return False for a non-tagged source."""
    src_path = tmp_path / "plain.pdf"
    _make_plain_pdf(src_path)

    dst = pikepdf.Pdf.new()
    dst_page = pikepdf.Page(dst.add_blank_page(page_size=(612, 792)))
    xobj = dst.make_stream(b"q Q")
    xobj.stream_dict["/Type"] = pikepdf.Name("/XObject")
    xobj.stream_dict["/Subtype"] = pikepdf.Name("/Form")
    xobj.stream_dict["/BBox"] = pikepdf.Array([0, 0, 612, 792])

    with pikepdf.open(src_path) as src:
        result = migrate_tags_for_xobject(src, 0, xobj, dst, dst_page.obj)

    assert result is False
    # No struct tree should be created
    assert "/StructTreeRoot" not in dst.Root


# ---------------------------------------------------------------------------
# 16. migrate_tags_for_xobject: tagged source populates MCR/Stm + ParentTree
# ---------------------------------------------------------------------------

def test_migrate_tags_xobj_tagged_basic(tmp_path: Path) -> None:
    """migrate_tags_for_xobject must:
    - return True
    - add /StructParents to the XObject stream dict
    - write a ParentTree slot for that XObject
    - add MCR dicts with /Stm to the dst struct tree
    """
    src_path = tmp_path / "tagged.pdf"
    out_path = tmp_path / "out.pdf"
    _make_tagged_pdf_with_content(src_path, mcids=[0, 1])

    dst = pikepdf.Pdf.new()
    dst_page = pikepdf.Page(dst.add_blank_page(page_size=(612, 792)))
    xobj = dst.make_stream(b"/Slide <</MCID 0>> BDC (t) Tj EMC\n/Slide <</MCID 1>> BDC (u) Tj EMC")
    xobj.stream_dict["/Type"] = pikepdf.Name("/XObject")
    xobj.stream_dict["/Subtype"] = pikepdf.Name("/Form")
    xobj.stream_dict["/BBox"] = pikepdf.Array([0, 0, 612, 792])
    xobj = dst.make_indirect(xobj)
    # Register in page resources so _xobj_struct_parents_slot helper can find it
    dst_page.obj["/Resources"] = pikepdf.Dictionary()
    dst_page.obj["/Resources"]["/XObject"] = pikepdf.Dictionary()
    dst_page.obj["/Resources"]["/XObject"]["/Xobj0"] = xobj

    with pikepdf.open(src_path) as src:
        result = migrate_tags_for_xobject(src, 0, xobj, dst, dst_page.obj)

    assert result is True
    dst.save(out_path)

    sp = _xobj_struct_parents_slot(out_path)
    assert sp >= 0, "XObject must have /StructParents set"
    assert _parent_tree_has_slot(out_path, sp), f"ParentTree must have slot {sp}"
    assert _has_mcr_with_stm(out_path), "Struct tree must contain MCR dicts with /Stm"


# ---------------------------------------------------------------------------
# 17. migrate_tags_for_xobject: slot does not collide with page slots
# ---------------------------------------------------------------------------

def test_migrate_tags_xobj_slot_no_collision(tmp_path: Path) -> None:
    """The XObject /StructParents slot must be strictly above all page slots."""
    src_path = tmp_path / "tagged.pdf"
    out_path = tmp_path / "out.pdf"
    _make_tagged_pdf_with_content(src_path, mcids=[0])

    # Build a dst with 2 pages, each consuming a ParentTree slot
    dst = pikepdf.Pdf.new()
    _ensure_dst_struct_root_helper = None  # use tagtree internals via merge_page_tags
    for i in range(2):
        p = dst.add_blank_page(page_size=(612, 792))
        with pikepdf.open(src_path) as src:
            merge_page_tags(src, dst, dst.pages[-1].obj, src_page_index=0)

    # Now add an XObject slot
    xobj = dst.make_stream(b"/Slide <</MCID 0>> BDC (x) Tj EMC")
    xobj.stream_dict["/Type"] = pikepdf.Name("/XObject")
    xobj.stream_dict["/Subtype"] = pikepdf.Name("/Form")
    xobj.stream_dict["/BBox"] = pikepdf.Array([0, 0, 612, 792])
    xobj = dst.make_indirect(xobj)

    with pikepdf.open(src_path) as src:
        migrate_tags_for_xobject(src, 0, xobj, dst, dst.pages[0].obj)

    dst.save(out_path)

    xobj_slot = _xobj_struct_parents_slot(out_path)
    page_slots = _parent_tree_slots(out_path)
    # XObject slot must not overlap with any page slot
    assert xobj_slot not in page_slots, (
        f"XObject slot {xobj_slot} collides with page slots {page_slots}"
    )


# ---------------------------------------------------------------------------
# 18. merge_from_xml Mode B end-to-end: XObject gets /StructParents + MCR/Stm
# ---------------------------------------------------------------------------

def test_merge_xml_mode_b_has_xobj_tags(tmp_path: Path) -> None:
    """After Mode B merge, the output must contain MCR dicts with /Stm — i.e.
    the main-page tag tree was migrated into the XObject."""
    main_path = tmp_path / "main.pdf"
    ext_path  = tmp_path / "blob-ext.pdf"
    out_path  = tmp_path / "out.pdf"
    _make_tagged_pdf_with_content(main_path, mcids=[0, 1])
    _make_plain_pdf(ext_path, width=960, height=540)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="item-b" BlobId="blob-ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="1">1</slideLocalId>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    xml_path.write_bytes(xml_content.encode("utf-16"))

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    assert out_path.exists()
    assert _has_mcr_with_stm(out_path), "Mode B output must have MCR dicts with /Stm"
    sp = _xobj_struct_parents_slot(out_path)
    assert sp >= 0
    assert _parent_tree_has_slot(out_path, sp)


# ---------------------------------------------------------------------------
# 19. merge_from_xml Mode A end-to-end: XObject gets /StructParents + MCR/Stm
# ---------------------------------------------------------------------------

def test_merge_xml_mode_a_has_xobj_tags(tmp_path: Path) -> None:
    """After Mode A merge, the output must contain MCR dicts with /Stm."""
    main_path = tmp_path / "main.pdf"
    ext_path  = tmp_path / "blob-ext.pdf"
    out_path  = tmp_path / "out.pdf"
    _make_tagged_pdf_with_content(main_path, mcids=[0])
    _make_plain_pdf(ext_path, width=612, height=792)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="false" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="item-a" BlobId="blob-ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="1">1</slideLocalId>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    xml_path.write_bytes(xml_content.encode("utf-16"))

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    assert out_path.exists()
    assert _has_mcr_with_stm(out_path), "Mode A output must have MCR dicts with /Stm"
    sp = _xobj_struct_parents_slot(out_path)
    assert sp >= 0
    assert _parent_tree_has_slot(out_path, sp)
