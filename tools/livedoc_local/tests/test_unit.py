"""Unit tests that need none of the heavy tools (no engine exe, dotnet, LibreOffice, WSL)."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pikepdf
import pytest

from livedoc_local import core, render as R, stages as S
from livedoc_local.srdp import (extract_embedded_pdfs, find_pdf_case_insensitive, inspect_srdp,
                                parse_index_xml, safe_blob_id)

INDEX = ('<?xml version="1.0"?><Index><Engine>V2</Engine>%s</Index>')


def make_srdp(path: Path, index: str | None = INDEX % "", extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        if index is not None:
            z.writestr("index.xml", index)
        z.writestr("MainLiveDoc.pptx", b"x")
        for n, b in (extra or {}).items():
            z.writestr(n, b)
    return path


def blank_pdf(path: Path, pages: int = 1) -> Path:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(200, 200))
    pdf.save(path)
    return path


# ------------------------------------------------------------------ SRDP parsing
def test_parse_index_xml_engine_and_external_pdfs():
    eng, ids = parse_index_xml(INDEX % '<ExternalPdfs><Pdf BlobId="aa-1"/><Pdf BlobId="bb-2"/></ExternalPdfs>')
    assert eng == "V2" and ids == ["aa-1", "bb-2"]


def test_inspect_engine_srdp(tmp_path):
    z = make_srdp(tmp_path / "a.zip", INDEX % '<ExternalPdfs><Pdf BlobId="aa"/><Pdf BlobId="zz"/></ExternalPdfs>',
                  {"InputFiles/aa.pdf": b"%PDF-1.4"})
    info = inspect_srdp(z)
    assert info.engine == "V2" and list(info.embedded_pdfs) == ["aa"]
    assert info.missing_declared == ["zz"]
    assert any("zz" in w for w in info.warnings)


def test_cts_srdp_json_rejected(tmp_path):
    z = tmp_path / "cts.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("srdp.json", "{}")
    with pytest.raises(core.StageError) as e:
        inspect_srdp(z)
    assert e.value.exit_code == core.EXIT_BAD_SRDP and "srdp.json" in str(e.value)


def test_word_srdp_rejected(tmp_path):
    z = tmp_path / "w.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("index.xml", INDEX % "")
        f.writestr("MainLiveDoc.docx", b"x")
    with pytest.raises(core.StageError):
        inspect_srdp(z)


def test_not_a_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_text("nope")
    with pytest.raises(core.StageError):
        inspect_srdp(p)


def test_extract_embedded_pdfs_and_zip_slip(tmp_path):
    z = make_srdp(tmp_path / "a.zip", extra={
        "InputFiles/good-1.pdf": b"%PDF-good",
        "InputFiles/../evil.pdf": b"%PDF-evil",
        "InputFiles/sub/dir.pdf": b"%PDF-sub",
    })
    info = inspect_srdp(z)
    assert list(info.embedded_pdfs) == ["good-1"]
    assert any("suspicious" in w for w in info.warnings)
    dest = tmp_path / "inputs"
    assert extract_embedded_pdfs(info, dest) == ["good-1"]
    assert (dest / "good-1.pdf").read_bytes() == b"%PDF-good"
    assert sorted(p.name for p in dest.iterdir()) == ["good-1.pdf"]
    assert not (tmp_path / "evil.pdf").exists()


@pytest.mark.parametrize("bid,ok", [("0021c935-ee3a-c7b3", True), ("a.b_c", True), ("../x", False),
                                    ("a/b", False), ("", False), ("a b", False), ("a..b", False)])
def test_safe_blob_id(bid, ok):
    assert safe_blob_id(bid) is ok


def test_find_pdf_case_insensitive(tmp_path):
    (tmp_path / "ABC.PDF").write_bytes(b"x")
    assert find_pdf_case_insensitive(tmp_path, "abc").name == "ABC.PDF"
    assert find_pdf_case_insensitive(tmp_path, "nope") is None


# ------------------------------------------------------------------ render command construction / page check
def test_win_to_wsl_path():
    assert R.win_to_wsl_path(r"C:\Users\Gary Liu\a b\x.pptx") == "/mnt/c/Users/Gary Liu/a b/x.pptx"
    assert R.win_to_wsl_path("D:/x/y.pdf") == "/mnt/d/x/y.pdf"
    with pytest.raises(ValueError):
        R.win_to_wsl_path("/already/posix")


def test_convert_to_arg_is_tagged_and_skips_hidden():
    arg = R.soffice_convert_to_arg()
    head, opts = arg.split(":", 2)[0:2], arg.split(":", 2)[2]
    assert arg.startswith("pdf:impress_pdf_Export:")
    o = json.loads(opts)
    assert o["UseTaggedPDF"]["value"] == "true" and o["ExportHiddenSlides"]["value"] == "false"


def test_wsl_commands_are_explicit_argv():
    cmds = R.wsl_commands("Ubuntu-24.04", Path(r"C:\w\in put.pptx"), Path(r"C:\w\main.pdf"), "/tmp/livedoc_local.AbC")
    assert [c[0] for c in cmds] == ["wsl.exe"] * 4
    assert cmds[0][:4] == ["wsl.exe", "-d", "Ubuntu-24.04", "-e"]
    assert cmds[0][4:] == ["cp", "/mnt/c/w/in put.pptx", "/tmp/livedoc_local.AbC/in.pptx"]
    conv = cmds[1]
    assert conv[4] == "soffice" and "--headless" in conv and "-env:UserInstallation=file:///tmp/livedoc_local.AbC/profile" in conv
    assert conv[conv.index("--convert-to") + 1] == R.soffice_convert_to_arg()
    assert conv[-1] == "/tmp/livedoc_local.AbC/in.pptx" and conv[conv.index("--outdir") + 1] == "/tmp/livedoc_local.AbC/out"
    assert cmds[2][-2:] == ["/tmp/livedoc_local.AbC/out/in.pdf", "/mnt/c/w/main.pdf"]
    assert cmds[3][-3:] == ["rm", "-rf", "/tmp/livedoc_local.AbC"]
    assert "-d" not in R.wsl_commands(None, Path(r"C:\a.pptx"), Path(r"C:\a.pdf"), "/tmp/x")[0]


def test_native_command(tmp_path):
    cmd = R.native_command("soffice", tmp_path / "in.pptx", tmp_path / "out", tmp_path / "prof")
    assert cmd[0] == "soffice" and "--headless" in cmd and cmd[-1].endswith("in.pptx")
    assert any(a.startswith("-env:UserInstallation=file:") for a in cmd)


def test_check_page_count_ok_and_diagnoses():
    assert R.check_page_count(8, 10, 2)[0]
    ok, msg = R.check_page_count(10, 10, 2)
    assert not ok and "hidden slides too" in msg
    ok, msg = R.check_page_count(12, 10, 0)
    assert not ok and "More pages" in msg
    ok, msg = R.check_page_count(5, 10, 0)
    assert not ok and "Fewer pages" in msg


# ------------------------------------------------------------------ stages helpers
def test_pdfmergepy_imported_from_this_checkout():
    path = S.import_pdfmergepy()
    assert Path(path).resolve() == (S.REPO_SRC / "pdfmergepy").resolve()


MERGE_XML = """<?xml version="1.0" encoding="utf-8"?>
<WorkspaceMergeInfo>
  <PDFMerge>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="3" id="i1" BlobId="blob-a">
      <MergedPdfFileInfo MergedPdfFileId="" StartIndexInMergedFile="-1" />
      <slideLocalId pdfPage="1" slideIndex="2">42</slideLocalId>
      <slideLocalId pdfPage="3" slideIndex="3">43</slideLocalId>
    </MergeItem>
    <MergeItem FitPDFSize="true" SlideFitPattern="AlignTopLeft" pageCount="1" id="i2" BlobId="blob-b">
      <MergedPdfFileInfo MergedPdfFileId="merged-m" StartIndexInMergedFile="4" />
      <slideLocalId pdfPage="2" slideIndex="4">44</slideLocalId>
    </MergeItem>
  </PDFMerge>
  <SlideInfo><info slideIndex="1" localId="10" hidden="false" /></SlideInfo>
</WorkspaceMergeInfo>
"""


def test_referenced_blobs(tmp_path):
    S.import_pdfmergepy()
    x = tmp_path / "merge-info.xml"
    x.write_text(MERGE_XML, encoding="utf-8")
    assert S.referenced_blobs(x) == {"blob-a": 3, "merged-m": 6}


def test_resolve_inputs_found_synth_missing(tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    d1.mkdir(); d2.mkdir()
    blank_pdf(d1 / "Blob-A.pdf", 3)
    need = {"blob-a": 3, "gone": 2}
    found, synth, missing = S.resolve_inputs(need, [d1, d2], tmp_path / "r", synthesize=False)
    assert (found, synth, missing) == (["blob-a"], [], ["gone"])
    assert (tmp_path / "r" / "blob-a.pdf").is_file()
    found, synth, missing = S.resolve_inputs(need, [d1], tmp_path / "r2", synthesize=True)
    assert synth == ["gone"] and not missing
    with pikepdf.open(tmp_path / "r2" / "gone.pdf") as p:
        assert len(p.pages) == 2


def test_find_replay_pptx_prefers_stdout_then_newest(tmp_path):
    a = tmp_path / "x_V2_output_1.pptx"; a.write_bytes(b"1")
    b = tmp_path / "x_V2_output_2.pptx"; b.write_bytes(b"2")
    assert S.find_replay_pptx(tmp_path, f"log\n{a}\n", 0)[0] == a
    p, warns = S.find_replay_pptx(tmp_path, "no path printed", 0)
    assert p.suffix == ".pptx" and warns
    with pytest.raises(core.StageError):
        S.find_replay_pptx(tmp_path / "empty_dir_missing", "", 0)


def test_mergeinfo_build_script_reports_missing_path(tmp_path, capsys):
    sys.path.insert(0, str(S.TOOL_DIR / "mergeinfo"))
    try:
        import build
        assert build.main(["--cts-assemblies", str(tmp_path / "nope")]) == 3
        assert "missing" in capsys.readouterr().err
        assert build.check_assemblies(tmp_path / "nope")
    finally:
        sys.path.pop(0)


# ------------------------------------------------------------------ report / state / caching
def test_exit_codes_unique():
    codes = [v for k, v in vars(core).items() if k.startswith("EXIT_")]
    assert len(codes) == len(set(codes))


def test_stage_cache_and_report(tmp_path):
    wd = core.Workdir(tmp_path / "w")
    wd.ensure()
    out = wd.root / "o.txt"
    calls = []

    def work(res):
        calls.append(1)
        out.write_text("x")
        res.details["n"] = 3
        res.warnings.append("assumption A")

    r1 = core.run_stage(wd, "replay", work, fp="f1", outputs=[out], force=False, log=lambda m: None)
    r2 = core.run_stage(wd, "replay", work, fp="f1", outputs=[out], force=False, log=lambda m: None)
    assert r1.status == "ok" and r2.status == "cached" and len(calls) == 1 and r2.details["n"] == 3
    core.run_stage(wd, "replay", work, fp="f2", outputs=[out], force=False, log=lambda m: None)   # fp changed
    core.run_stage(wd, "replay", work, fp="f2", outputs=[out], force=True, log=lambda m: None)    # forced
    assert len(calls) == 3

    def boom(res):
        raise core.StageError("kaput", core.EXIT_RENDER)
    with pytest.raises(core.StageError):
        core.run_stage(wd, "render", boom, fp="x", outputs=[], force=False, log=lambda m: None)
    rep = core.build_report(wd)
    assert [s["name"] for s in rep["stages"]] == ["replay", "render"]
    assert rep["ok"] is False and "[replay] assumption A" in rep["warnings"]
    assert json.loads(json.dumps(rep))["stages"][1]["error"] == "kaput"
    text = core.format_report(rep)
    assert "ERROR: kaput" in text and "assumption A" in text and "NOT OK" in text


def test_rerunning_upstream_invalidates_downstream(tmp_path):
    wd = core.Workdir(tmp_path / "w"); wd.ensure()
    f = wd.root / "f"; f.write_text("x")
    for name in ("replay", "mergeinfo"):
        core.run_stage(wd, name, lambda r: None, fp="1", outputs=[f], force=False, log=lambda m: None)
    core.run_stage(wd, "replay", lambda r: None, fp="2", outputs=[f], force=False, log=lambda m: None)
    assert "mergeinfo" not in wd.load_state()["stages"]


def test_cli_parser_and_usage_errors(tmp_path, capsys):
    from livedoc_local.cli import build_parser, main
    a = build_parser().parse_args(["all", "x.zip", "-w", "W", "--force", "--keep", "--json-report", "r.json",
                                   "-i", "d1", "-i", "d2", "--lo-mode", "native"])
    assert a.workdir == "W" and a.inputs_dir == ["d1", "d2"] and a.lo_mode == "native" and a.force
    assert main(["all", str(tmp_path / "x.zip")]) == 2          # no -w and no -o
    assert main(["replay", str(tmp_path / "x.zip"), "-w", str(tmp_path / "w")]) == core.EXIT_BAD_SRDP


def test_compare_identical_and_different(tmp_path):
    from livedoc_local.compare import compare_pdfs
    a, b, c = (blank_pdf(tmp_path / n, p) for n, p in (("a.pdf", 2), ("b.pdf", 2), ("c.pdf", 3)))
    assert compare_pdfs(a, b)["identical"]
    r = compare_pdfs(a, c)
    assert not r["identical"] and any("page count" in d for d in r["differences"])
