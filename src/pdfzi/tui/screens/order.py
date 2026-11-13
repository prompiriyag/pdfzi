"""Put the chosen files in order (merge, images to PDF) using only the keyboard."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from pdfzi.tui.screens.base import BaseScreen, TitleBar
from pdfzi.tui.tools import Tool
from pdfzi.tui.widgets import OrderList


class OrderScreen(BaseScreen):
    BINDINGS = [
        Binding("u,shift+up", "move_up", "Move up"),
        Binding("d,shift+down", "move_down", "Move down"),
        Binding("x,delete", "remove", "Remove"),
        Binding("ctrl+n", "next", "Next", show=False),
    ]

    def __init__(self, tool: Tool, files: list[Path]) -> None:
        super().__init__()
        self.tool = tool
        self.files = files  # shared with the next screen: reordering here changes the job

    def compose(self) -> ComposeResult:
        yield TitleBar()
        yield Static("Put the files in the order they should appear.", classes="title")
        yield OrderList(id="order")
        yield Static("", id="status", classes="status")
        yield Footer()

    def on_mount(self) -> None:
        self.set_title(f"{self.tool.title}: order the files")
        self.redraw(0)
        self.query_one("#order", OptionList).focus()

    def redraw(self, highlight: int) -> None:
        listing = self.query_one("#order", OptionList)
        listing.clear_options()
        listing.add_options(
            [
                Option(Text(f"{i + 1:>3}. {path.name}", no_wrap=True, overflow="ellipsis"))
                for i, path in enumerate(self.files)
            ]
        )
        if self.files:
            listing.highlighted = max(0, min(highlight, len(self.files) - 1))
        self.query_one("#status", Static).update(f"{len(self.files)} files")

    def _index(self) -> int | None:
        return self.query_one("#order", OptionList).highlighted

    def action_move_up(self) -> None:
        i = self._index()
        if i is not None and i > 0:
            self.files[i - 1], self.files[i] = self.files[i], self.files[i - 1]
            self.redraw(i - 1)

    def action_move_down(self) -> None:
        i = self._index()
        if i is not None and i < len(self.files) - 1:
            self.files[i + 1], self.files[i] = self.files[i], self.files[i + 1]
            self.redraw(i + 1)

    def action_remove(self) -> None:
        i = self._index()
        if i is not None and len(self.files) > self.tool.min_files:
            del self.files[i]
            self.redraw(i)
        else:
            self.query_one("#status", Static).update(
                Text(
                    f"[error] {self.tool.title} needs at least {self.tool.min_files} file(s).",
                    style="bold #FF7AA2",
                )
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter in the list (OptionList turns it into a selection) means: go on."""
        self.action_next()

    def action_next(self) -> None:
        from pdfzi.tui.screens.options import OptionsScreen

        self.app.push_screen(OptionsScreen(self.tool, self.files))
