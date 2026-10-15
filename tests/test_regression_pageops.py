"""Real-sample regression for select / delete / split / append.

For every PDF in the real inputs folder we run the page operations and require
that, relative to the *source file's own baseline*:

* pdftagvalicate reports no check id as Fail/Error that did not already
  Fail/Error on the source (outputs must not introduce NEW failed check ids);
* our structural integrity checker (tests/tagcheck.py) reports no more problems
  of any category than the source has, and never an orphan page object.

Skipped when ``C:/test/IText7Test/PdfMerge/inputs`` or pdftagvalicate is absent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest

from pdfmergepy import pageops

from tagcheck import tag_problems

INPUTS_DIR = Path("C:/test/IText7Test/PdfMerge/inputs")


def _collect() -> list[Path]:
    if not INPUTS_DIR.exists():
        return []
    seen, out = set(), []
    for p in sorted(INPUTS_DIR.iterdir()):
        if p.suffix.lower() != ".pdf" or p.name.lower() in seen:
            continue
        seen.add(p.name.lower())
        out.append(p)
    return out


def _have_validator() -> bool:
    try:
        import pdftagvalicate  # noqa: F401
        return True
    except ImportError:
        return False


SAMPLES = _collect()
pytestmark = pytest.mark.skipif(
    not SAMPLES or not _have_validator(),
    reason="real samples (C:/test/IText7Test/PdfMerge/inputs) or pdftagvalicate not available",
)


# Checks whose outcome legitimately depends on WHICH pages remain (not on the
# pruning): 09-007 "first heading is level 1" fails when the page holding the
# document's H1 is removed.  Documented, not hidden.
CONTENT_DEPENDENT = {"09-007"}


def failed_ids(pdf_path: Path) -> set[str]:
    r = subprocess.run([sys.executable, "-m", "pdftagvalicate", str(pdf_path), "--validate", "--json"],
                       capture_output=True, text=True)
    raw = r.stderr.strip() or r.stdout.strip()
    data = json.loads(raw)
    return {c["id"] for c in data["checks"] if c["severity"] in ("Fail", "Error")}


def problems(path: Path):
    with pikepdf.open(path) as p:
        return tag_problems(p)


def assert_no_regression(src_fail: set[str], src_prob, out: Path, label: str, extra_fail: set[str] = frozenset()):
    new = failed_ids(out) - src_fail - set(extra_fail) - CONTENT_DEPENDENT
    assert not new, f"{label}: NEW failed check ids vs source: {sorted(new)} (source fails {sorted(src_fail)})"
    out_prob = problems(out)
    assert out_prob["orphan_page_objects"] == 0, f"{label}: orphan page objects in output"
    for cat, n in out_prob.items():
        assert n <= src_prob.get(cat, 0), f"{label}: struct problem '{cat}' {n} > source {src_prob.get(cat, 0)}"


@pytest.mark.parametrize("sample", SAMPLES, ids=[p.stem[:24] for p in SAMPLES])
def test_pageops_do_not_introduce_new_failures(sample: Path, tmp_path: Path):
    n = pageops.page_count(sample)
    src_fail = failed_ids(sample)
    src_prob = problems(sample)

    # select: first page + (if any) the last page + an open-ended tail
    expr = "1" if n == 1 else f"1,{n}-"
    out = tmp_path / "sel.pdf"
    got = pageops.select_pages(sample, expr, out)
    assert got == (1 if n == 1 else 2)
    with pikepdf.open(out) as p:
        assert len(p.pages) == got
    assert_no_regression(src_fail, src_prob, out, f"select({expr})")

    if n >= 2:
        # delete: drop the first page
        out = tmp_path / "del.pdf"
        assert pageops.delete_pages(sample, "1", out) == n - 1
        assert_no_regression(src_fail, src_prob, out, "delete(1)")

        # split: halves; validate every part (cap to 4 parts for runtime)
        half = max(1, n // 2)
        files = pageops.split_pdf(sample, str(half), tmp_path / "parts")
        assert sum(len(pikepdf.open(f).pages) for f in files) == n
        for f in files[:4]:
            assert_no_regression(src_fail, src_prob, f, f"split({half}):{f.name}")

    # append the document with itself: tags/forms preserved through merge engine.
    # Known merge-engine limits are not page-op regressions: only compare id set
    # against the source baseline.
    out = tmp_path / "app.pdf"
    assert pageops.append_files([sample, sample], out) == 2 * n
    assert_no_regression(src_fail, {**{k: v * 2 for k, v in src_prob.items()}}, out, "append(x2)",
                         extra_fail=set())
