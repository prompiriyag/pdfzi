"""Reading and rewriting bookmarks (the PDF outline) when pages move between documents.

Only bookmarks that point at a concrete page are carried over. Named destinations and
other exotic targets cannot be resolved here and are counted as dropped. Scroll
position/zoom of a bookmark is not preserved: it jumps to the top of the page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pikepdf
from pikepdf import Name, OutlineItem


@dataclass
class BookmarkNode:
    title: str
    page: int | None  # 0-based page index; None if the target could not be resolved
    children: list[BookmarkNode] = field(default_factory=list)


def read_bookmarks(pdf: pikepdf.Pdf) -> list[BookmarkNode]:
    """Return the bookmark tree of ``pdf`` (empty when it has none or it is unreadable)."""
    if Name.Outlines not in pdf.Root:
        return []
    page_index = {page.objgen: i for i, page in enumerate(pdf.pages)}
    try:
        # Constructed without ``with``: leaving the context manager would write the
        # outline back into the (source) document.
        outline = pikepdf.Outline(pdf)
        return [_convert(item, page_index) for item in outline.root]
    except (
        pikepdf.PdfError,
        pikepdf.OutlineStructureError,  # e.g. an entry that points to something else
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
    ):
        return []


def _convert(item: OutlineItem, page_index: dict[tuple[int, int], int]) -> BookmarkNode:
    return BookmarkNode(
        title=str(item.title),
        page=_resolve_page(item, page_index),
        children=[_convert(child, page_index) for child in item.children],
    )


def _resolve_page(item: OutlineItem, page_index: dict[tuple[int, int], int]) -> int | None:
    dest = item.destination
    if dest is None and item.action is not None:
        action = item.action
        if action.get("/S") == Name.GoTo:
            dest = action.get("/D")
    if isinstance(dest, pikepdf.Array) and len(dest) > 0:
        first = dest[0]
        if isinstance(first, pikepdf.Object) and first.is_indirect:
            return page_index.get(first.objgen)
    return None


def shift_bookmarks(nodes: list[BookmarkNode], offset: int) -> list[BookmarkNode]:
    """Copy ``nodes`` with every page index moved by ``offset`` pages."""
    return [
        BookmarkNode(
            n.title,
            None if n.page is None else n.page + offset,
            shift_bookmarks(n.children, offset),
        )
        for n in nodes
    ]


def write_bookmarks(
    pdf: pikepdf.Pdf,
    nodes: list[BookmarkNode],
    page_map: Callable[[int], int | None],
) -> int:
    """Replace the outline of ``pdf`` with ``nodes`` remapped through ``page_map``.

    ``page_map`` turns an old page index into a page index in ``pdf`` (or ``None`` if that
    page is gone). A bookmark whose page is gone is dropped but its children are kept, moved
    up one level. Returns how many bookmarks were dropped.
    """
    dropped = 0

    def convert(node: BookmarkNode) -> list[OutlineItem]:
        nonlocal dropped
        kids = [item for child in node.children for item in convert(child)]
        target = None if node.page is None else page_map(node.page)
        if target is None:
            dropped += 1
            return kids
        item = OutlineItem(node.title, target)
        item.children.extend(kids)
        return [item]

    items = [item for node in nodes for item in convert(node)]
    if not items and Name.Outlines not in pdf.Root:
        return dropped
    with pdf.open_outline() as outline:
        outline.root.clear()
        outline.root.extend(items)
    return dropped
