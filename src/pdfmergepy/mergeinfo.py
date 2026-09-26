"""Dataclasses and XML parser for WorkspaceMergeInfo XML (CTS2.0 format).

Parses the XML produced by CTS2.0's WorkspaceMergeInfo serialiser and
provides helpers to resolve external PDF paths from the inputs directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SlideLocalId:
    """One slide→external-page mapping inside a MergeItem."""
    pdf_page: int     # 1-based page in the external PDF
    slide_index: int  # 1-based slide index in the main PDF
    local_id: int     # element text value (shape/slide local ID)


@dataclass
class MergeItem:
    id: str
    blob_id: str
    fit_pdf_size: bool          # True → Mode B (ext native size); False → Mode A (fit main size)
    slide_fit_pattern: str      # "AlignTopLeft" or "AlignBottomRight"
    page_count: int
    merged_pdf_file_id: str     # may be empty
    start_index_in_merged_file: int  # -1 means not set
    slides: list[SlideLocalId] = field(default_factory=list)


@dataclass
class SlideInfo:
    slide_index: int
    local_id: int
    hidden: bool


@dataclass
class WorkspaceMergeInfo:
    merge_items: list[MergeItem] = field(default_factory=list)
    slide_infos: list[SlideInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _parse_bool(s: str) -> bool:
    return s.strip().lower() in ("true", "1", "yes")


def _load_xml_root(path: Path) -> ET.Element:
    """Load an XML file into an ElementTree root, handling UTF-16 BOM encoding.

    Strategy:
    1. Detect UTF-16 BOM (``\\xff\\xfe`` LE or ``\\xfe\\xff`` BE).
    2. Decode to a Python str, which strips the BOM and gives a clean string.
    3. Strip any stray BOM character (\\ufeff) that may survive the decode.
    4. Replace the ``encoding="utf-16"`` declaration so expat doesn't reject
       it when we re-encode as UTF-8 for parsing.
    """
    import re as _re
    raw = path.read_bytes()

    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # UTF-16 file: decode and transcode to UTF-8 for ET
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

    # Strip any lingering BOM character that survived decoding
    text = text.lstrip("﻿")

    # Fix encoding declaration so expat doesn't reject it when we send UTF-8
    text = _re.sub(
        r'encoding=["\']utf-16["\']',
        'encoding="utf-8"',
        text,
        count=1,
        flags=_re.IGNORECASE,
    )

    return ET.fromstring(text.encode("utf-8"))


def parse_merge_info(path: Path) -> WorkspaceMergeInfo:
    """Parse a WorkspaceMergeInfo XML file.

    Handles UTF-16 (BOM) encoding used by CTS2.0 and falls back to UTF-8.

    Handles both schema variants:
      - <slideLocalId> directly under <MergeItem>
      - <slideLocalId> nested inside <ApplySlide>
    """
    root = _load_xml_root(path)

    # ---- MergeItems --------------------------------------------------------
    merge_items: list[MergeItem] = []
    for item_el in root.findall(".//PDFMerge/MergeItem"):
        blob_id = item_el.get("BlobId", "")
        fit_pdf_size = _parse_bool(item_el.get("FitPDFSize", "false"))
        slide_fit_pattern = item_el.get("SlideFitPattern", "AlignTopLeft")
        page_count = int(item_el.get("pageCount", "1"))
        item_id = item_el.get("id", "")

        # MergedPdfFileInfo
        merged_info_el = item_el.find("MergedPdfFileInfo")
        merged_pdf_file_id = ""
        start_index = -1
        if merged_info_el is not None:
            merged_pdf_file_id = merged_info_el.get("MergedPdfFileId", "")
            try:
                start_index = int(merged_info_el.get("StartIndexInMergedFile", "-1"))
            except (ValueError, TypeError):
                start_index = -1

        # slideLocalId elements — may be direct children or inside <ApplySlide>
        slides: list[SlideLocalId] = []
        apply_slide_el = item_el.find("ApplySlide")
        if apply_slide_el is not None:
            slide_els = apply_slide_el.findall("slideLocalId")
        else:
            slide_els = item_el.findall("slideLocalId")

        for sle in slide_els:
            pdf_page = int(sle.get("pdfPage", "1"))
            slide_index = int(sle.get("slideIndex", "1"))
            try:
                local_id = int((sle.text or "0").strip())
            except ValueError:
                local_id = 0
            slides.append(SlideLocalId(
                pdf_page=pdf_page,
                slide_index=slide_index,
                local_id=local_id,
            ))

        merge_items.append(MergeItem(
            id=item_id,
            blob_id=blob_id,
            fit_pdf_size=fit_pdf_size,
            slide_fit_pattern=slide_fit_pattern,
            page_count=page_count,
            merged_pdf_file_id=merged_pdf_file_id,
            start_index_in_merged_file=start_index,
            slides=slides,
        ))

    # ---- SlideInfo ---------------------------------------------------------
    slide_infos: list[SlideInfo] = []
    for info_el in root.findall(".//SlideInfo/info"):
        try:
            slide_index = int(info_el.get("slideIndex", "0"))
            local_id = int(info_el.get("localId", "0"))
        except ValueError:
            continue
        hidden = _parse_bool(info_el.get("hidden", "false"))
        slide_infos.append(SlideInfo(
            slide_index=slide_index,
            local_id=local_id,
            hidden=hidden,
        ))

    return WorkspaceMergeInfo(merge_items=merge_items, slide_infos=slide_infos)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_external_page(
    item: MergeItem,
    slide: SlideLocalId,
    inputs_dir: Path,
) -> tuple[Path, int]:
    """Return (pdf_path, 1-based page number) for this slide's external page.

    Logic (mirroring CTS2.0 PdfMerger.cs):
    - If MergedPdfFileId is non-empty AND StartIndexInMergedFile >= 0:
        use inputs_dir/<MergedPdfFileId>.pdf
        actual_page = StartIndexInMergedFile + slide.pdf_page
    - Else:
        use inputs_dir/<BlobId>.pdf
        actual_page = slide.pdf_page  (already 1-based)
    """
    use_merged = (
        bool(item.merged_pdf_file_id)
        and item.start_index_in_merged_file >= 0
    )

    if use_merged:
        file_id = item.merged_pdf_file_id
        actual_page = item.start_index_in_merged_file + slide.pdf_page
    else:
        file_id = item.blob_id
        actual_page = slide.pdf_page

    # Case-insensitive file lookup in inputs_dir
    target_lower = f"{file_id}.pdf".lower()
    for p in inputs_dir.iterdir():
        if p.is_file() and p.name.lower() == target_lower:
            return p, actual_page

    raise FileNotFoundError(
        f"External PDF not found in {inputs_dir}: {file_id}.pdf"
    )
