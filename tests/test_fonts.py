"""Tests for fonts.py / fontmap.py / fontembed.py (list-fonts, embed-fonts, remove-fonts)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from fontTools.cffLib import CFFFontSet
from fontTools.ttLib import TTFont

from fontfactory import (hex_text, make_font, make_pdf, simple_truetype, tounicode, type0_identity, gid)
from pdfmergepy import fonts as fl
from pdfmergepy.cli import main
from pdfmergepy.fontembed import embed_fonts, parse_tounicode, parse_w_array
from pdfmergepy.fontmap import FontCatalog, parse_style

try:
    import pypdfium2 as pdfium
    import numpy  # noqa: F401
except Exception:  # pragma: no cover
    pdfium = None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fontdir(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "TestSans-Regular.ttf", "TestSans", "Regular")
    make_font(d / "TestSans-Bold.ttf", "TestSans", "Bold", bold=True)
    make_font(d / "TestSans-Italic.ttf", "TestSans", "Italic", italic=True)
    make_font(d / "TestSans-BoldItalic.ttf", "TestSans", "Bold Italic", bold=True, italic=True)
    return d


def _fonts(path):
    with pikepdf.open(path) as pdf:
        return fl.list_fonts(pdf)


def _embed(src, dst, catalog, **kw):
    with pikepdf.open(src) as pdf:
        rep = embed_fonts(pdf, catalog, **kw)
        pdf.save(dst)
    return rep


def _catalog_dir(d):
    c = FontCatalog()
    c.add_directory(d)
    return c


def _font_bytes(pdf_path, key="/FontFile2", which=0):
    with pikepdf.open(pdf_path) as pdf:
        fonts = fl.collect_fonts(pdf)
        df = [f for f in fonts][which]
        return df.descriptor[key].read_bytes()


# ---------------------------------------------------------------------------
# parsing helpers / matching
# ---------------------------------------------------------------------------

def test_parse_style_tokens():
    assert parse_style("BoldItalic") == (True, True, "")
    assert parse_style("Bold Oblique") == (True, True, "")
    assert parse_style("Regular") == (False, False, "")
    assert parse_style("Light") == (False, False, "light")
    assert parse_style("BoldMT") == (True, False, "")


def test_parse_tounicode_and_w():
    data = (b"1 beginbfchar <0003> <0041> endbfchar 1 beginbfrange <0010> <0012> <0061> endbfrange "
            b"1 beginbfrange <0020> <0021> [<0042> <0043>] endbfrange")
    m = parse_tounicode(data)
    assert m[3] == 0x41 and m[0x11] == 0x62 and m[0x21] == 0x43
    pdf = pikepdf.Pdf.new()
    w = pikepdf.Array([3, pikepdf.Array([600, 700]), 10, 12, 500])
    assert parse_w_array(w) == {3: 600, 4: 700, 10: 500, 11: 500, 12: 500}


def test_matching_heuristics(fontdir):
    c = _catalog_dir(fontdir)
    ps = lambda n, **k: (lambda r: (r[0].ps_name if r[0] else None, r[1]))(c.match(n, **k))
    assert ps("TestSans-Regular") == ("TestSans-Regular", "postscript")
    assert ps("ABCDEF+TestSans-Bold") == ("TestSans-Bold", "postscript")          # subset prefix stripped
    assert ps("TestSans,Bold")[0] == "TestSans-Bold"                               # C# Family,Style form
    assert ps("TestSans,Italic")[0] == "TestSans-Italic"
    assert ps("TestSans,BoldItalic")[0] == "TestSans-BoldItalic"
    assert ps("TestSans")[0] == "TestSans-Regular"                                 # family only -> Regular
    assert ps("TestSansBold")[0] == "TestSans-Bold"                                # glued style
    assert ps("testsans-bolditalic")[0] == "TestSans-BoldItalic"
    assert ps("Nope") == (None, "")


def test_style_fallback_and_strict(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    make_font(d / "a.ttf", "OnlyReg", "Regular")
    c = _catalog_dir(d)
    f, how = c.match("OnlyReg,Bold")
    assert f is not None and how == "family-style-fallback"      # C#: "Use Regular style instead"
    assert c.match("OnlyReg,Bold", strict=True) == (None, "")


def test_font_map_json_and_text(tmp_path, fontdir):
    sub = tmp_path / "maps"
    sub.mkdir()
    (sub / "font-map.json").write_text(json.dumps({"files": [
        {"path": "../fonts/TestSans-Regular.ttf", "names": ["Corporate Sans", "TestSans-Regular"], "styles": "Regular",
         "type": "TTF", "format": "TrueType"},
        {"path": "../fonts/missing.ttf", "names": ["Ghost"]},
    ]}), encoding="utf-8")
    c = FontCatalog()
    assert c.add_font_map(str(sub / "font-map.json")) == 1
    assert any("not found" in w for w in c.warnings)
    assert c.match("CorporateSans")[0].ps_name == "TestSans-Regular"                # alias from json names
    c2 = FontCatalog()
    assert c2.add_font_map(f"Alias|{fontdir / 'TestSans-Bold.ttf'};{fontdir / 'TestSans-Italic.ttf'}") == 2
    assert c2.match("Alias")[0].ps_name == "TestSans-Bold"


def test_directory_uses_font_map_json_when_present(tmp_path, fontdir):
    (fontdir / "font-map.json").write_text(json.dumps({"files": [{"path": "TestSans-Bold.ttf", "names": ["TestSans"]}]}))
    c = _catalog_dir(fontdir)
    assert [f.ps_name for f in c.fonts] == ["TestSans-Bold"]


# ---------------------------------------------------------------------------
# list-fonts: where fonts are found
# ---------------------------------------------------------------------------

def test_list_fonts_all_locations(tmp_path):
    out = tmp_path / "all.pdf"
    _build_all_locations(out)
    got = {f["name"]: f for f in _fonts(out)}
    assert set(got) >= {"PageFont", "FormFont", "AnnotFont", "InnerFont", "DRFont"}
    assert got["PageFont"]["pages"] == [1] and "Resources" in got["PageFont"]["used_in"][0] or "Font/" in got["PageFont"]["used_in"][0]
    assert any("XObject/Fm0" in u for u in got["FormFont"]["used_in"])
    assert any("Annots[0]/AP/N" in u for u in got["AnnotFont"]["used_in"])
    assert any("Type3" in u for u in got["InnerFont"]["used_in"])
    assert any("AcroForm" in u for u in got["DRFont"]["used_in"])
    assert not any(f["embedded"] for f in got.values() if f["type"] != "Type3")
    t3 = [f for f in got.values() if f["type"] == "Type3"]
    assert len(t3) == 1 and t3[0]["embedded"]


def _build_all_locations(out):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 100))
    page = pdf.pages[0]
    # build using make_pdf's logic but sharing one Pdf: re-implement quickly via helper with factories
    from fontfactory import Array, Dictionary, Name
    page.obj.Resources = Dictionary(Font=Dictionary(PF=simple_truetype(pdf, "PageFont")))
    form = pdf.make_stream(b"BT /FF 12 Tf 0 0 Td (A) Tj ET")
    form.Type, form.Subtype, form.BBox = Name.XObject, Name.Form, Array([0, 0, 10, 10])
    form.Resources = Dictionary(Font=Dictionary(FF=simple_truetype(pdf, "FormFont")))
    page.obj.Resources.XObject = Dictionary(Fm0=form)
    ap = pdf.make_stream(b"BT /AF 12 Tf 0 0 Td (A) Tj ET")
    ap.Type, ap.Subtype, ap.BBox = Name.XObject, Name.Form, Array([0, 0, 10, 10])
    ap.Resources = Dictionary(Font=Dictionary(AF=simple_truetype(pdf, "AnnotFont")))
    page.obj.Annots = Array([pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.FreeText,
                                                          Rect=Array([0, 0, 10, 10]), AP=Dictionary(N=ap)))])
    cp = pdf.make_stream(b"1000 0 0 0 750 750 d1 0 0 750 750 re f")
    t3 = Dictionary(Type=Name.Font, Subtype=Name.Type3, FontBBox=Array([0, 0, 750, 750]),
                    FontMatrix=Array([0.001, 0, 0, 0.001, 0, 0]), CharProcs=Dictionary(a=cp),
                    Encoding=Dictionary(Type=Name.Encoding, Differences=Array([97, Name.a])),
                    FirstChar=97, LastChar=97, Widths=Array([1000]),
                    Resources=Dictionary(Font=Dictionary(In=simple_truetype(pdf, "InnerFont"))))
    page.obj.Resources.Font.T3 = pdf.make_indirect(t3)
    pdf.Root.AcroForm = Dictionary(Fields=Array(), DR=Dictionary(Font=Dictionary(DRF=simple_truetype(pdf, "DRFont"))))
    page.obj.Contents = pdf.make_stream(b"BT /PF 12 Tf 0 0 Td (A) Tj ET /Fm0 Do")
    pdf.save(out)


def test_inherited_resources_found(tmp_path):
    src = tmp_path / "s.pdf"
    pdf = pikepdf.Pdf.new()
    from fontfactory import Dictionary
    pdf.add_blank_page(page_size=(100, 100))
    pdf.Root.Pages.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "InheritedFont")))
    del pdf.pages[0].obj["/Resources"]            # page has no own /Resources -> inherits from /Pages
    pdf.pages[0].obj.Contents = pdf.make_stream(b"BT /F1 12 Tf (A) Tj ET")
    pdf.save(src)
    f = _fonts(src)
    assert [x["name"] for x in f] == ["InheritedFont"] and f[0]["pages"] == [1]


def test_shared_font_listed_once_with_all_pages(tmp_path):
    pdf = pikepdf.Pdf.new()
    from fontfactory import Dictionary
    font = simple_truetype(pdf, "Shared")
    for _ in range(3):
        pdf.add_blank_page(page_size=(50, 50))
        pdf.pages[-1].obj.Resources = Dictionary(Font=Dictionary(F1=font))
    out = tmp_path / "s.pdf"
    pdf.save(out)
    f = _fonts(out)
    assert len(f) == 1 and f[0]["pages"] == [1, 2, 3]


def test_cycle_in_form_xobjects_terminates(tmp_path):
    pdf = pikepdf.Pdf.new()
    from fontfactory import Array, Dictionary, Name
    pdf.add_blank_page(page_size=(50, 50))
    a = pdf.make_stream(b"")
    a.Type, a.Subtype, a.BBox = Name.XObject, Name.Form, Array([0, 0, 1, 1])
    a.Resources = Dictionary(XObject=Dictionary(Self=a), Font=Dictionary(F=simple_truetype(pdf, "Loop")))
    pdf.pages[0].obj.Resources = Dictionary(XObject=Dictionary(A=a))
    out = tmp_path / "c.pdf"
    pdf.save(out)
    assert [f["name"] for f in _fonts(out)] == ["Loop"]


# ---------------------------------------------------------------------------
# embed: simple fonts
# ---------------------------------------------------------------------------

def _pdf_with(tmp_path, build):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 100))
    build(pdf, pdf.pages[0])
    out = tmp_path / "in.pdf"
    pdf.save(out)
    return out


def test_embed_simple_truetype_keeps_encoding_widths_tounicode(tmp_path, fontdir):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf 20 20 Td (AB) Tj ET")

    src = _pdf_with(tmp_path, build)
    out = tmp_path / "out.pdf"
    rep = _embed(src, out, _catalog_dir(fontdir))
    assert (rep.candidates, len(rep.embedded), rep.remaining) == (1, 1, 0)
    with pikepdf.open(out) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        assert str(f.Subtype) == "/TrueType" and str(f.Encoding) == "/WinAnsiEncoding"
        assert int(f.Widths[ord("A") - 32]) == 600 and "/ToUnicode" in f
        fd = f.FontDescriptor
        assert "/FontFile2" in fd and "/FontBBox" in fd and "/Ascent" in fd and "/StemV" in fd
        data = fd.FontFile2.read_bytes()
        assert int(fd.FontFile2.Length1) == len(data)
        assert str(fd.FontName) == "/TestSans-Regular"
        tt = TTFont(io.BytesIO(data))          # parses with fontTools
        assert tt["name"].getDebugName(6) == "TestSans-Regular"
        assert int(fd.Flags) == 32               # existing flags preserved
        assert str(f.BaseFont) == "/TestSans-Regular"
    # nothing orphaned: the old font dict was edited in place (same object id)
    assert _fonts(out)[0]["object"] == _fonts(src)[0]["object"]


def test_embed_type1_standard14_style_generates_descriptor_and_widths(tmp_path, fontdir):
    from fontfactory import Dictionary, Name

    def build(pdf, page):
        f = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/Helvetica"),
                                         Encoding=Name.WinAnsiEncoding))
        page.obj.Resources = Dictionary(Font=Dictionary(F1=f))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf 20 20 Td (AB) Tj ET")

    src = _pdf_with(tmp_path, build)
    out = tmp_path / "out.pdf"
    c = _catalog_dir(fontdir)
    c.add_named(f"Helvetica={fontdir / 'TestSans-Regular.ttf'}")
    rep = _embed(src, out, c)
    assert len(rep.embedded) == 1 and rep.remaining == 0
    with pikepdf.open(out) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        assert "/Widths" in f and int(f.FirstChar) == 32
        assert int(f.Widths[ord("B") - 32]) == 700 and int(f.Widths[ord("A") - 32]) == 600
        assert "/FontFile2" in f.FontDescriptor and "/FontBBox" in f.FontDescriptor


def test_embed_cff_into_simple_font_uses_type1c(tmp_path):
    from fontfactory import Dictionary
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "cff.otf", "CffSans", "Regular", cff=True)

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "CffSans")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf 20 20 Td (AB) Tj ET")

    src = _pdf_with(tmp_path, build)
    out = tmp_path / "out.pdf"
    rep = _embed(src, out, _catalog_dir(d))
    assert len(rep.embedded) == 1
    with pikepdf.open(out) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        assert str(f.Subtype) == "/Type1"                                   # CFF => Type1, not TrueType
        ff = f.FontDescriptor.FontFile3
        assert str(ff.Subtype) == "/Type1C"
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(ff.read_bytes()), None)                      # raw CFF parses
        assert "A" in cff[cff.fontNames[0]].CharStrings


def test_restricted_license_skipped_unless_overridden(tmp_path):
    from fontfactory import Dictionary
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "r.ttf", "Locked", "Regular", fstype=2)

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "Locked")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf (A) Tj ET")

    src = _pdf_with(tmp_path, build)
    rep = _embed(src, tmp_path / "o1.pdf", _catalog_dir(d))
    assert rep.remaining == 1 and "license" in rep.skipped[0]["reason"]
    rep2 = _embed(src, tmp_path / "o2.pdf", _catalog_dir(d), ignore_license=True)
    assert rep2.remaining == 0


def test_embed_is_idempotent_and_skips_embedded(tmp_path, fontdir):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf (A) Tj ET")

    src = _pdf_with(tmp_path, build)
    o1, o2 = tmp_path / "o1.pdf", tmp_path / "o2.pdf"
    _embed(src, o1, _catalog_dir(fontdir))
    rep = _embed(o1, o2, _catalog_dir(fontdir))
    assert rep.candidates == 0 and rep.embedded == []


def test_one_font_stream_shared_by_several_fonts(tmp_path, fontdir):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans"),
                                                        F2=simple_truetype(pdf, "TestSans-Regular")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf (A) Tj /F2 12 Tf (B) Tj ET")

    src = _pdf_with(tmp_path, build)
    out = tmp_path / "o.pdf"
    rep = _embed(src, out, _catalog_dir(fontdir))
    assert len(rep.embedded) == 2
    with pikepdf.open(out) as pdf:
        r = pdf.pages[0].Resources.Font
        assert r.F1.FontDescriptor.FontFile2.objgen == r.F2.FontDescriptor.FontFile2.objgen


# ---------------------------------------------------------------------------
# embed: all locations + untouched tags
# ---------------------------------------------------------------------------

def test_embed_everywhere_and_structure_untouched(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    for n in ("PageFont", "FormFont", "AnnotFont", "InnerFont", "DRFont"):
        make_font(d / f"{n}.ttf", n, "Regular")
    src = tmp_path / "all.pdf"
    _build_all_locations(src)
    tagged = tmp_path / "tagged.pdf"
    with pikepdf.open(src) as pdf:                                  # add a tagged structure
        pdf.Root.MarkInfo = pikepdf.Dictionary(Marked=True)
        pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, K=pikepdf.Array(),
                                                                       ParentTree=pikepdf.Dictionary(Nums=pikepdf.Array())))
        pdf.save(tagged)
    src = tagged
    out = tmp_path / "out.pdf"
    rep = _embed(src, out, _catalog_dir(d))
    assert rep.candidates == 5 and rep.remaining == 0
    with pikepdf.open(src) as a, pikepdf.open(out) as b:
        assert b.Root.StructTreeRoot.objgen == a.Root.StructTreeRoot.objgen
        assert len(b.Root.StructTreeRoot.K) == 0 and bool(b.Root.MarkInfo.Marked)
        assert a.pages[0].Contents.read_bytes() == b.pages[0].Contents.read_bytes()   # content stream untouched
        assert all(f["embedded"] for f in fl.list_fonts(b))


def test_embed_inherited_resources(tmp_path, fontdir):
    pdf = pikepdf.Pdf.new()
    from fontfactory import Dictionary
    pdf.add_blank_page(page_size=(100, 100))
    pdf.Root.Pages.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans")))
    del pdf.pages[0].obj["/Resources"]
    pdf.pages[0].obj.Contents = pdf.make_stream(b"BT /F1 12 Tf (A) Tj ET")
    src = tmp_path / "s.pdf"
    pdf.save(src)
    rep = _embed(src, tmp_path / "o.pdf", _catalog_dir(fontdir))
    assert rep.remaining == 0 and len(rep.embedded) == 1


# ---------------------------------------------------------------------------
# embed: Type0 / CID
# ---------------------------------------------------------------------------

def _type0_pdf(tmp_path, **kw):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=type0_identity(pdf, "TestSans", **kw)))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf 20 20 Td " + hex_text("AB").encode() + b" Tj ET")

    return _pdf_with(tmp_path, build)


def test_type0_verified_identity_is_embedded(tmp_path, fontdir):
    src = _type0_pdf(tmp_path)
    out = tmp_path / "o.pdf"
    rep = _embed(src, out, _catalog_dir(fontdir))
    assert len(rep.embedded) == 1, rep.skipped
    with pikepdf.open(out) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        d = f.DescendantFonts[0]
        assert str(d.Subtype) == "/CIDFontType2" and str(d.CIDToGIDMap) == "/Identity"
        assert "/FontFile2" in d.FontDescriptor and "/ToUnicode" in f and str(f.Encoding) == "/Identity-H"
        assert "/W" in d
        TTFont(io.BytesIO(d.FontDescriptor.FontFile2.read_bytes()))


def test_type0_gid_mismatch_is_skipped_not_corrupted(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "rev.ttf", "TestSans", "Regular", reverse=True)       # same names, different glyph order
    src = _type0_pdf(tmp_path)
    out = tmp_path / "o.pdf"
    rep = _embed(src, out, _catalog_dir(d))
    assert rep.embedded == [] and rep.remaining == 1
    assert "mismatch" in rep.skipped[0]["reason"].lower()
    assert any("not embedded" in w for w in rep.warnings)
    assert _fonts(out)[0]["embedded"] is False


def test_type0_without_evidence_needs_override(tmp_path, fontdir):
    src = _type0_pdf(tmp_path, with_w=False, with_tu=False)
    rep = _embed(src, tmp_path / "o1.pdf", _catalog_dir(fontdir))
    assert rep.remaining == 1 and "unverifiable" in rep.skipped[0]["reason"]
    rep2 = _embed(src, tmp_path / "o2.pdf", _catalog_dir(fontdir), allow_unverified_cid=True)
    assert rep2.remaining == 0
    with pikepdf.open(tmp_path / "o2.pdf") as pdf:   # /W generated from the font since the PDF had none
        assert "/W" in pdf.pages[0].Resources.Font.F1.DescendantFonts[0]


def test_type0_predefined_cmap_skipped(tmp_path, fontdir):
    src = _type0_pdf(tmp_path, ordering="Japan1", encoding="/90ms-RKSJ-H")
    rep = _embed(src, tmp_path / "o.pdf", _catalog_dir(fontdir))
    assert rep.remaining == 1 and "predefined" in rep.skipped[0]["reason"]


def test_type0_cff_becomes_cidfonttype0c(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "c.otf", "TestSans", "Regular", cff=True)
    src = _type0_pdf(tmp_path)
    out = tmp_path / "o.pdf"
    rep = _embed(src, out, _catalog_dir(d))
    assert len(rep.embedded) == 1, rep.skipped
    with pikepdf.open(out) as pdf:
        dd = pdf.pages[0].Resources.Font.F1.DescendantFonts[0]
        assert str(dd.Subtype) == "/CIDFontType0" and "/CIDToGIDMap" not in dd
        assert str(dd.FontDescriptor.FontFile3.Subtype) == "/CIDFontType0C"


# ---------------------------------------------------------------------------
# remove-fonts
# ---------------------------------------------------------------------------

def _embedded_pdf(tmp_path, fontdir, subset_name=False):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans"),
                                                        F2=type0_identity(pdf, "TestSans")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf (A) Tj ET")

    src = _pdf_with(tmp_path, build)
    out = tmp_path / "emb.pdf"
    _embed(src, out, _catalog_dir(fontdir))
    return out


def test_remove_all_and_filters(tmp_path, fontdir):
    emb = _embedded_pdf(tmp_path, fontdir)
    assert all(f["embedded"] for f in _fonts(emb))
    with pikepdf.open(emb) as pdf:
        removed = fl.remove_fonts(pdf)
        out = tmp_path / "r.pdf"
        pdf.save(out)
    assert len(removed) == 2
    assert not any(f["embedded"] for f in _fonts(out))
    assert out.stat().st_size < emb.stat().st_size            # streams really gone
    # size threshold: fonts larger than N bytes are removed (C# semantics), fonts <= N are kept
    with pikepdf.open(emb) as pdf:
        assert fl.remove_fonts(pdf, fl.make_remove_predicate(min_size=10 ** 9)) == []
        assert len(fl.remove_fonts(pdf, fl.make_remove_predicate(min_size=10))) == 2
    # subset filter: none of these are subset fonts
    with pikepdf.open(emb) as pdf:
        assert fl.remove_fonts(pdf, fl.make_remove_predicate(subset=True)) == []
    with pikepdf.open(emb) as pdf:
        assert len(fl.remove_fonts(pdf, fl.make_remove_predicate(subset=False, name_regex="testsans"))) == 2


def test_remove_large_pdf_rule(tmp_path, fontdir):
    emb = _embedded_pdf(tmp_path, fontdir)
    with pikepdf.open(emb) as pdf:
        assert fl.remove_fonts(pdf, fl.large_font_predicate(emb.stat().st_size)) == []   # PDF < 10 MB -> untouched
        big = fl.large_font_predicate(11 * 1024 * 1024)
        assert fl.remove_fonts(pdf, big) == []          # fonts < 5 MB are kept even in a big PDF
    # the 5MB limit itself
    assert fl.large_font_predicate(11 * 1024 * 1024)(fl.RemovedFont("x", False, 5 * 1024 * 1024, "/FontFile2"))
    assert not fl.large_font_predicate(11 * 1024 * 1024)(fl.RemovedFont("x", False, 5 * 1024 * 1024 - 1, "/FontFile2"))
    assert not fl.large_font_predicate(10 * 1024 * 1024 - 1)(fl.RemovedFont("x", False, 6 * 1024 * 1024, "/FontFile2"))


def test_remove_subset_font_removed_by_subset_filter(tmp_path, fontdir):
    emb = _embedded_pdf(tmp_path, fontdir)
    with pikepdf.open(emb) as pdf:
        f = pdf.pages[0].Resources.Font.F1
        f.BaseFont = pikepdf.Name("/ABCDEF+TestSans-Regular")
        assert len(fl.remove_fonts(pdf, fl.make_remove_predicate(subset=True))) == 1


# ---------------------------------------------------------------------------
# encryption + CLI
# ---------------------------------------------------------------------------

def test_encrypted_input_error_and_password(tmp_path, fontdir, capsys):
    from fontfactory import Dictionary
    src = tmp_path / "enc.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(100, 100))
    pdf.pages[0].obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans")))
    pdf.pages[0].obj.Contents = pdf.make_stream(b"BT /F1 12 Tf (A) Tj ET")
    pdf.save(src, encryption=pikepdf.Encryption(owner="secret", user="secret", R=4))
    out = tmp_path / "o.pdf"
    rc = main(["embed-fonts", str(src), "-o", str(out), "--font-dir", str(fontdir)])
    assert rc == 1 and "encrypted" in capsys.readouterr().err
    rc = main(["list-fonts", str(src), "--password", "wrong"])
    assert rc == 1 and "incorrect" in capsys.readouterr().err
    rc = main(["embed-fonts", str(src), "-o", str(out), "--font-dir", str(fontdir), "--password", "secret"])
    assert rc == 0
    with pikepdf.open(out) as pdf:           # decrypted output: opens without password, font embedded
        assert fl.list_fonts(pdf)[0]["embedded"]


def test_cli_end_to_end(tmp_path, fontdir, capsys):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf (A) Tj ET")

    src = _pdf_with(tmp_path, build)
    assert main(["list-fonts", str(src)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing[0]["name"] == "TestSans" and listing[0]["embedded"] is False
    out = tmp_path / "e.pdf"
    rep = tmp_path / "rep.json"
    assert main(["embed-fonts", str(src), "-o", str(out), "--font-dir", str(fontdir), "--report", str(rep), "--require-all"]) == 0
    assert json.loads(rep.read_text())["non_embedded_after"] == 0
    capsys.readouterr()
    assert main(["list-fonts", str(out), "--missing-only"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    rm = tmp_path / "rm.pdf"
    assert main(["remove-fonts", str(out), "-o", str(rm)]) == 0
    assert "1 embedded font file" in capsys.readouterr().out
    assert main(["list-fonts", str(rm), "--missing-only"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    # --require-all with an unmatched font -> exit code 3
    other = tmp_path / "other"
    other.mkdir()
    make_font(other / "x.ttf", "Different", "Regular")
    assert main(["embed-fonts", str(src), "-o", str(tmp_path / "n.pdf"), "--font-dir", str(other), "--require-all"]) == 3
    assert main(["embed-fonts", str(src), "-o", str(tmp_path / "n.pdf")]) == 1                   # no font source given


def test_subprocess_cli(tmp_path, fontdir):
    from fontfactory import Dictionary

    def build(pdf, page):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=simple_truetype(pdf, "TestSans")))
        page.obj.Contents = pdf.make_stream(b"BT /F1 48 Tf (A) Tj ET")

    src = _pdf_with(tmp_path, build)
    r = subprocess.run([sys.executable, "-m", "pdfmergepy", "embed-fonts", str(src), "-o", str(tmp_path / "o.pdf"),
                        "--font", f"TestSans={fontdir / 'TestSans-Regular.ttf'}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "1 of 1" in r.stdout


# ---------------------------------------------------------------------------
# render check (pypdfium2, optional)
# ---------------------------------------------------------------------------

def _dark_ratio(pdf_path, box):
    page = pdfium.PdfDocument(str(pdf_path))[0]
    img = page.render(scale=2, grayscale=True).to_numpy()
    h = img.shape[0]
    x0, y0, x1, y1 = [int(v * 2) for v in box]            # PDF user space -> pixels (scale 2)
    region = img[h - y1:h - y0, x0:x1]
    return float((region < 100).mean())


@pytest.mark.skipif(pdfium is None, reason="pypdfium2/numpy not installed")
@pytest.mark.parametrize("kind", ["simple", "type0", "form", "annot"])
def test_render_before_after(tmp_path, fontdir, kind):
    pdf = pikepdf.Pdf.new()
    from fontfactory import Array, Dictionary, Name
    pdf.add_blank_page(page_size=(200, 100))
    page = pdf.pages[0]
    cid = kind == "type0"
    font = type0_identity(pdf, "TestSans") if cid else simple_truetype(pdf, "TestSans")
    text = hex_text("AB") if cid else "(AB)"
    show = f"BT /F1 48 Tf 20 20 Td {text} Tj ET".encode()
    if kind in ("simple", "type0"):
        page.obj.Resources = Dictionary(Font=Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(show)
    elif kind == "form":
        form = pdf.make_stream(show)
        form.Type, form.Subtype, form.BBox = Name.XObject, Name.Form, Array([0, 0, 200, 100])
        form.Resources = Dictionary(Font=Dictionary(F1=font))
        page.obj.Resources = Dictionary(XObject=Dictionary(Fm0=form))
        page.obj.Contents = pdf.make_stream(b"/Fm0 Do")
    else:
        ap = pdf.make_stream(show)
        ap.Type, ap.Subtype, ap.BBox = Name.XObject, Name.Form, Array([0, 0, 200, 100])
        ap.Resources = Dictionary(Font=Dictionary(F1=font))
        page.obj.Annots = Array([pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Stamp, F=4,
                                                              Rect=Array([0, 0, 200, 100]), AP=Dictionary(N=ap)))])
        page.obj.Contents = pdf.make_stream(b"q Q")
    src = tmp_path / "in.pdf"
    pdf.save(src)
    out = tmp_path / "out.pdf"
    rep = _embed(src, out, _catalog_dir(fontdir))
    assert rep.remaining == 0, rep.skipped
    # glyph A is a solid 600x700 box, B 700x700: at 48pt, A covers x 20..48.8, y 20..53.6
    box_a = (22, 22, 46, 50)
    after = _dark_ratio(out, box_a)
    before = _dark_ratio(src, box_a)
    assert after > 0.95, f"embedded test font not used (dark={after:.2f})"
    assert before < 0.95, "fallback font unexpectedly solid"


@pytest.mark.skipif(pdfium is None, reason="pypdfium2/numpy not installed")
def test_render_cid_mismatch_not_embedded_keeps_original(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "rev.ttf", "TestSans", "Regular", reverse=True)
    src = _type0_pdf(tmp_path)
    out = tmp_path / "o.pdf"
    _embed(src, out, _catalog_dir(d))
    assert _dark_ratio(out, (22, 22, 46, 50)) == _dark_ratio(src, (22, 22, 46, 50))


# ---------------------------------------------------------------------------
# Windows system fonts (skip if absent) - real Arial / TTC
# ---------------------------------------------------------------------------

ARIAL = Path("C:/Windows/Fonts/arial.ttf")


@pytest.mark.skipif(not ARIAL.exists(), reason="C:/Windows/Fonts/arial.ttf not available")
def test_real_arial_embeds_and_parses(tmp_path):
    from fontfactory import Dictionary, Name
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 100))
    f = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                     Encoding=Name.WinAnsiEncoding))
    pdf.pages[0].obj.Resources = Dictionary(Font=Dictionary(F1=f))
    pdf.pages[0].obj.Contents = pdf.make_stream(b"BT /F1 24 Tf 10 40 Td (Hello World) Tj ET")
    src = tmp_path / "i.pdf"
    pdf.save(src)
    c = FontCatalog()
    c.add_named(f"Helvetica={ARIAL}")
    out = tmp_path / "o.pdf"
    rep = _embed(src, out, c)
    assert rep.remaining == 0
    with pikepdf.open(out) as p:
        fd = p.pages[0].Resources.Font.F1.FontDescriptor
        tt = TTFont(io.BytesIO(fd.FontFile2.read_bytes()))
        assert tt["name"].getDebugName(6) == "ArialMT"
        w = p.pages[0].Resources.Font.F1.Widths
        assert int(w[ord("H") - 32]) == 722 and int(w[ord(" ") - 32]) == 278      # Arial metrics
    if pdfium is not None:
        page = pdfium.PdfDocument(str(out))[0]
        txt = page.get_textpage().get_text_range()
        assert "Hello World" in txt


@pytest.mark.skipif(pdfium is None, reason="pypdfium2/numpy not installed")
def test_type0_mismatch_remapped_via_tounicode_renders_correctly(tmp_path):
    d = tmp_path / "fonts"
    d.mkdir()
    make_font(d / "rev.ttf", "TestSans", "Regular", reverse=True)
    src = _type0_pdf(tmp_path)
    out = tmp_path / "o.pdf"
    rep = _embed(src, out, _catalog_dir(d), remap_cid=True)
    assert len(rep.embedded) == 1 and "rebuilt from /ToUnicode" in rep.embedded[0]["detail"]
    with pikepdf.open(out) as pdf:
        assert is_stream_obj(pdf.pages[0].Resources.Font.F1.DescendantFonts[0].CIDToGIDMap)
    assert _dark_ratio(out, (22, 22, 46, 50)) > 0.95      # glyph A (solid box) now drawn from the supplied font


def is_stream_obj(o):
    return fl.is_stream(o)
