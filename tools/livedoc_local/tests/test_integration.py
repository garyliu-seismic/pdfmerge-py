"""INTEGRATION TEST - skipped unless the heavy tools exist.

Needs: the engine exe (LIVEDOC_SRDP_EXE or C:\\test\\net10.0\\SRDP.NET10.exe), a built MergeInfoTool
(mergeinfo/build.py), LibreOffice (WSL distro or native soffice), and an engine SRDP zip given by
env LIVEDOC_LOCAL_TEST_SRDP.  Run:  LIVEDOC_LOCAL_TEST_SRDP=<zip> pytest tools/livedoc_local/tests -k integration
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pytest

from livedoc_local import stages as S
from livedoc_local.cli import main

SRDP = os.environ.get("LIVEDOC_LOCAL_TEST_SRDP")


def _lo_available() -> bool:
    if os.name == "nt":
        if not shutil.which("wsl.exe"):
            return False
        r = subprocess.run(["wsl.exe", "-e", "which", "soffice"], capture_output=True)
        return r.returncode == 0
    return bool(shutil.which("soffice"))


pytestmark = pytest.mark.skipif(
    not (SRDP and Path(SRDP).is_file() and Path(S.Options.engine_exe).is_file()
         and S.MERGEINFO_DLL.is_file() and shutil.which("dotnet") and _lo_available()),
    reason="integration: needs LIVEDOC_LOCAL_TEST_SRDP, engine exe, built MergeInfoTool, dotnet, LibreOffice")


def test_end_to_end(tmp_path):
    out = tmp_path / "final.pdf"
    rc = main(["all", SRDP, "-w", str(tmp_path / "w"), "-o", str(out), "--synthesize-missing",
               "--json-report", str(tmp_path / "r.json")])
    assert rc == 0
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages) >= 1
