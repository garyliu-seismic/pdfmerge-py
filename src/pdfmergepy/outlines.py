"""Outline (bookmark) cloning for merged output.

Ports the outline side of iText's ``PdfDocument.CopyPagesTo`` as used by
CTS2.0's ``CustomMerger.Merge`` (PdfMerger.cs): every page copied natively
from a source PDF brings along the source outline items whose destination is
that page, plus their ancestors.  Observed behaviour (verified against the
Seismic.CTS.Converter.LiveDoc exe, nested and flat outlines):

- cloning is per copied page, appended to the destination outline root in
  output page order; items are NOT de-duplicated across calls, so a container
  whose children sit on different pages shows up once per page;
- an item is cloned when its destination is the copied page; its ancestors
  are cloned as containers *without* a destination, only for the calls in
  which a descendant is cloned;
- destinations are re-pointed at the new page and keep their fit parameters;
- a container's /Count is its number of cloned direct children, positive when
  the source item was open, negative when closed; the root's /Count is its
  number of top-level children;
- pages that are never copied natively (the main page of a replaced slide,
  hidden slides) contribute no outline items.

``MergeNew`` initialises an (initially empty) outline root unconditionally.
"""

from __future__ import annotations

from typing import Optional

import pikepdf
from pikepdf import Array, Dictionary, Name

from pdfmergepy.prune import (
    Og,
    _as_list,
    _is_array,
    _is_dict,
    _iter_tree,
    _og,
)

_MAX_ITEMS = 1_000_000          # cycle / runaway guard for hostile outline trees


def _named_dests(pdf: pikepdf.Pdf) -> dict:
    """Map every named destination to its explicit destination array."""
    out: dict = {}

    def explicit(v):
        if v is not None and _is_dict(v):
            v = v.get("/D")
        return v if v is not None and _is_array(v) and len(v) > 0 else None

    names = pdf.Root.get("/Names")
    if names is not None and _is_dict(names) and "/Dests" in names:
        for k, v in _iter_tree(names["/Dests"], "/Names"):
            d = explicit(v)
            if d is not None:
                out[str(k)] = d
    legacy = pdf.Root.get("/Dests")
    if legacy is not None and _is_dict(legacy):
        for k in list(legacy.keys()):
            d = explicit(legacy[k])
            if d is not None:
                out[k[1:] if k.startswith("/") else k] = d
    return out


class _Node:
    __slots__ = ("item", "parent", "dest", "page", "open")

    def __init__(self, item, parent: int, dest, page: Optional[Og], is_open: bool):
        self.item = item
        self.parent = parent          # index into the node list, -1 for top level
        self.dest = dest              # explicit destination array or None
        self.page = page              # objgen of the destination page or None
        self.open = is_open


def _resolve_dest(item, named: dict):
    """Explicit destination array of an outline item (/Dest, or /A GoTo /D)."""
    d = item.get("/Dest")
    if d is None:
        a = item.get("/A")
        if a is not None and _is_dict(a) and a.get("/S") == "/GoTo":
            d = a.get("/D")
    if d is None:
        return None
    if _is_dict(d):
        d = d.get("/D")
    if d is not None and not _is_array(d):
        try:
            key = str(d)
            d = named.get(key)
            if d is None and key.startswith("/"):
                d = named.get(key[1:])
        except Exception:
            d = None
    if d is None or not _is_array(d) or len(d) == 0:
        return None
    return d


class _SourceOutlines:
    """Pre-order index of one source PDF's outline tree, keyed by dest page."""

    def __init__(self, src: pikepdf.Pdf):
        self.nodes: list[_Node] = []
        self.by_page: dict[Og, list[int]] = {}
        root = src.Root.get("/Outlines")
        if root is None or not _is_dict(root):
            return
        named = _named_dests(src)
        seen: set = set()
        # explicit stack of (first-child, parent index); siblings walk /Next
        stack = [(root.get("/First"), -1)]
        while stack and len(self.nodes) < _MAX_ITEMS:
            item, parent = stack.pop()
            if item is None or not _is_dict(item):
                continue
            og = _og(item)
            if og is not None:
                if og in seen:
                    continue
                seen.add(og)
            stack.append((item.get("/Next"), parent))        # next sibling after this subtree
            dest = _resolve_dest(item, named)
            page = _og(dest[0]) if dest is not None else None
            try:
                is_open = int(item.get("/Count", 0)) > 0
            except Exception:
                is_open = False
            idx = len(self.nodes)
            self.nodes.append(_Node(item, parent, dest, page, is_open))
            if page is not None:
                self.by_page.setdefault(page, []).append(idx)
            stack.append((item.get("/First"), idx))


class OutlineMerger:
    """Builds the destination outline tree page by page."""

    def __init__(self, dst: pikepdf.Pdf):
        self.dst = dst
        self._root = None
        self._top: list = []                 # cloned top-level items, in order
        self._cache: dict[int, _SourceOutlines] = {}
        self._keepalive: list = []           # keep indexed sources' ids unique
        self.ensure_root()

    def ensure_root(self):
        """C# ``InitializeOutlines``: the output always has an /Outlines root."""
        if self._root is None:
            self._root = self.dst.make_indirect(Dictionary(Type=Name.Outlines))
            self.dst.Root.Outlines = self._root
        return self._root

    def _index(self, src: pikepdf.Pdf) -> _SourceOutlines:
        key = id(src)
        idx = self._cache.get(key)
        if idx is None:
            idx = self._cache[key] = _SourceOutlines(src)
            self._keepalive.append(src)
        return idx

    def copy_page(self, src: pikepdf.Pdf, src_page_index: int, dst_page_obj) -> int:
        """Clone the outline items of ``src`` page ``src_page_index`` that point
        at the page just appended to the destination as ``dst_page_obj``.

        Returns the number of cloned items.
        """
        index = self._index(src)
        if not index.nodes:
            return 0
        page_og = src.pages[src_page_index].obj.objgen
        sel = index.by_page.get(page_og)
        if not sel:
            return 0

        keep: set[int] = set()
        for i in sel:
            while i >= 0 and i not in keep:
                keep.add(i)
                i = index.nodes[i].parent
        selected = set(sel)

        dst = self.dst
        clones: dict[int, pikepdf.Object] = {}
        kids: dict[int, list] = {}
        for i in sorted(keep):                       # pre-order == ascending index
            node = index.nodes[i]
            d = Dictionary()
            title = node.item.get("/Title")
            if title is not None:
                d["/Title"] = dst.copy_foreign(title) if title.is_indirect else title
            if i in selected:
                d["/Dest"] = Array([dst_page_obj] + [x for x in list(node.dest)[1:]])
            color = node.item.get("/C")
            if color is not None and _is_array(color):
                d["/C"] = Array([float(v) for v in color])
            flags = node.item.get("/F")
            if flags is not None:
                d["/F"] = int(flags)
            clone = dst.make_indirect(d)
            clones[i] = clone
            if node.parent >= 0:
                kids.setdefault(node.parent, []).append(clone)
                clone["/Parent"] = clones[node.parent]
            else:
                clone["/Parent"] = self._root
                if self._top:
                    clone["/Prev"] = self._top[-1]
                    self._top[-1]["/Next"] = clone
                self._top.append(clone)

        for i, ch in kids.items():
            parent = clones[i]
            for a, b in zip(ch, ch[1:]):
                a["/Next"] = b
                b["/Prev"] = a
            parent["/First"], parent["/Last"] = ch[0], ch[-1]
            parent["/Count"] = len(ch) if index.nodes[i].open else -len(ch)

        root = self._root
        root["/First"], root["/Last"] = self._top[0], self._top[-1]
        root["/Count"] = len(self._top)
        return len(clones)
