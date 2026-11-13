"""``rotate``: turn pages by 90/180/270 degrees (clockwise), in a new file."""

from __future__ import annotations

from pathlib import Path

from pdfzi.core.errors import InvalidOptionError
from pdfzi.core.fidelity import fidelity_notes
from pdfzi.core.output import DECRYPTED_NOTE, write_verified
from pdfzi.core.pageranges import page_numbers, parse_page_ranges
from pdfzi.core.paths import is_same_file, output_path_for
from pdfzi.core.pdfio import open_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult

VALID_ANGLES = (90, 180, 270)


def normalize_angle(angle: int) -> int:
    """Accept 90/180/270 and their negatives (-90 is 270, ...); reject anything else."""
    normalized = angle % 360
    if normalized not in VALID_ANGLES:
        raise InvalidOptionError("Angle must be 90, 180 or 270 degrees (clockwise).")
    return normalized


def rotate_pdf(
    source: Path,
    angle: int,
    pages: str | None = None,
    dest: Path | None = None,
    *,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> OperationResult:
    """Rotate ``pages`` (default: all) clockwise by ``angle``, adding to any existing rotation."""
    angle = normalize_angle(angle)
    dest = dest or output_path_for(source, "rotated")
    report(progress, 0, 2, f"Reading {source.name}", cancel)
    with open_pdf(source, password, in_memory=force and is_same_file(dest, source)) as pdf:
        count = len(pdf.pages)
        if pages is None:
            targets = list(range(count))
        else:
            targets = sorted({p - 1 for p in page_numbers(parse_page_ranges(pages, count))})
        for index in targets:
            pdf.pages[index].rotate(angle, relative=True)
        report(progress, 1, 2, "Writing result", cancel)
        was_encrypted = pdf.is_encrypted
        warnings = fidelity_notes(pdf)
        out_path = write_verified(pdf, dest, force=force, expected_pages=count)
    report(progress, 2, 2, "Done")
    return OperationResult(
        command="rotate",
        sources=[source],
        outputs=[out_path],
        pages_in=count,
        pages_out=count,
        bytes_in=source.stat().st_size,
        bytes_out=out_path.stat().st_size,
        notes=[f"Rotated {len(targets)} page(s) by {angle} degrees."]
        + ([DECRYPTED_NOTE] if was_encrypted else [])
        + warnings,
    )
