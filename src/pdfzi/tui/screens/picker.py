"""File chooser: paste a path, or browse folders and tick files; filter by name."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from pdfzi.core.fmt import format_size
from pdfzi.core.paths import IMAGE_SUFFIXES, PDF_SUFFIXES, natural_key
from pdfzi.core.textsafe import printable
from pdfzi.tui.screens.base import BaseScreen, TitleBar
from pdfzi.tui.tools import Tool
from pdfzi.tui.widgets import BarButton, FileList


@dataclass(frozen=True)
class Row:
    kind: str  # "up" | "dir" | "file"
    path: Path
    size: int = 0


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def parse_one_path(text: str) -> tuple[Path | None, str | None]:
    """The single path in pasted/typed text: ``(path, None)``, or ``(None, problem)``.

    Quotes around the path are dropped (dropping a file on a terminal and Explorer's
    "Copy as path" both add them). Empty text gives ``(None, None)``: nothing to do.
    """
    lines = [raw.strip() for raw in text.splitlines() if raw.strip()]
    if not lines:
        return None, None
    line = lines[0]
    if len(line) > 1 and line[0] == line[-1] and line[0] in "\"'":
        line = line[1:-1].strip()
    if len(lines) > 1 or '"' in line:
        return None, "paste one file path at a time."
    return Path(line).expanduser(), None


class PathInput(Input):
    """The path bar. A paste replaces its whole content; Enter then acts on it."""

    # ctrl+v: Textual's own binding pastes only text copied inside the app; read the real one
    BINDINGS = [
        Binding("ctrl+v", "screen.paste_clipboard", "Paste"),
        Binding("enter", "submit", "Go"),
    ]

    class Pasted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def _on_paste(self, event: events.Paste) -> None:
        event.prevent_default()
        event.stop()
        self.post_message(self.Pasted(event.text))


class PickerScreen(BaseScreen):
    BINDINGS = [
        Binding("space", "toggle", "Tick"),
        Binding("slash", "search", "Filter"),
        Binding("ctrl+v", "paste_clipboard", "Paste"),
        Binding("ctrl+l", "goto_path", "Path"),
        Binding("ctrl+n", "next", "Next", show=False),
        Binding("ctrl+a", "select_all", "Tick all", show=False),
        Binding("backspace", "up_dir", "Parent folder", show=False),
    ]

    def __init__(self, tool: Tool, start_dir: Path) -> None:
        super().__init__()
        self.tool = tool
        self.cwd = start_dir
        self.filter = ""
        self.selected: list[Path] = []
        self.rows: list[Row] = []
        self.suffixes = PDF_SUFFIXES if tool.accepts == "pdf" else IMAGE_SUFFIXES

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield TitleBar()
        with Horizontal(id="pathrow"):
            yield Label("Path ▸")
            yield PathInput(
                id="pathbar", placeholder="paste a folder or file path here, then press Enter"
            )
        yield Input(id="query")
        yield FileList(id="files")
        with Horizontal(classes="bar"):
            yield Static("", id="status", classes="status")
            yield BarButton("Continue", id="continue")
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """In a text box those keys are just letters: do not list them in the footer."""
        letters = {"toggle", "search", "select_all", "up_dir"}
        return not (isinstance(self.focused, Input) and action in letters)

    def on_mount(self) -> None:
        kind = "PDF files" if self.tool.accepts == "pdf" else "images (JPG, PNG)"
        need = f", at least {self.tool.min_files}" if self.tool.min_files > 1 else ""
        self.set_title(f"{self.tool.title}: choose {kind}{need}")
        self.query_one("#query", Input).display = False
        self.load_folder(self.cwd)
        self.action_goto_path()  # the path bar comes first: paste or type, or press down

    # ---------------------------------------------------------------- folder contents

    def _scan(self, folder: Path) -> list[Row]:
        rows: list[Row] = []
        if folder.parent != folder:
            rows.append(Row("up", folder.parent))
        dirs: list[Path] = []
        files: list[Row] = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        dirs.append(Path(entry.path))
                    elif entry.is_file() and Path(entry.name).suffix.lower() in self.suffixes:
                        files.append(Row("file", Path(entry.path), entry.stat().st_size))
                except OSError:
                    continue  # unreadable entry: skip it
        rows += [Row("dir", d) for d in sorted(dirs, key=natural_key)]
        rows += sorted(files, key=lambda r: natural_key(r.path))
        return rows

    def load_folder(self, folder: Path, highlight: Path | None = None) -> bool:
        """Show ``folder``. Returns False (and says why) if it cannot be read."""
        try:
            rows = self._scan(folder)
        except OSError as exc:
            self.say(
                f"[error] cannot open {folder.name or folder}: {exc.strerror or exc}", error=True
            )
            return False
        self.cwd = folder
        self.filter = ""
        self._all_rows = rows
        self.query_one("#pathbar", Input).value = str(folder)
        self._hide_filter()
        self.refresh_list(highlight)
        return True

    def visible_rows(self) -> list[Row]:
        needle = self.filter.casefold()
        return [r for r in self._all_rows if r.kind == "up" or needle in r.path.name.casefold()]

    # ---------------------------------------------------------------- drawing

    def row_text(self, row: Row, ticked: set[str] | None = None) -> Text:
        """The line for ``row``. ``ticked`` (keys of the selected files) can be passed when
        drawing many rows, so it is worked out once instead of once per row."""
        text = Text(no_wrap=True, overflow="ellipsis")
        if row.kind == "up":
            text.append("↑ .. (parent folder)", style="italic")
        elif row.kind == "dir":
            text.append(f"▸ {printable(row.path.name)}/", style="bold")
        else:
            if ticked is None:
                ticked = {_key(p) for p in self.selected}
            mark = "[x]" if _key(row.path) in ticked else "[ ]"
            text.append(f"{mark} {printable(row.path.name)}")
            text.append(f"  {format_size(row.size)}", style="dim")
        return text

    def refresh_list(self, highlight: Path | None = None) -> None:
        self.rows = self.visible_rows()
        listing = self.query_one("#files", OptionList)
        listing.clear_options()
        ticked = {_key(p) for p in self.selected}
        listing.add_options([Option(self.row_text(r, ticked)) for r in self.rows])
        index = 0
        if highlight is not None:
            for i, row in enumerate(self.rows):
                if _key(row.path) == _key(highlight):
                    index = i
                    break
        elif self.rows and self.rows[0].kind == "up" and len(self.rows) > 1:
            index = 1  # start on the first real entry, not on ".."
        if self.rows:
            listing.highlighted = index
        self.say()

    def say(self, message: str | None = None, *, error: bool = False) -> None:
        count = len(self.selected)
        if message is None:
            message = f"[ok] {count} selected" if count else "Nothing ticked yet"
            if not any(r.kind != "up" for r in self.rows):
                message += "   (no matching files or folders here)"
        status = self.query_one("#status", Static)
        status.update(Text(printable(message), style="bold #FF7AA2" if error else ""))

    # ---------------------------------------------------------------- actions

    def highlighted_row(self) -> Row | None:
        index = self.query_one("#files", OptionList).highlighted
        return self.rows[index] if index is not None and 0 <= index < len(self.rows) else None

    def toggle(self, row: Row) -> None:
        keys = [_key(p) for p in self.selected]
        if _key(row.path) in keys:
            del self.selected[keys.index(_key(row.path))]
        else:
            self.selected.append(row.path)
        listing = self.query_one("#files", OptionList)
        index = self.rows.index(row)
        listing.replace_option_prompt_at_index(index, self.row_text(row))
        self.say()

    def action_toggle(self) -> None:
        row = self.highlighted_row()
        if row is not None and row.kind == "file":
            self.toggle(row)

    def action_select_all(self) -> None:
        keys = {_key(p) for p in self.selected}
        for row in self.rows:
            if row.kind == "file" and _key(row.path) not in keys:
                self.selected.append(row.path)
        self.refresh_list(self.highlighted_row().path if self.highlighted_row() else None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        row = self.rows[event.option_index]
        if row.kind == "file":
            self.proceed(row)
        else:
            self.open_row(row)

    def on_file_list_clicked(self, event: FileList.Clicked) -> None:
        """A click ticks a file (enter would go on) and opens a folder, like enter."""
        row = self.rows[event.index]
        if row.kind == "file":
            self.toggle(row)
        else:
            self.open_row(row)

    def open_row(self, row: Row) -> None:
        if row.kind == "up":
            self.load_folder(row.path, highlight=self.cwd)
        else:
            self.load_folder(row.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue":
            self.action_next()

    def proceed(self, row: Row) -> None:
        """Enter on a file never ticks it (that is space). It goes on with the ticked files;
        with nothing ticked, a tool that works on one file uses the highlighted one."""
        if self.selected or self.tool.min_files > 1:
            self.action_next()
        else:
            self.use_file(row.path)

    def action_up_dir(self) -> None:
        if not isinstance(self.focused, Input) and self.cwd.parent != self.cwd:
            self.load_folder(self.cwd.parent, highlight=self.cwd)

    # ---------------------------------------------------------------- filter box

    def _hide_filter(self) -> None:
        box = self.query_one("#query", Input)
        if box.display:
            box.display = False
            box.value = ""
        if isinstance(self.focused, Input):
            self.query_one("#files", OptionList).focus()

    def action_search(self) -> None:
        box = self.query_one("#query", Input)
        box.placeholder = "type part of a file or folder name"
        box.display = True
        box.value = self.filter
        box.focus()
        box.cursor_position = len(self.filter)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "query":
            self.filter = event.value
            self.refresh_list()

    # ---------------------------------------------------------------- path bar

    def action_goto_path(self) -> None:
        bar = self.query_one("#pathbar", Input)
        bar.focus()
        bar.select_all()  # typing or pasting replaces the folder shown; right arrow keeps it

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query":
            self.query_one("#files", OptionList).focus()
        else:
            self.apply_path_text(event.value)

    def on_path_input_pasted(self, event: PathInput.Pasted) -> None:
        self.accept_paste(event.text)

    def on_paste(self, event: events.Paste) -> None:
        """Pasting while the list is focused goes to the path bar, so no click is needed."""
        if not isinstance(self.focused, Input):
            event.stop()
            self.accept_paste(event.text)

    def accept_paste(self, text: str) -> None:
        """Put a pasted path in the bar (one path only) and wait for Enter."""
        path, problem = parse_one_path(text)
        if problem:
            self.say(f"[error] {problem}", error=True)
        elif path is not None:
            bar = self.query_one("#pathbar", Input)
            bar.value = str(path)
            bar.focus()
            bar.cursor_position = len(bar.value)
            self.say("Press Enter to use this file.")

    def apply_path_text(self, text: str) -> None:
        """Enter in the path bar: open a folder, or go on with the one file it names."""
        path, problem = parse_one_path(text)
        if problem:
            self.say(f"[error] {problem}", error=True)
        elif path is None:
            return
        elif path.is_dir():
            if self.load_folder(path.resolve()):
                self.query_one("#files", OptionList).focus()
        elif not path.is_file():
            self.say(f"[error] File not found: {path}", error=True)
        elif path.suffix.lower() not in self.suffixes:
            kind = "PDF" if self.tool.accepts == "pdf" else "JPG or PNG"
            self.say(f"[error] {path.name} is not a {kind} file.", error=True)
        else:
            self.use_file(path.resolve())

    def use_file(self, path: Path) -> None:
        """One file from the path bar. A tool that works on one file goes straight to its
        settings; one that needs several (merge, images to PDF) just ticks it."""
        self.load_folder(path.parent, highlight=path)
        if self.tool.min_files <= 1:
            from pdfzi.tui.screens.options import OptionsScreen

            self.app.push_screen(OptionsScreen(self.tool, [path]))
            return
        if all(_key(p) != _key(path) for p in self.selected):
            self.selected.append(path)
            self.refresh_list(path)
        self.say(f"[ok] {len(self.selected)} selected. Paste another, or press ctrl+n when done.")
        self.query_one("#files", OptionList).focus()

    def action_paste_clipboard(self) -> None:
        """ctrl+v: take the path from the system clipboard (copied text, or a file copied
        in Explorer) and put it in the path bar."""
        from pdfzi.tui import clipboard

        text = clipboard.read_text()
        if text is None:
            self.say(
                "[error] cannot read the clipboard here. Paste with your terminal.", error=True
            )
        elif not text.strip():
            self.say("[error] the clipboard has no text or file to paste.", error=True)
        else:
            self.accept_paste(text)

    def on_key(self, event: events.Key) -> None:
        """Arrow keys move between the path bar and the list, so the keyboard never gets
        stuck: down leaves a text box, up from the top of the list reaches the path bar."""
        focused = self.focused
        if event.key == "down" and isinstance(focused, Input):
            event.stop()
            self.query_one("#files", OptionList).focus()
        elif (
            event.key == "up"
            and isinstance(focused, OptionList)
            and focused.id == "files"
            and not focused.highlighted  # None (empty list) or the first row
        ):
            event.stop()
            self.action_goto_path()

    def action_back(self) -> None:
        bar = self.query_one("#pathbar", Input)
        if bar.has_focus and bar.value != str(self.cwd):
            bar.value = str(self.cwd)  # first esc undoes what was typed, the next one goes back
            bar.select_all()
        elif self.filter or self.query_one("#query", Input).display:
            self.filter = ""
            self._hide_filter()
            self.refresh_list()
        else:
            self.app.pop_screen()

    # ---------------------------------------------------------------- moving on

    def action_next(self) -> None:
        if len(self.selected) < self.tool.min_files:
            self.say(
                f"[error] {self.tool.title} needs at least {self.tool.min_files} "
                f"file{'s' if self.tool.min_files != 1 else ''} (ticked: {len(self.selected)}).",
                error=True,
            )
            return
        from pdfzi.tui.screens.options import OptionsScreen
        from pdfzi.tui.screens.order import OrderScreen

        files = list(self.selected)
        if self.tool.ordered and len(files) > 1:  # one file has no order to set
            self.app.push_screen(OrderScreen(self.tool, files))
        else:
            self.app.push_screen(OptionsScreen(self.tool, files))
