"""The one way every operation publishes a PDF: temp file, verify, then replace."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pikepdf

from pdfzi.core.errors import PdfziError
from pdfzi.core.fmt import plural
from pdfzi.core.pdfio import save_pdf
from pdfzi.core.safeio import SafeOutput, safe_output
from pdfzi.core.verify import verify_output

DECRYPTED_NOTE = (
    "The input was password-protected but the result is not. "
    "Protect it again with 'pdfzi lock' if needed."
)


def write_verified(
    pdf: pikepdf.Pdf,
    dest: Path,
    *,
    force: bool,
    expected_pages: int,
    sources: Iterable[Path] = (),
    password: str | None = None,
    **save_options: Any,
) -> Path:
    """Save ``pdf`` next to ``dest``, check it reads back with ``expected_pages``, publish it.

    Returns the final path (which may differ from ``dest`` when the name was taken and
    ``force`` is off). Nothing is published if saving or verification fails. ``sources``
    are the files the result is made from (see :func:`~pdfzi.core.safeio.safe_output`).
    """
    out = publish_verified(
        pdf, dest, force=force, expected_pages=expected_pages, sources=sources,
        password=password, **save_options,
    )  # fmt: skip
    assert out.path is not None
    return out.path


def publish_verified(
    pdf: pikepdf.Pdf,
    dest: Path,
    *,
    force: bool,
    expected_pages: int,
    sources: Iterable[Path] = (),
    password: str | None = None,
    **save_options: Any,
) -> SafeOutput:
    """:func:`write_verified`, returning the details (such as ``replaced``)."""
    with safe_output(dest, force=force, sources=sources) as out:
        save_pdf(pdf, out.tmp, **save_options)
        verify_output(out.tmp, expected_pages=expected_pages, password=password)
    return out


def note_kept_files(error: BaseException, kept: list[Path]) -> None:
    """Tell the user, through ``error``, which finished results stay after a failure.

    Used when a run that writes several files stops halfway: files that replaced an older
    one (``--force``) are kept, because removing them would not bring the old ones back.
    A :class:`PdfziError` gets this added to its message. Anything else (Ctrl+C is a
    ``KeyboardInterrupt``) has no message for the user, so it gets a note
    (``__notes__``) that the command line prints.
    """
    if not kept:
        return
    names = ", ".join(p.name for p in kept)
    note = (
        f"Kept {plural(len(kept), 'finished file')} that had already replaced "
        f"{'an older one' if len(kept) == 1 else 'older ones'} (--force): {names}."
    )
    if not isinstance(error, PdfziError):
        error.add_note(note)
    elif error.args:
        error.args = (f"{error.args[0]} {note}", *error.args[1:])


def undo_new_files(created: list[Path], kept: list[Path], folder: Path | None) -> None:
    """Remove the results in ``created`` that are not in ``kept``, then ``folder`` if empty."""
    for path in created:
        if path not in kept:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
    if folder is not None:
        with contextlib.suppress(OSError):
            folder.rmdir()


def copy_docinfo(src: pikepdf.Pdf, dst: pikepdf.Pdf) -> None:
    """Carry title/author/etc. over to a document built from some of ``src``'s pages."""
    for key, value in src.docinfo.items():
        try:
            dst.docinfo[key] = str(value)
        except (pikepdf.PdfError, ValueError, TypeError):
            continue
