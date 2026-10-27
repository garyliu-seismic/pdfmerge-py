#!/usr/bin/env python3
"""Build the Office-free merge-info extractor (MergeInfoTool).

    python build.py --cts-assemblies <...>/src/CTS/assemblies/Release/converters/LiveDoc

The CTS assemblies directory can also be given via env LIVEDOC_CTS_ASSEMBLIES.
Third-party/CTS DLLs are copied into mergeinfo/bin (git-ignored); nothing binary is committed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED = [
    "Seismic.CTS.Implements.LiveDocs.dll",
    "DocumentFormat.OpenXml.dll",
    "DocumentFormat.OpenXml.Framework.dll",
    "Seismic.CTS.Converter.LiveDoc.deps.json",
]
DEFAULT_CTS = (r"C:\project_new\content-transformation-service-v2\src\CTS\assemblies"
               r"\Release\converters\LiveDoc")


def check_assemblies(path: Path) -> list[str]:
    """Return the list of missing required files (empty list == OK)."""
    if not path.is_dir():
        return [f"<directory not found: {path}>"]
    return [n for n in REQUIRED if not (path / n).is_file()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cts-assemblies", default=os.environ.get("LIVEDOC_CTS_ASSEMBLIES"),
                    help="dir with the released CTS LiveDoc converter DLLs (default: env "
                         f"LIVEDOC_CTS_ASSEMBLIES, then {DEFAULT_CTS})")
    ap.add_argument("--configuration", default="Release")
    ap.add_argument("--clean", action="store_true", help="delete bin/ and obj/ first")
    args = ap.parse_args(argv)

    cts = Path(args.cts_assemblies or DEFAULT_CTS)
    missing = check_assemblies(cts)
    if missing:
        print("error: cannot build MergeInfoTool - CTS assemblies path is missing/incomplete:\n"
              f"  path   : {cts}\n  missing: {', '.join(missing)}\n"
              "  Pass --cts-assemblies <dir> or set LIVEDOC_CTS_ASSEMBLIES to the "
              "'.../content-transformation-service-v2/src/CTS/assemblies/Release/converters/LiveDoc' folder.",
              file=sys.stderr)
        return 3
    dotnet = shutil.which("dotnet")
    if not dotnet:
        print("error: 'dotnet' (SDK 8+) not found on PATH", file=sys.stderr)
        return 3
    if args.clean:
        for d in ("bin", "obj"):
            shutil.rmtree(HERE / d, ignore_errors=True)
    cmd = [dotnet, "build", str(HERE / "MergeInfoTool.csproj"), "-c", args.configuration,
           "-o", str(HERE / "bin"), f"-p:CtsAssemblies={cts}", "-nologo", "-v", "q"]
    print("+", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"built: {HERE / 'bin' / 'MergeInfoTool.dll'}")
    return 0 if rc == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
