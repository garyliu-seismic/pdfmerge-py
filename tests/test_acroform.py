"""Tests for AcroForm field preservation across all merge paths.

Covers the three scenarios where fields must survive:
  1. merge_files (plain concat) — the ``merge`` CLI path
  2. merge_from_xml plain-copy path (slides without a MergeItem)
  3. merge_from_xml composite path Mode B (FitPDFSize=true)  — ext PDF fields
  4. merge_from_xml composite path Mode A (FitPDFSize=false) — ext PDF fields
  5. Name-conflict renaming (two source PDFs share a field name)
  6. Nested field trees (parent / kids)
  7. /AcroForm/DR (Default Resources) merge

All PDFs are constructed programmatically; no external fixtures are required.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdfmergepy.acroform import add_page_with_forms, add_pages_range_with_forms
from pdfmergepy.merge import merge_files
from pdfmergepy.mergeinfo import parse_merge_info
from pdfmergepy.composite import merge_from_xml
from pdfmergepy.pdfutil import PageSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_plain_pdf(path: Path, num_pages: int = 1,
                    width: float = 612, height: float = 792) -> None:
    """Plain PDF with no AcroForm."""
    pdf = pikepdf.Pdf.new()
    for _ in range(num_pages):
        pdf.add_blank_page(page_size=(width, height))
    pdf.save(path)


def _make_form_pdf(
    path: Path,
    fields: list[tuple[str, str, list[float]]],
    width: float = 612,
    height: float = 792,
    with_dr: bool = False,
) -> None:
    """PDF with one page and AcroForm text fields.

    ``fields`` is a list of (name, value, [llx, lly, urx, ury]).
    When ``with_dr=True`` a minimal /AcroForm/DR font entry is added.
    """
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(width, height))
    acro_fields: list[pikepdf.Object] = []
    annots: list[pikepdf.Object] = []

    for name, value, rect in fields:
        f = pdf.make_indirect(pikepdf.Dictionary(
            T=pikepdf.String(name),
            FT=pikepdf.Name("/Tx"),
            V=pikepdf.String(value),
            Subtype=pikepdf.Name("/Widget"),
            Type=pikepdf.Name("/Annot"),
            Rect=pikepdf.Array(rect),
            P=page.obj,
        ))
        acro_fields.append(f)
        annots.append(f)

    page.obj["/Annots"] = pikepdf.Array(annots)

    acro = pikepdf.Dictionary(Fields=pikepdf.Array(acro_fields))
    if with_dr:
        font_obj = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"),
        ))
        acro["/DR"] = pikepdf.Dictionary(
            Font=pikepdf.Dictionary({"/Helv": font_obj}),
        )
    pdf.Root["/AcroForm"] = acro
    pdf.save(path)


def _make_nested_form_pdf(path: Path) -> None:
    """PDF with a parent field 'address' containing children 'street' and 'city'."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))

    parent = pdf.make_indirect(pikepdf.Dictionary(
        T=pikepdf.String("address"),
        FT=pikepdf.Name("/Tx"),
    ))
    child1 = pdf.make_indirect(pikepdf.Dictionary(
        T=pikepdf.String("street"),
        FT=pikepdf.Name("/Tx"),
        V=pikepdf.String("123 Main St"),
        Parent=parent,
        Subtype=pikepdf.Name("/Widget"),
        Type=pikepdf.Name("/Annot"),
        Rect=pikepdf.Array([100, 650, 400, 670]),
        P=page.obj,
    ))
    child2 = pdf.make_indirect(pikepdf.Dictionary(
        T=pikepdf.String("city"),
        FT=pikepdf.Name("/Tx"),
        V=pikepdf.String("San Francisco"),
        Parent=parent,
        Subtype=pikepdf.Name("/Widget"),
        Type=pikepdf.Name("/Annot"),
        Rect=pikepdf.Array([100, 620, 300, 640]),
        P=page.obj,
    ))
    parent["/Kids"] = pikepdf.Array([child1, child2])
    page.obj["/Annots"] = pikepdf.Array([child1, child2])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary(
        Fields=pikepdf.Array([parent]),
    )
    pdf.save(path)


def _write_xml(path: Path, content: str) -> None:
    path.write_bytes(content.encode("utf-16"))


def _acroform_field_names(pdf_path: Path) -> list[str]:
    """Return the /T values of all top-level /AcroForm fields."""
    with pikepdf.open(pdf_path) as pdf:
        acro = pdf.Root.get("/AcroForm")
        if acro is None:
            return []
        return [str(f.get("/T", "")) for f in acro.get("/Fields", [])]


def _page_widget_names(pdf_path: Path, page_index: int = 0) -> list[str]:
    """Return /T of all Widget annotations on a page."""
    with pikepdf.open(pdf_path) as pdf:
        page_obj = pdf.pages[page_index].obj
        names = []
        for a in page_obj.get("/Annots", []):
            if a.get("/Subtype") == pikepdf.Name("/Widget"):
                t = a.get("/T")
                names.append(str(t) if t else "")
        return names


# ---------------------------------------------------------------------------
# 1. merge_files — plain concatenation preserves AcroForm fields
# ---------------------------------------------------------------------------

def test_merge_files_preserves_fields(tmp_path: Path) -> None:
    """merge_files must carry AcroForm fields from each source into the output."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"

    _make_form_pdf(a, [("firstname", "Alice", [100, 700, 300, 720])])
    _make_form_pdf(b, [("lastname",  "Smith", [100, 700, 300, 720])])

    specs = [
        PageSpec(path=a, pages=(1,)),
        PageSpec(path=b, pages=(1,)),
    ]
    merge_files(specs, out)

    field_names = _acroform_field_names(out)
    assert "firstname" in field_names, f"Expected 'firstname' in {field_names}"
    assert "lastname"  in field_names, f"Expected 'lastname'  in {field_names}"


def test_merge_files_no_acroform_source(tmp_path: Path) -> None:
    """Plain PDFs without AcroForm must still merge cleanly (no /AcroForm key
    required in output)."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"

    _make_plain_pdf(a, num_pages=2)
    _make_plain_pdf(b, num_pages=1)

    specs = [PageSpec(path=a, pages=(1, 2)), PageSpec(path=b, pages=(1,))]
    merge_files(specs, out)

    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) == 3
    # No crash — that's the assertion


# ---------------------------------------------------------------------------
# 2. Name-conflict renaming
# ---------------------------------------------------------------------------

def test_merge_files_field_name_conflict_renamed(tmp_path: Path) -> None:
    """When two source PDFs share a field name, the second is renamed (name+1)."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"

    _make_form_pdf(a, [("name", "Alice", [100, 700, 300, 720])])
    _make_form_pdf(b, [("name", "Bob",   [100, 700, 300, 720])])

    merge_files([
        PageSpec(path=a, pages=(1,)),
        PageSpec(path=b, pages=(1,)),
    ], out)

    field_names = _acroform_field_names(out)
    assert len(field_names) == 2, f"Expected 2 fields, got {field_names}"
    # One keeps 'name', the other gets a rename suffix
    assert "name" in field_names
    renamed = [n for n in field_names if n != "name"]
    assert len(renamed) == 1
    assert renamed[0].startswith("name")  # e.g. 'name+1'


def test_merge_files_widget_annots_match_fields(tmp_path: Path) -> None:
    """After conflict rename, the widget /Annot on the page must use the
    renamed /T so the two are consistent."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"

    _make_form_pdf(a, [("email", "alice@x.com", [100, 700, 350, 720])])
    _make_form_pdf(b, [("email", "bob@x.com",   [100, 700, 350, 720])])

    merge_files([
        PageSpec(path=a, pages=(1,)),
        PageSpec(path=b, pages=(1,)),
    ], out)

    field_names = set(_acroform_field_names(out))
    widget_pg0 = set(_page_widget_names(out, 0))
    widget_pg1 = set(_page_widget_names(out, 1))

    # Every widget name must appear in the AcroForm field list
    all_widgets = widget_pg0 | widget_pg1
    assert all_widgets <= field_names, (
        f"Widget names {all_widgets} not all in AcroForm fields {field_names}"
    )
    # Both original pages must each have exactly one widget
    assert len(widget_pg0) == 1
    assert len(widget_pg1) == 1
    # They must be different names (one was renamed)
    assert widget_pg0 != widget_pg1


# ---------------------------------------------------------------------------
# 3. Nested field tree (parent / kids)
# ---------------------------------------------------------------------------

def test_merge_files_nested_field_tree(tmp_path: Path) -> None:
    """A parent→kids field tree must survive the copy: 'address' with children
    'street' and 'city'."""
    src = tmp_path / "nested.pdf"
    out = tmp_path / "out.pdf"
    _make_nested_form_pdf(src)

    merge_files([PageSpec(path=src, pages=(1,))], out)

    with pikepdf.open(out) as pdf:
        acro = pdf.Root.get("/AcroForm")
        assert acro is not None
        top_fields = list(acro["/Fields"])
        assert len(top_fields) == 1
        parent = top_fields[0]
        assert str(parent.get("/T")) == "address"
        kids = list(parent.get("/Kids", []))
        kid_names = {str(k.get("/T")) for k in kids}
        assert kid_names == {"street", "city"}, f"Unexpected kids: {kid_names}"


# ---------------------------------------------------------------------------
# 4. /AcroForm/DR (Default Resources) carried through
# ---------------------------------------------------------------------------

def test_merge_files_dr_preserved(tmp_path: Path) -> None:
    """If source has /AcroForm/DR with a font entry, it must appear in dst."""
    src = tmp_path / "dr.pdf"
    out = tmp_path / "out.pdf"
    _make_form_pdf(src, [("f", "v", [100, 700, 300, 720])], with_dr=True)

    merge_files([PageSpec(path=src, pages=(1,))], out)

    with pikepdf.open(out) as pdf:
        acro = pdf.Root.get("/AcroForm")
        assert acro is not None, "No /AcroForm in output"
        dr = acro.get("/DR")
        assert dr is not None, "No /DR in /AcroForm"
        font_dict = dr.get("/Font")
        assert font_dict is not None, "No /Font in /DR"
        assert "/Helv" in font_dict, f"/Helv not in /DR/Font: {list(font_dict.keys())}"


# ---------------------------------------------------------------------------
# 5. merge_from_xml — plain-copy path (slides without MergeItem)
# ---------------------------------------------------------------------------

def test_merge_xml_plain_copy_preserves_fields(tmp_path: Path) -> None:
    """Slides that have no MergeItem are plain-copied; their AcroForm fields
    must appear in the output (matches the 'pages' list path in CTS2.0)."""
    main_path = tmp_path / "main.pdf"
    out_path  = tmp_path / "out.pdf"

    # main PDF has 2 pages, each with a form field
    pdf = pikepdf.Pdf.new()
    for i, name in enumerate(["slide1_field", "slide2_field"]):
        page = pdf.add_blank_page(page_size=(960, 540))
        f = pdf.make_indirect(pikepdf.Dictionary(
            T=pikepdf.String(name), FT=pikepdf.Name("/Tx"),
            V=pikepdf.String(f"val{i}"),
            Subtype=pikepdf.Name("/Widget"), Type=pikepdf.Name("/Annot"),
            Rect=pikepdf.Array([50, 500, 300, 520]), P=page.obj,
        ))
        page.obj["/Annots"] = pikepdf.Array([f])
        if i == 0:
            pdf.Root["/AcroForm"] = pikepdf.Dictionary(Fields=pikepdf.Array([f]))
        else:
            pdf.Root["/AcroForm"]["/Fields"].append(f)
    pdf.save(main_path)

    # No MergeItems → both slides are plain-copied
    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge />
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    field_names = _acroform_field_names(out_path)
    assert "slide1_field" in field_names, f"Missing slide1_field in {field_names}"
    assert "slide2_field" in field_names, f"Missing slide2_field in {field_names}"


# ---------------------------------------------------------------------------
# 6. merge_from_xml — composite Mode B (FitPDFSize=true) ext PDF fields
# ---------------------------------------------------------------------------

def test_merge_xml_mode_b_ext_fields_preserved(tmp_path: Path) -> None:
    """Mode B: the external PDF's AcroForm fields must appear in the output."""
    main_path = tmp_path / "main.pdf"
    ext_path  = tmp_path / "blob-ext.pdf"
    out_path  = tmp_path / "out.pdf"

    _make_plain_pdf(main_path, num_pages=1, width=960, height=540)
    _make_form_pdf(ext_path, [
        ("ext_field_a", "value_a", [50, 700, 300, 720]),
        ("ext_field_b", "value_b", [50, 660, 300, 680]),
    ], width=1280, height=960)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="item-b" BlobId="blob-ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="1">10</slideLocalId>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    field_names = _acroform_field_names(out_path)
    assert "ext_field_a" in field_names, f"Missing ext_field_a in {field_names}"
    assert "ext_field_b" in field_names, f"Missing ext_field_b in {field_names}"


# ---------------------------------------------------------------------------
# 7. merge_from_xml — composite Mode A (FitPDFSize=false) ext PDF fields
# ---------------------------------------------------------------------------

def test_merge_xml_mode_a_ext_fields_preserved(tmp_path: Path) -> None:
    """Mode A: the external PDF's AcroForm fields must appear in the output."""
    main_path = tmp_path / "main.pdf"
    ext_path  = tmp_path / "blob-ext.pdf"
    out_path  = tmp_path / "out.pdf"

    _make_plain_pdf(main_path, num_pages=1, width=960, height=540)
    _make_form_pdf(ext_path, [
        ("form_number", "INV-2024-001", [50, 700, 300, 720]),
    ], width=612, height=792)

    xml_content = """\
<?xml version="1.0" encoding="utf-16"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="false" SlideFitPattern="AlignTopLeft" pageCount="1"
               id="item-a" BlobId="blob-ext">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="1">20</slideLocalId>
    </MergeItem>
  </PDFMerge>
</WorkspaceMergeInfo>
"""
    xml_path = tmp_path / "merge.xml"
    _write_xml(xml_path, xml_content)

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, tmp_path, out_path)

    field_names = _acroform_field_names(out_path)
    assert "form_number" in field_names, f"Missing form_number in {field_names}"


# ---------------------------------------------------------------------------
# 8. add_page_with_forms — unit test for the acroform helper directly
# ---------------------------------------------------------------------------

def test_add_page_with_forms_basic(tmp_path: Path) -> None:
    """add_page_with_forms copies exactly one page and its fields into dst."""
    src_path = tmp_path / "src.pdf"
    _make_form_pdf(src_path, [
        ("alpha", "aaa", [10, 700, 200, 720]),
        ("beta",  "bbb", [10, 660, 200, 680]),
    ])

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        result = add_page_with_forms(dst, src, page_index=0, src_path=src_path)

    assert result.pages_added == 1
    assert result.fields_added == 2
    assert result.renamed_fields == {}

    out = tmp_path / "out.pdf"
    dst.save(out)
    assert set(_acroform_field_names(out)) == {"alpha", "beta"}


def test_add_pages_range_with_forms_subset(tmp_path: Path) -> None:
    """add_pages_range_with_forms copies only the requested page indices."""
    src_path = tmp_path / "src.pdf"

    pdf = pikepdf.Pdf.new()
    for i, name in enumerate(["f0", "f1", "f2"]):
        page = pdf.add_blank_page(page_size=(612, 792))
        f = pdf.make_indirect(pikepdf.Dictionary(
            T=pikepdf.String(name), FT=pikepdf.Name("/Tx"),
            V=pikepdf.String(f"v{i}"),
            Subtype=pikepdf.Name("/Widget"), Type=pikepdf.Name("/Annot"),
            Rect=pikepdf.Array([10, 700, 200, 720]), P=page.obj,
        ))
        page.obj["/Annots"] = pikepdf.Array([f])
        if i == 0:
            pdf.Root["/AcroForm"] = pikepdf.Dictionary(Fields=pikepdf.Array([f]))
        else:
            pdf.Root["/AcroForm"]["/Fields"].append(f)
    pdf.save(src_path)

    dst = pikepdf.Pdf.new()
    with pikepdf.open(src_path) as src:
        result = add_pages_range_with_forms(dst, src, page_indices=[0, 2])

    assert result.pages_added == 2
    assert result.fields_added == 2  # f0 and f2

    out = tmp_path / "out.pdf"
    dst.save(out)
    names = set(_acroform_field_names(out))
    assert "f0" in names
    assert "f2" in names
    assert "f1" not in names
