"""livedoc_local CLI: replay an engine SRDP and produce the final merged PDF with no CTS 2.0 / Office."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import stages as S
from .core import (EXIT_COMPARE_DIFF, EXIT_OK, StageError, Workdir, build_report, format_report)


def _common(p: argparse.ArgumentParser, workdir_required: bool) -> None:
    p.add_argument("--workdir", "-w", required=workdir_required, help="directory for intermediate artifacts "
                   "(kept; re-running reuses cached stages)")
    p.add_argument("--keep", action="store_true", help="keep an auto-created temp workdir (default: delete it)")
    p.add_argument("--force", action="store_true", help="re-run stages even if cached results exist")
    p.add_argument("--json-report", metavar="FILE", help="also write the final report as JSON")
    p.add_argument("--inputs-dir", "-i", action="append", default=[], metavar="DIR",
                   help="dir with {blobId}.pdf external PDFs (repeatable); SRDP-embedded InputFiles win")


def _render_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lo-mode", choices=["auto", "wsl", "native"], default="auto",
                   help="auto: WSL on Windows, native soffice elsewhere")
    p.add_argument("--wsl-distro", default=S.Options.wsl_distro, help="default %(default)s (env LIVEDOC_WSL_DISTRO)")
    p.add_argument("--soffice", default="soffice", help="soffice binary for --lo-mode native")
    p.add_argument("--allow-page-mismatch", action="store_true",
                   help="continue when PDF pages != slides - hidden (warns instead of failing)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="livedoc_local", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("replay", help="stage 1: run the engine SRDP replay, extract embedded external PDFs")
    p.add_argument("srdp", type=Path)
    p.add_argument("--engine-exe", default=S.Options.engine_exe, help="default %(default)s (env LIVEDOC_SRDP_EXE)")
    p.add_argument("--timeout", type=int, default=1800)
    _common(p, True)

    p = sub.add_parser("mergeinfo", help="stage 2: Office-free WorkspaceMergeInfo extraction -> stripped.pptx + merge-info.xml")
    p.add_argument("--pptx", type=Path, help="use this pptx instead of the replayed one")
    _common(p, True)

    p = sub.add_parser("render", help="stage 3: LibreOffice pptx -> tagged PDF, verify page count")
    _render_opts(p)
    _common(p, True)

    p = sub.add_parser("merge", help="stage 4: pdfmergepy merge-xml (+ partialPDF) -> final PDF")
    p.add_argument("-o", "--output", type=Path, help="default <workdir>/out/final.pdf")
    p.add_argument("--synthesize-missing", action="store_true",
                   help="create SYNTHETIC placeholder PDFs for missing external blobs (pipeline exercising only)")
    _common(p, True)

    for name in ("all", "run"):
        p = sub.add_parser(name, help="run all four stages" + (" (alias of all)" if name == "run" else ""))
        p.add_argument("srdp", type=Path)
        p.add_argument("-o", "--output", type=Path, help="final PDF (required without --workdir)")
        p.add_argument("--engine-exe", default=S.Options.engine_exe)
        p.add_argument("--timeout", type=int, default=1800)
        p.add_argument("--pptx", type=Path, help=argparse.SUPPRESS)
        p.add_argument("--synthesize-missing", action="store_true")
        _render_opts(p)
        _common(p, False)

    p = sub.add_parser("compare", help="diff two PDFs (e.g. local output vs a production output)")
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    p.add_argument("--dpi", type=int, default=50)
    p.add_argument("--json-report", metavar="FILE")
    return ap


def _opts(a: argparse.Namespace, wd: Workdir) -> S.Options:
    o = S.Options(workdir=wd, force=a.force, log=lambda m: print(m, file=sys.stderr))
    o.inputs_dirs = [Path(d) for d in a.inputs_dir]
    for attr, dest in (("engine_exe", "engine_exe"), ("timeout", "replay_timeout"), ("pptx", "pptx"),
                       ("lo_mode", "lo_mode"), ("wsl_distro", "wsl_distro"), ("soffice", "soffice"),
                       ("allow_page_mismatch", "allow_page_mismatch"), ("output", "output"),
                       ("synthesize_missing", "synthesize_missing")):
        if hasattr(a, attr):
            setattr(o, dest, getattr(a, attr))
    return o


def _finish(wd: Workdir, a: argparse.Namespace, code: int, err: str | None) -> int:
    rep = build_report(wd, {"exit_code": code})
    print(format_report(rep))
    if err:
        print(f"\nFAILED (exit {code}): {err}", file=sys.stderr)
    if a.json_report:
        Path(a.json_report).write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return code


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "compare":
        from .compare import compare_pdfs, format_compare
        res = compare_pdfs(a.a, a.b, dpi=a.dpi)
        print(format_compare(res))
        if a.json_report:
            Path(a.json_report).write_text(json.dumps(res, indent=2), encoding="utf-8")
        return EXIT_OK if res["identical"] else EXIT_COMPARE_DIFF

    temp = None
    if a.workdir:
        wd = Workdir(Path(a.workdir))
    else:
        if not a.output:
            print("error: without --workdir, -o/--output is required (the temp workdir is deleted)", file=sys.stderr)
            return 2
        temp = tempfile.mkdtemp(prefix="livedoc_local_")
        wd = Workdir(Path(temp))
    wd.ensure()
    o = _opts(a, wd)
    code, err = EXIT_OK, None
    try:
        S.import_pdfmergepy()
        if a.cmd in ("replay", "all", "run"):
            S.replay(o, a.srdp)
        if a.cmd in ("mergeinfo", "all", "run"):
            S.mergeinfo(o)
        if a.cmd in ("render", "all", "run"):
            S.render(o)
        if a.cmd in ("merge", "all", "run"):
            S.merge(o)
    except StageError as e:
        code, err = e.exit_code, str(e)
    try:
        return _finish(wd, a, code, err)
    finally:
        if temp and not a.keep:
            shutil.rmtree(temp, ignore_errors=True)
        elif temp:
            print(f"workdir kept: {temp}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
