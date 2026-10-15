"""select / delete / split / append / page-count on synthetic tagged PDFs."""
from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdfmergepy import pageops
from pdfmergepy.cli import main
from pdfmergepy.pageops import PdfInputError
from pdfmergepy.pageranges import PageRangeError
from pdfmergepy.prune import keep_only_pages

from pdfbuilder import build_pdf, page_text
from tagcheck import acroform_field_names, count_orphan_pages, tag_problems


@pytest.fixture
def src(tmp_path) -> Path:
    return build_pdf(tmp_path / "src.pdf", n=6)


def texts(path) -> list[str]:
    with pikepdf.open(path) as p:
        return [page_text(pg).split("(")[1].split(")")[0] for pg in p.pages]


def no_tag_problems(path):
    with pikepdf.open(path) as p:
        bad = tag_problems(p)
    assert sum(bad.values()) == 0, dict(bad)


# ----------------------------------------------------------------- baseline
def test_synthetic_source_is_clean(src):
    no_tag_problems(src)


def test_page_count(src):
    assert pageops.page_count(src) == 6


# ------------------------------------------------------------- select/delete
def test_select_pages_and_content(src, tmp_path):
    out = tmp_path / "o.pdf"
    n = pageops.select_pages(src, "1,3,5-", out)
    assert n == 4
    assert texts(out) == ["Page 1", "Page 3", "Page 5", "Page 6"]
    no_tag_problems(out)


def test_delete_pages_and_content(src, tmp_path):
    out = tmp_path / "o.pdf"
    assert pageops.delete_pages(src, "2-3,6", out) == 3
    assert texts(out) == ["Page 1", "Page 4", "Page 5"]
    no_tag_problems(out)


def test_select_all_is_valid(src, tmp_path):
    out = tmp_path / "o.pdf"
    assert pageops.select_pages(src, "1-", out) == 6
    no_tag_problems(out)


def test_select_out_of_range_ignored_and_empty_is_error(src, tmp_path):
    out = tmp_path / "o.pdf"
    assert pageops.select_pages(src, "2,50", out) == 1
    with pytest.raises(ValueError, match="empty document"):
        pageops.select_pages(src, "50-", out)
    with pytest.raises(ValueError, match="empty document"):
        pageops.delete_pages(src, "1-", out)


def test_bad_expression_error(src, tmp_path):
    with pytest.raises(PageRangeError):
        pageops.select_pages(src, "x", tmp_path / "o.pdf")


def test_output_same_as_input_rejected(src):
    with pytest.raises(ValueError, match="differ"):
        pageops.select_pages(src, "1", src)


def test_output_is_pdf_1_7_with_metadata(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "2-3", out)
    with pikepdf.open(out) as p:
        assert p.pdf_version == "1.7"
        assert str(p.Root.Lang) == "en-US"
        assert str(p.docinfo.Title) == "Synthetic Title"
        assert bool(p.Root.ViewerPreferences.DisplayDocTitle) is True
        assert bool(p.Root.MarkInfo.Marked) is True
        with p.open_metadata() as m:
            assert m["pdfuaid:part"] == "1"


def test_untagged_source_stays_untagged(tmp_path):
    s = build_pdf(tmp_path / "u.pdf", n=3, tagged=False, forms=False)
    out = tmp_path / "o.pdf"
    pageops.select_pages(s, "1,3", out)
    with pikepdf.open(out) as p:
        assert "/StructTreeRoot" not in p.Root
        assert len(p.pages) == 2
        assert bool(p.Root.ViewerPreferences.DisplayDocTitle) is True


# ----------------------------------------------------------- struct integrity
def test_struct_tree_pruned_not_just_hidden(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "2", out)
    with pikepdf.open(out) as p:
        doc = p.Root.StructTreeRoot.K
        assert len(doc.K) == 1                       # only the Sect of page 2 remains
        nums = list(p.Root.StructTreeRoot.ParentTree.Nums)
        keys = sorted(int(k) for k in nums[0::2])
        assert keys == sorted([int(p.pages[0].StructParents), int(p.pages[0].Annots[0].StructParent)])
        assert count_orphan_pages(p) == 0


def test_every_single_page_selection_is_consistent(src, tmp_path):
    for i in range(1, 7):
        out = tmp_path / f"o{i}.pdf"
        pageops.select_pages(src, str(i), out)
        no_tag_problems(out)
        assert texts(out) == [f"Page {i}"]


def test_every_single_page_deletion_is_consistent(src, tmp_path):
    for i in range(1, 7):
        out = tmp_path / f"d{i}.pdf"
        pageops.delete_pages(src, str(i), out)
        no_tag_problems(out)
        assert len(texts(out)) == 5


def test_element_spanning_pages_loses_only_removed_kid(tmp_path):
    s = tmp_path / "s.pdf"
    build_pdf(s, n=2, forms=False, outlines=False)
    with pikepdf.open(s, allow_overwriting_input=True) as p:
        sect0 = p.Root.StructTreeRoot.K.K[0]
        p1 = sect0.K[0]
        extra = pikepdf.Dictionary(Type=pikepdf.Name.MCR, Pg=p.pages[1].obj, MCID=0)
        p1.K = pikepdf.Array([0, extra])
        p.save(s)
    out = tmp_path / "o.pdf"
    pageops.select_pages(s, "1", out)
    with pikepdf.open(out) as p:
        k = list(p.Root.StructTreeRoot.K.K[0].K[0].K)
        assert len(k) == 1 and int(k[0]) == 0
        assert sum(tag_problems(p).values()) == 0


# --------------------------------------------------------------------- forms
def test_acroform_fields_survive_only_for_kept_pages(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "2,3", out)
    with pikepdf.open(out) as p:
        assert sorted(acroform_field_names(p)) == ["f1", "f2"]      # 'shared' (pages 1 & 6) gone
        assert len(p.Root.AcroForm.Fields) == 2


def test_shared_field_keeps_only_kept_widgets(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "1,2", out)
    with pikepdf.open(out) as p:
        assert sorted(acroform_field_names(p)) == ["f0", "f1", "shared"]
        shared = [f for f in p.Root.AcroForm.Fields if "/Kids" in f][0]
        assert len(shared.Kids) == 1
        assert shared.Kids[0].P.objgen == p.pages[0].obj.objgen


def test_acroform_removed_when_no_fields_left(tmp_path):
    s = build_pdf(tmp_path / "s.pdf", n=3)
    with pikepdf.open(s, allow_overwriting_input=True) as p:
        p.Root.AcroForm.Fields = pikepdf.Array([p.Root.AcroForm.Fields[0]])    # only f0 (page 1)
        p.save(s)
    out = tmp_path / "o.pdf"
    pageops.select_pages(s, "2-3", out)
    with pikepdf.open(out) as p:
        assert "/AcroForm" not in p.Root


def test_field_values_preserved(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "4", out)
    with pikepdf.open(out) as p:
        assert str(p.Root.AcroForm.Fields[0].V) == "val3"


# ------------------------------------------------- outlines / dests / labels
def walk_outline(p):
    """Validate the outline linked structure and return item titles in order."""
    root = p.Root.get("/Outlines")
    pages = {pg.obj.objgen for pg in p.pages}
    titles = []

    def rec(item):
        total = 0
        prev = None
        cur = item.get("/First")
        first = cur
        last = None
        while cur is not None:
            titles.append(str(cur.Title))
            d = cur.get("/Dest")
            if d is not None:
                assert d[0].objgen in pages, "dangling outline dest"
            assert cur.Parent.objgen == item.objgen
            if prev is None:
                assert "/Prev" not in cur
            else:
                assert cur.Prev.objgen == prev.objgen
            sub = rec(cur)
            if "/Count" in cur:
                assert abs(int(cur.Count)) == sub
            else:
                assert sub == 0
            total += 1 + (sub if int(cur.get("/Count", 0)) > 0 else 0)
            prev, last = cur, cur
            cur = cur.get("/Next")
        if first is not None:
            assert item.First.objgen == first.objgen and item.Last.objgen == last.objgen
        return total

    if root is not None:
        assert int(root.get("/Count", 0)) == rec(root)
    return titles


def test_outlines_pruned_and_relinked(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "1,3,4", out)
    with pikepdf.open(out) as p:
        titles = walk_outline(p)
    # Item0->p1 kept (Sub0->p2 removed); Item2->p3 with Sub2->p4; Item3->p4
    assert titles == ["Item 0", "Item 2", "Sub 2", "Item 3"]


def test_outline_container_kept_when_only_child_survives(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "2", out)
    with pikepdf.open(out) as p:
        assert walk_outline(p) == ["Item 0", "Sub 0", "Item 1"]
        assert "/Dest" not in p.Root.Outlines.First       # destination dropped, container kept


def test_outlines_single_item(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "6", out)
    with pikepdf.open(out) as p:
        assert walk_outline(p) == ["Item 4", "Sub 4", "Item 5"]   # Sub 4 -> page 6


def test_named_dests_and_link_dests_pruned(src, tmp_path):
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "1,2", out)
    with pikepdf.open(out) as p:
        names = [str(k) for k in list(p.Root.Names.Dests.Names)[0::2]]
        assert names == ["p0", "p1"]
        l0 = p.pages[0].Annots[0]
        l1 = p.pages[1].Annots[0]
        assert l0.Dest[0].objgen == p.pages[1].obj.objgen      # still valid
        assert "/Dest" not in l1                                # pointed at removed page 3
        assert count_orphan_pages(p) == 0


def test_page_labels_keep_displayed_labels(src, tmp_path):
    with pikepdf.open(src) as p:
        before = [pg.label for pg in p.pages]
    assert before[:4] == ["i", "ii", "1", "2"]
    out = tmp_path / "o.pdf"
    pageops.select_pages(src, "2,3,5", out)
    with pikepdf.open(out) as p:
        assert [pg.label for pg in p.pages] == [before[1], before[2], before[4]]


# --------------------------------------------------------------------- split
def test_split_naming_remainder_and_content(src, tmp_path):
    outdir = tmp_path / "parts"
    files = pageops.split_pdf(src, "2,3", outdir)
    assert [f.name for f in files] == ["0000.pdf", "0001.pdf", "0002.pdf"]
    assert [texts(f) for f in files] == [["Page 1", "Page 2"], ["Page 3", "Page 4", "Page 5"], ["Page 6"]]
    for f in files:
        no_tag_problems(f)


def test_split_stops_when_pages_exhausted(src, tmp_path):
    files = pageops.split_pdf(src, "4,10,3", tmp_path / "p")
    assert [len(texts(f)) for f in files] == [4, 2]


def test_split_exact_fit_has_no_remainder_file(src, tmp_path):
    files = pageops.split_pdf(src, "3,3", tmp_path / "p")
    assert [len(texts(f)) for f in files] == [3, 3]


def test_split_empty_spec_is_single_file(src, tmp_path):
    files = pageops.split_pdf(src, "", tmp_path / "p")
    assert [f.name for f in files] == ["0000.pdf"] and len(texts(files[0])) == 6


def test_split_rejects_bad_counts(src, tmp_path):
    with pytest.raises(PageRangeError):
        pageops.split_pdf(src, "3,0", tmp_path / "p")
    with pytest.raises(PageRangeError):
        pageops.split_pdf(src, "a", tmp_path / "p")


def test_split_parts_cover_every_form_field(src, tmp_path):
    files = pageops.split_pdf(src, "2,2", tmp_path / "p")
    seen = []
    for f in files:
        with pikepdf.open(f) as p:
            seen += acroform_field_names(p)
    assert sorted(x for x in seen if x != "shared") == [f"f{i}" for i in range(6)]
    assert seen.count("shared") == 2          # first and last widget land in different parts


# -------------------------------------------------------------------- append
def _no_dangling(p):
    bad = tag_problems(p)
    for k in ("dangling_pg", "dangling_elem_pg", "dangling_mcr_pg", "dangling_objr_pg", "orphan_page_objects"):
        assert bad[k] == 0, dict(bad)


def test_append_preserves_tags_and_forms(tmp_path):
    a = build_pdf(tmp_path / "a.pdf", n=2, text_prefix="A")
    b = build_pdf(tmp_path / "b.pdf", n=3, text_prefix="B")
    out = tmp_path / "o.pdf"
    assert pageops.append_files([a, b], out) == 5
    assert texts(out) == ["A 1", "A 2", "B 1", "B 2", "B 3"]
    with pikepdf.open(out) as p:
        assert "/StructTreeRoot" in p.Root
        assert len(p.Root.AcroForm.Fields) >= 5
        _no_dangling(p)


def test_append_then_select_roundtrip(tmp_path):
    a = build_pdf(tmp_path / "a.pdf", n=2, text_prefix="A")
    b = build_pdf(tmp_path / "b.pdf", n=2, text_prefix="B")
    app = tmp_path / "ab.pdf"
    pageops.append_files([a, b], app)
    out = tmp_path / "o.pdf"
    pageops.select_pages(app, "2-3", out)
    assert texts(out) == ["A 2", "B 1"]
    with pikepdf.open(out) as p:
        _no_dangling(p)


def test_append_output_same_as_input_rejected(tmp_path):
    a = build_pdf(tmp_path / "a.pdf", n=1)
    with pytest.raises(ValueError, match="differ"):
        pageops.append_files([a], a)


# ------------------------------------------------------------ bad input
def test_encrypted_input_gives_clear_error(src, tmp_path):
    enc = tmp_path / "enc.pdf"
    with pikepdf.open(src) as p:
        p.save(enc, encryption=pikepdf.Encryption(user="secret", owner="owner"))
    with pytest.raises(PdfInputError, match="encrypted"):
        pageops.page_count(enc)
    with pytest.raises(PdfInputError, match="encrypted"):
        pageops.select_pages(enc, "1", tmp_path / "o.pdf")
    assert pageops.select_pages(enc, "1-2", tmp_path / "o.pdf", password="secret") == 2
    with pikepdf.open(tmp_path / "o.pdf") as p:
        assert not p.is_encrypted


def test_owner_only_encrypted_opens(src, tmp_path):
    enc = tmp_path / "enc.pdf"
    with pikepdf.open(src) as p:
        p.save(enc, encryption=pikepdf.Encryption(user="", owner="owner"))
    assert pageops.page_count(enc) == 6


def test_malformed_input_gives_clear_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    with pytest.raises(PdfInputError, match="not a valid"):
        pageops.page_count(bad)
    with pytest.raises(PdfInputError):
        pageops.split_pdf(bad, "1", tmp_path / "p")


def test_missing_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        pageops.page_count(tmp_path / "nope.pdf")


# ----------------------------------------------------------------------- CLI
def test_cli_commands(src, tmp_path, capsys):
    out = tmp_path / "o.pdf"
    assert main(["page-count", str(src)]) == 0
    assert capsys.readouterr().out.strip() == "6"
    assert main(["select", str(src), "--pages", "1,3,6-8,10-", "-o", str(out)]) == 0
    assert texts(out) == ["Page 1", "Page 3", "Page 6"]
    assert main(["delete", str(src), "--pages", "1-5", "-o", str(out)]) == 0
    assert texts(out) == ["Page 6"]
    assert main(["split", str(src), "--pages", "5", "-o", str(tmp_path / "sp")]) == 0
    assert (tmp_path / "sp" / "0001.pdf").exists()
    assert main(["append", str(src), str(src), "-o", str(out)]) == 0
    assert len(texts(out)) == 12


def test_cli_errors_are_clean(src, tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"junk")
    assert main(["page-count", str(bad)]) == 1
    assert "error:" in capsys.readouterr().err
    assert main(["select", str(src), "--pages", "zzz", "-o", str(tmp_path / "o.pdf")]) == 1
    assert main(["select", str(tmp_path / "missing.pdf"), "--pages", "1", "-o", str(tmp_path / "o.pdf")]) == 1
    enc = tmp_path / "enc.pdf"
    with pikepdf.open(src) as p:
        p.save(enc, encryption=pikepdf.Encryption(user="u", owner="o"))
    assert main(["page-count", str(enc)]) == 1
    assert "encrypted" in capsys.readouterr().err


# ------------------------------------------------------ keep_only_pages API
def test_keep_only_pages_stats(src):
    with pikepdf.open(src) as p:
        st = keep_only_pages(p, [0, 1])
        assert st["removed_pages"] == 4 and st["removed_struct_elems"] > 0
        assert len(p.pages) == 2


def test_append_untagged_source_leaves_no_dangling_structparents(tmp_path):
    a = build_pdf(tmp_path / "a.pdf", n=2, text_prefix="A")
    b = build_pdf(tmp_path / "b.pdf", n=2, text_prefix="B")
    with pikepdf.open(b, allow_overwriting_input=True) as p:     # stale /StructParents, no tag tree
        del p.Root["/StructTreeRoot"]
        p.save(b)
    out = tmp_path / "o.pdf"
    pageops.append_files([a, b], out)
    with pikepdf.open(out) as p:
        assert tag_problems(p)["page_structparents_missing_key"] == 0
        assert all("/StructParents" not in pg.obj for pg in list(p.pages)[2:])
