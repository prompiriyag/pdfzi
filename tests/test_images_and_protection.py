"""img2pdf, pdf2img, lock, unlock, clean."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from typer.testing import CliRunner

from conftest import TRICKY_NAMES
from pdfgen import (
    Snapshot,
    extract_text,
    make_corrupt_pdf,
    make_pdf,
    page_count,
    page_text,
    photo_image,
)
from pdfzi import cli
from pdfzi.core import pdftoimg
from pdfzi.core.clean import clean_pdf, strip_jpeg_metadata
from pdfzi.core.errors import (
    InvalidOptionError,
    OperationCancelledError,
    OutputWriteError,
    PasswordRequiredError,
    WrongPasswordError,
)
from pdfzi.core.imgtopdf import InvalidImageError, images_to_pdf
from pdfzi.core.pdfimages import iter_images
from pdfzi.core.pdftoimg import pdf_to_images
from pdfzi.core.progress import CancelToken
from pdfzi.core.protect import (
    NotEncryptedError,
    OwnerPasswordRequiredError,
    lock_pdf,
    unlock_pdf,
)

runner = CliRunner()


def run(*args, env=None, input=None):
    return runner.invoke(cli.app, [str(a) for a in args], env=env, input=input)


def leftovers(folder):
    return [p.name for p in folder.rglob("*.tmp")]


# ------------------------------------------------------------------ img2pdf


def save_images(folder: Path, size=(300, 200)):
    jpg = folder / "photo ภาพ.jpg"
    photo_image(*size, seed=1).save(jpg, "JPEG", quality=85)
    png = folder / "pic.png"
    photo_image(*size, seed=2).save(png, "PNG")
    return jpg, png


def test_images_to_pdf_keeps_jpeg_bytes_and_order(workdir):
    jpg, png = save_images(workdir)
    snaps = [Snapshot(jpg), Snapshot(png)]
    r = images_to_pdf([png, jpg])
    out = r.outputs[0]
    assert out == workdir / "pic-images.pdf" and page_count(out) == 2
    with pikepdf.open(out) as pdf:
        images = list(iter_images(pdf))
        assert images[1].read_raw_bytes() == jpg.read_bytes()  # JPEG embedded untouched
        assert str(images[1].Filter) == "/DCTDecode"
        assert [int(i.Width) for i in images] == [300, 300]
    for s in snaps:
        s.assert_unchanged()
    assert leftovers(workdir) == []


def test_png_with_transparency_and_palette_and_16bit(workdir):
    rgba = workdir / "alpha.png"
    Image.new("RGBA", (50, 40), (255, 0, 0, 128)).save(rgba)
    pal = workdir / "pal.png"
    Image.new("P", (50, 40), 3).save(pal)
    deep = workdir / "deep.png"
    Image.new("I;16", (50, 40), 40000).save(deep)
    out = images_to_pdf([rgba, pal, deep]).outputs[0]
    assert page_count(out) == 3
    with pikepdf.open(out) as pdf:
        assert "/SMask" in next(iter_images(pdf))  # transparency kept
    for i in range(3):
        render = pdfium.PdfDocument(out)[i].render(scale=1).to_pil()
        assert render.size[0] > 0


def test_page_sizes_and_orientation(workdir):
    wide = workdir / "wide.png"
    photo_image(800, 400).save(wide)
    tall = workdir / "tall.png"
    photo_image(400, 800).save(tall)
    with pikepdf.open(images_to_pdf([wide, tall], page_size="a4").outputs[0]) as pdf:
        boxes = [(round(float(p.mediabox[2])), round(float(p.mediabox[3]))) for p in pdf.pages]
    assert boxes == [(842, 595), (595, 842)]
    with pikepdf.open(images_to_pdf([wide], page_size="letter").outputs[0]) as pdf:
        assert (float(pdf.pages[0].mediabox[2]), float(pdf.pages[0].mediabox[3])) == (792, 612)


def test_exif_orientation_is_applied(workdir):
    path = workdir / "rot.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 clockwise to display
    photo_image(400, 200).save(path, "JPEG", exif=exif)
    with pikepdf.open(images_to_pdf([path]).outputs[0]) as pdf:
        page = pdf.pages[0]
        w, h = float(page.mediabox[2]), float(page.mediabox[3])
        if int(page.obj.get("/Rotate", 0)) in (90, 270):
            w, h = h, w
    assert h > w  # a 400x200 picture marked "rotate 90" must be shown upright (portrait)


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_images_with_tricky_names(workdir, name):
    img = workdir / (Path(name).stem + ".png")
    photo_image(60, 40).save(img)
    assert page_count(images_to_pdf([img]).outputs[0]) == 1


def test_bad_images_give_friendly_errors(workdir):
    fake = workdir / "fake.jpg"
    fake.write_text("hello, not an image")
    with pytest.raises(InvalidImageError, match="not a readable image"):
        images_to_pdf([fake])
    gif = workdir / "a.gif"
    Image.new("P", (10, 10)).save(gif)
    with pytest.raises(InvalidImageError, match="only JPG and PNG"):
        images_to_pdf([gif])
    truncated = workdir / "cut.jpg"
    good = workdir / "good.jpg"
    photo_image(300, 300).save(good, "JPEG")
    truncated.write_bytes(good.read_bytes()[:200])
    with pytest.raises(InvalidImageError):
        images_to_pdf([truncated])
    with pytest.raises(InvalidOptionError):
        images_to_pdf([])
    with pytest.raises(InvalidOptionError, match="Page size"):
        images_to_pdf([good], page_size="a0")
    assert sorted(p.name for p in workdir.iterdir()) == ["a.gif", "cut.jpg", "fake.jpg", "good.jpg"]


def test_images_to_pdf_numbers_instead_of_overwriting(workdir):
    jpg, _ = save_images(workdir)
    first = images_to_pdf([jpg]).outputs[0]
    second = images_to_pdf([jpg]).outputs[0]
    assert (first.name, second.name) == ("photo ภาพ-images.pdf", "photo ภาพ-images-2.pdf")


# ------------------------------------------------------------------ pdf2img


def test_pdf2img_sizes_names_and_unchanged_source(workdir):
    src = make_pdf(workdir / "รายงาน.pdf", pages=12)
    snap = Snapshot(src)
    r = pdf_to_images(src, dpi=72)
    folder = workdir / "รายงาน-pages"
    assert [p.name for p in r.outputs[:2]] == ["รายงาน-p01.png", "รายงาน-p02.png"]
    assert r.outputs[-1] == folder / "รายงาน-p12.png" and len(r.outputs) == 12
    with Image.open(r.outputs[0]) as img:
        assert img.size == (612, 792) and img.format == "PNG"
        assert round(img.info["dpi"][0]) == 72
        assert img.convert("L").getextrema()[0] < 100  # text drawn: not a blank page
    snap.assert_unchanged()
    assert leftovers(workdir) == []


def test_pdf2img_dpi_scales_and_jpg_quality(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2, images_per_page=1, image_size=(600, 400))
    one = pdf_to_images(src, dpi=72, fmt="jpg", out_dir=workdir / "o1", quality=90).outputs[0]
    two = pdf_to_images(src, dpi=144, fmt="jpeg", out_dir=workdir / "o2", quality=90).outputs[0]
    low = pdf_to_images(src, dpi=144, fmt="jpg", out_dir=workdir / "o3", quality=20).outputs[0]
    with Image.open(one) as a, Image.open(two) as b:
        assert a.format == b.format == "JPEG" and b.size == (a.size[0] * 2, a.size[1] * 2)
    assert low.stat().st_size < two.stat().st_size


def test_pdf2img_page_selection_rotation_and_sizes(workdir):
    src = make_pdf(workdir / "a.pdf", pages=4, sizes=[(300, 400), (500, 200)], rotations={1: 90})
    r = pdf_to_images(src, dpi=72, pages="2,4")
    assert [p.name for p in r.outputs] == ["a-p2.png", "a-p4.png"]
    sizes = []
    for p in r.outputs:
        with Image.open(p) as img:
            sizes.append(img.size)
    assert sizes == [(200, 500), (500, 200)]  # page 2 is rotated, so shown portrait


def test_pdf2img_folder_rules(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2)
    first = pdf_to_images(src, dpi=50)
    second = pdf_to_images(src, dpi=50)
    assert (
        first.outputs[0].parent.name == "a-pages" and second.outputs[0].parent.name == "a-pages-2"
    )
    target = workdir / "mine"
    pdf_to_images(src, dpi=50, out_dir=target)
    again = pdf_to_images(src, dpi=50, out_dir=target)  # same folder: files are numbered
    assert [p.name for p in again.outputs] == ["a-p1-2.png", "a-p2-2.png"]
    forced = pdf_to_images(src, dpi=50, out_dir=target, force=True)
    assert [p.name for p in forced.outputs] == ["a-p1.png", "a-p2.png"]


def test_pdf2img_errors_and_limits(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2)
    for kwargs, text in [
        ({"fmt": "gif"}, "png or jpg"),
        ({"dpi": 10}, "DPI must be"),
        ({"dpi": 5000}, "DPI must be"),
        ({"quality": 0}, "quality"),
        ({"dpi": 1200}, "megapixels"),
    ]:
        with pytest.raises(InvalidOptionError, match=text):
            pdf_to_images(src, **kwargs)
    with pytest.raises(Exception, match="out of range"):
        pdf_to_images(src, pages="9")
    assert sorted(p.name for p in workdir.iterdir()) == ["a.pdf"]


def test_pdf2img_encrypted_and_corrupt(workdir):
    locked = make_pdf(workdir / "l.pdf", pages=1, password="pw")
    with pytest.raises(PasswordRequiredError):
        pdf_to_images(locked)
    with pytest.raises(WrongPasswordError):
        pdf_to_images(locked, password="bad")
    assert len(pdf_to_images(locked, dpi=50, password="pw").outputs) == 1
    from pdfzi.core.errors import CorruptPdfError

    with pytest.raises(CorruptPdfError):
        pdf_to_images(make_corrupt_pdf(workdir / "bad.pdf"))


def test_pdf2img_cancel_and_failure_cleanup(workdir, monkeypatch):
    src = make_pdf(workdir / "a.pdf", pages=5)
    token = CancelToken()

    def progress(p):
        if p.done == 2:
            token.cancel()

    with pytest.raises(OperationCancelledError):
        pdf_to_images(src, dpi=50, progress=progress, cancel=token)
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]

    real = pdftoimg._render_one
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise OutputWriteError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(pdftoimg, "_render_one", flaky)
    with pytest.raises(OutputWriteError):
        pdf_to_images(src, dpi=50)
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]


def test_round_trip_pdf_images_pdf(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3)
    imgs = pdf_to_images(src, dpi=100).outputs
    back = images_to_pdf(imgs, dest=workdir / "back.pdf").outputs[0]
    assert page_count(back) == 3


# ------------------------------------------------------------------ lock / unlock


def test_lock_requires_password_and_uses_aes256(workdir):
    src = make_pdf(workdir / "ต้นฉบับ.pdf", pages=3)
    snap = Snapshot(src)
    r = lock_pdf(src, "s3cret ลับ")
    out = r.outputs[0]
    assert out.name == "ต้นฉบับ-locked.pdf"
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out)
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out, password="wrong")
    with pikepdf.open(out, password="s3cret ลับ") as pdf:
        assert (pdf.encryption.R, pdf.encryption.bits) == (6, 256)
        assert len(pdf.pages) == 3
    snap.assert_unchanged()
    assert page_text(1) in extract_text(lock_pdf(src, "plain").outputs[0], 1, "plain")  # pdfium too


def test_lock_with_separate_owner_password(workdir):
    src = make_pdf(workdir / "a.pdf", pages=1)
    out = lock_pdf(src, "user", owner_password="owner").outputs[0]
    with pikepdf.open(out, password="owner") as pdf:
        assert pdf.owner_password_matched
    with pikepdf.open(out, password="user") as pdf:
        assert pdf.user_password_matched and not pdf.owner_password_matched


def test_lock_rejects_empty_password_and_handles_protected_input(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2, password="old")
    with pytest.raises(InvalidOptionError, match="empty"):
        lock_pdf(src, "")
    with pytest.raises(PasswordRequiredError):
        lock_pdf(src, "new")
    out = lock_pdf(src, "new", input_password="old").outputs[0]
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(out, password="old")
    assert page_count(out, "new") == 2


def test_unlock_round_trip(workdir):
    src = make_pdf(workdir / "a.pdf", pages=3, bookmarks=True)
    locked = lock_pdf(src, "pw").outputs[0]
    snap = Snapshot(locked)
    r = unlock_pdf(locked, "pw")
    out = r.outputs[0]
    assert out.name == "a-locked-unlocked.pdf"
    with pikepdf.open(out) as pdf:  # no password needed any more
        assert not pdf.is_encrypted and len(pdf.pages) == 3
    assert page_text(2) in extract_text(out, 2)
    snap.assert_unchanged()


def test_unlock_errors(workdir):
    plain = make_pdf(workdir / "plain.pdf")
    with pytest.raises(NotEncryptedError, match="not password-protected"):
        unlock_pdf(plain, "x")
    locked = make_pdf(workdir / "l.pdf", password="pw")
    with pytest.raises(WrongPasswordError):
        unlock_pdf(locked, "nope")
    with pytest.raises(InvalidOptionError, match="empty"):
        unlock_pdf(locked, "")
    assert sorted(p.name for p in workdir.iterdir()) == ["l.pdf", "plain.pdf"]


def make_restricted(path: Path, owner="own"):
    """Opens without a password but forbids printing/copying: only the owner may lift that."""
    src = make_pdf(path.with_name("src-" + path.name), pages=2)
    with pikepdf.open(src) as pdf:
        pdf.save(
            path,
            encryption=pikepdf.Encryption(
                user="",
                owner=owner,
                R=6,
                allow=pikepdf.Permissions(extract=False, print_lowres=False, print_highres=False),
            ),
        )
    return path


def test_unlock_does_not_strip_restrictions_without_the_owner_password(workdir):
    restricted = make_restricted(workdir / "r.pdf")
    for guess in ("whatever", "own-not"):
        with pytest.raises((OwnerPasswordRequiredError, WrongPasswordError)):
            unlock_pdf(restricted, guess)
    assert not (workdir / "r-unlocked.pdf").exists()
    out = unlock_pdf(restricted, "own").outputs[0]
    with pikepdf.open(out) as pdf:
        assert not pdf.is_encrypted


# ------------------------------------------------------------------ clean


def marked_pdf(path: Path) -> Path:
    src = make_pdf(path, pages=2)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        with pdf.open_metadata() as meta:
            meta["dc:creator"] = ["Alice Secret"]
        pdf.docinfo["/Author"] = "Alice Secret"
        pdf.docinfo["/Title"] = "Private plans"
        pdf.docinfo["/Producer"] = "SecretWriter 1.0"
        page = pdf.pages[0]
        page.obj["/PieceInfo"] = pikepdf.Dictionary(App="Alice Secret")
        page.obj["/LastModified"] = pikepdf.String("D:20200101000000Z")
        pdf.save(src)
    return src


def stream_bytes(path: Path) -> bytes:
    chunks = [path.read_bytes()]
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                with contextlib.suppress(pikepdf.PdfError):
                    chunks.append(obj.read_bytes())
    return b"\n".join(chunks)


def test_clean_removes_info_xmp_and_private_entries(workdir):
    src = marked_pdf(workdir / "ลับ.pdf")
    assert b"Alice Secret" in stream_bytes(src)
    snap = Snapshot(src)
    r = clean_pdf(src)
    out = r.outputs[0]
    assert out.name == "ลับ-clean.pdf"
    assert r.info_fields == ["Author", "Producer", "Title"] and r.xmp_removed
    assert r.other_entries_removed >= 2
    assert b"Alice Secret" not in stream_bytes(out) and b"SecretWriter" not in stream_bytes(out)
    with pikepdf.open(out) as pdf:
        assert "/Info" not in pdf.trailer and "/Metadata" not in pdf.Root
        assert "/PieceInfo" not in pdf.pages[0].obj and "/LastModified" not in pdf.pages[0].obj
        assert len(pdf.pages) == 2
    assert page_text(0) in extract_text(out, 0)
    assert any("Author" in n for n in r.notes) and any("XMP" in n for n in r.notes)
    snap.assert_unchanged()


def jpeg_with_gps(size=(200, 150)) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (13.0, 45.0, 0.0), "E", (100.0, 30.0, 0.0)
    buf = io.BytesIO()
    photo_image(*size, seed=4).save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def test_strip_jpeg_metadata_unit():
    data = jpeg_with_gps()
    assert b"SecretCam" in data
    stripped = strip_jpeg_metadata(data)
    assert stripped is not None and b"SecretCam" not in stripped and len(stripped) < len(data)
    with Image.open(io.BytesIO(data)) as a, Image.open(io.BytesIO(stripped)) as b:
        assert a.tobytes() == b.tobytes() and not b.getexif()
    assert strip_jpeg_metadata(stripped) is None  # nothing left to strip
    assert strip_jpeg_metadata(b"not a jpeg") is None
    assert strip_jpeg_metadata(data[:50]) is None  # truncated: leave it alone


def test_strip_keeps_adobe_marker_of_cmyk_jpegs():
    buf = io.BytesIO()
    photo_image(64, 64).convert("CMYK").save(buf, "JPEG")
    data = buf.getvalue()
    exif_segment = b"\xff\xe1\x00\x0aExif\x00\x00ab"
    injected = data[:2] + exif_segment + data[2:]
    stripped = strip_jpeg_metadata(injected)
    assert stripped is not None and b"\xff\xee" in stripped  # APP14 'Adobe' kept
    with Image.open(io.BytesIO(stripped)) as img:
        img.load()
        assert img.mode == "CMYK"


def test_clean_strips_gps_from_embedded_photos_without_touching_pixels(workdir):
    src = make_pdf(workdir / "photo.pdf", pages=1)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        data = jpeg_with_gps()
        img = pdf.make_stream(
            data, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image, Width=200, Height=150,
            ColorSpace=pikepdf.Name.DeviceRGB, BitsPerComponent=8, Filter=pikepdf.Name.DCTDecode,
        )  # fmt: skip
        pdf.pages[0].Resources.XObject = pikepdf.Dictionary(Im0=img)
        pdf.pages[0].Contents = pdf.make_stream(b"q 100 0 0 75 50 50 cm /Im0 Do Q")
        pdf.save(src)
    assert b"SecretCam" in stream_bytes(src)
    r = clean_pdf(src)
    assert r.images_cleaned == 1 and any("EXIF" in n for n in r.notes)
    assert b"SecretCam" not in stream_bytes(r.outputs[0])
    with pikepdf.open(r.outputs[0]) as pdf:
        raw = next(iter_images(pdf)).read_raw_bytes()
    with Image.open(io.BytesIO(data)) as a, Image.open(io.BytesIO(raw)) as b:
        assert a.tobytes() == b.tobytes()


def test_clean_with_nothing_to_remove_writes_nothing(workdir):
    src = make_pdf(workdir / "a.pdf")
    r = clean_pdf(src)
    assert r.nothing_found and r.outputs == [] and "No metadata found" in r.notes[0]
    assert [p.name for p in workdir.iterdir()] == ["a.pdf"]


def test_clean_encrypted_input(workdir):
    src = marked_pdf(workdir / "a.pdf")
    locked = lock_pdf(src, "pw").outputs[0]
    with pytest.raises(PasswordRequiredError):
        clean_pdf(locked)
    r = clean_pdf(locked, password="pw")
    assert any("password-protected" in n for n in r.notes)
    assert page_count(r.outputs[0]) == 2


def test_clean_force_in_place(workdir):
    src = marked_pdf(workdir / "a.pdf")
    clean_pdf(src, dest=src, force=True)
    assert b"Alice Secret" not in stream_bytes(src) and leftovers(workdir) == []


# ------------------------------------------------------------------ CLI


def test_cli_img2pdf_and_pdf2img(workdir):
    save_images(workdir)
    r = run("img2pdf", workdir / "*.png", workdir / "*.jpg", "-o", workdir / "all.pdf")
    assert r.exit_code == 0, r.output
    assert "Combined 2 image(s): pic.png, photo ภาพ.jpg" in r.output and "2 pages" in r.output
    r = run("pdf2img", workdir / "all.pdf", "--dpi", "50", "--format", "jpg", "-o", workdir / "o")
    assert r.exit_code == 0 and len(list((workdir / "o").glob("*.jpg"))) == 2
    r = run("img2pdf", workdir / "nope.png")
    assert r.exit_code == 1 and "File not found" in r.output
    r = run("img2pdf", workdir / "all.pdf")  # a PDF is not an image
    assert r.exit_code == 1 and "not a readable image" in r.output and "Traceback" not in r.output


def test_cli_lock_unlock_with_prompt_option_and_env(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2)
    r = run("lock", src, input="pw\npw\n")  # asked, twice
    assert r.exit_code == 0 and "AES-256" in r.output
    assert page_count(workdir / "a-locked.pdf", "pw") == 2
    r = run("unlock", workdir / "a-locked.pdf", "--password", "pw")
    assert r.exit_code == 0 and page_count(workdir / "a-locked-unlocked.pdf") == 2
    r = run("unlock", workdir / "a-locked.pdf", env={"PDFZI_PASSWORD": "bad"})
    assert r.exit_code == 1 and "password is incorrect" in r.output
    r = run("unlock", workdir / "a-locked.pdf", input="")  # nothing to read
    assert r.exit_code == 1 and "password is required" in r.output
    r = run("lock", src, "--password", "x", "--json", "-o", workdir / "j.pdf")
    assert json.loads(r.output)[0]["ok"] is True


def test_cli_clean_and_batch_errors(workdir):
    marked = marked_pdf(workdir / "m.pdf")
    plain = make_pdf(workdir / "p.pdf")
    bad = make_corrupt_pdf(workdir / "x.pdf")
    r = run("clean", marked, plain, bad)
    assert r.exit_code == 1
    assert "[ok] m.pdf -> m-clean.pdf" in r.output and "Removed document info: Author" in r.output
    assert "[ok] p.pdf: left unchanged" in r.output
    assert "[error] x.pdf: this file is not a valid PDF" in r.output


@pytest.mark.parametrize(
    "args",
    [
        ("lock", "--password", "pw"),
        ("clean",),
        ("pdf2img", "--dpi", "40"),
    ],
)
def test_cli_phase4_commands_never_modify_inputs(workdir, args):
    src = marked_pdf(workdir / "ต้นฉบับ one.pdf")
    snap = Snapshot(src)
    cmd, *rest = args
    assert run(cmd, src, *rest).exit_code == 0
    snap.assert_unchanged()
