"""Helpers for driving the TUI with Textual's Pilot (no extra pytest plugin needed)."""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from pdfzi.tui.app import PdfziApp
from pdfzi.tui.screens import MenuScreen, ResultScreen
from pdfzi.tui.tools import TOOLS

SIZE = (80, 24)
_TEXT = re.compile(r'<text class="[^"]*" x="([\d.]+)" y="([\d.]+)"[^>]*>(.*?)</text>', re.S)


def screen_text(app: PdfziApp) -> str:
    """The visible screen as plain text, one line per terminal row (from the SVG export)."""
    svg = app.export_screenshot()
    rows: dict[float, list[tuple[float, str]]] = {}
    for x, y, content in _TEXT.findall(svg):
        rows.setdefault(float(y), []).append(
            (float(x), html.unescape(content).replace("\xa0", " "))
        )
    lines = []
    for y in sorted(rows):
        line = ""
        for x, text in sorted(rows[y]):
            col = round(x / 12.2)
            line = line.ljust(col) + text
        lines.append(line.rstrip())
    return "\n".join(lines)


def run_tui(scenario: Callable[..., Awaitable[None]], start_dir: Path, size=SIZE) -> None:
    async def main() -> None:
        app = PdfziApp(start_dir)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.2)  # let the footer finish composing its key list
            await scenario(app, pilot)

    asyncio.run(main())


async def open_tool(pilot, tool_id: str) -> None:
    """From the menu, move to the tool and press enter."""
    ids = [t.id for t in TOOLS]
    assert isinstance(pilot.app.screen, MenuScreen)
    await pilot.press("home")  # the menu remembers its last highlight
    for _ in range(ids.index(tool_id)):
        await pilot.press("down")
    await pilot.press("enter")
    await pilot.pause()


async def tick(pilot, names: list[str]) -> None:
    """In the file picker, tick the files with these names (searching by name first)."""
    screen = pilot.app.screen
    screen.query_one("#files").focus()  # the picker opens on its path bar; space needs the list
    for name in names:
        index = next(i for i, r in enumerate(screen.rows) if r.path.name == name)
        screen.query_one("#files").highlighted = index
        await pilot.pause()
        await pilot.press("space")


async def wait_for_result(pilot, timeout: float = 30.0) -> ResultScreen:
    waited = 0.0
    while not isinstance(pilot.app.screen, ResultScreen):
        await pilot.pause(0.05)
        waited += 0.05
        assert waited < timeout, "job did not finish; screen:\n" + screen_text(pilot.app)
    await pilot.pause(0.1)  # let the new screen paint before anyone reads it
    return pilot.app.screen
