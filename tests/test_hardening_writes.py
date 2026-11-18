"""Second round of hardening: photos ``clean`` used to miss, drawings that take forever to
measure, results written while something else takes the same name, results more readable
than their sources, and finished files lost when a run stops halfway."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import threading
import zlib
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name
from PIL import Image
from typer.testing import CliRunner

from pdfgen import Snapshot, make_pdf, page_count, photo_image
from pdfzi.cli import app
from pdfzi.core import clean, pdfimages, pdftoimg, safeio, subset
from pdfzi.core.batch import BatchEntry
from pdfzi.core.clean import clean_pdf
from pdfzi.core.compress import compress_pdf
from pdfzi.core.errors import OperationCancelledError, OutputWriteError
from pdfzi.core.output import note_kept_files
from pdfzi.core.pdfimages import TooComplexError, image_extents
from pdfzi.core.pdftoimg import pdf_to_images
from pdfzi.core.progress import CancelToken
from pdfzi.core.protect import lock_pdf, unlock_pdf
from pdfzi.core.safeio import safe_output
from pdfzi.core.subset import split_pdf
from pdfzi.tui.screens.result import Outcome, describe
from pdfzi.tui.tools import TOOLS

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")

# ---------------------------------------------------------------- clean: every JPEG form


def jpeg_with_gps(size=(200, 150)) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (13.0, 45.0, 0.0), "E", (100.0, 30.0, 0.0)
    buf = io.BytesIO()
    photo_image(*size, seed=4).save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def photo_pdf(path: Path, data: bytes, filters, parms=None, size=(200, 150)) -> Path:
    """A page showing one photo stored with ``filters`` (no document info, no XMP)."""
    pdf = pikepdf.new()
    pdf.add_blank_page()
    extra = {"DecodeParms": parms} if parms is not None else {}
    img = pdf.make_stream(
        data, Type=Name.XObject, Subtype=Name.Image, Width=size[0], Height=size[1],
        ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=filters, **extra,
    )  # fmt: skip
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(Im0=img))
    pdf.pages[0].Contents = pdf.make_stream(b"q 100 0 0 75 50 50 cm /Im0 Do Q")
    pdf.save(path)
    return path


def decoded_streams(path: Path) -> bytes:
    """Every stream's data, with a Flate wrapper around a JPEG undone too."""
    with pikepdf.open(path) as pdf:
        out = []
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            raw = bytes(obj.read_raw_bytes())
            out.append(raw)
            with contextlib.suppress(pikepdf.PdfError):
                out.append(obj.read_bytes())
            with contextlib.suppress(zlib.error):
                out.append(zlib.decompress(raw))
        return b"\n".join(out)


def pixels(jpeg: bytes) -> bytes:
    with Image.open(io.BytesIO(jpeg)) as img:
        return img.convert("RGB").tobytes()


@pytest.mark.parametrize(
    ("filters", "wrap", "parms"),
    [
        (Name.DCTDecode, lambda d: d, None),
        (Array([Name.DCTDecode]), lambda d: d, None),
        (Array([Name.DCTDecode]), lambda d: d, Array([Dictionary(ColorTransform=1)])),
        (Array([Name.FlateDecode, Name.DCTDecode]), zlib.compress, None),
        (Array([Name.FlateDecode, Name.DCTDecode]), zlib.compress, Array([None, None])),
    ],
    ids=["name", "array", "array-with-parms", "flate-wrapped", "flate-wrapped-null-parms"],
)
def test_clean_removes_exif_whatever_way_the_jpeg_filter_is_written(workdir, filters, wrap, parms):
    data = jpeg_with_gps()
    src = photo_pdf(workdir / "photo.pdf", wrap(data), filters, parms)
    assert b"SecretCam" in decoded_streams(src)

    r = clean_pdf(src)

    assert r.images_cleaned == 1 and r.images_skipped == 0
    assert b"SecretCam" not in decoded_streams(r.outputs[0])
    with pikepdf.open(r.outputs[0]) as pdf:
        image = pdf.pages[0].Resources.XObject.Im0
        assert image.Filter == Name.DCTDecode
        assert pixels(bytes(image.read_raw_bytes())) == pixels(data)  # the same picture
        if parms is not None and parms[-1] is not None:
            assert image.DecodeParms.ColorTransform == 1  # the JPEG's own parameter is kept


def _no_metadata_report(r) -> None:
    assert r.nothing_found and r.outputs == []
    assert r.notes[0].startswith("No metadata that pdfzi can remove was found")
    assert any("Could not check 1 embedded photo" in n for n in r.notes)


def test_clean_reports_a_photo_behind_a_filter_it_cannot_undo(workdir):
    data = jpeg_with_gps()
    src = photo_pdf(
        workdir / "hex.pdf",
        data.hex().encode() + b">",
        Array([Name.ASCIIHexDecode, Name.DCTDecode]),
    )
    r = clean_pdf(src)
    assert r.images_skipped == 1 and r.images_cleaned == 0
    _no_metadata_report(r)


@pytest.mark.parametrize(
    "outer_parms", [Dictionary(Predictor=12, Columns=10), 7], ids=["predictor", "not-a-dict"]
)
def test_clean_reports_a_flate_wrapper_with_parameters(workdir, outer_parms):
    parms = Array([outer_parms, None])
    src = photo_pdf(
        workdir / "pred.pdf",
        zlib.compress(jpeg_with_gps()),
        Array([Name.FlateDecode, Name.DCTDecode]),
        parms,
    )
    _no_metadata_report(clean_pdf(src))


def test_clean_does_not_inflate_a_zip_bomb_around_a_jpeg(workdir):
    bomb = zlib.compress(jpeg_with_gps() + bytes(40 * 1024 * 1024), 9)
    src = photo_pdf(workdir / "bomb.pdf", bomb, Array([Name.FlateDecode, Name.DCTDecode]))
    _no_metadata_report(clean_pdf(src))


def test_clean_caps_what_it_unpacks_whatever_size_the_photo_claims(workdir, monkeypatch):
    # /Width and /Height come from the file: 100000 x 100000 used to allow about 80 GB.
    monkeypatch.setattr(clean, "_MAX_UNWRAPPED_JPEG", 1024 * 1024, raising=False)
    padded = zlib.compress(jpeg_with_gps() + bytes(2 * 1024 * 1024))
    src = photo_pdf(
        workdir / "liar.pdf",
        padded,
        Array([Name.FlateDecode, Name.DCTDecode]),
        size=(100_000, 100_000),
    )
    _no_metadata_report(clean_pdf(src))


def test_clean_says_when_it_skipped_photos_even_after_removing_other_metadata(workdir):
    src = photo_pdf(
        workdir / "mixed.pdf",
        jpeg_with_gps().hex().encode() + b">",
        Array([Name.ASCIIHexDecode, Name.DCTDecode]),
    )
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Author"] = "Somebody"
        pdf.save(src)
    r = clean_pdf(src)
    assert r.outputs and r.images_skipped == 1
    assert any("Could not check 1 embedded photo" in n for n in r.notes)
    assert r.to_dict()["images_skipped"] == 1


def test_clean_still_says_nothing_found_for_a_plain_file(workdir):
    src = workdir / "plain.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(src)
    r = clean_pdf(src)
    assert r.nothing_found and r.notes == ["No metadata found; the file was left unchanged."]


# ---------------------------------------------------------------- drawings too intricate


def big_photo(pdf: pikepdf.Pdf, size=(2400, 1800)) -> pikepdf.Object:
    buf = io.BytesIO()
    photo_image(*size, seed=2).save(buf, "JPEG", quality=95)
    return pdf.make_stream(
        buf.getvalue(), Type=Name.XObject, Subtype=Name.Image, Width=size[0], Height=size[1],
        ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=Name.DCTDecode,
    )  # fmt: skip


def intricate_pdf(path: Path, *, bomb: bool = True) -> Path:
    """Page 1 draws a large photo at a small size, so compress would normally shrink it.

    With ``bomb``, page 2 holds 8 levels of forms that each draw the next one 10 times:
    10^8 visits from a file of a few KB.
    """
    pdf = pikepdf.new()
    pdf.add_blank_page()
    photo = big_photo(pdf)
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(Im0=photo))
    pdf.pages[0].Contents = pdf.make_stream(b"q 144 0 0 108 50 50 cm /Im0 Do Q")
    if bomb:
        pdf.add_blank_page()
        tiny = pdf.make_stream(
            b"\x80", Type=Name.XObject, Subtype=Name.Image, Width=1, Height=1,
            ColorSpace=Name.DeviceGray, BitsPerComponent=8,
        )  # fmt: skip
        resources = Dictionary(XObject=Dictionary(X=tiny))
        draw_ten = b" ".join([b"q 1 0 0 1 1 1 cm /X Do Q"] * 10)
        for _ in range(8):
            form = pdf.make_stream(
                draw_ten, Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 9, 9],
                Resources=resources,
            )  # fmt: skip
            resources = Dictionary(XObject=Dictionary(X=form))
        pdf.pages[1].Resources = resources
        pdf.pages[1].Contents = pdf.make_stream(draw_ten)
    pdf.save(path)
    return path


def test_measuring_an_exponential_form_tree_stops_at_the_work_limit(workdir):
    src = intricate_pdf(workdir / "bomb.pdf")
    with pikepdf.open(src) as pdf, pytest.raises(TooComplexError):
        image_extents(pdf)  # used to run for hours


def test_measuring_can_be_cancelled(workdir, monkeypatch):
    monkeypatch.setattr(pdfimages, "MAX_FORM_WORK", 10**12)  # only cancelling can stop it
    src = intricate_pdf(workdir / "bomb.pdf")

    class StopSoon(CancelToken):
        calls = 0

        def check(self) -> None:
            self.calls += 1
            if self.calls > 3:
                raise OperationCancelledError

    with pikepdf.open(src) as pdf, pytest.raises(OperationCancelledError):
        image_extents(pdf, StopSoon())


def test_a_form_drawn_many_times_is_still_measured_at_its_largest(workdir):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    photo = big_photo(pdf, (300, 200))
    form = pdf.make_stream(
        b"/Im0 Do", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 1, 1],
        Resources=Dictionary(XObject=Dictionary(Im0=photo)),
    )  # fmt: skip
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(F=form))
    small = b"q 72 0 0 72 0 0 cm /F Do Q " * 50
    pdf.pages[0].Contents = pdf.make_stream(small + b"q 216 0 0 144 0 0 cm /F Do Q")
    ((w, h),) = image_extents(pdf).values()  # the parsed form is reused, sizes are not
    assert (w, h) == (3.0, 2.0)


def test_compress_shrinks_the_photo_when_the_drawings_are_ordinary(workdir):
    # Control for the next test: without the form tree the photo is made smaller.
    r = compress_pdf(intricate_pdf(workdir / "plain.pdf", bomb=False), level="medium")
    assert r.images_recompressed == 1
    with pikepdf.open(r.outputs[0]) as pdf:
        assert int(pdf.pages[0].Resources.XObject.Im0.Width) < 2400


@pytest.mark.parametrize("how", [{"level": "medium"}, {"level": "high"}, {"target": 1}])
def test_compress_leaves_all_images_alone_when_the_drawings_cannot_be_measured(workdir, how):
    src = intricate_pdf(workdir / "bomb.pdf")
    r = compress_pdf(src, **how)
    # Neither the size measured on page 1 nor the max_side fallback is used.
    assert r.images_recompressed == 0
    assert any("too intricate or damaged to measure" in n for n in r.notes)
    for out in r.outputs:
        with pikepdf.open(out) as pdf:
            assert int(pdf.pages[0].Resources.XObject.Im0.Width) == 2400


def nested_pdf(path: Path, levels: int) -> Path:
    """One photo drawn 1 inch wide on the page itself, and 6 inches wide through ``levels``
    forms nested inside each other."""
    pdf = pikepdf.new()
    pdf.add_blank_page()
    photo = big_photo(pdf)
    inner: pikepdf.Object = photo
    for _ in range(levels):
        inner = pdf.make_stream(
            b"/X Do", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 1, 1],
            Resources=Dictionary(XObject=Dictionary(X=inner)),
        )  # fmt: skip
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(Im0=photo, F=inner))
    pdf.pages[0].Contents = pdf.make_stream(
        b"q 72 0 0 48 50 50 cm /Im0 Do Q q 432 0 0 288 50 300 cm /F Do Q"
    )
    pdf.save(path)
    return path


def test_an_image_inside_nine_nested_forms_is_measured_at_its_largest(workdir):
    src = nested_pdf(workdir / "nine.pdf", 9)
    with pikepdf.open(src) as pdf:
        ((w, h),) = image_extents(pdf).values()
    assert (w, h) == (6.0, 4.0)  # used to stop at 8 levels and report 1 inch


def test_forms_nested_too_deep_cannot_be_measured(workdir):
    src = nested_pdf(workdir / "forty.pdf", 40)
    with pikepdf.open(src) as pdf, pytest.raises(TooComplexError):
        image_extents(pdf)  # used to return the 1 inch use only


def test_compress_leaves_images_alone_when_forms_nest_too_deep(workdir):
    r = compress_pdf(nested_pdf(workdir / "forty.pdf", 40), level="high")
    # The photo is 400 dpi where the forms draw it, 2400 dpi where the page does: going by
    # the small use alone would shrink it far too much.
    assert r.images_recompressed == 0
    assert any("too intricate or damaged to measure" in n for n in r.notes)
    for out in r.outputs:
        with pikepdf.open(out) as pdf:
            assert int(pdf.pages[0].Resources.XObject.Im0.Width) == 2400


def test_a_form_that_draws_itself_cannot_be_measured(workdir):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    form = pdf.make_stream(
        b"/Im0 Do /F Do", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 1, 1]
    )
    form.Resources = Dictionary(XObject=Dictionary(Im0=big_photo(pdf, (300, 200)), F=form))
    pdf.pages[0].Resources = Dictionary(XObject=Dictionary(F=form))
    pdf.pages[0].Contents = pdf.make_stream(b"q 72 0 0 72 0 0 cm /F Do Q")
    with pytest.raises(TooComplexError):
        image_extents(pdf)  # used to skip the loop and return what it had


def test_content_that_cannot_be_read_cannot_be_measured(workdir, monkeypatch):
    src = intricate_pdf(workdir / "plain.pdf", bomb=False)

    def unreadable(*_args, **_kwargs):
        raise pikepdf.PdfError("damaged content stream")

    monkeypatch.setattr(pdfimages.pikepdf, "parse_content_stream", unreadable)
    with pikepdf.open(src) as pdf, pytest.raises(TooComplexError):
        image_extents(pdf)  # used to count the page as drawing nothing


# ---------------------------------------------------------------- never replace by accident


def test_a_name_taken_after_the_check_is_still_not_replaced(workdir, monkeypatch):
    dest = make_pdf(workdir / "o.pdf", pages=1)
    snap = Snapshot(dest)
    real_exists = Path.exists

    def stale_exists(self, *args, **kwargs):
        # The file "appears" right after pdfzi looked: the check says the name is free.
        return False if self == dest else real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", stale_exists)
    with safe_output(dest) as out:
        out.tmp.write_bytes(b"new result")
    monkeypatch.undo()
    snap.assert_unchanged()
    assert out.path == workdir / "o-2.pdf" and out.path.read_bytes() == b"new result"
    assert not out.replaced


def test_runs_writing_the_same_name_at_once_all_keep_their_results(workdir):
    dest = workdir / "same.pdf"
    results: list[Path] = []
    errors: list[BaseException] = []
    start = threading.Barrier(12)

    def write(n: int) -> None:
        try:
            start.wait()
            with safe_output(dest) as out:
                out.tmp.write_bytes(f"result {n}".encode())
            results.append(out.path)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(results)) == 12
    assert sorted(p.read_text() for p in results) == sorted(f"result {n}" for n in range(12))


def test_force_reports_whether_it_replaced_something(workdir):
    with safe_output(workdir / "a.pdf", force=True) as first:
        first.tmp.write_bytes(b"1")
    with safe_output(workdir / "a.pdf", force=True) as second:
        second.tmp.write_bytes(b"2")
    assert (first.replaced, second.replaced) == (False, True)
    assert (workdir / "a.pdf").read_bytes() == b"2"


@POSIX
def test_drives_without_hard_links_reserve_the_name_first(workdir, monkeypatch):
    def no_links(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")  # what FAT32/exFAT answer

    monkeypatch.setattr(safeio.os, "link", no_links)
    dest = make_pdf(workdir / "o.pdf", pages=1)
    snap = Snapshot(dest)
    with safe_output(dest) as out:
        out.tmp.write_bytes(b"x")
    snap.assert_unchanged()
    assert out.path == workdir / "o-2.pdf" and out.path.read_bytes() == b"x"
    assert [p.name for p in workdir.iterdir() if p.suffix == ".tmp"] == []


@POSIX
@pytest.mark.parametrize("someone_wrote_into_it", [False, True])
def test_a_failed_move_removes_only_an_untouched_reservation(
    workdir, monkeypatch, someone_wrote_into_it
):
    monkeypatch.setattr(safeio.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(95, "")))
    dest = workdir / "o.pdf"

    def failing_replace(src, dst):
        if someone_wrote_into_it:
            Path(dst).write_bytes(b"another program's data")
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(safeio.os, "replace", failing_replace)
    with pytest.raises(OutputWriteError), safe_output(dest) as out:
        out.tmp.write_bytes(b"x")
    if someone_wrote_into_it:
        assert dest.read_bytes() == b"another program's data"
    else:
        assert not dest.exists()


# ---------------------------------------------------------------- file modes (macOS/Linux)


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@POSIX
def test_the_temp_file_is_private_while_it_is_written(workdir, umask_022):
    with safe_output(workdir / "o.pdf") as out:
        assert mode(out.tmp) == 0o600
        out.tmp.write_bytes(b"x")
    assert mode(out.path) == 0o644  # an ordinary new file in the end


@POSIX
def test_a_result_is_no_more_readable_than_its_private_source(workdir, umask_022):
    src = make_pdf(workdir / "private.pdf", pages=1)
    src.chmod(0o600)
    r = lock_pdf(src, "pw")
    assert mode(r.outputs[0]) == 0o600
    locked = r.outputs[0]
    plain = unlock_pdf(locked, "pw").outputs[0]  # the readable copy: the case that matters
    assert mode(plain) == 0o600


@POSIX
def test_several_sources_allow_only_what_all_of_them_allow(workdir, umask_022):
    a = make_pdf(workdir / "a.pdf", pages=1)
    b = make_pdf(workdir / "b.pdf", pages=1)
    a.chmod(0o640)
    b.chmod(0o604)
    with safe_output(workdir / "m.pdf", sources=[a, b]) as out:
        out.tmp.write_bytes(b"x")
    assert mode(out.path) == 0o600


@POSIX
def test_force_also_follows_the_file_it_replaces(workdir, umask_022):
    src = make_pdf(workdir / "a.pdf", pages=1)
    dest = make_pdf(workdir / "b.pdf", pages=1)
    dest.chmod(0o600)
    with safe_output(dest, force=True, sources=[src]) as out:
        out.tmp.write_bytes(b"x")
    assert mode(dest) == 0o600


@POSIX
def test_a_drive_that_cannot_set_modes_does_not_stop_the_save(workdir, monkeypatch):
    def refuse(self, *_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "chmod", refuse)
    with safe_output(workdir / "o.pdf", sources=[make_pdf(workdir / "a.pdf")]) as out:
        out.tmp.write_bytes(b"x")
    assert out.path.read_bytes() == b"x"


# ---------------------------------------------------------------- stopping halfway


def _fail_on_third(monkeypatch, module, name, error):
    real = getattr(module, name)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, flaky)


@pytest.mark.parametrize("error", [OutputWriteError("disk full"), OperationCancelledError()])
def test_split_keeps_results_that_already_replaced_older_files(workdir, monkeypatch, error):
    src = make_pdf(workdir / "a.pdf", pages=4)
    folder = workdir / "parts"
    folder.mkdir()
    old = make_pdf(folder / "a-p1.pdf", pages=3)  # an older result, replaced with --force
    _fail_on_third(monkeypatch, subset, "publish_verified", error)

    with pytest.raises(type(error)) as caught:
        split_pdf(src, out_dir=folder, force=True)

    assert page_count(old) == 1  # the new page 1 stays: removing it would not undo --force
    assert sorted(p.name for p in folder.iterdir()) == ["a-p1.pdf"]  # new a-p2 removed
    message = str(caught.value)
    assert message.startswith(("disk full ", "Cancelled. "))
    assert "Kept 1 finished file" in message and "a-p1.pdf" in message


def test_split_without_force_still_removes_everything_it_made(workdir, monkeypatch):
    src = make_pdf(workdir / "a.pdf", pages=4)
    _fail_on_third(monkeypatch, subset, "publish_verified", OutputWriteError("disk full"))
    with pytest.raises(OutputWriteError) as caught:
        split_pdf(src)
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]
    assert str(caught.value) == "disk full"


def test_pdf2img_keeps_results_that_already_replaced_older_files(workdir, monkeypatch):
    src = make_pdf(workdir / "a.pdf", pages=4)
    folder = workdir / "imgs"
    folder.mkdir()
    old = folder / "a-p1.png"
    old.write_bytes(b"old picture")
    _fail_on_third(monkeypatch, pdftoimg, "_render_one", OperationCancelledError())

    with pytest.raises(OperationCancelledError) as caught:
        pdf_to_images(src, dpi=40, out_dir=folder, force=True)

    assert old.read_bytes() != b"old picture"
    with Image.open(old) as img:
        img.load()
    assert sorted(p.name for p in folder.iterdir()) == ["a-p1.png"]
    assert str(caught.value).startswith("Cancelled. Kept 1 finished file that")


def test_ctrl_c_in_the_cli_says_which_files_were_kept(workdir, monkeypatch):
    src = make_pdf(workdir / "a.pdf", pages=4)
    folder = workdir / "parts"
    folder.mkdir()
    old = make_pdf(folder / "a-p1.pdf", pages=3)  # an older result, replaced with --force
    _fail_on_third(monkeypatch, subset, "publish_verified", KeyboardInterrupt())

    r = CliRunner().invoke(app, ["split", str(src), "-o", str(folder), "--force"])

    assert r.exit_code == 130
    assert "Kept 1 finished file" in r.output and "a-p1.pdf" in r.output
    assert r.output.rstrip().endswith("Interrupted.")
    assert page_count(old) == 1  # the new page 1 stays
    assert sorted(p.name for p in folder.iterdir()) == ["a-p1.pdf"]  # new a-p2 removed


def test_tui_shows_a_cancel_that_kept_files_as_a_cancel(workdir):
    error = OperationCancelledError()
    note_kept_files(error, [workdir / "a-p1.pdf"])
    outcome = Outcome(entries=[BatchEntry(workdir / "a.pdf", error=str(error))], cancelled=True)
    assert outcome.failed == 0
    text = describe(TOOLS[0], outcome).plain
    assert "Cancelled" in text and "a-p1.pdf" in text and "[error]" not in text
