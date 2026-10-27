"""Struct-tree parity of merge-xml with the C# merger (PdfMerger + PdfMergeTagHelper).

Reference behaviour (C# HEAD): a replaced slide keeps the *external* page's tags
(iText CopyPagesTo copies page + struct elements) and the main page's tags are
migrated on top as XObject MCRs.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdfbuilder import build_pdf
from pdfmergepy.composite import merge_from_xml
from pdfmergepy.mergeinfo import parse_merge_info
from tagcheck import tag_problems


def _xml(path: Path, replaced: dict[int, int], n_slides: int, *, fit: str) -> None:
    ids = "\n".join(
        f'      <slideLocalId pdfPage="{pg}" slideIndex="{s}">{s}0</slideLocalId>'
        for s, pg in replaced.items())
    infos = "\n".join(f'    <info slideIndex="{s}" localId="{s}0" hidden="false" />'
                      for s in range(1, n_slides + 1))
    path.write_bytes(f"""<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="{fit}" SlideFitPattern="AlignTopLeft" pageCount="{len(replaced)}"
               id="item" BlobId="ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
{ids}
    </MergeItem>
  </PDFMerge>
  <SlideInfo>
{infos}
  </SlideInfo>
</WorkspaceMergeInfo>
""".encode("utf-16"))


def _run(tmp_path: Path, replaced: dict[int, int], n_main: int, n_ext: int, fit: str,
         *, main_kw: dict | None = None, ext_kw: dict | None = None) -> Path:
    build_pdf(tmp_path / "main.pdf", n_main, **(main_kw or {}))
    build_pdf(tmp_path / "ext.pdf", n_ext, text_prefix="Ext", **(ext_kw or {}))
    _xml(tmp_path / "m.xml", replaced, n_main, fit=fit)
    out = tmp_path / "out.pdf"
    merge_from_xml(parse_merge_info(tmp_path / "m.xml"), tmp_path / "main.pdf", tmp_path, out)
    return out


def _problems(pdf: pikepdf.Pdf) -> dict:
    """tag_problems minus ``orphan_page_objects``: a replaced slide's main page is copied
    as a Form XObject and drags its (unreferenced) /Page dict along via the annotations'
    /P, on master as well; unrelated to the struct tree."""
    bad = dict(tag_problems(pdf))
    bad.pop("orphan_page_objects", None)
    return bad


def _kids(e) -> list:
    k = e.get("/K")
    return [] if k is None else (list(k) if isinstance(k, pikepdf.Array) else [k])


def _is_elem(k) -> bool:
    return isinstance(k, pikepdf.Dictionary) and k.get("/Type") not in ("/MCR", "/OBJR")


def _document(pdf: pikepdf.Pdf):
    docs = [k for k in _kids(pdf.Root.StructTreeRoot) if _is_elem(k) and k.S == "/Document"]
    assert len(docs) == 1
    return docs[0]


def _walk(e):
    yield e
    for k in _kids(e):
        if _is_elem(k):
            yield from _walk(k)


def _mcr_kinds(e) -> set[str]:
    """'page' for an MCR/MCID on the page content, 'xobj' for one with /Stm."""
    out = set()
    for k in _kids(e):
        if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR":
            out.add("xobj" if "/Stm" in k else "page")
        elif isinstance(k, int):
            out.add("page")
    return out


def _page_no(pdf: pikepdf.Pdf, e) -> int | None:
    pg = e.get("/Pg")
    if pg is None:
        return None
    return next((i + 1 for i, p in enumerate(pdf.pages) if p.obj.objgen == pg.objgen), None)


@pytest.mark.parametrize("fit", ["true", "false"])
def test_replaced_slide_keeps_external_tags(tmp_path: Path, fit: str) -> None:
    """The external page's P/Span elements are present on the replaced page, ahead of the
    main page's migrated elements, with a valid ParentTree entry for each."""
    out = _run(tmp_path, {2: 1}, n_main=3, n_ext=2, fit=fit)
    with pikepdf.open(out) as pdf:
        assert _problems(pdf) == {}
        # external leaves (page-content MCRs) first, main leaves (XObject MCRs) after them
        leaves = [e for e in _walk(_document(pdf))
                  if e.S in ("/P", "/Span") and _page_no(pdf, e) == 2]
        assert [sorted(_mcr_kinds(e)) for e in leaves] == [["page"], ["page"], ["xobj"], ["xobj"]]
        # page 2 ParentTree entry covers the *external* content (MCIDs 0 and 1)
        arr = pdf.Root.StructTreeRoot.ParentTree.Nums
        slot = int(pdf.pages[1].obj.StructParents)
        entry = next(arr[i + 1] for i in range(0, len(arr), 2) if int(arr[i]) == slot)
        assert [e.S for e in entry] == ["/P", "/Span"]
        # the XObject has its own, different slot (C# EnsureUniqueXObjectStructParents)
        xo = next(iter(pdf.pages[1].obj.Resources.XObject.values()))
        assert int(xo.StructParents) != slot


def test_mode_b_main_tags_attach_to_document_mode_a_to_page_owner(tmp_path: Path) -> None:
    """Mode B (C# explicitAttachPoint = Document): main's Sect is a sibling of the external's.
    Mode A (FindPageOwnerStructElem): main's Sect nests under the external's topmost element."""
    b = tmp_path / "b"; b.mkdir()
    a = tmp_path / "a"; a.mkdir()
    out_b = _run(b, {2: 1}, 3, 2, "true")
    out_a = _run(a, {2: 1}, 3, 2, "false")
    with pikepdf.open(out_b) as pdf:
        top = [e for e in _kids(_document(pdf)) if _is_elem(e) and e.S == "/Sect" and _page_no(pdf, e) == 2]
        assert len(top) == 2                       # external Sect + main Sect, siblings
    with pikepdf.open(out_a) as pdf:
        top = [e for e in _kids(_document(pdf)) if _is_elem(e) and e.S == "/Sect" and _page_no(pdf, e) == 2]
        assert len(top) == 1                       # main's Sect is nested inside the external's
        nested = [e for e in _kids(top[0]) if _is_elem(e) and e.S == "/Sect"]
        assert len(nested) == 1 and "xobj" in {k for c in _walk(nested[0]) for k in _mcr_kinds(c)}


def test_untagged_external_leaves_no_stale_structparents(tmp_path: Path) -> None:
    """An untagged external page contributes no tags and its copied /StructParents is dropped."""
    out = _run(tmp_path, {2: 1}, 3, 2, "true", ext_kw={"tagged": False})
    with pikepdf.open(out) as pdf:
        assert _problems(pdf) == {}
        page2 = pdf.pages[1].obj
        assert "/StructParents" not in page2
        # only main's migrated tags (XObject MCRs) exist for page 2
        leaves = [e for e in _walk(_document(pdf)) if e.S in ("/P", "/Span") and _page_no(pdf, e) == 2]
        assert leaves and all(_mcr_kinds(e) == {"xobj"} for e in leaves)


# ---------------------------------------------------------------------------
# Plain-copy mirror: source order, Link OBJRs, empty table cells, /Kids ParentTree
# ---------------------------------------------------------------------------

from pikepdf import Array, Dictionary, Name, String  # noqa: E402

from pdfmergepy.merge import merge_files  # noqa: E402
from pdfmergepy.pdfutil import PageSpec  # noqa: E402


def _tagged_page_pdf(path: Path, build, *, kids_parent_tree: bool = False) -> Path:
    """One 200x200 page whose struct tree is produced by ``build(pdf, page, doc) -> {mcid: elem}``."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    page = pdf.pages[0]
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root, K=Array()))
    root.K = doc
    pdf.Root.StructTreeRoot = root
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.Root.Lang = String("en-US")
    mcid_elems = build(pdf, page, doc)
    n = max(mcid_elems) + 1 if mcid_elems else 0
    body = "".join(f"/P <</MCID {i}>> BDC BT /F1 10 Tf 10 {180 - 10 * i} Td (t{i}) Tj ET EMC\n"
                   for i in range(n))
    page.Contents = pdf.make_stream(body.encode())
    page.StructParents = 0
    arr = Array([mcid_elems.get(i) for i in range(n)])
    nums = Array([0, arr])
    for key, elem in getattr(page, "_extra_parent_entries", []):   # annotation entries
        nums.append(key)
        nums.append(elem)
    if kids_parent_tree:
        root.ParentTree = pdf.make_indirect(Dictionary(Kids=Array([
            pdf.make_indirect(Dictionary(Nums=nums, Limits=Array([0, 0])))])))
    else:
        root.ParentTree = pdf.make_indirect(Dictionary(Nums=nums))
    root.ParentTreeNextKey = 1
    pdf.save(path)
    return path


def _el(pdf, role, parent, page, *kids):
    e = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name(role), P=parent, Pg=page.obj))
    if kids:
        e.K = kids[0] if len(kids) == 1 else Array(list(kids))
    return e


def _merged(tmp_path: Path, src: Path) -> pikepdf.Pdf:
    out = tmp_path / "out.pdf"
    merge_files([PageSpec(path=src, pages=(1,))], out)
    return pikepdf.open(out)


def _roles(pdf: pikepdf.Pdf) -> list[str]:
    return [str(e.S) for e in _walk(_document(pdf))]


def test_plain_copy_keeps_source_kid_order(tmp_path: Path) -> None:
    """Kids are mirrored in the source /K order, not sorted by MCID."""
    def build(pdf, page, doc):
        sect = _el(pdf, "/Sect", doc, page)
        second = _el(pdf, "/H1", sect, page, 1)      # MCID 1 first in /K ...
        first = _el(pdf, "/P", sect, page, 0)        # ... MCID 0 after it
        sect.K = Array([second, first])
        doc.K.append(sect)
        return {0: first, 1: second}
    with _merged(tmp_path, _tagged_page_pdf(tmp_path / "s.pdf", build)) as pdf:
        assert _problems(pdf) == {}
        assert _roles(pdf) == ["/Document", "/Sect", "/H1", "/P"]


def test_plain_copy_link_with_objr_is_not_duplicated(tmp_path: Path) -> None:
    """A tagged Link (text MCR + OBJR) is copied once, with an OBJR to the copied annotation
    and a /StructParent for it; fix_link_annots must not wrap the annotation a second time."""
    def build(pdf, page, doc):
        annot = pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=Array([10, 150, 60, 170]),
            Border=Array([0, 0, 0]), P=page.obj, Contents=String("go"), StructParent=1))
        page.Annots = Array([annot])
        p = _el(pdf, "/P", doc, page)
        link = _el(pdf, "/Link", p, page, 0, Dictionary(Type=Name.OBJR, Obj=annot, Pg=page.obj))
        p.K = link
        doc.K.append(p)
        page._extra_parent_entries = [(1, link)]
        return {0: link}
    src = _tagged_page_pdf(tmp_path / "s.pdf", build)
    with _merged(tmp_path, src) as pdf:
        assert _problems(pdf) == {}
        links = [e for e in _walk(_document(pdf)) if e.S == "/Link"]
        assert len(links) == 1
        objrs = [k for k in _kids(links[0]) if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/OBJR"]
        assert len(objrs) == 1
        annot = pdf.pages[0].obj.Annots[0]
        assert objrs[0].Obj.objgen == annot.objgen and objrs[0].Pg.objgen == pdf.pages[0].obj.objgen
        # ParentTree[annot /StructParent] is the (single) Link element, and no clash with the page slot
        key = int(annot.StructParent)
        nums = pdf.Root.StructTreeRoot.ParentTree.Nums
        entries = {int(nums[i]): nums[i + 1] for i in range(0, len(nums), 2)}
        assert entries[key].objgen == links[0].objgen
        assert key != int(pdf.pages[0].obj.StructParents)
        assert [int(nums[i]) for i in range(0, len(nums), 2)] == sorted(entries)   # valid number tree


def test_plain_copy_keeps_empty_table_cells_like_itext(tmp_path: Path) -> None:
    """iText keeps empty TD/TH cells (and rows made only of them) under a Table that has
    content, drops other empty elements and a table without any content."""
    def build(pdf, page, doc):
        table = _el(pdf, "/Table", doc, page)
        tbody = _el(pdf, "/TBody", table, page)
        tr1 = _el(pdf, "/TR", tbody, page)
        td_content = _el(pdf, "/TD", tr1, page)
        p = _el(pdf, "/P", td_content, page, 0)
        td_content.K = p
        td_empty = _el(pdf, "/TD", tr1, page)
        th_empty = _el(pdf, "/TH", tr1, page)
        tr1.K = Array([td_content, td_empty, th_empty])
        tr2 = _el(pdf, "/TR", tbody, page)                  # row of empty cells only
        tr2.K = Array([_el(pdf, "/TD", tr2, page), _el(pdf, "/TD", tr2, page)])
        tr3 = _el(pdf, "/TR", tbody, page)                  # row without any kid: dropped
        tbody.K = Array([tr1, tr2, tr3])
        table.K = tbody
        sect = _el(pdf, "/Sect", doc, page)                 # empty non-cell elements: dropped
        sect.K = Array([_el(pdf, "/P", sect, page), _el(pdf, "/Span", sect, page)])
        dead = _el(pdf, "/Table", doc, page)                # table without content: dropped
        dtr = _el(pdf, "/TR", dead, page)
        dtr.K = _el(pdf, "/TD", dtr, page)
        dead.K = dtr
        doc.K.extend([table, sect, dead])
        return {0: p}
    with _merged(tmp_path, _tagged_page_pdf(tmp_path / "s.pdf", build)) as pdf:
        assert _problems(pdf) == {}
        from collections import Counter
        assert Counter(_roles(pdf)) == Counter({
            "/Document": 1, "/Table": 1, "/TBody": 1, "/TR": 2,
            "/TD": 4, "/TH": 1, "/P": 1})


def test_plain_copy_reads_parent_tree_with_kids(tmp_path: Path) -> None:
    """A /ParentTree that is a /Kids number tree (instead of a flat /Nums) must not lose the tags."""
    def build(pdf, page, doc):
        p = _el(pdf, "/P", doc, page, 0)
        doc.K.append(p)
        return {0: p}
    src = _tagged_page_pdf(tmp_path / "s.pdf", build, kids_parent_tree=True)
    with _merged(tmp_path, src) as pdf:
        assert _problems(pdf) == {}
        assert _roles(pdf) == ["/Document", "/P"]


# ---------------------------------------------------------------------------
# Hostile struct trees: cyclic /K and very deep nesting must not abort the merge
# ---------------------------------------------------------------------------

def _cyclic_pdf(path: Path) -> Path:
    """Document > Sect > P(mcid 0), and the Sect lists itself and a Sect2 that loops back."""
    def build(pdf, page, doc):
        sect = _el(pdf, "/Sect", doc, page)
        sect2 = _el(pdf, "/Sect", sect, page)
        leaf = _el(pdf, "/P", sect, page, 0)
        sect.K = Array([leaf, sect, sect2])
        sect2.K = Array([sect])                       # sect -> sect2 -> sect
        doc.K.append(sect)
        return {0: leaf}
    return _tagged_page_pdf(path, build)


def _deep_pdf(path: Path, depth: int = 3000) -> Path:
    """Document > Div x depth > P(mcid 0): nested far beyond the interpreter recursion limit."""
    def build(pdf, page, doc):
        parent = doc
        for _ in range(depth):
            div = _el(pdf, "/Div", parent, page)
            if parent is doc:
                doc.K.append(div)
            else:
                parent.K = div
            parent = div
        leaf = _el(pdf, "/P", parent, page, 0)
        parent.K = leaf
        return {0: leaf}
    return _tagged_page_pdf(path, build)


def _xml_merge(tmp_path: Path, hostile: Path, *, as_external: bool) -> Path:
    """merge-xml replacing main slide 1 with the hostile PDF (external) or, with
    as_external=False, merging the hostile PDF as the main PDF (replaced slide = XObject path
    for its tags, slide 2 = plain copy)."""
    import shutil
    if as_external:
        build_pdf(tmp_path / "main.pdf", 2)
        shutil.copy(hostile, tmp_path / "ext.pdf")
    else:
        build_pdf(tmp_path / "ext.pdf", 1, text_prefix="Ext")
        shutil.copy(hostile, tmp_path / "main.pdf")
    _xml(tmp_path / "m.xml", {1: 1}, 1 if not as_external else 2, fit="true")
    out = tmp_path / "out.pdf"
    merge_from_xml(parse_merge_info(tmp_path / "m.xml"), tmp_path / "main.pdf", tmp_path, out)
    return out


def _flat_roles(pdf: pikepdf.Pdf) -> list[str]:
    """Roles of the whole tree, iteratively (``_walk`` would hit the recursion limit)."""
    roles, stack = [], [_document(pdf)]
    while stack:
        e = stack.pop()
        roles.append(str(e.S))
        stack.extend(k for k in _kids(e) if _is_elem(k))
    return roles


def _assert_guarded(out: Path) -> None:
    with pikepdf.open(out) as pdf:
        assert "/P" in _flat_roles(pdf)             # the tagged content survived


@pytest.mark.parametrize("make", [_cyclic_pdf, _deep_pdf], ids=["cyclic-K", "deep-3000"])
def test_hostile_struct_tree_merge(tmp_path: Path, make, caplog) -> None:
    """`merge` (plain copy) finishes and keeps the page content tagged (master handled these)."""
    src = make(tmp_path / "s.pdf")
    out = tmp_path / "out.pdf"
    with caplog.at_level("WARNING", logger="pdfmergepy.tagtree"):
        merge_files([PageSpec(path=src, pages=(1,))], out)
    _assert_guarded(out)
    if make is _cyclic_pdf:
        assert any("cyclic" in r.message for r in caplog.records)
        with pikepdf.open(out) as pdf:
            assert _flat_roles(pdf).count("/Sect") == 1          # the loop edges were cut, not followed


@pytest.mark.parametrize("make", [_cyclic_pdf, _deep_pdf], ids=["cyclic-K", "deep-3000"])
@pytest.mark.parametrize("as_external", [True, False], ids=["external", "main"])
def test_hostile_struct_tree_merge_xml(tmp_path: Path, make, as_external: bool) -> None:
    """`merge-xml` finishes too, whether the hostile tree comes from the external PDF
    (plain copy) or from the main PDF."""
    out = _xml_merge(tmp_path, make(tmp_path / "h.pdf"), as_external=as_external)
    _assert_guarded(out)


def test_hostile_struct_tree_cli(tmp_path: Path) -> None:
    """End to end through the CLI entry point: exit status 0 for both commands."""
    from pdfmergepy.cli import main
    src = _cyclic_pdf(tmp_path / "s.pdf")
    assert main(["merge", str(src), "-o", str(tmp_path / "o.pdf")]) == 0
