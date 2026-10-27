# livedoc_local - local SRDP -> final PDF (no CTS 2.0, no Office 365)

A **dev/QA tool, not production**. It replays an *engine* SRDP, extracts the merge info the way
CTS 2.0's `PowerPointConverterBase.__ProcessLiveDoc` does (but Office-free), renders the pptx to a
tagged PDF with LibreOffice, and runs `pdfmergepy` to produce the final merged PDF.

```
SRDP.zip --replay--> <name>_V2_output_<ts>.pptx --mergeinfo--> stripped.pptx + merge-info.xml
        --render (LibreOffice, tagged PDF)--> main.pdf --merge (pdfmergepy + external PDFs)--> final.pdf
```

Each stage is a subcommand, leaves its artifacts on disk in `--workdir`, and is cached (re-running a
stage with unchanged inputs prints `cached`; `--force` re-runs). `all` (alias `run`) runs the four.

## Prerequisites (exact)

| Need | Used by | Notes |
|---|---|---|
| Python >= 3.10, `pikepdf>=9`, `fonttools` | everything | what pdfmergepy already requires (`pip install pikepdf fonttools`) |
| `pymupdf` (optional) | `compare` | text / raster / fonts / outlines / link checks; without it those are reported as skipped |
| Engine replay exe, net10 build | `replay` | default `C:\test\net10.0\SRDP.NET10.exe` (`--engine-exe` / env `LIVEDOC_SRDP_EXE`). Usage `<exe> p0 <zip> <existing outDir>`. Needs `fonts\arial.ttf` next to the exe; one subprocess per replay (engine has process-global statics) |
| .NET SDK 8+ and the **released** CTS LiveDoc converter DLLs | `mergeinfo` | built once by `mergeinfo/build.py` (below). No third-party DLLs are committed |
| LibreOffice >= 24.2 | `render` | Windows: inside WSL2 (default distro `Ubuntu-24.04`, `--wsl-distro` / env `LIVEDOC_WSL_DISTRO`; check `wsl -d Ubuntu-24.04 -e soffice --version`). Linux/macOS: `soffice` on PATH (or `--soffice`) |

### One-time: build the Office-free merge-info tool

```
python tools/livedoc_local/mergeinfo/build.py --cts-assemblies <ctsRepo>\src\CTS\assemblies\Release\converters\LiveDoc
```
(or set env `LIVEDOC_CTS_ASSEMBLIES`). The script fails with a clear message if the directory or
`Seismic.CTS.Implements.LiveDocs.dll` / `DocumentFormat.OpenXml*.dll` / `Seismic.CTS.Converter.LiveDoc.deps.json`
are missing. It copies the converter's runtime DLLs into `mergeinfo/bin/` (git-ignored) and installs the
converter's `deps.json` as `MergeInfoTool.deps.json` (needed so `System.IO.Packaging 8.0.0.0` loads).
`mergeinfo/Program.cs` ports CTS `PowerPointConverterBase.cs:599-680`: removes the modification verifier,
strips local-file image relationships, builds `SlideInfo` (hidden = `slide.Show == false`), runs
`LiveDoc.BeforeConversion` (strips placeholder slides), writes `merge-info.xml`.

## Usage

Run from anywhere (do **not** use `python -I`: it ignores the path entries the launcher sets):

```
python tools/livedoc_local/run_livedoc_local.py all  SRDP.zip -w WORK [-o final.pdf] [-i INPUTS_DIR]... \
        [--synthesize-missing] [--allow-page-mismatch] [--force] [--keep] [--json-report r.json]
python tools/livedoc_local/run_livedoc_local.py replay    SRDP.zip -w WORK
python tools/livedoc_local/run_livedoc_local.py mergeinfo -w WORK [--pptx other.pptx] [-i DIR]
python tools/livedoc_local/run_livedoc_local.py render    -w WORK [--lo-mode auto|wsl|native]
python tools/livedoc_local/run_livedoc_local.py merge     -w WORK [-o final.pdf] [-i DIR] [--synthesize-missing]
python tools/livedoc_local/run_livedoc_local.py compare   A.pdf B.pdf [--dpi 50] [--json-report c.json]
```
(`python -m livedoc_local ...` works too with `tools/livedoc_local` as cwd or on `PYTHONPATH`.)

* Without `--workdir`, `all` uses a temp dir that is deleted unless `--keep`; `-o` is then required.
* **pdfmergepy is imported in-process from this checkout's `src/`** (the launcher/stages put it first on
  `sys.path` and verify `pdfmergepy.__file__`), never from a stale pip-editable install. The path used is
  printed in the merge stage details.
* Workdir layout: `replay/engine_out/*.pptx`, `inputs/{blobId}.pdf`, `mergeinfo/{stripped.pptx,merge-info.xml}`,
  `render/main.pdf`, `inputs_resolved/`, `out/final.pdf`, `state.json` (per-stage status/timing/warnings), logs.

### External PDFs

1. If the SRDP contains `InputFiles/{blobId}.pdf` (future opt-in engine feature; `index.xml` gets
   `<ExternalPdfs><Pdf BlobId=.../></ExternalPdfs>`), they are extracted to `inputs/` (safe names only).
2. Otherwise/additionally pass `--inputs-dir DIR` (repeatable) with `{blobId}.pdf` files (case-insensitive;
   SRDP-embedded files win).
3. The merge stage lists every blob id the merge-info references (including `MergedPdfFileId` and
   `partialPDF` blobs). If any is missing it **fails with exit 10 and the list**, unless
   `--synthesize-missing` creates clearly-marked SYNTHETIC placeholder PDFs (pipeline exercising only; a
   warning is always recorded).

### Exit codes

0 ok - 2 usage - 3 missing prerequisite - 4 replay failed - 5 mergeinfo failed - 6 render failed -
7 page-count mismatch (PDF pages != slides - hidden; `--allow-page-mismatch` downgrades to a warning) -
8 merge failed - 9 `compare`: PDFs differ - 10 external PDFs missing - 11 not an engine SRDP
(CTS `srdp.json`, Word, not a zip). The final report lists per-stage status and timing plus every
warning/assumption (missing PDFs, synthesized PDFs, probable LibreOffice font substitution, hidden slides,
no merge info, ...); `--json-report` writes the same as JSON.

## End-to-end example

```
python tools/livedoc_local/mergeinfo/build.py --cts-assemblies C:\project_new\content-transformation-service-v2\src\CTS\assemblies\Release\converters\LiveDoc
python tools/livedoc_local/run_livedoc_local.py all "C:\bak\910ExpiredUnpublish.zip" -w C:\tmp\ld_work ^
        -i C:\test\IText7Test\PdfMerge\inputs -o C:\tmp\final.pdf --json-report C:\tmp\ld_work\report.json
python tools/livedoc_local/run_livedoc_local.py compare C:\tmp\final.pdf <production-output>.pdf
```

## Tests

```
python -m pytest tools/livedoc_local/tests -q        # unit tests, no heavy tools
LIVEDOC_LOCAL_TEST_SRDP=<engine srdp zip> python -m pytest tools/livedoc_local/tests -k end_to_end
```
The end-to-end integration test is skipped unless the engine exe, built MergeInfoTool, dotnet and
LibreOffice are all available.

## Limitations

* LibreOffice fidelity differs from Office/CTS (fonts, text layout, effects); expect raster/text diffs vs production.
  Fonts referenced by the pptx but not found by name in the PDF are reported as probable substitutions (heuristic).
* PowerPoint SRDPs only (`<Engine>` V1/V2 index.xml with a pptx); Word SRDPs and CTS `srdp.json` packages are rejected.
* `mergeinfo` depends on the released CTS `Seismic.CTS.Implements.LiveDocs.dll` behaviour (Office-free port, not byte-identical to CTS).
