"""CLI behaviour: output text, exit codes, batch, JSON."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pdfgen import Snapshot, make_corrupt_pdf, make_pdf, page_count
from pdfzi.cli import app

runner = CliRunner()


def run(*args, env=None):
    return runner.invoke(app, [str(a) for a in args], env=env)


def test_info_text_and_json(workdir):
    a = make_pdf(workdir / "รายงาน ประจำปี.pdf", pages=3, bookmarks=True)
    text = run("info", a)
    assert text.exit_code == 0
    assert "Pages:       3" in text.output and "รายงาน ประจำปี.pdf" in text.output
    data = json.loads(run("info", a, "--json").output)
    assert data[0]["ok"] is True and data[0]["info"]["page_count"] == 3


def test_info_encrypted_without_password_is_not_a_failure(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2, password="pw")
    r = run("info", a)
    assert r.exit_code == 0 and "password needed" in r.output
    r = run("info", a, "--password", "pw")
    assert "AES-256" in r.output


def test_glob_is_expanded_by_pdfzi_and_batch_continues_after_a_bad_file(workdir):
    make_pdf(workdir / "a.pdf", pages=3)
    make_corrupt_pdf(workdir / "b.pdf")
    make_pdf(workdir / "c.pdf", pages=3)
    r = run("extract", workdir / "*.pdf", "--pages", "1")
    assert r.exit_code == 1
    assert "[ok] a.pdf -> a-extracted.pdf" in r.output
    assert "[error] b.pdf:" in r.output and "not a valid PDF" in r.output
    assert "[ok] c.pdf -> c-extracted.pdf" in r.output
    assert "Traceback" not in r.output
    # the next run globs again; earlier results are inputs too, so they are processed as well
    assert (workdir / "c-extracted.pdf").exists()


def test_missing_input_is_one_clean_line(workdir):
    r = run("rotate", workdir / "nope.pdf", "--angle", "90")
    assert r.exit_code == 1
    assert r.output.strip() == f"Error: File not found: {workdir / 'nope.pdf'}"


def test_merge_in_given_order_with_output_folder(workdir):
    a = make_pdf(workdir / "a.pdf", pages=1)
    b = make_pdf(workdir / "b.pdf", pages=2)
    r = run("merge", b, a, "-o", str(workdir / "out") + "/")  # trailing slash = a folder
    assert r.exit_code == 0, r.output
    assert "Merged 2 files: b.pdf, a.pdf" in r.output
    assert page_count(workdir / "out" / "b-merged.pdf") == 3


def test_merge_with_one_file_fails_cleanly(workdir):
    r = run("merge", make_pdf(workdir / "a.pdf"))
    assert r.exit_code == 1 and "at least two" in r.output


def test_merge_json(workdir):
    a = make_pdf(workdir / "a.pdf", pages=1)
    b = make_pdf(workdir / "b.pdf", pages=1)
    data = json.loads(run("merge", a, b, "--json").output)
    assert data[0]["ok"] and data[0]["result"]["pages_out"] == 2
    assert len(data[0]["result"]["sources"]) == 2


def test_split_every_and_ranges(workdir):
    a = make_pdf(workdir / "a.pdf", pages=5)
    r = run("split", a, "--every", "2")
    assert r.exit_code == 0 and "3 files" in r.output
    r = run("split", a, "--ranges", "1-2,5", "-o", workdir / "mine")
    assert r.exit_code == 0
    assert sorted(p.name for p in (workdir / "mine").iterdir()) == ["a-p1-2.pdf", "a-p5.pdf"]
    bad = run("split", a, "--every", "2", "--ranges", "1")
    assert bad.exit_code == 1 and "not both" in bad.output


def test_split_several_files_into_one_output_folder(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2)
    b = make_pdf(workdir / "b.pdf", pages=2)
    r = run("split", a, b, "-o", workdir / "o")
    assert r.exit_code == 0
    assert (workdir / "o" / "a-split" / "a-p1.pdf").exists()
    assert (workdir / "o" / "b-split" / "b-p2.pdf").exists()


def test_delete_and_rotate(workdir):
    a = make_pdf(workdir / "a.pdf", pages=4)
    r = run("delete", a, "--pages", "1,3")
    assert r.exit_code == 0 and page_count(workdir / "a-removed.pdf") == 2
    r = run("rotate", a, "--angle", "270", "--pages", "2")
    assert r.exit_code == 0 and "Rotated 1 page(s) by 270" in r.output
    r = run("rotate", a, "--angle", "45")
    assert r.exit_code == 1 and "90, 180 or 270" in r.output
    r = run("delete", a, "--pages", "1-4")
    assert r.exit_code == 1 and "every page" in r.output


def test_force_and_numbering(workdir):
    a = make_pdf(workdir / "a.pdf", pages=3)
    run("extract", a, "--pages", "1")
    run("extract", a, "--pages", "1-2")
    assert page_count(workdir / "a-extracted.pdf") == 1
    assert page_count(workdir / "a-extracted-2.pdf") == 2
    run("extract", a, "--pages", "1-3", "-o", workdir / "a-extracted.pdf", "--force")
    assert page_count(workdir / "a-extracted.pdf") == 3


def test_password_from_option_and_environment(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2, password="pw")
    r = run("extract", a, "--pages", "1")
    assert r.exit_code == 1 and "password-protected" in r.output
    r = run("extract", a, "--pages", "1", "--password", "wrong")
    assert r.exit_code == 1 and "incorrect" in r.output
    r = run("extract", a, "--pages", "1", env={"PDFZI_PASSWORD": "pw"})
    assert r.exit_code == 0, r.output


def test_json_errors_are_reported_per_file(workdir):
    good = make_pdf(workdir / "good.pdf", pages=2)
    bad = make_corrupt_pdf(workdir / "bad.pdf")
    r = run("rotate", good, bad, "--angle", "90", "--json")
    assert r.exit_code == 1
    data = json.loads(r.output)
    assert [e["ok"] for e in data] == [True, False]
    assert "not a valid PDF" in data[1]["error"]


@pytest.mark.parametrize(
    "args",
    [
        ("info",),
        ("merge", "{b}"),
        ("split",),
        ("split", "--every", "2"),
        ("extract", "--pages", "1-2"),
        ("delete", "--pages", "2"),
        ("rotate", "--angle", "90"),
        ("rotate", "--angle", "180", "--pages", "1"),
    ],
)
def test_no_command_modifies_its_inputs(workdir, args):
    a = make_pdf(workdir / "ต้นฉบับ one.pdf", pages=4, bookmarks=True, images_per_page=1)
    b = make_pdf(workdir / "other.pdf", pages=2)
    snaps = [Snapshot(a), Snapshot(b)]
    argv = [a.__str__() if x == "{a}" else b.__str__() if x == "{b}" else x for x in args]
    cmd, *rest = argv
    r = run(cmd, a, *rest)
    assert r.exit_code == 0, r.output
    for s in snaps:
        s.assert_unchanged()
    assert not list(workdir.rglob("*.tmp"))
