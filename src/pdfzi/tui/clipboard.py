"""Read the operating system's clipboard, for ``ctrl+v``.

Textual's own ``ctrl+v`` only knows text copied inside the app, so it does nothing for a
path copied in Explorer. This reads the real clipboard on this computer: nothing is sent
anywhere. On Windows a file copied in Explorer (not only copied text) is understood too.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def read_text() -> str | None:
    """The clipboard as text (one path per line for files copied in Explorer).

    Returns ``None`` if the clipboard cannot be read here (e.g. no ``xclip`` on Linux).
    """
    try:
        if sys.platform == "win32":
            return _windows()
        if sys.platform == "darwin":
            return _run(["pbpaste"])
        for command in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
            if shutil.which(command[0]):
                return _run(command)
        if shutil.which("xsel"):
            return _run(["xsel", "--clipboard", "--output"])
    except Exception:  # a clipboard problem must never crash the app
        return None
    return None


def _run(command: list[str]) -> str | None:
    done = subprocess.run(command, capture_output=True, timeout=3, check=False)
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else None


def _windows() -> str | None:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    cf_unicodetext, cf_hdrop = 13, 15
    user32, kernel32, shell32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.shell32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    shell32.DragQueryFileW.restype = wintypes.UINT

    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(cf_unicodetext)
        if handle:
            pointer = kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        drop = user32.GetClipboardData(cf_hdrop)  # files copied in Explorer
        if drop:
            paths = []
            for i in range(shell32.DragQueryFileW(drop, 0xFFFFFFFF, None, 0)):
                size = shell32.DragQueryFileW(drop, i, None, 0)
                buffer = ctypes.create_unicode_buffer(size + 1)
                shell32.DragQueryFileW(drop, i, buffer, size + 1)
                paths.append(buffer.value)
            return "\n".join(paths)
        return ""
    finally:
        user32.CloseClipboard()
