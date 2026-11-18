from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

HELP_TEXT = """\
[b]Everywhere[/b]   q quit   esc back / cancel   ? this help   up/down move

[b]Choosing files[/b]  (the path bar is selected first)
 one file: paste (ctrl+v / right-click / drop it here) or type its path, then Enter -
 goes straight to the settings. A folder path opens the folder.
 several files: down arrow to the list, space ticks each, Enter goes on
 up arrow from the top of the list (or ctrl+l) returns to the path bar
 space  tick a file     enter  open a folder, or continue     ctrl+a  tick all shown
 /  filter     backspace  parent folder     ctrl+n  continue (same as enter)
 mouse: a click ticks a file or opens a folder; the Continue button goes on

[b]Ordering files[/b] (merge, images to PDF)
 u / d  (or shift+up/down)  move file up / down      x  remove it
 mouse: click a file, then the buttons under the list

[b]Settings[/b]
 up/down (or tab)  next / previous field     left/right  change a choice
 space  tick or change     enter  next field (on Start: run)     ctrl+r  start from anywhere

Your files never leave this computer. Results are new files; nothing is
overwritten.                                   [b]esc[/b] or [b]?[/b] closes this help
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-box")

    def action_close(self) -> None:
        self.dismiss(None)
