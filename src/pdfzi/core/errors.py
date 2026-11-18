"""User-facing errors.

Every exception here carries a message that is safe to show to the user as-is.
The CLI and TUI print ``str(error)`` and never a traceback for these.
"""

from __future__ import annotations

from pathlib import Path


class PdfziError(Exception):
    """Base class for all errors whose message is meant for the user."""


class InputNotFoundError(PdfziError):
    """An input path or glob pattern did not resolve to an existing file."""


class NotAFileError(PdfziError):
    """An input path exists but is not a regular file (e.g. a folder)."""


class InputReadError(PdfziError):
    """An input file exists but could not be read (permissions, I/O error)."""


class CorruptPdfError(PdfziError):
    """The file is not a PDF, or it is too damaged to read."""

    def __init__(self, path: Path, detail: str | None = None) -> None:
        msg = f"{path.name}: this file is not a valid PDF or is too damaged to read."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.path = path


class PasswordRequiredError(PdfziError):
    """The PDF is encrypted and no password was supplied."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path.name}: this PDF is password-protected. Provide the password.")
        self.path = path


class WrongPasswordError(PdfziError):
    """A password was supplied but it does not open the PDF."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path.name}: the password is incorrect.")
        self.path = path


class InvalidOptionError(PdfziError):
    """An option value or combination of options makes no sense."""


class PageRangeError(PdfziError):
    """A page-range expression is malformed or outside the document."""


class OutputWriteError(PdfziError):
    """The result could not be written to disk. The original files are untouched."""


class OutputVerificationError(PdfziError):
    """The freshly written result failed verification and was discarded."""


class OperationCancelledError(PdfziError):
    """The user cancelled a running operation."""

    def __init__(self) -> None:
        super().__init__("Cancelled.")


class UnsafeInputError(PdfziError):
    """The file holds something built to exhaust memory; it is refused, not processed."""

    def __init__(self, name: str, what: str, limit: int) -> None:
        super().__init__(
            f"{name}: {what} would unpack to more than {limit // (1024 * 1024)} MB, far more "
            "than real files hold. pdfzi does not process it, because a file like this is "
            "usually made to use up the computer's memory."
        )
