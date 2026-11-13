"""``merge``: combine several PDFs into one, keeping bookmarks where possible."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

import pikepdf
from pikepdf import Name

from pdfzi.core.errors import InvalidOptionError
from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.outline import read_bookmarks, shift_bookmarks, write_bookmarks
from pdfzi.core.output import DECRYPTED_NOTE, write_verified
from pdfzi.core.paths import is_same_file, output_path_for
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult


def merge_pdfs(
    sources: Sequence[Path],
    dest: Path | None = None,
    *,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Merge ``sources`` in the given order into one new PDF at ``dest``.

    ``dest`` defaults to ``<first-stem>-merged.pdf`` next to the first source.
    ``password`` is tried on every encrypted input; the result is not encrypted.
    """
    sources = list(sources)
    if len(sources) < 2:
        raise InvalidOptionError("Merging needs at least two PDF files.")
    dest = dest or output_path_for(sources[0], "merged")
    overwrites = force and any(is_same_file(dest, s) for s in sources)

    notes: list[str] = []
    with ExitStack() as stack:
        merged = stack.enter_context(pikepdf.Pdf.new())
        bookmarks = []
        dropped = 0
        has_forms = False
        any_encrypted = False
        warnings: list[str] = []
        total = len(sources)
        for i, source in enumerate(sources):
            report(progress, i, total + 1, f"Reading {source.name}", cancel)
            pdf = stack.enter_context(open_pdf(source, password, in_memory=overwrites))
            offset = len(merged.pages)
            nodes = read_bookmarks(pdf)
            merged.pages.extend(pdf.pages)
            bookmarks += shift_bookmarks(nodes, offset)
            has_forms = has_forms or Name.AcroForm in pdf.Root
            any_encrypted = any_encrypted or pdf.is_encrypted
            warnings += [n for n in fidelity_notes(pdf) if n not in warnings]

        dropped = write_bookmarks(merged, bookmarks, lambda p: p)
        if dropped:
            notes.append(f"{dropped} bookmark(s) could not be carried over.")
        if has_forms:
            notes.append("Interactive form fields are not preserved in the merged file.")

        if any_encrypted:
            notes.append(DECRYPTED_NOTE)
        notes += warnings
        report(progress, total, total + 1, "Writing result", cancel)
        page_total = len(merged.pages)
        bytes_in = sum(s.stat().st_size for s in sources)
        out_path = write_verified(merged, dest, force=force, expected_pages=page_total)

    report(progress, total + 1, total + 1, "Done")
    return OperationResult(
        command="merge",
        sources=sources,
        outputs=[out_path],
        pages_in=page_total,
        pages_out=page_total,
        bytes_in=bytes_in,
        bytes_out=out_path.stat().st_size,
        notes=notes,
    )
