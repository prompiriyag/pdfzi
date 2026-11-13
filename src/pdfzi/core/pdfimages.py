"""Finding image XObjects in a PDF, and how big they are drawn (``info`` and ``compress``)."""

from __future__ import annotations

import math
from collections.abc import Iterator

import pikepdf
from pikepdf import Name


def iter_images(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Object]:
    """Yield each distinct image XObject once, even if many pages share it.

    Looks inside form XObjects too. Inline images (``BI ... EI`` inside a content stream)
    are not image objects and are not reported.
    """
    seen: set[tuple[int, int]] = set()

    def walk(resources: pikepdf.Object | None) -> Iterator[pikepdf.Object]:
        if resources is None or Name.XObject not in resources:
            return
        for xobject in resources.XObject.values():
            key = xobject.objgen
            if key != (0, 0):
                if key in seen:
                    continue
                seen.add(key)
            subtype = xobject.get("/Subtype")
            if subtype == Name.Image:
                yield xobject
            elif subtype == Name.Form:
                yield from walk(xobject.get("/Resources"))

    for page in pdf.pages:
        yield from walk(page.obj.get("/Resources"))


# ---------------------------------------------------------------- how big images are drawn

Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_MAX_FORM_DEPTH = 8


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    """``m`` then ``n`` (PDF row-vector convention: a point is transformed by m first)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _numbers(values: object) -> Matrix | None:
    try:
        nums = tuple(float(v) for v in values)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return None
    return nums if len(nums) == 6 else None  # type: ignore[return-value]


def _page_resources(page: pikepdf.Page) -> pikepdf.Object | None:
    node: pikepdf.Object | None = page.obj
    while node is not None:  # /Resources may be inherited from the page tree
        if "/Resources" in node:
            return node.Resources
        node = node.get("/Parent")
    return None


def _user_unit(page: pikepdf.Page) -> float:
    """The page's ``/UserUnit`` (default 1). Large-format pages use it to scale their units."""
    try:
        unit = float(page.obj.get("/UserUnit", 1))
    except (TypeError, ValueError):
        return 1.0
    return unit if unit > 0 else 1.0


def image_extents(pdf: pikepdf.Pdf) -> dict[tuple[int, int], tuple[float, float]]:
    """How large each image is drawn, in inches: ``{objgen: (width, height)}``.

    Follows the page content (``q``/``Q``/``cm``/``Do``, also inside form XObjects). An image
    drawn more than once gets the largest size, so nothing is made too small for any use.
    Images that are never drawn in content pdfzi can read (annotations, patterns, inline
    images) are simply absent from the result.
    """
    found: dict[tuple[int, int], tuple[float, float]] = {}

    def run(
        content: pikepdf.Object,
        resources: pikepdf.Object | None,
        ctm: Matrix,
        depth: int,
        path: frozenset[tuple[int, int]],
    ) -> None:
        try:
            instructions = pikepdf.parse_content_stream(content)
        except (pikepdf.PdfError, ValueError, TypeError):
            return
        stack: list[Matrix] = []
        for instruction in instructions:
            op = str(instruction.operator)
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                if stack:
                    ctm = stack.pop()
            elif op == "cm":
                m = _numbers(instruction.operands)
                if m is not None:
                    ctm = _multiply(m, ctm)
            elif op == "Do" and resources is not None and Name.XObject in resources:
                xobject = resources.XObject.get(str(instruction.operands[0]))
                if xobject is None:
                    continue
                subtype = xobject.get("/Subtype")
                if subtype == Name.Image:
                    width = math.hypot(ctm[0], ctm[1]) / 72
                    height = math.hypot(ctm[2], ctm[3]) / 72
                    if width > 0 and height > 0 and xobject.objgen != (0, 0):
                        old = found.get(xobject.objgen, (0.0, 0.0))
                        found[xobject.objgen] = (max(old[0], width), max(old[1], height))
                elif subtype == Name.Form and depth < _MAX_FORM_DEPTH:
                    key = xobject.objgen
                    if key in path:
                        continue  # a form that draws itself
                    matrix = _numbers(xobject.get("/Matrix", _IDENTITY)) or _IDENTITY
                    run(
                        xobject,
                        xobject.get("/Resources", resources),
                        _multiply(matrix, ctm),
                        depth + 1,
                        path | {key},
                    )

    for page in pdf.pages:
        unit = _user_unit(page)  # 1 unit = unit/72 inch; almost always 1
        run(page, _page_resources(page), (unit, 0.0, 0.0, unit, 0.0, 0.0), 0, frozenset())
    return found
