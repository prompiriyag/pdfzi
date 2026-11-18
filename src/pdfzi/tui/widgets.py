"""Small widgets shared by the screens: a keyboard-friendly selector, a tick box, the logo."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.content import Content
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, OptionList, Static

ACCENT, PRIMARY = "#F87171", "#DC2626"

# "ANSI Shadow" lettering, red fading to dark red from top to bottom.
LOGO_BIG = (
    "██████╗ ██████╗ ███████╗███████╗██╗",
    "██╔══██╗██╔══██╗██╔════╝╚══███╔╝██║",
    "██████╔╝██║  ██║█████╗    ███╔╝ ██║",
    "██╔═══╝ ██║  ██║██╔══╝   ███╔╝  ██║",
    "██║     ██████╔╝██║     ███████╗██║",
    "╚═╝     ╚═════╝ ╚═╝     ╚══════╝╚═╝",
)
LOGO_SMALL = (
    "╔═╗╔╦╗╔═╗╔═╗╦",
    "╠═╝ ║║╠╣ ╔═╝║",
    "╩  ═╩╝╚  ╚═╝╩",
)
_FADE = ("#FCA5A5", "#F87171", "#EF4444", "#DC2626", "#B91C1C", "#991B1B")


class Logo(Static):
    """``pdfzi`` in big letters. Picks the largest version that fits the terminal height."""

    def __init__(self) -> None:
        super().__init__("", id="logo")

    def on_mount(self) -> None:
        self.fit(self.app.size.height)

    def on_resize(self) -> None:
        self.fit(self.app.size.height)

    def fit(self, rows: int) -> None:
        if rows >= 24:
            lines, colours = LOGO_BIG, _FADE
        elif rows >= 16:
            lines, colours = LOGO_SMALL, (_FADE[1], _FADE[3], _FADE[5])
        else:
            lines, colours = (), ()
        text = Text(no_wrap=True)
        for i, (line, colour) in enumerate(zip(lines, colours, strict=True)):
            text.append(line + ("\n" if i < len(lines) - 1 else ""), style=f"bold {colour}")
        self.update(text)
        self.display = bool(lines)
        self.styles.height = len(lines)


class Selector(Static, can_focus=True):
    """A row of choices. ``left``/``right`` change it; ``up``/``down`` leave it (the screen
    moves the focus); ``space`` goes to the next choice; a click picks one.

    Rendered as ``(●) lossless  ( ) low`` so the state is readable without colour.
    """

    BINDINGS = [
        Binding("left", "previous", "Change", key_display="←→"),
        Binding("right", "next", "Next", show=False),
        Binding("space", "cycle", "Next choice", show=False),
        Binding("enter", "screen.next_field", "Next"),  # enter never changes a choice
    ]

    class Changed(Message):
        """The chosen option changed (by keys or by a click)."""

        def __init__(self, selector: Selector, value: str) -> None:
            super().__init__()
            self.selector = selector
            self.value = value

    def __init__(self, choices: tuple[str, ...], default: str | None = None, *, id: str) -> None:
        super().__init__("", id=id)
        self.choices = choices
        self.index = choices.index(default) if default in choices else 0
        self._draw()

    @property
    def value(self) -> str:
        return self.choices[self.index]

    def select(self, value: str) -> None:
        self.index = self.choices.index(value)
        self._draw()

    def _spans(self) -> list[tuple[int, int]]:
        spans, start = [], 0
        for choice in self.choices:
            width = len(f"(●) {choice}")
            spans.append((start, start + width))
            start += width + 2
        return spans

    def _draw(self) -> None:
        text = Text(no_wrap=True)
        for i, choice in enumerate(self.choices):
            chosen = i == self.index
            style = "bold white on #DC2626" if chosen and self.has_focus else "bold #F87171"
            text.append(f"({'●' if chosen else ' '}) {choice}", style=style if chosen else "")
            if i < len(self.choices) - 1:
                text.append("  ")
        self.update(text)

    def on_focus(self) -> None:
        self._draw()

    def on_blur(self) -> None:
        self._draw()

    def _move(self, step: int) -> None:
        self._set((self.index + step) % len(self.choices))

    def _set(self, index: int) -> None:
        changed = index != self.index
        self.index = index
        self._draw()
        if changed:
            self.post_message(self.Changed(self, self.value))

    def action_previous(self) -> None:
        self._move(-1)

    def action_next(self) -> None:
        self._move(1)

    def action_cycle(self) -> None:
        self._move(1)

    def on_click(self, event) -> None:
        for i, (start, end) in enumerate(self._spans()):
            if start <= event.x < end:
                self._set(i)
                break


class Tick(Checkbox):
    """Checkbox that shows [x] / [ ] instead of relying on colour.

    Only ``space`` ticks it (Textual's box also toggles on ``enter``); ``enter`` moves to
    the next field, like in a text box.
    """

    BINDINGS = [
        Binding("space", "toggle_button", "Tick"),
        Binding("enter", "screen.next_field", "Next"),
    ]

    @property
    def _button(self) -> Content:
        glyph = "[x]" if self.value else "[ ]"
        return Content.assemble((glyph, self.get_visual_style("toggle--button")))


class FieldInput(Input):
    """A text box of the settings form. ``enter`` moves to the next field (shown in the footer)."""

    BINDINGS = [Binding("enter", "screen.next_field", "Next")]


class StartButton(Button):
    """The Start button; the footer says what ``enter`` does on it."""

    BINDINGS = [Binding("enter", "press", "Run")]


class BarButton(Button, can_focus=False):
    """A button in the row under a list, for the mouse. It never takes the focus, so the
    keyboard stays on the list (where the same actions have keys)."""


class MenuList(OptionList):
    """A list whose keys are named in the footer: up/down move, enter opens."""

    BINDINGS = [
        Binding("down", "cursor_down", "Move", key_display="↑↓"),
        Binding("up", "cursor_up", "Move", show=False),
        Binding("enter", "select", "Open"),
    ]


class FileList(MenuList):
    """The file list: enter opens a folder, or goes on with the ticked files.

    A click is not enter: it ticks a file (or opens a folder), so the mouse can pick
    several files.
    """

    BINDINGS = [
        Binding("down", "cursor_down", "Move", show=False),  # the footer is full already
        Binding("enter", "select", "Continue"),
    ]

    class Clicked(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    async def _on_click(self, event) -> None:
        event.prevent_default()  # not OptionList's own click, which would select (= enter)
        index = event.style.meta.get("option")
        if index is not None:
            self.highlighted = index
            self.post_message(self.Clicked(index))


class OrderList(MenuList):
    """The order list: enter goes on to the settings; a click only picks a file to move."""

    BINDINGS = [
        Binding("down", "cursor_down", "Move", show=False),
        Binding("enter", "select", "Continue"),
    ]

    async def _on_click(self, event) -> None:
        event.prevent_default()
        index = event.style.meta.get("option")
        if index is not None:
            self.highlighted = index
