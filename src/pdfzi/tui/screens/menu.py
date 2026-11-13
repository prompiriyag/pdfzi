from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from pdfzi.tui.screens.base import BaseScreen, TitleBar
from pdfzi.tui.screens.picker import PickerScreen
from pdfzi.tui.tools import TOOLS
from pdfzi.tui.widgets import Logo, MenuList


class MenuScreen(BaseScreen):
    """The tool list. There is nothing to go back to, so ``esc`` is hidden here."""

    BINDINGS = [Binding("escape", "back", "Back", show=False)]

    def compose(self) -> ComposeResult:
        yield TitleBar()
        yield Logo()
        yield Static(
            "Local PDF toolkit  ·  your files never leave this computer",
            id="tagline",
        )
        options = []
        for tool in TOOLS:
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"{tool.title:<16}", style="bold")
            row.append(tool.blurb)
            options.append(Option(row, id=tool.id))
        yield MenuList(*options, id="tools")
        yield Footer()

    def on_mount(self) -> None:
        self.set_title("choose a tool")
        self.query_one("#tools", OptionList).focus()

    def action_back(self) -> None:
        """Nothing is below the menu."""

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        tool = TOOLS[event.option_index]
        self.app.push_screen(PickerScreen(tool, self.app.start_dir))
