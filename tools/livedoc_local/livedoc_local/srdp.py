"""SRDP (engine replay package) parsing and external-PDF extraction.

SRDPs are untrusted: only specific, validated entries are ever read or written; archive
paths are never used to build output paths directly.
"""
from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .core import EXIT_BAD_SRDP, StageError

_BLOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PDF_BYTES = 2 * 1024**3
INPUT_PREFIX = "InputFiles/"


@dataclass
class SrdpInfo:
    path: Path
    engine: str | None = None                       # "V1" / "V2" from index.xml <Engine>
    entries: list[str] = field(default_factory=list)
    main_files: list[str] = field(default_factory=list)   # *.pptx/*.docx/*.xlsx at any level
    embedded_pdfs: dict[str, str] = field(default_factory=dict)   # blobId -> zip entry name
    declared_pdfs: list[str] = field(default_factory=list)        # blob ids from <ExternalPdfs>
    warnings: list[str] = field(default_factory=list)

    @property
    def missing_declared(self) -> list[str]:
        """Declared in index.xml but no InputFiles/{id}.pdf entry present."""
        have = {k.lower() for k in self.embedded_pdfs}
        return [b for b in self.declared_pdfs if b.lower() not in have]


def safe_blob_id(blob_id: str) -> bool:
    return bool(_BLOB_RE.match(blob_id)) and ".." not in blob_id


def parse_index_xml(xml_text: str) -> tuple[str | None, list[str]]:
    """Return (engine, declared external pdf blob ids) from an engine index.xml."""
    try:
        root = ET.fromstring(xml_text.lstrip("﻿"))
    except ET.ParseError as e:
        raise StageError(f"index.xml is not valid XML: {e}", EXIT_BAD_SRDP)
    engine = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "Engine" and el.text:
            engine = el.text.strip()
            break
    declared = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "Pdf" and el.get("BlobId"):
            declared.append(el.get("BlobId"))
    return engine, declared


def inspect_srdp(path: Path) -> SrdpInfo:
    """Validate that `path` is an engine SRDP zip (index.xml) and describe it."""
    path = Path(path)
    if not path.is_file():
        raise StageError(f"SRDP not found: {path}", EXIT_BAD_SRDP)
    if not zipfile.is_zipfile(path):
        raise StageError(f"not a zip file: {path}", EXIT_BAD_SRDP)
    info = SrdpInfo(path=path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        info.entries = names
        low = [n.lower() for n in names]
        if "index.xml" not in low:
            hint = (" (looks like a CTS srdp.json package - this tool replays ENGINE SRDPs only)"
                    if "srdp.json" in low else "")
            raise StageError(f"no index.xml in {path.name}: not an engine SRDP{hint}", EXIT_BAD_SRDP)
        idx_name = names[low.index("index.xml")]
        info.engine, info.declared_pdfs = parse_index_xml(z.read(idx_name).decode("utf-8-sig", "replace"))
        for n in names:
            ln = n.lower()
            if ln.endswith((".pptx", ".docx", ".xlsx")):
                info.main_files.append(n)
            if ln.startswith(INPUT_PREFIX.lower()) and ln.endswith(".pdf") and not n.endswith("/"):
                blob = n[len(INPUT_PREFIX):-4]
                if "/" in blob or not safe_blob_id(blob):
                    info.warnings.append(f"ignored suspicious InputFiles entry name: {n!r}")
                    continue
                info.embedded_pdfs[blob] = n
    if info.engine not in ("V1", "V2"):
        info.warnings.append(f"index.xml <Engine> is {info.engine!r} (expected V1 or V2)")
    if any(m.lower().endswith(".docx") for m in info.main_files) and \
            not any(m.lower().endswith(".pptx") for m in info.main_files):
        raise StageError(f"{path.name} is a Word SRDP; this tool handles PowerPoint SRDPs only", EXIT_BAD_SRDP)
    if info.missing_declared:
        info.warnings.append("index.xml <ExternalPdfs> declares blob ids with no InputFiles/ entry: "
                             + ", ".join(info.missing_declared))
    return info


def extract_embedded_pdfs(info: SrdpInfo, dest: Path) -> list[str]:
    """Extract InputFiles/{blobId}.pdf -> dest/{blobId}.pdf. Returns extracted blob ids."""
    out: list[str] = []
    if not info.embedded_pdfs:
        return out
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(info.path) as z:
        for blob, entry in sorted(info.embedded_pdfs.items()):
            zi = z.getinfo(entry)
            if zi.file_size > MAX_PDF_BYTES:
                info.warnings.append(f"skipped {entry}: {zi.file_size} bytes exceeds limit")
                continue
            target = dest / f"{blob}.pdf"
            with z.open(zi) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(blob)
    return out


def find_pdf_case_insensitive(directory: Path, blob_id: str) -> Path | None:
    want = f"{blob_id}.pdf".lower()
    try:
        for p in directory.iterdir():
            if p.is_file() and p.name.lower() == want:
                return p
    except OSError:
        pass
    return None
