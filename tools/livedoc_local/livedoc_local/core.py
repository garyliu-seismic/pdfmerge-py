"""Shared plumbing: exit codes, workdir layout, persisted stage state, report."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---- exit codes --------------------------------------------------------------
EXIT_OK = 0
EXIT_USAGE = 2            # argparse default
EXIT_PREREQ = 3           # missing tool / path (engine exe, MergeInfoTool, soffice, ...)
EXIT_REPLAY = 4
EXIT_MERGEINFO = 5
EXIT_RENDER = 6
EXIT_PAGE_MISMATCH = 7    # LibreOffice PDF pages != slides - hidden
EXIT_MERGE = 8
EXIT_COMPARE_DIFF = 9     # compare: PDFs differ
EXIT_MISSING_INPUTS = 10  # merge-info references external PDFs that are not available
EXIT_BAD_SRDP = 11        # not an engine SRDP (e.g. CTS srdp.json) / unsupported

STAGES = ("replay", "mergeinfo", "render", "merge")


class StageError(Exception):
    """A stage failed; carries the process exit code and a diagnosis."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class StageResult:
    name: str
    status: str = "not-run"        # ok | cached | failed | not-run
    seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "status": self.status, "seconds": round(self.seconds, 3),
            "details": self.details, "warnings": self.warnings, "error": self.error,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StageResult":
        return cls(d["name"], d.get("status", "not-run"), d.get("seconds", 0.0), d.get("details", {}),
                   d.get("warnings", []), d.get("error"), d.get("fingerprint", ""))


class Workdir:
    """On-disk layout (all intermediates are kept so stages can be re-run / inspected):

    replay/engine_out/   engine output (<name>_V2_output_<ts>.pptx), replay.log
    inputs/              {blobId}.pdf extracted from the SRDP (InputFiles/)
    mergeinfo/           stripped.pptx, merge-info.xml, mergeinfo.log
    render/              main.pdf, render.log
    inputs_resolved/     the exact {blobId}.pdf set handed to the merge
    out/                 final.pdf (default output)
    state.json           per-stage status/timing/warnings (drives caching + the report)
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.engine_out = self.root / "replay" / "engine_out"
        self.replay_log = self.root / "replay" / "replay.log"
        self.inputs = self.root / "inputs"
        self.mergeinfo_dir = self.root / "mergeinfo"
        self.stripped = self.mergeinfo_dir / "stripped.pptx"
        self.merge_info = self.mergeinfo_dir / "merge-info.xml"
        self.render_dir = self.root / "render"
        self.main_pdf = self.render_dir / "main.pdf"
        self.inputs_resolved = self.root / "inputs_resolved"
        self.out_dir = self.root / "out"
        self.final_pdf = self.out_dir / "final.pdf"
        self.state_path = self.root / "state.json"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- state ----
    def load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"meta": {}, "stages": {}}

    def save_state(self, state: dict[str, Any]) -> None:
        self.ensure()
        self.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def fingerprint(*parts: Any) -> str:
    """Stable short hash of strings and (path -> size, mtime_ns) of existing files."""
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, Path):
            try:
                st = p.stat()
                h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}".encode())
            except OSError:
                h.update(f"{p.name}|missing".encode())
        else:
            h.update(str(p).encode())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def run_stage(wd: Workdir, name: str, fn: Callable[[StageResult], None], *, fp: str,
              outputs: list[Path], force: bool, log: Callable[[str], None]) -> StageResult:
    """Run `fn(result)` unless a valid cached result exists (same fingerprint, outputs present).

    Persists the result in state.json whether it succeeds or fails.
    """
    state = wd.load_state()
    prev = state["stages"].get(name)
    if prev and not force and prev.get("status") in ("ok", "cached") and prev.get("fingerprint") == fp \
            and all(o.exists() for o in outputs):
        res = StageResult.from_dict(prev)
        res.status = "cached"
        log(f"[{name}] cached (use --force to re-run)")
        return res
    res = StageResult(name, fingerprint=fp)
    t0 = time.perf_counter()
    try:
        fn(res)
        res.status = "ok"
    except StageError as e:
        res.status = "failed"
        res.error = str(e)
        res.details["exit_code"] = e.exit_code
        raise
    finally:
        res.seconds = time.perf_counter() - t0
        state = wd.load_state()
        state["stages"][name] = res.to_dict()
        # downstream stages are stale once an upstream stage re-ran
        for later in STAGES[STAGES.index(name) + 1:]:
            state["stages"].pop(later, None)
        wd.save_state(state)
    return res


def build_report(wd: Workdir, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    state = wd.load_state()
    stages = [state["stages"][s] for s in STAGES if s in state["stages"]]
    warnings: list[str] = []
    for s in stages:
        warnings += [f"[{s['name']}] {w}" for w in s.get("warnings", [])]
    rep = {
        "workdir": str(wd.root),
        "meta": state.get("meta", {}),
        "stages": stages,
        "total_seconds": round(sum(s["seconds"] for s in stages), 3),
        "warnings": warnings,
        "ok": bool(stages) and all(s["status"] in ("ok", "cached") for s in stages),
    }
    if extra:
        rep.update(extra)
    return rep


def format_report(rep: dict[str, Any]) -> str:
    lines = ["", "=== livedoc_local report ===", f"workdir: {rep['workdir']}"]
    for k, v in rep.get("meta", {}).items():
        lines.append(f"{k}: {v}")
    for s in rep["stages"]:
        lines.append(f"- {s['name']:<10} {s['status']:<7} {s['seconds']:7.2f}s  "
                     + ", ".join(f"{k}={v}" for k, v in s["details"].items()
                                 if not isinstance(v, (list, dict)) and k != "exit_code" and len(str(v)) <= 60))
        if s.get("error"):
            lines.append(f"    ERROR: {s['error']}")
    lines.append(f"total: {rep['total_seconds']:.2f}s   result: {'OK' if rep['ok'] else 'NOT OK'}")
    if rep["warnings"]:
        lines.append(f"warnings/assumptions ({len(rep['warnings'])}):")
        lines += [f"  * {w}" for w in rep["warnings"]]
    else:
        lines.append("warnings/assumptions: none")
    return "\n".join(lines)
