"""Parsing of page-range expressions such as ``1-3,7,10-``.

Pages are 1-based and ranges are inclusive, matching what users see in a PDF viewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pdfzi.core.errors import PageRangeError

_PART = re.compile(r"^(\d+)(?:\s*-\s*(\d*))?$")


@dataclass(frozen=True)
class PageRange:
    """Inclusive 1-based range of pages."""

    start: int
    end: int

    def __iter__(self):
        return iter(range(self.start, self.end + 1))

    def __len__(self) -> int:
        return self.end - self.start + 1


def parse_page_ranges(spec: str, page_count: int) -> list[PageRange]:
    """Parse ``spec`` against a document of ``page_count`` pages.

    Accepted parts, separated by commas: ``7`` (one page), ``1-3`` (a range) and
    ``10-`` (page 10 through the last page). Order and duplicates are preserved.
    """
    if page_count < 1:
        raise PageRangeError("The document has no pages.")
    if not spec or not spec.strip():
        raise PageRangeError("No pages given. Example: 1-3,7,10-")

    ranges: list[PageRange] = []
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            raise PageRangeError(f"Empty item in page list '{spec}'. Example: 1-3,7,10-")
        match = _PART.match(part)
        if not match:
            raise PageRangeError(
                f"Cannot understand '{part}'. Use numbers, ranges like 2-5, "
                "or open ranges like 10-. Example: 1-3,7,10-"
            )
        start = int(match.group(1))
        if match.group(2) is None:  # single page
            end = start
        elif match.group(2) == "":  # open range "10-"
            end = page_count
        else:
            end = int(match.group(2))

        if start < 1:
            raise PageRangeError("Page numbers start at 1.")
        if end < start:
            raise PageRangeError(f"Range '{part}' is backwards. Write it as {end}-{start}.")
        if start > page_count or end > page_count:
            bad = start if start > page_count else end
            raise PageRangeError(
                f"Page {bad} is out of range: the document has {page_count} "
                f"page{'s' if page_count != 1 else ''}."
            )
        ranges.append(PageRange(start, end))
    return ranges


def page_numbers(ranges: list[PageRange]) -> list[int]:
    """Flatten ranges into 1-based page numbers, keeping order and duplicates."""
    return [page for r in ranges for page in r]
