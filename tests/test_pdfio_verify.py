from __future__ import annotations

import pytest

from conftest import TRICKY_NAMES
from pdfgen import Snapshot, make_corrupt_pdf, make_pdf
from pdfzi.core.errors import (
    CorruptPdfError,
    InputNotFoundError,
    OutputVerificationError,
    PasswordRequiredError,
    WrongPasswordError,
)
from pdfzi.core.pdfio import open_pdf, save_pdf
from pdfzi.core.verify import verify_output


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_open_unicode_and_spaced_names(workdir, name):
    p = make_pdf(workdir / name, pages=2)
    snap = Snapshot(p)
    with open_pdf(p) as pdf:
        assert len(pdf.pages) == 2
    snap.assert_unchanged()


def test_encrypted_needs_password(workdir):
    p = make_pdf(workdir / "ลับ.pdf", pages=2, password="pw")
    with pytest.raises(PasswordRequiredError, match="password-protected"):
        open_pdf(p)
    with pytest.raises(WrongPasswordError, match="incorrect"):
        open_pdf(p, "nope")
    with open_pdf(p, "pw") as pdf:
        assert len(pdf.pages) == 2


@pytest.mark.parametrize("kind", ["garbage", "header-only"])
def test_corrupt_files_give_friendly_error(workdir, kind):
    p = make_corrupt_pdf(workdir / "เสีย.pdf", kind)
    with pytest.raises(CorruptPdfError, match="not a valid PDF") as info:
        open_pdf(p)
    assert "Traceback" not in str(info.value)


def test_missing_file(workdir):
    with pytest.raises(InputNotFoundError):
        open_pdf(workdir / "nope.pdf")


def test_verify_ok(workdir):
    p = make_pdf(workdir / "a.pdf", pages=4)
    assert verify_output(p, expected_pages=4) == 4


def test_verify_wrong_page_count(workdir):
    p = make_pdf(workdir / "a.pdf", pages=4)
    with pytest.raises(OutputVerificationError, match="4 pages but 5"):
        verify_output(p, expected_pages=5)


def test_verify_unreadable(workdir):
    p = make_corrupt_pdf(workdir / "bad.pdf")
    with pytest.raises(OutputVerificationError, match="could not be read back"):
        verify_output(p, expected_pages=1)


def test_verify_empty_file(workdir):
    p = workdir / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(OutputVerificationError, match="empty"):
        verify_output(p, expected_pages=1)


def test_verify_encrypted_output(workdir):
    p = make_pdf(workdir / "a.pdf", pages=2, password="pw")
    assert verify_output(p, expected_pages=2, password="pw") == 2
    with pytest.raises(OutputVerificationError):
        verify_output(p, expected_pages=2)


def test_save_pdf_failure_is_friendly(workdir):
    p = make_pdf(workdir / "a.pdf", pages=1)
    with open_pdf(p) as pdf:
        from pdfzi.core.errors import OutputWriteError

        with pytest.raises(OutputWriteError, match="Could not write"):
            save_pdf(pdf, workdir / "no-such-folder" / "o.pdf")
