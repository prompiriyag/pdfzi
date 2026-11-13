"""The pdfzi application object."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from pdfzi.tui.screens import MenuScreen
from pdfzi.tui.theme import CSS, PDFZI_RED, THEME_NAME


class PdfziApp(App[None]):
    TITLE = "pdfzi"
    SUB_TITLE = "local PDF toolkit"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, start_dir: Path | None = None) -> None:
        super().__init__()
        self.start_dir = (start_dir or Path.cwd()).resolve()

    def on_mount(self) -> None:
        self.register_theme(PDFZI_RED)
        self.theme = THEME_NAME
        self.push_screen(MenuScreen())

    def pop_to_menu(self) -> None:
        """Drop every screen above the menu (the first screen is Textual's empty default)."""
        while len(self.screen_stack) > 2:
            self.pop_screen()


def run(start_dir: Path | None = None) -> None:
    PdfziApp(start_dir).run()
