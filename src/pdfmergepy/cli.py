from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pikepdf

from pdfmergepy.merge import merge_files
from pdfmergepy.mergeinfo import parse_merge_info
from pdfmergepy.composite import merge_from_xml
from pdfmergepy.partial import apply_partial_pdf_from_xml, merge_xml_then_partial
from pdfmergepy import fonts as fontlib
from pdfmergepy.fontembed import embed_fonts
from pdfmergepy.fontmap import FontCatalog
from pdfmergepy import pageops
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

    n_merge, n_partial = merge_xml_then_partial(xml_path, main_path, inputs_dir, output_path)
    print(
        f"merge-xml: {n_merge} MergeItem(s), {n_partial} partialPDF entry/entries "
        f"-> {output_path}"
    )
    return 0


def _cmd_list_fonts(args: argparse.Namespace) -> int:
    with fontlib.open_pdf(args.input, args.password) as pdf:
        result = fontlib.list_fonts(pdf)
    if args.missing_only:
        result = [f for f in result if not f["embedded"]]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _build_catalog(args: argparse.Namespace) -> FontCatalog:
    cat = FontCatalog()
    for fm in args.font_map or []:
        cat.add_font_map(fm)
    for d in args.font_dir or []:
        cat.add_directory(Path(d))
    for f in args.font or []:
        cat.add_named(f)
    return cat


def _cmd_embed_fonts(args: argparse.Namespace) -> int:
    if not (args.font_map or args.font_dir or args.font):
        print("error: give at least one of --font-map, --font-dir, --font", file=sys.stderr)
        return 1
    catalog = _build_catalog(args)
    if not catalog.fonts:
        print("error: no usable font files found", file=sys.stderr)
        return 1
    with fontlib.open_pdf(args.input, args.password) as pdf:
        report = embed_fonts(pdf, catalog, strict=args.strict, ignore_license=args.ignore_license,
                             allow_unverified_cid=args.allow_unverified_cid, remap_cid=args.remap_cid_via_tounicode)
        pdf.save(args.output)
    for w in report.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.report:
        text = json.dumps(report.to_json(), indent=2, ensure_ascii=False)
        if args.report == "-":
            print(text)
        else:
            Path(args.report).write_text(text, encoding="utf-8")
    print(f"embed-fonts: {len(report.embedded)} of {report.candidates} non-embedded font(s) embedded, "
          f"{report.remaining} remain -> {args.output}")
    return 3 if (args.require_all and report.remaining) else 0


def _cmd_remove_fonts(args: argparse.Namespace) -> int:
    subset = {"yes": True, "no": False, None: None}[args.subset]
    if args.large_pdf_rule:
        pred = fontlib.large_font_predicate(Path(args.input).stat().st_size)
    else:
        pred = fontlib.make_remove_predicate(args.min_size, subset, args.name_regex)
    with fontlib.open_pdf(args.input, args.password) as pdf:
        removed = fontlib.remove_fonts(pdf, pred)
        pdf.save(args.output)
    freed = sum(r.size for r in removed)
    print(f"remove-fonts: {len(removed)} embedded font file(s) removed ({freed} bytes) -> {args.output}")
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
        help="merge PDFs according to a WorkspaceMergeInfo XML file (CTS2.0 format); runs <PDFMerge> then <partialPDF> if both are present",
    )
    xml_p.add_argument("xml", help="path to WorkspaceMergeInfo XML file")
    xml_p.add_argument("--main", required=True, help="path to main/base PDF")
    xml_p.add_argument(
        "--inputs-dir", required=True,
        help="directory containing external PDFs referenced by BlobId/MergedPdfFileId",
    )
    xml_p.add_argument("-o", "--output", required=True, help="output PDF path")
    xml_p.set_defaults(func=_cmd_merge_xml)

    partial_p = sub.add_parser(
        "partial-merge",
        help="overlay partial PDFs (sub-region XObjects) onto an existing PDF, driven by <partialPDF> XML",
    )
    partial_p.add_argument("xml", help="path to WorkspaceMergeInfo XML containing <partialPDF> block")
    partial_p.add_argument("--main", required=True, help="path to the base PDF to draw onto")
    partial_p.add_argument(
        "--inputs-dir", required=True,
        help="directory containing external PDFs referenced by blob-id",
    )
    partial_p.add_argument("-o", "--output", required=True, help="output PDF path")
    partial_p.set_defaults(func=_cmd_partial_merge)

    lf = sub.add_parser("list-fonts", help="list every font used in a PDF as JSON (name, type, embedded?, subset?, where used)")
    lf.add_argument("input", help="input PDF")
    lf.add_argument("--password", default="", help="password for an encrypted PDF")
    lf.add_argument("--missing-only", action="store_true", help="only list fonts that are not embedded")
    lf.set_defaults(func=_cmd_list_fonts)

    ef = sub.add_parser("embed-fonts", help="embed full font programs into non-embedded fonts (CTS2.0 EmbedFont equivalent)")
    ef.add_argument("input", help="input PDF")
    ef.add_argument("-o", "--output", required=True, help="output PDF path")
    ef.add_argument("--font-map", action="append", metavar="MAP",
                    help="font-map.json (CTS2.0 schema) or text form 'Name|path;Name|path'; repeatable")
    ef.add_argument("--font-dir", action="append", metavar="DIR",
                    help="directory of fonts (uses DIR/font-map.json when present, else scans *.ttf/otf/ttc/pfb); repeatable")
    ef.add_argument("--font", action="append", metavar="NAME=PATH",
                    help="use font file PATH for PDF fonts named NAME (PostScript or family name); repeatable")
    ef.add_argument("--password", default="", help="password for an encrypted PDF")
    ef.add_argument("--strict", action="store_true",
                    help="only exact PostScript-name or exact family+style matches (no Bold->Regular style fallback)")
    ef.add_argument("--ignore-license", action="store_true", help="embed even fonts whose OS/2 fsType forbids embedding")
    ef.add_argument("--allow-unverified-cid", action="store_true",
                    help="embed Type0 fonts even when CID==glyph-id cannot be verified from /W or /ToUnicode")
    ef.add_argument("--remap-cid-via-tounicode", action="store_true",
                    help="when a Type0 font's CIDs do not match the supplied TrueType font's glyph ids (e.g. a removed "
                         "subset), rebuild /CIDToGIDMap from /ToUnicode instead of skipping the font")
    ef.add_argument("--report", metavar="FILE", help="write a JSON report (embedded/skipped/warnings); '-' for stdout")
    ef.add_argument("--require-all", action="store_true", help="exit 3 if any non-embedded font remains")
    ef.set_defaults(func=_cmd_embed_fonts)

    rf = sub.add_parser("remove-fonts", help="remove embedded font programs (CTS2.0 RemoveFonts equivalent)")
    rf.add_argument("input", help="input PDF")
    rf.add_argument("-o", "--output", required=True, help="output PDF path")
    rf.add_argument("--min-size", type=int, default=0, metavar="BYTES",
                    help="only remove font files larger than BYTES (C# 'size' option; 0 = no limit)")
    rf.add_argument("--subset", choices=["yes", "no"], help="only remove subset (yes) / full (no) fonts")
    rf.add_argument("--name-regex", help="only remove fonts whose name matches this regex (stand-in for C# only-system-fonts)")
    rf.add_argument("--large-pdf-rule", action="store_true",
                    help="CTS2.0 rule: do nothing unless the input PDF is >= 10 MB, then remove fonts >= 5 MB")
    rf.add_argument("--password", default="", help="password for an encrypted PDF")
    rf.set_defaults(func=_cmd_remove_fonts)
    # -- page operations (ports of CTS2.0 pdf split/select/delete, GetPageCount) --
    split_p = sub.add_parser("split", help="split into 0000.pdf, 0001.pdf... ('3,6' = 3 pages, 6 pages, rest)")
    split_p.add_argument("input")
    split_p.add_argument("--pages", required=True, help="chunk sizes, e.g. 3,6")
    split_p.add_argument("-o", "--output-dir", required=True, help="output directory")
    split_p.add_argument("--password", default="")
    split_p.set_defaults(func=_cmd_split)

    sel_p = sub.add_parser("select", help="keep only the given pages, e.g. '1,3,6-8,10-'")
    sel_p.add_argument("input")
    sel_p.add_argument("--pages", required=True)
    sel_p.add_argument("-o", "--output", required=True)
    sel_p.add_argument("--password", default="")
    sel_p.set_defaults(func=_cmd_select)

    del_p = sub.add_parser("delete", help="delete the given pages, e.g. '1,3,6-8,10-'")
    del_p.add_argument("input")
    del_p.add_argument("--pages", required=True)
    del_p.add_argument("-o", "--output", required=True)
    del_p.add_argument("--password", default="")
    del_p.set_defaults(func=_cmd_delete)

    app_p = sub.add_parser("append", help="append whole PDFs (tags/forms preserved; same engine as merge)")
    app_p.add_argument("inputs", nargs="+")
    app_p.add_argument("-o", "--output", required=True)
    app_p.set_defaults(func=_cmd_append)

    pc_p = sub.add_parser("page-count", help="print the number of pages")
    pc_p.add_argument("input")
    pc_p.add_argument("--password", default="")
    pc_p.set_defaults(func=_cmd_page_count)

    return parser


def _cmd_split(args: argparse.Namespace) -> int:
    files = pageops.split_pdf(args.input, args.pages, args.output_dir, args.password)
    for f in files:
        print(f)
    print(f"split: {len(files)} file(s) -> {args.output_dir}")
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    n = pageops.select_pages(args.input, args.pages, args.output, args.password)
    print(f"select: {n} page(s) -> {args.output}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    n = pageops.delete_pages(args.input, args.pages, args.output, args.password)
    print(f"delete: {n} page(s) remain -> {args.output}")
    return 0


def _cmd_append(args: argparse.Namespace) -> int:
    n = pageops.append_files(args.inputs, args.output)
    print(f"append: {len(args.inputs)} file(s), {n} page(s) -> {args.output}")
    return 0


def _cmd_page_count(args: argparse.Namespace) -> int:
    print(pageops.page_count(args.input, args.password))
    return 0


def _cmd_partial_merge(args: argparse.Namespace) -> int:
    xml_path   = Path(args.xml)
    main_path  = Path(args.main)
    inputs_dir = Path(args.inputs_dir)
    output     = Path(args.output)

    for label, p in [("xml", xml_path), ("main", main_path), ("inputs-dir", inputs_dir)]:
        if not p.exists():
            print(f"error: {label} not found: {p}", file=sys.stderr)
            return 1

    n = apply_partial_pdf_from_xml(xml_path, main_path, inputs_dir, output)
    print(f"partial-merge: {n} entry/entries applied -> {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except pikepdf.PasswordError:
        print("error: PDF is encrypted and requires a password", file=sys.stderr)
        return 1
    except pikepdf.PdfError as ex:
        print(f"error: not a valid / readable PDF ({ex})", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
