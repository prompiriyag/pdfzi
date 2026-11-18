"""Behaviour shared by every screen: ``esc`` back, ``?`` help, ``q`` quit."""

from __future__ import annotations

from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static


class TitleBar(Static):
    """Top bar: ``pdfzi - <where you are>``. Plain text, no background tasks."""

    def __init__(self) -> None:
        super().__init__("pdfzi", id="titlebar")


class BaseScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit_app", "Quit"),
    ]

    def set_title(self, text: str) -> None:
        self.query_one(TitleBar).update(f"pdfzi — {text}")

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_help(self) -> None:
        from pdfzi.tui.screens.help import HelpScreen

        self.app.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        self.app.exit()
