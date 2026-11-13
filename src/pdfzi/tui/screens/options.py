"""Per-tool settings form. Everything works with the keyboard alone:

``up``/``down`` (or ``tab``/``shift+tab``) move between fields, ``left``/``right`` change a
choice or ticks, ``enter`` goes to the next field, ``ctrl+r`` starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, Static

from pdfzi.core.errors import PdfziError
from pdfzi.tui.screens.base import BaseScreen, TitleBar
from pdfzi.tui.screens.run import RunScreen
from pdfzi.tui.tools import Field, Tool
from pdfzi.tui.widgets import FieldInput, Selector, StartButton, Tick

FIELD_TYPES = (Input, Selector, Tick)


class OptionsScreen(BaseScreen):
    BINDINGS = [
        Binding("ctrl+r", "start", "Start"),
        # priority: the scrolling form around the fields has its own arrow bindings, which
        # would otherwise scroll instead of moving the focus whenever the form is taller
        # than the window
        Binding("up", "field_up", "Field", key_display="↑↓", priority=True),
        Binding("down", "field_down", "Next field", show=False, priority=True),
    ]

    def __init__(self, tool: Tool, files: list[Path]) -> None:
        super().__init__()
        self.tool = tool
        self.files = files

    def compose(self) -> ComposeResult:
        yield TitleBar()
        names = ", ".join(p.name for p in self.files[:3]) + (" ..." if len(self.files) > 3 else "")
        count = f"{len(self.files)} file{'s' if len(self.files) != 1 else ''}"
        yield Static(
            Text(f"{self.tool.title}: {count}", no_wrap=True, overflow="ellipsis"), classes="title"
        )
        yield Static(Text(names, no_wrap=True, overflow="ellipsis"), classes="hint")
        with VerticalScroll(id="form"):
            for field in self.tool.fields:
                with Horizontal(classes="row"):
                    yield Label(field.label, classes="label")
                    if field.kind == "size":
                        # a number to type and its unit to pick: no unit to remember or misspell
                        yield FieldInput(
                            placeholder="e.g. 2", type="number", id=f"f-{field.key}",
                            classes="size-input",
                        )  # fmt: skip
                        yield Selector(field.choices, field.choices[0], id=f"f-{field.key}-unit")
                    else:
                        yield self._widget(field)
                    if field.details:  # a short note beside the options, for the chosen one
                        yield Static("", id=f"note-{field.key}", classes="note")
        yield Static("", id="field-hint", classes="hint")
        yield Static("", id="error", classes="status")
        yield StartButton("Start", id="start")
        yield Footer()

    def _widget(self, field: Field):
        wid = f"f-{field.key}"
        if field.kind == "choice":
            return Selector(field.choices, field.default, id=wid)
        if field.kind == "bool":
            return Tick("yes", value=bool(field.default), id=wid)
        return FieldInput(
            value=str(field.default or ""),
            password=field.kind == "password",
            type="integer" if field.kind == "int" else "text",
            id=wid,
        )

    def on_mount(self) -> None:
        self.set_title(f"{self.tool.title}: settings")
        self.refresh_notes()
        fields = self.query("#form Input, #form Selector, #form Checkbox")
        if fields:
            fields.first().focus()
        else:
            self.query_one("#start", StartButton).focus()

    # ---------------------------------------------------------------- values

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field in self.tool.fields:
            widget = self.query_one(f"#f-{field.key}")
            if field.kind == "size":
                number = widget.value.strip()
                unit = self.query_one(f"#f-{field.key}-unit", Selector).value
                result[field.key] = f"{number}{unit}" if number else ""
            else:
                result[field.key] = widget.value
        return result

    def show_error(self, message: str) -> None:
        self.query_one("#error", Static).update(Text(f"[error] {message}", style="bold #FF7AA2"))

    def refresh_notes(self) -> None:
        """Beside each choice field: a few dim words about the option that is selected."""
        for field in self.tool.fields:
            if field.details:
                choice = self.query_one(f"#f-{field.key}", Selector).value
                short = next(s for c, s, _ in field.details if c == choice)
                self.query_one(f"#note-{field.key}", Static).update(Text(short))

    def on_selector_changed(self, event: Selector.Changed) -> None:
        self.refresh_notes()
        self.update_help(event.selector)  # the sentence under the form follows the choice

    def on_descendant_focus(self, event) -> None:
        self.update_help(event.widget)

    def update_help(self, widget) -> None:
        """The line under the form: what the focused field is (the keys are in the footer)."""
        wid = (getattr(widget, "id", None) or "").removesuffix("-unit")
        hint = ""
        for field in self.tool.fields:
            if wid == f"f-{field.key}":
                hint = field.hint
                if field.details and isinstance(widget, Selector) and widget.id == wid:
                    hint = next(long for c, _, long in field.details if c == widget.value)
                break
        self.query_one("#field-hint", Static).update(hint)

    # ---------------------------------------------------------------- moving around

    def action_field_up(self) -> None:
        self.focus_previous()

    def action_field_down(self) -> None:
        self.focus_next()

    def action_next_field(self) -> None:
        self.focus_next()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.focus_next()  # enter: on to the next field (the last one leads to Start)

    # ---------------------------------------------------------------- go

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.action_start()

    def action_start(self) -> None:
        self.query_one("#error", Static).update("")
        try:
            runner = self.tool.build(self.values())
        except PdfziError as exc:
            self.show_error(str(exc))
            return
        self.app.push_screen(RunScreen(self.tool, self.files, runner))
