from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pikepdf

from pdfmergepy.merge import merge_files
from pdfmergepy.mergeinfo import parse_merge_info
from pdfmergepy.composite import merge_from_xml
from pdfmergepy.pdfutil import PageSpec, page_geometry, parse_input_arg, parse_page_range


def _build_specs(inputs: list[str]) -> list[PageSpec]:
    specs = []
    for arg in inputs:
        path, range_spec = parse_input_arg(arg)
        if not path.exists():
            raise FileNotFoundError(f"input file not found: {path}")
        with pikepdf.open(path) as pdf:
            total_pages = len(pdf.pages)
        pages = parse_page_range(range_spec, total_pages)
        specs.append(PageSpec(path=path, pages=pages))
    return specs


def _cmd_merge(args: argparse.Namespace) -> int:
    specs = _build_specs(args.inputs)
    merge_files(specs, Path(args.output))
    total = sum(len(s.pages) for s in specs)
    print(f"Merged {len(specs)} file(s), {total} page(s) -> {args.output}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    result = []
    for arg in args.inputs:
        path, range_spec = parse_input_arg(arg)
        with pikepdf.open(path) as pdf:
            total_pages = len(pdf.pages)
            pages = parse_page_range(range_spec, total_pages)
            result.append({
                "file": str(path),
                "total_pages": total_pages,
                "pages": [
                    {"page": p, **page_geometry(pdf.pages[p - 1])} for p in pages
                ],
            })
    print(json.dumps(result, indent=2))
    return 0


def _cmd_merge_xml(args: argparse.Namespace) -> int:
    xml_path = Path(args.xml)
    main_path = Path(args.main)
    inputs_dir = Path(args.inputs_dir)
    output_path = Path(args.output)

    if not xml_path.exists():
        print(f"error: merge-info XML not found: {xml_path}", file=sys.stderr)
        return 1
    if not main_path.exists():
        print(f"error: main PDF not found: {main_path}", file=sys.stderr)
        return 1
    if not inputs_dir.is_dir():
        print(f"error: inputs directory not found: {inputs_dir}", file=sys.stderr)
        return 1

    merge_info = parse_merge_info(xml_path)
    merge_from_xml(merge_info, main_path, inputs_dir, output_path)
    print(
        f"merge-xml: {len(merge_info.merge_items)} MergeItem(s) "
        f"-> {output_path}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfmergepy",
        description="Page-level PDF merge CLI (pikepdf-based POC), for diffing against CTS2.0's itext7 PdfMerger",
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s 0.1.0 (pikepdf/QPDF)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    merge_p = sub.add_parser("merge", help="concatenate page ranges from one or more PDFs into one output PDF")
    merge_p.add_argument(
        "inputs",
        nargs="+",
        help="input PDFs, each optionally suffixed with a page range, e.g. file.pdf:1-5 or file.pdf:2,4,6-8",
    )
    merge_p.add_argument("-o", "--output", required=True, help="output PDF path")
    merge_p.set_defaults(func=_cmd_merge)

    info_p = sub.add_parser("info", help="print per-page MediaBox/CropBox/Rotate as JSON (for diffing vs itext7 output)")
    info_p.add_argument("inputs", nargs="+", help="input PDFs, each optionally suffixed with a page range")
    info_p.set_defaults(func=_cmd_info)

    xml_p = sub.add_parser(
        "merge-xml",
        help="merge PDFs according to a WorkspaceMergeInfo XML file (CTS2.0 format)",
    )
    xml_p.add_argument("xml", help="path to WorkspaceMergeInfo XML file")
    xml_p.add_argument("--main", required=True, help="path to main/base PDF")
    xml_p.add_argument(
        "--inputs-dir", required=True,
        help="directory containing external PDFs referenced by BlobId/MergedPdfFileId",
    )
    xml_p.add_argument("-o", "--output", required=True, help="output PDF path")
    xml_p.set_defaults(func=_cmd_merge_xml)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
