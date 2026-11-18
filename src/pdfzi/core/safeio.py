"""Safe output writing: temp file first, published only on success.

Usage::

    with safe_output(dest, force=False, sources=[source]) as out:
        save_pdf(pdf, out.tmp)
        verify_output(out.tmp, expected_pages=n)   # raise here -> nothing is published
    print(out.path)                                # the final path actually used

If the body raises (including cancellation), the temp file is deleted and ``dest`` is
left exactly as it was.

Without ``force`` an existing file is never replaced: the result gets ``-2``, ``-3`` ...
appended instead. The name is claimed by an operation that fails when the name is taken
(a rename on Windows, a hard link on macOS/Linux), so a file that appears while pdfzi is
working is not replaced either. Where hard links are not available (FAT32/exFAT drives,
some network shares) the name is first reserved with an empty file and the result then
moved onto it. That stops two pdfzi runs from taking the same name, but not a program
that writes into the reserved file in the moment before pdfzi moves the result onto it.

With ``force`` the result atomically replaces ``dest``; ``out.replaced`` says whether a
file was there.

On macOS/Linux a result is never readable by more people than its sources: its mode is
the usual one for a new file (from the umask), narrowed to what every source (and, with
``force``, the file it replaces) allows. While it is being written it is private.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pdfzi.core.errors import OutputWriteError
from pdfzi.core.paths import candidate_paths

_POSIX = os.name != "nt"


@dataclass
class SafeOutput:
    tmp: Path
    dest: Path
    path: Path | None = None  # set once the result has been published
    replaced: bool = False  # True if publishing replaced an existing file (force only)
    _mode: int | None = field(default=None, repr=False)  # mode a new file gets (POSIX)


def _umask() -> int:
    """The process umask. It can only be read by setting it, so it is put straight back;
    in between, a file another thread creates can only come out more private."""
    mask = os.umask(0o077)
    os.umask(mask)
    return mask


def _new_temp_file(folder: Path) -> tuple[Path, int | None]:
    """Create an empty, private temp file next to the result.

    It is private from the moment it exists: a file that is readable for a moment can be
    opened by someone else then, and stays readable to them through that handle.

    Returns its path and, on macOS/Linux, the mode the umask gives a new file (which the
    result gets in the end, see :func:`_final_mode`).
    """
    for _ in range(100):
        path = folder / f".pdfzi-{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return path, (0o666 & ~_umask() if _POSIX else None)
    raise FileExistsError(f"could not pick a free temporary name in {folder}")


def _final_mode(usual: int, limits: Iterable[Path]) -> int:
    """``usual`` narrowed to what every file in ``limits`` allows. The owner keeps rw."""
    mode = usual
    for path in limits:
        try:
            mode &= stat.S_IMODE(path.stat().st_mode) | 0o600
        except OSError:
            continue  # gone or unreadable: nothing to follow
    return mode


def _apply_mode(out: SafeOutput, sources: Iterable[Path], replacing: Path | None) -> None:
    if out._mode is None:
        return
    limits = [*sources, replacing] if replacing is not None else list(sources)
    with contextlib.suppress(OSError):  # some drives have no POSIX modes: never fail on it
        out.tmp.chmod(_final_mode(out._mode, limits))


def _taken(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _same_empty_file(path: Path, before: os.stat_result) -> bool:
    try:
        now = path.lstat()
    except OSError:
        return False
    return (now.st_dev, now.st_ino, now.st_size) == (before.st_dev, before.st_ino, 0)


def _reserve_then_move(tmp: Path, dest: Path) -> bool:
    """Fallback for drives without hard links: reserve ``dest`` empty, then move onto it."""
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        reserved = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, dest)  # noqa: PTH105 - onto our own reservation
    except BaseException:
        if _same_empty_file(dest, reserved):  # never remove something another program wrote
            with contextlib.suppress(OSError):
                dest.unlink()
        raise
    return True


def _place_new(tmp: Path, dest: Path) -> bool:
    """Move ``tmp`` to ``dest`` only if no file has that name. False if the name is taken."""
    if not _POSIX:
        try:
            os.rename(tmp, dest)  # noqa: PTH104 - on Windows this refuses an existing name
        except FileExistsError:
            return False
        return True
    try:
        os.link(tmp, dest)  # fails if the name is taken, atomically
    except FileExistsError:
        return False
    except OSError:
        if _taken(dest):
            return False
        return _reserve_then_move(tmp, dest)
    with contextlib.suppress(OSError):
        tmp.unlink()
    return True


def _publish(out: SafeOutput, *, force: bool, sources: Iterable[Path]) -> Path:
    if force:
        replacing = out.dest if _taken(out.dest) else None
        _apply_mode(out, sources, replacing)
        if _place_new(out.tmp, out.dest):
            return out.dest
        os.replace(out.tmp, out.dest)  # noqa: PTH105 - atomic replace, asked for by --force
        out.replaced = True
        return out.dest
    _apply_mode(out, sources, None)
    for candidate in candidate_paths(out.dest):
        if not _taken(candidate) and _place_new(out.tmp, candidate):
            return candidate
    raise OutputWriteError(
        f"Too many files named like {out.dest.name} already exist in {out.dest.parent}."
    )


@contextmanager
def safe_output(
    dest: Path, *, force: bool = False, sources: Iterable[Path] = ()
) -> Iterator[SafeOutput]:
    """Write to ``out.tmp`` inside the block; the result is published when it ends.

    ``sources`` are the files the result is made from; on macOS/Linux the result is not
    made readable by anyone who cannot read all of them.
    """
    dest = Path(dest)
    sources = list(sources)
    if dest.is_dir():
        raise OutputWriteError(f"Cannot write {dest}: it is a folder.")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_name, usual = _new_temp_file(dest.parent)
    except OSError as exc:
        raise OutputWriteError(
            f"Cannot write to folder {dest.parent} ({exc.strerror or exc})."
        ) from exc

    out = SafeOutput(tmp=Path(tmp_name), dest=dest, _mode=usual)
    try:
        yield out
        try:
            out.path = _publish(out, force=force, sources=sources)
        except OutputWriteError:
            raise
        except OSError as exc:
            raise OutputWriteError(
                f"Cannot save {dest.name} ({exc.strerror or exc}). "
                "If it is open in another program, close it and try again."
            ) from exc
    finally:
        with contextlib.suppress(OSError):
            out.tmp.unlink(missing_ok=True)
