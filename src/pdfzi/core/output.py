"""The one way every operation publishes a PDF: temp file, verify, then replace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf

from pdfzi.core.pdfio import save_pdf
from pdfzi.core.safeio import safe_output
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
    password: str | None = None,
    **save_options: Any,
) -> Path:
    """Save ``pdf`` next to ``dest``, check it reads back with ``expected_pages``, publish it.

    Returns the final path (which may differ from ``dest`` when the name was taken and
    ``force`` is off). Nothing is published if saving or verification fails.
    """
    with safe_output(dest, force=force) as out:
        save_pdf(pdf, out.tmp, **save_options)
        verify_output(out.tmp, expected_pages=expected_pages, password=password)
    assert out.path is not None
    return out.path


def copy_docinfo(src: pikepdf.Pdf, dst: pikepdf.Pdf) -> None:
    """Carry title/author/etc. over to a document built from some of ``src``'s pages."""
    for key, value in src.docinfo.items():
        try:
            dst.docinfo[key] = str(value)
        except (pikepdf.PdfError, ValueError, TypeError):
            continue
