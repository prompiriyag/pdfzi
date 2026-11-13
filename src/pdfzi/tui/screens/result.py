"""What happened: one block per file; every status line starts with [ok], [warn] or [error]."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from pdfzi.core.batch import BatchEntry
from pdfzi.tui.screens.base import BaseScreen, TitleBar
from pdfzi.tui.tools import Tool

OK_STYLE, WARN_STYLE, ERR_STYLE = "bold #4ADE80", "bold #FBBF24", "bold #FF7AA2"


@dataclass
class Outcome:
    entries: list[BatchEntry] = field(default_factory=list)
    cancelled: bool = False
    fatal: str | None = None
    bug: bool = False
    seconds: float = 0.0

    @property
    def succeeded(self) -> int:
        return sum(1 for e in self.entries if e.ok)

    @property
    def failed(self) -> int:
        return sum(1 for e in self.entries if not e.ok and e.error != "Cancelled.")


def headline(outcome: Outcome) -> Text:
    if outcome.fatal:
        return Text(f"[error] {outcome.fatal}", style=ERR_STYLE)
    if outcome.cancelled:
        return Text(
            f"[warn] Cancelled. {outcome.succeeded} finished before you stopped it.",
            style=WARN_STYLE,
        )
    if outcome.failed:
        return Text(
            f"[warn] Finished with problems: {outcome.succeeded} succeeded, "
            f"{outcome.failed} failed.",
            style=WARN_STYLE,
        )
    word = "file" if outcome.succeeded == 1 else "files"
    return Text(f"[ok] Done: {outcome.succeeded} {word} processed.", style=OK_STYLE)


def describe(tool: Tool, outcome: Outcome) -> Text:
    out = Text()
    for entry in outcome.entries:
        result = entry.result
        if result is None:
            if entry.error == "Cancelled.":
                out.append("[warn] Cancelled  ", style=WARN_STYLE)
                out.append(f"{entry.source.name}\n")
            else:
                out.append("[error]  ", style=ERR_STYLE)
                message = entry.error or ""
                prefix = f"{entry.source.name}:"
                out.append(message if message.startswith(prefix) else f"{prefix} {message}")
                out.append("\n")
            continue
        if result.command == "info":
            out.append("\n".join(result.notes) + "\n\n")
            continue
        out.append("[ok]  ", style=OK_STYLE)
        if result.outputs:
            names = (
                result.outputs[0].name
                if len(result.outputs) == 1
                else f"{len(result.outputs)} files"
            )
            out.append(f"{entry.source.name} → {names}\n")
        else:
            out.append(f"{entry.source.name}: left unchanged\n")
        details = []
        if result.pages_out is not None:
            details.append(f"{result.pages_out} page{'s' if result.pages_out != 1 else ''}")
        if result.size_summary:
            details.append(result.size_summary)
        if details:
            out.append("    " + " · ".join(details) + "\n")
        for note in result.notes:
            out.append(f"    • {note}\n")
        for path in result.outputs[:1]:
            out.append(f"    saved in {path.parent}\n", style="dim")
    if outcome.bug:
        out.append("\nPlease report this bug. Run `pdfzi` with PDFZI_DEBUG=1 for details.\n")
    return out


class ResultScreen(BaseScreen):
    BINDINGS = [
        Binding("down", "scroll_down", "Scroll", key_display="↑↓ PgUp PgDn"),
        Binding("up", "scroll_up", "Scroll", show=False),
        Binding("pagedown", "scroll_page_down", "Scroll", show=False),
        Binding("pageup", "scroll_page_up", "Scroll", show=False),
        Binding("home", "scroll_home", "Scroll", show=False),
        Binding("end", "scroll_end", "Scroll", show=False),
        Binding("enter", "menu", "Main menu"),
        Binding("m", "menu", "Main menu", show=False),
    ]

    def __init__(self, tool: Tool, files: list[Path], outcome: Outcome) -> None:
        super().__init__()
        self.tool = tool
        self.files = files
        self.outcome = outcome

    def compose(self) -> ComposeResult:
        yield TitleBar()
        yield Static(headline(self.outcome), id="headline", classes="status")
        with VerticalScroll(can_focus=False):  # the screen's own keys scroll it (and show up below)
            yield Static(describe(self.tool, self.outcome), id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.set_title(f"{self.tool.title}: result")
        # The scroll keys appear as soon as the layout shows the report is taller than the
        # window (and go away if a bigger window makes it fit).
        self.watch(self._scroller(), "virtual_size", lambda _: self.refresh_bindings())
        self.call_after_refresh(self.refresh_bindings)

    def on_resize(self) -> None:
        self.call_after_refresh(self.refresh_bindings)

    def _scroller(self) -> VerticalScroll:
        return self.query_one(VerticalScroll)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """The scroll keys exist (and are listed in the footer) only for a long report."""
        if action.startswith("scroll_"):
            return self._scroller().max_scroll_y > 0
        return True

    def action_scroll_down(self) -> None:
        self._scroller().scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self._scroller().scroll_up(animate=False)

    def action_scroll_page_down(self) -> None:
        self._scroller().scroll_page_down(animate=False)

    def action_scroll_page_up(self) -> None:
        self._scroller().scroll_page_up(animate=False)

    def action_scroll_home(self) -> None:
        self._scroller().scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self._scroller().scroll_end(animate=False)

    def action_menu(self) -> None:
        self.app.pop_to_menu()
