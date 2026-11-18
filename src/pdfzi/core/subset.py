"""Operations that build a new document from some pages of another:
``extract``, ``delete`` and ``split``.

Building a fresh document (instead of deleting pages in place) means removed pages leave
no trace: nothing in the output keeps their content alive. Title/author are copied over;
form fields, page labels and named destinations are not.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pikepdf

from pdfzi.core.errors import CorruptPdfError, InvalidOptionError, PageRangeError
from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.fmt import plural
from pdfzi.core.limits import refuse_oversized_content
from pdfzi.core.outline import BookmarkNode, read_bookmarks, write_bookmarks
from pdfzi.core.output import (
    DECRYPTED_NOTE,
    copy_docinfo,
    note_kept_files,
    publish_verified,
    undo_new_files,
    write_verified,
)
from pdfzi.core.pageranges import page_numbers, parse_page_ranges
from pdfzi.core.paths import is_same_file, output_path_for, unique_dir
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult


def _count(nodes: list[BookmarkNode]) -> int:
    return sum(1 + _count(n.children) for n in nodes)


# Keys through which an annotation can reach objects that are not on its own page: a form
# field's parent (whose /Kids lists the widgets, and values, of fields on other pages), the
# comment a reply answers (/IRT), and a pop-up note (/Popup) or the comment it belongs to.
_ANNOT_LINKS = ("/Parent", "/IRT", "/Popup")


def _cut_ties_to_left_out_pages(new: pikepdf.Pdf) -> None:
    """Make sure nothing in ``new`` keeps content of the pages that were left out.

    Copying a page copies everything it refers to. Links to other pages become null by
    themselves, but three things can still carry content of a left-out page along:

    * annotation links (see ``_ANNOT_LINKS``): kept only when they point at an annotation
      that is itself on a kept page. Form fields are not carried over anyway, so a widget
      looks the same without its parent field.
    * article thread beads (``/B``): thread chains run through other pages; threads are
      not carried over either, so the beads are useless and removed.
    * a resource dictionary shared by many pages (common in generated PDFs) lists the
      images and fonts of every page; whatever a kept page does not draw is removed.
    """
    pages_annots = []
    kept: set[tuple[int, int]] = set()
    for page in new.pages:
        annots = page.obj.get("/Annots")
        found = [a for a in annots if isinstance(a, pikepdf.Dictionary)] if annots else []
        pages_annots.append(found)
        kept.update(a.objgen for a in found if a.is_indirect)
        if "/B" in page.obj:
            del page.obj["/B"]
    for annots in pages_annots:
        for annot in annots:
            for key in _ANNOT_LINKS:
                target = annot.get(key)
                if target is None:
                    continue
                if not (
                    isinstance(target, pikepdf.Dictionary)
                    and target.is_indirect
                    and target.objgen in kept
                ):
                    del annot[key]
    new.remove_unreferenced_resources()


def _build_subset(
    src: pikepdf.Pdf, indices: Sequence[int], bookmarks: list[BookmarkNode], source: Path
) -> tuple[pikepdf.Pdf, int]:
    """New document with ``src``'s pages at ``indices`` (0-based, in that order).

    Returns the document and how many bookmarks had to be dropped. ``source`` is only used
    to name the file in error messages.
    """
    new = pikepdf.Pdf.new()
    try:
        new.pages.extend([src.pages[i] for i in indices])
        _cut_ties_to_left_out_pages(new)
    except pikepdf.PdfError as exc:
        # Leaving the step out could carry content of removed pages along, so the file is
        # refused instead (seen with forms whose /Resources is not a dictionary).
        new.close()
        raise CorruptPdfError(source, "its pages could not be separated") from exc
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
        refuse_oversized_content(pdf, source.name, indices)
        new, dropped = _build_subset(pdf, indices, bookmarks, source)
        with new:
            report(progress, 2, 3, "Writing result", cancel)
            out_path = write_verified(
                new, dest, force=force, expected_pages=len(indices), sources=[source]
            )
    report(progress, 3, 3, "Done")
    notes = []
    if dropped:
        total = _count(bookmarks)
        notes.append(f"{dropped} of {plural(total, 'bookmark')} pointed at removed pages; dropped.")
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
    ``<stem>-split`` next to the source. If anything fails or is cancelled, the new files
    this run made are removed again; files that already replaced an older one (``force``)
    are kept, and the error message lists them.
    """
    created_dir = False
    created: list[Path] = []
    replaced: list[Path] = []  # results that took the place of an older file (force)
    total_dropped = 0
    with open_pdf(source, password) as pdf:
        count = len(pdf.pages)
        groups = _split_groups(count, every, ranges)
        bookmarks = read_bookmarks(pdf)
        was_encrypted = pdf.is_encrypted
        warnings = fidelity_notes(pdf)
        width = len(str(count))
        refuse_oversized_content(pdf, source.name)
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
                new, dropped = _build_subset(pdf, group, bookmarks, source)
                total_dropped += dropped
                with new:
                    out = publish_verified(
                        new, folder / name, force=force, expected_pages=len(group),
                        sources=[source],
                    )  # fmt: skip
                assert out.path is not None
                created.append(out.path)
                if out.replaced:
                    replaced.append(out.path)
        except BaseException as exc:
            undo_new_files(created, replaced, folder if created_dir else None)
            note_kept_files(exc, replaced)
            raise

    report(progress, len(groups), len(groups), "Done")
    notes = [f"Created {plural(len(created), 'file')}."]
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
