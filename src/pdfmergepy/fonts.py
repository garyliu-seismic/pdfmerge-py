"""Font inventory, embedding and removal for existing PDFs (pikepdf + fontTools).

Python equivalent of CTS2.0:
  * ``IPdfManager.GetUsedFonts``                       -> :func:`list_fonts`
  * ``PdfOtfFontProcessor.EmbedFont``                  -> :func:`embed_fonts`   (see ``fontembed.py``)
  * ``InternalUtility.RemoveFonts`` (+ the 10MB/5MB rule in ``OfficeUtility``) -> :func:`remove_fonts`

Font discovery walks, per page: the page /Resources (inherited through the page tree),
Form XObjects, tiling Patterns, ExtGState /SMask groups and /Font entries, annotation
appearance streams (/AP /N /D /R), Type3 /Resources and AcroForm /DR. (The C#
``CollectFonts`` ignores inherited resources and, due to a bug at InternalUtility.cs:123,
reads the XObject from the *page* dictionary instead of the XObject dictionary, so it never
actually recurses into Form XObjects.)

Design choice: font dictionaries are modified **in place** (the C# creates a new font
object and repoints the resource entry, orphaning the old one). In-place editing keeps
every reference (resource entries, AcroForm /DR, shared dicts) valid and leaves no orphan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pikepdf import Name, ObjectType

MAX_DEPTH = 50
FONT_FILE_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")

LARGE_PDF_SIZE = 10 * 1024 * 1024   # OfficeUtility.__LARGE_PDF_SIZE_FOR_REMOVING_EMBEDDED_FONTS
LARGE_FONT_SIZE = 5 * 1024 * 1024   # OfficeUtility.__LARGE_EMBED_SIZE_FOR_REMOVING


# ---------------------------------------------------------------------------
# small pikepdf helpers
# ---------------------------------------------------------------------------

def is_stream(o) -> bool:
    return isinstance(o, pikepdf.Object) and o._type_code == ObjectType.stream


def is_dict(o) -> bool:
    return isinstance(o, pikepdf.Object) and o._type_code in (ObjectType.dictionary, ObjectType.stream)


def is_array(o) -> bool:
    return isinstance(o, pikepdf.Object) and o._type_code == ObjectType.array


def _name_str(o) -> str:
    try:
        return str(o) if isinstance(o, pikepdf.Name) else ""
    except Exception:
        return ""


def open_pdf(path, password: str = "") -> pikepdf.Pdf:
    """Open a PDF, turning password problems into a clear ``ValueError``."""
    try:
        return pikepdf.open(path, password=password or "")
    except pikepdf.PasswordError as ex:
        if password:
            raise ValueError(f"{path}: the supplied --password is incorrect for this encrypted PDF") from ex
        raise ValueError(f"{path}: PDF is encrypted; supply the password with --password") from ex


def inherited(page_obj: pikepdf.Object, key: str):
    """Look up an inheritable page attribute (/Resources ...) up the /Parent chain."""
    node = page_obj
    for _ in range(MAX_DEPTH * 4):
        if not is_dict(node):
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _key(o: pikepdf.Object):
    og = o.objgen
    return og if og != (0, 0) else None


# ---------------------------------------------------------------------------
# font model
# ---------------------------------------------------------------------------

@dataclass
class DocFont:
    obj: pikepdf.Object                     # the (top-level) font dictionary
    index: int
    pages: set[int] = field(default_factory=set)
    uses: list[str] = field(default_factory=list)

    # --- structure
    @property
    def subtype(self) -> str:
        return _name_str(self.obj.get("/Subtype")).lstrip("/")

    @property
    def descendant(self) -> pikepdf.Object | None:
        if self.subtype == "Type0":
            d = self.obj.get("/DescendantFonts")
            if is_array(d) and len(d) > 0 and is_dict(d[0]):
                return d[0]
        return None

    @property
    def descriptor(self) -> pikepdf.Object | None:
        holder = self.descendant if self.subtype == "Type0" else self.obj
        if holder is None:
            return None
        fd = holder.get("/FontDescriptor")
        return fd if is_dict(fd) else None

    @property
    def base_font(self) -> str:
        bf = self.obj.get("/BaseFont")
        if bf is None and self.descendant is not None:
            bf = self.descendant.get("/BaseFont")
        s = _name_str(bf).lstrip("/")
        if not s:
            fd = self.descriptor
            s = _name_str(fd.get("/FontName")).lstrip("/") if fd is not None else ""
        return s

    @property
    def name(self) -> str:
        return re.sub(r"^[A-Z]{6}\+", "", self.base_font)

    @property
    def is_subset(self) -> bool:
        return bool(re.match(r"^[A-Z]{6}\+", self.base_font))

    @property
    def font_file_key(self) -> str | None:
        fd = self.descriptor
        if fd is None:
            return None
        for k in FONT_FILE_KEYS:
            if k in fd and is_stream(fd[k]):
                return k
        return None

    @property
    def embedded(self) -> bool:
        # Type3 glyphs are defined inline in the PDF: nothing to embed.
        return self.subtype == "Type3" or self.font_file_key is not None

    @property
    def type_label(self) -> str:
        st = self.subtype
        if st == "Type0":
            d = self.descendant
            return "Type0/" + (_name_str(d.get("/Subtype")).lstrip("/") if d is not None else "?")
        return st or "?"

    @property
    def obj_label(self) -> str | None:
        k = _key(self.obj)
        return f"{k[0]} {k[1]}" if k else None

    def to_json(self, max_uses: int = 20) -> dict:
        fk = self.font_file_key
        size = None
        if fk is not None:
            try:
                size = len(self.descriptor[fk].read_raw_bytes())
            except Exception:
                size = None
        enc = self.obj.get("/Encoding")
        return {
            "name": self.name,
            "base_font": self.base_font,
            "type": self.type_label,
            "embedded": self.embedded,
            "subset": self.is_subset,
            "font_file": fk.lstrip("/") if fk else None,
            "font_file_subtype": (_name_str(self.descriptor[fk].get("/Subtype")).lstrip("/")
                                  if fk and is_stream(self.descriptor[fk]) else None),
            "font_file_size": size,
            "encoding": (_name_str(enc).lstrip("/") if isinstance(enc, pikepdf.Name)
                         else ("<custom>" if enc is not None else None)),
            "has_tounicode": "/ToUnicode" in self.obj,
            "object": self.obj_label,
            "pages": sorted(self.pages),
            "used_in": self.uses[:max_uses] + (["..."] if len(self.uses) > max_uses else []),
        }


class FontScanner:
    """Collect every distinct font dictionary reachable from pages, annotations and AcroForm /DR."""

    def __init__(self, pdf: pikepdf.Pdf):
        self.pdf = pdf
        self.fonts: list[DocFont] = []
        self._by_key: dict[tuple[int, int], DocFont] = {}
        # memo: objgen -> list[(font index, sub-path)]
        self._memo: dict[tuple[int, int], list[tuple[int, str]]] = {}
        self._active: set[tuple[int, int]] = set()

    # -- registration -------------------------------------------------
    def _register(self, fobj) -> int | None:
        if not is_dict(fobj):
            return None
        k = _key(fobj)
        if k is not None and k in self._by_key:
            return self._by_key[k].index
        df = DocFont(obj=fobj, index=len(self.fonts))
        self.fonts.append(df)
        if k is not None:
            self._by_key[k] = df
        return df.index

    # -- traversal ----------------------------------------------------
    def scan(self) -> list[DocFont]:
        for pno, page in enumerate(self.pdf.pages, start=1):
            found: list[tuple[int, str]] = []
            res = inherited(page.obj, "/Resources")
            if is_dict(res):
                found += self._scan_resources(res, "Resources", res, 0)
            annots = page.obj.get("/Annots")
            if is_array(annots):
                for i, a in enumerate(annots):
                    found += self._scan_annot(a, f"Annots[{i}]", res, 0)
            for idx, path in found:
                f = self.fonts[idx]
                f.pages.add(pno)
                label = f"page {pno}: {path}"
                if label not in f.uses and len(f.uses) < 500:
                    f.uses.append(label)
        # AcroForm default resources (fonts referenced from /DA strings)
        af = self.pdf.Root.get("/AcroForm")
        if is_dict(af) and is_dict(af.get("/DR")):
            for idx, path in self._scan_resources(af["/DR"], "AcroForm/DR", None, 0):
                self.fonts[idx].uses.append(f"AcroForm: {path}")
        return self.fonts

    def _scan_annot(self, annot, label, page_res, depth) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        if not is_dict(annot):
            return out
        ap = annot.get("/AP")
        if not is_dict(ap):
            return out
        for st in ("/N", "/D", "/R"):
            v = ap.get(st)
            if v is None:
                continue
            if is_stream(v):
                out += self._scan_form(v, f"{label}/AP{st}", page_res, depth + 1)
            elif is_dict(v):  # appearance-state dictionary: state name -> stream
                for sk in v.keys():
                    sv = v[sk]
                    if is_stream(sv):
                        out += self._scan_form(sv, f"{label}/AP{st}{sk}", page_res, depth + 1)
        return out

    def _scan_form(self, form, path, fallback_res, depth) -> list[tuple[int, str]]:
        if depth > MAX_DEPTH:
            return []
        res = form.get("/Resources")
        own = is_dict(res)
        k = _key(form)
        if own and k is not None and k in self._memo:
            return [(i, f"{path}/{p}") for i, p in self._memo[k]]
        if k is not None:
            if k in self._active:
                return []
            self._active.add(k)
        try:
            use = res if own else fallback_res
            sub = self._scan_resources(use, "Resources", use, depth + 1) if is_dict(use) else []
        finally:
            if k is not None:
                self._active.discard(k)
        if own and k is not None:
            self._memo[k] = sub
        return [(i, f"{path}/{p}") for i, p in sub]

    def _scan_resources(self, res, path, fallback_res, depth) -> list[tuple[int, str]]:
        if depth > MAX_DEPTH or not is_dict(res):
            return []
        k = _key(res)
        if k is not None:
            if k in self._memo:
                return list(self._memo[k])
            if k in self._active:
                return []
            self._active.add(k)
        out: list[tuple[int, str]] = []
        try:
            fonts = res.get("/Font")
            if is_dict(fonts):
                for fk in fonts.keys():
                    idx = self._register(fonts[fk])
                    if idx is None:
                        continue
                    out.append((idx, f"Font{fk}"))
                    out += self._scan_type3(self.fonts[idx], f"Font{fk}", res, depth)
            xo = res.get("/XObject")
            if is_dict(xo):
                for xk in xo.keys():
                    x = xo[xk]
                    if is_stream(x) and _name_str(x.get("/Subtype")) == "/Form":
                        out += self._scan_form(x, f"XObject{xk}", res, depth + 1)
            pat = res.get("/Pattern")
            if is_dict(pat):
                for pk in pat.keys():
                    p = pat[pk]
                    if is_stream(p) and is_dict(p.get("/Resources")):
                        out += self._scan_form(p, f"Pattern{pk}", res, depth + 1)
            gs = res.get("/ExtGState")
            if is_dict(gs):
                for gk in gs.keys():
                    g = gs[gk]
                    if not is_dict(g):
                        continue
                    fa = g.get("/Font")
                    if is_array(fa) and len(fa) >= 1:
                        idx = self._register(fa[0])
                        if idx is not None:
                            out.append((idx, f"ExtGState{gk}/Font"))
                    sm = g.get("/SMask")
                    if is_dict(sm) and is_stream(sm.get("/G")):
                        out += self._scan_form(sm["/G"], f"ExtGState{gk}/SMask", res, depth + 1)
        finally:
            if k is not None:
                self._active.discard(k)
        if k is not None:
            self._memo[k] = list(out)
        return list(out)

    def _scan_type3(self, df: DocFont, path, outer_res, depth) -> list[tuple[int, str]]:
        if df.subtype != "Type3":
            return []
        r = df.obj.get("/Resources")
        use = r if is_dict(r) else outer_res
        if not is_dict(use):
            return []
        return [(i, f"{path}/Type3/{p}") for i, p in self._scan_resources(use, "Resources", use, depth + 1)]


def collect_fonts(pdf: pikepdf.Pdf) -> list[DocFont]:
    return FontScanner(pdf).scan()


# ---------------------------------------------------------------------------
# list-fonts  (GetUsedFonts equivalent)
# ---------------------------------------------------------------------------

def list_fonts(pdf: pikepdf.Pdf) -> list[dict]:
    return [f.to_json() for f in collect_fonts(pdf)]


def used_font_names(pdf: pikepdf.Pdf) -> list[str]:
    """Distinct font names (subset prefix kept, like ``GetUsedFonts``)."""
    seen: dict[str, None] = {}
    for f in collect_fonts(pdf):
        if f.base_font:
            seen.setdefault(f.base_font, None)
    return list(seen)


# ---------------------------------------------------------------------------
# remove-fonts
# ---------------------------------------------------------------------------

@dataclass
class RemovedFont:
    name: str
    subset: bool
    size: int
    key: str


def _descriptors(df: DocFont):
    """Yield every FontDescriptor that may hold a font file (simple font or all descendants)."""
    if df.subtype == "Type0":
        d = df.obj.get("/DescendantFonts")
        if is_array(d):
            for sub in d:
                if is_dict(sub) and is_dict(sub.get("/FontDescriptor")):
                    yield sub["/FontDescriptor"]
    else:
        fd = df.obj.get("/FontDescriptor")
        if is_dict(fd):
            yield fd


def remove_fonts(pdf: pikepdf.Pdf, predicate=None) -> list[RemovedFont]:
    """Delete FontFile/FontFile2/FontFile3 entries whose ``predicate(RemovedFont)`` is true.

    ``predicate`` mirrors ``Predicate<PdfEmbeddedFont>`` (name, subset, size). Streams that become
    unreferenced are dropped by QPDF at save time. /Length1-3 live on the (dropped) stream, so
    nothing else needs cleaning in the descriptor.
    """
    removed: list[RemovedFont] = []
    for df in collect_fonts(pdf):
        for desc in _descriptors(df):
            for key in FONT_FILE_KEYS:
                if key not in desc or not is_stream(desc[key]):
                    continue
                size = len(desc[key].read_raw_bytes())
                info = RemovedFont(df.name, df.is_subset, size, key)
                if predicate is not None and not predicate(info):
                    continue
                del desc[key]
                removed.append(info)
    return removed


def large_font_predicate(pdf_size: int):
    """The CTS2.0 rule: only PDFs >= 10 MB are touched, and then only fonts >= 5 MB."""
    if pdf_size < LARGE_PDF_SIZE:
        return lambda f: False
    return lambda f: f.size >= LARGE_FONT_SIZE


def make_remove_predicate(min_size: int = 0, subset: bool | None = None, name_regex: str | None = None):
    """C# ``PdfRemoveFontsConverter.__RemovePredicate``: size is a threshold, fonts *larger* than it go."""
    rx = re.compile(name_regex, re.I) if name_regex else None

    def pred(f: RemovedFont) -> bool:
        if min_size > 0 and f.size <= min_size:
            return False
        if subset is not None and subset != f.subset:
            return False
        if rx is not None and not rx.search(f.name):
            return False
        return True

    return pred


def embed_fonts(pdf, catalog, **kwargs):
    """Convenience wrapper for :func:`pdfmergepy.fontembed.embed_fonts` (lazy import: circular dependency)."""
    from pdfmergepy.fontembed import embed_fonts as _embed
    return _embed(pdf, catalog, **kwargs)
