"""Crafted and damaged inputs: streams that unpack to absurd sizes, broken page entries and
names, inputs with the same name, and passwords typed with spaces."""

from __future__ import annotations

import base64
import contextlib
import io
import subprocess
import sys
import tracemalloc
import zlib
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name
from typer.testing import CliRunner

from pdfgen import make_pdf, page_count, photo_image
from pdfzi.cli import app
from pdfzi.core import limits, pdfimages, pdftoimg
from pdfzi.core.clean import clean_pdf
from pdfzi.core.compress import compress_pdf
from pdfzi.core.errors import CorruptPdfError, PdfziError, UnsafeInputError
from pdfzi.core.info import get_info
from pdfzi.core.limits import unpacked_size
from pdfzi.core.paths import plan_folders, plan_outputs
from pdfzi.core.pdftoimg import RenderError, pdf_to_images
from pdfzi.core.rotate import rotate_pdf
from pdfzi.core.subset import extract_pages, split_pdf
from pdfzi.tui import tools

MB = 1024 * 1024


def packed(data: bytes) -> bytes:
    return zlib.compress(data, 9)


# ---------------------------------------------------------------- unpacked_size


_DOC = pikepdf.new()  # streams belong to a document, which must outlive them


def _stream(data: bytes, filters=None) -> pikepdf.Object:
    stream = _DOC.make_stream(b"")
    if filters is None:
        stream.write(data)
    else:
        stream.write(data, filter=filters)
    return stream


def test_unpacked_size_counts_without_unpacking_past_the_limit():
    assert unpacked_size(_stream(b"x" * 1000), 2000) == 1000
    assert unpacked_size(_stream(b"x" * 1000), 999) is None
    flate = _stream(packed(b"q Q " * (MB // 4)), Name.FlateDecode)  # 1 MB unpacked
    assert unpacked_size(flate, 2 * MB) == MB
    assert unpacked_size(flate, MB - 1) is None
    twice = _stream(packed(packed(b"\0" * (3 * MB))), Array([Name.FlateDecode] * 2))
    assert unpacked_size(twice, 4 * MB) == 3 * MB
    assert unpacked_size(twice, MB) is None
    short = _stream(packed(b"x" * 1000), Name.Fl)  # abbreviated filter name
    assert unpacked_size(short, 2000) == 1000


def test_unpacked_size_of_data_qpdf_would_not_unpack_is_its_stored_size():
    assert unpacked_size(_stream(b"not flate at all", Name.FlateDecode), 100) == 0
    assert unpacked_size(_stream(b"zzzz", Name.NoSuchFilter), 100) == 4
    assert unpacked_size(_stream(b"41424344>", Name.ASCIIHexDecode), 100) == 4
    broken_hex = _stream(b"4142 not hex", Array([Name.ASCIIHexDecode, Name.FlateDecode]))
    assert unpacked_size(broken_hex, 100) == 12


def test_unpacked_size_refuses_malformed_filters():
    stream = _stream(b"x")
    stream.Filter = 5
    assert unpacked_size(stream, MB) is None


def _lzw(data: bytes, early: int = 1) -> bytes:
    """LZW as PDF writers make it, tracking the reader's table to know the code width."""
    out = bytearray()
    buffer = bits = 0
    width = 9

    def emit(code: int) -> None:
        nonlocal buffer, bits
        buffer = (buffer << width) | code
        bits += width
        while bits >= 8:
            bits -= 8
            out.append((buffer >> bits) & 0xFF)
        buffer &= (1 << bits) - 1

    def reset() -> tuple[dict[bytes, int], int]:
        return {bytes([i]): i for i in range(256)}, 258

    table, next_code = reset()
    reader_size, reader_has_prev = 258, False

    def after_emit() -> None:
        nonlocal width, reader_size, reader_has_prev
        if reader_has_prev and reader_size < 4096:
            reader_size += 1
        reader_has_prev = True
        if width < 12 and reader_size + early >= 1 << width:
            width += 1

    emit(256)
    word = b""
    for value in data:
        longer = word + bytes([value])
        if longer in table:
            word = longer
            continue
        emit(table[word])
        after_emit()
        table[longer] = next_code
        next_code += 1
        word = bytes([value])
        if next_code >= 4000:
            emit(256)
            table, next_code = reset()
            width, reader_size, reader_has_prev = 9, 258, False
    if word:
        emit(table[word])
        after_emit()
    emit(257)
    if bits:
        out.append((buffer << (8 - bits)) & 0xFF)
    return bytes(out)


def _run_length(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        run = 1
        while i + run < len(data) and run < 128 and data[i + run] == data[i]:
            run += 1
        if run > 1:
            out += bytes([257 - run, data[i]])
        else:
            literal = data[i : i + 128]
            out += bytes([len(literal) - 1]) + literal
            run = len(literal)
        i += run
    return bytes(out + b"\x80")


_TEXT = b"".join(b"BT /F1 12 Tf %d %d Td (line %d) Tj ET\n" % (i % 97, i, i) for i in range(4000))


@pytest.mark.parametrize(
    ("encoded", "filters", "parms"),
    [
        (_lzw(_TEXT), [Name.LZWDecode], None),
        (_lzw(_TEXT, early=0), [Name.LZWDecode], Dictionary(EarlyChange=0)),
        (_lzw(bytes(range(256)) * 300), [Name.LZWDecode], None),
        (_run_length(b"\0" * 5000 + _TEXT[:3000]), [Name.RunLengthDecode], None),
        (base64.a85encode(b"\0" * 40 + _TEXT) + b"~>", [Name.ASCII85Decode], None),
        (
            base64.a85encode(packed(_TEXT), wrapcol=72) + b"~>",
            [Name.ASCII85Decode, Name.FlateDecode],
            None,
        ),
        (packed(_lzw(_TEXT)).hex().encode() + b">", [Name.AHx, Name.Fl, Name.LZW], None),
    ],
    ids=["lzw", "lzw-early-0", "lzw-wide-codes", "run-length", "a85", "a85-flate", "chain"],
)
def test_unpacked_size_matches_what_qpdf_unpacks(encoded, filters, parms):
    stream = _stream(b"")
    stream.write(encoded, filter=Array(filters), decode_parms=parms)
    expected = len(stream.read_bytes(pikepdf.StreamDecodeLevel.specialized))
    assert expected > 0
    assert unpacked_size(stream, 2 * expected) == expected
    assert unpacked_size(stream, expected - 1) is None


def test_a_chain_of_filters_is_not_unpacked_past_the_limit():
    # Hex text around two rounds of Flate: well under 1 KB in the file, 64 MB unpacked.
    # A chain like this used to be handed to qpdf, which unpacked all of it to measure it.
    bomb = _stream(
        packed(packed(bytes(64 * MB))).hex().encode(),
        Array([Name.ASCIIHexDecode, Name.FlateDecode, Name.FlateDecode]),
    )
    tracemalloc.start()
    try:
        assert unpacked_size(bomb, MB) is None
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * MB


def test_a_predictor_inside_a_chain_cannot_be_measured():
    stream = _stream(b"")
    stream.write(
        packed(packed(b"x" * 1000)),
        filter=Array([Name.FlateDecode, Name.FlateDecode]),
        decode_parms=Array([Dictionary(Predictor=12, Columns=10), Dictionary()]),
    )
    assert unpacked_size(stream, MB) is None


# ---------------------------------------------------------------- XMP that unpacks to a lot


def _raw_pdf_with_xmp(xmp_packed: bytes) -> bytes:
    """A one-page PDF written by hand, so the XMP stream stays packed on disk."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Metadata 4 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        b"<< /Type /Metadata /Subtype /XML /Filter /FlateDecode /Length %d >>\nstream\n"
        % len(xmp_packed)
        + xmp_packed
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.fixture
def xmp_bomb(workdir, monkeypatch) -> Path:
    monkeypatch.setattr(limits, "MAX_METADATA", MB)  # keeps the test small
    path = workdir / "xmp.pdf"
    path.write_bytes(_raw_pdf_with_xmp(packed(b" " * (3 * MB))))
    return path


@pytest.mark.parametrize("command", ["info", "rotate", "clean", "extract", "compress"])
def test_a_file_whose_xmp_unpacks_to_a_lot_is_refused(xmp_bomb, command):
    run = {
        "info": lambda: get_info(xmp_bomb),
        "rotate": lambda: rotate_pdf(xmp_bomb, 90),
        "clean": lambda: clean_pdf(xmp_bomb),
        "extract": lambda: extract_pages(xmp_bomb, "1"),
        "compress": lambda: compress_pdf(xmp_bomb, level="high"),
    }[command]
    with pytest.raises(UnsafeInputError, match="XMP metadata would unpack to more than 1 MB"):
        run()
    assert sorted(p.name for p in xmp_bomb.parent.iterdir()) == ["xmp.pdf"]


def test_ordinary_xmp_is_fine(workdir):
    path = workdir / "xmp.pdf"
    path.write_bytes(_raw_pdf_with_xmp(packed(b"<x:xmpmeta>" + b" " * 2000 + b"</x:xmpmeta>")))
    assert page_count(rotate_pdf(path, 90).outputs[0]) == 1


def test_cli_explains_a_refused_file(xmp_bomb):
    r = CliRunner().invoke(app, ["rotate", str(xmp_bomb), "--angle", "90"])
    assert r.exit_code == 1
    assert "would unpack to more than 1 MB" in r.output and "bug" not in r.output


# ---------------------------------------------------------------- drawing instructions


def bloated_pdf(path: Path, instructions: bytes, *, spare_image: bool = False) -> Path:
    """A page with a photo drawn small, whose instructions are ``instructions`` (packed)."""
    pdf = pikepdf.new()
    pdf.add_blank_page()
    buf = io.BytesIO()
    photo_image(2400, 1800, seed=3).save(buf, "JPEG", quality=95)
    photo = pdf.make_stream(
        buf.getvalue(), Type=Name.XObject, Subtype=Name.Image, Width=2400, Height=1800,
        ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=Name.DCTDecode,
    )  # fmt: skip
    xobjects = Dictionary(Im0=photo)
    if spare_image:  # never drawn: removing unused resources drops it
        xobjects.Spare = pdf.make_stream(
            buf.getvalue(), Type=Name.XObject, Subtype=Name.Image, Width=2400, Height=1800,
            ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=Name.DCTDecode,
        )  # fmt: skip
    pdf.pages[0].Resources = Dictionary(XObject=xobjects)
    content = pdf.make_stream(b"")
    content.write(
        packed(b"q 144 0 0 108 50 50 cm /Im0 Do Q " + instructions), filter=Name.FlateDecode
    )
    pdf.pages[0].Contents = content
    pdf.save(path, compress_streams=False, recompress_flate=False)
    return path


def test_compress_does_not_read_instructions_too_large_to_read_safely(workdir, monkeypatch):
    monkeypatch.setattr(pdfimages, "MAX_PARSE", 64 * 1024)
    src = bloated_pdf(workdir / "a.pdf", b"q Q " * 50_000)  # 200 KB unpacked
    r = compress_pdf(src, level="high")
    assert r.images_recompressed == 0  # its size on the page is unknown: left alone
    assert any("cannot tell how large they appear" in n for n in r.notes)


def test_compress_stops_reading_once_the_whole_file_is_too_much(workdir, monkeypatch):
    monkeypatch.setattr(pdfimages, "MAX_PARSE_TOTAL", 100 * 1024)
    src = bloated_pdf(workdir / "a.pdf", b"q Q " * 30_000)  # 120 KB: within one page's limit
    r = compress_pdf(src, level="high")
    assert r.images_recompressed == 0


def test_compress_still_shrinks_images_of_an_ordinary_page(workdir):
    r = compress_pdf(bloated_pdf(workdir / "a.pdf", b"q Q " * 1000), level="high")
    assert r.images_recompressed == 1


def test_compress_leaves_unused_resources_when_instructions_are_oversized(workdir, monkeypatch):
    src = bloated_pdf(workdir / "a.pdf", b"", spare_image=True)
    assert not compress_pdf(src).kept_original  # normally the unused photo is dropped
    monkeypatch.setattr(limits, "MAX_CONTENT", 16)
    r = compress_pdf(src, dest=workdir / "b.pdf")
    assert r.kept_original  # not read, so nothing was dropped, and nothing got smaller


@pytest.mark.parametrize("command", ["extract", "split"])
def test_page_selection_refuses_oversized_instructions(workdir, monkeypatch, command):
    monkeypatch.setattr(limits, "MAX_CONTENT", 64 * 1024)
    src = bloated_pdf(workdir / "a.pdf", b"q Q " * 50_000)
    with pytest.raises(UnsafeInputError, match="drawing instructions of page 1"):
        extract_pages(src, "1") if command == "extract" else split_pdf(src)
    assert sorted(p.name for p in workdir.iterdir()) == ["a.pdf"]


def test_page_selection_only_checks_the_pages_it_keeps(workdir, monkeypatch):
    monkeypatch.setattr(limits, "MAX_CONTENT", 64 * 1024)
    src = make_pdf(workdir / "a.pdf", pages=2)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[1].Contents = pdf.make_stream(b"")
        pdf.pages[1].Contents.write(packed(b"q Q " * 50_000), filter=Name.FlateDecode)
        pdf.save(src, compress_streams=False, recompress_flate=False)
    assert page_count(extract_pages(src, "1").outputs[0]) == 1


# ---------------------------------------------------------------- same names with --force


def test_inputs_with_the_same_name_get_distinct_results(workdir):
    a, b = workdir / "a" / "report.pdf", workdir / "b" / "Report.pdf"
    out = plan_outputs([a, b], "rotated", workdir / "out")
    assert [p.name for p in out] == ["report-rotated.pdf", "Report-rotated-2.pdf"]
    assert plan_folders([a, b], "split", workdir / "out") == [
        workdir / "out" / "report-split",
        workdir / "out" / "Report-split-2",
    ]
    assert plan_folders([a], "split", workdir / "out") == [workdir / "out"]
    assert plan_folders([a, b], "split", None) == [None, None]


def test_force_with_two_inputs_of_the_same_name_writes_both(workdir):
    for sub, pages in (("a", 1), ("b", 3)):
        (workdir / sub).mkdir()
        make_pdf(workdir / sub / "report.pdf", pages=pages)
    out = workdir / "out"
    r = CliRunner().invoke(
        app,
        ["rotate", str(workdir / "a" / "report.pdf"), str(workdir / "b" / "report.pdf"),
         "--angle", "90", "-o", str(out), "--force"],
    )  # fmt: skip
    assert r.exit_code == 0
    assert sorted((p.name, page_count(p)) for p in out.iterdir()) == [
        ("report-rotated-2.pdf", 3),
        ("report-rotated.pdf", 1),
    ]


def test_split_with_force_gives_same_named_inputs_their_own_folders(workdir):
    for sub, pages in (("a", 1), ("b", 2)):
        (workdir / sub).mkdir()
        make_pdf(workdir / sub / "book.pdf", pages=pages)
    out = workdir / "out"
    r = CliRunner().invoke(
        app,
        ["split", str(workdir / "a" / "book.pdf"), str(workdir / "b" / "book.pdf"),
         "-o", str(out), "--force"],
    )  # fmt: skip
    assert r.exit_code == 0
    assert sorted(len(list(d.iterdir())) for d in out.iterdir()) == [1, 2]


# ---------------------------------------------------------------- damaged files


def _damaged(path: Path, change) -> Path:
    make_pdf(path, pages=2, images_per_page=1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        change(pdf)
        pdf.save(path)
    return path


@pytest.mark.parametrize(
    "change",
    [
        lambda pdf: setattr(pdf.pages[0], "CropBox", Name.Foo),
        lambda pdf: setattr(pdf.pages[0], "Rotate", Name.Foo),
    ],
    ids=["cropbox-not-numbers", "rotate-not-a-number"],
)
def test_info_survives_page_entries_that_are_not_numbers(workdir, change):
    info = get_info(_damaged(workdir / "a.pdf", change))
    assert info.page_count == 2


def test_extract_survives_an_outline_entry_that_points_elsewhere(workdir):
    def change(pdf):
        item = pdf.make_indirect(Dictionary(Title="x"))
        item.Next = pdf.make_indirect(Array([1]))  # an outline entry must be a dictionary
        pdf.Root.Outlines = pdf.make_indirect(Dictionary(First=item, Last=item))

    assert page_count(extract_pages(_damaged(workdir / "a.pdf", change), "1").outputs[0]) == 1


def test_extract_reports_a_form_with_broken_resources_in_words(workdir):
    def change(pdf):
        form = pdf.make_stream(b"/Im0 Do", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 1, 1])
        form.Resources = 5
        pdf.pages[0].Resources.XObject.F = form
        pdf.pages[0].Contents = pdf.make_stream(b"/F Do")

    with pytest.raises(CorruptPdfError, match="pages could not be separated"):
        extract_pages(_damaged(workdir / "a.pdf", change), "1")


def test_names_that_are_not_utf8_do_not_stop_compress_or_clean(workdir):
    bad = pikepdf.Object.parse(b"/Bad#d9Name")

    def change(pdf):
        image = next(iter(pdf.pages[0].Resources.XObject.values()))
        image.Filter = Array([bad])
        pdf.pages[1].Contents = pdf.make_stream(b"q 10 0 0 10 0 0 cm /X#a9 Do Q")

    src = _damaged(workdir / "a.pdf", change)
    for run in (lambda: compress_pdf(src, level="high"), lambda: clean_pdf(src)):
        # A clear refusal is fine; any other exception would be "a bug in pdfzi".
        with contextlib.suppress(PdfziError):
            run()


def test_pdf2img_reports_a_page_pdfium_cannot_load(workdir, monkeypatch):
    src = make_pdf(workdir / "a.pdf", pages=1)
    real_open = pdftoimg._open_doc

    class Broken:
        def __init__(self, doc):
            self.doc = doc

        def __len__(self):
            return len(self.doc)

        def __getitem__(self, index):
            raise pdftoimg.pdfium.PdfiumError("Failed to load page.")

        def close(self):
            self.doc.close()

    monkeypatch.setattr(pdftoimg, "_open_doc", lambda *a: Broken(real_open(*a)))
    with pytest.raises(RenderError, match="Page 1 could not be loaded"):
        pdf_to_images(src, dpi=40)


# ---------------------------------------------------------------- small things


def test_tui_uses_passwords_exactly_as_typed():
    values = {
        "password": "  secret ",
        "new_password": " a b ",
        "confirm": " a b ",
        "pages": " 1-3 ",
    }
    assert tools._text(values, "password") == "  secret "
    assert tools._text(values, "new_password") == " a b "
    assert tools._text(values, "pages") == "1-3"


_PILLOW_CHECK = """
import warnings
import pdfzi.core
from PIL import Image
with warnings.catch_warnings(record=True) as caught:
    Image._decompression_bomb_check((10_000, 9_500))  # 95 megapixels: Pillow would warn
print(len(caught))
"""


def test_pillows_large_picture_warning_never_reaches_the_screen():
    # A fresh interpreter, as when pdfzi runs: pytest resets warning filters on its own.
    done = subprocess.run(
        [sys.executable, "-c", _PILLOW_CHECK], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "0"
