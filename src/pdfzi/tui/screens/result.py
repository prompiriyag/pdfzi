"""What happened: one block per file; every status line starts with [ok], [warn] or [error]."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from pdfzi.core.batch import BatchEntry
from pdfzi.core.fmt import format_size, plural
from pdfzi.core.textsafe import printable
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
    def files_done(self) -> int:
        """Input files handled: a merge of three files is one job but three files."""
        return sum(len(e.result.sources) or 1 for e in self.entries if e.result is not None)

    @property
    def failed(self) -> int:
        return sum(1 for e in self.entries if not e.ok and not _is_cancel(e.error))


_CANCELLED = "Cancelled."


def _is_cancel(error: str | None) -> bool:
    # A cancelled split or pdf2img may add which finished files it kept.
    return error is not None and error.startswith(_CANCELLED)


def headline(outcome: Outcome) -> Text:
    if outcome.fatal:
        return Text(printable(f"[error] {outcome.fatal}"), style=ERR_STYLE)
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
    return Text(f"[ok] Done: {plural(outcome.files_done, 'file')} processed.", style=OK_STYLE)


class Report:
    """The result text: lines with a prefix (a status word, an indent, a bullet) and a body
    that wraps under itself, not under the prefix. Outside text in it (file names, notes,
    PDF metadata) is made printable."""

    def __init__(self) -> None:
        self.lines: list[tuple[Text, Text]] = []

    def add(self, prefix: str | Text, body: str | Text = "") -> None:
        if isinstance(body, str):
            body = Text(printable(body, multiline=True))
        else:
            body = Text(printable(body.plain, multiline=True), style=body.style)
        self.lines.append((Text(prefix) if isinstance(prefix, str) else prefix, body))

    @property
    def plain(self) -> str:
        return "\n".join(prefix.plain + body.plain for prefix, body in self.lines)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for prefix, body in self.lines:
            if not prefix.plain:
                yield body
                continue
            row = Table.grid(expand=True)
            row.add_column(width=prefix.cell_len, no_wrap=True)
            row.add_column(ratio=1)
            row.add_row(prefix, body)
            yield row


def _sizes(result) -> str | None:
    """Before/after only where the size is the point (compress); elsewhere the new size."""
    if result.command == "compress":
        return result.size_summary
    if result.bytes_out is None:
        return None
    return format_size(result.bytes_out) + (" in total" if len(result.outputs) > 1 else "")


def describe(tool: Tool, outcome: Outcome) -> Report:
    out = Report()
    for entry in outcome.entries:
        result = entry.result
        if result is None:
            if _is_cancel(entry.error):
                detail = (entry.error or "").removeprefix(_CANCELLED).strip()
                out.add(
                    Text("[warn] Cancelled  ", style=WARN_STYLE),
                    entry.source.name + (f"  {detail}" if detail else ""),
                )
            else:
                message = entry.error or ""
                prefix = f"{entry.source.name}:"
                out.add(
                    Text("[error]  ", style=ERR_STYLE),
                    message if message.startswith(prefix) else f"{prefix} {message}",
                )
            continue
        if result.command == "info":
            for line in result.notes:
                out.add("", line)
            out.add("")
            continue
        # merge and images to PDF make one file of several
        name = plural(len(result.sources), "file") if len(result.sources) > 1 else None
        name = name or entry.source.name
        if result.outputs:
            made = (
                result.outputs[0].name
                if len(result.outputs) == 1
                else plural(len(result.outputs), "file")
            )
            out.add(Text("[ok]  ", style=OK_STYLE), f"{name} → {made}")
        else:
            out.add(Text("[ok]  ", style=OK_STYLE), f"{name}: left unchanged")
        details = []
        if result.pages_out is not None:
            details.append(plural(result.pages_out, "page"))
        if sizes := _sizes(result):
            details.append(sizes)
        if details:
            out.add("    ", " · ".join(details))
        for note in result.notes:
            out.add("    • ", note)
        for path in result.outputs[:1]:
            out.add("    ", Text(f"saved in {path.parent}", style="dim"))
    if outcome.bug:
        out.add("")
        out.add("", "Please report this bug. Run `pdfzi` with PDFZI_DEBUG=1 for details.")
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
