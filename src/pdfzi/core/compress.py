"""``compress``: make PDFs smaller without turning pages into pictures.

Text and vector content are never rasterised. Three levels:

* ``lossless``: images are untouched; the file is rewritten with better stream compression,
  object streams, and unused page resources removed.
* ``medium`` / ``high``: additionally, images that are sharper than needed are scaled down
  (to about 170 / 120 dpi *as they are drawn on the page*, so a small picture with many
  pixels shrinks and a large one is not squeezed more than its size calls for) and
  re-encoded as JPEG.
* ``target``: tries lossless first, then ever stronger image settings, each time starting
  again from the original, until the result is no bigger than the target (or the attempts
  run out, in which case the smallest result is returned and ``target_reached`` is False).

A result that is not at least 1 % smaller than the input is not written either (unless a
``target`` was asked for and reached): the input is kept as is and the result says so
(``kept_original``).
"""

from __future__ import annotations

import io
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Name
from PIL import Image

from pdfzi.core.errors import InvalidOptionError, OutputWriteError
from pdfzi.core.fidelity import claims_pdfa, fidelity_notes
from pdfzi.core.output import DECRYPTED_NOTE
from pdfzi.core.paths import output_path_for
from pdfzi.core.pdfimages import image_extents, iter_images
from pdfzi.core.pdfio import open_pdf, save_pdf
from pdfzi.core.progress import CancelToken, ProgressCallback, report
from pdfzi.core.results import OperationResult
from pdfzi.core.safeio import safe_output
from pdfzi.core.verify import verify_output

LEVELS = ("lossless", "medium", "high")


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


# 170 dpi is about 2000 px across a whole A4 page, 120 dpi about 1400 px: the values that
# were checked by eye on scanned text and photos.
PROFILES = {
    "lossless": Profile("lossless"),
    "medium": Profile("medium", dpi=170, quality=80, max_side=2000),
    "high": Profile("high", dpi=120, quality=60, max_side=1400),
}
# --target: from gentle to aggressive. Index 0 (lossless) is always tried first.
LADDER = (
    Profile("t1", 220, 88, 2400),
    Profile("t2", 170, 82, 2000),
    Profile("t3", 140, 75, 1600),
    Profile("t4", 120, 68, 1400),
    Profile("t5", 100, 60, 1200),
    Profile("t6", 85, 52, 1000),
    Profile("t7", 70, 45, 800),
    Profile("t8", 55, 38, 640),
    Profile("t9", 40, 30, 480),
)
_SCALE_ONLY_BELOW = 0.85  # resizing for a smaller saving than 15 % only softens the picture
MAX_ATTEMPTS = 1 + len(LADDER)
_MAX_PIXELS = 250_000_000  # refuse to decode absurdly large images
MIN_SAVING = 0.01  # a result that saves less than this is not worth a new file

_SKIP_SOFT_MASK = "has a transparency mask"
_SKIP_STENCIL = "is a 1-bit or stencil image"
_SKIP_FILTER = "uses a filter pdfzi does not touch (JBIG2, CCITT, JPEG2000, ...)"
_SKIP_COLORSPACE = "uses a special colour space"
_SKIP_DEPTH = "is not 8 bits per channel"
_SKIP_DECODE = "has a custom /Decode array"
_SKIP_CMYK_JPEG = "is a CMYK JPEG"
_SKIP_UNREADABLE = "could not be decoded"
_SKIP_NOT_SMALLER = "did not get smaller"


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
    data = obj.read_bytes()
    if len(data) != width * height * comps:
        raise ValueError("unexpected amount of pixel data")
    return Image.frombytes(plan.mode, (width, height), data)


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


def _recompress_image(
    obj: pikepdf.Object,
    plan: _Plan,
    profile: Profile,
    grayscale: bool,
    extent: tuple[float, float] | None = None,
) -> bool:
    """Try to replace ``obj`` with a smaller JPEG. Returns False if it did not get smaller.

    ``extent`` is the size in inches at which the image is drawn (from
    :func:`~pdfzi.core.pdfimages.image_extents`). The PDF object is only modified once
    everything has succeeded.
    """
    assert profile.quality is not None
    img = _decode(obj, plan)
    width, height = img.size
    new_size = _scaled_size(width, height, extent, profile)
    if new_size != (width, height):
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    mode = plan.mode
    if grayscale and mode != "L":
        img = img.convert("L")
        mode = "L"

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=profile.quality, optimize=True)
    jpeg = buf.getvalue()
    if len(jpeg) >= len(obj.read_raw_bytes()):
        return False

    colorspace = plan.colorspace if mode == plan.mode else Name.DeviceGray
    obj.write(jpeg, filter=Name.DCTDecode)  # also drops any old /DecodeParms
    obj.Width, obj.Height = img.size
    obj.ColorSpace = colorspace
    obj.BitsPerComponent = 8
    if mode == "CMYK":
        # Pillow (like Photoshop) stores CMYK JPEG values inverted; /Decode undoes that.
        obj.Decode = pikepdf.Array([1, 0, 1, 0, 1, 0, 1, 0])
    return True


def _recompress_all(
    pdf: pikepdf.Pdf,
    profile: Profile,
    grayscale: bool,
    stats: _ImageStats,
    on_image: Any,
) -> None:
    images = list(iter_images(pdf))  # each distinct image once, however many pages use it
    extents = image_extents(pdf)  # how big each is drawn on the page(s)
    stats.found = len(images)
    for i, obj in enumerate(images):
        on_image(i, len(images))
        plan = _inspect(obj)
        if isinstance(plan, str):
            stats.skipped[plan] += 1
            continue
        try:
            done = _recompress_image(obj, plan, profile, grayscale, extents.get(obj.objgen))
        except (
            OSError,
            ValueError,
            TypeError,
            MemoryError,
            pikepdf.PdfError,
            Image.DecompressionBombError,
        ):
            stats.skipped[_SKIP_UNREADABLE] += 1
            continue
        if done:
            stats.recompressed += 1
        else:
            stats.skipped[_SKIP_NOT_SMALLER] += 1


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


def _attempt(
    source: Path,
    password: str | None,
    profile: Profile,
    grayscale: bool,
    out_path: Path,
    on_image: Any,
) -> _Attempt:
    """Compress ``source`` with ``profile`` into ``out_path`` (a scratch file)."""
    stats = _ImageStats()
    with open_pdf(source, password) as pdf:
        pages = len(pdf.pages)
        was_encrypted = pdf.is_encrypted
        fidelity = fidelity_notes(pdf)
        if profile.lossy:
            _recompress_all(pdf, profile, grayscale, stats, on_image)
        else:
            stats.found = sum(1 for _ in iter_images(pdf))
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
        out_path, out_path.stat().st_size, profile, stats, pages, was_encrypted, fidelity
    )


def _image_notes(profile: Profile, stats: _ImageStats) -> list[str]:
    """Sentences about what happened to images (only meaningful for lossy attempts)."""
    if not profile.lossy or not stats.found:
        return []
    lines = [f"Images: {stats.recompressed} of {stats.found} recompressed ({profile.describe()})."]
    lines += [f"{count} image(s) left as they were: {why}." for why, count in stats.skipped.items()]
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
    images to grey and needs ``medium``/``high`` (or ``target``).
    """
    if level not in LEVELS:
        raise InvalidOptionError(f"Unknown level '{level}'. Choose lossless, medium or high.")
    if target is not None and level != "lossless":
        raise InvalidOptionError("--target picks the level itself; do not combine it with --level.")
    if grayscale and target is None and level == "lossless":
        raise InvalidOptionError("--grayscale needs --level medium or high (or --target).")
    if target is not None and target < 1:
        raise InvalidOptionError("The target size must be greater than zero.")

    dest = dest or output_path_for(source, "compressed")
    original = source.stat().st_size
    plan = [PROFILES[level]] if target is None else [PROFILES["lossless"], *LADDER]

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

    best: _Attempt | None = None
    attempts_done = 0
    with tempfile.TemporaryDirectory(prefix="pdfzi-") as scratch:
        scratch_dir = Path(scratch)
        total_steps = len(plan) * 10 + 5  # coarse: 10 steps per attempt, then publishing
        for n, profile in enumerate(plan):
            label = (
                f"Attempt {n + 1}/{len(plan)}: {profile.describe()}"
                if target
                else profile.describe()
            )

            def on_image(i: int, count: int, n: int = n, label: str = label) -> None:
                report(progress, n * 10 + (i * 10 // max(count, 1)), total_steps, label, cancel)

            report(progress, n * 10, total_steps, label, cancel)
            current = _attempt(
                source, password, profile, grayscale, scratch_dir / f"attempt-{n}.pdf", on_image
            )
            attempts_done += 1
            if best is None or current.size < best.size:
                best = current
            if target is not None and current.size <= target:
                break

        assert best is not None
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
                ],
                **extras,
            )

        report(progress, total_steps - 3, total_steps, "Writing result", cancel)
        with safe_output(dest, force=force) as out:
            try:
                shutil.copyfile(best.path, out.tmp)
            except OSError as exc:
                raise OutputWriteError(
                    f"Could not write the result ({exc.strerror or exc})."
                ) from exc
            verify_output(out.tmp, expected_pages=best.pages, password=None)
        assert out.path is not None

    notes = _image_notes(best.profile, stats)
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
