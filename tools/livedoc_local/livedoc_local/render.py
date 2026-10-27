"""LibreOffice rendering: pptx -> tagged PDF (command construction + page-count check).

On Windows LibreOffice is driven inside WSL2 (files are copied into the WSL filesystem first;
Windows paths are translated to /mnt/<drive>/... by hand, and every wsl.exe call uses an explicit
argv list - never a shell string - so Git Bash / PowerShell path mangling cannot interfere).
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath

PDF_FILTER_OPTIONS = {
    "UseTaggedPDF": {"type": "boolean", "value": "true"},
    "ExportHiddenSlides": {"type": "boolean", "value": "false"},
}


def soffice_convert_to_arg() -> str:
    """The --convert-to value: pdf:impress_pdf_Export:{json options} (tagged, hidden slides skipped)."""
    return "pdf:impress_pdf_Export:" + json.dumps(PDF_FILTER_OPTIONS, separators=(",", ":"))


def win_to_wsl_path(p: str | Path) -> str:
    """C:\\a\\b c.pptx -> /mnt/c/a/b c.pptx  (no shell involved, spaces are fine)."""
    s = str(p)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if not m:
        raise ValueError(f"not an absolute Windows drive path: {s!r}")
    rest = PureWindowsPath(m.group(2)).as_posix()
    return f"/mnt/{m.group(1).lower()}/{rest}"


def native_command(soffice: str, pptx: Path, outdir: Path, profile_dir: Path) -> list[str]:
    return [soffice, "--headless", f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
            "--convert-to", soffice_convert_to_arg(), "--outdir", str(outdir), str(pptx)]


def wsl_commands(distro: str | None, win_pptx: Path, win_pdf: Path, tmp: str) -> list[list[str]]:
    """Ordered argv lists to run via subprocess (after `tmp` = `wsl mktemp -d` result).

    copy in -> convert -> copy back -> cleanup.  The converted file is always <tmp>/out/in.pdf.
    """
    base = ["wsl.exe"] + (["-d", distro] if distro else []) + ["-e"]
    return [
        base + ["cp", win_to_wsl_path(win_pptx), f"{tmp}/in.pptx"],
        base + ["soffice", "--headless", f"-env:UserInstallation=file://{tmp}/profile",
                "--convert-to", soffice_convert_to_arg(), "--outdir", f"{tmp}/out", f"{tmp}/in.pptx"],
        base + ["cp", f"{tmp}/out/in.pdf", win_to_wsl_path(win_pdf)],
        base + ["rm", "-rf", tmp],
    ]


def wsl_mktemp_command(distro: str | None) -> list[str]:
    return ["wsl.exe"] + (["-d", distro] if distro else []) + ["-e", "mktemp", "-d", "/tmp/livedoc_local.XXXXXX"]


def check_page_count(pdf_pages: int, slides: int, hidden: int) -> tuple[bool, str]:
    """Verify PDF pages == slides - hidden. Returns (ok, human diagnosis)."""
    expected = slides - hidden
    if pdf_pages == expected:
        return True, f"PDF has {pdf_pages} page(s) == {slides} slides - {hidden} hidden"
    msg = f"page-count mismatch: PDF has {pdf_pages} page(s), expected {expected} ({slides} slides - {hidden} hidden). "
    if hidden and pdf_pages == slides:
        msg += ("LibreOffice exported the hidden slides too (ExportHiddenSlides=false was not honoured); "
                "the merge would mis-align external pages.")
    elif pdf_pages > expected:
        msg += ("More pages than expected: slides may have been split (e.g. notes/handout export, or a "
                "table/text overflow producing extra pages) or hidden slides were exported.")
    else:
        msg += ("Fewer pages than expected: LibreOffice may have dropped/failed to import slides "
                "(corrupt shapes, unsupported content) or treated more slides as hidden.")
    return False, msg
