"""``clean``: strip metadata for privacy, without touching what the pages look like.

Removed: the document info dictionary (author, title, creator, producer, dates...), XMP
metadata everywhere in the file, private application data (``/PieceInfo``), page
thumbnails, last-modified stamps, the author name and dates of comments and other
annotations, and EXIF/IPTC/XMP/comment blocks inside embedded JPEGs (which can hold GPS
positions). JPEG pixel data is not re-encoded. A JPEG wrapped in a further compression
(``[/FlateDecode /DCTDecode]``) is unwrapped and cleaned too; a photo stored in a way pdfzi
cannot safely unwrap is left alone and reported. The file identifier (``/ID``), which can tie
the result to the original file, is replaced by a new one.

Not touched: the text of comments, form field names and values, attached files.
"""

from __future__ import annotations

import io
import secrets
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pikepdf import Array, Dictionary, Name, String
from PIL import Image

from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.fmt import plural
from pdfzi.core.output import DECRYPTED_NOTE, write_verified
from pdfzi.core.paths import is_same_file, output_path_for
from pdfzi.core.pdfimages import iter_images
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult

_PRIVATE_KEYS = ("/Metadata", "/PieceInfo", "/LastModified", "/Thumb")
_STRIP_MARKERS = {0xE1: "EXIF/XMP", 0xED: "Photoshop/IPTC", 0xFE: "comment"}
# On an annotation /T is the author's name (Acrobat fills in the user name), /M and
# /CreationDate are when it was written. On a form widget /T is the field's name instead,
# which forms need, so widgets only lose the dates.
_ANNOT_DATES = ("/M", "/CreationDate")
# Filters in front of /DCTDecode that clean undoes to reach the JPEG (only without
# /DecodeParms, i.e. no predictor). Anything else is reported as not checked.
_UNWRAPPABLE = frozenset({"/FlateDecode"})
# The most clean unpacks to reach a wrapped JPEG, whatever size the image claims to be:
# /Width and /Height come from the file, and a crafted one can claim anything.
_MAX_UNWRAPPED_JPEG = 100 * 1024 * 1024
_SKIPPED_PHOTOS = (
    "Could not check {photos} (stored in an unusual way); they may still carry EXIF/GPS data."
)


@dataclass
class CleanResult(OperationResult):
    info_fields: list[str] = field(default_factory=list)
    xmp_removed: bool = False
    other_entries_removed: int = 0
    annotations_cleaned: int = 0
    images_cleaned: int = 0
    images_skipped: int = 0
    nothing_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            info_fields=list(self.info_fields),
            xmp_removed=self.xmp_removed,
            other_entries_removed=self.other_entries_removed,
            annotations_cleaned=self.annotations_cleaned,
            images_cleaned=self.images_cleaned,
            images_skipped=self.images_skipped,
            nothing_found=self.nothing_found,
        )
        return data


def strip_jpeg_metadata(data: bytes) -> bytes | None:
    """Return ``data`` without EXIF/XMP, Photoshop/IPTC and comment segments.

    Returns None if there is nothing to remove or the JPEG looks unusual (in which case it is
    better left alone). ICC profiles (APP2), JFIF (APP0) and Adobe (APP14, needed for CMYK)
    segments are kept, and the compressed image data is copied byte for byte.
    """
    return _strip_jpeg(data)[0]


def _strip_jpeg(data: bytes) -> tuple[bytes | None, bool]:
    """:func:`strip_jpeg_metadata`, plus whether the JPEG could be read at all."""
    if data[:2] != b"\xff\xd8":
        return None, False
    out = bytearray(b"\xff\xd8")
    pos, removed, reached_scan = 2, False, False
    while pos + 2 <= len(data):
        if data[pos] != 0xFF:
            return None, False
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
            return None, False
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2 or pos + 2 + length > len(data):
            return None, False
        if marker in _STRIP_MARKERS:
            removed = True
        else:
            out += data[pos : pos + 2 + length]
        pos += 2 + length
    if not reached_scan:
        return None, False  # truncated
    return (bytes(out) if removed else None), True


def _filter_names(image: Any) -> list[str] | None:
    """The image's filters as a list (``/F`` and ``[/F]`` alike); None if malformed."""
    value = image.get("/Filter")
    if value is None:
        return []
    try:
        if isinstance(value, Name):
            return [str(value)]
        if isinstance(value, Array) and all(isinstance(v, Name) for v in value):
            return [str(v) for v in value]
    except UnicodeDecodeError:  # a name that is not valid UTF-8: no filter pdfzi knows
        return None
    return None


def _inflate(data: bytes, limit: int) -> bytes | None:
    """Undo /FlateDecode, refusing output over ``limit`` bytes (a zip bomb). None if bad."""
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or inflater.unconsumed_tail:
        return None
    return out


def _jpeg_inside(image: Any, filters: list[str]) -> tuple[bytes, Any] | None:
    """The JPEG of an image whose last filter is /DCTDecode, and that filter's parameters.

    None if pdfzi cannot get at it safely (an unknown filter in front, a predictor ...).
    """
    parms = image.get("/DecodeParms")
    if isinstance(parms, Array):
        parm_list: list[Any] = list(parms)
    elif parms is None:
        parm_list = [None] * len(filters)
    elif len(filters) == 1:
        parm_list = [parms]
    else:
        return None
    if len(parm_list) != len(filters):
        return None
    data = bytes(image.read_raw_bytes())
    if len(filters) > 1:
        try:
            width, height = int(image.Width), int(image.Height)
        except (AttributeError, TypeError, ValueError):
            return None
        limit = max(width, 1) * max(height, 1) * 8 + (1 << 20)  # far above any real JPEG
        limit = min(limit, _MAX_UNWRAPPED_JPEG)
        for name, parm in zip(filters[:-1], parm_list[:-1], strict=True):
            plain = parm is None or (isinstance(parm, Dictionary) and len(parm) == 0)
            if name not in _UNWRAPPABLE or not plain:
                return None
            inflated = _inflate(data, limit)
            if inflated is None:
                return None
            data = inflated
    own = parm_list[-1]
    return data, (own if isinstance(own, Dictionary) and len(own) > 0 else None)


def _strip_annotation_identity(obj: Any) -> bool:
    """Remove the author name and dates from ``obj`` if it is an annotation.

    Returns True if anything was removed.
    """
    try:
        if "/Rect" not in obj or "/Subtype" not in obj:  # every annotation has both
            return False
        keys = list(_ANNOT_DATES)
        if obj.Subtype != Name.Widget:
            keys.append("/T")
        present = [k for k in keys if k in obj]
    except (ValueError, TypeError, AttributeError):
        return False  # not a dictionary
    for key in present:
        del obj[key]
    return bool(present)


def _same_picture(original: bytes, cleaned: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(original)) as a, Image.open(io.BytesIO(cleaned)) as b:
            a.load()
            b.load()
            return a.size == b.size and a.mode == b.mode and a.tobytes() == b.tobytes()
    except (OSError, ValueError, MemoryError, Image.DecompressionBombError):
        return False  # cannot be checked, so it is left as it is


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
            try:
                info_fields = sorted(str(k).lstrip("/") for k in info)
            except UnicodeDecodeError:  # field names that are not valid UTF-8
                info_fields = ["(document info)"]
            del pdf.trailer["/Info"]

        xmp = "/Metadata" in pdf.Root
        report(progress, 1, 4, "Removing metadata", cancel)
        other = 0
        annotations = 0
        root_id = pdf.Root.objgen
        for obj in pdf.objects:
            annotations += _strip_annotation_identity(obj)
            try:
                keys = [k for k in _PRIVATE_KEYS if k in obj]
            except (ValueError, TypeError, AttributeError):
                continue  # not a dictionary or stream
            for key in keys:
                del obj[key]
                if not (key == "/Metadata" and obj.objgen == root_id):  # root XMP is `xmp`
                    other += 1
        # Annotations written directly inside a page's /Annots are not in pdf.objects.
        for page in pdf.pages:
            annots = page.obj.get("/Annots")
            for annot in annots if isinstance(annots, Array) else []:
                if isinstance(annot, Dictionary) and not annot.is_indirect:
                    annotations += _strip_annotation_identity(annot)

        report(progress, 2, 4, "Cleaning embedded photos", cancel)
        cleaned_images = skipped_images = 0
        for image in iter_images(pdf):
            filters = _filter_names(image)
            if filters is None:
                skipped_images += 1  # cannot tell what it holds
                continue
            if not filters or filters[-1] != "/DCTDecode":
                continue  # not a JPEG
            found_jpeg = _jpeg_inside(image, filters)
            if found_jpeg is None:
                skipped_images += 1
                continue
            jpeg, own_parms = found_jpeg
            stripped, readable = _strip_jpeg(jpeg)
            if not readable or (stripped is not None and not _same_picture(jpeg, stripped)):
                skipped_images += 1
            elif stripped is not None:
                # Written as plain /DCTDecode: any wrapping in front of it is dropped.
                if own_parms is not None:
                    image.write(stripped, filter=Name.DCTDecode, decode_parms=own_parms)
                else:
                    image.write(stripped, filter=Name.DCTDecode)
                cleaned_images += 1

        found = bool(info_fields) or xmp or other or annotations or cleaned_images
        skipped_note = (
            [_SKIPPED_PHOTOS.format(photos=plural(skipped_images, "embedded photo"))]
            if skipped_images
            else []
        )
        if not found:
            report(progress, 4, 4, "Done")
            first = (
                "No metadata that pdfzi can remove was found; the file was left unchanged."
                if skipped_images
                else "No metadata found; the file was left unchanged."
            )
            return CleanResult(
                command="clean", sources=[source], pages_in=count, pages_out=count,
                bytes_in=source.stat().st_size, bytes_out=source.stat().st_size,
                images_skipped=skipped_images, nothing_found=True,
                notes=[first, *skipped_note],
            )  # fmt: skip

        # The first half of /ID is kept by every rewrite and so ties the result to the
        # original file. Deleting it is not enough (qpdf remembers the original one), so a
        # random one is set; the second half is then worked out from the result's own bytes.
        # (It does not count as "found": almost every PDF has one, and it alone is no
        # reason to write a file.)
        fresh = String(secrets.token_bytes(16))
        pdf.trailer.ID = Array([fresh, fresh])
        report(progress, 3, 4, "Writing result", cancel)
        out_path = write_verified(
            pdf, dest, force=force, expected_pages=count, sources=[source],
            deterministic_id=True,
        )  # fmt: skip

    notes = []
    if info_fields:
        notes.append("Removed document info: " + ", ".join(info_fields) + ".")
    if xmp:
        notes.append("Removed XMP metadata.")
    if other:
        noun = "entry" if other == 1 else "entries"
        notes.append(f"Removed {other} other private {noun} (app data, thumbnails, dates).")
    if annotations:
        notes.append(
            "Removed author names and dates from "
            + plural(annotations, "comment or annotation", "comments or annotations")
            + "."
        )
    if cleaned_images:
        notes.append(f"Removed EXIF/IPTC data from {plural(cleaned_images, 'embedded photo')}.")
    notes += skipped_note
    if was_encrypted:
        notes.append(DECRYPTED_NOTE)
    notes += warnings
    report(progress, 4, 4, "Done")
    return CleanResult(
        command="clean", sources=[source], outputs=[out_path], pages_in=count, pages_out=count,
        bytes_in=source.stat().st_size, bytes_out=out_path.stat().st_size,
        info_fields=info_fields, xmp_removed=xmp, other_entries_removed=other,
        annotations_cleaned=annotations, images_cleaned=cleaned_images,
        images_skipped=skipped_images, notes=notes,
    )  # fmt: skip
