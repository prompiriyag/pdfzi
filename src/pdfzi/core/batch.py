"""Run one operation over many files, collecting per-file results and errors."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pdfzi.core.errors import OperationCancelledError, PdfziError
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult


@dataclass
class BatchEntry:
    source: Path
    result: OperationResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "ok": self.ok,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
        }


def run_batch(
    sources: Sequence[Path],
    func: Callable[[Path, int], OperationResult],
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> list[BatchEntry]:
    """Call ``func(source, index)`` for each source; one failure never stops the others.

    If the user cancels, the file being processed is recorded as cancelled, the remaining
    files are not started, and the entries so far are returned (check ``cancel.cancelled``).
    """
    entries: list[BatchEntry] = []
    total = len(sources)
    for i, source in enumerate(sources):
        try:
            report(progress, i, total, source.name, cancel)
            entries.append(BatchEntry(source, result=func(source, i)))
        except OperationCancelledError as exc:
            entries.append(BatchEntry(source, error=str(exc)))
            return entries
        except PdfziError as exc:
            entries.append(BatchEntry(source, error=str(exc)))
    report(progress, total, total, "")
    return entries
