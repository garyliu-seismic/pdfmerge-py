"""Unit tests for pageranges.py (port of PdfUtility.EnumAllPageSections)."""
import pytest

from pdfmergepy.pageranges import (
    INT_MAX, INT_MIN, PageRangeError, PageSection, enum_all_page_sections,
    parse_split_counts, remaining_pages, selected_pages,
)


def test_section_shapes_match_csharp():
    assert enum_all_page_sections("5") == [PageSection(5, 0)]
    assert enum_all_page_sections("3-7") == [PageSection(3, 7)]
    assert enum_all_page_sections("4-") == [PageSection(4, INT_MAX)]
    assert enum_all_page_sections("-6") == [PageSection(INT_MIN, 6)]


def test_documented_example_1_3_6_8_10_open():
    assert selected_pages("1,3,6-8,10-", 12) == [1, 3, 6, 7, 8, 10, 11, 12]
    assert remaining_pages("1,3,6-8,10-", 12) == [2, 4, 5, 9]


def test_whitespace_and_empty_items_ignored():
    assert selected_pages(" 1 , , 3 - 4 ,", 6) == [1, 3, 4]
    assert enum_all_page_sections("") == []
    assert enum_all_page_sections(None) == []
    assert selected_pages("", 5) == []
    assert remaining_pages("", 5) == [1, 2, 3, 4, 5]


def test_open_start_means_first_n_pages():
    assert selected_pages("-3", 6) == [1, 2, 3]


def test_out_of_range_and_duplicates_are_harmless():
    assert selected_pages("2,2,1-3,99", 4) == [1, 2, 3]          # no dup, original order
    assert selected_pages("100-", 4) == []
    assert selected_pages("3-99", 4) == [3, 4]
    assert remaining_pages("1-99", 4) == []


def test_reversed_range_matches_nothing_not_an_error():
    assert selected_pages("7-3", 10) == []
    assert remaining_pages("7-3", 5) == [1, 2, 3, 4, 5]


def test_output_order_is_document_order_not_expression_order():
    assert selected_pages("5,1,3", 6) == [1, 3, 5]


@pytest.mark.parametrize("bad", ["-", "0", "abc", "1-b", "0-3", "3-0", "--3", "3-5-7",
                                 "1,-,3", "2147483648", "1.5", "1_0"])
def test_invalid_expressions_raise(bad):
    with pytest.raises(PageRangeError):
        enum_all_page_sections(bad)


def test_dotnet_style_sign_and_int32_limit():
    assert enum_all_page_sections("+3") == [PageSection(3, 0)]
    assert enum_all_page_sections("2147483647") == [PageSection(2147483647, 0)]
    with pytest.raises(PageRangeError):
        enum_all_page_sections("-5-")        # '-5' -> index 0 -> parse '5-' fails


def test_page_range_error_is_value_error():
    assert issubclass(PageRangeError, ValueError)


def test_split_counts():
    assert parse_split_counts("3,6") == [3, 6]
    assert parse_split_counts(" 3 ,, 6 ") == [3, 6]
    assert parse_split_counts("") == []
    for bad in ("0", "-1", "a", "3,x", "1-2"):
        with pytest.raises(PageRangeError):
            parse_split_counts(bad)
