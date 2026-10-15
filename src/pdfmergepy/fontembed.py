"""Embed full font programs into the non-embedded fonts of an existing PDF.

Equivalent of CTS2.0 ``PdfOtfFontProcessor.EmbedFont`` + ``__ChangeInvalidFontReferences``
with these deliberate differences (see STATUS.md):

* font dictionaries are edited **in place** (no orphaned old font object);
* the *existing* /Encoding, /Widths, /ToUnicode, /W, /DW are preserved (the C# re-creates a
  WinAnsi simple font from the file, which only works for single-byte WinAnsi text);
* content streams, structure tree and tags are never touched;
* Type0 (CID) fonts are only upgraded when the CID -> glyph assumption can be *verified*
  (via /W widths and /ToUnicode against the supplied font); otherwise the font is skipped with
  a warning instead of silently rendering wrong glyphs.

Font program storage (safest-for-viewers option):
  glyf TrueType/OpenType-TT  -> /FontFile2 (+/Length1), simple /TrueType or CIDFontType2
  CFF OpenType               -> raw 'CFF ' table as /FontFile3 /Type1C (simple /Type1) or
                                /CIDFontType0C (CIDFontType0). The OpenType wrapper is not used,
                                which keeps PDF 1.2/1.3 compatible and avoids viewer differences.
  Type1 .pfb                 -> /FontFile (+/Length1/2/3), simple /Type1 only
"""

from __future__ import annotations

import io
import re
import struct
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from fontTools.agl import toUnicode
from fontTools.encodings.MacRoman import MacRoman
from fontTools.encodings.StandardEncoding import StandardEncoding
from fontTools.ttLib import TTFont
from pikepdf import Array, Dictionary, Name

from pdfmergepy.fontmap import FontCatalog, FontFileInfo, norm
from pdfmergepy.fonts import (DocFont, FONT_FILE_KEYS, collect_fonts, is_array, is_dict, is_stream)


@dataclass
class EmbedReport:
    embedded: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0            # non-embedded fonts found
    remaining: int = 0             # non-embedded fonts left afterwards

    def to_json(self) -> dict:
        return {"non_embedded_before": self.candidates, "non_embedded_after": self.remaining,
                "embedded": self.embedded, "skipped": self.skipped, "warnings": self.warnings}


class _Loaded:
    """A parsed font file plus the PDF stream objects already built from it."""

    def __init__(self, info: FontFileInfo):
        self.info = info
        self.raw = info.path.read_bytes()
        self.tt: TTFont | None = None
        self.streams: dict[str, pikepdf.Object] = {}
        if info.kind != "type1":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.tt = TTFont(str(info.path), fontNumber=info.font_number if info.font_number is not None else -1)
            if info.font_number is not None:  # extract one face of a collection
                buf = io.BytesIO()
                self.tt.save(buf)
                self.raw = buf.getvalue()

    @property
    def upem(self) -> int:
        return self.tt["head"].unitsPerEm

    def adv(self, gid: int) -> float | None:
        order = self.tt.getGlyphOrder()
        if gid < 0 or gid >= len(order):
            return None
        return self.tt["hmtx"][order[gid]][0] * 1000.0 / self.upem


def _table_bytes(tt: TTFont, tag: str) -> bytes:
    return tt.reader[tag]


# ---------------------------------------------------------------------------
# ToUnicode / W parsing for CID verification
# ---------------------------------------------------------------------------

_HEX = r"<([0-9A-Fa-f\s]*)>"


def parse_tounicode(data: bytes) -> dict[int, int]:
    """code -> single Unicode code point (multi-char / empty destinations are ignored)."""
    txt = data.decode("latin-1")
    out: dict[int, int] = {}

    def cp(h: str) -> int | None:
        h = re.sub(r"\s", "", h)
        if len(h) % 4 or not h:
            return None
        try:
            s = bytes.fromhex(h).decode("utf-16-be", errors="ignore")
        except ValueError:
            return None
        return ord(s) if len(s) == 1 else None

    for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(_HEX + r"\s*" + _HEX, blk):
            c = cp(b)
            if c is not None and a.strip():
                out[int(re.sub(r"\s", "", a), 16)] = c
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for m in re.finditer(_HEX + r"\s*" + _HEX + r"\s*(?:" + _HEX + r"|\[(.*?)\])", blk, re.S):
            lo, hi = int(re.sub(r"\s", "", m.group(1)), 16), int(re.sub(r"\s", "", m.group(2)), 16)
            if hi - lo > 0xFFFF:
                continue
            if m.group(3) is not None:
                base = cp(m.group(3))
                if base is None:
                    continue
                for i in range(lo, hi + 1):
                    out[i] = base + (i - lo)
            else:
                for i, h in zip(range(lo, hi + 1), re.findall(_HEX, m.group(4) or "")):
                    c = cp(h)
                    if c is not None:
                        out[i] = c
    return out


def parse_w_array(w) -> dict[int, float]:
    out: dict[int, float] = {}
    if not is_array(w):
        return out
    items = list(w)
    i = 0
    while i < len(items):
        try:
            c = int(items[i])
        except Exception:
            break
        if i + 1 < len(items) and is_array(items[i + 1]):
            for j, wv in enumerate(items[i + 1]):
                out[c + j] = float(wv)
            i += 2
        elif i + 2 < len(items):
            c2, wv = int(items[i + 1]), float(items[i + 2])
            if c2 - c <= 0xFFFF:
                for k in range(c, c2 + 1):
                    out[k] = wv
            i += 3
        else:
            break
    return out


def _cff_cid_to_gid(tt: TTFont) -> dict[int, int]:
    top = tt["CFF "].cff.topDictIndex[0]
    charset = top.charset
    m = {}
    for gid, nm in enumerate(charset):
        mm = re.match(r"cid0*(\d+)$", nm)
        if mm:
            m[int(mm.group(1))] = gid
    if 0 not in m:
        m[0] = 0
    return m


def verify_cid_font(loaded: _Loaded, font_obj, desc_font, cid2gid_stream: bytes | None) -> tuple[str, str]:
    """Decide whether CIDs of the PDF font equal CIDs/GIDs of the supplied font.

    Returns ('ok'|'mismatch'|'unverified', detail)."""
    info = loaded.info
    tt = loaded.tt
    cff_map = _cff_cid_to_gid(tt) if (info.kind == "cff" and info.cff_cid_keyed) else None

    def gid_of(cid: int) -> int | None:
        if cid2gid_stream is not None and info.kind == "truetype":
            if 2 * cid + 1 < len(cid2gid_stream):
                return struct.unpack(">H", cid2gid_stream[2 * cid: 2 * cid + 2])[0]
            return None
        if cff_map is not None:
            return cff_map.get(cid)
        return cid

    verdicts: list[tuple[str, bool, float]] = []

    # --- /W widths vs hmtx
    w = parse_w_array(desc_font.get("/W"))
    if w:
        sample = sorted(w)[:600]
        ok = tot = 0
        for cid in sample:
            if cid == 0:
                continue
            gid = gid_of(cid)
            adv = loaded.adv(gid) if gid is not None else None
            tot += 1
            if adv is not None and abs(adv - w[cid]) <= max(2.0, 0.015 * w[cid]):
                ok += 1
        if tot >= 3:
            verdicts.append(("W", ok / tot >= 0.9, ok / tot))

    # --- /ToUnicode vs cmap
    tu = font_obj.get("/ToUnicode")
    if is_stream(tu):
        try:
            m = parse_tounicode(tu.read_bytes())
        except Exception:
            m = {}
        cmap = tt.getBestCmap() or {}
        order = {n: i for i, n in enumerate(tt.getGlyphOrder())}
        ok = tot = 0
        for cid in sorted(m)[:600]:
            u = m[cid]
            if u in (0x20, 0xA0, 0xAD, 0x2009, 0x200B) or cid == 0:
                continue
            gname = cmap.get(u)
            tot += 1
            if gname is not None and order.get(gname) == gid_of(cid):
                ok += 1
        if tot >= 3:
            verdicts.append(("ToUnicode", ok / tot >= 0.9, ok / tot))

    if not verdicts:
        return "unverified", "no /W or /ToUnicode evidence to prove that CID == glyph id of the supplied font"
    detail = ", ".join(f"{n}:{r:.0%}" for n, _, r in verdicts)
    if all(ok for _, ok, _ in verdicts):
        return "ok", detail
    return "mismatch", detail


# ---------------------------------------------------------------------------
# descriptor / widths helpers
# ---------------------------------------------------------------------------

def _scaled(v, upem) -> int:
    return int(round(v * 1000.0 / upem))


def _fill_descriptor(pdf, desc, loaded: _Loaded, new_name: str, simple_symbolic_hint: bool = False):
    """Add missing FontDescriptor keys from the font file (existing values are kept)."""
    info = loaded.info
    desc["/Type"] = Name.FontDescriptor
    desc["/FontName"] = Name("/" + new_name)

    if info.kind == "type1":
        txt = loaded.raw[:8192].decode("latin-1")
        bb = re.search(r"/FontBBox\s*[{\[]\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)", txt)
        box = [float(x) for x in bb.groups()] if bb else [-200, -250, 1000, 900]
        ia = re.search(r"/ItalicAngle\s+(-?[\d.]+)", txt)
        defaults = {"/Flags": 32 | (64 if info.italic else 0), "/FontBBox": Array(box),
                    "/ItalicAngle": float(ia.group(1)) if ia else 0, "/Ascent": box[3], "/Descent": box[1],
                    "/CapHeight": box[3], "/StemV": 120 if info.bold else 80}
    else:
        tt = loaded.tt
        up = loaded.upem
        head, hhea = tt["head"], tt["hhea"]
        os2 = tt["OS/2"] if "OS/2" in tt else None
        post = tt["post"] if "post" in tt else None
        asc = _scaled(os2.sTypoAscender if os2 and os2.sTypoAscender else hhea.ascent, up)
        dsc = _scaled(os2.sTypoDescender if os2 and os2.sTypoDescender else hhea.descent, up)
        cap = _scaled(getattr(os2, "sCapHeight", 0) or 0, up) if os2 and os2.version >= 2 else 0
        cmap = tt["cmap"].tables if "cmap" in tt else []
        symbolic_only = bool(cmap) and all(t.platformID == 3 and t.platEncID == 0 for t in cmap)
        flags = 4 if (symbolic_only or simple_symbolic_hint) else 32
        if post is not None and post.isFixedPitch:
            flags |= 1
        if info.italic or (post is not None and post.italicAngle):
            flags |= 64
        if info.bold and os2 is not None and os2.usWeightClass >= 700:
            flags |= 0x40000
        wc = os2.usWeightClass if os2 else (700 if info.bold else 400)
        defaults = {
            "/Flags": flags,
            "/FontBBox": Array([_scaled(head.xMin, up), _scaled(head.yMin, up), _scaled(head.xMax, up), _scaled(head.yMax, up)]),
            "/ItalicAngle": float(post.italicAngle) if post is not None else 0,
            "/Ascent": asc, "/Descent": dsc,
            "/CapHeight": cap or asc,
            "/StemV": int(50 + (wc / 65.0) ** 2),
        }
    for k, v in defaults.items():
        if k not in desc:
            desc[k] = v


def _simple_base_encoding(font) -> tuple[list[str | None] | None, bool]:
    """(code -> glyph name list or None, is_winansi)."""
    enc = font.get("/Encoding")
    base = None
    diffs = {}
    if isinstance(enc, pikepdf.Name):
        base = str(enc)
    elif is_dict(enc):
        if isinstance(enc.get("/BaseEncoding"), pikepdf.Name):
            base = str(enc["/BaseEncoding"])
        d = enc.get("/Differences")
        if is_array(d):
            code = 0
            for it in d:
                if isinstance(it, pikepdf.Name):
                    diffs[code] = str(it).lstrip("/")
                    code += 1
                else:
                    code = int(it)
    names: list[str | None]
    if base == "/WinAnsiEncoding" or (base is None and False):
        names = []
        for c in range(256):
            try:
                ch = bytes([c]).decode("cp1252")
                names.append("uni%04X" % ord(ch))
            except UnicodeDecodeError:
                names.append(None)
    elif base == "/MacRomanEncoding":
        names = list(MacRoman)
    else:
        names = list(StandardEncoding)
    for c, n in diffs.items():
        if 0 <= c < 256:
            names[c] = n
    return names, base == "/WinAnsiEncoding"


def _build_simple_widths(font, loaded: _Loaded, symbolic: bool):
    """/FirstChar /LastChar /Widths from the font program (only used when the PDF font has none)."""
    if loaded.tt is None:
        return
    cmap = loaded.tt.getBestCmap() or {}
    sym = None
    for t in loaded.tt["cmap"].tables:
        if t.platformID == 3 and t.platEncID == 0:
            sym = t.cmap
    names, _ = _simple_base_encoding(font)
    widths = []
    for code in range(32, 256):
        gname = None
        if symbolic and sym is not None:
            gname = sym.get(0xF000 + code) or sym.get(code)
        else:
            n = names[code]
            if n:
                m = re.match(r"uni([0-9A-F]{4})$", n)
                u = int(m.group(1), 16) if m else (ord(toUnicode(n)) if len(toUnicode(n) or "") == 1 else None)
                if u is not None:
                    gname = cmap.get(u)
                if gname is None and n in loaded.tt.getGlyphOrder():
                    gname = n
        if gname is None:
            widths.append(0)
        else:
            widths.append(int(round(loaded.tt["hmtx"][gname][0] * 1000.0 / loaded.upem)))
    font["/FirstChar"] = 32
    font["/LastChar"] = 255
    font["/Widths"] = Array(widths)


def _cid_to_gid_via_tounicode(loaded: _Loaded, font_obj) -> tuple[bytes | None, float]:
    tu = font_obj.get("/ToUnicode")
    if not is_stream(tu):
        return None, 0.0
    m = parse_tounicode(tu.read_bytes())
    if not m:
        return None, 0.0
    cmap = loaded.tt.getBestCmap() or {}
    order = {n: i for i, n in enumerate(loaded.tt.getGlyphOrder())}
    table = bytearray(2 * (max(m) + 1))
    hit = 0
    for cid, u in m.items():
        g = order.get(cmap.get(u))
        if g is not None:
            struct.pack_into(">H", table, 2 * cid, g)
            hit += 1
    cov = hit / len(m)
    return (bytes(table), cov) if cov >= 0.9 else (None, cov)


def _build_cid_widths(loaded: _Loaded) -> Array:
    n = len(loaded.tt.getGlyphOrder())
    w = Array()
    # one run per 256 glyphs keeps the array readable
    for start in range(0, n, 256):
        run = [int(round(loaded.adv(g) or 0)) for g in range(start, min(n, start + 256))]
        w.append(start)
        w.append(Array(run))
    return w


# ---------------------------------------------------------------------------
# stream creation
# ---------------------------------------------------------------------------

def _font_stream(pdf, loaded: _Loaded, kind: str) -> pikepdf.Object:
    """Build (once per loaded font and role) the font-file stream."""
    if kind in loaded.streams:
        return loaded.streams[kind]
    info = loaded.info
    if kind == "FontFile2":
        s = pdf.make_stream(loaded.raw)
        s["/Length1"] = len(loaded.raw)
    elif kind in ("Type1C", "CIDFontType0C"):
        data = _table_bytes(loaded.tt, "CFF ")
        s = pdf.make_stream(data)
        s["/Subtype"] = Name("/" + kind)
    elif kind == "FontFile":
        s = _pfb_stream(pdf, loaded.raw)
    else:  # pragma: no cover
        raise ValueError(kind)
    loaded.streams[kind] = s
    return s


def _pfb_stream(pdf, raw: bytes):
    segs = []
    i = 0
    if raw[:1] != b"\x80":  # .pfa-like text: split at 'eexec'
        k = raw.find(b"eexec")
        l1 = k + 6 if k >= 0 else len(raw)
        data = raw
        l2, l3 = len(raw) - l1, 0
    else:
        while i + 6 <= len(raw) and raw[i] == 0x80:
            t = raw[i + 1]
            if t == 3:
                break
            ln = struct.unpack("<I", raw[i + 2:i + 6])[0]
            segs.append((t, raw[i + 6:i + 6 + ln]))
            i += 6 + ln
        ascii_parts = [d for t, d in segs if t == 1]
        bin_parts = [d for t, d in segs if t == 2]
        l1 = len(ascii_parts[0]) if ascii_parts else 0
        l2 = sum(len(b) for b in bin_parts)
        l3 = sum(len(a) for a in ascii_parts[1:])
        data = b"".join(d for _, d in segs)
    s = pdf.make_stream(data)
    s["/Length1"], s["/Length2"], s["/Length3"] = l1, l2, l3
    return s


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def embed_fonts(pdf: pikepdf.Pdf, catalog: FontCatalog, strict: bool = False, ignore_license: bool = False,
                allow_unverified_cid: bool = False, remap_cid: bool = False) -> EmbedReport:
    rep = EmbedReport()
    rep.warnings.extend(catalog.warnings)
    loaded_cache: dict[tuple, _Loaded] = {}

    def load(info: FontFileInfo) -> _Loaded:
        k = (str(info.path), info.font_number)
        if k not in loaded_cache:
            loaded_cache[k] = _Loaded(info)
        return loaded_cache[k]

    fonts = collect_fonts(pdf)
    for df in fonts:
        if df.embedded or df.subtype not in ("Type1", "MMType1", "TrueType", "Type0"):
            continue
        rep.candidates += 1
        entry = {"name": df.base_font, "type": df.type_label, "object": df.obj_label, "pages": sorted(df.pages)}

        info, how = catalog.match(df.base_font, strict=strict)
        if info is None:
            rep.skipped.append({**entry, "reason": "no matching font file"})
            continue
        if info.restricted_license and not ignore_license:
            rep.skipped.append({**entry, "reason": f"font license forbids embedding (OS/2 fsType=0x{info.fs_type:04X}): {info.describe()}"})
            continue
        try:
            loaded = load(info)
            ok, detail = _embed_one(pdf, df, loaded, allow_unverified_cid, remap_cid)
        except Exception as ex:  # a broken font must not abort the document
            rep.skipped.append({**entry, "reason": f"error: {type(ex).__name__}: {ex}", "font": info.describe()})
            continue
        if ok:
            rep.embedded.append({**entry, "font": info.describe(), "match": how, "detail": detail})
            if how == "family-style-fallback":
                rep.warnings.append(f"{df.base_font}: approximate match (style fallback) -> {info.describe()}")
        else:
            rep.skipped.append({**entry, "reason": detail, "font": info.describe()})
            rep.warnings.append(f"{df.base_font}: not embedded - {detail}")

    rep.remaining = sum(1 for f in collect_fonts(pdf)
                        if not f.embedded and f.subtype in ("Type1", "MMType1", "TrueType", "Type0"))
    return rep


def _clean_name(s: str) -> str:
    return re.sub(r"[\s()<>\[\]{}/%#]", "", s)


def _embed_one(pdf, df: DocFont, loaded: _Loaded, allow_unverified_cid: bool, remap_cid: bool = False) -> tuple[bool, str]:
    info = loaded.info
    font = df.obj
    new_name = _clean_name(info.ps_name)

    if df.subtype == "Type0":
        return _embed_type0(pdf, df, loaded, new_name, allow_unverified_cid, remap_cid)

    # ----- simple font -------------------------------------------------
    if info.kind == "cff" and info.cff_cid_keyed:
        return False, "supplied font is a CID-keyed CFF; cannot be used for a simple font"
    desc = df.descriptor
    if desc is None:
        desc = pdf.make_indirect(Dictionary())
        font["/FontDescriptor"] = desc
    symbolic = bool(int(desc.get("/Flags", 32)) & 4)
    if info.kind == "truetype":
        stream, key, sub = _font_stream(pdf, loaded, "FontFile2"), "/FontFile2", "/TrueType"
    elif info.kind == "cff":
        stream, key, sub = _font_stream(pdf, loaded, "Type1C"), "/FontFile3", "/Type1"
    else:
        stream, key, sub = _font_stream(pdf, loaded, "FontFile"), "/FontFile", "/Type1"
    had_widths = "/Widths" in font
    _fill_descriptor(pdf, desc, loaded, new_name, symbolic)
    for k in FONT_FILE_KEYS:
        if k in desc:
            del desc[k]
    desc[key] = stream
    font["/Subtype"] = Name(sub)
    font["/BaseFont"] = Name("/" + new_name)
    if not had_widths and info.kind != "type1":
        _build_simple_widths(font, loaded, symbolic)
    return True, f"{key[1:]} ({stream.get('/Subtype') or 'raw'}) {len(loaded.raw)} bytes"


def _embed_type0(pdf, df: DocFont, loaded: _Loaded, new_name: str, allow_unverified: bool,
                 remap_cid: bool = False) -> tuple[bool, str]:
    info = loaded.info
    font = df.obj
    dfont = df.descendant
    if dfont is None:
        return False, "Type0 font without DescendantFonts"
    if info.kind == "type1":
        return False, "Type1 (.pfb) program cannot be used for a CID font"
    enc = font.get("/Encoding")
    ordering = ""
    csi = dfont.get("/CIDSystemInfo")
    if is_dict(csi) and "/Ordering" in csi:
        ordering = str(csi["/Ordering"])
    identity = (isinstance(enc, pikepdf.Name) and str(enc) in ("/Identity-H", "/Identity-V")) or ordering == "Identity"
    if not identity:
        return False, (f"CID font uses predefined/non-Identity CMap ({enc if isinstance(enc, pikepdf.Name) else 'custom'}, "
                       f"ordering '{ordering}'): CIDs are collection CIDs, not glyph ids of the supplied font")
    c2g = dfont.get("/CIDToGIDMap")
    c2g_bytes = c2g.read_bytes() if is_stream(c2g) else None
    verdict, detail = verify_cid_font(loaded, font, dfont, c2g_bytes)
    new_c2g = None
    if verdict == "mismatch" and remap_cid and info.kind == "truetype":
        # The original CIDs are glyph ids of a *different* glyph order (typically a removed subset).
        # Rebuild CID -> GID through /ToUnicode so the text keeps rendering with the supplied font.
        new_c2g, cov = _cid_to_gid_via_tounicode(loaded, font)
        if new_c2g is None:
            return False, f"CID/GID mismatch and /ToUnicode remap impossible ({detail})"
        v2, d2 = verify_cid_font(loaded, font, dfont, new_c2g)
        if v2 == "mismatch":
            return False, f"CID/GID mismatch; /ToUnicode remap did not reconcile /W ({detail} -> {d2})"
        verdict, detail = "remapped", f"{detail}; CID->GID rebuilt from /ToUnicode, coverage {cov:.0%}, then {d2}"
    elif verdict == "mismatch":
        return False, f"CID/GID mismatch: glyph order of the supplied font differs from the original ({detail})"
    if verdict == "unverified" and not allow_unverified:
        return False, f"CID/GID assumption unverifiable: {detail} (use --allow-unverified-cid to force)"

    desc = dfont.get("/FontDescriptor")
    if not is_dict(desc):
        desc = pdf.make_indirect(Dictionary())
        dfont["/FontDescriptor"] = desc
    _fill_descriptor(pdf, desc, loaded, new_name, False)
    for k in FONT_FILE_KEYS:
        if k in desc:
            del desc[k]
    if info.kind == "truetype":
        desc["/FontFile2"] = _font_stream(pdf, loaded, "FontFile2")
        dfont["/Subtype"] = Name.CIDFontType2
        if new_c2g is not None:
            dfont["/CIDToGIDMap"] = pdf.make_stream(new_c2g)
        elif c2g is None:
            dfont["/CIDToGIDMap"] = Name.Identity
        label = "FontFile2"
    else:
        desc["/FontFile3"] = _font_stream(pdf, loaded, "CIDFontType0C")
        dfont["/Subtype"] = Name.CIDFontType0
        if "/CIDToGIDMap" in dfont:
            del dfont["/CIDToGIDMap"]
        label = "FontFile3/CIDFontType0C"
    if "/W" not in dfont and "/DW" not in dfont and not info.cff_cid_keyed:
        dfont["/W"] = _build_cid_widths(loaded)
    dfont["/BaseFont"] = Name("/" + new_name)
    font["/BaseFont"] = Name("/" + new_name)
    return True, f"{label} {len(loaded.raw)} bytes; CID check {verdict} ({detail})"
