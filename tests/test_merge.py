from pathlib import Path

import pikepdf
import pytest

from pdfmergepy.merge import merge_files
from pdfmergepy.pdfutil import PageSpec, parse_input_arg, parse_page_range


def _make_pdf(path: Path, num_pages: int, rotate: int = 0) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(num_pages):
        page = pikepdf.Page(pdf.add_blank_page(page_size=(200, 300)))
        if rotate:
            page.obj["/Rotate"] = rotate
    pdf.save(path)


def test_parse_page_range_all():
    assert parse_page_range("", 5) == (1, 2, 3, 4, 5)


def test_parse_page_range_mixed():
    assert parse_page_range("2,4-6", 10) == (2, 4, 5, 6)


def test_parse_page_range_out_of_bounds():
    with pytest.raises(ValueError):
        parse_page_range("1-99", 5)


def test_parse_input_arg_with_range():
    path, spec = parse_input_arg("file.pdf:3-7")
    assert str(path) == "file.pdf"
    assert spec == "3-7"


def test_parse_input_arg_windows_path_no_range():
    path, spec = parse_input_arg("C:/tmp/file.pdf")
    assert path == Path("C:/tmp/file.pdf")
    assert spec == ""


def test_merge_concatenates_pages(tmp_path: Path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    out = tmp_path / "out.pdf"
    _make_pdf(a, 3)
    _make_pdf(b, 2, rotate=90)

    specs = [
        PageSpec(path=a, pages=(1, 2)),
        PageSpec(path=b, pages=(1,)),
    ]
    merge_files(specs, out)

    with pikepdf.open(out) as merged:
        assert len(merged.pages) == 3
        assert int(merged.pages[2].obj.get("/Rotate", 0)) == 90
        assert str(merged.docinfo["/Producer"]) == "pdfmergepy (pikepdf/QPDF)"
        assert merged.pdf_version == "1.7"
