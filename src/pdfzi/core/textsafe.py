"""Make text from outside pdfzi safe to show in a terminal.

File names and PDF metadata (title, author ...) are chosen by whoever made the file. They
can contain control characters such as ESC, which a terminal treats as commands, not text:
a title could change the window title or, on terminals that allow it (OSC 52), write
something into the user's clipboard. Everything that is printed or drawn on screen and
did not come from pdfzi itself goes through :func:`printable` first.
"""

from __future__ import annotations

import re

# C0 controls, DEL and C1 controls (0x9B is a one-character "CSI" on some terminals).
_ANY_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_EXCEPT_LINES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n
_C1_AND_DEL = re.compile(r"[\x7f-\x9f]")


def _single_line(match: re.Match[str]) -> str:
    return " " if match.group() in "\t\n\r" else "?"


def printable(text: str, *, multiline: bool = False) -> str:
    """``text`` with every control character made harmless.

    Control characters become ``?`` (as ``ls`` shows them). By default the result is one
    line: tabs and line breaks become spaces, so a crafted title cannot fake extra output
    lines. ``multiline=True`` keeps tabs and ``\\n`` (for pdfzi's own multi-line messages).
    Ordinary text, Thai included, is returned unchanged.
    """
    if multiline:
        return _CONTROL_EXCEPT_LINES.sub("?", text)
    return _ANY_CONTROL.sub(_single_line, text)


def escape_json_controls(text: str) -> str:
    """Escape DEL and C1 controls in JSON text (``json.dumps`` escapes only C0 ones).

    The result is the same JSON value: ``\\u009b`` and the raw character mean the same.
    """
    return _C1_AND_DEL.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
