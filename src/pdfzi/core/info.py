"""``info``: report what a PDF contains without changing anything."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Name

from pdfzi.core.errors import PasswordRequiredError
from pdfzi.core.fmt import format_size, plural
from pdfzi.core.pdfimages import iter_images
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.textsafe import printable

_NAMED_SIZES = {  # (short side, long side) in points
    "A3": (842, 1191),
    "A4": (595, 842),
    "A5": (420, 595),
    "Letter": (612, 792),
    "Legal": (612, 1008),
}
_METADATA_KEYS = ("Title", "Author", "Subject", "Keywords", "Creator", "Producer")
# Keyed by the name of pikepdf's EncryptionMethod enum (the enum is not exported).
_METHODS = {"aesv3": "AES-256", "aesv2": "AES-128", "rc4": "RC4"}


@dataclass
class PageSizeGroup:
    width_pt: float
    height_pt: float
    name: str | None
    pages: int

    def describe(self) -> str:
        label = f"{self.name} " if self.name else ""
        noun = "page" if self.pages == 1 else "pages"
        return f"{label}{self.width_pt:.0f} x {self.height_pt:.0f} pt ({self.pages} {noun})"


@dataclass
class PdfInfo:
    path: Path
    size_bytes: int
    encrypted: bool
    readable: bool = True  # False: encrypted and no password given, so details are unknown
    page_count: int | None = None
    pdf_version: str | None = None
    encryption: str | None = None
    page_sizes: list[PageSizeGroup] = field(default_factory=list)
    rotated_pages: int | None = None
    image_count: int | None = None
    has_bookmarks: bool | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "encrypted": self.encrypted,
            "readable": self.readable,
            "page_count": self.page_count,
            "pdf_version": self.pdf_version,
            "encryption": self.encryption,
            "page_sizes": [
                {"width_pt": g.width_pt, "height_pt": g.height_pt, "name": g.name, "pages": g.pages}
                for g in self.page_sizes
            ],
            "rotated_pages": self.rotated_pages,
            "image_count": self.image_count,
            "has_bookmarks": self.has_bookmarks,
            "metadata": dict(self.metadata),
        }


def _size_name(width: float, height: float) -> str | None:
    short, long_ = sorted((width, height))
    for name, (s, lng) in _NAMED_SIZES.items():
        if abs(short - s) <= 3 and abs(long_ - lng) <= 3:
            return name
    return None


def _box_size(box: object) -> tuple[float, float] | None:
    try:
        width = abs(float(box[2]) - float(box[0]))  # type: ignore[index]
        height = abs(float(box[3]) - float(box[1]))  # type: ignore[index]
    except (TypeError, ValueError, IndexError, KeyError, pikepdf.PdfError):
        return None
    return width, height


def _displayed_size(page: pikepdf.Page) -> tuple[float, float, int]:
    """Width and height as shown, and the rotation. A damaged box or /Rotate (not numbers)
    falls back to the media box, then to 0 x 0, and to no rotation."""
    size = None
    for get_box in (lambda: page.cropbox, lambda: page.mediabox):
        try:
            size = _box_size(get_box())
        except (TypeError, ValueError, pikepdf.PdfError):
            size = None
        if size is not None:
            break
    width, height = size or (0.0, 0.0)
    try:
        rotate = int(page.obj.get("/Rotate", 0)) % 360
    except (TypeError, ValueError, OverflowError, pikepdf.PdfError):
        rotate = 0
    if rotate in (90, 270):
        width, height = height, width
    return round(width, 1), round(height, 1), rotate


def get_info(path: Path, password: str | None = None) -> PdfInfo:
    """Collect facts about ``path``.

    A password-protected file without a password is *not* an error here: it is reported
    as encrypted with the details unknown.
    """
    size = path.stat().st_size
    try:
        pdf = open_pdf(path, password)
    except PasswordRequiredError:
        return PdfInfo(path=path, size_bytes=size, encrypted=True, readable=False)

    with pdf:
        groups: dict[tuple[float, float], int] = {}
        rotated = 0
        for page in pdf.pages:
            w, h, rot = _displayed_size(page)
            groups[(w, h)] = groups.get((w, h), 0) + 1
            rotated += bool(rot)

        encryption = None
        if pdf.is_encrypted:
            enc = pdf.encryption
            encryption = f"{_METHODS.get(enc.file_method.name, 'unknown')} (R{enc.R})"

        metadata = {}
        for key in _METADATA_KEYS:
            value = pdf.docinfo.get(f"/{key}")
            if value is not None and str(value).strip():
                metadata[key] = str(value)

        return PdfInfo(
            path=path,
            size_bytes=size,
            encrypted=pdf.is_encrypted,
            page_count=len(pdf.pages),
            pdf_version=str(pdf.pdf_version),
            encryption=encryption,
            page_sizes=[
                PageSizeGroup(w, h, _size_name(w, h), n)
                for (w, h), n in sorted(groups.items(), key=lambda kv: -kv[1])
            ],
            rotated_pages=rotated,
            image_count=sum(1 for _ in iter_images(pdf)),
            has_bookmarks=Name.Outlines in pdf.Root and Name.First in pdf.Root.Outlines,
            metadata=metadata,
        )


def format_info_lines(d: PdfInfo) -> list[str]:
    """Plain-text report for one file (shared by the CLI and the TUI).

    Metadata is written by whoever made the PDF, so each value is reduced to one line of
    printable text (no terminal control codes, no fake extra lines).
    """
    lines = [
        printable(d.path.name),
        f"  Size:        {format_size(d.size_bytes)} ({d.size_bytes:,} bytes)",
    ]
    if not d.readable:
        lines.append("  Encrypted:   yes - password needed for more details")
        return lines
    lines += [
        f"  Pages:       {d.page_count}",
        f"  PDF version: {d.pdf_version}",
        f"  Encrypted:   {d.encryption if d.encrypted else 'no'}",
        f"  Page sizes:  {'; '.join(g.describe() for g in d.page_sizes)}",
    ]
    if d.rotated_pages:
        rotated = plural(d.rotated_pages, "page has", "pages have")
        lines.append(f"  Rotated:     {rotated} a /Rotate setting")
    lines += [
        f"  Images:      {d.image_count}",
        f"  Bookmarks:   {'yes' if d.has_bookmarks else 'no'}",
    ]
    lines += [f"  {key + ':':<12} {printable(value)}" for key, value in d.metadata.items()]
    return lines
