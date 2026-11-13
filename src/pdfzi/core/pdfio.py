"""Opening and saving PDFs with pikepdf, translating failures into friendly errors."""

from __future__ import annotations

import io
import logging
import warnings
from pathlib import Path
from typing import Any

import pikepdf

from pdfzi.core.errors import (
    CorruptPdfError,
    InputNotFoundError,
    InputReadError,
    OutputWriteError,
    PasswordRequiredError,
    WrongPasswordError,
)

# qpdf reports recoverable problems as log warnings. We never want them printed raw to
# the user's terminal, so give the library logger a handler that swallows them.
logging.getLogger("pikepdf").addHandler(logging.NullHandler())


def open_pdf(path: Path, password: str | None = None, *, in_memory: bool = False) -> pikepdf.Pdf:
    """Open ``path`` read-only-in-spirit; the caller must close it (use ``with``).

    pdfzi never saves back to the object it opened from, so the input is never modified.
    ``in_memory`` reads the whole file first so no OS handle stays open; needed when the
    result will replace this very file (``--force``), because Windows cannot replace an
    open file.
    """
    try:
        with warnings.catch_warnings():
            # A password given for a batch is simply unused by files that need none.
            warnings.filterwarnings("ignore", message="A password was provided")
            if in_memory:
                return pikepdf.Pdf.open(io.BytesIO(path.read_bytes()), password=password or "")
            return pikepdf.Pdf.open(path, password=password or "")
    except pikepdf.PasswordError as exc:
        if password:
            raise WrongPasswordError(path) from exc
        raise PasswordRequiredError(path) from exc
    except FileNotFoundError as exc:
        raise InputNotFoundError(f"File not found: {path}") from exc
    except PermissionError as exc:
        raise InputReadError(f"{path.name}: permission denied while reading the file.") from exc
    except pikepdf.PdfError as exc:
        raise CorruptPdfError(path, _first_line(exc).removeprefix(f"{path}: ")) from exc
    except OSError as exc:
        raise InputReadError(f"{path.name}: cannot read the file ({exc.strerror or exc}).") from exc


def save_pdf(pdf: pikepdf.Pdf, dest: Path, **options: Any) -> None:
    """Save ``pdf`` to ``dest`` (normally a temp path from ``safe_output``).

    pikepdf would otherwise re-serialise the XMP metadata to "fix" its PDF version field,
    which changes bytes the user never asked to change (and PDF/A cares about).
    """
    options.setdefault("fix_metadata_version", False)
    try:
        pdf.save(dest, **options)
    except OSError as exc:
        raise OutputWriteError(f"Could not write the result ({exc.strerror or exc}).") from exc
    except pikepdf.PdfError as exc:
        raise OutputWriteError(f"Could not write the result ({_first_line(exc)}).") from exc


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
