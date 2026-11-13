"""``clean``: strip metadata for privacy, without touching what the pages look like.

Removed: the document info dictionary (author, title, creator, producer, dates...), XMP
metadata everywhere in the file, private application data (``/PieceInfo``), page
thumbnails, last-modified stamps, and EXIF/IPTC/XMP/comment blocks inside embedded JPEGs
(which can hold GPS positions). JPEG pixel data is not re-encoded.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pikepdf import Name
from PIL import Image

from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.output import DECRYPTED_NOTE, write_verified
from pdfzi.core.paths import is_same_file, output_path_for
from pdfzi.core.pdfimages import iter_images
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult

_PRIVATE_KEYS = ("/Metadata", "/PieceInfo", "/LastModified", "/Thumb")
_STRIP_MARKERS = {0xE1: "EXIF/XMP", 0xED: "Photoshop/IPTC", 0xFE: "comment"}


@dataclass
class CleanResult(OperationResult):
    info_fields: list[str] = field(default_factory=list)
    xmp_removed: bool = False
    other_entries_removed: int = 0
    images_cleaned: int = 0
    nothing_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            info_fields=list(self.info_fields),
            xmp_removed=self.xmp_removed,
            other_entries_removed=self.other_entries_removed,
            images_cleaned=self.images_cleaned,
            nothing_found=self.nothing_found,
        )
        return data


def strip_jpeg_metadata(data: bytes) -> bytes | None:
    """Return ``data`` without EXIF/XMP, Photoshop/IPTC and comment segments.

    Returns None if there is nothing to remove or the JPEG looks unusual (in which case it is
    better left alone). ICC profiles (APP2), JFIF (APP0) and Adobe (APP14, needed for CMYK)
    segments are kept, and the compressed image data is copied byte for byte.
    """
    if data[:2] != b"\xff\xd8":
        return None
    out = bytearray(b"\xff\xd8")
    pos, removed, reached_scan = 2, False, False
    while pos + 2 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0xDA:  # start of scan: the rest is entropy-coded data
            out += data[pos:]
            reached_scan = True
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:  # markers without a length
            out += data[pos : pos + 2]
            pos += 2
            continue
        if pos + 4 > len(data):
            return None
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2 or pos + 2 + length > len(data):
            return None
        if marker in _STRIP_MARKERS:
            removed = True
        else:
            out += data[pos : pos + 2 + length]
        pos += 2 + length
    if not (removed and reached_scan):
        return None
    return bytes(out)


def _same_picture(original: bytes, cleaned: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(original)) as a, Image.open(io.BytesIO(cleaned)) as b:
            a.load()
            b.load()
            return a.size == b.size and a.mode == b.mode and a.tobytes() == b.tobytes()
    except (OSError, ValueError):
        return False


def clean_pdf(
    source: Path,
    dest: Path | None = None,
    *,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> CleanResult:
    """Write a metadata-free copy of ``source``; if nothing is found, no file is written."""
    dest = dest or output_path_for(source, "clean")
    report(progress, 0, 4, f"Reading {source.name}", cancel)
    with open_pdf(source, password, in_memory=force and is_same_file(dest, source)) as pdf:
        count = len(pdf.pages)
        was_encrypted = pdf.is_encrypted
        warnings = fidelity_notes(pdf)

        info_fields: list[str] = []
        if Name.Info in pdf.trailer:
            info = pdf.trailer.Info
            info_fields = sorted(str(k).lstrip("/") for k in info)
            del pdf.trailer["/Info"]

        xmp = "/Metadata" in pdf.Root
        report(progress, 1, 4, "Removing metadata", cancel)
        other = 0
        root_id = pdf.Root.objgen
        for obj in pdf.objects:
            try:
                keys = [k for k in _PRIVATE_KEYS if k in obj]
            except (ValueError, TypeError, AttributeError):
                continue  # not a dictionary or stream
            for key in keys:
                del obj[key]
                if not (key == "/Metadata" and obj.objgen == root_id):  # root XMP is `xmp`
                    other += 1

        report(progress, 2, 4, "Cleaning embedded photos", cancel)
        cleaned_images = 0
        for image in iter_images(pdf):
            if str(image.get("/Filter")) != "/DCTDecode":
                continue
            raw = bytes(image.read_raw_bytes())
            stripped = strip_jpeg_metadata(raw)
            if stripped is not None and _same_picture(raw, stripped):
                image.write(stripped, filter=Name.DCTDecode)
                cleaned_images += 1

        found = bool(info_fields) or xmp or other or cleaned_images
        if not found:
            report(progress, 4, 4, "Done")
            return CleanResult(
                command="clean", sources=[source], pages_in=count, pages_out=count,
                bytes_in=source.stat().st_size, bytes_out=source.stat().st_size,
                nothing_found=True, notes=["No metadata found; the file was left unchanged."],
            )  # fmt: skip

        report(progress, 3, 4, "Writing result", cancel)
        out_path = write_verified(pdf, dest, force=force, expected_pages=count)

    notes = []
    if info_fields:
        notes.append("Removed document info: " + ", ".join(info_fields) + ".")
    if xmp:
        notes.append("Removed XMP metadata.")
    if other:
        noun = "entry" if other == 1 else "entries"
        notes.append(f"Removed {other} other private {noun} (app data, thumbnails, dates).")
    if cleaned_images:
        notes.append(f"Removed EXIF/IPTC data from {cleaned_images} embedded photo(s).")
    if was_encrypted:
        notes.append(DECRYPTED_NOTE)
    notes += warnings
    report(progress, 4, 4, "Done")
    return CleanResult(
        command="clean", sources=[source], outputs=[out_path], pages_in=count, pages_out=count,
        bytes_in=source.stat().st_size, bytes_out=out_path.stat().st_size,
        info_fields=info_fields, xmp_removed=xmp, other_entries_removed=other,
        images_cleaned=cleaned_images, notes=notes,
    )  # fmt: skip
