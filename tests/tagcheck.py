"""Struct-tree / ParentTree integrity checker used by the page-op tests.

``tag_problems(pdf)`` returns a ``collections.Counter`` keyed by problem
category (so a *source* PDF with pre-existing defects can be used as the
baseline: the output must not have more problems of any category).
"""
from __future__ import annotations

from collections import Counter

import pikepdf


def _og(o):
    return o.objgen if isinstance(o, pikepdf.Object) and o.is_indirect else None


def _kids(node):
    k = node.get("/K")
    if k is None:
        return []
    if isinstance(k, pikepdf.Array):
        return list(k)
    return [k]


def _isint(o):
    return isinstance(o, int) or (isinstance(o, pikepdf.Object) and o._type_code == pikepdf.ObjectType.integer)


def _isdict(o):
    return isinstance(o, pikepdf.Object) and o._type_code == pikepdf.ObjectType.dictionary


def _parent_tree_pairs(pt):
    stack = [pt]
    while stack:
        n = stack.pop()
        if n is None or not _isdict(n):
            continue
        nums = n.get("/Nums")
        if nums is not None:
            for i in range(0, len(nums) - 1, 2):
                yield int(nums[i]), nums[i + 1]
        kids = n.get("/Kids")
        if kids is not None:
            stack.extend(list(kids))


def count_orphan_pages(pdf: pikepdf.Pdf) -> int:
    pages = {p.obj.objgen for p in pdf.pages}
    n = 0
    for o in pdf.objects:
        if isinstance(o, pikepdf.Object) and _isdict(o) and o.get("/Type") == "/Page" and o.objgen not in pages:
            n += 1
    return n


def tag_problems(pdf: pikepdf.Pdf) -> Counter:
    bad: Counter = Counter()
    pages = {p.obj.objgen for p in pdf.pages}
    bad["orphan_page_objects"] += count_orphan_pages(pdf)
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return bad

    elems: dict = {}               # objgen -> elem
    mcids: dict = {}               # (page_og, mcid) -> elem og
    stack = [(root, None)]
    seen = set()
    while stack:
        parent, _ = stack.pop()
        pg_default = parent.get("/Pg")
        for k in _kids(parent):
            if _isint(k):
                pg = _og(pg_default)
                if pg is None:
                    bad["mcid_without_pg"] += 1
                elif pg not in pages:
                    bad["dangling_pg"] += 1
                else:
                    mcids[(pg, int(k))] = _og(parent)
            elif _isdict(k):
                t = k.get("/Type")
                if t == "/MCR":
                    pg = _og(k.get("/Pg", pg_default))
                    if pg is None or pg not in pages:
                        bad["dangling_mcr_pg"] += 1
                    elif "/Stm" not in k:
                        mcids[(pg, int(k["/MCID"]))] = _og(parent)
                elif t == "/OBJR":
                    pg = k.get("/Pg", pg_default)
                    if pg is not None and _og(pg) not in pages:
                        bad["dangling_objr_pg"] += 1
                    if k.get("/Obj") is None:
                        bad["objr_without_obj"] += 1
                else:
                    og = _og(k)
                    if og in seen:
                        continue
                    seen.add(og)
                    elems[og] = k
                    if k.get("/Pg") is not None and _og(k["/Pg"]) not in pages:
                        bad["dangling_elem_pg"] += 1
                    if k.get("/P") is not None and _og(k["/P"]) != _og(parent) and _og(parent) is not None:
                        bad["wrong_parent_P"] += 1
                    if "/_PruneDead" in k:
                        bad["dead_marker_leaked"] += 1
                    stack.append((k, parent))

    pt = root.get("/ParentTree")
    entries = dict(_parent_tree_pairs(pt)) if pt is not None else {}
    for key, val in entries.items():
        items = list(val) if isinstance(val, pikepdf.Array) else [val]
        for e in items:
            if e is None or (isinstance(e, pikepdf.Object) and e._type_code == pikepdf.ObjectType.null):
                continue
            if _og(e) not in elems:
                bad["parenttree_unreachable_elem"] += 1
    for p in pdf.pages:
        sp = p.obj.get("/StructParents")
        if sp is None:
            continue
        val = entries.get(int(sp))
        if val is None:
            bad["page_structparents_missing_key"] += 1
            continue
        arr = list(val) if isinstance(val, pikepdf.Array) else []
        pg = p.obj.objgen
        for (mpg, mcid), eog in mcids.items():
            if mpg != pg:
                continue
            if mcid >= len(arr) or _og(arr[mcid]) != eog:
                bad["mcid_not_in_parenttree"] += 1
    return bad


def acroform_field_names(pdf: pikepdf.Pdf) -> list[str]:
    """Fully-qualified names of terminal fields reachable from /AcroForm."""
    af = pdf.Root.get("/AcroForm")
    out: list[str] = []
    if af is None:
        return out

    def walk(node, prefix):
        t = node.get("/T")
        name = prefix + ("." if prefix and t is not None else "") + (str(t) if t is not None else "")
        kids = node.get("/Kids")
        field_kids = [k for k in (kids or []) if "/T" in k or "/Kids" in k]
        if kids is not None and field_kids:
            for k in kids:
                walk(k, name)
        else:
            out.append(name)

    for f in af.get("/Fields", []):
        walk(f, "")
    return out


def widget_pages(pdf: pikepdf.Pdf) -> int:
    return sum(1 for p in pdf.pages for a in p.obj.get("/Annots", []) if a.get("/Subtype") == "/Widget")
