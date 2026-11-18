"""``img2pdf``: combine JPG/PNG images into one PDF.

This is the ONLY module allowed to import the ``img2pdf`` package (LGPL-3.0). It is used as
an ordinary, unmodified, separately installed dependency; a test enforces the rule.
JPEG data is embedded as is (no re-encoding); PNG is stored losslessly.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import img2pdf
from PIL import Image, UnidentifiedImageError

from pdfzi.core.errors import InputReadError, InvalidOptionError, OutputWriteError, PdfziError
from pdfzi.core.fmt import plural
from pdfzi.core.paths import output_path_for
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult
from pdfzi.core.safeio import safe_output
from pdfzi.core.verify import verify_output

PAGE_SIZES = {"fit": None, "a4": (595.276, 841.89), "letter": (612.0, 792.0)}  # points
# "fit" makes a page as big as the picture is *meant* to be printed. Pictures with no
# resolution, or a placeholder one (72 / 96 dpi from screens and phones), would give pages
# several feet wide, so those are fitted onto A4 instead.
_MIN_REAL_DPI = 100
_ALLOWED_FORMATS = {"JPEG", "PNG"}


class InvalidImageError(PdfziError):
    """An input file is not a usable JPG/PNG image."""


def _load(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise InputReadError(f"File not found: {path}") from exc
    except OSError as exc:
        raise InputReadError(f"{path.name}: cannot read the file ({exc.strerror or exc}).") from exc
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            img.verify()
    except Image.DecompressionBombError as exc:
        raise InvalidImageError(
            f"{path.name}: this image is too large to process safely ({exc})."
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidImageError(f"{path.name}: this is not a readable image file.") from exc
    if fmt not in _ALLOWED_FORMATS:
        raise InvalidImageError(
            f"{path.name}: only JPG and PNG images are supported (found {fmt})."
        )
    return data


def _normalized_png(data: bytes) -> bytes:
    """Fallback for PNGs/JPEGs img2pdf rejects: re-save as plain 8-bit PNG (lossless for 8-bit)."""
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        has_alpha = img.mode in {"RGBA", "LA"} or "transparency" in img.info
        converted = img.convert("RGBA" if has_alpha else "RGB")
    buf = io.BytesIO()
    converted.save(buf, "PNG")
    return buf.getvalue()


def _has_real_dpi(data: bytes) -> bool:
    """True if the picture says how big it is meant to be (a resolution of 100 dpi or more)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            dpi = img.info.get("dpi")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return False
    try:
        return bool(dpi) and min(float(dpi[0]), float(dpi[1])) >= _MIN_REAL_DPI
    except (TypeError, ValueError, IndexError):
        return False


def _check_page_size(page_size: str) -> None:
    if page_size not in PAGE_SIZES:
        raise InvalidOptionError("Page size must be fit, a4 or letter.")


def _layout(page_size: str, datas: list[bytes]):
    """The page layout function for ``datas`` (img2pdf calls it once per image, in order)."""
    _check_page_size(page_size)
    size = PAGE_SIZES[page_size]
    onto_page = None
    if size is not None:
        onto_page = img2pdf.get_layout_fun(size, fit=img2pdf.FitMode.into, auto_orient=True)
    if page_size != "fit":
        return onto_page
    onto_a4 = img2pdf.get_layout_fun(PAGE_SIZES["a4"], fit=img2pdf.FitMode.into, auto_orient=True)
    real = iter([_has_real_dpi(d) for d in datas])

    def fit(width_px, height_px, dpi):
        if next(real, True):
            return img2pdf.default_layout_fun(width_px, height_px, dpi)
        return onto_a4(width_px, height_px, dpi)

    return fit


def _convert(datas: list[bytes], page_size: str) -> bytes:
    options = {"rotation": img2pdf.Rotation.ifvalid, "producer": "pdfzi"}
    layout = _layout(page_size, datas)
    if layout is not None:  # passing layout_fun=None would break img2pdf's default
        options["layout_fun"] = layout
    return img2pdf.convert(*datas, **options)


def images_to_pdf(
    sources: Sequence[Path],
    dest: Path | None = None,
    *,
    page_size: str = "fit",
    force: bool = False,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Put ``sources`` (JPG/PNG), one per page and in that order, into a new PDF.

    ``page_size``: ``fit`` (each page matches its image), ``a4`` or ``letter`` (image scaled
    to fit inside the page, page turned to match the image's orientation).
    """
    sources = list(sources)
    if not sources:
        raise InvalidOptionError("No images given.")
    _check_page_size(page_size)
    dest = dest or output_path_for(sources[0], "images")

    datas: list[bytes] = []
    for i, source in enumerate(sources):
        report(progress, i, len(sources) + 1, f"Reading {source.name}", cancel)
        datas.append(_load(source))

    report(progress, len(sources), len(sources) + 1, "Building PDF", cancel)
    try:
        pdf_bytes = _convert(datas, page_size)
    except Exception:  # img2pdf raises many types; retry per image below
        pdf_bytes = _convert_with_fallback(sources, datas, page_size)

    with safe_output(dest, force=force, sources=sources) as out:
        try:
            out.tmp.write_bytes(pdf_bytes)
        except OSError as exc:
            raise OutputWriteError(f"Could not write the result ({exc.strerror or exc}).") from exc
        verify_output(out.tmp, expected_pages=len(sources))
    assert out.path is not None

    notes = []
    if page_size == "fit":
        unsized = sum(1 for d in datas if not _has_real_dpi(d))
        if unsized:
            notes.append(
                f"{plural(unsized, 'picture has', 'pictures have')} no usable resolution (dpi) "
                f"information, so {'its page was' if unsized == 1 else 'their pages were'} "
                "fitted to A4."
            )
    report(progress, len(sources) + 1, len(sources) + 1, "Done")
    return OperationResult(
        command="img2pdf",
        sources=sources,
        outputs=[out.path],
        pages_in=len(sources),
        pages_out=len(sources),
        bytes_in=sum(len(d) for d in datas),
        bytes_out=out.path.stat().st_size,
        notes=notes,
    )


def _convert_with_fallback(sources: list[Path], datas: list[bytes], page_size: str) -> bytes:
    fixed = list(datas)
    for i, (source, data) in enumerate(zip(sources, datas, strict=True)):
        try:
            _convert([data], page_size)
        except Exception:
            try:
                fixed[i] = _normalized_png(data)
                _convert([fixed[i]], page_size)
            except Exception as exc:
                raise InvalidImageError(
                    f"{source.name}: this image cannot be placed in a PDF ({_why(exc)})."
                ) from exc
    try:
        return _convert(fixed, page_size)
    except Exception as exc:
        raise InvalidImageError(f"The images cannot be combined ({_why(exc)}).") from exc


def _why(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__
