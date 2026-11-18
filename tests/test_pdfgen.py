"""Sanity checks for the test-PDF builder itself, so other tests can trust it."""

from __future__ import annotations

import pikepdf
import pytest

from conftest import TRICKY_NAMES
from pdfgen import extract_text, make_corrupt_pdf, make_pdf, page_count, page_text


def test_text_is_extractable(workdir):
    p = make_pdf(workdir / "a.pdf", pages=3)
    assert page_count(p) == 3
    for i in range(3):
        assert page_text(i) in extract_text(p, i)


def test_mixed_sizes_and_rotation(workdir):
    p = make_pdf(workdir / "a.pdf", pages=3, sizes=[(612, 792), (200, 300)], rotations={1: 90})
    with pikepdf.open(p) as pdf:
        assert [float(pg.mediabox[2]) for pg in pdf.pages] == [612, 200, 612]
        assert pdf.pages[1].get("/Rotate") == 90
        assert pdf.pages[0].get("/Rotate") is None


def test_bookmarks(workdir):
    p = make_pdf(workdir / "a.pdf", pages=3, bookmarks=True)
    with pikepdf.open(p) as pdf, pdf.open_outline() as outline:
        assert [i.title for i in outline.root] == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_encrypted_requires_password(workdir):
    p = make_pdf(workdir / "a.pdf", pages=2, password="s3cret")
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(p)
    assert page_count(p, "s3cret") == 2
    with pikepdf.open(p, password="s3cret") as pdf:
        assert pdf.is_encrypted


def test_images_shared_vs_separate(workdir):
    shared = make_pdf(workdir / "s.pdf", pages=3, images_per_page=1, share_image=True)
    separate = make_pdf(workdir / "n.pdf", pages=3, images_per_page=1)

    def image_ids(path):
        with pikepdf.open(path) as pdf:
            return {pg.Resources.XObject.Im0.objgen for pg in pdf.pages}

    assert len(image_ids(shared)) == 1
    assert len(image_ids(separate)) == 3
    assert shared.stat().st_size < separate.stat().st_size


@pytest.mark.parametrize("kind", ["garbage", "header-only"])
def test_corrupt_pdf_does_not_open(workdir, kind):
    p = make_corrupt_pdf(workdir / "bad.pdf", kind)
    with pytest.raises(pikepdf.PdfError):
        pikepdf.open(p)


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_tricky_filenames(workdir, name):
    p = make_pdf(workdir / name, pages=1)
    assert page_count(p) == 1
