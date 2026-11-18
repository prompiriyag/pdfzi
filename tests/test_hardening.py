"""Hostile or unusual input: content leaking between pages, terminal control codes in names
and metadata, metadata that ``clean`` must remove, and files crafted to crash or exhaust."""

from __future__ import annotations

import io
import json
import sys
import zlib
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String
from PIL import Image
from typer.testing import CliRunner

from pdfgen import make_pdf, mean_abs_diff, page_count, render_page
from pdfzi.cli import app
from pdfzi.core.batch import BatchEntry
from pdfzi.core.clean import _same_picture, clean_pdf
from pdfzi.core.compress import _check_inflated_size, compress_pdf
from pdfzi.core.imgtopdf import InvalidImageError, images_to_pdf
from pdfzi.core.info import PdfInfo, format_info_lines
from pdfzi.core.pdfimages import iter_images
from pdfzi.core.results import OperationResult
from pdfzi.core.subset import delete_pages, extract_pages, split_pdf
from pdfzi.core.textsafe import escape_json_controls, printable
from pdfzi.tui.screens.result import Outcome, describe, headline
from pdfzi.tui.tools import TOOLS

SECRET = "TOPSECRET-4471"
# Window title (OSC 0) and clipboard write (OSC 52), as a crafted title would carry them.
EVIL = "Report\x1b]0;owned\x07\x1b]52;c;ZWNobyBvd25lZA==\x1b\\\x9b2J"


def everything_in(path: Path) -> bytes:
    """Every object in the file (streams decoded) as one blob, to search for leftovers."""
    chunks = []
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    chunks.append(obj.read_bytes())
                except pikepdf.PdfError:
                    chunks.append(obj.read_raw_bytes())
            chunks.append(repr(obj).encode("utf-8", "replace"))
    return b"\n".join(chunks)


# ---------------------------------------------------------------- 1. removed pages leave no trace


def _annot(pdf, page, **entries):
    annot = pdf.make_indirect(Dictionary(Type=Name.Annot, Rect=[50, 50, 250, 80], **entries))
    annot.P = page.obj
    annots = page.obj.get("/Annots")
    if annots is None:
        page.obj.Annots = pdf.make_indirect(Array([annot]))
    else:
        annots.append(annot)
    return annot


def hostile_pdf(path: Path) -> Path:
    """Three pages; page 3 holds the secret, reachable from page 1 in every known way."""
    make_pdf(path, pages=3)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        p1, p3 = pdf.pages[0], pdf.pages[2]
        # A form: one parent field, a widget on page 1 and a filled-in widget on page 3.
        parent = pdf.make_indirect(Dictionary(T=String("person"), FT=Name.Tx))
        shown = pdf.make_stream(b"0 0 1 rg 0 0 200 30 re f")  # page 1's widget: a blue box
        name = _annot(
            pdf,
            p1,
            Subtype=Name.Widget,
            T=String("name"),
            V=String("Alice"),
            Parent=parent,
            AP=Dictionary(N=shown),
        )
        ssn_look = pdf.make_stream(f"BT /Helv 9 Tf ({SECRET}-look) Tj ET".encode())
        ssn = _annot(
            pdf,
            p3,
            Subtype=Name.Widget,
            T=String("ssn"),
            V=String(f"{SECRET}-value"),
            Parent=parent,
            AP=Dictionary(N=ssn_look),
        )
        parent.Kids = Array([name, ssn])
        pdf.Root.AcroForm = Dictionary(Fields=Array([parent]))
        # A comment on page 3 and a reply to it on page 1.
        comment = _annot(pdf, p3, Subtype=Name.Text, Contents=String(f"{SECRET}-comment"))
        _annot(pdf, p1, Subtype=Name.Text, Contents=String("agreed"), IRT=comment)
        # A note on page 3 whose pop-up window was (oddly) put on page 1.
        note = _annot(pdf, p3, Subtype=Name.Text, Contents=String(f"{SECRET}-note"))
        note.Popup = _annot(pdf, p1, Subtype=Name.Popup, Parent=note)
        # An article thread running from page 1 into page 3.
        info = Dictionary(Title=String(f"{SECRET}-thread"))
        thread = pdf.make_indirect(Dictionary(Type=Name.Thread, I=info))
        bead1 = pdf.make_indirect(Dictionary(Type=Name.Bead, T=thread, P=p1.obj, R=[0, 0, 9, 9]))
        bead3 = pdf.make_indirect(Dictionary(Type=Name.Bead, T=thread, P=p3.obj, R=[0, 0, 9, 9]))
        bead1.N, bead1.V, bead3.N, bead3.V = bead3, bead3, bead1, bead1
        thread.F = bead1
        p1.obj.B, p3.obj.B = Array([bead1]), Array([bead3])
        pdf.Root.Threads = Array([thread])
        pdf.save(path)
    assert everything_in(path).count(SECRET.encode()) >= 5
    return path


@pytest.mark.parametrize("command", ["delete", "extract", "split"])
def test_left_out_pages_leave_nothing_behind(workdir, command):
    src = hostile_pdf(workdir / "form.pdf")
    if command == "delete":
        outs = delete_pages(src, "3").outputs
    elif command == "extract":
        outs = extract_pages(src, "1-2").outputs
    else:
        outs = split_pdf(src, ranges="1-2,3").outputs[:1]  # the file without page 3
    out = outs[0]
    assert page_count(out) == 2
    assert SECRET.encode() not in everything_in(out)
    # What is left looks exactly the same (page 1 carries the widget, reply and pop-up).
    for i in range(2):
        assert mean_abs_diff(render_page(src, i), render_page(out, i)) < 0.5
    with pikepdf.open(out) as pdf:
        annots = pdf.pages[0].obj.Annots
        widget = next(a for a in annots if a.get("/Subtype") == Name.Widget)
        assert str(widget.T) == "name" and "/AP" in widget  # still drawn as before


def test_kept_pages_keep_links_between_their_own_annotations(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        p1 = pdf.pages[0]
        comment = _annot(pdf, p1, Subtype=Name.Text, Contents=String("question"))
        reply = _annot(pdf, p1, Subtype=Name.Text, Contents=String("answer"), IRT=comment)
        comment.Popup = _annot(pdf, p1, Subtype=Name.Popup, Parent=comment)
        assert reply.IRT.objgen == comment.objgen
        pdf.save(src)
    out = delete_pages(src, "2").outputs[0]
    with pikepdf.open(out) as pdf:
        by_text = {str(a.get("/Contents", "")): a for a in pdf.pages[0].obj.Annots}
        assert by_text["answer"].IRT.objgen == by_text["question"].objgen
        assert "/Popup" in by_text["question"]


@pytest.mark.parametrize("inherited", [False, True], ids=["shared", "inherited"])
def test_images_of_left_out_pages_do_not_ride_along_in_shared_resources(workdir, inherited):
    """Many generators give every page one resource dictionary listing every image."""
    src = workdir / "shared.pdf"
    pdf = pikepdf.new()

    def image(data: bytes):
        return pdf.make_stream(
            data,
            Type=Name.XObject,
            Subtype=Name.Image,
            Width=len(data),
            Height=1,
            ColorSpace=Name.DeviceGray,
            BitsPerComponent=8,
        )

    resources = Dictionary(
        XObject=Dictionary(Pub=image(b"\x80" * 20), Sec=image(SECRET.encode() * 2))
    )
    shared = pdf.make_indirect(resources)
    for name in ["Pub", "Pub", "Sec"]:
        pdf.add_blank_page()
        page = pdf.pages[-1]
        page.Contents = pdf.make_stream(f"q 200 0 0 20 50 50 cm /{name} Do Q".encode())
        if "/Resources" in page.obj:
            del page.obj["/Resources"]
        if not inherited:
            page.Resources = shared  # the same dictionary on every page
    if inherited:
        pdf.Root.Pages.Resources = shared  # handed down to every page by the page tree
    pdf.save(src)

    out = extract_pages(src, "1-2").outputs[0]
    assert SECRET.encode() not in everything_in(out)
    assert mean_abs_diff(render_page(src, 0), render_page(out, 0)) < 0.5


# ---------------------------------------------------------------- 2. terminal control codes


def test_printable_leaves_ordinary_text_alone_and_defuses_controls():
    assert printable("รายงาน ประจำปี (1) [draft].pdf") == "รายงาน ประจำปี (1) [draft].pdf"
    assert printable("a\x1b]0;x\x07b") == "a?]0;x?b"
    assert printable("two\nlines\tand\rmore") == "two lines and more"
    assert printable("c1\x9bcsi\x7f") == "c1?csi?"
    assert printable("keep\nlines\tand tabs\x1b", multiline=True) == "keep\nlines\tand tabs?"
    assert escape_json_controls('"a\x9bb\x7f"') == '"a\\u009bb\\u007f"'


def _titled(path: Path, title: str) -> Path:
    make_pdf(path, pages=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = title
        pdf.save(path)
    return path


def test_cli_info_never_prints_control_codes_from_a_pdf(workdir):
    src = _titled(workdir / "evil.pdf", EVIL + "\n[ok] fake line")
    r = CliRunner().invoke(app, ["info", str(src)])
    assert r.exit_code == 0
    assert not any(c in r.output for c in "\x1b\x07\x9b")
    title = next(line for line in r.output.splitlines() if "Title:" in line)
    assert "Report?]0;owned?" in title and title.endswith("[ok] fake line")  # one line


def test_cli_json_keeps_the_exact_value_but_escaped(workdir):
    src = _titled(workdir / "evil.pdf", EVIL)
    r = CliRunner().invoke(app, ["info", str(src), "--json"])
    assert r.exit_code == 0 and not any(c in r.output for c in "\x1b\x07\x9b")
    assert json.loads(r.output)[0]["info"]["metadata"]["Title"] == EVIL


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold ESC")
def test_cli_file_names_with_control_codes_are_shown_harmlessly(workdir):
    src = make_pdf(workdir / "a\x1b]0;x\x07.pdf", pages=1)
    r = CliRunner().invoke(app, ["rotate", str(src), "--angle", "90"])
    assert r.exit_code == 0 and "\x1b" not in r.output and "a?]0;x?" in r.output


def test_info_lines_are_single_printable_lines():
    info = PdfInfo(
        path=Path("name\x1b.pdf"),
        size_bytes=1,
        encrypted=False,
        page_count=1,
        metadata={"Title": EVIL, "Author": "สมชาย\nใจดี"},
    )
    lines = format_info_lines(info)
    assert lines[0] == "name?.pdf"
    assert not any(ch in "".join(lines) for ch in "\x1b\x07\x9b\n")
    assert any(line.endswith("สมชาย ใจดี") for line in lines)


def test_tui_result_screen_text_has_no_control_codes():
    tool = TOOLS[0]
    ok = OperationResult(command="info", sources=[Path("x.pdf")], notes=[f"  Title: {EVIL}"])
    entries = [
        BatchEntry(Path("a\x1b[2J.pdf"), result=ok),
        BatchEntry(Path("b.pdf"), error=f"b.pdf: broken {EVIL}"),
    ]
    text = describe(tool, Outcome(entries=entries)).plain
    assert not any(c in text for c in "\x1b\x07\x9b")
    assert "Report?]0;owned?" in text
    assert "\x1b" not in headline(Outcome(fatal=EVIL)).plain


# ---------------------------------------------------------------- 3. clean removes comment authors


def test_clean_removes_comment_authors_and_dates_but_keeps_form_field_names(workdir):
    src = make_pdf(workdir / "notes.pdf", pages=1)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        _annot(
            pdf,
            page,
            Subtype=Name.Text,
            T=String("Somchai Secret"),
            Contents=String("please fix"),
            M=String("D:20261101120000+07'00'"),
            CreationDate=String("D:20261101110000+07'00'"),
        )
        page.obj.Annots.append(  # a direct annotation, written inside the array itself
            Dictionary(
                Type=Name.Annot, Subtype=Name.Square, Rect=[0, 0, 9, 9], T=String("Direct Author")
            )
        )
        _annot(
            pdf,
            page,
            Subtype=Name.Widget,
            FT=Name.Tx,
            T=String("email"),
            M=String("D:20261101120000"),
        )
        pdf.save(src)
    with pikepdf.open(src) as pdf:
        original_ids = {bytes(part) for part in pdf.trailer.ID}

    r = clean_pdf(src)
    out = r.outputs[0]
    assert r.annotations_cleaned == 3 and r.to_dict()["annotations_cleaned"] == 3
    assert any("author names and dates from 3" in n for n in r.notes)
    blob = everything_in(out)
    assert b"Somchai Secret" not in blob and b"Direct Author" not in blob
    assert b"D:2026" not in blob
    assert b"please fix" in blob  # the comment itself stays
    with pikepdf.open(out) as pdf:
        widget = next(a for a in pdf.pages[0].obj.Annots if a.get("/Subtype") == Name.Widget)
        assert str(widget.T) == "email"  # a field name, which forms need
        # No longer tied to the original: neither half of the file identifier is reused.
        assert not {bytes(part) for part in pdf.trailer.ID} & original_ids
    assert mean_abs_diff(render_page(src), render_page(out)) < 0.5


def test_clean_of_a_file_with_only_comment_authors_writes_a_result(workdir):
    src = make_pdf(workdir / "c.pdf", pages=1)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        if "/Info" in pdf.trailer:
            del pdf.trailer["/Info"]
        _annot(pdf, pdf.pages[0], Subtype=Name.Text, T=String("Me"))
        pdf.save(src)
    r = clean_pdf(src)
    assert not r.nothing_found and r.outputs and r.annotations_cleaned == 1


# ---------------------------------------------------------------- 5. crafted files


def _image_pdf(path: Path, **entries) -> Path:
    """One page that draws one image with the given dictionary entries (and 100 bytes)."""
    pdf = pikepdf.new()
    pdf.add_blank_page()
    image = pdf.make_stream(b"\x80" * 100, **entries)
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(Im=image))
    pdf.pages[0].Contents = pdf.make_stream(b"q 300 0 0 300 50 50 cm /Im Do Q")
    pdf.save(path)
    return path


def test_check_inflated_size():
    exact = zlib.compress(b"\x00" * 1000)
    _check_inflated_size(exact, limit=1000)  # fine
    with pytest.raises(ValueError, match="larger"):
        _check_inflated_size(zlib.compress(b"\x00" * 50_000_000), limit=1000)
    _check_inflated_size(b"not zlib at all", limit=10)  # left to qpdf, which is more forgiving


def test_compress_skips_a_zip_bomb_image(workdir):
    bomb = zlib.compress(b"\x00" * 50_000_000, 9)  # about 50 KB that inflate to 50 MB
    src = workdir / "bomb.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    im = pdf.make_stream(
        b"",
        Type=Name.XObject,
        Subtype=Name.Image,
        Width=100,
        Height=100,
        ColorSpace=Name.DeviceGray,
        BitsPerComponent=8,
    )
    im.write(bomb, filter=Name.FlateDecode)
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(Im=im))
    pdf.pages[0].Contents = pdf.make_stream(b"q 300 0 0 300 50 50 cm /Im Do Q")
    pdf.save(src)
    r = compress_pdf(src, level="high")
    assert r.images_skipped.get("could not be decoded") == 1


@pytest.mark.parametrize(
    "entries",
    [
        {"BitsPerComponent": Name("/Eight"), "ColorSpace": Name.DeviceGray},
        {"BitsPerComponent": 8, "ColorSpace": Array([Name.ICCBased, Dictionary(N=Name("/x"))])},
        {"BitsPerComponent": 8, "ColorSpace": Name.DeviceGray, "Width": Name("/wide")},
    ],
    ids=["bpc-not-a-number", "icc-not-a-stream", "width-not-a-number"],
)
def test_compress_skips_a_malformed_image_instead_of_failing(workdir, entries):
    entries = {"Type": Name.XObject, "Subtype": Name.Image, "Width": 10, "Height": 10, **entries}
    src = _image_pdf(workdir / "odd.pdf", **entries)
    r = compress_pdf(src, level="high")  # must not raise
    assert r.images_found == 1 and r.images_recompressed == 0


def test_deeply_nested_forms_do_not_crash(workdir):
    src = workdir / "deep.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    inner = pdf.make_stream(
        b"\x80" * 100,
        Type=Name.XObject,
        Subtype=Name.Image,
        Width=10,
        Height=10,
        ColorSpace=Name.DeviceGray,
        BitsPerComponent=8,
    )
    resources = Dictionary(XObject=Dictionary(Im=inner))
    for _ in range(3000):  # far beyond Python's recursion limit
        form = pdf.make_stream(
            b"/X Do", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 1, 1], Resources=resources
        )
        resources = Dictionary(XObject=Dictionary(X=form))
    pdf.pages[0].Resources = resources
    pdf.pages[0].Contents = pdf.make_stream(b"/X Do")
    pdf.save(src)
    with pikepdf.open(src) as opened:
        list(iter_images(opened))  # used to raise RecursionError
    compress_pdf(src, level="high")
    r = CliRunner().invoke(app, ["info", str(src)])
    assert r.exit_code == 0 and "bug" not in r.output


def test_oversized_images_are_refused_in_words(workdir, monkeypatch):
    big = workdir / "big.png"
    Image.new("RGB", (60, 60), "white").save(big)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # 3600 px is now "a bomb"
    with pytest.raises(InvalidImageError, match="too large"):
        images_to_pdf([big])
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), "white").save(buf, "JPEG")
    assert _same_picture(buf.getvalue(), buf.getvalue()) is False  # not checkable: left alone
