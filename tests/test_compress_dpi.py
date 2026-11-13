"""Compress sizes images by how big they are drawn on the page (dpi), not by pixel count."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name, Pdf

from pdfgen import make_image
from pdfzi.core.compress import PROFILES, Profile, _scaled_size, compress_pdf
from pdfzi.core.pdfimages import image_extents


def build(
    path: Path, draws, *, form: bool = False, unplaced: tuple[int, int] | None = None
) -> Path:
    """One page. ``draws`` is a list of (px_w, px_h, pt_w, pt_h): one image drawn at that size.

    ``form`` draws them from inside a form XObject (scaled by the form's own matrix).
    ``unplaced`` adds an image that is in the page resources but never drawn.
    """
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    xobjects = Dictionary()
    ops = []
    for i, (pw, ph, tw, th) in enumerate(draws):
        xobjects[f"/Im{i}"] = make_image(pdf, "dct-rgb", pw, ph, seed=i + 1)
        ops.append(b"q %f 0 0 %f 72 %d cm /Im%d Do Q" % (tw, th, 72 + 200 * i, i))
    if unplaced:
        xobjects["/Spare"] = make_image(pdf, "dct-rgb", *unplaced, seed=9)
    if form:
        inner = pdf.make_stream(
            b"\n".join(ops), Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 612, 792],
            Matrix=[0.5, 0, 0, 0.5, 0, 0], Resources=Dictionary(XObject=xobjects),
        )  # fmt: skip
        page.Resources = Dictionary(XObject=Dictionary(Fm0=inner))
        page.Contents = pdf.make_stream(b"/Fm0 Do")
    else:
        page.Resources = Dictionary(XObject=xobjects)
        page.Contents = pdf.make_stream(b"\n".join(ops))
    pdf.save(path)
    return path


def sizes(path: Path) -> list[tuple[int, int]]:
    from pdfzi.core.pdfimages import iter_images

    with pikepdf.open(path) as pdf:
        return [(int(o.Width), int(o.Height)) for o in iter_images(pdf)]


# ------------------------------------------------------------------ where images are drawn


def test_extents_follow_cm_and_report_inches(tmp_path):
    src = build(tmp_path / "a.pdf", [(1000, 500, 360, 180)])  # 5 x 2.5 inch
    with pikepdf.open(src) as pdf:
        ((w, h),) = image_extents(pdf).values()
    assert (round(w, 3), round(h, 3)) == (5.0, 2.5)


def test_extents_use_the_largest_use_of_a_shared_image(tmp_path):
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    img = make_image(pdf, "dct-rgb", 800, 600)
    page.Resources = Dictionary(XObject=Dictionary(Im=img))
    page.Contents = pdf.make_stream(
        b"q 72 0 0 54 10 10 cm /Im Do Q q 288 0 0 216 10 100 cm /Im Do Q"  # 1 inch, then 4 inch
    )
    ((w, h),) = image_extents(pdf).values()
    assert (round(w, 3), round(h, 3)) == (4.0, 3.0)


def test_extents_apply_the_form_matrix_and_nested_q(tmp_path):
    src = build(tmp_path / "f.pdf", [(1000, 500, 360, 180)], form=True)  # form halves it
    with pikepdf.open(src) as pdf:
        ((w, h),) = image_extents(pdf).values()
    assert (round(w, 3), round(h, 3)) == (2.5, 1.25)


def test_extents_ignore_images_that_are_never_drawn(tmp_path):
    src = build(tmp_path / "u.pdf", [(1000, 500, 360, 180)], unplaced=(300, 300))
    with pikepdf.open(src) as pdf:
        assert len(image_extents(pdf)) == 1


def test_extents_survive_garbled_content(tmp_path):
    pdf = Pdf.new()
    page = pdf.add_blank_page()
    page.Resources = Dictionary(XObject=Dictionary(Im=make_image(pdf, "dct-rgb", 50, 50)))
    page.Contents = pdf.make_stream(b"q 1 2 cm cm cm /Im Do Q Q Q [[[ (")
    assert isinstance(image_extents(pdf), dict)  # broken operators are skipped, no crash


# ------------------------------------------------------------------ the size rule


def test_scaled_size_targets_the_dpi_where_the_image_is_drawn():
    medium = PROFILES["medium"]  # 170 dpi
    assert _scaled_size(3400, 2200, (10.0, 6.0), medium) == (1700, 1100)  # shown at 340 dpi: half
    assert _scaled_size(3400, 2200, (20.0, 12.0), medium) == (3400, 2200)  # already 170: untouched


def test_scaled_size_never_enlarges_and_skips_tiny_savings():
    medium = PROFILES["medium"]
    assert _scaled_size(500, 400, (10.0, 8.0), medium) == (500, 400)  # only 50 dpi
    assert _scaled_size(1000, 1000, (5.5, 5.5), medium) == (1000, 1000)  # 182 dpi: <15 % saving
    assert _scaled_size(1200, 1200, (5.0, 5.0), medium) == (850, 850)  # 240 dpi: scaled


def test_scaled_size_keeps_aspect_and_both_axes_at_the_dpi():
    # drawn stretched (5 x 2 inch from a 1000 x 1000 image): the sharper axis decides
    w, h = _scaled_size(1000, 1000, (5.0, 2.0), PROFILES["medium"])
    assert (w == h and w >= 850 and w <= 1000) or w == 850
    assert (w / 5.0 >= 169 and h / 2.0 >= 169) or w == 850


def test_scaled_size_falls_back_to_pixels_when_the_drawn_size_is_unknown():
    assert _scaled_size(4000, 3000, None, PROFILES["medium"]) == (2000, 1500)
    assert _scaled_size(1500, 1000, None, PROFILES["medium"]) == (1500, 1000)
    assert _scaled_size(4000, 3000, None, Profile("x", 100, 50)) == (4000, 3000)  # no pixel limit


# ------------------------------------------------------------------ end to end


def test_a_small_picture_with_many_pixels_is_shrunk_far_more_than_a_pixel_cap_would(tmp_path):
    """1800 x 1200 drawn 2 x 1.33 inch is 900 dpi. The old 2000 px cap would keep it whole."""
    src = build(tmp_path / "small.pdf", [(1800, 1200, 144, 96)])
    out = compress_pdf(src, level="medium").outputs[0]
    ((w, h),) = sizes(out)
    assert (w, h) == (340, 227)  # 170 dpi over 2 inch


def test_a_picture_drawn_large_keeps_the_pixels_it_needs(tmp_path):
    """The same image drawn 8 x 5.33 inch is 225 dpi: scaled to 170 dpi, not to a fixed cap."""
    src = build(tmp_path / "big.pdf", [(1800, 1200, 576, 384)])
    out = compress_pdf(src, level="medium").outputs[0]
    ((w, h),) = sizes(out)
    assert (w, h) == (1360, 907)


def test_high_goes_lower_than_medium_for_the_same_drawing(tmp_path):
    src = build(tmp_path / "x.pdf", [(1800, 1200, 576, 384)])
    medium = sizes(compress_pdf(src, level="medium", dest=tmp_path / "m.pdf").outputs[0])[0]
    high = sizes(compress_pdf(src, level="high", dest=tmp_path / "h.pdf").outputs[0])[0]
    assert high[0] < medium[0] and high == (960, 640)


def test_a_shared_image_keeps_enough_pixels_for_its_largest_use(tmp_path):
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    img = make_image(pdf, "dct-rgb", 2400, 1600)
    page.Resources = Dictionary(XObject=Dictionary(Im=img))
    page.Contents = pdf.make_stream(
        b"q 72 0 0 48 10 10 cm /Im Do Q q 432 0 0 288 10 100 cm /Im Do Q"  # 1 inch and 6 inch
    )
    src = tmp_path / "shared.pdf"
    pdf.save(src)
    out = compress_pdf(src, level="medium").outputs[0]
    ((w, h),) = sizes(out)
    assert (w, h) == (1020, 680)  # 170 dpi over the 6 inch use, not over the 1 inch one


def test_images_inside_forms_use_the_form_scale(tmp_path):
    src = build(tmp_path / "form.pdf", [(1800, 1200, 576, 384)], form=True)  # shown at 4 inch
    out = compress_pdf(src, level="medium").outputs[0]
    ((w, h),) = sizes(out)
    assert (w, h) == (680, 453)


@pytest.mark.parametrize("level", ["medium", "high"])
def test_the_effective_dpi_of_every_image_is_close_to_the_profile(tmp_path, level):
    draws = [(3000, 2000, 360, 240), (1600, 1200, 288, 216), (2400, 1600, 144, 96)]
    src = build(tmp_path / "many.pdf", draws)
    out = compress_pdf(src, level=level).outputs[0]
    dpi = PROFILES[level].dpi
    for (pw, _ph), (_, _, tw, _th) in zip(sizes(out), draws, strict=True):
        effective = pw / (tw / 72)
        assert dpi * 0.97 <= effective <= dpi * 1.03


# ------------------------------------------------------------------ /UserUnit


def user_unit_pdf(path: Path, unit: float | None) -> Path:
    """A 1000 x 1000 px image drawn 288 x 288 units: 4 inch on an ordinary page."""
    pdf = Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    if unit is not None:
        page.UserUnit = unit
    page.Resources = Dictionary(XObject=Dictionary(Im=make_image(pdf, "dct-rgb", 1000, 1000)))
    page.Contents = pdf.make_stream(b"q 288 0 0 288 10 10 cm /Im Do Q")
    pdf.save(path)
    return path


def test_user_unit_scales_the_drawn_size(tmp_path):
    plain = user_unit_pdf(tmp_path / "plain.pdf", None)
    big = user_unit_pdf(tmp_path / "big.pdf", 2.0)  # every unit is 2/72 inch
    with pikepdf.open(plain) as a, pikepdf.open(big) as b:
        ((pw, ph),) = image_extents(a).values()
        ((bw, bh),) = image_extents(b).values()
    assert (round(pw, 3), round(ph, 3)) == (4.0, 4.0)
    assert (round(bw, 3), round(bh, 3)) == (8.0, 8.0)


def test_compress_respects_user_unit(tmp_path):
    """Drawn 8 inch wide the image is only 125 dpi: nothing to scale. As 4 inch it is 250."""
    big = user_unit_pdf(tmp_path / "big.pdf", 2.0)
    plain = user_unit_pdf(tmp_path / "plain.pdf", None)
    assert sizes(compress_pdf(big, level="medium", dest=tmp_path / "b.pdf").outputs[0]) == [
        (1000, 1000)
    ]
    assert sizes(compress_pdf(plain, level="medium", dest=tmp_path / "p.pdf").outputs[0]) == [
        (680, 680)
    ]


@pytest.mark.parametrize("bad", [0, -2, "x"])
def test_a_nonsense_user_unit_is_ignored(tmp_path, bad):
    src = user_unit_pdf(tmp_path / "bad.pdf", None)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/UserUnit"] = bad if not isinstance(bad, str) else pikepdf.Name.X
        pdf.save(src)
    with pikepdf.open(src) as pdf:
        ((w, h),) = image_extents(pdf).values()
    assert (round(w, 3), round(h, 3)) == (4.0, 4.0)
