"""Safe output writing: temp file first, atomic ``os.replace`` only on success.

Usage::

    with safe_output(dest, force=False) as out:
        save_pdf(pdf, out.tmp)
        verify_output(out.tmp, expected_pages=n)   # raise here -> nothing is published
    print(out.path)                                # the final path actually used

If the body raises (including cancellation), the temp file is deleted and ``dest`` is
left exactly as it was. Without ``force`` an existing ``dest`` is never replaced: the
result gets ``-2``, ``-3`` ... appended instead.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pdfzi.core.errors import OutputWriteError
from pdfzi.core.paths import unique_path


@dataclass
class SafeOutput:
    tmp: Path
    dest: Path
    path: Path | None = None  # set once the result has been published


def _new_temp_file(folder: Path) -> Path:
    """Create an empty temp file next to the result and return its path.

    Unlike ``tempfile.mkstemp`` (always private, mode 0600) the file gets the mode any new
    file would get from the user's umask, so a result is as readable as the user's other files.
    """
    for _ in range(100):
        path = folder / f".pdfzi-{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise FileExistsError(f"could not pick a free temporary name in {folder}")


@contextmanager
def safe_output(dest: Path, *, force: bool = False) -> Iterator[SafeOutput]:
    dest = Path(dest)
    if dest.is_dir():
        raise OutputWriteError(f"Cannot write {dest}: it is a folder.")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = _new_temp_file(dest.parent)
    except OSError as exc:
        raise OutputWriteError(
            f"Cannot write to folder {dest.parent} ({exc.strerror or exc})."
        ) from exc

    out = SafeOutput(tmp=Path(tmp_name), dest=dest)
    try:
        yield out
        final = dest if force else unique_path(dest)
        try:
            os.replace(out.tmp, final)  # noqa: PTH105 - project rule: atomic os.replace
        except OSError as exc:
            raise OutputWriteError(
                f"Cannot save {final.name} ({exc.strerror or exc}). "
                "If it is open in another program, close it and try again."
            ) from exc
        out.path = final
    finally:
        with contextlib.suppress(OSError):
            out.tmp.unlink(missing_ok=True)
