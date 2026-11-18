"""``lock`` / ``unlock``: add or remove password protection (AES-256, PDF 2.0 / R6)."""

from __future__ import annotations

from pathlib import Path

import pikepdf

from pdfzi.core.errors import (
    InvalidOptionError,
    OutputVerificationError,
    PdfziError,
    WrongPasswordError,
)
from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.output import write_verified
from pdfzi.core.paths import is_same_file, output_path_for
from pdfzi.core.pdfio import open_pdf, save_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult
from pdfzi.core.safeio import safe_output
from pdfzi.core.verify import verify_output


class NotEncryptedError(PdfziError):
    """``unlock`` was asked to open a PDF that has no password protection."""


class OwnerPasswordRequiredError(PdfziError):
    """The PDF opens freely but has usage restrictions only the owner password can lift."""


def lock_pdf(
    source: Path,
    new_password: str,
    dest: Path | None = None,
    *,
    owner_password: str | None = None,
    input_password: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Write a copy of ``source`` that needs ``new_password`` to open (AES-256).

    ``owner_password`` defaults to the same value. ``input_password`` is only for sources
    that are already protected.
    """
    if not new_password:
        raise InvalidOptionError("The password must not be empty.")
    dest = dest or output_path_for(source, "locked")
    report(progress, 0, 3, f"Reading {source.name}", cancel)
    with open_pdf(source, input_password, in_memory=force and is_same_file(dest, source)) as pdf:
        count = len(pdf.pages)
        warnings = fidelity_notes(pdf)
        report(progress, 1, 3, "Encrypting", cancel)
        encryption = pikepdf.Encryption(
            user=new_password, owner=owner_password or new_password, R=6
        )
        with safe_output(dest, force=force, sources=[source]) as out:
            save_pdf(pdf, out.tmp, encryption=encryption)
            verify_output(out.tmp, expected_pages=count, password=new_password)
            _assert_locked(out.tmp, new_password)
    assert out.path is not None
    report(progress, 3, 3, "Done")
    return OperationResult(
        command="lock",
        sources=[source],
        outputs=[out.path],
        pages_in=count,
        pages_out=count,
        bytes_in=source.stat().st_size,
        bytes_out=out.path.stat().st_size,
        notes=[
            "Protected with AES-256. If you forget the password the file cannot be recovered.",
            *warnings,
        ],
    )


def _assert_locked(path: Path, password: str) -> None:
    """The result must refuse to open without the password and report AES-256."""
    try:
        with pikepdf.Pdf.open(path):
            raise OutputVerificationError("The result opened without a password; it was discarded.")
    except pikepdf.PasswordError:
        pass
    with pikepdf.Pdf.open(path, password=password) as pdf:
        info = pdf.encryption
        if not pdf.is_encrypted or info.bits != 256 or info.R != 6:
            raise OutputVerificationError("The result is not AES-256 encrypted; it was discarded.")


def _opens_without_password(path: Path) -> bool:
    try:
        with pikepdf.Pdf.open(path, password=""):
            return True
    except pikepdf.PasswordError:
        return False


def unlock_pdf(
    source: Path,
    password: str,
    dest: Path | None = None,
    *,
    force: bool = False,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Write an unprotected copy of ``source``; you must know its password.

    A file that opens without any password but carries usage restrictions (no printing,
    no copying) is only unlocked with its *owner* password.
    """
    if not password:
        raise InvalidOptionError("The password must not be empty.")
    dest = dest or output_path_for(source, "unlocked")
    report(progress, 0, 3, f"Reading {source.name}", cancel)
    with open_pdf(source, password, in_memory=force and is_same_file(dest, source)) as pdf:
        if not pdf.is_encrypted:
            raise NotEncryptedError(f"{source.name}: this PDF is not password-protected.")
        needs_user_password = not _opens_without_password(source)
        owner_ok = pdf.owner_password_matched
        if not (owner_ok or (needs_user_password and pdf.user_password_matched)):
            if needs_user_password:
                raise WrongPasswordError(source)
            raise OwnerPasswordRequiredError(
                f"{source.name}: this PDF opens without a password but has usage restrictions. "
                "They can only be removed with the owner password."
            )
        count = len(pdf.pages)
        warnings = fidelity_notes(pdf)
        report(progress, 1, 3, "Writing result", cancel)
        out_path = write_verified(pdf, dest, force=force, expected_pages=count, sources=[source])
    report(progress, 3, 3, "Done")
    return OperationResult(
        command="unlock",
        sources=[source],
        outputs=[out_path],
        pages_in=count,
        pages_out=count,
        bytes_in=source.stat().st_size,
        bytes_out=out_path.stat().st_size,
        notes=warnings,
    )
