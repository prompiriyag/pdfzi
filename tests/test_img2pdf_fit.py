"""Images to PDF, page size "fit": real resolutions give real sizes, others fit onto A4."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pdfgen import photo_image
from pdfzi.core.imgtopdf import _has_real_dpi, images_to_pdf


def save(path: Path, size: tuple[int, int], dpi: tuple[int, int] | None, fmt: str = "JPEG") -> Path:
    img = photo_image(*size, seed=3)
    if dpi:
        img.save(path, fmt, dpi=dpi)
    else:
        img.save(path, fmt)
    return path


def page_sizes(path: Path) -> list[tuple[float, float]]:
    with pikepdf.open(path) as pdf:
        return [(round(float(p.mediabox[2]), 1), round(float(p.mediabox[3]), 1)) for p in pdf.pages]


def test_a_picture_with_a_real_dpi_keeps_its_physical_size(tmp_path):
    src = save(tmp_path / "scan.jpg", (1200, 800), (300, 300))  # 4 x 2.67 inch
    out = images_to_pdf([src], tmp_path / "o.pdf").outputs[0]
    assert page_sizes(out) == [(288.0, 192.0)]


@pytest.mark.parametrize("dpi", [None, (72, 72), (96, 96)])
def test_a_picture_without_a_real_dpi_is_fitted_to_a4(tmp_path, dpi):
    """No dpi, or the 72 / 96 that phones and screens write, would make a page feet wide."""
    src = save(tmp_path / "p.jpg", (2480, 3508), dpi)
    r = images_to_pdf([src], tmp_path / "o.pdf")
    ((w, h),) = page_sizes(r.outputs[0])
    assert (round(w), round(h)) == (595, 842)
    assert any("fitted to A4" in n for n in r.notes)


def test_a_landscape_picture_gets_a_landscape_a4_page(tmp_path):
    src = save(tmp_path / "wide.jpg", (4000, 3000), (72, 72))
    ((w, h),) = page_sizes(images_to_pdf([src], tmp_path / "o.pdf").outputs[0])
    assert (round(w), round(h)) == (842, 595)


def test_mixed_pictures_get_the_right_page_each_in_order(tmp_path):
    a = save(tmp_path / "a.jpg", (1200, 800), (300, 300))
    b = save(tmp_path / "b.jpg", (3000, 4000), None)
    c = save(tmp_path / "c.png", (600, 600), (150, 150), "PNG")
    r = images_to_pdf([a, b, c], tmp_path / "o.pdf")
    sizes = page_sizes(r.outputs[0])
    assert sizes[0] == (288.0, 192.0)
    assert (round(sizes[1][0]), round(sizes[1][1])) == (595, 842)
    assert sizes[2] == (288.0, 288.0)
    notes = [n for n in r.notes if "fitted to A4" in n]
    assert len(notes) == 1 and notes[0].startswith("1 picture(s) have no usable resolution")


def test_no_note_when_every_picture_has_a_real_dpi(tmp_path):
    src = save(tmp_path / "scan.jpg", (1200, 800), (300, 300))
    assert images_to_pdf([src], tmp_path / "o.pdf").notes == []


def test_explicit_page_sizes_are_unchanged_by_dpi(tmp_path):
    src = save(tmp_path / "scan.jpg", (1200, 800), (300, 300))
    ((w, h),) = page_sizes(images_to_pdf([src], tmp_path / "o.pdf", page_size="letter").outputs[0])
    assert (w, h) == (792.0, 612.0)  # letter, turned to match the landscape picture


def test_has_real_dpi_on_unreadable_data():
    assert _has_real_dpi(b"not an image") is False
