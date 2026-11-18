"""Builds test PDFs in code. No real documents are ever committed to the repo."""

from __future__ import annotations

import hashlib
import io
import random
import zlib
from collections.abc import Sequence
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from pikepdf import Array, Dictionary, Name, Pdf
from PIL import Image

LETTER = (612, 792)


def page_text(index: int) -> str:
    """The text written on page ``index`` (0-based); tests search for it after operations."""
    return f"pdfzi test page {index + 1}"


def _escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def photo_image(width: int, height: int, seed: int = 0) -> Image.Image:
    """Deterministic photo-like RGB image: smooth random blobs plus fine grain.

    Detailed enough that lossy recompression clearly shrinks it, and fast to build.
    """
    rng = random.Random(seed)
    cell = 16
    small = Image.frombytes(
        "RGB",
        (max(2, width // cell), max(2, height // cell)),
        rng.randbytes(max(2, width // cell) * max(2, height // cell) * 3),
    )
    base = small.resize((width, height), Image.Resampling.BICUBIC)
    grain = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    return Image.blend(base, grain, 0.12)


def _photo_jpeg(width: int, height: int, seed: int, quality: int = 95) -> bytes:
    buf = io.BytesIO()
    photo_image(width, height, seed).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def make_pdf(
    path: Path,
    pages: int = 3,
    *,
    sizes: Sequence[tuple[float, float]] | None = None,
    rotations: dict[int, int] | None = None,
    password: str | None = None,
    bookmarks: bool = False,
    images_per_page: int = 0,
    image_size: tuple[int, int] = (640, 480),
    share_image: bool = False,
    text_prefix: str = "",
) -> Path:
    """Write a PDF to ``path``.

    * ``sizes``: per-page (width, height) in points, cycled; default US Letter.
    * ``rotations``: ``{page_index: /Rotate value}``.
    * ``password``: encrypt with AES-256 (user and owner password).
    * ``bookmarks``: one outline entry per page ("Chapter N").
    * ``images_per_page`` / ``share_image``: embed JPEGs; with ``share_image`` every
      page references one single image object.
    """
    pdf = Pdf.new()
    font = pdf.make_indirect(
        Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
    )
    shared = None
    if images_per_page and share_image:
        shared = _add_image(pdf, *image_size, seed=0)

    for i in range(pages):
        w, h = sizes[i % len(sizes)] if sizes else LETTER
        pdf.add_blank_page(page_size=(w, h))
        page = pdf.pages[i]
        xobjects = Dictionary()
        ops = [
            b"BT /F1 24 Tf 72 %d Td (%s) Tj ET"
            % (int(h) - 100, _escape(text_prefix + page_text(i)))
        ]
        for k in range(images_per_page):
            img = shared if shared is not None else _add_image(pdf, *image_size, seed=i * 10 + k)
            name = f"Im{k}"
            xobjects[f"/{name}"] = img
            iw, ih = min(w - 144, 300), min(h - 300, 225)
            ops.append(b"q %d 0 0 %d 72 %d cm /%s Do Q" % (iw, ih, 72 + k * 10, name.encode()))
        page.Resources = Dictionary(Font=Dictionary(F1=font), XObject=xobjects)
        page.Contents = pdf.make_stream(b"\n".join(ops))
        if rotations and i in rotations:
            page.Rotate = rotations[i]

    if bookmarks:
        with pdf.open_outline() as outline:
            for i in range(pages):
                outline.root.append(pikepdf.OutlineItem(f"Chapter {i + 1}", i))

    options: dict = {}
    if password is not None:
        options["encryption"] = pikepdf.Encryption(user=password, owner=password, R=6)
    pdf.save(path, **options)
    pdf.close()
    return path


def _add_image(pdf: Pdf, width: int, height: int, seed: int) -> pikepdf.Object:
    return pdf.make_stream(
        _photo_jpeg(width, height, seed),
        Type=Name.XObject,
        Subtype=Name.Image,
        Width=width,
        Height=height,
        ColorSpace=Name.DeviceRGB,
        BitsPerComponent=8,
        Filter=Name.DCTDecode,
    )


def make_corrupt_pdf(path: Path, kind: str = "garbage") -> Path:
    """Write a file that is not a readable PDF. ``kind``: ``garbage`` or ``header-only``."""
    if kind == "garbage":
        path.write_bytes(b"this is definitely not a pdf file\n" * 20)
    elif kind == "header-only":
        path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Broken")
    else:
        raise ValueError(kind)
    return path


def extract_text(path: Path, page_index: int, password: str | None = None) -> str:
    """Extract page text with PDFium (an independent reader from the one that wrote the file)."""
    doc = pdfium.PdfDocument(path, password=password)
    try:
        textpage = doc[page_index].get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            textpage.close()
    finally:
        doc.close()


def page_count(path: Path, password: str | None = None) -> int:
    with pikepdf.Pdf.open(path, password=password or "") as pdf:
        return len(pdf.pages)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Snapshot:
    """Fingerprint of a file (content hash + mtime) to prove an operation left it alone."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.digest = sha256(path)
        self.mtime_ns = path.stat().st_mtime_ns

    def assert_unchanged(self) -> None:
        assert self.path.exists(), f"{self.path} disappeared"
        assert sha256(self.path) == self.digest, f"{self.path} content changed"
        assert self.path.stat().st_mtime_ns == self.mtime_ns, f"{self.path} mtime changed"


# ---------------------------------------------------------------- image-specific PDFs

IMAGE_KINDS = (
    "dct-rgb", "dct-gray", "flate-rgb", "flate-gray", "flate-cmyk", "icc-rgb",
    "smask", "stencil", "one-bit", "indexed", "jbig2", "jpx",
)  # fmt: skip


def _compressed(pdf: Pdf, data: bytes, w: int, h: int, colorspace, bpc: int, extra: dict):
    stream = pdf.make_stream(data)
    stream.Type = Name.XObject
    stream.Subtype = Name.Image
    stream.Width, stream.Height = w, h
    if colorspace is not None:
        stream.ColorSpace = colorspace
    stream.BitsPerComponent = bpc
    for key, value in extra.items():
        stream[f"/{key}"] = value
    stream.write(zlib.compress(data, 6), filter=Name.FlateDecode)
    return stream


def make_image(pdf: Pdf, kind: str, w: int, h: int, seed: int = 1) -> pikepdf.Object:
    """One image XObject of the given ``kind`` (see ``IMAGE_KINDS``)."""
    from PIL import ImageCms

    photo = photo_image(w, h, seed)
    if kind == "dct-rgb":
        return _add_image(pdf, w, h, seed)
    if kind == "dct-gray":
        buf = io.BytesIO()
        photo.convert("L").save(buf, "JPEG", quality=95)
        return pdf.make_stream(
            buf.getvalue(), Type=Name.XObject, Subtype=Name.Image, Width=w, Height=h,
            ColorSpace=Name.DeviceGray, BitsPerComponent=8, Filter=Name.DCTDecode,
        )  # fmt: skip
    if kind == "flate-rgb":
        return _compressed(pdf, photo.tobytes(), w, h, Name.DeviceRGB, 8, {})
    if kind == "flate-gray":
        return _compressed(pdf, photo.convert("L").tobytes(), w, h, Name.DeviceGray, 8, {})
    if kind == "flate-cmyk":
        return _compressed(pdf, photo.convert("CMYK").tobytes(), w, h, Name.DeviceCMYK, 8, {})
    if kind == "icc-rgb":
        icc = pdf.make_stream(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        icc.N = 3
        icc.Alternate = Name.DeviceRGB
        cs = Array([Name.ICCBased, icc])
        return _compressed(pdf, photo.tobytes(), w, h, cs, 8, {})
    if kind == "smask":
        mask = _compressed(pdf, bytes([255, 128] * (w * h // 2)), w, h, Name.DeviceGray, 8, {})
        return _compressed(pdf, photo.tobytes(), w, h, Name.DeviceRGB, 8, {"SMask": mask})
    if kind == "stencil":
        bits = bytes([0b10101010] * (((w + 7) // 8) * h))
        return _compressed(pdf, bits, w, h, None, 1, {"ImageMask": True})
    if kind == "one-bit":
        bits = bytes([0b11001100] * (((w + 7) // 8) * h))
        return _compressed(pdf, bits, w, h, Name.DeviceGray, 1, {})
    if kind == "indexed":
        palette = bytes(range(256)) * 3
        cs = Array([Name.Indexed, Name.DeviceRGB, 255, pikepdf.String(palette)])
        return _compressed(pdf, bytes(i % 256 for i in range(w * h)), w, h, cs, 8, {})
    if kind in {"jbig2", "jpx"}:
        flt = Name.JBIG2Decode if kind == "jbig2" else Name.JPXDecode
        return pdf.make_stream(
            b"not really an image", Type=Name.XObject, Subtype=Name.Image, Width=w, Height=h,
            ColorSpace=Name.DeviceGray, BitsPerComponent=8, Filter=flt,
        )  # fmt: skip
    raise ValueError(kind)


def make_image_pdf(
    path: Path, kinds: Sequence[str], size: tuple[int, int] = (800, 600), *, text: bool = True
) -> Path:
    """One page per entry of ``kinds``, each showing one image of that kind."""
    pdf = Pdf.new()
    font = pdf.make_indirect(
        Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
    )
    for i, kind in enumerate(kinds):
        pdf.add_blank_page(page_size=LETTER)
        page = pdf.pages[i]
        ops = [b"BT /F1 24 Tf 72 740 Td (%s) Tj ET" % _escape(page_text(i))] if text else []
        ops.append(b"q 400 0 0 300 72 300 cm /Im0 Do Q")
        page.Resources = Dictionary(
            Font=Dictionary(F1=font),
            XObject=Dictionary(Im0=make_image(pdf, kind, *size, seed=i + 1)),
        )
        page.Contents = pdf.make_stream(b"\n".join(ops))
    pdf.save(path)
    pdf.close()
    return path


def render_page(path: Path, index: int = 0, scale: float = 0.5, password: str | None = None):
    """Render a page with PDFium to a PIL image."""
    doc = pdfium.PdfDocument(path, password=password)
    try:
        return doc[index].render(scale=scale).to_pil().convert("RGB")
    finally:
        doc.close()


def mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    """Average per-channel difference (0-255) between two same-size images."""
    from PIL import ImageChops, ImageStat

    assert a.size == b.size, (a.size, b.size)
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
