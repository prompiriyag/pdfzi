"""Human-readable formatting of counts, sizes and before/after comparisons."""

from __future__ import annotations

import re

from pdfzi.core.errors import InvalidOptionError

_UNITS = ("B", "KB", "MB", "GB", "TB")


def plural(n: int, word: str, words: str | None = None) -> str:
    """``1 page``, ``3 pages``: the number with the word in the right form.

    ``words`` is the plural when it is not ``word`` + "s".
    """
    return f"{n} {word if n == 1 else (words or word + 's')}"


def format_size(num_bytes: int) -> str:
    """Format a byte count using 1024-based units, e.g. ``1.5 MB``."""
    size = float(num_bytes)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def format_change(before: int, after: int) -> str:
    """Format ``before → after`` plus relative change, e.g. ``4.2 MB → 1.1 MB (74% smaller)``."""
    head = f"{format_size(before)} → {format_size(after)}"
    if before <= 0 or after == before:
        return f"{head} (unchanged)"
    percent = round(abs(before - after) / before * 100)
    word = "smaller" if after < before else "larger"
    return f"{head} ({percent}% {word})"


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b)?\s*$", re.IGNORECASE)
_FACTORS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(text: str, default_unit: str = "") -> int:
    """Parse ``2MB``, ``500 KB``, ``1.5m`` (1024-based units) into bytes.

    A number with no unit is in ``default_unit`` (``""`` = bytes, ``"k"``, ``"m"``, ...).
    """
    match = _SIZE.match(text)
    if not match:
        raise InvalidOptionError(
            f"Cannot understand size '{text}'. Examples: 2, 500KB, 1.5MB (use a dot, not a comma)."
        )
    bare = not match.group(2) and not match.group(3)  # "2", but not "2B": that is 2 bytes
    unit = (default_unit if bare else match.group(2)).lower()
    size = int(float(match.group(1)) * _FACTORS[unit])
    if size < 1:
        raise InvalidOptionError("The size must be greater than zero.")
    return size
