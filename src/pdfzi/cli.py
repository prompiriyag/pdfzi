"""Command-line interface (Typer). All real work lives in :mod:`pdfzi.core`."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from pdfzi import __version__
from pdfzi.core.batch import BatchEntry, run_batch
from pdfzi.core.clean import clean_pdf
from pdfzi.core.compress import CompressResult, compress_pdf
from pdfzi.core.errors import InvalidOptionError, PdfziError
from pdfzi.core.fmt import format_size, parse_size
from pdfzi.core.imgtopdf import images_to_pdf
from pdfzi.core.info import PdfInfo, format_info_lines, get_info
from pdfzi.core.merge import merge_pdfs
from pdfzi.core.paths import IMAGE_SUFFIXES, PDF_SUFFIXES, expand_inputs, plan_outputs
from pdfzi.core.pdftoimg import pdf_to_images
from pdfzi.core.protect import lock_pdf, unlock_pdf
from pdfzi.core.results import OperationResult
from pdfzi.core.rotate import rotate_pdf
from pdfzi.core.subset import delete_pages, extract_pages, split_pdf

app = typer.Typer(
    name="pdfzi",
    help="Local-only PDF toolkit. Your files never leave this computer.",
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=False,
)

FilesArg = Annotated[
    list[str],
    typer.Argument(help="PDF files. Patterns like *.pdf are expanded by pdfzi itself."),
]
OutputOpt = Annotated[
    str | None,
    typer.Option("--output", "-o", help="Result file or folder (a folder for several inputs)."),
]
ForceOpt = Annotated[
    bool, typer.Option("--force", help="Overwrite an existing result instead of numbering it.")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]
PasswordOpt = Annotated[
    str | None,
    typer.Option(
        "--password",
        envvar="PDFZI_PASSWORD",
        help="Password for encrypted inputs (or set PDFZI_PASSWORD).",
    ),
]


@contextmanager
def _guard() -> Iterator[None]:
    """Turn argument-level errors (missing file, bad option) into one clean line."""
    try:
        yield
    except PdfziError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pdfzi {__version__}")
        raise typer.Exit


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is None:
        if not _is_interactive():
            typer.echo(ctx.get_help())
            typer.echo("\n(The interactive screen needs a terminal; showing help instead.)")
            raise typer.Exit
        from pdfzi.tui.app import run  # imported late: the CLI should start fast

        run()


# ---------------------------------------------------------------- output helpers


def _echo(message: str, *, err: bool = False) -> None:
    """Print, falling back to ASCII arrows on consoles that cannot show them."""
    stream = sys.stderr if err else sys.stdout
    try:
        message.encode(getattr(stream, "encoding", None) or "utf-8")
    except (UnicodeEncodeError, LookupError):
        message = message.replace("→", "->")
    typer.echo(message, err=err)


def _json_text(data: Any) -> str:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    return json.dumps(data, indent=2, ensure_ascii=encoding != "utf8")


def _describe(result: OperationResult) -> str:
    parts = []
    if result.pages_out is not None:
        parts.append(f"{result.pages_out} page{'s' if result.pages_out != 1 else ''}")
    if result.size_summary and result.command == "compress":
        parts.append(result.size_summary)
    elif result.bytes_out is not None:
        parts.append(format_size(result.bytes_out))
    return ", ".join(parts)


def _error_line(entry: BatchEntry) -> str:
    prefix = f"{entry.source.name}:"
    message = entry.error or ""
    return f"[error] {message}" if message.startswith(prefix) else f"[error] {prefix} {message}"


def _finish(entries: list[BatchEntry], as_json: bool) -> None:
    """Print every entry and exit with status 1 if any failed."""
    if as_json:
        typer.echo(_json_text([e.to_dict() for e in entries]))
    else:
        for e in entries:
            if e.result is None:
                _echo(_error_line(e), err=True)
                continue
            outs = e.result.outputs
            if not outs:
                _echo(f"[ok] {e.source.name}: left unchanged ({_describe(e.result)})")
            else:
                target = outs[0].name if len(outs) == 1 else f"{len(outs)} files"
                _echo(f"[ok] {e.source.name} -> {target} ({_describe(e.result)})")
            for note in e.result.notes:
                _echo(f"     note: {note}")
    if any(not e.ok for e in entries):
        raise typer.Exit(1)


def _run_each(
    files: list[str],
    suffix: str,
    output: str | None,
    as_json: bool,
    op: Callable[[Path, Path], OperationResult],
) -> None:
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)
        dests = plan_outputs(sources, suffix, output)
    entries = run_batch(sources, lambda source, i: op(source, dests[i]))
    _finish(entries, as_json)


# ---------------------------------------------------------------- commands


@app.command()
def info(files: FilesArg, json_out: JsonOpt = False, password: PasswordOpt = None) -> None:
    """Show pages, size, version, encryption, metadata and image count."""
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)
    rows: list[tuple[Path, PdfInfo | None, str | None]] = []
    for source in sources:
        try:
            rows.append((source, get_info(source, password), None))
        except PdfziError as exc:
            rows.append((source, None, str(exc)))

    if json_out:
        typer.echo(
            _json_text(
                [
                    {
                        "source": str(s),
                        "ok": i is not None,
                        "info": i.to_dict() if i else None,
                        "error": e,
                    }
                    for s, i, e in rows
                ]
            )
        )
    else:
        for source, data, error in rows:
            if data is None:
                typer.echo(f"[error] {source.name}: {error}", err=True)
            else:
                _print_info(data)
    if any(i is None for _, i, _ in rows):
        raise typer.Exit(1)


def _print_info(d: PdfInfo) -> None:
    for line in format_info_lines(d):
        if "password needed" in line:
            line += " (use --password)"
        typer.echo(line)


@app.command()
def merge(
    files: FilesArg,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Merge PDFs, in the order given, into one new file."""
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)
        if len(sources) < 2:
            raise InvalidOptionError("Merging needs at least two PDF files.")
        dest = plan_outputs(sources[:1], "merged", output)[0]
    entries = run_batch(
        sources[:1],
        lambda _s, _i: merge_pdfs(sources, dest, force=force, password=password),
    )
    entries[0].source = sources[0]
    if entries[0].result is not None and not json_out:
        names = ", ".join(s.name for s in sources)
        typer.echo(f"Merged {len(sources)} files: {names}")
    _finish(entries, json_out)


@app.command()
def split(
    files: FilesArg,
    every: Annotated[
        int | None, typer.Option("--every", help="Pieces of N pages (default 1).")
    ] = None,
    ranges: Annotated[
        str | None, typer.Option("--ranges", help="One file per range, e.g. 1-3,7,10-.")
    ] = None,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Split into one file per page, per N pages, or per page range."""
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)

    def op(source: Path, i: int) -> OperationResult:
        folder = None
        if output is not None:
            folder = Path(output) / f"{source.stem}-split" if len(sources) > 1 else Path(output)
        return split_pdf(
            source, every=every, ranges=ranges, out_dir=folder, force=force, password=password
        )

    _finish(run_batch(sources, op), json_out)


@app.command()
def compress(
    files: FilesArg,
    level: Annotated[
        str | None,
        typer.Option("--level", help="lossless (default), medium or high."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Aim for this size or smaller, e.g. 2MB or 500KB (a plain number means MB). "
            "Picks the level.",
        ),
    ] = None,
    grayscale: Annotated[
        bool, typer.Option("--grayscale", help="Convert colour images to grey (medium/high).")
    ] = False,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Shrink PDFs. Text stays text; a file that cannot get smaller is left alone."""
    with _guard():
        target_bytes = parse_size(target, default_unit="m") if target is not None else None

    def op(source: Path, dest: Path) -> CompressResult:
        return compress_pdf(
            source, dest, level=level or "lossless", target=target_bytes, grayscale=grayscale,
            force=force, password=password,
        )  # fmt: skip

    _run_each(files, "compressed", output, json_out, op)


@app.command()
def extract(
    files: FilesArg,
    pages: Annotated[str, typer.Option("--pages", help="Pages to keep, e.g. 1-3,7,10-.")],
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Copy the chosen pages into a new file."""
    _run_each(
        files, "extracted", output, json_out,
        lambda s, d: extract_pages(s, pages, d, force=force, password=password),
    )  # fmt: skip


@app.command()
def delete(
    files: FilesArg,
    pages: Annotated[str, typer.Option("--pages", help="Pages to remove, e.g. 2,5-6.")],
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Write a copy without the chosen pages."""
    _run_each(
        files, "removed", output, json_out,
        lambda s, d: delete_pages(s, pages, d, force=force, password=password),
    )  # fmt: skip


@app.command()
def rotate(
    files: FilesArg,
    angle: Annotated[int, typer.Option("--angle", help="Clockwise: 90, 180 or 270.")],
    pages: Annotated[str | None, typer.Option("--pages", help="Only these pages.")] = None,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Rotate the whole file or only some pages."""
    _run_each(
        files, "rotated", output, json_out,
        lambda s, d: rotate_pdf(s, angle, pages, d, force=force, password=password),
    )  # fmt: skip


@app.command(name="img2pdf")
def img2pdf_cmd(
    images: Annotated[
        list[str],
        typer.Argument(
            help="JPG/PNG images, one page each, in the order given (or sorted for patterns)."
        ),
    ],
    page_size: Annotated[
        str, typer.Option("--page-size", help="fit (page = image), a4 or letter.")
    ] = "fit",
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Combine JPG/PNG images into one PDF (JPEGs are embedded without re-encoding)."""
    with _guard():
        sources = expand_inputs(images, suffixes=IMAGE_SUFFIXES)
        dest = plan_outputs(sources[:1], "images", output)[0]
    entries = run_batch(
        sources[:1], lambda _s, _i: images_to_pdf(sources, dest, page_size=page_size, force=force)
    )
    if entries[0].result is not None and not json_out:
        _echo(f"Combined {len(sources)} image(s): " + ", ".join(s.name for s in sources))
    _finish(entries, json_out)


@app.command(name="pdf2img")
def pdf2img_cmd(
    files: FilesArg,
    fmt: Annotated[str, typer.Option("--format", help="png or jpg.")] = "png",
    dpi: Annotated[int, typer.Option("--dpi", help="Resolution, 36-1200.")] = 150,
    pages: Annotated[str | None, typer.Option("--pages", help="Only these pages.")] = None,
    quality: Annotated[int, typer.Option("--quality", help="JPEG quality 1-95.")] = 90,
    output: Annotated[
        str | None, typer.Option("--output", "-o", help="Folder for the images.")
    ] = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Render pages to PNG or JPG images, one file per page."""
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)

    def op(source: Path, i: int) -> OperationResult:
        folder = None
        if output is not None:
            folder = Path(output) / f"{source.stem}-pages" if len(sources) > 1 else Path(output)
        return pdf_to_images(
            source, fmt=fmt, dpi=dpi, pages=pages, quality=quality, out_dir=folder,
            force=force, password=password,
        )  # fmt: skip

    _finish(run_batch(sources, op), json_out)


def _ask_password(value: str | None, label: str, *, confirm: bool = False) -> str:
    if value:
        return value
    try:
        return typer.prompt(label, hide_input=True, confirmation_prompt=confirm)
    except typer.Abort:
        raise InvalidOptionError(
            "A password is required: use --password, set PDFZI_PASSWORD, or type it when asked."
        ) from None


@app.command()
def lock(
    files: FilesArg,
    password: Annotated[
        str | None,
        typer.Option(
            "--password", envvar="PDFZI_PASSWORD", help="Password to set (asked if omitted)."
        ),
    ] = None,
    owner_password: Annotated[
        str | None,
        typer.Option(
            "--owner-password",
            envvar="PDFZI_OWNER_PASSWORD",
            help="Separate owner password (optional).",
        ),
    ] = None,
    input_password: Annotated[
        str | None,
        typer.Option(
            "--input-password",
            envvar="PDFZI_INPUT_PASSWORD",
            help="Current password, if the input is already protected.",
        ),
    ] = None,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Protect PDFs with a password (AES-256)."""
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)
        dests = plan_outputs(sources, "locked", output)
        new_password = _ask_password(password, "New password", confirm=True)
    entries = run_batch(
        sources,
        lambda s, i: lock_pdf(
            s, new_password, dests[i], owner_password=owner_password,
            input_password=input_password, force=force,
        ),
    )  # fmt: skip
    _finish(entries, json_out)


@app.command()
def unlock(
    files: FilesArg,
    password: Annotated[
        str | None,
        typer.Option(
            "--password", envvar="PDFZI_PASSWORD", help="The PDF's password (asked if omitted)."
        ),
    ] = None,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Remove the password from PDFs, when you know it."""
    with _guard():
        sources = expand_inputs(files, suffixes=PDF_SUFFIXES)
        dests = plan_outputs(sources, "unlocked", output)
        current = _ask_password(password, "Password")
    _finish(
        run_batch(sources, lambda s, i: unlock_pdf(s, current, dests[i], force=force)), json_out
    )


@app.command()
def clean(
    files: FilesArg,
    output: OutputOpt = None,
    force: ForceOpt = False,
    json_out: JsonOpt = False,
    password: PasswordOpt = None,
) -> None:
    """Remove metadata (author, dates, XMP, photo EXIF/GPS) for privacy."""
    _run_each(
        files, "clean", output, json_out,
        lambda s, d: clean_pdf(s, d, force=force, password=password),
    )  # fmt: skip


# ---------------------------------------------------------------- entry point


def _is_closed_pipe(exc: BaseException) -> bool:
    """True when the program reading our output (``| head``) went away. Not a bug."""
    if isinstance(exc, BrokenPipeError):
        return True
    # Windows reports a closed pipe as "Invalid argument" and names no file.
    return (
        isinstance(exc, OSError)
        and sys.platform == "win32"
        and exc.errno == errno.EINVAL
        and exc.filename is None
    )


def _leave_quietly() -> NoReturn:
    """Stop without a message; point stdout at nowhere so the final flush cannot complain."""
    with contextlib.suppress(OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    raise SystemExit(1)


def main() -> None:
    """Run the CLI. User-facing errors print one clear line, never a traceback."""
    # Thai (or any non-ASCII) file names must never crash printing on legacy consoles.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")

    try:
        app()
    except PdfziError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        typer.echo("Interrupted.", err=True)
        raise SystemExit(130) from None
    except Exception as exc:
        if _is_closed_pipe(exc):
            _leave_quietly()
        typer.echo(
            f"Unexpected error: {exc}\nThis is a bug in pdfzi. Set PDFZI_DEBUG=1 for details.",
            err=True,
        )
        if os.environ.get("PDFZI_DEBUG"):
            traceback.print_exc()
        raise SystemExit(1) from None
