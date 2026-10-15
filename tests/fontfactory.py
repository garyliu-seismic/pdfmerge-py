"""Test helpers: tiny generated fonts (fontTools FontBuilder) and synthetic PDFs with non-embedded fonts.

Every glyph is a solid box (so a render check can tell an embedded test font from a viewer's
fallback font); advances differ per glyph so /W-based CID verification is discriminating.
No font binaries are committed - everything is generated at test time.
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from pikepdf import Array, Dictionary, Name, String

CHARS = "ABCDEFG"
ADV = {"space": 250, "A": 600, "B": 700, "C": 800, "D": 650, "E": 550, "F": 750, "G": 900}


def _box(pen, adv):
    pen.moveTo((0, 0))
    pen.lineTo((0, 700))
    pen.lineTo((adv, 700))
    pen.lineTo((adv, 0))
    pen.closePath()


def make_font(path: Path, family="TestSans", style="Regular", ps=None, cff=False, bold=False,
              italic=False, fstype=0, reverse=False, upem=1000) -> Path:
    """Generate a tiny TTF (glyf) or CFF-OTF. ``reverse`` flips glyph order (GID mismatch tests)."""
    ps = ps or f"{family.replace(' ', '')}-{style.replace(' ', '')}"
    names = [".notdef", "space"] + list(CHARS)
    if reverse:
        names = [".notdef"] + list(reversed(names[1:]))
    fb = FontBuilder(upem, isTTF=not cff)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({0x20: "space", **{ord(c): c for c in CHARS}})
    adv = {"space": ADV["space"], **{c: ADV[c] for c in CHARS}, ".notdef": 500}
    if cff:
        glyphs = {}
        for n in names:
            pen = T2CharStringPen(adv[n], None)
            if n != "space":
                _box(pen, adv[n] if n != ".notdef" else 400)
            glyphs[n] = pen.getCharString()
        fb.setupCFF(ps, {"FullName": f"{family} {style}"}, glyphs, {})
    else:
        glyphs = {}
        for n in names:
            pen = TTGlyphPen(None)
            if n != "space":
                _box(pen, adv[n] if n != ".notdef" else 400)
            glyphs[n] = pen.glyph()
        fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (adv[n], 0) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": style, "psName": ps,
                       "fullName": f"{family} {style}", "uniqueFontIdentifier": ps})
    sel = (0x20 if bold else 0) | (0x01 if italic else 0) | (0x40 if not (bold or italic) else 0)
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWeightClass=700 if bold else 400,
                sCapHeight=700, fsType=fstype, fsSelection=sel, version=4)
    fb.setupPost()
    fb.font["head"].macStyle = (1 if bold else 0) | (2 if italic else 0)
    fb.save(str(path))
    return path


def gid(char: str, reverse=False) -> int:
    names = [".notdef", "space"] + list(CHARS)
    if reverse:
        names = [".notdef"] + list(reversed(names[1:]))
    return names.index("space" if char == " " else char)


# ---------------------------------------------------------------------------
# synthetic PDFs
# ---------------------------------------------------------------------------

def widths_array(first=32, last=255):
    out = []
    for c in range(first, last + 1):
        ch = chr(c)
        out.append(ADV["space"] if ch == " " else ADV.get(ch, 0))
    return Array(out)


def tounicode(pdf, cid_to_char: dict[int, str]):
    lines = ["/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
             "1 begincodespacerange <0000> <FFFF> endcodespacerange",
             f"{len(cid_to_char)} beginbfchar"]
    for cid, ch in cid_to_char.items():
        lines.append("<%04X> <%04X>" % (cid, ord(ch)))
    lines += ["endbfchar", "endcmap CMapName currentdict /CMap defineresource pop end end"]
    return pdf.make_stream("\n".join(lines).encode())


def simple_truetype(pdf, base="TestSans", descriptor=True, widths=True, subtype="/TrueType", encoding="/WinAnsiEncoding",
                    flags=32):
    d = Dictionary(Type=Name.Font, Subtype=Name(subtype), BaseFont=Name("/" + base))
    if encoding:
        d.Encoding = Name(encoding)
    if widths:
        d.FirstChar, d.LastChar, d.Widths = 32, 255, widths_array()
    if descriptor:
        d.FontDescriptor = pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/" + base),
                                                         Flags=flags))
    d.ToUnicode = tounicode(pdf, {ord(c): c for c in "ABC"})
    return pdf.make_indirect(d)


def type0_identity(pdf, base="TestSans", reverse=False, with_w=True, with_tu=True, ordering="Identity",
                   encoding="/Identity-H", cid_subtype="/CIDFontType2"):
    gids = {c: gid(c, reverse) for c in CHARS}
    cidf = Dictionary(Type=Name.Font, Subtype=Name(cid_subtype), BaseFont=Name("/" + base),
                      CIDSystemInfo=Dictionary(Registry=String("Adobe"), Ordering=String(ordering), Supplement=0),
                      FontDescriptor=pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/" + base),
                                                                  Flags=4)),
                      )
    if with_w:
        cidf.DW = 1000
    if cid_subtype == "/CIDFontType2":
        cidf.CIDToGIDMap = Name.Identity
    if with_w:
        # widths as the ORIGINAL font (non-reversed order) would report them
        w = []
        for c in CHARS:
            w += [gid(c, False), Array([ADV[c]])]
        cidf.W = Array(w)
    top = Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name("/" + base), Encoding=Name(encoding),
                     DescendantFonts=Array([pdf.make_indirect(cidf)]))
    if with_tu:
        top.ToUnicode = tounicode(pdf, {gid(c, False): c for c in CHARS})
    return pdf.make_indirect(top)


def hex_text(chars: str, reverse=False) -> str:
    return "<" + "".join("%04X" % gid(c, reverse) for c in chars) + ">"


def page_content(font_name="F1", size=48, text="AB", cid=False, x=20, y=20, reverse=False):
    t = hex_text(text, reverse) if cid else f"({text})"
    return f"BT /{font_name} {size} Tf {x} {y} Td {t} Tj ET".encode()


def make_pdf(path: Path, *, page_fonts=None, inherited_fonts=None, form_fonts=None, annot_fonts=None,
             type3=False, acroform_dr=None, tagged=False, size=(200, 100), content_cid=False, encrypt=None) -> Path:
    """Build a 1-page PDF. ``*_fonts`` map resource name -> font object factory(pdf) result."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=size)
    contents = b""
    if inherited_fonts:
        res = Dictionary(Font=Dictionary({("/" + k): v for k, v in inherited_fonts.items()}))
        pdf.Root.Pages.Resources = res
        if "Resources" in page.obj:
            del page.obj["Resources"]
        first = next(iter(inherited_fonts))
        contents += page_content(first, cid=content_cid)
    if page_fonts:
        page.obj.Resources = Dictionary(Font=Dictionary({("/" + k): v for k, v in page_fonts.items()}))
        first = next(iter(page_fonts))
        contents += page_content(first, cid=content_cid)
    if form_fonts:
        fdict = Dictionary({("/" + k): v for k, v in form_fonts.items()})
        form = pdf.make_stream(page_content(next(iter(form_fonts)), cid=content_cid, y=60))
        form.Type, form.Subtype = Name.XObject, Name.Form
        form.BBox = Array([0, 0, size[0], size[1]])
        form.Resources = Dictionary(Font=fdict)
        if "/Resources" not in page.obj:
            page.obj.Resources = Dictionary()
        xo = page.obj.Resources.get("/XObject") or Dictionary()
        xo.Fm0 = form
        page.obj.Resources.XObject = xo
        contents += b"/Fm0 Do\n"
    if annot_fonts:
        ap = pdf.make_stream(page_content(next(iter(annot_fonts)), size=12, cid=content_cid))
        ap.Type, ap.Subtype = Name.XObject, Name.Form
        ap.BBox = Array([0, 0, 80, 20])
        ap.Resources = Dictionary(Font=Dictionary({("/" + k): v for k, v in annot_fonts.items()}))
        annot = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.FreeText, Rect=Array([10, 70, 90, 90]),
                                             Contents=String("x"), AP=Dictionary(N=ap)))
        page.obj.Annots = Array([annot])
    if type3:
        cp = pdf.make_stream(b"1000 0 0 0 750 750 d1 0 0 750 750 re f")
        t3res = Dictionary(Font=Dictionary(Inner=simple_truetype(pdf, "InnerFont")))
        t3 = Dictionary(Type=Name.Font, Subtype=Name.Type3, FontBBox=Array([0, 0, 750, 750]),
                        FontMatrix=Array([0.001, 0, 0, 0.001, 0, 0]), CharProcs=Dictionary(a=cp),
                        Encoding=Dictionary(Type=Name.Encoding, Differences=Array([97, Name.a])),
                        FirstChar=97, LastChar=97, Widths=Array([1000]), Resources=t3res)
        if "/Resources" not in page.obj:
            page.obj.Resources = Dictionary()
        fd = page.obj.Resources.get("/Font") or Dictionary()
        fd.T3 = pdf.make_indirect(t3)
        page.obj.Resources.Font = fd
    if acroform_dr:
        pdf.Root.AcroForm = Dictionary(Fields=Array(), DR=Dictionary(Font=Dictionary({("/" + k): v for k, v in acroform_dr.items()})))
    if tagged:
        pdf.Root.MarkInfo = Dictionary(Marked=True)
        pdf.Root.StructTreeRoot = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot, K=Array(),
                                                                ParentTree=Dictionary(Nums=Array())))
    page.obj.Contents = pdf.make_stream(contents or b"q Q")
    kwargs = {}
    if encrypt:
        import pikepdf as pk
        kwargs["encryption"] = pk.Encryption(owner=encrypt, user=encrypt, R=4)
    pdf.save(path, **kwargs)
    return path
