"""Post-write verification: never report success for output we cannot read back."""

from __future__ import annotations

from pathlib import Path

import pikepdf

from pdfzi.core.errors import OutputVerificationError


def verify_output(path: Path, *, expected_pages: int, password: str | None = None) -> int:
    """Re-open ``path`` and check it is readable with ``expected_pages`` pages.

    Returns the page count. Raises :class:`OutputVerificationError` otherwise; callers
    run this on the temp file so a bad result is discarded before it replaces anything.
    """
    try:
        if path.stat().st_size == 0:
            raise OutputVerificationError("The result file is empty; it was discarded.")
        with pikepdf.Pdf.open(path, password=password or "") as pdf:
            count = len(pdf.pages)
            for page in pdf.pages:  # force every page object to load
                _ = page.obj.keys()
    except OutputVerificationError:
        raise
    except (pikepdf.PdfError, pikepdf.PasswordError, OSError) as exc:
        raise OutputVerificationError(
            "The result could not be read back, so it was discarded and nothing was changed."
        ) from exc

    if count != expected_pages:
        raise OutputVerificationError(
            f"The result has {count} pages but {expected_pages} were expected; "
            "it was discarded and nothing was changed."
        )
    return count
