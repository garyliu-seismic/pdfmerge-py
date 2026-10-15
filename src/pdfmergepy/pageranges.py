"""Page-range expressions for select / delete (port of CTS2.0 ``PdfUtility``).

Ports ``PdfUtility.EnumAllPageSections`` and ``PageSection.IsInclude``
(Seismic.CTS.Implements.Pdf/PdfUtility.cs) *exactly*, including their quirks:

* items are comma separated; empty / whitespace-only items are ignored
  (``SplitNoEmpty``), each item is trimmed;
* ``"5"``   -> single page (Start=5, End=0; End==0 means "only Start");
* ``"3-7"`` -> inclusive range; a *reversed* range such as ``"7-3"`` is **not**
  an error, it simply matches nothing;
* ``"4-"``  -> open ended, End = int.MaxValue;
* ``"-6"``  -> open start, Start = int.MinValue, so pages 1..6;
* ``"-"`` alone, non numeric values, numbers <= 0 and numbers that do not fit
  in a signed 32 bit int are errors;
* pages beyond the document and duplicate / overlapping items are silently
  harmless (selection is a *predicate over page numbers*, so output keeps the
  original page order and never repeats a page).

Note that ``pdfutil.parse_page_range`` (used by ``merge``'s ``file.pdf:1-3``
syntax) is a different, stricter grammar and is intentionally left untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INT_MIN = -(2**31)
INT_MAX = 2**31 - 1

# .NET int.TryParse (NumberStyles.Integer): optional surrounding whitespace and
# one optional leading sign, ASCII digits only.
_INT_RE = re.compile(r"\s*[+-]?[0-9]+\s*", re.ASCII)


class PageRangeError(ValueError):
    """Invalid page-range expression (CTS ``ArgumentError``)."""


@dataclass(frozen=True)
class PageSection:
    start: int
    end: int = 0

    def is_include(self, page_num: int) -> bool:
        if self.end == 0:
            return page_num == self.start
        return self.start <= page_num <= self.end


def _split_no_empty(s: str | None) -> list[str]:
    if s is None:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _int_parse(s: str, expression: str) -> int:
    if not _INT_RE.fullmatch(s):
        raise PageRangeError(f"Format error: '{s}' in expression '{expression}'")
    num = int(s)
    if not (INT_MIN <= num <= INT_MAX):
        raise PageRangeError(f"Format error: '{s}' in expression '{expression}'")
    if num <= 0:
        raise PageRangeError(f"Page number must large than zero, expression '{expression}'")
    return num


def enum_all_page_sections(expression: str | None) -> list[PageSection]:
    """Parse e.g. ``"1,3,6-8,10-"`` into :class:`PageSection` objects."""
    expression = expression or ""
    sections: list[PageSection] = []
    for exp in _split_no_empty(expression):
        if exp == "-":
            raise PageRangeError(f"Format error: '{exp}' in expression '{expression}'")
        index = exp.find("-")
        if index < 0:
            sections.append(PageSection(start=_int_parse(exp, expression)))
        elif index == 0:
            sections.append(PageSection(start=INT_MIN, end=_int_parse(exp[1:], expression)))
        elif index == len(exp) - 1:
            sections.append(PageSection(start=_int_parse(exp[:index], expression), end=INT_MAX))
        else:
            sections.append(PageSection(
                start=_int_parse(exp[:index], expression),
                end=_int_parse(exp[index + 1:], expression),
            ))
    return sections


def selected_pages(expression: str | None, total_pages: int) -> list[int]:
    """1-based page numbers matched by *expression* (ascending, no duplicates)."""
    sections = enum_all_page_sections(expression)
    return [n for n in range(1, total_pages + 1) if any(s.is_include(n) for s in sections)]


def remaining_pages(expression: str | None, total_pages: int) -> list[int]:
    """1-based page numbers NOT matched by *expression* (the ``delete`` result)."""
    sections = enum_all_page_sections(expression)
    return [n for n in range(1, total_pages + 1) if not any(s.is_include(n) for s in sections)]


def parse_split_counts(expression: str | None) -> list[int]:
    """Parse the ``split`` argument ``"3,6"`` into positive chunk sizes.

    Port of ``PdfSplitConverter.__ParsePage``: every non-empty item must be an
    int > 0, otherwise an ``ArgumentError`` is raised.
    """
    counts: list[int] = []
    for s in _split_no_empty(expression):
        if _INT_RE.fullmatch(s) and INT_MIN <= int(s) <= INT_MAX and int(s) > 0:
            counts.append(int(s))
        else:
            raise PageRangeError(
                "Argument 'pages' format error, should be numbers (large than 0) splitted by commas"
            )
    return counts
