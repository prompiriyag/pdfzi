"""compress: every level, --target, skipping rules, and the never-bigger guarantee."""

from __future__ import annotations

import io
import json

import pikepdf
import pytest
from PIL import Image
from typer.testing import CliRunner

from conftest import TRICKY_NAMES
from pdfgen import (
    IMAGE_KINDS,
    Snapshot,
    extract_text,
    make_corrupt_pdf,
    make_image_pdf,
    make_pdf,
    mean_abs_diff,
    page_count,
    page_text,
    render_page,
)
from pdfzi import cli
from pdfzi.core import compress as compress_mod
from pdfzi.core.compress import LADDER, MAX_ATTEMPTS, compress_pdf
from pdfzi.core.errors import (
    CorruptPdfError,
    InvalidOptionError,
    OperationCancelledError,
    PasswordRequiredError,
)
from pdfzi.core.fmt import parse_size
from pdfzi.core.pdfimages import iter_images
from pdfzi.core.progress import CancelToken

runner = CliRunner()


def image_info(path):
    with pikepdf.open(path) as pdf:
        return [
            (int(o.Width), int(o.Height), _colourspace(o), _filter(o), _payload(o))
            for o in iter_images(pdf)
        ]


def _payload(obj):
    """Image data as it will be displayed: decoded pixels, or the JPEG for DCT images."""
    if _filter(obj) in ("/DCTDecode", "/JBIG2Decode", "/JPXDecode"):
        return bytes(obj.read_raw_bytes())
    return bytes(obj.read_bytes())


def _colourspace(obj):
    cs = obj.get("/ColorSpace")
    if cs is None:
        return None
    if isinstance(cs, pikepdf.Name):
        return str(cs)
    return f"ICC{int(cs[1].N)}" if cs[0] == pikepdf.Name.ICCBased else str(cs[0])


def _filter(obj):
    f = obj.get("/Filter")
    return None if f is None else str(f)


def assert_text_intact(path, pages):
    for i in range(pages):
        assert page_text(i) in extract_text(path, i)


def bloat(path):
    """Re-save without any stream compression, so lossless has real work to do."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.save(path, compress_streams=False, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return path


# ------------------------------------------------------------------ lossless


def test_lossless_shrinks_structure_and_changes_nothing_visible(workdir):
    src = bloat(make_pdf(workdir / "bloated.pdf", pages=30))
    snap = Snapshot(src)
    r = compress_pdf(src)
    assert not r.kept_original and r.bytes_out < r.bytes_in
    out = r.outputs[0]
    assert out == workdir / "bloated-compressed.pdf"
    assert page_count(out) == 30 == r.pages_out
    assert_text_intact(out, 30)
    assert mean_abs_diff(render_page(src, 3), render_page(out, 3)) == 0
    assert r.settings == "lossless" and r.images_recompressed == 0
    snap.assert_unchanged()


def test_lossless_leaves_image_streams_byte_identical(workdir):
    src = bloat(make_pdf(workdir / "a.pdf", pages=40, images_per_page=1, image_size=(160, 120)))
    out = compress_pdf(src).outputs[0]
    before = [(w, h, cs, f, data) for w, h, cs, f, data in image_info(src)]
    after = [(w, h, cs, f, data) for w, h, cs, f, data in image_info(out)]
    assert before == after


def test_a_saving_under_one_percent_is_not_written(photos, workdir):
    """Big JPEG images dominate the file: lossless can only save a sliver, so no new file."""
    snap = Snapshot(photos)
    r = compress_pdf(photos)
    assert r.kept_original and not r.outputs and r.bytes_out == r.bytes_in
    assert [p.name for p in workdir.iterdir()] == [photos.name]
    assert any("left unchanged" in n for n in r.notes)
    snap.assert_unchanged()


def test_the_minimum_saving_is_one_percent_and_is_explained(workdir, monkeypatch):
    src = bloat(make_pdf(workdir / "b.pdf", pages=30))
    assert compress_mod.MIN_SAVING == 0.01
    monkeypatch.setattr(compress_mod, "MIN_SAVING", 0.99)  # pretend 99 % is required
    r = compress_pdf(src)
    assert r.kept_original and not r.outputs
    assert any("saves less than 99%" in n and "left unchanged" in n for n in r.notes)


def test_a_reached_target_is_written_even_if_the_saving_is_small(workdir, monkeypatch):
    src = bloat(make_pdf(workdir / "c.pdf", pages=30))
    monkeypatch.setattr(compress_mod, "MIN_SAVING", 0.99)
    r = compress_pdf(src, target=int(src.stat().st_size * 0.9))
    assert r.target_reached is True and r.outputs and not r.kept_original


def test_lossless_removes_unused_resources(workdir):
    src = make_pdf(workdir / "a.pdf", pages=1)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Resources.XObject = pikepdf.Dictionary(
            Unused=make_image_obj(pdf)  # never painted by the content stream
        )
        pdf.save(src)
    assert len(image_info(src)) == 1
    r = compress_pdf(src)
    assert r.outputs and len(image_info(r.outputs[0])) == 0
    assert r.bytes_out < r.bytes_in
    assert_text_intact(r.outputs[0], 1)


def make_image_obj(pdf):
    from pdfgen import make_image

    return make_image(pdf, "flate-rgb", 300, 300)


# ------------------------------------------------------------------ medium / high


@pytest.fixture
def photos(workdir):
    return make_pdf(
        workdir / "ภาพถ่าย photos.pdf", pages=3, images_per_page=1, image_size=(3000, 2000)
    )


@pytest.mark.parametrize(("level", "dpi", "max_ratio"), [("medium", 170, 0.6), ("high", 120, 0.35)])
def test_lossy_levels_downscale_and_shrink(photos, level, dpi, max_ratio):
    snap = Snapshot(photos)
    r = compress_pdf(photos, level=level)
    out = r.outputs[0]
    assert r.bytes_out <= r.bytes_in * max_ratio
    assert page_count(out) == 3
    assert_text_intact(out, 3)  # text is still text: pages were not rasterised
    for w, h, cs, flt, _ in image_info(out):
        # drawn 300 x 225 pt (4.17 x 3.12 in): scaled to the dpi, from 3000 x 2000 pixels
        assert w < 3000 and h < 2000
        assert w / (300 / 72) >= dpi * 0.97 and h / (225 / 72) >= dpi * 0.97
        assert flt == "/DCTDecode" and cs == "/DeviceRGB"
    assert (r.images_found, r.images_recompressed) == (3, 3)
    assert mean_abs_diff(render_page(photos, 0, 0.4), render_page(out, 0, 0.4)) < 8
    snap.assert_unchanged()


def test_high_is_smaller_than_medium(photos):
    medium = compress_pdf(photos, level="medium")
    high = compress_pdf(photos, level="high")
    assert high.bytes_out < medium.bytes_out


def test_recompressed_images_decode_with_matching_dimensions(photos):
    out = compress_pdf(photos, level="high").outputs[0]
    for w, h, _cs, _f, raw in image_info(out):
        with Image.open(io.BytesIO(raw)) as img:
            assert img.size == (w, h)


def test_shared_image_is_compressed_once(workdir):
    src = make_pdf(
        workdir / "logo.pdf", pages=6, images_per_page=1, share_image=True, image_size=(2400, 1800)
    )
    r = compress_pdf(src, level="medium")
    assert (r.images_found, r.images_recompressed) == (1, 1)
    assert len(image_info(r.outputs[0])) == 1
    assert r.bytes_out < r.bytes_in


def test_grayscale(photos):
    r = compress_pdf(photos, level="high", grayscale=True)
    assert {cs for _, _, cs, _, _ in image_info(r.outputs[0])} == {"/DeviceGray"}
    colour = compress_pdf(photos, level="high")
    assert r.bytes_out < colour.bytes_out
    page = render_page(r.outputs[0], 0, 0.3)
    px = page.getpixel((page.width // 4, page.height * 3 // 10))
    assert abs(px[0] - px[1]) < 6 and abs(px[1] - px[2]) < 6  # a grey pixel inside the picture


def test_grayscale_requires_a_lossy_level(workdir):
    src = make_pdf(workdir / "a.pdf")
    with pytest.raises(InvalidOptionError, match="--grayscale needs"):
        compress_pdf(src, grayscale=True)


# ------------------------------------------------------------------ which images are touched


COMPRESSIBLE = ["dct-rgb", "dct-gray", "flate-rgb", "flate-gray", "flate-cmyk", "icc-rgb"]
SKIPPED = ["smask", "stencil", "one-bit", "indexed", "jbig2", "jpx"]


@pytest.mark.parametrize("kind", COMPRESSIBLE)
def test_supported_image_kinds_are_recompressed_and_look_the_same(workdir, kind):
    src = make_image_pdf(workdir / "k.pdf", [kind], size=(1500, 1000))
    r = compress_pdf(src, level="medium")
    assert (r.images_found, r.images_recompressed) == (1, 1), r.images_skipped
    before, after = image_info(src)[0], image_info(r.outputs[0])[0]
    assert after[3] == "/DCTDecode" and len(after[4]) < len(before[4])
    assert after[2] == before[2]  # colour space (incl. ICC profile) kept
    assert mean_abs_diff(render_page(src, 0, 0.4), render_page(r.outputs[0], 0, 0.4)) < 3


@pytest.mark.parametrize("kind", SKIPPED)
def test_unsupported_image_kinds_are_left_byte_for_byte(workdir, kind):
    src = make_image_pdf(workdir / "k.pdf", [kind, "flate-rgb"], size=(1200, 800))
    r = compress_pdf(src, level="high")
    assert (r.images_found, r.images_recompressed) == (2, 1)
    assert sum(r.images_skipped.values()) == 1
    before, after = image_info(src), image_info(r.outputs[0])
    assert before[0] == after[0]  # the unsupported one is untouched
    assert after[1][3] == "/DCTDecode"
    assert mean_abs_diff(render_page(src, 0, 0.4), render_page(r.outputs[0], 0, 0.4)) == 0


def test_all_kinds_together_still_open_and_render(workdir):
    src = make_image_pdf(workdir / "k.pdf", list(IMAGE_KINDS), size=(900, 700))
    r = compress_pdf(src, level="high")
    assert page_count(r.outputs[0]) == len(IMAGE_KINDS)
    for i in range(len(IMAGE_KINDS)):
        assert page_text(i) in extract_text(r.outputs[0], i)
        assert mean_abs_diff(render_page(src, i, 0.3), render_page(r.outputs[0], i, 0.3)) < 4


def test_image_that_would_get_bigger_is_skipped(workdir):
    src = make_image_pdf(workdir / "k.pdf", ["dct-rgb"], size=(200, 150))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        img = next(iter_images(pdf))
        img.write(img.read_raw_bytes(), filter=pikepdf.Name.DCTDecode)  # same bytes
        pdf.save(src)
    # Re-encoding an already tiny JPEG at q60 cannot beat it by enough; whatever happens,
    # the invariant must hold: never bigger.
    r = compress_pdf(src, level="high")
    assert r.kept_original or r.bytes_out < r.bytes_in


# ------------------------------------------------------------------ never bigger


@pytest.mark.parametrize("level", ["lossless", "medium", "high"])
def test_compressing_twice_never_grows_the_file(workdir, level):
    src = make_pdf(workdir / "a.pdf", pages=4, images_per_page=1, image_size=(1000, 800))
    first = compress_pdf(src, level=level)
    assert first.kept_original or first.bytes_out < first.bytes_in
    current = first.outputs[0] if first.outputs else src
    second = compress_pdf(current, level=level)
    assert second.kept_original or second.bytes_out < second.bytes_in


def test_keeps_original_and_says_so_when_nothing_helps(workdir):
    src = make_pdf(workdir / "tiny.pdf", pages=1)
    first = compress_pdf(src)
    base = first.outputs[0] if first.outputs else src
    again = compress_pdf(base)
    assert again.kept_original and again.outputs == [] and again.bytes_out == again.bytes_in
    assert "left unchanged" in again.notes[0]
    assert not (workdir / f"{base.stem}-compressed.pdf").exists()


def test_unchanged_result_never_leaves_files_behind(workdir):
    src = make_pdf(workdir / "tiny.pdf", pages=1)
    base = compress_pdf(src)
    base_file = base.outputs[0] if base.outputs else src
    before = sorted(p.name for p in workdir.iterdir())
    compress_pdf(base_file)
    assert sorted(p.name for p in workdir.iterdir()) == before


# ------------------------------------------------------------------ --target


@pytest.fixture
def big(workdir):
    return make_pdf(workdir / "big.pdf", pages=4, images_per_page=1, image_size=(2400, 1800))


def test_target_is_reached_with_the_gentlest_settings_that_work(big):
    size = big.stat().st_size
    target = int(size * 0.5)
    r = compress_pdf(big, target=target)
    assert r.target_reached is True and r.bytes_out <= target
    assert 1 < r.attempts <= MAX_ATTEMPTS
    assert r.target_bytes == target
    assert page_count(r.outputs[0]) == 4
    assert_text_intact(r.outputs[0], 4)
    # a looser target must not need harsher settings than a tighter one
    loose = compress_pdf(big, target=int(size * 0.9))
    assert loose.attempts <= r.attempts
    assert loose.bytes_out >= r.bytes_out * 0.9


def test_each_attempt_starts_from_the_original(big, monkeypatch):
    seen = []
    real = compress_mod._attempt

    def spy(source, *args, **kwargs):
        seen.append(source)
        return real(source, *args, **kwargs)

    monkeypatch.setattr(compress_mod, "_attempt", spy)
    r = compress_pdf(big, target=1)  # impossible: runs every rung
    assert len(seen) == r.attempts == MAX_ATTEMPTS
    assert set(seen) == {big}


def test_unreachable_target_returns_smallest_and_says_so(big):
    r = compress_pdf(big, target=500)
    assert r.target_reached is False and r.attempts == MAX_ATTEMPTS
    assert r.bytes_out < r.bytes_in
    assert r.settings == LADDER[-1].describe()
    assert any("not reached" in n for n in r.notes)
    assert r.outputs and page_count(r.outputs[0]) == 4


def test_target_above_current_size_changes_nothing(big):
    r = compress_pdf(big, target=big.stat().st_size * 2)
    assert r.kept_original and r.target_reached is True and r.outputs == []
    assert "already within" in r.notes[0]


def test_target_cannot_be_mixed_with_level(big):
    with pytest.raises(InvalidOptionError, match="picks the level"):
        compress_pdf(big, level="high", target=1000)
    with pytest.raises(InvalidOptionError):
        compress_pdf(big, target=0)


def test_target_with_grayscale(big):
    r = compress_pdf(big, target=int(big.stat().st_size * 0.2), grayscale=True)
    assert r.target_reached and r.grayscale


@pytest.mark.parametrize(
    ("text", "value"),
    [("2MB", 2 * 1024**2), ("500 kb", 500 * 1024), ("1.5m", int(1.5 * 1024**2)),
     ("1GiB", 1024**3), ("4096", 4096), (" 10K ", 10 * 1024)],
)  # fmt: skip
def test_parse_size(text, value):
    assert parse_size(text) == value


def test_parse_size_default_unit_applies_only_to_a_bare_number():
    assert parse_size("2", default_unit="m") == 2 * 1024**2
    assert parse_size("1.5", default_unit="M") == int(1.5 * 1024**2)
    assert parse_size("500KB", default_unit="m") == 500 * 1024  # a given unit always wins
    assert parse_size("2") == 2  # no default: bytes, as before
    assert parse_size("300B", default_unit="m") == 300  # an explicit B means bytes


@pytest.mark.parametrize("text", ["", "abc", "MB", "-5MB", "0", "1.2.3MB", "5XB", "2,5MB"])
def test_parse_size_rejects_nonsense(text):
    with pytest.raises(InvalidOptionError):
        parse_size(text)


# ------------------------------------------------------------------ robustness


def test_invalid_level(workdir):
    with pytest.raises(InvalidOptionError, match="Unknown level"):
        compress_pdf(make_pdf(workdir / "a.pdf"), level="extreme")


def test_encrypted_input(workdir):
    src = make_pdf(
        workdir / "a.pdf", pages=2, password="pw", images_per_page=1, image_size=(1500, 1000)
    )
    with pytest.raises(PasswordRequiredError):
        compress_pdf(src, level="medium")
    r = compress_pdf(src, level="medium", password="pw")
    assert page_count(r.outputs[0]) == 2
    assert any("password-protected" in n for n in r.notes)
    assert r.bytes_out < r.bytes_in


def test_corrupt_input(workdir):
    bad = make_corrupt_pdf(workdir / "bad.pdf")
    with pytest.raises(CorruptPdfError):
        compress_pdf(bad)
    assert [p.name for p in workdir.iterdir()] == ["bad.pdf"]


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_unicode_and_spaced_names(workdir, name):
    src = make_pdf(workdir / name, pages=2, images_per_page=1, image_size=(1600, 1200))
    snap = Snapshot(src)
    r = compress_pdf(src, level="medium")
    assert r.outputs[0].name == src.stem + "-compressed.pdf"
    snap.assert_unchanged()


def test_mixed_sizes_and_rotation_survive(workdir):
    src = make_pdf(
        workdir / "a.pdf", pages=3, sizes=[(300, 400), (500, 200)], rotations={1: 90, 2: 180},
        images_per_page=1, image_size=(1600, 1200),
    )  # fmt: skip
    out = compress_pdf(src, level="medium").outputs[0]
    with pikepdf.open(out) as pdf:
        assert [float(p.mediabox[2]) for p in pdf.pages] == [300, 500, 300]
        assert [int(p.obj.get("/Rotate", 0)) for p in pdf.pages] == [0, 90, 180]


def test_second_run_is_numbered_and_force_overwrites(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2, images_per_page=1, image_size=(1600, 1200))
    first = compress_pdf(src, level="medium").outputs[0]
    snap = Snapshot(first)
    second = compress_pdf(src, level="high").outputs[0]
    assert second.name == "a-compressed-2.pdf"
    snap.assert_unchanged()
    forced = compress_pdf(src, level="high", dest=first, force=True).outputs[0]
    assert forced == first and first.stat().st_size == second.stat().st_size


def test_force_can_replace_the_original_in_place(workdir):
    src = make_pdf(workdir / "a.pdf", pages=2, images_per_page=1, image_size=(1600, 1200))
    before = src.stat().st_size
    r = compress_pdf(src, level="medium", dest=src, force=True)
    assert r.outputs == [src] and src.stat().st_size < before
    assert page_count(src) == 2
    assert not list(workdir.glob("*.tmp"))


def test_progress_reports_are_ordered_and_bounded(photos):
    seen = []
    compress_pdf(photos, level="medium", progress=seen.append)
    assert seen and all(0 <= p.done <= p.total for p in seen)
    assert [p.done for p in seen] == sorted(p.done for p in seen)
    assert seen[-1].done == seen[-1].total


def test_cancel_leaves_nothing_behind(photos, workdir):
    token = CancelToken()

    def progress(p):
        if "scaled down" in p.message and p.done > 0:
            token.cancel()

    with pytest.raises(OperationCancelledError):
        compress_pdf(photos, level="medium", progress=progress, cancel=token)
    assert [p.name for p in workdir.iterdir()] == [photos.name]


def test_result_is_json_serialisable(photos):
    data = compress_pdf(photos, level="medium").to_dict()
    json.dumps(data)
    assert data["images_recompressed"] == 3 and data["level"] == "medium"


# ------------------------------------------------------------------ CLI


def run(*args, env=None):
    return runner.invoke(cli.app, [str(a) for a in args], env=env)


def test_cli_compress_levels_and_summary(photos, workdir):
    r = run("compress", photos, "--level", "medium")
    assert r.exit_code == 0, r.output
    assert "[ok] ภาพถ่าย photos.pdf -> ภาพถ่าย photos-compressed.pdf" in r.output
    assert "smaller)" in r.output and "Images: 3 of 3 recompressed" in r.output


def test_cli_target_json_and_unreached(big):
    r = run("compress", big, "--target", "300B", "--json")
    data = json.loads(r.output)
    assert r.exit_code == 0
    assert (
        data[0]["result"]["target_reached"] is False
        and data[0]["result"]["attempts"] == MAX_ATTEMPTS
    )


def test_cli_target_without_a_unit_means_megabytes(big):
    r = run("compress", big, "--target", "2", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["result"]["target_bytes"] == 2 * 1024**2


def test_cli_left_unchanged_message(workdir):
    src = make_pdf(workdir / "tiny.pdf", pages=1)
    first = compress_pdf(src)
    base = first.outputs[0] if first.outputs else src
    r = run("compress", base)
    assert r.exit_code == 0 and "left unchanged" in r.output


def test_cli_bad_options_are_clean_errors(workdir):
    src = make_pdf(workdir / "a.pdf")
    for args, text in [
        (["--target", "abc"], "Cannot understand size"),
        (["--level", "ultra"], "Unknown level"),
        (["--grayscale"], "--grayscale needs"),
        (["--level", "high", "--target", "1MB"], "picks the level"),
    ]:
        r = run("compress", src, *args)
        assert r.exit_code == 1 and text in r.output and "Traceback" not in r.output, args


def test_cli_batch_with_a_corrupt_file(workdir):
    good = make_pdf(workdir / "good.pdf", pages=2, images_per_page=1, image_size=(1600, 1200))
    bad = make_corrupt_pdf(workdir / "bad.pdf")
    r = run("compress", workdir / "*.pdf", "--level", "medium")
    assert r.exit_code == 1
    assert (
        "[ok] good.pdf" in r.output and "[error] bad.pdf: this file is not a valid PDF" in r.output
    )
    assert good.exists() and bad.exists()
