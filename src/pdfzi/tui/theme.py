"""The ``pdfzi-red`` theme: red on a near-black background.

Colour never carries meaning alone: success, warning and error lines always start with
a word in brackets: ``[ok]``, ``[warn]`` or ``[error]`` (see :mod:`pdfzi.tui.screens.result`).
"""

from __future__ import annotations

from textual.theme import Theme

THEME_NAME = "pdfzi-red"

PDFZI_RED = Theme(
    name=THEME_NAME,
    primary="#DC2626",
    secondary="#991B1B",
    accent="#F87171",
    foreground="#F5EDED",
    background="#0F0A0A",
    surface="#1C1212",
    panel="#2A1818",
    success="#4ADE80",
    warning="#FBBF24",
    error="#FF7AA2",  # pink-ish so it differs from the red theme colour; the [error] word still marks it
    dark=True,
)

CSS = """
Screen { background: $background; color: $foreground; }
#titlebar { background: $primary; color: white; text-style: bold; height: 1; padding: 0 1; }
Footer { background: $surface; }
Footer .footer-key--key { color: $accent; background: $surface; text-style: bold; }
Footer .footer-key--description { color: $foreground; background: $surface; }
.title { color: $primary; text-style: bold; padding: 0 1; height: 1; }
.hint { color: $foreground 60%; padding: 0 1; }
.status { padding: 0 1; height: 1; }
OptionList { background: $surface; border: none; padding: 0 1; }
OptionList:focus { border: none; }
OptionList > .option-list--option-highlighted { background: $primary; color: white; text-style: bold; }
OptionList:focus > .option-list--option-highlighted { background: $primary; color: white; }
Input { border: none; height: 1; padding: 0 1; background: $panel; }
Input:focus { border: none; background: $secondary; }
Selector { height: 1; width: auto; padding: 0 1; background: $surface; }
Checkbox { border: none; height: 1; width: auto; padding: 0 1; background: $surface; }
Checkbox:focus { background: $primary; color: white; }
Checkbox:focus > .toggle--label { background: $primary; color: white; text-style: bold; }
Button { border: none; height: 1; min-width: 14; background: $primary; color: white; text-style: bold; }
Button:focus { background: $accent; color: $background; }
.bar { height: 1; }
.bar .status { width: 1fr; }
BarButton { min-width: 0; width: auto; padding: 0 2; margin: 0 0 0 1; background: $panel; color: $foreground; }
BarButton:hover { background: $secondary; }
BarButton#continue { background: $primary; color: white; }
BarButton#continue:hover { background: $accent; color: $background; }
ProgressBar { padding: 0 1; }
Bar > .bar--bar { color: $primary; }
Bar > .bar--complete { color: $success; }
.row { height: 1; margin: 0; }
.row .label { width: 20; padding: 0 1; }
.row:focus-within { background: $panel; }
.size-input { width: 12; }
.note { color: $foreground 55%; padding: 0 1; width: 1fr; height: 1; }
.row:focus-within .label { color: $accent; text-style: bold; }
#logo { padding: 0 2; margin: 1 0; }
#tagline { color: $foreground 70%; padding: 0 2; height: 1; }
#pathrow { height: 1; background: $panel; }
#pathrow Label { width: auto; padding: 0 1; color: $accent; text-style: bold; }
#pathbar { width: 1fr; background: $panel; }
#pathbar:focus { background: $secondary; }
#form { height: auto; max-height: 13; }
#files, #order, #tools { height: 1fr; }
#log { height: auto; padding: 0 1; }
HelpScreen { align: center middle; }
#help-box { width: 76; height: auto; background: $surface; border: round $primary; padding: 0 2; }
"""
