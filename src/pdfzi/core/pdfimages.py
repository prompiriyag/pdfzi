"""Finding image XObjects in a PDF, and how big they are drawn (``info`` and ``compress``)."""

from __future__ import annotations

import math
from collections.abc import Iterator

import pikepdf
from pikepdf import Name

from pdfzi.core.limits import MB, unpacked_content_size
from pdfzi.core.progress import CancelToken

# Forms nested deeper than this are not looked into. Real files nest a few levels; a crafted
# one could nest thousands to exhaust Python's recursion limit.
_MAX_WALK_DEPTH = 64


def iter_images(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Object]:
    """Yield each distinct image XObject once, even if many pages share it.

    Looks inside form XObjects too. Inline images (``BI ... EI`` inside a content stream)
    are not image objects and are not reported. Entries that are not proper XObjects are
    ignored.
    """
    seen: set[tuple[int, int]] = set()

    def walk(resources: pikepdf.Object | None, depth: int) -> Iterator[pikepdf.Object]:
        if not isinstance(resources, pikepdf.Dictionary) or Name.XObject not in resources:
            return
        xobjects = resources.XObject
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for xobject in xobjects.values():
            if not isinstance(xobject, pikepdf.Stream):
                continue  # every XObject is a stream; anything else is damage
            key = xobject.objgen
            if key != (0, 0):
                if key in seen:
                    continue
                seen.add(key)
            subtype = xobject.get("/Subtype")
            if subtype == Name.Image:
                yield xobject
            elif subtype == Name.Form and depth < _MAX_WALK_DEPTH:
                yield from walk(xobject.get("/Resources"), depth + 1)

    for page in pdf.pages:
        yield from walk(page.obj.get("/Resources"), 0)


# ---------------------------------------------------------------- how big images are drawn

Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
# Forms nested deeper than this are not followed: image_extents gives up instead (see
# TooComplexError). The time spent is bounded by MAX_FORM_WORK; this only keeps the walk
# well inside Python's recursion limit. Real files nest a few levels.
_MAX_FORM_DEPTH = 32


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


class TooComplexError(Exception):
    """The drawings cannot be measured completely, so no sizes are given at all.

    Raised when the forms take more than :data:`MAX_FORM_WORK` steps to follow, when forms
    are nested more than ``_MAX_FORM_DEPTH`` levels deep, when a form draws itself, when
    the content of a page or form cannot be read, or when it is too large to read safely
    (:data:`MAX_PARSE`, :data:`MAX_PARSE_TOTAL`). In each case an image may be drawn larger
    in the part that was not followed.
    """


# Steps (q, Q, cm and Do instructions) image_extents follows inside form XObjects before it
# gives up. A form drawn many times is followed every time, so a small crafted file can
# nest forms that each draw the next one many times and make the work grow exponentially.
# Real files stay far below this: a logo drawn on 10,000 pages is about 100,000 steps.
MAX_FORM_WORK = 2_000_000
# Unpacked drawing instructions read for one page or form, and for the whole file. Reading
# turns every instruction into Python objects, many times the size of the text, so a few KB
# of packed "q Q q Q ..." could otherwise take gigabytes. Pages past this are rare
# (detailed maps, CAD drawings); for them compress leaves the images as they are.
MAX_PARSE = 4 * MB
MAX_PARSE_TOTAL = 256 * MB
_CANCEL_EVERY = 10_000
_DRAW_OPS = frozenset({"q", "Q", "cm", "Do"})

_Step = tuple[str, object]  # ("cm", Matrix | None), ("Do", name), ("q", None), ("Q", None)


def _steps(content: pikepdf.Object) -> tuple[list[_Step], int]:
    """The instructions of ``content`` (a page or form) that matter for where images are
    drawn, and how many bytes of instructions were read.

    Raises :class:`TooComplexError` if the content cannot be read, or is too large to read
    safely: what it draws, and how large, is then unknown.
    """
    size = unpacked_content_size(content.obj if isinstance(content, pikepdf.Page) else content,
                                 MAX_PARSE)  # fmt: skip
    if size is None:
        raise TooComplexError
    try:
        instructions = pikepdf.parse_content_stream(content, " ".join(_DRAW_OPS))
    except (pikepdf.PdfError, ValueError, TypeError) as exc:
        raise TooComplexError from exc
    steps: list[_Step] = []
    for instruction in instructions:
        op = str(instruction.operator)
        if op == "cm":
            steps.append((op, _numbers(instruction.operands)))
        elif op == "Do":
            # The name object itself, not str(): a name need not be valid UTF-8.
            if instruction.operands and isinstance(instruction.operands[0], Name):
                steps.append((op, instruction.operands[0]))
        elif op in _DRAW_OPS:
            steps.append((op, None))
    return steps, size


def image_extents(
    pdf: pikepdf.Pdf, cancel: CancelToken | None = None
) -> dict[tuple[int, int], tuple[float, float]]:
    """How large each image is drawn, in inches: ``{objgen: (width, height)}``.

    Follows the page content (``q``/``Q``/``cm``/``Do``, also inside form XObjects). An image
    drawn more than once gets the largest size, so nothing is made too small for any use.
    Images that are never drawn in content pdfzi can read (annotations, patterns, inline
    images) are simply absent from the result.

    Raises :class:`TooComplexError` when the drawings cannot be followed to the end: the
    forms take more than :data:`MAX_FORM_WORK` steps, nest too deep or draw themselves, or
    some content cannot be read. The sizes found up to then are not returned: a larger use
    of an image may be in the part that was not followed.
    """
    found: dict[tuple[int, int], tuple[float, float]] = {}
    parsed: dict[tuple[int, int], list[_Step]] = {}  # each form's content is parsed once
    work = 0
    done = 0
    read = 0  # bytes of instructions read so far

    def steps_of(content: pikepdf.Object) -> list[_Step]:
        nonlocal read
        steps, size = _steps(content)
        read += size
        if read > MAX_PARSE_TOTAL:
            raise TooComplexError
        return steps

    def form_steps(form: pikepdf.Stream) -> list[_Step]:
        key = form.objgen
        if key == (0, 0):
            return steps_of(form)
        if key not in parsed:
            parsed[key] = steps_of(form)
        return parsed[key]

    def run(
        steps: list[_Step],
        resources: pikepdf.Object | None,
        ctm: Matrix,
        depth: int,
        path: frozenset[tuple[int, int]],
    ) -> None:
        nonlocal work, done
        if depth:
            work += len(steps) + 1  # +1: entering a form is work even if it is empty
            if work > MAX_FORM_WORK:
                raise TooComplexError
        stack: list[Matrix] = []
        for op, value in steps:
            done += 1
            if cancel is not None and done % _CANCEL_EVERY == 0:
                cancel.check()
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                if stack:
                    ctm = stack.pop()
            elif op == "cm":
                if value is not None:
                    ctm = _multiply(value, ctm)  # type: ignore[arg-type]
            elif op == "Do" and isinstance(resources, pikepdf.Dictionary):
                xobjects = resources.get("/XObject")
                if not isinstance(xobjects, pikepdf.Dictionary):
                    continue
                xobject = xobjects.get(value)
                if not isinstance(xobject, pikepdf.Stream):
                    continue
                subtype = xobject.get("/Subtype")
                if subtype == Name.Image:
                    width = math.hypot(ctm[0], ctm[1]) / 72
                    height = math.hypot(ctm[2], ctm[3]) / 72
                    if width > 0 and height > 0 and xobject.objgen != (0, 0):
                        old = found.get(xobject.objgen, (0.0, 0.0))
                        found[xobject.objgen] = (max(old[0], width), max(old[1], height))
                elif subtype == Name.Form:
                    key = xobject.objgen
                    if depth >= _MAX_FORM_DEPTH or key in path:
                        # Nested too deep, or a form that draws itself. Skipping it would
                        # hide whatever it draws, so the measurement is given up instead.
                        raise TooComplexError
                    matrix = _numbers(xobject.get("/Matrix", _IDENTITY)) or _IDENTITY
                    run(
                        form_steps(xobject),
                        xobject.get("/Resources", resources),
                        _multiply(matrix, ctm),
                        depth + 1,
                        path | {key},
                    )

    for page in pdf.pages:
        if cancel is not None:
            cancel.check()
        unit = _user_unit(page)  # 1 unit = unit/72 inch; almost always 1
        identity = (unit, 0.0, 0.0, unit, 0.0, 0.0)
        run(steps_of(page), _page_resources(page), identity, 0, frozenset())
    return found
