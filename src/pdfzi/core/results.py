"""Result types returned by core operations. Plain data, JSON-friendly."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdfzi.core.fmt import format_change


@dataclass
class OperationResult:
    """Outcome of one core operation on one logical job (which may use several inputs)."""

    command: str
    sources: list[Path] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    pages_in: int | None = None
    pages_out: int | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def size_summary(self) -> str | None:
        """``before → after (N% smaller)`` when both sizes are known."""
        if self.bytes_in is None or self.bytes_out is None:
            return None
        return format_change(self.bytes_in, self.bytes_out)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form used by ``--json``."""
        return {
            "command": self.command,
            "sources": [str(p) for p in self.sources],
            "outputs": [str(p) for p in self.outputs],
            "pages_in": self.pages_in,
            "pages_out": self.pages_out,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "notes": list(self.notes),
        }
