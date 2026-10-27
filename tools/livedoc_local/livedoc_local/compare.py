"""Compare two PDFs (e.g. local pipeline output vs a production CTS output).

Ported from the ad-hoc cmp_pdf.py used during parity work. Producer/dates/IDs are deliberately
ignored. Needs pikepdf (always present); text/raster/fonts/outlines/links need PyMuPDF (optional:
`pip install pymupdf`) - without it those checks are reported as skipped.
"""
from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import Any


def _r(v) -> list[float]:
    return [round(float(x), 2) for x in v]


def struct_roles(pdf) -> tuple[dict[str, int], bool]:
    import pikepdf
    roles: collections.Counter = collections.Counter()
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return {}, False
    seen: set = set()

    def walk(n) -> None:
        if isinstance(n, pikepdf.Array):
            for c in n:
                walk(c)
            return
        if not isinstance(n, pikepdf.Dictionary):
            return
        key = n.objgen
        if key != (0, 0):
            if key in seen:
                return
            seen.add(key)
        s = n.get("/S")
        if s is not None:
            roles[str(s)] += 1
        if "/K" in n:
            walk(n.K)

    walk(root)
    return dict(roles), True


def _mupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
            return pymupdf
        except ImportError:
            return None


def compare_pdfs(a: Path, b: Path, dpi: int = 50, raster_tol: float = 0.001) -> dict[str, Any]:
    """Return a structured diff. result['identical'] is True when no check reported a difference."""
    import pikepdf
    diffs: list[str] = []
    res: dict[str, Any] = {"a": str(a), "b": str(b), "checks": {}, "skipped": []}
    pa, pb = pikepdf.open(a), pikepdf.open(b)
    na, nb = len(pa.pages), len(pb.pages)
    res["pages"] = {"a": na, "b": nb}
    if na != nb:
        diffs.append(f"page count differs: {na} vs {nb}")
    n = min(na, nb)

    box = []
    for i in range(n):
        A, B = pa.pages[i], pb.pages[i]
        ba = (_r(A.MediaBox), _r(A.get("/CropBox", A.MediaBox)), int(A.get("/Rotate", 0)))
        bb = (_r(B.MediaBox), _r(B.get("/CropBox", B.MediaBox)), int(B.get("/Rotate", 0)))
        if ba != bb:
            box.append({"page": i + 1, "a": ba, "b": bb})
    res["checks"]["boxes_rotate"] = box
    diffs += [f"p{d['page']} box/rotate differ: {d['a']} vs {d['b']}" for d in box]

    ra, ta = struct_roles(pa)
    rb, tb = struct_roles(pb)
    role_diff = {k: (ra.get(k, 0), rb.get(k, 0)) for k in set(ra) | set(rb) if ra.get(k, 0) != rb.get(k, 0)}
    res["checks"]["struct"] = {"tagged_a": ta, "tagged_b": tb, "role_diff": role_diff}
    if ta != tb:
        diffs.append(f"tagged differs: {ta} vs {tb}")
    if role_diff:
        diffs.append(f"structure role counts differ: {role_diff}")

    def annots(pdf) -> int:
        return sum(len(p.get("/Annots", [])) for p in pdf.pages)
    la, lb = annots(pa), annots(pb)
    res["checks"]["annots"] = {"a": la, "b": lb}
    if la != lb:
        diffs.append(f"annotation count differs: {la} vs {lb}")

    mu = _mupdf()
    if mu is None:
        res["skipped"].append("text/raster/fonts/outlines/links (PyMuPDF not installed: pip install pymupdf)")
    else:
        da, db = mu.open(str(a)), mu.open(str(b))
        text_d, ras_d = [], []
        mat = mu.Matrix(dpi / 72, dpi / 72)
        for i in range(n):
            if " ".join(da[i].get_text().split()) != " ".join(db[i].get_text().split()):
                text_d.append(i + 1)
            xa, xb = da[i].get_pixmap(matrix=mat, alpha=False), db[i].get_pixmap(matrix=mat, alpha=False)
            if (xa.width, xa.height) != (xb.width, xb.height):
                ras_d.append({"page": i + 1, "reason": "size", "a": [xa.width, xa.height], "b": [xb.width, xb.height]})
            else:
                bad = sum(1 for x, y in zip(xa.samples, xb.samples) if abs(x - y) > 24)
                frac = bad / max(len(xa.samples), 1)
                if frac > raster_tol:
                    ras_d.append({"page": i + 1, "reason": "pixels", "fraction": round(frac, 5)})
        res["checks"]["text_pages_differ"] = text_d
        res["checks"]["raster_pages_differ"] = ras_d
        if text_d:
            diffs.append(f"text differs on pages {text_d}")
        if ras_d:
            diffs.append(f"raster differs on pages {[d['page'] for d in ras_d]} (dpi {dpi})")
        oa, ob = da.get_toc(simple=True), db.get_toc(simple=True)
        res["checks"]["outlines"] = {"a": len(oa), "b": len(ob), "same": oa == ob}
        if oa != ob:
            diffs.append(f"outlines differ ({len(oa)} vs {len(ob)} entries)")

        def fonts(doc):
            c: collections.Counter = collections.Counter()
            for i in range(doc.page_count):
                for f in doc.get_page_fonts(i):
                    c[(re.sub(r"^[A-Z]{6}\+", "", f[3]), f[2])] += 1
            return c
        fa, fb = fonts(da), fonts(db)
        fd = {"only_a": [f"{k[0]}/{k[1]}" for k in (fa - fb)], "only_b": [f"{k[0]}/{k[1]}" for k in (fb - fa)]}
        res["checks"]["fonts"] = fd
        if fd["only_a"] or fd["only_b"]:
            diffs.append(f"fonts differ: only-A {fd['only_a'][:6]} only-B {fd['only_b'][:6]}")
        links = (sum(len(p.get_links()) for p in da), sum(len(p.get_links()) for p in db))
        res["checks"]["links"] = {"a": links[0], "b": links[1]}
        if links[0] != links[1]:
            diffs.append(f"link count differs: {links[0]} vs {links[1]}")
    res["differences"] = diffs
    res["identical"] = not diffs
    return res


def format_compare(res: dict[str, Any]) -> str:
    lines = [f"A: {res['a']}", f"B: {res['b']}", f"pages: A={res['pages']['a']} B={res['pages']['b']}"]
    lines += [f"  DIFF {d}" for d in res["differences"]]
    lines += [f"  skipped: {s}" for s in res["skipped"]]
    lines.append("RESULT: " + ("identical (within ignored noise)" if res["identical"] else
                               f"{len(res['differences'])} difference(s)"))
    return "\n".join(lines)
