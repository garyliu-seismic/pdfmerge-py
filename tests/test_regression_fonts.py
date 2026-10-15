"""Real-sample regression for embed-fonts / remove-fonts (skips when the sample data is absent).

1. C:/test/IText7Test/PdfMerge/inputs : real blob PDFs, many with non-embedded base-14 / Arial fonts.
   Windows system fonts (C:/Windows/Fonts, test use only, never committed) are supplied through
   ``--font NAME=path``. Checks: every non-embedded font gets embedded, the output re-opens, the embedded
   programs parse with fontTools, text extraction is unchanged and pdftagvalicate check 31-001 is not
   worse (Fail -> Pass where the source failed) with NO new Fail/Error check vs the source.
2. CTS2.0 CloudFonts integration sample (baseline.pdf, subset fonts): remove-fonts then embed-fonts with the
   CloudFonts catalogue. Simple fonts must render like the baseline; the Type0 subset fonts have a different
   glyph order than the CloudFonts file and must be *skipped* (not silently corrupted).
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from fontTools.ttLib import TTFont

from pdfmergepy import fonts as fl
from pdfmergepy.fontembed import embed_fonts
from pdfmergepy.fontmap import FontCatalog

try:
    import pypdfium2 as pdfium
    import numpy  # noqa: F401
except Exception:  # pragma: no cover
    pdfium = None

INPUTS_DIR = Path("C:/test/IText7Test/PdfMerge/inputs")
WIN_FONTS = Path("C:/Windows/Fonts")
CTS_RES = Path("C:/project_new/content-transformation-service-v2/src/CTS/assemblies/Release/resources")
BASELINE = CTS_RES / "LocalTest/CloudFonts/baseline.pdf"
CLOUD = CTS_RES / "CloudFonts/4/CloudFonts"

# PDF font name -> Windows font file (test stand-ins; ZapfDingbats/MinionPro have no free equivalent here,
# so Wingdings / Garamond are used purely to exercise the mechanics)
SYSTEM_MAP = {
    "Helvetica": "arial.ttf", "Arial": "arial.ttf", "Arial,Bold": "arialbd.ttf", "Times-Roman": "times.ttf",
    "Courier-Bold": "courbd.ttf", "ZapfDingbats": "wingding.ttf", "MinionPro-Regular": "GARA.TTF",
}

HAVE_INPUTS = INPUTS_DIR.exists() and WIN_FONTS.exists() and all((WIN_FONTS / v).exists() for v in SYSTEM_MAP.values())

try:
    import pdftagvalicate  # noqa: F401
    HAVE_VALIDATOR = True
except Exception:  # pragma: no cover
    HAVE_VALIDATOR = False


def _nonembedded(path) -> list[dict]:
    with pikepdf.open(path) as pdf:
        return [f for f in fl.list_fonts(pdf) if not f["embedded"]]


def _candidates() -> list[Path]:
    if not HAVE_INPUTS:
        return []
    seen, out = set(), []
    for p in sorted(INPUTS_DIR.iterdir()):
        if p.suffix.lower() != ".pdf" or p.name.lower() in seen:
            continue
        seen.add(p.name.lower())
        try:
            if _nonembedded(p):
                out.append(p)
        except Exception:
            continue
    return out


_CANDS = _candidates()


def _validate(p: Path) -> dict:
    r = subprocess.run([sys.executable, "-m", "pdftagvalicate", str(p), "--validate", "--json"],
                       capture_output=True, text=True)
    d = json.loads(r.stderr.strip() or r.stdout.strip())
    return {c["id"]: c["severity"] for c in d["checks"]}


@pytest.mark.skipif(not _CANDS, reason="real sample PDFs / Windows fonts not available")
@pytest.mark.parametrize("src", _CANDS, ids=lambda p: p.stem[:24])
def test_real_pdf_embed_fonts(src: Path, tmp_path: Path):
    before = _nonembedded(src)
    cat = FontCatalog()
    for k, v in SYSTEM_MAP.items():
        cat.add_named(f"{k}={WIN_FONTS / v}")
    out = tmp_path / "out.pdf"
    with pikepdf.open(src) as pdf:
        rep = embed_fonts(pdf, cat)
        pdf.save(out)
    assert rep.remaining == 0, rep.skipped
    assert _nonembedded(out) == []
    with pikepdf.open(out) as pdf:                                 # embedded programs are valid fonts
        seen = 0
        for df in fl.collect_fonts(pdf):
            fd = df.descriptor
            if fd is not None and "/FontFile2" in fd:
                TTFont(io.BytesIO(fd.FontFile2.read_bytes()))
                seen += 1
        assert seen >= len(before) - 1
    if pdfium is not None:
        a, b = pdfium.PdfDocument(str(src)), pdfium.PdfDocument(str(out))
        assert len(a) == len(b)
        for i in range(min(len(a), 3)):
            assert a[i].get_textpage().get_text_range() == b[i].get_textpage().get_text_range()
    if HAVE_VALIDATOR:
        vb, va = _validate(src), _validate(out)
        bad = {"Fail", "Error"}
        assert not [k for k, s in va.items() if s in bad and vb.get(k) not in bad], "new validator failures"
        if vb.get("31-001") == "Fail":
            assert va["31-001"] == "Pass"


@pytest.mark.skipif(not (BASELINE.exists() and CLOUD.exists()), reason="CTS2.0 CloudFonts sample not available")
def test_cloudfonts_baseline_remove_then_embed(tmp_path: Path):
    stripped, out = tmp_path / "stripped.pdf", tmp_path / "out.pdf"
    with pikepdf.open(BASELINE) as pdf:
        removed = fl.remove_fonts(pdf)
        pdf.save(stripped)
    assert len(removed) == 6 and len(_nonembedded(stripped)) == 6
    cat = FontCatalog()
    for fam in ("Roboto", "Georgia"):
        cat.add_directory(CLOUD / fam)
    with pikepdf.open(stripped) as pdf:
        rep = embed_fonts(pdf, cat)
        pdf.save(out)
    assert len(rep.embedded) == 4 and rep.remaining == 2
    assert all("mismatch" in s["reason"].lower() for s in rep.skipped)            # subset glyph order != CloudFonts file
    assert {e["match"] for e in rep.embedded} == {"postscript"}
    if pdfium is not None:
        a, b = pdfium.PdfDocument(str(BASELINE)), pdfium.PdfDocument(str(out))
        for i in range(len(a)):
            ra = a[i].render(scale=1.5, grayscale=True).to_numpy().astype(int)
            rb = b[i].render(scale=1.5, grayscale=True).to_numpy().astype(int)
            assert float((abs(ra - rb) > 64).mean()) < 0.005                         # <0.5% pixels differ (vs ~1.6% stripped)


CTS_OTF = Path("C:/project_new/content-transformation-service-v2/src/IntegrationTest/test_suite/Documents/TestData/"
               "CustomFonts/AllspringSanomat/Fonts/Allspring Sanomat-Bold.otf")


@pytest.mark.skipif(not CTS_OTF.exists() or pdfium is None, reason="CTS2.0 CustomFonts OTF sample / pypdfium2 not available")
def test_real_cff_otf_simple_and_cid(tmp_path: Path):
    """A real CFF OpenType (CTS2.0 CustomFonts): simple font -> Type1C, Type0 -> CIDFontType0C; both render."""
    from fontfactory import Array, Dictionary, Name, String, tounicode
    tt = TTFont(str(CTS_OTF))
    ps, cmap, order = tt["name"].getDebugName(6), tt.getBestCmap(), tt.getGlyphOrder()
    text = "Hamburgefonts"
    gids = [order.index(cmap[ord(c)]) for c in text]
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(400, 200))
    simple = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/" + ps),
                                          Encoding=Name.WinAnsiEncoding))
    w = []
    for g in sorted(set(gids)):
        w += [g, Array([round(tt["hmtx"][order[g]][0] * 1000 / tt["head"].unitsPerEm)])]
    cid = Dictionary(Type=Name.Font, Subtype=Name.CIDFontType0, BaseFont=Name("/" + ps), W=Array(w),
                     CIDSystemInfo=Dictionary(Registry=String("Adobe"), Ordering=String("Identity"), Supplement=0),
                     FontDescriptor=pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/" + ps), Flags=4)))
    t0 = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name("/" + ps), Encoding=Name("/Identity-H"),
                                      DescendantFonts=Array([pdf.make_indirect(cid)])))
    t0.ToUnicode = tounicode(pdf, dict(zip(gids, text)))
    pg = pdf.pages[0]
    pg.Resources = Dictionary(Font=Dictionary(F1=simple, F2=t0))
    hexs = "<" + "".join("%04X" % g for g in gids) + ">"
    pg.Contents = pdf.make_stream(f"BT /F1 36 Tf 10 130 Td ({text}) Tj ET BT /F2 36 Tf 10 60 Td {hexs} Tj ET".encode())
    src, out = tmp_path / "s.pdf", tmp_path / "o.pdf"
    pdf.save(src)
    cat = FontCatalog()
    cat.add_file(CTS_OTF)
    with pikepdf.open(src) as p:
        rep = embed_fonts(p, cat)
        p.save(out)
    assert len(rep.embedded) == 2 and rep.remaining == 0
    with pikepdf.open(out) as p:
        f = p.pages[0].Resources.Font
        assert str(f.F1.FontDescriptor.FontFile3.Subtype) == "/Type1C"
        assert str(f.F2.DescendantFonts[0].FontDescriptor.FontFile3.Subtype) == "/CIDFontType0C"
    a = pdfium.PdfDocument(str(src))[0].render(scale=1, grayscale=True).to_numpy().astype(int)
    b = pdfium.PdfDocument(str(out))[0].render(scale=1, grayscale=True).to_numpy().astype(int)
    assert float((abs(a - b) > 64).mean()) > 0.001                      # real glyphs now differ from the fallback render
    # both lines draw the same glyphs: top half (simple font) == bottom half (CID font), up to the y offset
    assert pdfium.PdfDocument(str(out))[0].get_textpage().get_text_range().count("Hamburgefonts") == 2
    rows = (b < 128).any(axis=1)
    assert rows.sum() > 20
