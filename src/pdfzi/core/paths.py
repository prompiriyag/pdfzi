"""Input expansion and output naming. Everything is ``pathlib`` and Unicode-safe.

Windows shells do not expand ``*.pdf`` for us, so :func:`expand_inputs` does it.
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Collection, Iterable
from pathlib import Path

from pdfzi.core.errors import (
    InputNotFoundError,
    InvalidOptionError,
    NotAFileError,
    OutputWriteError,
)

PDF_SUFFIXES = frozenset({".pdf"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})

_GLOB_CHARS = frozenset("*?[")
_DIGITS = re.compile(r"(\d+)")


def natural_key(path: Path) -> list[object]:
    """Sort key so that ``page2`` comes before ``page10`` (case-insensitive)."""
    return [int(t) if t.isdigit() else t.casefold() for t in _DIGITS.split(path.name)]


def _same_file_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))  # noqa: PTH100 - no pathlib equivalent that skips symlinks


def expand_inputs(
    args: Iterable[str | Path],
    *,
    suffixes: Collection[str] | None = None,
) -> list[Path]:
    """Resolve command-line inputs to existing files.

    An argument that names an existing file is used as is (so ``report[1].pdf`` works).
    Otherwise, if it contains ``* ? [`` it is treated as a glob pattern; matches are
    naturally sorted and, when ``suffixes`` is given, filtered to those extensions.
    Duplicates are dropped, keeping the first occurrence.
    """
    wanted = {s.lower() for s in suffixes} if suffixes else None
    result: list[Path] = []
    seen: set[str] = set()

    for arg in args:
        text = str(arg)
        path = Path(text)
        if path.exists():
            matches = [path]
        elif _GLOB_CHARS & set(text):
            matches = sorted((Path(m) for m in glob.glob(text)), key=natural_key)  # noqa: PTH207
            matches = [
                m for m in matches if m.is_file() and (wanted is None or m.suffix.lower() in wanted)
            ]
            if not matches:
                raise InputNotFoundError(f"No files match '{text}'.")
        else:
            raise InputNotFoundError(f"File not found: {path}")

        for match in matches:
            if not match.is_file():
                raise NotAFileError(f"Not a file (is it a folder?): {match}")
            key = _same_file_key(match)
            if key not in seen:
                seen.add(key)
                result.append(match)
    return result


def unique_path(path: Path) -> Path:
    """Return ``path`` if free, else ``name-2.ext``, ``name-3.ext`` ... in the same folder."""

    def taken(p: Path) -> bool:
        return p.exists() or p.is_symlink()

    if not taken(path):
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not taken(candidate):
            return candidate
    raise OutputWriteError(f"Too many files named like {path.name} already exist in {path.parent}.")


def unique_dir(path: Path) -> Path:
    """Return ``path`` if no such entry exists, else ``path-2``, ``path-3`` ..."""

    def taken(p: Path) -> bool:
        return p.exists() or p.is_symlink()

    if not taken(path):
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.name}-{n}")
        if not taken(candidate):
            return candidate
    raise OutputWriteError(f"Too many folders named like {path.name} already exist.")


def is_same_file(a: Path, b: Path) -> bool:
    """True if both paths exist and point at the same file."""
    try:
        return a.samefile(b)
    except OSError:
        return False


def looks_like_folder(output: str | Path) -> bool:
    """A folder is an existing directory or a path the user ended with a separator."""
    return Path(output).is_dir() or str(output).endswith(("/", "\\"))


def plan_outputs(
    sources: list[Path],
    suffix: str,
    output: str | Path | None,
    *,
    ext: str = ".pdf",
) -> list[Path]:
    """Output path for every source in a batch (see :func:`output_path_for`).

    With several sources, ``output`` must be a folder (it is created when missing).
    """
    if output is not None and len(sources) > 1:
        if Path(output).suffix.lower() == ext and not looks_like_folder(output):
            raise InvalidOptionError(
                f"With several input files, --output must be a folder, not a file ({output})."
            )
        folder = Path(output)
        return [folder / f"{s.stem}-{suffix}{ext}" for s in sources]
    return [output_path_for(s, suffix, output=output, ext=ext) for s in sources]


def output_path_for(
    source: Path,
    suffix: str,
    *,
    output: str | Path | None = None,
    ext: str = ".pdf",
) -> Path:
    """Decide where the result for ``source`` should go (before collision handling).

    * no ``output``: next to the source, as ``<stem>-<suffix><ext>``
    * ``output`` is an existing folder or ends with a path separator: inside it, same name
    * otherwise ``output`` is taken to be the exact result file path

    Pass ``output`` as the raw string the user typed: ``Path`` drops a trailing separator,
    which is how users say "this is a folder that does not exist yet".
    Collisions are resolved at write time by :func:`pdfzi.core.safeio.safe_output`.
    """
    name = f"{source.stem}-{suffix}{ext}"
    if output is None:
        return source.with_name(name)
    path = Path(output)
    if path.is_dir():
        return path / name
    text = str(output)
    if text.endswith(("/", "\\")):
        # A trailing separator names a folder. Drop it (both kinds, so that a backslash typed
        # on macOS or Linux, where it is an ordinary character, does not end up in the name).
        return Path(text.rstrip("/\\") or text) / name
    return path
