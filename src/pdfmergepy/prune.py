"""Delete pages from an open document and repair everything that pointed at them.

This is the heart of select / delete / split.  iText7's ``CopyPagesTo`` builds a
*new* document and copies the tag tree / ParentTree / AcroForm fields that
belong to the copied pages.  pikepdf's ``add_pages_from`` does not copy the tag
tree (it would dangle), so we invert the problem: keep the whole source
document, delete the unwanted pages, then **prune** every structure that still
references the removed pages:

* ``/StructTreeRoot``: drop MCID / MCR / OBJR kids that live on removed pages,
  cascade-delete StructElems left with no kids, clear ``/Pg`` of survivors that
  pointed at a removed page, filter ``/Ref`` and ``/IDTree``;
* ``/ParentTree``: drop entries whose elements are all gone, null out removed
  elements inside surviving arrays (array index == MCID, so no compaction),
  rewrite as a flat ``/Nums`` (valid, avoids stale ``/Limits``);
* ``/AcroForm``: keep only fields that have a widget on a kept page (plus
  widget-less fields); drop ``/XFA`` (no longer matches the page set);
* ``/Outlines``: remove items whose destination page is gone (items with kept
  descendants stay, minus their destination), relink and recount;
* named destinations, ``/Dest`` / ``/A`` GoTo on kept annotations,
  ``/OpenAction``, ``/PageLabels`` (re-indexed so the *displayed* labels of the
  kept pages do not change);
* a final safety-net scan nulls any remaining reference to a removed page so
  an orphan page (with all its content) can never be dragged into the output.

All walks are iterative (no recursion limit problems on deep tag trees).
"""

from __future__ import annotations

import bisect
import logging
from typing import Iterator, Optional

import pikepdf
from pikepdf import Array, Dictionary, Name, Object, ObjectType

log = logging.getLogger(__name__)

_DEAD = "/_PruneDead"
Og = tuple[int, int]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _og(obj) -> Optional[Og]:
    """objgen of an indirect object, else None."""
    if isinstance(obj, Object) and obj.is_indirect:
        return obj.objgen
    return None


def _tc(o):
    return o._type_code if isinstance(o, Object) else None


def _is_dict(o) -> bool:
    return _tc(o) == ObjectType.dictionary


def _is_array(o) -> bool:
    return _tc(o) == ObjectType.array


def _is_int(o) -> bool:
    return isinstance(o, int) or _tc(o) == ObjectType.integer


def _is_null(o) -> bool:
    return o is None or _tc(o) == ObjectType.null


def _is_container(o) -> bool:
    return _tc(o) in (ObjectType.dictionary, ObjectType.stream, ObjectType.array)


def _as_list(k) -> list:
    if k is None or _is_null(k):
        return []
    if _is_array(k):
        return list(k)
    return [k]


def _iter_tree(node, leaf_key: str) -> Iterator[tuple[object, object]]:
    """Yield (key, value) pairs of a name/number tree, iteratively."""
    stack = [node]
    seen: set = set()
    while stack:
        n = stack.pop()
        if n is None or not _is_dict(n):
            continue
        og = _og(n)
        if og is not None:
            if og in seen:
                continue
            seen.add(og)
        leaf = n.get(leaf_key)
        if leaf is not None and _is_array(leaf):
            for i in range(0, len(leaf) - 1, 2):
                yield leaf[i], leaf[i + 1]
        kids = n.get("/Kids")
        if kids is not None and _is_array(kids):
            stack.extend(reversed(list(kids)))


def _write_flat_tree(node, leaf_key: str, pairs: list) -> None:
    arr = Array()
    for k, v in pairs:
        arr.append(k)
        arr.append(v)
    for stale in ("/Kids", "/Limits"):
        if stale in node:
            del node[stale]
    node[leaf_key] = arr


def _dest_page_og(dest, name_pages: dict) -> tuple[bool, Optional[Og]]:
    """Resolve a destination to (is_page_dest, page objgen)."""
    if dest is None or _is_null(dest):
        return False, None
    if _is_dict(dest):
        return _dest_page_og(dest.get("/D"), name_pages)
    if _is_array(dest):
        if len(dest) == 0:
            return False, None
        og = _og(dest[0])
        return (og is not None), og
    try:                                   # name / string -> named destination
        key = str(dest)
    except Exception:
        return False, None
    if key in name_pages:
        return True, name_pages[key]
    return False, None


def _is_mcr(k) -> bool:
    t = k.get("/Type")
    return t == "/MCR" or (t is None and "/MCID" in k)


def _is_objr(k) -> bool:
    t = k.get("/Type")
    return t == "/OBJR" or (t is None and "/Obj" in k and "/MCID" not in k and "/S" not in k)


# --------------------------------------------------------------------------
# struct tree
# --------------------------------------------------------------------------

def _prune_struct_tree(pdf: pikepdf.Pdf, removed: set[Og]) -> set[Og]:
    """Prune StructTreeRoot + ParentTree.  Returns objgens of deleted StructElems."""
    root = pdf.Root.get("/StructTreeRoot")
    if root is None:
        return set()

    def elem_kids(parent) -> list:
        return [k for k in _as_list(parent.get("/K"))
                if _is_dict(k) and not _is_mcr(k) and not _is_objr(k)]

    nodes: list = []            # StructElems in pre-order
    seen: set = set()
    stack = list(reversed(elem_kids(root)))
    while stack:
        n = stack.pop()
        og = _og(n)
        if og is not None:
            if og in seen:
                continue
            seen.add(og)
        nodes.append(n)
        stack.extend(reversed(elem_kids(n)))

    removed_elems: set[Og] = set()

    def mark_dead(node) -> None:
        node[_DEAD] = True
        og = _og(node)
        if og:
            removed_elems.add(og)

    def prune_node(node, is_root: bool) -> None:
        node_pg = node.get("/Pg")
        node_pg_gone = node_pg is not None and _og(node_pg) in removed
        items = _as_list(node.get("/K"))
        keep = []
        changed = False
        for k in items:
            if _is_int(k):
                if node_pg_gone:
                    changed = True
                    continue
            elif _is_dict(k):
                if _is_mcr(k):
                    pg = k.get("/Pg", node_pg)
                    if pg is not None and _og(pg) in removed:
                        changed = True
                        continue
                elif _is_objr(k):
                    pg = k.get("/Pg", node_pg)
                    obj = k.get("/Obj")
                    if _is_null(obj):
                        changed = True
                        continue
                    if pg is None and _is_dict(obj):
                        pg = obj.get("/P")
                    if pg is not None and _og(pg) in removed:
                        changed = True
                        continue
                elif k.get(_DEAD):
                    changed = True
                    continue
            keep.append(k)
        if changed:
            if keep:
                node["/K"] = Array(keep)
            else:
                del node["/K"]
        if node_pg_gone and not is_root:
            del node["/Pg"]
        if not is_root and not keep and (changed or node_pg_gone):
            mark_dead(node)

    for node in reversed(nodes):          # descendants before ancestors
        prune_node(node, False)
    prune_node(root, True)

    if removed_elems:
        # /Ref (elements pointing at other elements)
        for node in nodes:
            if node.get(_DEAD):
                continue
            ref = node.get("/Ref")
            if ref is not None and _is_array(ref):
                good = [r for r in ref if _og(r) not in removed_elems]
                if len(good) != len(ref):
                    if good:
                        node["/Ref"] = Array(good)
                    else:
                        del node["/Ref"]
        # /IDTree
        idt = root.get("/IDTree")
        if idt is not None:
            pairs = [(k, v) for k, v in _iter_tree(idt, "/Names") if _og(v) not in removed_elems]
            _write_flat_tree(idt, "/Names", pairs)
        # /ParentTree
        pt = root.get("/ParentTree")
        if pt is not None:
            out = []
            for k, v in _iter_tree(pt, "/Nums"):
                if _is_array(v):
                    items = list(v)
                    dead = [_og(e) in removed_elems for e in items]
                    if any(dead):
                        if all(d or _is_null(e) for d, e in zip(dead, items)):
                            continue
                        v = Array([None if d else e for d, e in zip(dead, items)])
                elif _og(v) in removed_elems:
                    continue
                out.append((k, v))
            _write_flat_tree(pt, "/Nums", out)
    return removed_elems


# --------------------------------------------------------------------------
# AcroForm
# --------------------------------------------------------------------------

def _prune_acroform(pdf: pikepdf.Pdf, kept_widgets: set[Og]) -> None:
    af = pdf.Root.get("/AcroForm")
    if af is None:
        return
    if "/XFA" in af:          # XFA packets describe the whole, un-subsetted form
        del af["/XFA"]
    fields = af.get("/Fields")
    if fields is None or not _is_array(fields) or len(fields) == 0:
        return

    nodes: list = []
    seen: set = set()
    stack = list(reversed(list(fields)))
    while stack:
        n = stack.pop()
        if not _is_dict(n):
            continue
        og = _og(n)
        if og is not None:
            if og in seen:
                continue
            seen.add(og)
        nodes.append(n)
        kids = n.get("/Kids")
        if kids is not None and _is_array(kids):
            stack.extend(reversed(list(kids)))

    dead_og: set[Og] = set()
    for n in reversed(nodes):
        kids = n.get("/Kids")
        if kids is not None and _is_array(kids) and len(kids):
            good = [k for k in kids if not (_is_dict(k) and k.get(_DEAD))]
            if len(good) != len(kids):
                n["/Kids"] = Array(good)
            if not good:
                n[_DEAD] = True
        elif n.get("/Subtype") == "/Widget" and _og(n) is not None and _og(n) not in kept_widgets:
            n[_DEAD] = True
        # widget-less terminal fields are kept (harmless; may carry calc info)
        if n.get(_DEAD) and _og(n):
            dead_og.add(_og(n))

    good_top = [f for f in fields if not (_is_dict(f) and f.get(_DEAD))]
    if len(good_top) != len(fields):
        af["/Fields"] = Array(good_top)
    co = af.get("/CO")
    if co is not None and _is_array(co):
        af["/CO"] = Array([f for f in co if _og(f) not in dead_og])
    if not good_top:
        del pdf.Root["/AcroForm"]


# --------------------------------------------------------------------------
# destinations / outlines / labels
# --------------------------------------------------------------------------

def _collect_name_pages(pdf: pikepdf.Pdf) -> dict:
    """Map every named destination to its page objgen (None if not a page dest)."""
    out: dict = {}
    names = pdf.Root.get("/Names")
    if names is not None and _is_dict(names) and "/Dests" in names:
        for k, v in _iter_tree(names["/Dests"], "/Names"):
            out[str(k)] = _dest_page_og(v, {})[1]
    legacy = pdf.Root.get("/Dests")
    if legacy is not None and _is_dict(legacy):
        for k in list(legacy.keys()):
            out[k[1:] if k.startswith("/") else k] = _dest_page_og(legacy[k], {})[1]
    return out


def _prune_named_dests(pdf: pikepdf.Pdf, removed: set[Og]) -> None:
    names = pdf.Root.get("/Names")
    if names is not None and _is_dict(names) and "/Dests" in names:
        tree = names["/Dests"]
        pairs = list(_iter_tree(tree, "/Names"))
        good = [(k, v) for k, v in pairs if _dest_page_og(v, {})[1] not in removed]
        if len(good) != len(pairs):
            _write_flat_tree(tree, "/Names", good)
    legacy = pdf.Root.get("/Dests")
    if legacy is not None and _is_dict(legacy):
        for k in list(legacy.keys()):
            if _dest_page_og(legacy[k], {})[1] in removed:
                del legacy[k]


def _action_dest(action):
    if action is not None and _is_dict(action) and action.get("/S") == "/GoTo":
        return action.get("/D")
    return None


def _dest_is_removed(dest, name_pages: dict, removed: set[Og]) -> bool:
    is_page, og = _dest_page_og(dest, name_pages)
    return is_page and og in removed


def _fix_annot_dests(kept_pages, name_pages: dict, removed: set[Og]) -> None:
    for page in kept_pages:
        annots = page.obj.get("/Annots")
        if annots is None or not _is_array(annots):
            continue
        for a in annots:
            if not _is_dict(a):
                continue
            d = a.get("/Dest")
            if d is not None and _dest_is_removed(d, name_pages, removed):
                del a["/Dest"]
            ad = _action_dest(a.get("/A"))
            if ad is not None and _dest_is_removed(ad, name_pages, removed):
                del a["/A"]


def _fix_open_action(pdf: pikepdf.Pdf, name_pages: dict, removed: set[Og]) -> None:
    oa = pdf.Root.get("/OpenAction")
    if oa is None:
        return
    d = oa if _is_array(oa) else _action_dest(oa)
    if d is not None and _dest_is_removed(d, name_pages, removed):
        del pdf.Root["/OpenAction"]


def _prune_outlines(pdf: pikepdf.Pdf, name_pages: dict, removed: set[Og]) -> None:
    root = pdf.Root.get("/Outlines")
    if root is None or not _is_dict(root):
        return

    def key(item):
        return _og(item) or id(item)

    def children(item) -> list:
        out, seen = [], set()
        cur = item.get("/First")
        while cur is not None and _is_dict(cur):
            og = _og(cur)
            if og in seen:
                break
            seen.add(og)
            out.append(cur)
            cur = cur.get("/Next")
        return out

    def dest_gone(item) -> bool:
        d = item.get("/Dest")
        if d is None:
            d = _action_dest(item.get("/A"))
        return d is not None and _dest_is_removed(d, name_pages, removed)

    # iterative post-order
    order: list = []
    kids_of: dict = {}
    was_open: dict = {}
    stack = [(root, False)]
    while stack:
        item, visited = stack.pop()
        if visited:
            order.append(item)
            continue
        ch = children(item)
        kids_of[key(item)] = ch
        was_open[key(item)] = int(item.get("/Count", 0)) > 0
        stack.append((item, True))
        stack.extend((c, False) for c in ch)

    dead: set = set()
    open_count: dict = {}        # visible descendants if the item were expanded
    root_key = key(root)
    for item in order:
        k = key(item)
        is_root = k == root_key
        ch = [c for c in kids_of[k] if key(c) not in dead]
        gone = (not is_root) and dest_gone(item)
        if gone and not ch:
            dead.add(k)
            continue
        if gone:
            for name in ("/Dest", "/A"):
                if name in item:
                    del item[name]
        if len(ch) != len(kids_of[k]):
            for i, c in enumerate(ch):
                c["/Parent"] = item
                if i > 0:
                    c["/Prev"] = ch[i - 1]
                elif "/Prev" in c:
                    del c["/Prev"]
                if i < len(ch) - 1:
                    c["/Next"] = ch[i + 1]
                elif "/Next" in c:
                    del c["/Next"]
            if ch:
                item["/First"], item["/Last"] = ch[0], ch[-1]
            else:
                for name in ("/First", "/Last"):
                    if name in item:
                        del item[name]
        n = sum(1 + (open_count[key(c)] if was_open[key(c)] else 0) for c in ch)
        open_count[k] = n
        if n == 0:
            if "/Count" in item:
                del item["/Count"]
        else:
            item["/Count"] = n if (was_open[k] or is_root) else -n


def _remap_page_labels(pdf: pikepdf.Pdf, kept_old_idx: list[int]) -> None:
    pl = pdf.Root.get("/PageLabels")
    if pl is None or not _is_dict(pl):
        return
    ranges = sorted(((int(k), v) for k, v in _iter_tree(pl, "/Nums")), key=lambda t: t[0])
    if not ranges:
        return
    keys = [k for k, _ in ranges]
    pairs: list = []
    prev_range = None
    prev_old = None
    for new_idx, old in enumerate(kept_old_idx):
        pos = bisect.bisect_right(keys, old) - 1
        if prev_range == pos and prev_old is not None and old == prev_old + 1:
            prev_old = old
            continue
        d = Dictionary()
        if pos >= 0 and _is_dict(ranges[pos][1]):
            entry, start = ranges[pos][1], keys[pos]
            for name in ("/S", "/P"):
                if name in entry:
                    d[name] = entry[name]
            st = int(entry.get("/St", 1)) + (old - start)
        else:
            d["/S"] = Name("/D")
            st = old + 1
        if st != 1:
            d["/St"] = st
        pairs.append((new_idx, d))
        prev_range, prev_old = pos, old
    if pairs and pairs[0][0] != 0:
        pairs.insert(0, (0, Dictionary(S=Name("/D"))))
    _write_flat_tree(pl, "/Nums", pairs)


# --------------------------------------------------------------------------
# safety net
# --------------------------------------------------------------------------

def _scrub_dangling_refs(pdf: pikepdf.Pdf, removed: set[Og]) -> int:
    """Null every remaining reference to a removed page; return the count."""
    hits = 0
    for obj in pdf.objects:
        if not _is_container(obj) or _og(obj) in removed:
            continue
        stack = [obj]
        while stack:
            c = stack.pop()
            if _is_array(c):
                for i in range(len(c)):
                    v = c[i]
                    if _og(v) in removed:
                        c[i] = None
                        hits += 1
                    elif _is_container(v) and not v.is_indirect:
                        stack.append(v)
            else:
                for k in list(c.keys()):
                    v = c[k]
                    if _og(v) in removed:
                        del c[k]
                        hits += 1
                    elif _is_container(v) and not v.is_indirect:
                        stack.append(v)
    return hits


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------

def keep_only_pages(pdf: pikepdf.Pdf, keep_indices: list[int], *, safety_scan: bool = True) -> dict:
    """Keep exactly *keep_indices* (0-based) of *pdf*, in place, original order.

    Returns a small stats dict (used by tests / diagnostics).
    """
    n = len(pdf.pages)
    keep = sorted(set(keep_indices))
    stats = {"removed_pages": 0, "removed_struct_elems": 0, "scrubbed_refs": 0}
    if len(keep) == n:
        return stats
    keep_set = set(keep)
    removed_idx = [i for i in range(n) if i not in keep_set]
    removed: set[Og] = {pdf.pages[i].obj.objgen for i in removed_idx}
    kept_pages = [pdf.pages[i] for i in keep]

    kept_widgets: set[Og] = set()
    for p in kept_pages:
        annots = p.obj.get("/Annots")
        if annots is not None and _is_array(annots):
            for a in annots:
                if _is_dict(a) and a.get("/Subtype") == "/Widget" and _og(a):
                    kept_widgets.add(_og(a))

    name_pages = _collect_name_pages(pdf)

    runs: list[list[int]] = []              # contiguous removed runs
    for i in removed_idx:
        if runs and runs[-1][1] == i:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    for a, b in reversed(runs):
        del pdf.pages[a:b]
    stats["removed_pages"] = len(removed_idx)

    stats["removed_struct_elems"] = len(_prune_struct_tree(pdf, removed))
    _prune_acroform(pdf, kept_widgets)
    _prune_outlines(pdf, name_pages, removed)
    _prune_named_dests(pdf, removed)
    _fix_annot_dests(kept_pages, name_pages, removed)
    _fix_open_action(pdf, name_pages, removed)
    _remap_page_labels(pdf, keep)
    if safety_scan:
        stats["scrubbed_refs"] = _scrub_dangling_refs(pdf, removed)
    return stats
