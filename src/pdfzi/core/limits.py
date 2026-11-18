"""How large a stream becomes once unpacked, found out without unpacking it all at once.

A few KB of Flate data can unpack to gigabytes, and filters can be chained so that each
one multiplies what the one before it produced. Before pdfzi (or qpdf) holds a stream's
data in memory, these helpers unpack it here, one filter after another, counting as they
go and giving up as soon as a limit is passed. The data is never handed to qpdf to unpack
in one go.
"""

from __future__ import annotations

import base64
import binascii
import zlib
from collections.abc import Callable, Iterable, Iterator

import pikepdf
from pikepdf import Name

from pdfzi.core.errors import UnsafeInputError

MB = 1024 * 1024

# XMP metadata: real files carry a few KB, rarely a few MB. qpdf writes it back unpacked,
# so a bomb here would also make every result file enormous.
MAX_METADATA = 16 * MB
# One content stream (a page's drawing instructions, or a form's). Heavy vector pages
# reach a few MB; qpdf needs a few times the unpacked size to work through one.
MAX_CONTENT = 64 * MB

_CHUNK = MB
_ABBREVIATIONS = {
    "/Fl": "/FlateDecode",
    "/AHx": "/ASCIIHexDecode",
    "/A85": "/ASCII85Decode",
    "/LZW": "/LZWDecode",
    "/RL": "/RunLengthDecode",
}
_PDF_WHITESPACE = b"\x00\t\n\x0c\r "


class _UndecodableError(Exception):
    """The data is broken in a way qpdf does not unpack either."""


Parms = pikepdf.Dictionary | None
# One filter: (data, limit, its /DecodeParms, keep the output?) -> (size, output), or None
# as soon as the output passes the limit. The output is b"" when it is not kept.
_Stage = Callable[[bytes, int, Parms, bool], tuple[int, bytes] | None]


def _filters(stream: pikepdf.Object) -> list[str] | None:
    """The stream's filter names, abbreviations spelled out; None if malformed."""
    value = stream.get("/Filter")
    if value is None:
        return []
    items = [value] if isinstance(value, Name) else value
    if not isinstance(items, (list, pikepdf.Array)):
        return None
    names = []
    for item in items:
        if not isinstance(item, Name):
            return None
        try:
            text = str(item)
        except UnicodeDecodeError:
            return None
        names.append(_ABBREVIATIONS.get(text, text))
    return names


def _decode_parms(stream: pikepdf.Object, count: int) -> list[Parms]:
    """Each filter's /DecodeParms (None where it has none)."""
    value = stream.get("/DecodeParms")
    items = list(value) if isinstance(value, pikepdf.Array) else [value] * count
    items += [None] * (count - len(items))
    return [p if isinstance(p, pikepdf.Dictionary) else None for p in items[:count]]


def _int_parm(parms: Parms, key: str, default: int) -> int:
    if parms is None:
        return default
    try:
        return int(parms.get(key, default))
    except (TypeError, ValueError, pikepdf.PdfError):
        return default


def _within(out: bytes, limit: int, keep: bool) -> tuple[int, bytes] | None:
    if len(out) > limit:
        return None
    return len(out), out if keep else b""


def _inflate(data: bytes, limit: int, _parms: Parms, keep: bool) -> tuple[int, bytes] | None:
    inflater = zlib.decompressobj()
    kept: list[bytes] = []
    total = 0
    pending = data
    try:
        while True:
            chunk = inflater.decompress(pending, _CHUNK)
            total += len(chunk)
            if total > limit:
                return None
            if keep:
                kept.append(chunk)
            pending = inflater.unconsumed_tail
            if not pending and len(chunk) < _CHUNK:
                break
        rest = inflater.flush()
        total += len(rest)
        if keep:
            kept.append(rest)
    except zlib.error:
        pass  # damaged data: qpdf stops at the same place, so this is all there is
    if total > limit:
        return None
    return total, b"".join(kept)


_LZW_CLEAR, _LZW_END = 256, 257
_LZW_ROOTS = [bytes([i]) for i in range(256)] + [b"", b""]  # codes 256 and 257 are signals


def _lzw(data: bytes, limit: int, parms: Parms, keep: bool) -> tuple[int, bytes] | None:
    early = _int_parm(parms, "/EarlyChange", 1)
    table = list(_LZW_ROOTS)
    width, prev, total = 9, b"", 0
    kept: list[bytes] = []
    buffer = bits = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        bits += 8
        while bits >= width:
            bits -= width
            code = buffer >> bits
            buffer &= (1 << bits) - 1
            if code == _LZW_CLEAR:
                del table[258:]
                width, prev = 9, b""
                continue
            if code == _LZW_END:
                return total, b"".join(kept)
            if code < len(table):
                entry = table[code]
            elif code == len(table) and prev:
                entry = prev + prev[:1]
            else:
                raise _UndecodableError
            if prev and len(table) < 4096:
                table.append(prev + entry[:1])
            prev = entry
            if width < 12 and len(table) + early >= 1 << width:
                width += 1
            total += len(entry)
            if total > limit:
                return None
            if keep:
                kept.append(entry)
    return total, b"".join(kept)


def _ascii_hex(data: bytes, limit: int, _parms: Parms, keep: bool) -> tuple[int, bytes] | None:
    end = data.find(b">")
    digits = (data if end < 0 else data[:end]).translate(None, _PDF_WHITESPACE)
    if len(digits) % 2:
        digits += b"0"
    try:
        return _within(binascii.unhexlify(digits), limit, keep)
    except (binascii.Error, ValueError):
        raise _UndecodableError from None


def _ascii85(data: bytes, limit: int, _parms: Parms, keep: bool) -> tuple[int, bytes] | None:
    end = data.find(b"~>")
    chars = (data if end < 0 else data[:end]).translate(None, _PDF_WHITESPACE)
    zeros = chars.count(b"z")  # one character for four zero bytes
    if 4 * zeros + (len(chars) - zeros) * 4 // 5 > limit:
        return None
    try:
        return _within(base64.a85decode(chars), limit, keep)
    except ValueError:
        raise _UndecodableError from None


def _run_length(data: bytes, limit: int, _parms: Parms, keep: bool) -> tuple[int, bytes] | None:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        length = data[i]
        if length == 128:  # end of data
            break
        if length < 128:  # the next length + 1 bytes as they are
            out += data[i + 1 : i + 2 + length]
            i += 2 + length
        else:  # the next byte, 257 - length times
            out += data[i + 1 : i + 2] * (257 - length)
            i += 2
        if len(out) > limit:
            return None
    return len(out), bytes(out) if keep else b""


_STAGES: dict[str, _Stage] = {
    "/FlateDecode": _inflate,
    "/LZWDecode": _lzw,
    "/ASCIIHexDecode": _ascii_hex,
    "/ASCII85Decode": _ascii85,
    "/RunLengthDecode": _run_length,
}


def unpacked_size(stream: pikepdf.Object, limit: int) -> int | None:
    """The size of ``stream``'s data once unpacked, or None if it is over ``limit``.

    Each filter's output in a chain is held to ``limit`` too. Data that qpdf cannot
    unpack (an unknown filter, a broken one) counts as its stored size: qpdf leaves it as
    it is, so it cannot grow. None is also given when the size cannot be found out safely.
    """
    try:
        filters = _filters(stream)
        parms = _decode_parms(stream, len(filters)) if filters else []
        raw = bytes(stream.read_raw_bytes())
    except (pikepdf.PdfError, ValueError, TypeError, AttributeError):
        return None
    if filters is None:
        return None
    stored = len(raw) if len(raw) <= limit else None
    if not filters or any(f not in _STAGES for f in filters):
        # Stored as it is, or with a filter qpdf will not unpack: the stored size is it.
        return stored
    data = raw
    for i, (name, stage_parms) in enumerate(zip(filters, parms, strict=True)):
        last = i == len(filters) - 1
        if not last and _int_parm(stage_parms, "/Predictor", 1) > 1:
            # The next filter reads this output only after its predictor is undone, which
            # is not done here, so its size cannot be found out safely.
            return None
        try:
            result = _STAGES[name](data, limit, stage_parms, not last)
        except _UndecodableError:
            return stored
        if result is None:
            return None
        size, data = result
        if last:
            return size
    return None  # not reached: there is at least one filter


def content_streams(obj: pikepdf.Object) -> list[pikepdf.Object]:
    """The streams holding drawing instructions: a page's /Contents, or a form itself."""
    if isinstance(obj, pikepdf.Stream):
        return [obj]
    try:
        contents = obj.get("/Contents")
    except (pikepdf.PdfError, ValueError, TypeError, AttributeError):
        return []
    if isinstance(contents, pikepdf.Stream):
        return [contents]
    if isinstance(contents, pikepdf.Array):
        return [c for c in contents if isinstance(c, pikepdf.Stream)]
    return []


def unpacked_content_size(obj: pikepdf.Object, limit: int) -> int | None:
    """Total unpacked size of a page's or form's drawing instructions; None if over."""
    total = 0
    for stream in content_streams(obj):
        size = unpacked_size(stream, limit - total)
        if size is None:
            return None
        total += size
    return total


def _streams(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Object]:
    """Every stream object in the file (stops quietly where the file is too damaged)."""
    try:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                yield obj
    except pikepdf.PdfError:
        return


def _forms(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Object]:
    for obj in _streams(pdf):
        try:
            if obj.get("/Subtype") == Name.Form:
                yield obj
        except (pikepdf.PdfError, ValueError, TypeError):
            continue


def oversized_content(pdf: pikepdf.Pdf, pages: Iterable[int] | None = None) -> str | None:
    """Describe the first page or form whose instructions unpack past ``MAX_CONTENT``.

    ``pages`` (0-based) limits the pages looked at; forms are always all checked.
    Returns None when everything is within the limit.
    """
    indices = range(len(pdf.pages)) if pages is None else pages
    for i in indices:
        if unpacked_content_size(pdf.pages[i].obj, MAX_CONTENT) is None:
            return f"the drawing instructions of page {i + 1}"
    for form in _forms(pdf):
        if unpacked_size(form, MAX_CONTENT) is None:
            return "the drawing instructions of a form (a reusable drawing)"
    return None


def refuse_oversized_metadata(pdf: pikepdf.Pdf, name: str) -> None:
    """Raise :class:`UnsafeInputError` if any XMP metadata unpacks past ``MAX_METADATA``."""
    for obj in _streams(pdf):
        try:
            is_metadata = obj.get("/Type") == Name.Metadata
        except (pikepdf.PdfError, ValueError, TypeError):
            continue
        if is_metadata and unpacked_size(obj, MAX_METADATA) is None:
            raise UnsafeInputError(name, "its XMP metadata", MAX_METADATA)


def refuse_oversized_content(
    pdf: pikepdf.Pdf, name: str, pages: Iterable[int] | None = None
) -> None:
    """Raise :class:`UnsafeInputError` if drawing instructions unpack past ``MAX_CONTENT``."""
    what = oversized_content(pdf, pages)
    if what is not None:
        raise UnsafeInputError(name, what, MAX_CONTENT)
