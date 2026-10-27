"""The four pipeline stages: replay -> mergeinfo -> render -> merge."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import render as R
from .core import (EXIT_MERGE, EXIT_MERGEINFO, EXIT_MISSING_INPUTS, EXIT_PAGE_MISMATCH, EXIT_PREREQ,
                   EXIT_RENDER, EXIT_REPLAY, StageError, StageResult, Workdir, fingerprint, run_stage)
from .srdp import extract_embedded_pdfs, find_pdf_case_insensitive, inspect_srdp

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_SRC = TOOL_DIR.parents[1] / "src"          # <repo>/src  (this worktree's pdfmergepy)
DEFAULT_ENGINE_EXE = r"C:\test\net10.0\SRDP.NET10.exe"
MERGEINFO_DLL = TOOL_DIR / "mergeinfo" / "bin" / "MergeInfoTool.dll"


@dataclass
class Options:
    workdir: Workdir
    force: bool = False
    log: Callable[[str], None] = print
    # replay
    engine_exe: str = os.environ.get("LIVEDOC_SRDP_EXE", DEFAULT_ENGINE_EXE)
    replay_timeout: int = 1800
    inputs_dirs: list[Path] = field(default_factory=list)   # user-supplied {blobId}.pdf dirs
    # mergeinfo
    pptx: Path | None = None                                # override replayed pptx
    mergeinfo_timeout: int = 600
    # render
    lo_mode: str = "auto"            # auto | wsl | native
    wsl_distro: str | None = os.environ.get("LIVEDOC_WSL_DISTRO", "Ubuntu-24.04")
    soffice: str = "soffice"
    render_timeout: int = 900
    allow_page_mismatch: bool = False
    # merge
    output: Path | None = None
    synthesize_missing: bool = False


# ---------------------------------------------------------------------------- helpers
def import_pdfmergepy() -> str:
    """Import pdfmergepy from THIS worktree's src (not any pip-editable install). Returns its path."""
    src = str(REPO_SRC)
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)
    for m in [m for m in sys.modules if m == "pdfmergepy" or m.startswith("pdfmergepy.")]:
        del sys.modules[m]
    import pdfmergepy  # noqa: E402
    got = Path(pdfmergepy.__file__).resolve()
    if REPO_SRC.resolve() not in got.parents:
        raise StageError(f"pdfmergepy imported from {got}, expected under {REPO_SRC}", EXIT_PREREQ)
    return str(got.parent)


def run_logged(cmd: list[str], log_path: Path, timeout: int, cwd: Path | None = None,
               append: bool = False) -> tuple[int, str]:
    """Run argv list, tee stdout+stderr to log_path, return (rc, text). TimeoutExpired -> rc -9."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cp = subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
        text = cp.stdout.decode("utf-8", "replace")
        rc = cp.returncode
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or b"").decode("utf-8", "replace") + f"\n*** TIMEOUT after {timeout}s ***"
        rc = -9
    except FileNotFoundError as e:
        text, rc = f"*** cannot execute {cmd[0]}: {e}", -1
    with open(log_path, "a" if append else "w", encoding="utf-8") as f:
        f.write("$ " + " ".join(cmd) + "\n" + text + f"\n[exit {rc}]\n")
    return rc, text


def _tail(text: str, n: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def pdf_page_count(path: Path) -> int:
    import pikepdf
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def find_replay_pptx(outdir: Path, stdout: str, started: float) -> tuple[Path, list[str]]:
    """Locate the pptx produced by the engine: path printed on stdout, else newest *_output_*.pptx."""
    warns: list[str] = []
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.lower().endswith(".pptx") and Path(line).is_file():
            return Path(line), warns
    cands = [p for p in outdir.glob("*.pptx")]
    pref = [p for p in cands if re.search(r"_output_\d+", p.name)] or cands
    pref = [p for p in pref if p.stat().st_mtime >= started - 2] or pref
    if not pref:
        raise StageError(f"engine produced no .pptx in {outdir}", EXIT_REPLAY)
    pref.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if len(pref) > 1:
        warns.append(f"{len(pref)} pptx files in engine output; using newest {pref[0].name}")
    return pref[0], warns


# ---------------------------------------------------------------------------- stage 1: replay
def replay(opts: Options, srdp: Path) -> StageResult:
    wd = opts.workdir
    info = inspect_srdp(srdp)        # raises StageError(EXIT_BAD_SRDP) for CTS/Word/garbage

    def work(res: StageResult) -> None:
        exe = Path(opts.engine_exe)
        if not exe.is_file():
            raise StageError(f"SRDP engine exe not found: {exe} (set --engine-exe or LIVEDOC_SRDP_EXE)",
                             EXIT_PREREQ)
        if not (exe.parent / "fonts" / "arial.ttf").is_file():
            res.warnings.append(f"{exe.parent}\\fonts\\arial.ttf not found - engine font metrics may differ")
        res.warnings += info.warnings
        if wd.engine_out.exists():
            shutil.rmtree(wd.engine_out)
        wd.engine_out.mkdir(parents=True)
        # one subprocess per replay: the engine has process-global statics
        import time
        t0 = time.time()
        rc, text = run_logged([str(exe), "p0", str(Path(srdp).resolve()), str(wd.engine_out)],
                              wd.replay_log, opts.replay_timeout, cwd=exe.parent)
        if rc != 0:
            raise StageError(f"engine exited with code {rc} (log: {wd.replay_log}):\n{_tail(text)}", EXIT_REPLAY)
        pptx, w = find_replay_pptx(wd.engine_out, text, t0)
        res.warnings += w
        extracted = extract_embedded_pdfs(info, wd.inputs)
        res.details.update(engine=info.engine, pptx=str(pptx), srdp_entries=len(info.entries),
                           embedded_pdfs=len(extracted), declared_external_pdfs=len(info.declared_pdfs))
        if not extracted and not opts.inputs_dirs:
            res.warnings.append("SRDP carries no InputFiles/{blobId}.pdf; external PDFs must come from --inputs-dir")
    state_fp = fingerprint(Path(srdp), opts.engine_exe)
    out_marker = wd.replay_log
    r = run_stage(wd, "replay", work, fp=state_fp, outputs=[out_marker], force=opts.force, log=opts.log)
    st = wd.load_state()
    st["meta"].update(srdp=str(Path(srdp).resolve()), engine=info.engine)
    wd.save_state(st)
    return r


# ---------------------------------------------------------------------------- stage 2: mergeinfo
def referenced_blobs(merge_info_xml: Path) -> dict[str, int]:
    """Blob ids the merge reads from the inputs dir -> highest external page number needed."""
    from pdfmergepy.mergeinfo import parse_merge_info
    from pdfmergepy.partial import parse_partial_pdf_info
    need: dict[str, int] = {}
    mi = parse_merge_info(merge_info_xml)
    for item in mi.merge_items:
        for s in item.slides:
            if item.merged_pdf_file_id and item.start_index_in_merged_file > 0:
                fid, page = item.merged_pdf_file_id, item.start_index_in_merged_file + s.pdf_page
            else:
                fid, page = item.blob_id, s.pdf_page
            need[fid] = max(need.get(fid, 1), page, 1)
        if not item.slides:
            need.setdefault(item.blob_id, max(item.page_count, 1))
    for p in parse_partial_pdf_info(merge_info_xml):
        need.setdefault(p.blob_id, 1)
    return need


def _replay_pptx(opts: Options) -> Path:
    if opts.pptx:
        return Path(opts.pptx)
    st = opts.workdir.load_state()["stages"].get("replay")
    if not st or "pptx" not in st["details"]:
        raise StageError("no replayed pptx: run `replay` first or pass --pptx", EXIT_MERGEINFO)
    return Path(st["details"]["pptx"])


def mergeinfo(opts: Options) -> StageResult:
    wd = opts.workdir
    pptx = _replay_pptx(opts)

    def work(res: StageResult) -> None:
        if not pptx.is_file():
            raise StageError(f"pptx not found: {pptx}", EXIT_MERGEINFO)
        if not MERGEINFO_DLL.is_file():
            raise StageError(f"MergeInfoTool is not built ({MERGEINFO_DLL}). Run: python "
                             f"{TOOL_DIR / 'mergeinfo' / 'build.py'} --cts-assemblies <CTS LiveDoc converter dir>",
                             EXIT_PREREQ)
        dotnet = shutil.which("dotnet")
        if not dotnet:
            raise StageError("'dotnet' not found on PATH (needs .NET 8 runtime)", EXIT_PREREQ)
        wd.mergeinfo_dir.mkdir(parents=True, exist_ok=True)
        wd.merge_info.unlink(missing_ok=True)
        wd.inputs.mkdir(parents=True, exist_ok=True)
        dirs = [wd.inputs] + [Path(d) for d in opts.inputs_dirs]
        rc, text = run_logged([dotnet, str(MERGEINFO_DLL), str(pptx), str(wd.mergeinfo_dir)] + [str(d) for d in dirs],
                              wd.mergeinfo_dir / "mergeinfo.log", opts.mergeinfo_timeout)
        if rc != 0:
            raise StageError(f"MergeInfoTool failed (exit {rc}):\n{_tail(text)}", EXIT_MERGEINFO)
        m = re.search(r"SUMMARY slides=(\d+) hidden=(\d+) expected_pages=(\d+) merge_info=(\d) changed=(\d)", text)
        if not m:
            raise StageError(f"MergeInfoTool printed no SUMMARY line:\n{_tail(text)}", EXIT_MERGEINFO)
        slides, hidden, expected, has_info, changed = map(int, m.groups())
        res.details.update(slides=slides, hidden=hidden, expected_pdf_pages=expected,
                           has_merge_info=bool(has_info), pptx_modified=bool(changed), source_pptx=str(pptx))
        for line in text.splitlines():
            if line.startswith("WARNING:"):
                res.warnings.append(line[8:].strip())
        if has_info:
            from pdfmergepy.mergeinfo import parse_merge_info
            from pdfmergepy.partial import parse_partial_pdf_info
            mi = parse_merge_info(wd.merge_info)
            need = referenced_blobs(wd.merge_info)
            n_part = len(parse_partial_pdf_info(wd.merge_info))
            res.details.update(merge_items=len(mi.merge_items), partial_pdf=n_part,
                               referenced_blob_ids=sorted(need))
        else:
            res.details.update(merge_items=0, partial_pdf=0, referenced_blob_ids=[])
            res.warnings.append("no WorkspaceMergeInfo in the pptx: the final PDF is just the rendered main PDF")
        if hidden:
            res.warnings.append(f"{hidden} hidden slide(s): they are excluded from the PDF and from external-page placement")
    fp = fingerprint(pptx, MERGEINFO_DLL, *map(str, opts.inputs_dirs))
    return run_stage(wd, "mergeinfo", work, fp=fp, outputs=[wd.stripped], force=opts.force, log=opts.log)


# ---------------------------------------------------------------------------- stage 3: render
def fonts_requested(pptx: Path) -> set[str]:
    out: set[str] = set()
    with zipfile.ZipFile(pptx) as z:
        for n in z.namelist():
            if re.match(r"ppt/(slides/slide\d+|theme/theme\d+)\.xml$", n):
                txt = z.read(n).decode("utf-8", "replace")
                out.update(re.findall(r'<a:(?:latin|ea|cs)\s+typeface="([^"+][^"]*)"', txt))
    return {f for f in out if f.strip()}


def fonts_in_pdf(pdf: Path) -> set[str]:
    import pikepdf
    names: set[str] = set()
    with pikepdf.open(pdf) as p:
        for page in p.pages:
            res = page.get("/Resources")
            fonts = res.get("/Font") if res is not None else None
            if fonts is None:
                continue
            for _k, f in fonts.items():
                bf = f.get("/BaseFont")
                if bf is not None:
                    names.add(re.sub(r"^[A-Z]{6}\+", "", str(bf).lstrip("/")))
    return names


def _norm_font(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def font_substitution_warnings(pptx: Path, pdf: Path) -> list[str]:
    try:
        have = [_norm_font(f) for f in fonts_in_pdf(pdf)]
        miss = sorted(f for f in fonts_requested(pptx)
                      if not any(_norm_font(f) in h or h in _norm_font(f) for h in have if h))
    except Exception as e:  # heuristic only
        return [f"font-substitution check skipped: {e}"]
    if not miss:
        return []
    return ["fonts referenced by the pptx but not found by name in the PDF (probable LibreOffice substitution): "
            + ", ".join(miss[:12]) + (" ..." if len(miss) > 12 else "")]


def pick_lo_mode(opts: Options) -> str:
    if opts.lo_mode != "auto":
        return opts.lo_mode
    if sys.platform == "win32":
        return "wsl" if shutil.which("wsl.exe") or shutil.which("wsl") else "native"
    return "native"


def _render_wsl(opts: Options, res: StageResult, pptx: Path, pdf: Path) -> None:
    wd = opts.workdir
    log = wd.render_dir / "render.log"
    rc, text = run_logged(R.wsl_mktemp_command(opts.wsl_distro), log, 60)
    tmp = text.strip().splitlines()[0].strip() if rc == 0 and text.strip() else ""
    if rc != 0 or not tmp.startswith("/tmp/"):
        raise StageError(f"cannot create temp dir in WSL (distro {opts.wsl_distro!r}):\n{_tail(text)}", EXIT_PREREQ)
    cmds = R.wsl_commands(opts.wsl_distro, pptx, pdf, tmp)
    try:
        for i, cmd in enumerate(cmds[:3]):
            rc, text = run_logged(cmd, log, opts.render_timeout, append=True)
            if rc != 0:
                what = ("copy into WSL", "soffice convert", "copy out of WSL")[i]
                hint = " (is LibreOffice installed in that distro? `wsl -e soffice --version`)" if i == 1 else ""
                raise StageError(f"WSL step '{what}' failed (exit {rc}){hint}:\n{_tail(text)}", EXIT_RENDER)
    finally:
        run_logged(cmds[3], log, 60, append=True)
    rc, text = run_logged(["wsl.exe"] + (["-d", opts.wsl_distro] if opts.wsl_distro else []) + ["-e", "soffice", "--version"],
                          log, 60, append=True)
    res.details["libreoffice"] = text.strip().splitlines()[0].strip() if rc == 0 and text.strip() else "unknown"


def _render_native(opts: Options, res: StageResult, pptx: Path, pdf: Path) -> None:
    wd = opts.workdir
    soffice = shutil.which(opts.soffice) or opts.soffice
    if not shutil.which(soffice) and not Path(soffice).is_file():
        raise StageError(f"soffice not found ({opts.soffice}); install LibreOffice or pass --soffice", EXIT_PREREQ)
    outdir = wd.render_dir / "lo_out"
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True)
    src = wd.render_dir / "in.pptx"
    shutil.copyfile(pptx, src)
    cmd = R.native_command(soffice, src, outdir, wd.render_dir / "lo_profile")
    rc, text = run_logged(cmd, wd.render_dir / "render.log", opts.render_timeout)
    produced = outdir / "in.pdf"
    if rc != 0 or not produced.is_file():
        raise StageError(f"soffice convert failed (exit {rc}):\n{_tail(text)}", EXIT_RENDER)
    shutil.copyfile(produced, pdf)
    rc, text = run_logged([soffice, "--version"], wd.render_dir / "render.log", 60, append=True)
    res.details["libreoffice"] = text.strip().splitlines()[0].strip() if rc == 0 and text.strip() else "unknown"


def render(opts: Options) -> StageResult:
    wd = opts.workdir
    mi = wd.load_state()["stages"].get("mergeinfo")
    if not mi or not wd.stripped.is_file():
        raise StageError("no stripped.pptx: run `mergeinfo` first", EXIT_RENDER)
    slides, hidden = mi["details"]["slides"], mi["details"]["hidden"]

    def work(res: StageResult) -> None:
        mode = pick_lo_mode(opts)
        wd.render_dir.mkdir(parents=True, exist_ok=True)
        wd.main_pdf.unlink(missing_ok=True)
        (_render_wsl if mode == "wsl" else _render_native)(opts, res, wd.stripped, wd.main_pdf)
        if not wd.main_pdf.is_file():
            raise StageError("render produced no PDF", EXIT_RENDER)
        pages = pdf_page_count(wd.main_pdf)
        ok, diag = R.check_page_count(pages, slides, hidden)
        res.details.update(mode=mode, slides=slides, hidden=hidden, pdf_pages=pages,
                           expected_pdf_pages=slides - hidden, page_count_ok=ok)
        res.warnings += font_substitution_warnings(wd.stripped, wd.main_pdf)
        if not ok:
            if not opts.allow_page_mismatch:
                raise StageError(diag + " (re-run with --allow-page-mismatch to continue anyway)", EXIT_PAGE_MISMATCH)
            res.warnings.append(diag + " [continuing: --allow-page-mismatch]")
    return run_stage(wd, "render", work, fp=fingerprint(wd.stripped, pick_lo_mode(opts), opts.allow_page_mismatch),
                     outputs=[wd.main_pdf], force=opts.force, log=opts.log)


# ---------------------------------------------------------------------------- stage 4: merge
def synthesize_pdf(path: Path, pages: int) -> None:
    """Placeholder external PDF (blank Letter pages with a visible marker) - for pipeline exercising only."""
    import pikepdf
    pdf = pikepdf.new()
    for i in range(max(pages, 1)):
        page = pdf.add_blank_page(page_size=(612, 792))
        content = (f"BT /F1 24 Tf 72 700 Td (SYNTHETIC {path.stem[:20]} p{i + 1}) Tj ET").encode()
        page.Contents = pdf.make_stream(content)
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica)))
    pdf.save(path)


def resolve_inputs(need: dict[str, int], search_dirs: list[Path], dest: Path,
                   synthesize: bool) -> tuple[list[str], list[str], list[str]]:
    """Copy needed {blobId}.pdf into dest. Returns (found, synthesized, missing) blob ids."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    found, synth, missing = [], [], []
    for blob in sorted(need):
        src = next((p for d in search_dirs if (p := find_pdf_case_insensitive(Path(d), blob))), None)
        if src:
            shutil.copyfile(src, dest / f"{blob}.pdf")
            found.append(blob)
        elif synthesize:
            synthesize_pdf(dest / f"{blob}.pdf", need[blob])
            synth.append(blob)
        else:
            missing.append(blob)
    return found, synth, missing


def merge(opts: Options) -> StageResult:
    wd = opts.workdir
    out = Path(opts.output) if opts.output else wd.final_pdf
    if not wd.main_pdf.is_file():
        raise StageError("no main.pdf: run `render` first", EXIT_MERGE)

    def work(res: StageResult) -> None:
        res.details["pdfmergepy"] = import_pdfmergepy()
        res.details["main_pdf_pages"] = pdf_page_count(wd.main_pdf)
        out.parent.mkdir(parents=True, exist_ok=True)
        if not wd.merge_info.is_file():
            shutil.copyfile(wd.main_pdf, out)
            res.warnings.append("no merge-info.xml: output is the rendered main PDF unchanged")
            res.details.update(merge_items=0, partial_pdf=0, output=str(out), output_pages=pdf_page_count(out))
            return
        need = referenced_blobs(wd.merge_info)
        found, synth, missing = resolve_inputs(need, [wd.inputs] + [Path(d) for d in opts.inputs_dirs],
                                               wd.inputs_resolved, opts.synthesize_missing)
        res.details.update(referenced_blob_ids=len(need), inputs_found=len(found),
                           inputs_synthesized=len(synth), inputs_missing=missing)
        if synth:
            res.warnings.append("SYNTHETIC placeholder PDFs used for blob ids (output is NOT representative): "
                                + ", ".join(synth))
        if missing:
            raise StageError("merge-info references external PDFs that are not available: " + ", ".join(missing)
                             + "\nProvide them via --inputs-dir <dir with {blobId}.pdf> (or an SRDP built with the "
                             "embedded-PDF engine option), or pass --synthesize-missing to exercise the path "
                             "with placeholders.", EXIT_MISSING_INPUTS)
        from pdfmergepy.partial import merge_xml_then_partial
        try:
            n_merge, n_partial = merge_xml_then_partial(wd.merge_info, wd.main_pdf, wd.inputs_resolved, out)
        except Exception as e:
            raise StageError(f"pdfmergepy merge-xml failed: {type(e).__name__}: {e}", EXIT_MERGE)
        res.details.update(merge_items=n_merge, partial_pdf=n_partial, output=str(out),
                           output_pages=pdf_page_count(out))
    fp = fingerprint(wd.main_pdf, wd.merge_info, str(out), opts.synthesize_missing, *map(str, opts.inputs_dirs))
    return run_stage(wd, "merge", work, fp=fp, outputs=[out], force=opts.force, log=opts.log)
