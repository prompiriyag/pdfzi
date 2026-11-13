"""``pdf2img``: render PDF pages to PNG or JPEG files with PDFium."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from pdfzi.core.errors import (
    InvalidOptionError,
    OutputVerificationError,
    PdfziError,
)
from pdfzi.core.pageranges import page_numbers, parse_page_ranges
from pdfzi.core.paths import unique_dir
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult
from pdfzi.core.safeio import safe_output

FORMATS = {"png": ("PNG", ".png"), "jpg": ("JPEG", ".jpg")}
MIN_DPI, MAX_DPI = 36, 1200
_MAX_PIXELS = 89_000_000  # below Pillow's own decompression-bomb warning threshold


class RenderError(PdfziError):
    """PDFium could not render a page."""


def pdf_to_images(
    source: Path,
    *,
    fmt: str = "png",
    dpi: int = 150,
    pages: str | None = None,
    quality: int = 90,
    out_dir: Path | None = None,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Render ``pages`` (default: all) of ``source`` into ``out_dir``.

    Files are named ``<stem>-p01.png`` and so on; ``out_dir`` defaults to a new folder
    ``<stem>-pages`` next to the source. If anything fails or is cancelled, files created
    by this run are removed again.
    """
    fmt = fmt.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in FORMATS:
        raise InvalidOptionError("Format must be png or jpg.")
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise InvalidOptionError(f"DPI must be between {MIN_DPI} and {MAX_DPI}.")
    if not 1 <= quality <= 95:
        raise InvalidOptionError("JPEG quality must be between 1 and 95.")
    pil_format, ext = FORMATS[fmt]

    # pikepdf gives us the friendly password/corruption errors and the page count.
    with open_pdf(source, password) as probe:
        count = len(probe.pages)
    indices = (
        list(range(count))
        if pages is None
        else [p - 1 for p in page_numbers(parse_page_ranges(pages, count))]
    )

    folder = out_dir or unique_dir(source.with_name(f"{source.stem}-pages"))
    created: list[Path] = []
    created_dir = False
    width_digits = len(str(count))
    scale = dpi / 72
    doc = _open_doc(source, password)
    try:
        if not folder.exists():
            folder.mkdir(parents=True)
            created_dir = True
        for n, index in enumerate(indices):
            name = f"{source.stem}-p{index + 1:0{width_digits}d}{ext}"
            report(progress, n, len(indices), name, cancel)
            created.append(
                _render_one(doc, index, scale, dpi, pil_format, quality, folder / name, force)
            )
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        if created_dir:
            with contextlib.suppress(OSError):
                folder.rmdir()
        raise
    finally:
        doc.close()

    report(progress, len(indices), len(indices), "Done")
    return OperationResult(
        command="pdf2img",
        sources=[source],
        outputs=created,
        pages_in=count,
        pages_out=len(created),
        bytes_in=source.stat().st_size,
        bytes_out=sum(p.stat().st_size for p in created),
        notes=[f"Created {len(created)} {fmt.upper()} file(s) at {dpi} DPI in {folder}."],
    )


def _open_doc(source: Path, password: str | None) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(source, password=password or None)
    except pdfium.PdfiumError as exc:
        raise RenderError(f"{source.name}: PDFium could not open this file ({exc}).") from exc


def _render_one(
    doc: pdfium.PdfDocument,
    index: int,
    scale: float,
    dpi: int,
    pil_format: str,
    quality: int,
    dest: Path,
    force: bool,
) -> Path:
    page = doc[index]
    try:
        width_pt, height_pt = page.get_size()
        pixels = (width_pt * scale) * (height_pt * scale)
        if pixels > _MAX_PIXELS:
            raise InvalidOptionError(
                f"Page {index + 1} would be {pixels / 1e6:.0f} megapixels at {dpi} DPI. "
                "Choose a lower DPI."
            )
        try:
            image = page.render(scale=scale).to_pil()
        except pdfium.PdfiumError as exc:
            raise RenderError(f"Page {index + 1} could not be rendered ({exc}).") from exc
    finally:
        page.close()

    with image:
        expected = image.size
        if pil_format == "JPEG":
            image = image.convert("RGB")
            save_args = {"quality": quality, "optimize": True, "dpi": (dpi, dpi)}
        else:
            save_args = {"optimize": True, "dpi": (dpi, dpi)}
        with safe_output(dest, force=force) as out:
            image.save(out.tmp, format=pil_format, **save_args)
            _verify_image(out.tmp, pil_format, expected)
    assert out.path is not None
    return out.path


def _verify_image(path: Path, pil_format: str, size: tuple[int, int]) -> None:
    try:
        with Image.open(path) as img:
            img.load()
            ok = img.format == pil_format and img.size == size
    except (OSError, ValueError) as exc:
        raise OutputVerificationError(
            "The image could not be read back; it was discarded."
        ) from exc
    if not ok:
        raise OutputVerificationError("The image does not match what was rendered; discarded.")
