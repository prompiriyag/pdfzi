"""Operations that build a new document from some pages of another:
``extract``, ``delete`` and ``split``.

Building a fresh document (instead of deleting pages in place) means removed pages leave
no trace: nothing in the output keeps their content alive. Title/author are copied over;
form fields, page labels and named destinations are not.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path

import pikepdf

from pdfzi.core.errors import InvalidOptionError, PageRangeError
from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.outline import BookmarkNode, read_bookmarks, write_bookmarks
from pdfzi.core.output import DECRYPTED_NOTE, copy_docinfo, write_verified
from pdfzi.core.pageranges import page_numbers, parse_page_ranges
from pdfzi.core.paths import is_same_file, output_path_for, unique_dir
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult


def _count(nodes: list[BookmarkNode]) -> int:
    return sum(1 + _count(n.children) for n in nodes)


def _build_subset(
    src: pikepdf.Pdf, indices: Sequence[int], bookmarks: list[BookmarkNode]
) -> tuple[pikepdf.Pdf, int]:
    """New document with ``src``'s pages at ``indices`` (0-based, in that order).

    Returns the document and how many bookmarks had to be dropped.
    """
    new = pikepdf.Pdf.new()
    new.pages.extend([src.pages[i] for i in indices])
    copy_docinfo(src, new)
    first_position: dict[int, int] = {}
    for position, index in enumerate(indices):
        first_position.setdefault(index, position)
    return new, write_bookmarks(new, bookmarks, first_position.get)


def extract_pages(
    source: Path,
    pages: str,
    dest: Path | None = None,
    *,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Write the pages named by ``pages`` (e.g. ``1-3,7,10-``) to a new PDF, in that order."""
    return _single_output(
        "extract", "extracted", source, pages, dest, keep=True,
        force=force, password=password, progress=progress, cancel=cancel,
    )  # fmt: skip


def delete_pages(
    source: Path,
    pages: str,
    dest: Path | None = None,
    *,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Write a copy of ``source`` without the pages named by ``pages``."""
    return _single_output(
        "delete", "removed", source, pages, dest, keep=False,
        force=force, password=password, progress=progress, cancel=cancel,
    )  # fmt: skip


def _single_output(
    command: str,
    suffix: str,
    source: Path,
    spec: str,
    dest: Path | None,
    *,
    keep: bool,
    force: bool,
    password: str | None,
    progress: ProgressCallback | None,
    cancel: CancelToken | None,
) -> OperationResult:
    dest = dest or output_path_for(source, suffix)
    report(progress, 0, 3, f"Reading {source.name}", cancel)
    with open_pdf(source, password, in_memory=force and is_same_file(dest, source)) as pdf:
        count = len(pdf.pages)
        chosen = page_numbers(parse_page_ranges(spec, count))
        if keep:
            indices = [p - 1 for p in chosen]
        else:
            removed = set(chosen)
            indices = [i for i in range(count) if (i + 1) not in removed]
            if not indices:
                raise PageRangeError("That would delete every page. Keep at least one page.")
        bookmarks = read_bookmarks(pdf)
        was_encrypted = pdf.is_encrypted
        warnings = fidelity_notes(pdf)
        report(progress, 1, 3, "Building new document", cancel)
        new, dropped = _build_subset(pdf, indices, bookmarks)
        with new:
            report(progress, 2, 3, "Writing result", cancel)
            out_path = write_verified(new, dest, force=force, expected_pages=len(indices))
    report(progress, 3, 3, "Done")
    notes = []
    if dropped:
        total = _count(bookmarks)
        notes.append(f"{dropped} of {total} bookmark(s) pointed at removed pages; dropped.")
    if was_encrypted:
        notes.append(DECRYPTED_NOTE)
    notes += warnings
    return OperationResult(
        command=command,
        sources=[source],
        outputs=[out_path],
        pages_in=count,
        pages_out=len(indices),
        bytes_in=source.stat().st_size,
        bytes_out=out_path.stat().st_size,
        notes=notes,
    )


def _split_groups(count: int, every: int | None, ranges: str | None) -> list[list[int]]:
    """0-based page indices for every output file."""
    if every is not None and ranges is not None:
        raise InvalidOptionError("Use either --every or --ranges, not both.")
    if ranges is not None:
        return [[p - 1 for p in r] for r in parse_page_ranges(ranges, count)]
    step = 1 if every is None else every
    if step < 1:
        raise InvalidOptionError("--every must be 1 or more.")
    return [list(range(i, min(i + step, count))) for i in range(0, count, step)]


def split_pdf(
    source: Path,
    *,
    every: int | None = None,
    ranges: str | None = None,
    out_dir: Path | None = None,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Split ``source`` into several PDFs inside ``out_dir``.

    By default every page becomes its own file; ``every=N`` makes chunks of N pages;
    ``ranges="1-3,7,10-"`` makes one file per range. ``out_dir`` defaults to a new folder
    ``<stem>-split`` next to the source. If anything fails or is cancelled, the files
    created by this run are removed again.
    """
    created_dir = False
    created: list[Path] = []
    total_dropped = 0
    with open_pdf(source, password) as pdf:
        count = len(pdf.pages)
        groups = _split_groups(count, every, ranges)
        bookmarks = read_bookmarks(pdf)
        was_encrypted = pdf.is_encrypted
        warnings = fidelity_notes(pdf)
        width = len(str(count))
        folder = out_dir or unique_dir(source.with_name(f"{source.stem}-split"))
        try:
            if not folder.exists():
                folder.mkdir(parents=True)
                created_dir = True
            for n, group in enumerate(groups):
                first, last = group[0] + 1, group[-1] + 1
                label = f"p{first:0{width}d}" + (f"-{last:0{width}d}" if last != first else "")
                name = f"{source.stem}-{label}.pdf"
                report(progress, n, len(groups), name, cancel)
                new, dropped = _build_subset(pdf, group, bookmarks)
                total_dropped += dropped
                with new:
                    path = write_verified(
                        new, folder / name, force=force, expected_pages=len(group)
                    )
                created.append(path)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            if created_dir:
                with contextlib.suppress(OSError):
                    folder.rmdir()
            raise

    report(progress, len(groups), len(groups), "Done")
    notes = [f"Created {len(created)} file(s) in {folder}."]
    if total_dropped:
        notes.append("Bookmarks pointing at pages in other files were left out.")
    if was_encrypted:
        notes.append(DECRYPTED_NOTE)
    notes += warnings
    return OperationResult(
        command="split",
        sources=[source],
        outputs=created,
        pages_in=count,
        pages_out=sum(len(g) for g in groups),
        bytes_in=source.stat().st_size,
        bytes_out=sum(p.stat().st_size for p in created),
        notes=notes,
    )
