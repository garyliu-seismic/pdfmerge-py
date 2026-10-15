"""Synthetic multi-page PDFs for the page-op tests."""
from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String


def build_pdf(path: Path, n: int = 6, *, tagged: bool = True, forms: bool = True,
              outlines: bool = True, title: str = "Synthetic Title", lang: str = "en-US",
              text_prefix: str = "Page") -> Path:
    """n pages; page i shows text '<prefix> <i+1>' (MCID 0 + MCID 1), a forward Link annot.

    Tags : Document > Sect(per page) > {P(mcid0), Span(mcid1), Link(OBJR)}
    Forms: text field 'f<i>' per page + field 'shared' with widgets on first & last page
    Outl.: item per page, even items have a child pointing at the next page
    Dests: name tree p<i>;  Labels: pages 0-1 roman, 2.. decimal starting at 1
    """
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    pdf.Root.Lang = String(lang)
    pdf.docinfo["/Title"] = String(title)

    pages = []
    for i in range(n):
        pdf.add_blank_page(page_size=(200, 200))
        pages.append(pdf.pages[i])

    # struct skeleton
    if tagged:
        root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root))
        root.K = doc
        pdf.Root.StructTreeRoot = root
        pdf.Root.MarkInfo = Dictionary(Marked=True)
        doc.K = Array()
        nums = Array()

    shared_widgets = []
    all_fields = Array()
    for i, page in enumerate(pages):
        txt = f"{text_prefix} {i + 1}"
        if tagged:
            body = (f"/P <</MCID 0>> BDC BT /F1 12 Tf 10 150 Td ({txt}) Tj ET EMC\n"
                    f"/Span <</MCID 1>> BDC BT /F1 10 Tf 10 120 Td (second {i + 1}) Tj ET EMC\n")
        else:
            body = f"BT /F1 12 Tf 10 150 Td ({txt}) Tj ET\n"
        page.Contents = pdf.make_stream(body.encode())
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        annots = Array()

        link = pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=Array([10, 10, 60, 30]), Border=Array([0, 0, 0]),
            P=page.obj, Contents=String(f"to next {i}")))
        if i + 1 < n:
            link.Dest = Array([pages[i + 1].obj, Name.Fit])
        annots.append(link)

        if forms:
            w = pdf.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx, T=String(f"f{i}"),
                V=String(f"val{i}"), Rect=Array([70, 10, 150, 30]), P=page.obj, F=4))
            annots.append(w)
            all_fields.append(w)
            if i in (0, n - 1):
                sw = pdf.make_indirect(Dictionary(
                    Type=Name.Annot, Subtype=Name.Widget, Rect=Array([70, 40, 150, 60]),
                    P=page.obj, F=4))
                annots.append(sw)
                shared_widgets.append(sw)
        page.Annots = annots

        if tagged:
            sect = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Sect, P=doc, Pg=page.obj))
            p1 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.P, P=sect, Pg=page.obj, K=0))
            p2 = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Span, P=sect, Pg=page.obj, K=1))
            lk = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Link, P=sect, Pg=page.obj))
            lk.K = Dictionary(Type=Name.OBJR, Obj=link, Pg=page.obj)
            sect.K = Array([p1, p2, lk])
            doc.K.append(sect)
            page.StructParents = i
            link.StructParent = n + i
            nums.append(i)
            nums.append(Array([p1, p2]))
            nums.append(n + i)
            nums.append(lk)

    if tagged:
        pt = pdf.make_indirect(Dictionary(Nums=nums))
        root.ParentTree = pt
        root.ParentTreeNextKey = 2 * n

    if forms:
        shared = pdf.make_indirect(Dictionary(FT=Name.Tx, T=String("shared"), V=String("S")))
        shared.Kids = Array(shared_widgets)
        for sw in shared_widgets:
            sw.Parent = shared
        all_fields.append(shared)
        pdf.Root.AcroForm = Dictionary(Fields=all_fields, DA=String("/Helv 0 Tf 0 g"))

    # named destinations
    names = Array()
    for i in range(n):
        names.append(String(f"p{i}"))
        names.append(Array([pages[i].obj, Name.Fit]))
    pdf.Root.Names = Dictionary(Dests=Dictionary(Names=names))

    # page labels
    if n > 2:
        pdf.Root.PageLabels = Dictionary(Nums=Array([
            0, Dictionary(S=Name.r),
            2, Dictionary(S=Name.D, St=1),
        ]))

    if outlines:
        ol = pdf.make_indirect(Dictionary(Type=Name.Outlines))
        items = []
        for i in range(n):
            it = pdf.make_indirect(Dictionary(Title=String(f"Item {i}"), Parent=ol,
                                              Dest=Array([pages[i].obj, Name.Fit])))
            items.append(it)
            if i % 2 == 0 and i + 1 < n:
                ch = pdf.make_indirect(Dictionary(Title=String(f"Sub {i}"), Parent=it,
                                                  Dest=Array([pages[i + 1].obj, Name.Fit])))
                it.First = ch
                it.Last = ch
                it.Count = 1
        for a, b in zip(items, items[1:]):
            a.Next = b
            b.Prev = a
        ol.First = items[0]
        ol.Last = items[-1]
        ol.Count = len(items) + sum(1 for i in range(n) if i % 2 == 0 and i + 1 < n)
        pdf.Root.Outlines = ol

    pdf.save(path, min_version="1.7")
    return path


def page_text(page: pikepdf.Page) -> str:
    """Concatenated text of the (single) content stream, for content assertions."""
    data = page.Contents.read_bytes() if not isinstance(page.Contents, pikepdf.Array) else b"".join(
        c.read_bytes() for c in page.Contents)
    return data.decode("latin-1")
