"""Prototype: manual StructTree merge for tagged PDFs (plain-copy path)."""
import pikepdf, tempfile
from pathlib import Path


def make_tagged_single_page(path, role="/Slide", mcid_text=b"Hello"):
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))

    struct_root = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/StructTreeRoot")))
    rolemap = pikepdf.Dictionary()
    rolemap["/Slide"] = pikepdf.Name("/Sect")
    struct_root["/RoleMap"] = rolemap

    doc_elem = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/Document"), P=struct_root))
    slide_elem = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name(role), P=doc_elem, Pg=page.obj))
    para_elem = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"), S=pikepdf.Name("/P"), P=slide_elem, Pg=page.obj))

    mcr = pikepdf.Dictionary()
    mcr["/Type"] = pikepdf.Name("/MCR")
    mcr["/Pg"] = page.obj
    mcr["/MCID"] = pikepdf.Integer(0)
    para_elem["/K"] = mcr

    slide_elem["/K"] = pikepdf.Array([para_elem])
    doc_elem["/K"] = pikepdf.Array([slide_elem])
    struct_root["/K"] = pikepdf.Array([doc_elem])

    pt = pikepdf.Dictionary()
    pt["/Nums"] = pikepdf.Array([pikepdf.Integer(0), pikepdf.Array([para_elem])])
    struct_root["/ParentTree"] = pt
    struct_root["/ParentTreeNextKey"] = pikepdf.Integer(1)

    page.obj["/StructParents"] = pikepdf.Integer(0)
    pdf.Root["/StructTreeRoot"] = struct_root
    markinfo = pikepdf.Dictionary()
    markinfo["/Marked"] = pikepdf.Boolean(True)
    pdf.Root["/MarkInfo"] = markinfo
    page.obj["/Contents"] = pdf.make_stream(
        b"/P <</MCID 0>> BDC (" + mcid_text + b") Tj EMC"
    )
    pdf.save(path)


def _is_tagged(pdf):
    return "/StructTreeRoot" in pdf.Root


def _ensure_dst_tagged(dst):
    if "/StructTreeRoot" not in dst.Root:
        sr = dst.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name("/StructTreeRoot")))
        sr["/RoleMap"] = pikepdf.Dictionary()
        sr["/ParentTreeNextKey"] = pikepdf.Integer(0)
        pt = pikepdf.Dictionary()
        pt["/Nums"] = pikepdf.Array()
        sr["/ParentTree"] = pt
        dst.Root["/StructTreeRoot"] = sr
        markinfo = pikepdf.Dictionary()
        markinfo["/Marked"] = pikepdf.Boolean(True)
        dst.Root["/MarkInfo"] = markinfo
    return dst.Root["/StructTreeRoot"]


def _get_or_create_document(dst, dst_root):
    k = dst_root.get("/K")
    if k is not None:
        kids = list(k) if isinstance(k, pikepdf.Array) else [k]
        for kid in kids:
            if hasattr(kid, "get") and kid.get("/S") == pikepdf.Name("/Document"):
                return kid
    doc = dst.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/StructElem"),
        S=pikepdf.Name("/Document"),
        P=dst_root,
    ))
    doc["/K"] = pikepdf.Array()
    if k is None:
        dst_root["/K"] = pikepdf.Array([doc])
    elif isinstance(k, pikepdf.Array):
        k.append(doc)
    else:
        dst_root["/K"] = pikepdf.Array([k, doc])
    return doc


def _merge_rolemap(src_root, dst_root):
    src_rm = src_root.get("/RoleMap")
    if not src_rm:
        return
    dst_rm = dst_root.get("/RoleMap")
    if dst_rm is None:
        dst_root["/RoleMap"] = pikepdf.Dictionary()
        dst_rm = dst_root["/RoleMap"]
    for key in src_rm.keys():
        if key not in dst_rm:
            dst_rm[key] = src_rm[key]


def _update_pg_recursive(elem, dst_page):
    if not hasattr(elem, "get"):
        return
    t = elem.get("/Type")
    s = elem.get("/S")
    if s is not None or t == pikepdf.Name("/StructElem"):
        elem["/Pg"] = dst_page
    k = elem.get("/K")
    if k is None:
        return
    if isinstance(k, pikepdf.Array):
        for child in k:
            _update_pg_recursive(child, dst_page)
    elif isinstance(k, pikepdf.Dictionary):
        if "/Pg" in k:
            k["/Pg"] = dst_page


def _collect_leaf_elems(elems):
    result = []
    def _walk(elem):
        if not hasattr(elem, "get"):
            return
        k = elem.get("/K")
        if k is None:
            result.append(elem)
            return
        if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == pikepdf.Name("/MCR"):
            result.append(elem)
            return
        if isinstance(k, pikepdf.Array):
            for child in k:
                _walk(child)
        else:
            _walk(k)
    for e in elems:
        _walk(e)
    return result


def merge_tagged_page(src, dst, dst_page):
    if not _is_tagged(src):
        return
    dst_root = _ensure_dst_tagged(dst)
    src_root = src.Root["/StructTreeRoot"]
    _merge_rolemap(src_root, dst_root)
    dst_doc = _get_or_create_document(dst, dst_root)

    slot = int(dst_root.get("/ParentTreeNextKey", 0))
    dst_root["/ParentTreeNextKey"] = pikepdf.Integer(slot + 1)
    dst_page["/StructParents"] = pikepdf.Integer(slot)

    src_k = src_root.get("/K")
    src_docs = list(src_k) if isinstance(src_k, pikepdf.Array) else ([src_k] if src_k else [])
    src_doc_elem = next(
        (d for d in src_docs if hasattr(d, "get") and d.get("/S") == pikepdf.Name("/Document")),
        None
    )
    if src_doc_elem is None:
        return

    copied_slides = []
    for src_slide in list(src_doc_elem.get("/K", [])):
        copied = dst.copy_foreign(src_slide)
        copied["/P"] = dst_doc
        _update_pg_recursive(copied, dst_page)
        dst_doc["/K"].append(copied)
        copied_slides.append(copied)

    para_elems = _collect_leaf_elems(copied_slides)
    pt = dst_root["/ParentTree"]
    pt["/Nums"].append(pikepdf.Integer(slot))
    pt["/Nums"].append(pikepdf.Array(para_elems))


# ---- run ----
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    make_tagged_single_page(td / "a.pdf", "/Slide", b"Slide A")
    make_tagged_single_page(td / "b.pdf", "/Slide", b"Slide B")

    dst = pikepdf.Pdf.new()
    for src_path in [td / "a.pdf", td / "b.pdf"]:
        with pikepdf.open(src_path) as src:
            dst.add_pages_from(src, forms="preserve")
            dst_page = dst.pages[-1].obj
            merge_tagged_page(src, dst, dst_page)

    out = td / "merged_tagged.pdf"
    dst.save(out)
    print("Saved:", out)

    with pikepdf.open(out) as v:
        sr = v.Root.get("/StructTreeRoot")
        print("StructTreeRoot:", sr is not None)
        print("MarkInfo:", v.Root.get("/MarkInfo"))
        if sr:
            k = sr["/K"]
            docs = list(k) if isinstance(k, pikepdf.Array) else [k]
            print(f"Root kids: {len(docs)}")
            for d in docs:
                kids = list(d.get("/K", []))
                print(f"  /Document -> {len(kids)} slide kids")
                for slide in kids:
                    ss = slide.get("/S")
                    pg = slide.get("/Pg")
                    pk = slide.get("/K")
                    nk = len(list(pk)) if isinstance(pk, pikepdf.Array) else 1
                    print(f"    {ss}: /Pg={'OK' if pg else 'MISSING'}, {nk} child(ren)")
            pt = sr.get("/ParentTree")
            nums = list(pt.get("/Nums", []))
            print(f"ParentTree entries: {len(nums)//2} slot(s)")
            for i in range(0, len(nums), 2):
                slot = nums[i]
                refs = list(nums[i+1]) if isinstance(nums[i+1], pikepdf.Array) else [nums[i+1]]
                print(f"  slot {slot}: {len(refs)} parent ref(s), all have /S: "
                      f"{all(hasattr(r,'get') and r.get('/S') for r in refs)}")
