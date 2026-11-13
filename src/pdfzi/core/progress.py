"""Progress reporting and cooperative cancellation shared by every core operation."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from pdfzi.core.errors import OperationCancelledError


@dataclass(frozen=True)
class Progress:
    """A progress update. ``total`` is 0 when the amount of work is unknown."""

    done: int
    total: int
    message: str = ""

    @property
    def fraction(self) -> float | None:
        return self.done / self.total if self.total else None


ProgressCallback = Callable[[Progress], None]


class CancelToken:
    """Thread-safe cancellation flag. UIs call :meth:`cancel`; core calls :meth:`check`."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise :class:`OperationCancelledError` if cancellation was requested."""
        if self._event.is_set():
            raise OperationCancelledError


def report(
    callback: ProgressCallback | None,
    done: int,
    total: int,
    message: str = "",
    cancel: CancelToken | None = None,
) -> None:
    """Send a progress update (if anyone listens) and honour cancellation."""
    if cancel is not None:
        cancel.check()
    if callback is not None:
        callback(Progress(done, total, message))
