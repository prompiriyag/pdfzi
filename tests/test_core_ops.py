"""Core operations: info, merge, split, extract, delete, rotate."""

from __future__ import annotations

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name

from conftest import TRICKY_NAMES
from pdfgen import (
    Snapshot,
    extract_text,
    make_corrupt_pdf,
    make_pdf,
    page_count,
    page_text,
)
from pdfzi.core import subset
from pdfzi.core.batch import run_batch
from pdfzi.core.errors import (
    CorruptPdfError,
    InvalidOptionError,
    OperationCancelledError,
    OutputWriteError,
    PageRangeError,
    PasswordRequiredError,
    WrongPasswordError,
)
from pdfzi.core.info import get_info
from pdfzi.core.merge import merge_pdfs
from pdfzi.core.outline import read_bookmarks
from pdfzi.core.paths import plan_outputs
from pdfzi.core.progress import CancelToken
from pdfzi.core.rotate import rotate_pdf
from pdfzi.core.subset import delete_pages, extract_pages, split_pdf


def texts(path, password=None):
    return [extract_text(path, i, password) for i in range(page_count(path, password))]


def has_page(text, number):
    return page_text(number - 1) in text


def leftovers(folder):
    return [p.name for p in folder.rglob("*.tmp")]


def raw_streams(path) -> bytes:
    """Every decoded stream in the file, to prove removed content is really gone."""
    chunks = []
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    chunks.append(obj.read_bytes())
                except pikepdf.PdfError:
                    chunks.append(obj.read_raw_bytes())
    return b"\n".join(chunks)


# ------------------------------------------------------------------ info


def test_info_basics(workdir):
    p = make_pdf(
        workdir / "ข้อมูล.pdf", pages=3, sizes=[(595, 842)], images_per_page=1,
        share_image=True, bookmarks=True,
    )  # fmt: skip
    with pikepdf.open(p, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = "Hello"
        pdf.save(p)
    info = get_info(p)
    assert (info.page_count, info.encrypted, info.readable) == (3, False, True)
    assert info.image_count == 1  # one image shared by three pages
    assert info.has_bookmarks is True
    assert info.metadata == {"Title": "Hello"}
    assert info.page_sizes[0].name == "A4"
    assert info.size_bytes == p.stat().st_size
    assert info.to_dict()["page_count"] == 3


def test_info_rotated_page_reports_displayed_size(workdir):
    p = make_pdf(workdir / "a.pdf", pages=2, rotations={1: 90})
    info = get_info(p)
    assert info.rotated_pages == 1
    assert {(g.width_pt, g.height_pt) for g in info.page_sizes} == {(612, 792), (792, 612)}


def test_info_encrypted(workdir):
    p = make_pdf(workdir / "a.pdf", pages=2, password="pw")
    locked = get_info(p)
    assert locked.encrypted and not locked.readable and locked.page_count is None
    opened = get_info(p, "pw")
    assert opened.readable and opened.page_count == 2 and opened.encryption == "AES-256 (R6)"
    with pytest.raises(WrongPasswordError):
        get_info(p, "bad")


def test_info_corrupt(workdir):
    with pytest.raises(CorruptPdfError):
        get_info(make_corrupt_pdf(workdir / "bad.pdf"))


# ------------------------------------------------------------------ merge


def test_merge_order_text_and_originals_untouched(workdir):
    a = make_pdf(workdir / "รายงาน ก.pdf", pages=2)
    b = make_pdf(workdir / "my file (b).pdf", pages=3, text_prefix="B-")
    snaps = [Snapshot(a), Snapshot(b)]
    result = merge_pdfs([b, a])
    out = result.outputs[0]
    assert out == workdir / "my file (b)-merged.pdf"
    t = texts(out)
    assert len(t) == 5
    assert t[0].startswith("B-") and t[3].startswith("pdfzi")
    assert (result.pages_in, result.pages_out) == (5, 5)
    for s in snaps:
        s.assert_unchanged()
    assert leftovers(workdir) == []


def test_merge_preserves_bookmarks_with_offsets(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2, bookmarks=True)
    b = make_pdf(workdir / "b.pdf", pages=3, bookmarks=True)
    out = merge_pdfs([a, b]).outputs[0]
    with pikepdf.open(out) as pdf:
        marks = [(n.title, n.page) for n in read_bookmarks(pdf)]
    assert marks == [
        ("Chapter 1", 0), ("Chapter 2", 1), ("Chapter 1", 2), ("Chapter 2", 3), ("Chapter 3", 4),
    ]  # fmt: skip


def test_merge_mixed_sizes_and_rotation_survive(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2, sizes=[(200, 300)], rotations={0: 90})
    b = make_pdf(workdir / "b.pdf", pages=1)
    out = merge_pdfs([a, b]).outputs[0]
    with pikepdf.open(out) as pdf:
        assert [float(p.mediabox[2]) for p in pdf.pages] == [200, 200, 612]
        assert pdf.pages[0].get("/Rotate") == 90


def test_merge_encrypted_input(workdir):
    a = make_pdf(workdir / "a.pdf", pages=1, password="pw")
    b = make_pdf(workdir / "b.pdf", pages=1)
    with pytest.raises(PasswordRequiredError):
        merge_pdfs([a, b])
    assert not (workdir / "a-merged.pdf").exists()
    out = merge_pdfs([a, b], password="pw").outputs[0]
    assert page_count(out) == 2  # result is not encrypted


def test_merge_needs_two_files(workdir):
    with pytest.raises(InvalidOptionError, match="at least two"):
        merge_pdfs([make_pdf(workdir / "a.pdf")])


def test_merge_bad_input_publishes_nothing(workdir):
    a = make_pdf(workdir / "a.pdf")
    bad = make_corrupt_pdf(workdir / "bad.pdf")
    with pytest.raises(CorruptPdfError):
        merge_pdfs([a, bad])
    assert sorted(p.name for p in workdir.iterdir()) == ["a.pdf", "bad.pdf"]


def test_merge_same_file_twice_and_numbering(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2)
    first = merge_pdfs([a, a]).outputs[0]
    second = merge_pdfs([a, a]).outputs[0]
    assert first.name == "a-merged.pdf" and second.name == "a-merged-2.pdf"
    assert page_count(first) == 4


def test_merge_force_can_overwrite_a_source(workdir):
    a = make_pdf(workdir / "a.pdf", pages=2)
    b = make_pdf(workdir / "b.pdf", pages=1)
    out = merge_pdfs([a, b], dest=a, force=True).outputs[0]
    assert out == a and page_count(a) == 3
    assert leftovers(workdir) == []


# ------------------------------------------------------------------ extract / delete


def test_extract_order_and_duplicates(workdir):
    src = make_pdf(workdir / "ต้นฉบับ.pdf", pages=5)
    snap = Snapshot(src)
    out = extract_pages(src, "4,1-2,4").outputs[0]
    t = texts(out)
    assert [has_page(x, n) for x, n in zip(t, (4, 1, 2, 4), strict=True)] == [True] * 4
    snap.assert_unchanged()


def test_extract_open_range_and_out_of_range(workdir):
    src = make_pdf(workdir / "a.pdf", pages=5)
    assert page_count(extract_pages(src, "4-").outputs[0]) == 2
    with pytest.raises(PageRangeError, match="out of range"):
        extract_pages(src, "9")
    assert sorted(p.name for p in workdir.iterdir() if p.suffix == ".pdf") == [
        "a-extracted.pdf",
        "a.pdf",
    ]


def test_extract_and_delete_carry_bookmarks_and_title(workdir):
    src = make_pdf(workdir / "a.pdf", pages=4, bookmarks=True)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = "Keep me"
        pdf.save(src)

    out = extract_pages(src, "3-4").outputs[0]
    with pikepdf.open(out) as pdf:
        assert [(n.title, n.page) for n in read_bookmarks(pdf)] == [
            ("Chapter 3", 0),
            ("Chapter 4", 1),
        ]
        assert str(pdf.docinfo["/Title"]) == "Keep me"

    result = delete_pages(src, "2")
    with pikepdf.open(result.outputs[0]) as pdf:
        assert [(n.title, n.page) for n in read_bookmarks(pdf)] == [
            ("Chapter 1", 0), ("Chapter 3", 1), ("Chapter 4", 2),
        ]  # fmt: skip
    assert "1 of 4" in result.notes[0]


def test_delete_pages_leaves_no_trace_of_removed_content(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3)
    # A link on page 1 pointing at page 2 must not keep page 2 alive in the output.
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        link = Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 100, 100],
            Dest=Array([pdf.pages[1].obj, Name.Fit]),
        )  # fmt: skip
        pdf.pages[0].Annots = pdf.make_indirect(Array([pdf.make_indirect(link)]))
        pdf.save(src)
    assert page_text(1).encode() in raw_streams(src)

    out = delete_pages(src, "2").outputs[0]
    assert page_count(out) == 2
    assert page_text(1).encode() not in raw_streams(out)
    assert page_text(0).encode() in raw_streams(out)


def test_delete_everything_is_refused(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2)
    with pytest.raises(PageRangeError, match="every page"):
        delete_pages(src, "1-2")
    assert not (workdir / "a-removed.pdf").exists()


def test_delete_encrypted_requires_password(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3, password="pw")
    with pytest.raises(PasswordRequiredError):
        delete_pages(src, "1")
    assert page_count(delete_pages(src, "1", password="pw").outputs[0]) == 2


# ------------------------------------------------------------------ split


def test_split_every_page_names_and_contents(workdir):
    src = make_pdf(workdir / "รายงาน ประจำปี.pdf", pages=12)
    snap = Snapshot(src)
    result = split_pdf(src)
    assert len(result.outputs) == 12
    folder = workdir / "รายงาน ประจำปี-split"
    assert result.outputs[0] == folder / "รายงาน ประจำปี-p01.pdf"
    assert result.outputs[-1].name == "รายงาน ประจำปี-p12.pdf"
    for i, out in enumerate(result.outputs):
        assert page_count(out) == 1 and has_page(extract_text(out, 0), i + 1)
    snap.assert_unchanged()
    assert leftovers(workdir) == []


def test_split_every_n_and_ranges(workdir):
    src = make_pdf(workdir / "a.pdf", pages=7)
    by_n = split_pdf(src, every=3)
    assert [page_count(o) for o in by_n.outputs] == [3, 3, 1]
    by_range = split_pdf(src, ranges="1-2,7,4-")
    assert [o.name for o in by_range.outputs] == ["a-p1-2.pdf", "a-p7.pdf", "a-p4-7.pdf"]
    assert [page_count(o) for o in by_range.outputs] == [2, 1, 4]
    assert by_range.outputs[0].parent.name == "a-split-2"  # the first run already took a-split


def test_split_option_errors_leave_nothing_behind(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3)
    with pytest.raises(InvalidOptionError, match="not both"):
        split_pdf(src, every=2, ranges="1")
    with pytest.raises(InvalidOptionError):
        split_pdf(src, every=0)
    with pytest.raises(PageRangeError):
        split_pdf(src, ranges="1-9")
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]


def test_split_cancel_removes_partial_outputs(workdir):
    src = make_pdf(workdir / "a.pdf", pages=6)
    token = CancelToken()

    def progress(p):
        if p.done == 3:
            token.cancel()

    with pytest.raises(OperationCancelledError):
        split_pdf(src, progress=progress, cancel=token)
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]


def test_split_failure_midway_cleans_up(workdir, monkeypatch):
    src = make_pdf(workdir / "a.pdf", pages=4)
    real = subset.publish_verified
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise OutputWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(subset, "publish_verified", flaky)
    with pytest.raises(OutputWriteError):
        split_pdf(src)
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]


def test_split_into_chosen_folder(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2)
    out = workdir / "sub" / "dir"
    result = split_pdf(src, out_dir=out)
    assert {p.parent for p in result.outputs} == {out}


# ------------------------------------------------------------------ rotate


def test_rotate_all_and_relative(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3, rotations={1: 90}, sizes=[(300, 400), (500, 200)])
    snap = Snapshot(src)
    out = rotate_pdf(src, 90).outputs[0]
    with pikepdf.open(out) as pdf:
        assert [int(p.obj.get("/Rotate", 0)) for p in pdf.pages] == [90, 180, 90]
        assert [float(p.mediabox[2]) for p in pdf.pages] == [300, 500, 300]
    assert all(has_page(t, i + 1) for i, t in enumerate(texts(out)))
    snap.assert_unchanged()


def test_rotate_selected_pages_and_negative_angle(workdir):
    src = make_pdf(workdir / "a.pdf", pages=4)
    out = rotate_pdf(src, -90, "2,4").outputs[0]
    with pikepdf.open(out) as pdf:
        assert [int(p.obj.get("/Rotate", 0)) for p in pdf.pages] == [0, 270, 0, 270]


@pytest.mark.parametrize("angle", [0, 45, 360, 91])
def test_rotate_rejects_bad_angles(workdir, angle):
    with pytest.raises(InvalidOptionError, match="90, 180 or 270"):
        rotate_pdf(make_pdf(workdir / "a.pdf"), angle)


def test_rotate_encrypted(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2, password="pw")
    with pytest.raises(PasswordRequiredError):
        rotate_pdf(src, 90)
    assert page_count(rotate_pdf(src, 90, password="pw").outputs[0]) == 2


# ------------------------------------------------------------------ naming, force, unicode, batch


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_every_operation_handles_tricky_names(workdir, name):
    src = make_pdf(workdir / name, pages=4)
    snap = Snapshot(src)
    assert page_count(extract_pages(src, "1-2").outputs[0]) == 2
    assert page_count(delete_pages(src, "1").outputs[0]) == 3
    assert page_count(rotate_pdf(src, 180).outputs[0]) == 4
    assert len(split_pdf(src, every=2).outputs) == 2
    other = make_pdf(workdir / ("2-" + name), pages=1)
    assert page_count(merge_pdfs([src, other]).outputs[0]) == 5
    snap.assert_unchanged()


def test_second_run_numbers_instead_of_overwriting(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3)
    first = extract_pages(src, "1").outputs[0]
    snap = Snapshot(first)
    second = extract_pages(src, "2").outputs[0]
    assert (first.name, second.name) == ("a-extracted.pdf", "a-extracted-2.pdf")
    snap.assert_unchanged()


def test_force_overwrites_result_but_never_without_it(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3)
    first = extract_pages(src, "1").outputs[0]
    again = extract_pages(src, "1-2", dest=first, force=True).outputs[0]
    assert again == first and page_count(first) == 2


def test_force_can_overwrite_the_original_itself(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3)
    assert (
        extract_pages(src, "1-2", dest=src).outputs[0].name == "a-2.pdf"
    )  # without force: numbered
    assert page_count(src) == 3
    extract_pages(src, "1-2", dest=src, force=True)
    assert page_count(src) == 2
    rotate_pdf(src, 90, dest=src, force=True)
    delete_pages(src, "1", dest=src, force=True)
    assert page_count(src) == 1
    assert leftovers(workdir) == []


def test_plan_outputs_rules(workdir):
    a, b = workdir / "a.pdf", workdir / "b.pdf"
    assert plan_outputs([a, b], "x", None) == [workdir / "a-x.pdf", workdir / "b-x.pdf"]
    out = workdir / "out"
    assert plan_outputs([a, b], "x", str(out)) == [out / "a-x.pdf", out / "b-x.pdf"]
    assert plan_outputs([a], "x", str(workdir / "r.pdf")) == [workdir / "r.pdf"]
    with pytest.raises(InvalidOptionError, match="must be a folder"):
        plan_outputs([a, b], "x", str(workdir / "r.pdf"))


def test_run_batch_continues_after_a_bad_file(workdir):
    good = make_pdf(workdir / "good.pdf", pages=2)
    bad = make_corrupt_pdf(workdir / "bad.pdf")
    good2 = make_pdf(workdir / "good2.pdf", pages=2)
    entries = run_batch([good, bad, good2], lambda s, i: rotate_pdf(s, 90))
    assert [e.ok for e in entries] == [True, False, True]
    assert "not a valid PDF" in entries[1].error
    assert entries[1].to_dict()["result"] is None


def test_run_batch_cancel_stops_early(workdir):
    files = [make_pdf(workdir / f"{i}.pdf") for i in range(3)]
    token = CancelToken()

    def func(source, i):
        result = rotate_pdf(source, 90)
        token.cancel()
        return result

    entries = run_batch(files, func, cancel=token)
    assert [e.ok for e in entries] == [True, False]
    assert entries[1].error == "Cancelled."


def test_corrupt_pdf_in_every_operation_gives_friendly_error(workdir):
    bad = make_corrupt_pdf(workdir / "bad.pdf")
    ops = [
        lambda: extract_pages(bad, "1"),
        lambda: delete_pages(bad, "1"),
        lambda: rotate_pdf(bad, 90),
        lambda: split_pdf(bad),
        lambda: merge_pdfs([bad, bad]),
    ]
    for op in ops:
        with pytest.raises(CorruptPdfError):
            op()
    assert [p.name for p in workdir.iterdir()] == ["bad.pdf"]
