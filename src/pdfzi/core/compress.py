"""``compress``: make PDFs smaller without turning pages into pictures.

Text and vector content are never rasterised. Four levels:

* ``lossless``: images are untouched; the file is rewritten with better stream compression,
  object streams, and unused page resources removed.
* ``low`` / ``medium`` / ``high``: additionally, images that are sharper than needed are
  scaled down (to about 220 / 180 / 140 dpi *as they are drawn on the page*, so a small
  picture with many pixels shrinks and a large one is not squeezed more than its size calls
  for) and re-encoded as (progressive) JPEG.
* ``target``: tries lossless first, then the strongest allowed settings, then searches the
  strength scale (:func:`strength_profile`, 0 = gentlest, 100 = strongest) by halving, each
  time starting again from the original, for the gentlest settings whose result is no bigger
  than the target. A result well under the target is then brought closer to it by giving
  some images back their gentler (or original) version. If even the strongest settings are
  too big, the smallest result is returned and ``target_reached`` is False.

An image is only replaced when that makes it at least 20 % smaller, and a JPEG that would
not be scaled down is left alone when it already is of the chosen quality or lower:
recompressing it would only add blockiness for a few bytes.

A result that is not at least 1 % smaller than the input is not written either (unless a
``target`` was asked for and reached): the input is kept as is and the result says so
(``kept_original``).
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Name
from PIL import Image

from pdfzi.core.errors import InvalidOptionError, OutputWriteError
from pdfzi.core.fidelity import claims_pdfa, fidelity_notes
from pdfzi.core.fmt import plural
from pdfzi.core.limits import oversized_content
from pdfzi.core.output import DECRYPTED_NOTE
from pdfzi.core.paths import output_path_for
from pdfzi.core.pdfimages import TooComplexError, image_extents, iter_images
from pdfzi.core.pdfio import open_pdf, save_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult
from pdfzi.core.safeio import safe_output
from pdfzi.core.verify import verify_output

LEVELS = ("lossless", "low", "medium", "high")


@dataclass(frozen=True)
class Profile:
    """How hard to squeeze images.

    ``dpi``: images drawn sharper than this (pixels per inch, measured where the image is
    drawn on the page) are scaled down to it. ``max_side`` (pixels) does the same for the
    rare image whose size on the page cannot be worked out. ``quality`` is JPEG 1-95.
    """

    name: str
    dpi: int | None = None
    quality: int | None = None
    max_side: int | None = None

    @property
    def lossy(self) -> bool:
        return self.quality is not None

    def describe(self) -> str:
        if not self.lossy:
            return "lossless"
        return f"images above {self.dpi} dpi scaled down, JPEG quality {self.quality}"


def _max_side(dpi: int) -> int:
    """The pixel limit that matches ``dpi`` on the long side of an A4 page (11.7 inch)."""
    return round(dpi * 11.7 / 50) * 50


# The strength scale used by --target: (strength, dpi, JPEG quality), straight lines in
# between. The resolution comes down first and the quality mostly near the end, because
# fewer pixels make a picture softer, while a low quality makes it break into blocks, and
# every JPEG block covers more of the page as the resolution drops. The last point is the
# floor: below about 70 dpi, Thai text in scans loses its vowels and tone marks, and below
# JPEG quality 45 flat areas break into blocks (checked by eye at 100 % zoom). A target
# that needs more than that is reported as not reached instead.
MAX_STRENGTH = 100
STRENGTH_POINTS = (
    (0, 300, 90),
    (20, 220, 85),  # = low
    (40, 180, 80),  # = medium
    (60, 140, 70),  # = high
    (85, 85, 62),
    (MAX_STRENGTH, 70, 45),
)


def strength_profile(strength: int) -> Profile:
    """Settings for ``strength`` (0 = gentlest, ``MAX_STRENGTH`` = the floor)."""
    if not 0 <= strength <= MAX_STRENGTH:
        raise ValueError(f"strength must be 0-{MAX_STRENGTH}")
    for (s0, d0, q0), (s1, d1, q1) in pairwise(STRENGTH_POINTS):
        if s0 <= strength <= s1:
            t = (strength - s0) / (s1 - s0)
            dpi, quality = round(d0 + (d1 - d0) * t), round(q0 + (q1 - q0) * t)
            return Profile(f"strength {strength}", dpi, quality, _max_side(dpi))
    raise AssertionError("unreachable")  # pragma: no cover


# The levels are fixed points on the same scale (220 dpi is about 2550 px along an A4 page,
# 140 dpi about 1650 px).
PROFILES = {
    "lossless": Profile("lossless"),
    "low": Profile("low", dpi=220, quality=85, max_side=_max_side(220)),
    "medium": Profile("medium", dpi=180, quality=80, max_side=_max_side(180)),
    "high": Profile("high", dpi=140, quality=70, max_side=_max_side(140)),
}
# --target: lossless, the floor, then halving 0..MAX_STRENGTH (101 values: 7 halvings).
MAX_ATTEMPTS = 2 + (MAX_STRENGTH + 1).bit_length()
TARGET_TOLERANCE = 0.03  # a result this close under the target ends the search
_SCALE_ONLY_BELOW = 0.85  # resizing for a smaller saving than 15 % only softens the picture
MIN_IMAGE_SAVING = 0.20  # an image that shrinks less than this keeps its original
_MAX_PIXELS = 250_000_000  # refuse to decode absurdly large images
_CACHE_BYTES = 512 * 1024**2  # decoded pixels kept between --target attempts
MIN_SAVING = 0.01  # a result that saves less than this is not worth a new file

_SKIP_SOFT_MASK = "has a transparency mask"
_SKIP_STENCIL = "is a 1-bit or stencil image"
_SKIP_FILTER = "uses a filter pdfzi does not touch (JBIG2, CCITT, JPEG2000, ...)"
_SKIP_COLORSPACE = "uses a special colour space"
_SKIP_DEPTH = "is not 8 bits per channel"
_SKIP_DECODE = "has a custom /Decode array"
_SKIP_CMYK_JPEG = "is a CMYK JPEG"
_SKIP_UNREADABLE = "could not be decoded"
_SKIP_NOT_SMALLER = f"would get less than {MIN_IMAGE_SAVING:.0%} smaller"
_SKIP_LOW_QUALITY = "is already a JPEG of this quality or lower"
_SKIP_NOT_NEEDED = "was not needed to reach the target"
_SKIP_TOO_COMPLEX = "too intricate to measure"  # reported by _INTRICATE_NOTE instead
_INTRICATE_NOTE = (
    "Images were left as they were: the pages draw them in a way too intricate or damaged "
    "to measure, so pdfzi cannot tell how large they appear. "
    "The rest of the file was still compressed."
)

Extents = dict[tuple[int, int], tuple[float, float]]


@dataclass
class CompressResult(OperationResult):
    level: str = "lossless"
    grayscale: bool = False
    kept_original: bool = False
    target_bytes: int | None = None
    target_reached: bool | None = None
    attempts: int = 1
    settings: str = ""
    images_found: int = 0
    images_recompressed: int = 0
    images_skipped: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(
            level=self.level,
            grayscale=self.grayscale,
            kept_original=self.kept_original,
            target_bytes=self.target_bytes,
            target_reached=self.target_reached,
            attempts=self.attempts,
            settings=self.settings,
            images_found=self.images_found,
            images_recompressed=self.images_recompressed,
            images_skipped=dict(self.images_skipped),
        )
        return data


# ---------------------------------------------------------------- image handling


@dataclass
class _ImageStats:
    found: int = 0
    recompressed: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


@dataclass
class _Plan:
    """Decoded facts about an image that pdfzi is able to recompress."""

    mode: str  # PIL mode of the pixel data: "L", "RGB" or "CMYK"
    is_jpeg: bool
    colorspace: Any  # the original /ColorSpace object, reused if the colour model is unchanged


@dataclass(frozen=True)
class _Encoded:
    """A finished replacement for one image: the JPEG and what to write beside it."""

    jpeg: bytes
    width: int
    height: int
    mode: str


# What became of one image in an attempt: its replacement, or why it kept its original.
_Outcome = _Encoded | str


def _filters(obj: pikepdf.Object) -> list[str]:
    f = obj.get("/Filter")
    if f is None:
        return []
    if isinstance(f, pikepdf.Array):
        return [str(x) for x in f]
    return [str(f)]


def _colour_model(obj: pikepdf.Object) -> str | None:
    """``"L"``, ``"RGB"`` or ``"CMYK"`` for plain device / ICC based spaces, else None."""
    cs = obj.get("/ColorSpace")
    if cs is None:
        return None
    if isinstance(cs, pikepdf.Name):
        return {"/DeviceGray": "L", "/DeviceRGB": "RGB", "/DeviceCMYK": "CMYK"}.get(str(cs))
    if isinstance(cs, pikepdf.Array) and len(cs) == 2 and cs[0] == Name.ICCBased:
        n = cs[1].get("/N")
        return {1: "L", 3: "RGB", 4: "CMYK"}.get(int(n)) if n is not None else None
    return None


def _inspect(obj: pikepdf.Object) -> _Plan | str:
    """Return a plan if the image may be recompressed, else the reason it is skipped."""
    if obj.get("/ImageMask", False):
        return _SKIP_STENCIL
    if "/SMask" in obj or "/Mask" in obj:
        return _SKIP_SOFT_MASK
    filters = _filters(obj)
    if filters not in ([], ["/FlateDecode"], ["/DCTDecode"]):
        return _SKIP_FILTER
    bpc = obj.get("/BitsPerComponent")
    if bpc is None and filters == ["/DCTDecode"]:
        bpc = 8
    if bpc is None or int(bpc) != 8:
        return _SKIP_STENCIL if bpc is not None and int(bpc) == 1 else _SKIP_DEPTH
    if "/Decode" in obj:
        return _SKIP_DECODE
    mode = _colour_model(obj)
    if mode is None:
        return _SKIP_COLORSPACE
    is_jpeg = filters == ["/DCTDecode"]
    if is_jpeg and mode == "CMYK":
        return _SKIP_CMYK_JPEG
    return _Plan(mode=mode, is_jpeg=is_jpeg, colorspace=obj.get("/ColorSpace"))


def _decode(obj: pikepdf.Object, plan: _Plan) -> Image.Image:
    width, height = int(obj.Width), int(obj.Height)
    if width * height > _MAX_PIXELS:
        raise ValueError("image too large")
    comps = {"L": 1, "RGB": 3, "CMYK": 4}[plan.mode]
    if plan.is_jpeg:
        img = Image.open(io.BytesIO(obj.read_raw_bytes()))
        img.load()
        if img.size != (width, height) or img.mode != plan.mode:
            raise ValueError("JPEG does not match its PDF description")
        return img
    expected = width * height * comps
    if _filters(obj) == ["/FlateDecode"]:
        # A few kilobytes of Flate data can inflate to gigabytes ("zip bomb"), and qpdf
        # would inflate all of it before the size check below. So first inflate with a
        # cap: the pixels plus one predictor byte per row is the most a real image needs.
        _check_inflated_size(obj.read_raw_bytes(), limit=expected + height + 64)
    data = obj.read_bytes()
    if len(data) != expected:
        raise ValueError("unexpected amount of pixel data")
    return Image.frombytes(plan.mode, (width, height), data)


def _check_inflated_size(raw: bytes, *, limit: int) -> None:
    """Raise ValueError if ``raw`` (zlib data) inflates to more than ``limit`` bytes.

    Never inflates more than ``limit + 1`` bytes. Data zlib cannot read is left to qpdf,
    which is more forgiving; it stops at the same broken spot, so it cannot inflate more.
    """
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(raw, limit + 1)
    except zlib.error:
        return
    if len(out) > limit:
        raise ValueError("image data is much larger than the image size says")


# The example luminance table of the JPEG standard, which libjpeg (and so most software)
# scales by the quality setting. Comparing a file's table with it tells its quality.
_STANDARD_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
)  # fmt: skip


def _standard_table_sum(quality: int) -> int:
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    return sum(min(255, max(1, (v * scale + 50) // 100)) for v in _STANDARD_LUMA)


_QUALITY_SUMS = {q: _standard_table_sum(q) for q in range(1, 101)}


def _jpeg_quality(raw: bytes) -> int | None:
    """Roughly the quality setting a JPEG was saved with (1-100), None if unknown.

    Exact for files written by libjpeg-based software (Pillow, most scanners and editors);
    for others it is the standard quality whose table is about as coarse.
    """
    with Image.open(io.BytesIO(raw)) as img:
        tables = getattr(img, "quantization", None)
    if not tables or 0 not in tables:
        return None
    total = sum(tables[0])
    return min(_QUALITY_SUMS, key=lambda q: (abs(_QUALITY_SUMS[q] - total), q))


def _scaled_size(
    width: int, height: int, extent: tuple[float, float] | None, profile: Profile
) -> tuple[int, int]:
    """Pixel size an image should have: the size that gives ``profile.dpi`` where it is drawn.

    An image only gets smaller, and not for a tiny gain (it would just look softer).
    Without a known drawn size the pixel limit ``max_side`` is used instead.
    """
    if extent is not None and profile.dpi:
        need_w, need_h = extent[0] * profile.dpi, extent[1] * profile.dpi
        scale = max(need_w / width, need_h / height)  # the same factor on both sides
        if scale > _SCALE_ONLY_BELOW:
            return width, height
    elif profile.max_side and max(width, height) > profile.max_side:
        scale = profile.max_side / max(width, height)
    else:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))


def _encode_image(
    obj: pikepdf.Object,
    raw: bytes,
    plan: _Plan,
    profile: Profile,
    grayscale: bool,
    extent: tuple[float, float] | None,
    shared: _Shared,
) -> _Outcome:
    """A smaller JPEG for ``obj``, or the reason it keeps its original. Changes nothing.

    ``raw`` is the image's stream data as stored. ``extent`` is the size in inches at which
    the image is drawn (from :func:`~pdfzi.core.pdfimages.image_extents`).
    """
    assert profile.quality is not None
    raw_size = len(raw)
    width, height = int(obj.Width), int(obj.Height)
    new_size = _scaled_size(width, height, extent, profile)
    to_grey = grayscale and plan.mode != "L"
    if plan.is_jpeg and new_size == (width, height) and not to_grey:
        # Same pixels, only a lower quality: worth it only if the JPEG is finer than that.
        quality = _jpeg_quality(raw)
        if quality is not None and quality <= profile.quality:
            return _SKIP_LOW_QUALITY

    img = shared.decoded(obj, plan)
    if new_size != img.size:
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    mode = plan.mode
    if to_grey:
        img = img.convert("L")
        mode = "L"

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=profile.quality, optimize=True, progressive=True)
    jpeg = buf.getvalue()
    if len(jpeg) > raw_size * (1 - MIN_IMAGE_SAVING):
        return _SKIP_NOT_SMALLER
    return _Encoded(jpeg, img.width, img.height, mode)


def _write_image(obj: pikepdf.Object, plan: _Plan, new: _Encoded) -> None:
    """Replace the image data of ``obj`` with ``new``."""
    colorspace = plan.colorspace if new.mode == plan.mode else Name.DeviceGray
    obj.write(new.jpeg, filter=Name.DCTDecode)  # also drops any old /DecodeParms
    obj.Width, obj.Height = new.width, new.height
    obj.ColorSpace = colorspace
    obj.BitsPerComponent = 8
    if new.mode == "CMYK":
        # Pillow (like Photoshop) stores CMYK JPEG values inverted; /Decode undoes that.
        obj.Decode = pikepdf.Array([1, 0, 1, 0, 1, 0, 1, 0])


class _Shared:
    """What every attempt of one run can reuse (every attempt opens the same file, so
    object numbers agree): how large images are drawn, and their decoded pixels."""

    def __init__(self, cancel: CancelToken | None, *, keep_pixels: bool) -> None:
        self.cancel = cancel
        self.done = False
        self.extents: Extents | None = None  # None when the drawings cannot be measured
        self._contents_ok: bool | None = None
        self._pixels: dict[tuple[int, int], Image.Image] = {}
        # with a single attempt nothing is decoded twice: holding pixels would only cost memory
        self._pixel_room = _CACHE_BYTES if keep_pixels else 0

    def contents_ok(self, pdf: pikepdf.Pdf) -> bool:
        """Whether every page's and form's instructions are of a size qpdf can work through.

        If not, unused resources are left in place: removing them makes qpdf read all the
        instructions, which a crafted file can make enormous (see :mod:`pdfzi.core.limits`).
        """
        if self._contents_ok is None:
            self._contents_ok = oversized_content(pdf) is None
        return self._contents_ok

    def measure(self, pdf: pikepdf.Pdf) -> Extents | None:
        if not self.done:
            try:
                self.extents = image_extents(pdf, self.cancel)
            except TooComplexError:
                self.extents = None
            self.done = True
        return self.extents

    def decoded(self, obj: pikepdf.Object, plan: _Plan) -> Image.Image:
        """The pixels of ``obj``, decoded once per run while they fit in the cache."""
        key = obj.objgen
        img = self._pixels.get(key)
        if img is None:
            img = _decode(obj, plan)
            size = img.width * img.height * len(img.getbands())
            if key != (0, 0) and size <= self._pixel_room:
                self._pixels[key] = img
                self._pixel_room -= size
        return img


def _recompress_all(
    pdf: pikepdf.Pdf,
    profile: Profile,
    grayscale: bool,
    stats: _ImageStats,
    on_image: Any,
    shared: _Shared,
    chosen: dict[tuple[int, int], _Outcome] | None,
) -> tuple[dict[tuple[int, int], _Outcome], dict[tuple[int, int], int]]:
    """Recompress the images of ``pdf``. Returns each image's outcome and original size.

    ``chosen`` gives some images a ready outcome instead of encoding them with ``profile``.
    """
    images = list(iter_images(pdf))  # each distinct image once, however many pages use it
    stats.found = len(images)
    outcomes: dict[tuple[int, int], _Outcome] = {}
    raw_sizes: dict[tuple[int, int], int] = {}
    extents = shared.measure(pdf)  # how big each is drawn on the page(s)
    if extents is None:
        # Not even the max_side fallback: with sizes unknown, any image could end up too
        # small for where it is drawn. Images are left alone; the rest is still compressed.
        if images:
            stats.skipped[_SKIP_TOO_COMPLEX] = len(images)
        return outcomes, raw_sizes
    for i, obj in enumerate(images):
        on_image(i, len(images))
        key = obj.objgen
        try:
            # Inside the try: a malformed image dictionary (a /Width that is not a number,
            # an ICC profile that is not a stream ...) must only skip that image.
            plan = _inspect(obj)
            if isinstance(plan, str):
                stats.skipped[plan] += 1
                continue
            raw = obj.read_raw_bytes()
            raw_size = len(raw)
            if chosen is not None and key in chosen:
                outcome = chosen[key]
            else:
                outcome = _encode_image(
                    obj, raw, plan, profile, grayscale, extents.get(key), shared
                )
            if isinstance(outcome, _Encoded):
                _write_image(obj, plan, outcome)
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            MemoryError,
            zlib.error,
            pikepdf.PdfError,
            Image.DecompressionBombError,
        ):
            stats.skipped[_SKIP_UNREADABLE] += 1
            continue
        outcomes[key], raw_sizes[key] = outcome, raw_size
        if isinstance(outcome, _Encoded):
            stats.recompressed += 1
        else:
            stats.skipped[outcome] += 1
    return outcomes, raw_sizes


# ---------------------------------------------------------------- one attempt


@dataclass
class _Attempt:
    path: Path
    size: int
    profile: Profile
    stats: _ImageStats
    pages: int
    was_encrypted: bool
    fidelity: list[str]
    outcomes: dict[tuple[int, int], _Outcome] = field(default_factory=dict)
    raw_sizes: dict[tuple[int, int], int] = field(default_factory=dict)
    note: str | None = None  # how the images were mixed to come close to a target


def _attempt(
    source: Path,
    password: str | None,
    profile: Profile,
    grayscale: bool,
    out_path: Path,
    on_image: Any,
    shared: _Shared,
    chosen: dict[tuple[int, int], _Outcome] | None = None,
) -> _Attempt:
    """Compress ``source`` with ``profile`` into ``out_path`` (a scratch file)."""
    stats = _ImageStats()
    outcomes: dict[tuple[int, int], _Outcome] = {}
    raw_sizes: dict[tuple[int, int], int] = {}
    with open_pdf(source, password) as pdf:
        pages = len(pdf.pages)
        was_encrypted = pdf.is_encrypted
        fidelity = fidelity_notes(pdf)
        if profile.lossy:
            outcomes, raw_sizes = _recompress_all(
                pdf, profile, grayscale, stats, on_image, shared, chosen
            )
        else:
            stats.found = sum(1 for _ in iter_images(pdf))
        if shared.contents_ok(pdf):
            pdf.remove_unreferenced_resources()
        # PDF/A-1 forbids object streams (and the cross-reference streams that come with
        # them), and every PDF/A wants its XMP packet left as it is: for those files only
        # the stream compression is improved.
        archival = claims_pdfa(pdf)
        pikepdf.settings.set_flate_compression_level(9)  # process-wide; restored below
        try:
            save_pdf(
                pdf,
                out_path,
                compress_streams=True,
                recompress_flate=True,
                stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                object_stream_mode=(
                    pikepdf.ObjectStreamMode.disable
                    if archival
                    else pikepdf.ObjectStreamMode.generate
                ),
            )
        finally:
            pikepdf.settings.set_flate_compression_level(-1)
    return _Attempt(
        out_path, out_path.stat().st_size, profile, stats, pages, was_encrypted, fidelity,
        outcomes, raw_sizes,
    )  # fmt: skip


def _gentler_mix(
    gentle: _Attempt, strong: _Attempt, budget: int
) -> tuple[dict[tuple[int, int], _Outcome], int] | None:
    """Give images of ``strong`` their version from ``gentle`` while ``budget`` bytes last.

    ``gentle`` is a gentler attempt that was too big (lossless: the originals). The images
    whose gentler version costs the fewest extra bytes go first, so as many as possible
    keep it. Returns the outcomes to use and how many images got a gentler JPEG (rather
    than their original back), or None if no image fits the budget.
    """

    def stored(key: tuple[int, int], outcome: _Outcome) -> int:
        return len(outcome.jpeg) if isinstance(outcome, _Encoded) else strong.raw_sizes[key]

    swaps = []
    for key, mine in strong.outcomes.items():
        if not isinstance(mine, _Encoded):
            continue  # already the original
        theirs = gentle.outcomes.get(key, _SKIP_NOT_NEEDED) if gentle.profile.lossy else None
        theirs = _SKIP_NOT_NEEDED if theirs is None else theirs
        swaps.append((stored(key, theirs) - stored(key, mine), key, theirs))
    swaps.sort(key=lambda s: (s[0], s[1]))
    chosen = dict(strong.outcomes)
    used = changed = gentler = 0
    for extra, key, theirs in swaps:
        if used + extra > budget:
            break
        chosen[key] = theirs
        used += extra
        changed += 1
        gentler += isinstance(theirs, _Encoded)
    return (chosen, gentler) if changed else None


def _image_notes(profile: Profile, stats: _ImageStats) -> list[str]:
    """Sentences about what happened to images (only meaningful for lossy attempts)."""
    if not profile.lossy or not stats.found:
        return []
    lines = [f"Images: {stats.recompressed} of {stats.found} recompressed ({profile.describe()})."]
    lines += [
        f"{plural(count, 'image')} left as {'it was' if count == 1 else 'they were'}: {why}."
        for why, count in stats.skipped.items()
        if why != _SKIP_TOO_COMPLEX
    ]
    return lines


# ---------------------------------------------------------------- public API


def compress_pdf(
    source: Path,
    dest: Path | None = None,
    *,
    level: str = "lossless",
    target: int | None = None,
    grayscale: bool = False,
    force: bool = False,
    password: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> CompressResult:
    """Compress ``source`` into a new file (``<stem>-compressed.pdf`` by default).

    ``target`` (bytes) switches to the search described in the module docstring and cannot
    be combined with a ``level`` other than the default. ``grayscale`` converts colour
    images to grey and needs ``low``/``medium``/``high`` (or ``target``).
    """
    if level not in LEVELS:
        raise InvalidOptionError(f"Unknown level '{level}'. Choose lossless, low, medium or high.")
    if target is not None and level != "lossless":
        raise InvalidOptionError("--target picks the level itself; do not combine it with --level.")
    if grayscale and target is None and level == "lossless":
        raise InvalidOptionError("--grayscale needs --level low, medium or high (or --target).")
    if target is not None and target < 1:
        raise InvalidOptionError("The target size must be greater than zero.")

    dest = dest or output_path_for(source, "compressed")
    original = source.stat().st_size

    def result(**kwargs: Any) -> CompressResult:
        return CompressResult(command="compress", sources=[source], level=level,
                              grayscale=grayscale, target_bytes=target, bytes_in=original,
                              **kwargs)  # fmt: skip

    if target is not None and original <= target:
        report(progress, 1, 1, "Already within the target")
        return result(
            kept_original=True, target_reached=True, bytes_out=original, attempts=0,
            notes=["The file is already within the target size; nothing was changed."],
        )  # fmt: skip

    attempts_done = 0
    shared = _Shared(cancel, keep_pixels=target is not None)
    # coarse: 10 steps per attempt (and one for fine-tuning to a target), then publishing
    total_steps = (10 + 5) if target is None else (MAX_ATTEMPTS * 10 + 15)
    with tempfile.TemporaryDirectory(prefix="pdfzi-") as scratch:
        scratch_dir = Path(scratch)
        steps = 0

        def attempt(
            profile: Profile, label: str, chosen: dict[tuple[int, int], _Outcome] | None = None
        ) -> _Attempt:
            nonlocal steps
            start = steps

            def on_image(i: int, count: int) -> None:
                report(progress, start + (i * 10 // max(count, 1)), total_steps, label, cancel)

            report(progress, start, total_steps, label, cancel)
            done = _attempt(
                source, password, profile, grayscale,
                scratch_dir / f"attempt-{start // 10}.pdf", on_image, shared, chosen,
            )  # fmt: skip
            steps += 10
            return done

        def search_attempt(profile: Profile) -> _Attempt:
            nonlocal attempts_done
            label = f"Attempt {attempts_done + 1}/{MAX_ATTEMPTS}: {profile.describe()}"
            attempts_done += 1
            return attempt(profile, label)

        if target is None:
            best = attempt(PROFILES[level], PROFILES[level].describe())
            attempts_done = 1
        else:
            best = _search(target, search_attempt, attempt)

        # Said whichever attempt won: with --target the lossless one may be the smallest.
        intricate = [_INTRICATE_NOTE] if shared.done and shared.extents is None else []
        reached = None if target is None else best.size <= target
        stats = best.stats
        extras: dict[str, Any] = {
            "attempts": attempts_done,
            "settings": best.profile.describe(),
            "images_found": stats.found,
            "images_recompressed": stats.recompressed,
            "images_skipped": dict(stats.skipped),
            "pages_in": best.pages,
            "target_reached": reached,
        }

        mixed = [best.note] if best.note else []
        worthwhile = best.size <= original * (1 - MIN_SAVING) or bool(reached)
        if best.size >= original or not worthwhile:
            report(progress, total_steps, total_steps, "Done")
            why = (
                "pdfzi could not make this file smaller"
                if best.size >= original
                else f"The best attempt saves less than {MIN_SAVING:.0%} (not worth a new file)"
            )
            return result(
                kept_original=True,
                bytes_out=original,
                notes=[
                    f"{why}, so it was left unchanged "
                    f"(best attempt: {best.size:,} bytes, original: {original:,} bytes).",
                    *_image_notes(best.profile, stats),
                    *intricate,
                ],
                **extras,
            )

        report(progress, total_steps - 3, total_steps, "Writing result", cancel)
        with safe_output(dest, force=force, sources=[source]) as out:
            try:
                shutil.copyfile(best.path, out.tmp)
            except OSError as exc:
                raise OutputWriteError(
                    f"Could not write the result ({exc.strerror or exc})."
                ) from exc
            verify_output(out.tmp, expected_pages=best.pages, password=None)
        assert out.path is not None

    notes = _image_notes(best.profile, stats) + mixed + intricate
    if best.was_encrypted:
        notes.append(DECRYPTED_NOTE)
    notes += best.fidelity
    if target is not None and not reached:
        notes.append(
            f"The target was not reached: the smallest result is {best.size:,} bytes "
            f"and the target is {target:,} bytes."
        )
    report(progress, total_steps, total_steps, "Done")
    return result(
        outputs=[out.path],
        pages_out=best.pages,
        bytes_out=out.path.stat().st_size,
        notes=notes,
        **extras,
    )


def _search(target: int, search_attempt: Any, attempt: Any) -> _Attempt:
    """The ``--target`` search: the gentlest settings whose result fits ``target``.

    Lossless first; then the floor (if even that is too big, the smallest result is
    returned); then halving the strength scale between the strongest attempt that is
    known to be too big and the gentlest that fits, until a result lands within
    ``TARGET_TOLERANCE`` under the target or the two are neighbours. A result still well
    under the target finally gets some images back at the gentler settings.
    """
    lossless = search_attempt(PROFILES["lossless"])
    if lossless.size <= target:
        return lossless
    floor = search_attempt(strength_profile(MAX_STRENGTH))
    if floor.size > target:
        return min(lossless, floor, key=lambda a: a.size)

    # Only the attempt that fits is kept on disk; of a too big one only its images matter.
    close_enough = target * (1 - TARGET_TOLERANCE)
    too_big, too_big_at = lossless, -1  # strength -1 stands for lossless
    fits, fits_at = floor, MAX_STRENGTH
    lossless.path.unlink()
    while fits_at - too_big_at > 1 and fits.size < close_enough:
        middle = (too_big_at + fits_at) // 2
        current = search_attempt(strength_profile(middle))
        if current.size <= target:
            fits.path.unlink()
            fits, fits_at = current, middle
        else:
            current.path.unlink()
            too_big, too_big_at = current, middle
    if fits.size >= close_enough:
        return fits

    # Well under the target, but the next gentler step is too big: give that step's
    # version (or the original) back to as many images as the room left allows.
    room = target - fits.size - max(1024, target // 100)  # a margin for the PDF around them
    mix = _gentler_mix(too_big, fits, room)
    if mix is None:
        return fits
    chosen, gentler = mix
    mixed = attempt(fits.profile, "Fine-tuning to the target", chosen)
    if not fits.size < mixed.size <= target:
        mixed.path.unlink()
        return fits
    fits.path.unlink()
    if gentler:  # (images given back their original are listed among the skipped ones)
        mixed.note = (
            f"To stay close to the target, {gentler} of the recompressed images use gentler "
            f"settings ({too_big.profile.describe()})."
        )
    return mixed
