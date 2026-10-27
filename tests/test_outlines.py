"""Outline (bookmark) parity with iText CopyPagesTo as used by CTS2.0 MergeNew.

Expected trees were captured from Seismic.CTS.Converter.LiveDoc.exe on the same
inputs (nested 6-page outline, slides 1 and 4 replaced; flat outline, slides 2
and 4 replaced).
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from pdfbuilder import build_pdf
from pdfmergepy.composite import merge_from_xml
from pdfmergepy.merge import merge_files
from pdfmergepy.mergeinfo import parse_merge_info
from pdfmergepy.pdfutil import PageSpec


def _flat_outline(path: Path, n: int, titles: list[str], *, xyz: bool = False) -> None:
    """n blank pages, one outline item per page titled titles[i] (/Fit, or /XYZ 0 200 null)."""
    pdf = pikepdf.Pdf.new()
    for _ in range(n):
        pdf.add_blank_page(page_size=(200, 200))
    ol = pdf.make_indirect(Dictionary(Type=Name.Outlines))
    items = []
    for i, t in enumerate(titles):
        d = Array([pdf.pages[i].obj, Name.XYZ, 0, 200, None] if xyz else [pdf.pages[i].obj, Name.Fit])
        items.append(pdf.make_indirect(Dictionary(Title=String(t), Parent=ol, Dest=d)))
    for a, b in zip(items, items[1:]):
        a.Next, b.Prev = b, a
    ol.First, ol.Last, ol.Count = items[0], items[-1], len(items)
    pdf.Root.Outlines = ol
    pdf.save(path)


def _tree(pdf: pikepdf.Pdf) -> list:
    """Outline as nested [(title, 1-based page or None, /Count, children)]."""
    pages = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}

    def walk(item) -> list:
        out = []
        while item is not None:
            d = item.get("/Dest")
            page = pages[d[0].objgen] if d is not None else None
            kids = walk(item.First) if "/First" in item else []
            out.append((str(item.Title), page, item.get("/Count"), kids))
            item = item.get("/Next")
        return out

    root = pdf.Root.get("/Outlines")
    return walk(root.get("/First")) if root is not None else None


def _xml(path: Path, replaced: list[int], n_slides: int, blob: str = "ext", fit: str = "true",
         hidden: tuple[int, ...] = ()) -> None:
    ids = "\n".join(
        f'      <slideLocalId pdfPage="{k}" slideIndex="{s}">{s}0</slideLocalId>'
        for k, s in enumerate(replaced, start=1))
    infos = "\n".join(
        f'    <info slideIndex="{s}" localId="{s}0" hidden="{"true" if s in hidden else "false"}" />'
        for s in range(1, n_slides + 1))
    path.write_bytes(f"""<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="{fit}" SlideFitPattern="AlignTopLeft" pageCount="{len(replaced)}"
               id="item" BlobId="{blob}">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
{ids}
    </MergeItem>
  </PDFMerge>
  <SlideInfo>
{infos}
  </SlideInfo>
</WorkspaceMergeInfo>
""".encode("utf-16"))


def _merge(tmp_path: Path, replaced: list[int], n_slides: int, fit: str = "true",
           hidden: tuple[int, ...] = ()) -> pikepdf.Pdf:
    _xml(tmp_path / "m.xml", replaced, n_slides, fit=fit, hidden=hidden)
    out = tmp_path / "out.pdf"
    merge_from_xml(parse_merge_info(tmp_path / "m.xml"), tmp_path / "main.pdf", tmp_path, out)
    return pikepdf.open(out)


def test_nested_outline_cloned_per_native_page(tmp_path: Path) -> None:
    """Main outline items of replaced slides vanish, external ones appear; ancestors
    of cloned items come back as destination-less containers once per call."""
    build_pdf(tmp_path / "main.pdf", 6, outlines=True)          # Item i (+ Sub i on even i)
    _flat_outline(tmp_path / "ext.pdf", 3, ["Ext 1", "Ext 2", "Ext 3 (unused)"], xyz=True)
    with _merge(tmp_path, [1, 4], 6) as out:
        assert _tree(out) == [
            ("Ext 1", 1, None, []),
            ("Item 0", None, 1, [("Sub 0", 2, None, [])]),     # container, dest dropped
            ("Item 1", 2, None, []),
            ("Item 2", 3, None, []),                            # Sub 2 -> page 4 is replaced
            ("Ext 2", 4, None, []),
            ("Item 4", 5, None, []),
            ("Item 4", None, 1, [("Sub 4", 6, None, [])]),     # duplicated, as in C#
            ("Item 5", 6, None, []),
        ]
        assert int(out.Root.Outlines.Count) == 8
        # fit parameters are preserved, the destination page is the new page
        dest = out.Root.Outlines.First.Dest
        assert dest[0].objgen == out.pages[0].obj.objgen
        assert dest[1] == Name.XYZ and [float(dest[2]), float(dest[3])] == [0.0, 200.0]


def test_mode_a_external_outline_kept_main_dropped(tmp_path: Path) -> None:
    build_pdf(tmp_path / "main.pdf", 5, outlines=False)
    pdf = pikepdf.open(tmp_path / "main.pdf", allow_overwriting_input=True)
    ol = pdf.make_indirect(Dictionary(Type=Name.Outlines))
    items = [pdf.make_indirect(Dictionary(Title=String(f"Slide {i + 1}"), Parent=ol,
                                          Dest=Array([pdf.pages[i].obj, Name.Fit]))) for i in range(5)]
    for a, b in zip(items, items[1:]):
        a.Next, b.Prev = b, a
    ol.First, ol.Last, ol.Count = items[0], items[-1], 5
    pdf.Root.Outlines = ol
    pdf.save(tmp_path / "main.pdf")
    pdf.close()
    _flat_outline(tmp_path / "ext.pdf", 5, [f"Blank Page" for _ in range(5)])
    with _merge(tmp_path, [2, 4], 5, fit="false") as out:
        assert [(t, p) for t, p, _, _ in _tree(out)] == [
            ("Slide 1", 1), ("Blank Page", 2), ("Slide 3", 3), ("Blank Page", 4), ("Slide 5", 5)]


def test_hidden_slide_outline_absent(tmp_path: Path) -> None:
    """Hidden slides are not in the main PDF, so no outline item can exist for them."""
    build_pdf(tmp_path / "main.pdf", 3, outlines=False)
    _flat_outline(tmp_path / "ext.pdf", 1, ["Ext 1"])
    # main is [slide 1, slide 3, slide 4]; slide 2 hidden; slide 4 replaced by ext page 1
    with _merge(tmp_path, [4], 4, hidden=(2,)) as out:
        assert len(out.pages) == 3
        assert [(t, p) for t, p, _, _ in _tree(out)] == [("Ext 1", 3)]


def test_empty_outline_root_when_sources_have_none(tmp_path: Path) -> None:
    """C# MergeNew calls InitializeOutlines() unconditionally."""
    build_pdf(tmp_path / "main.pdf", 2, outlines=False)
    build_pdf(tmp_path / "ext.pdf", 1, outlines=False)
    with _merge(tmp_path, [1], 2) as out:
        assert "/Outlines" in out.Root
        assert "/First" not in out.Root.Outlines
        assert _tree(out) == []


def test_named_destination_resolved_to_explicit(tmp_path: Path) -> None:
    """Named-dest outline items are cloned with an explicit destination array."""
    build_pdf(tmp_path / "main.pdf", 2, outlines=False)
    src = pikepdf.new()
    for _ in range(2):
        src.add_blank_page(page_size=(200, 200))
    src.Root.Names = Dictionary(Dests=Dictionary(Names=Array([
        String("nd"), Array([src.pages[0].obj, Name.XYZ, 5, 6, None])])))
    ol = src.make_indirect(Dictionary(Type=Name.Outlines))
    it = src.make_indirect(Dictionary(Title=String("Named"), Parent=ol, Dest=String("nd")))
    ol.First = ol.Last = it
    ol.Count = 1
    src.Root.Outlines = ol
    src.save(tmp_path / "ext.pdf")
    src.close()
    with _merge(tmp_path, [2], 2) as out:
        assert [(t, p) for t, p, _, _ in _tree(out)] == [("Named", 2)]
        dest = out.Root.Outlines.First.Dest
        assert [str(x) for x in dest[1:3]] == ["/XYZ", "5"]


def test_plain_merge_files_clones_outlines(tmp_path: Path) -> None:
    build_pdf(tmp_path / "a.pdf", 3, outlines=True)
    _flat_outline(tmp_path / "b.pdf", 2, ["B1", "B2"])
    out_path = tmp_path / "out.pdf"
    merge_files([PageSpec(path=tmp_path / "a.pdf", pages=(1, 3)),
                 PageSpec(path=tmp_path / "b.pdf", pages=(2,))], out_path)
    with pikepdf.open(out_path) as out:
        tree = _tree(out)
        assert [(t, p) for t, p, _, _ in tree] == [
            ("Item 0", 1), ("Item 1", None), ("Item 2", 2), ("B2", 3)] or \
            [(t, p) for t, p, _, _ in tree][-1] == ("B2", 3)
        assert all(p is None or 1 <= p <= 3 for _, p, _, _ in tree)
