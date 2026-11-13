"""What the TUI can do: one :class:`Tool` per core operation.

A tool declares its input type, its option fields, and ``build`` which validates the form
values and returns a runner. Runners are plain functions executed in a worker thread; they
return per-file :class:`~pdfzi.core.batch.BatchEntry` results.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pdfzi.core.batch import BatchEntry, run_batch
from pdfzi.core.clean import clean_pdf
from pdfzi.core.compress import PROFILES, compress_pdf
from pdfzi.core.errors import InvalidOptionError
from pdfzi.core.fmt import parse_size
from pdfzi.core.imgtopdf import images_to_pdf
from pdfzi.core.info import format_info_lines, get_info
from pdfzi.core.merge import merge_pdfs
from pdfzi.core.paths import plan_outputs
from pdfzi.core.pdftoimg import pdf_to_images
from pdfzi.core.progress import CancelToken, Progress, ProgressCallback
from pdfzi.core.protect import lock_pdf, unlock_pdf
from pdfzi.core.results import OperationResult
from pdfzi.core.rotate import rotate_pdf
from pdfzi.core.subset import delete_pages, extract_pages, split_pdf

Runner = Callable[[list[Path], ProgressCallback | None, CancelToken], list[BatchEntry]]


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: Literal["choice", "text", "int", "bool", "password", "size"]
    choices: tuple[str, ...] = ()
    default: Any = ""
    hint: str = ""
    # what each choice of a "choice" field does: ((choice, short note, sentence), ...).
    # The short note (about 20 characters) sits beside the options, the sentence under the form.
    details: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Tool:
    id: str
    title: str
    blurb: str
    accepts: Literal["pdf", "image"]
    build: Callable[[dict[str, Any]], Runner]
    fields: tuple[Field, ...] = ()
    min_files: int = 1
    ordered: bool = False  # the order of the chosen files matters (merge, images -> PDF)


def _level_details() -> tuple[tuple[str, str, str], ...]:
    """What each compress level does, read from the real settings so the two cannot differ."""
    medium, high = PROFILES["medium"], PROFILES["high"]
    return (
        (
            "lossless",
            "images untouched",
            "Images untouched; only the file structure is tidied. Nothing visible changes.",
        ),
        (
            "medium",
            f"to {medium.dpi} dpi, JPEG q{medium.quality}",
            f"Sharper images scaled to {medium.dpi} dpi, JPEG q{medium.quality}. "
            "Same look on screen.",
        ),
        (
            "high",
            f"to {high.dpi} dpi, JPEG q{high.quality}",
            f"Sharper images scaled to {high.dpi} dpi, JPEG q{high.quality}. "
            "Smallest; scans look soft.",
        ),
    )


PASSWORD = Field("password", "Password", "password", hint="only if the PDF is password-protected")
OUTPUT = Field("output", "Output folder", "text", hint="empty = next to the original")


# ------------------------------------------------------------------ helpers


def _text(values: dict[str, Any], key: str) -> str:
    return str(values.get(key) or "").strip()


def _output(values: dict[str, Any]) -> str | None:
    """Output folder as typed by the user, with a trailing separator so it is a folder."""
    folder = _text(values, "output")
    return folder + os.sep if folder else None


def _require(values: dict[str, Any], key: str, what: str) -> str:
    value = _text(values, key)
    if not value:
        raise InvalidOptionError(f"{what} is required.")
    return value


def _batch(
    files: list[Path],
    op: Callable[[Path, int, ProgressCallback], OperationResult],
    progress: ProgressCallback | None,
    cancel: CancelToken,
) -> list[BatchEntry]:
    """Run ``op`` over each file, mapping per-file progress onto one overall bar."""
    n = len(files)

    def one(source: Path, index: int) -> OperationResult:
        def inner(p: Progress) -> None:
            if progress is not None:
                fraction = p.fraction or 0.0
                done = int((index + fraction) * 100)
                progress(Progress(done, n * 100, f"{source.name}: {p.message}"))

        return op(source, index, inner)

    return run_batch(files, one, cancel=cancel)


def _single(
    files: list[Path],
    op: Callable[[ProgressCallback | None], OperationResult],
    cancel: CancelToken,
    progress: ProgressCallback | None,
) -> list[BatchEntry]:
    """Many files -> one result (merge, images to PDF), reported as one entry."""
    return run_batch(files[:1], lambda _s, _i: op(progress), cancel=cancel)


def _folder_for(out: str, source: Path, count: int, suffix: str) -> Path | None:
    """Folder for tools that write several files per input (split, pdf2img)."""
    if not out:
        return None
    return Path(out) / f"{source.stem}-{suffix}" if count > 1 else Path(out)


# ------------------------------------------------------------------ builders


def _build_compress(v: dict[str, Any]) -> Runner:
    target_text = _text(v, "target")
    target = parse_size(target_text) if target_text else None
    level = "lossless" if target else str(v.get("level") or "lossless")
    grayscale = bool(v.get("grayscale"))
    if grayscale and not target and level == "lossless":
        raise InvalidOptionError("Grayscale needs level medium or high (or a target size).")
    password, out = _text(v, "password") or None, _output(v)

    def run(files, progress, cancel):
        dests = plan_outputs(files, "compressed", out)

        def op(s, i, cb):
            return compress_pdf(
                s,
                dests[i],
                level=level,
                target=target,
                grayscale=grayscale,
                password=password,
                progress=cb,
                cancel=cancel,
            )

        return _batch(files, op, progress, cancel)

    return run


def _build_merge(v: dict[str, Any]) -> Runner:
    password, out = _text(v, "password") or None, _output(v)

    def run(files, progress, cancel):
        dest = plan_outputs(files[:1], "merged", out)[0]

        def op(cb):
            return merge_pdfs(files, dest, password=password, progress=cb, cancel=cancel)

        return _single(files, op, cancel, progress)

    return run


def _build_split(v: dict[str, Any]) -> Runner:
    mode = str(v.get("mode") or "every")
    every: int | None = None
    ranges: str | None = None
    if mode == "every":
        try:
            every = int(_text(v, "value") or 1)
        except ValueError:
            raise InvalidOptionError("Pieces of N pages: N must be a whole number.") from None
        if every < 1:
            raise InvalidOptionError("Pieces of N pages: N must be 1 or more.")
    else:
        ranges = _require(v, "value", "Page ranges")
    password, out = _text(v, "password") or None, _text(v, "output")

    def run(files, progress, cancel):
        def op(source, i, cb):
            return split_pdf(
                source,
                every=every,
                ranges=ranges,
                out_dir=_folder_for(out, source, len(files), "split"),
                password=password,
                progress=cb,
                cancel=cancel,
            )

        return _batch(files, op, progress, cancel)

    return run


def _page_tool_builder(
    suffix: str, func: Callable[..., OperationResult], what: str
) -> Callable[[dict[str, Any]], Runner]:
    def build(v: dict[str, Any]) -> Runner:
        pages = _require(v, "pages", what)
        password, out = _text(v, "password") or None, _output(v)

        def run(files, progress, cancel):
            dests = plan_outputs(files, suffix, out)

            def op(s, i, cb):
                return func(s, pages, dests[i], password=password, progress=cb, cancel=cancel)

            return _batch(files, op, progress, cancel)

        return run

    return build


def _build_rotate(v: dict[str, Any]) -> Runner:
    angle = int(v.get("angle") or 90)
    pages = _text(v, "pages") or None
    password, out = _text(v, "password") or None, _output(v)

    def run(files, progress, cancel):
        dests = plan_outputs(files, "rotated", out)

        def op(s, i, cb):
            return rotate_pdf(
                s, angle, pages, dests[i], password=password, progress=cb, cancel=cancel
            )

        return _batch(files, op, progress, cancel)

    return run


def _build_img2pdf(v: dict[str, Any]) -> Runner:
    page_size, out = str(v.get("page_size") or "fit"), _output(v)

    def run(files, progress, cancel):
        dest = plan_outputs(files[:1], "images", out)[0]

        def op(cb):
            return images_to_pdf(files, dest, page_size=page_size, progress=cb, cancel=cancel)

        return _single(files, op, cancel, progress)

    return run


def _build_pdf2img(v: dict[str, Any]) -> Runner:
    fmt = str(v.get("format") or "png")
    try:
        dpi = int(_text(v, "dpi") or 150)
    except ValueError:
        raise InvalidOptionError("DPI must be a whole number.") from None
    pages = _text(v, "pages") or None
    password, out = _text(v, "password") or None, _text(v, "output")

    def run(files, progress, cancel):
        def op(source, i, cb):
            return pdf_to_images(
                source,
                fmt=fmt,
                dpi=dpi,
                pages=pages,
                out_dir=_folder_for(out, source, len(files), "pages"),
                password=password,
                progress=cb,
                cancel=cancel,
            )

        return _batch(files, op, progress, cancel)

    return run


def _build_lock(v: dict[str, Any]) -> Runner:
    new_password = _require(v, "new_password", "A password")
    if new_password != _text(v, "confirm"):
        raise InvalidOptionError("The two passwords do not match.")
    current, out = _text(v, "password") or None, _output(v)

    def run(files, progress, cancel):
        dests = plan_outputs(files, "locked", out)

        def op(s, i, cb):
            return lock_pdf(
                s, new_password, dests[i], input_password=current, progress=cb, cancel=cancel
            )

        return _batch(files, op, progress, cancel)

    return run


def _build_unlock(v: dict[str, Any]) -> Runner:
    password = _require(v, "password", "The password")
    out = _output(v)

    def run(files, progress, cancel):
        dests = plan_outputs(files, "unlocked", out)

        def op(s, i, cb):
            return unlock_pdf(s, password, dests[i], progress=cb, cancel=cancel)

        return _batch(files, op, progress, cancel)

    return run


def _build_clean(v: dict[str, Any]) -> Runner:
    password, out = _text(v, "password") or None, _output(v)

    def run(files, progress, cancel):
        dests = plan_outputs(files, "clean", out)

        def op(s, i, cb):
            return clean_pdf(s, dests[i], password=password, progress=cb, cancel=cancel)

        return _batch(files, op, progress, cancel)

    return run


def _build_info(v: dict[str, Any]) -> Runner:
    password = _text(v, "password") or None

    def run(files, progress, cancel):
        def op(source, i, cb):
            lines = format_info_lines(get_info(source, password))
            return OperationResult(command="info", sources=[source], notes=lines)

        return _batch(files, op, progress, cancel)

    return run


# ------------------------------------------------------------------ registry

LEVELS = ("lossless", "medium", "high")

TOOLS: tuple[Tool, ...] = (
    Tool(
        "compress",
        "Compress",
        "Make PDFs smaller; text stays text",
        "pdf",
        _build_compress,
        fields=(
            Field(
                "level",
                "Level",
                "choice",
                LEVELS,
                "lossless",
                "how much the images are squeezed",
                details=_level_details(),
            ),
            Field(
                "target",
                "Target size",
                "size",
                choices=("MB", "KB"),  # the unit; the number is typed
                hint="optional: type a number, then choose MB or KB (overrides level)",
            ),
            Field(
                "grayscale",
                "Grayscale",
                "bool",
                default=False,
                hint="turn colour images grey (medium/high)",
            ),
            PASSWORD,
            OUTPUT,
        ),
    ),
    Tool(
        "merge",
        "Merge",
        "Join PDFs into one, in the order you choose",
        "pdf",
        _build_merge,
        fields=(PASSWORD, OUTPUT),
        min_files=2,
        ordered=True,
    ),
    Tool(
        "split",
        "Split",
        "One file per page, per N pages, or per range",
        "pdf",
        _build_split,
        fields=(
            Field(
                "mode",
                "Split by",
                "choice",
                ("every", "ranges"),
                "every",
                details=(
                    (
                        "every",
                        "N pages per file",
                        "One file for every N pages; N is typed in the next field.",
                    ),
                    (
                        "ranges",
                        "one file per range",
                        "One file per range, such as 1-3,7,10-, typed in the next field.",
                    ),
                ),
            ),
            Field("value", "N pages / ranges", "text", default="1", hint="N, or like 1-3,7,10-"),
            PASSWORD,
            OUTPUT,
        ),
    ),
    Tool(
        "extract",
        "Extract pages",
        "Copy the pages you pick into a new PDF",
        "pdf",
        _page_tool_builder("extracted", extract_pages, "Pages to keep"),
        fields=(Field("pages", "Pages to keep", "text", hint="e.g. 1-3,7,10-"), PASSWORD, OUTPUT),
    ),
    Tool(
        "delete",
        "Delete pages",
        "Copy a PDF without the pages you pick",
        "pdf",
        _page_tool_builder("removed", delete_pages, "Pages to remove"),
        fields=(Field("pages", "Pages to remove", "text", hint="e.g. 2,5-6"), PASSWORD, OUTPUT),
    ),
    Tool(
        "rotate",
        "Rotate",
        "Turn the whole file or only some pages",
        "pdf",
        _build_rotate,
        fields=(
            Field("angle", "Angle (clockwise)", "choice", ("90", "180", "270"), "90"),
            Field("pages", "Only pages", "text", hint="optional, empty = all pages"),
            PASSWORD,
            OUTPUT,
        ),
    ),
    Tool(
        "img2pdf",
        "Images to PDF",
        "Combine JPG/PNG pictures into one PDF",
        "image",
        _build_img2pdf,
        ordered=True,
        fields=(
            Field(
                "page_size",
                "Page size",
                "choice",
                ("fit", "a4", "letter"),
                "fit",
                details=(
                    (
                        "fit",
                        "as big as the picture",
                        "Each page is as big as its picture is meant to print; no dpi on it: A4.",
                    ),
                    (
                        "a4",
                        "scaled to A4",
                        "Each picture is scaled to fit an A4 page (turned to match the picture).",
                    ),
                    (
                        "letter",
                        "scaled to Letter",
                        "Each picture is scaled to fit a US Letter page (turned to match).",
                    ),
                ),
            ),
            OUTPUT,
        ),
    ),
    Tool(
        "pdf2img",
        "PDF to images",
        "Save pages as PNG or JPG pictures",
        "pdf",
        _build_pdf2img,
        fields=(
            Field(
                "format",
                "Format",
                "choice",
                ("png", "jpg"),
                "png",
                details=(
                    (
                        "png",
                        "exact, larger files",
                        "PNG keeps every pixel exactly; the files are larger.",
                    ),
                    (
                        "jpg",
                        "smaller, JPEG q90",
                        "JPG files are much smaller; JPEG quality 90, a little detail is lost.",
                    ),
                ),
            ),
            Field("dpi", "Resolution (DPI)", "int", default="150", hint="36-1200"),
            Field("pages", "Only pages", "text", hint="optional, empty = all pages"),
            PASSWORD,
            OUTPUT,
        ),
    ),
    Tool(
        "lock",
        "Lock",
        "Protect with a password (AES-256)",
        "pdf",
        _build_lock,
        fields=(
            Field("new_password", "New password", "password"),
            Field("confirm", "Repeat password", "password"),
            Field(
                "password",
                "Current password",
                "password",
                hint="only if the PDF is already protected",
            ),
            OUTPUT,
        ),
    ),
    Tool(
        "unlock",
        "Unlock",
        "Remove a password you know",
        "pdf",
        _build_unlock,
        fields=(Field("password", "Password", "password"), OUTPUT),
    ),
    Tool(
        "clean",
        "Clean metadata",
        "Remove author, dates, XMP and photo GPS",
        "pdf",
        _build_clean,
        fields=(PASSWORD, OUTPUT),
    ),
    Tool(
        "info",
        "Info",
        "Pages, size, encryption, metadata, images",
        "pdf",
        _build_info,
        fields=(PASSWORD,),
    ),
)

TOOLS_BY_ID = {t.id: t for t in TOOLS}
