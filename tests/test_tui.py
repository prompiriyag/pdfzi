"""TUI tests with Textual's Pilot: the main path of every tool, keyboard handling, 80x24."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pikepdf
import pytest
from textual import events
from textual.widgets import Button, Input, OptionList

from pdfgen import (
    Snapshot,
    extract_text,
    make_corrupt_pdf,
    make_pdf,
    page_count,
    page_text,
    photo_image,
)
from pdfzi.core.progress import CancelToken, report
from pdfzi.core.results import OperationResult
from pdfzi.tui import tools as tools_mod
from pdfzi.tui.screens import (
    HelpScreen,
    MenuScreen,
    OptionsScreen,
    OrderScreen,
    PickerScreen,
    ResultScreen,
    RunScreen,
)
from pdfzi.tui.theme import THEME_NAME
from pdfzi.tui.tools import TOOLS
from pdfzi.tui.widgets import Selector
from tui_helpers import (
    SIZE,
    open_tool,
    run_tui,
    screen_text,
    settled_screen,
    tick,
    wait_for_result,
    wait_until,
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    d = tmp_path / "เอกสาร ทดสอบ"
    d.mkdir()
    make_pdf(d / "alpha.pdf", pages=4, bookmarks=True)
    make_pdf(d / "รายงาน ประจำปี.pdf", pages=3, text_prefix="B-")
    make_pdf(d / "photos.pdf", pages=2, images_per_page=1, image_size=(1800, 1200))
    make_corrupt_pdf(d / "broken.pdf")
    photo_image(300, 200, 1).save(d / "one.png")
    photo_image(200, 300, 2).save(d / "two.jpg", "JPEG")
    (d / "notes.txt").write_text("not a pdf")
    (d / "sub").mkdir()
    make_pdf(d / "sub" / "inner.pdf", pages=2)
    return d


async def set_value(pilot, field: str, value: str) -> None:
    widget = pilot.app.screen.query_one(f"#f-{field}")
    widget.focus()
    widget.value = value
    await pilot.pause()


async def choose(pilot, field: str, label: str) -> None:
    """Pick a radio choice with real keys: focus the set, move right until it is highlighted."""
    selector = pilot.app.screen.query_one(f"#f-{field}", Selector)
    selector.focus()
    for _ in range(selector.choices.index(label)):
        await pilot.press("right")
    await pilot.pause()
    assert selector.value == label


async def go(pilot, tool: str, names: list[str], *, ordered_next=True) -> None:
    await open_tool(pilot, tool)
    assert isinstance(pilot.app.screen, PickerScreen)
    await tick(pilot, names)
    await pilot.press("ctrl+n")
    await pilot.pause()


async def start_and_wait(pilot) -> ResultScreen:
    await pilot.press("ctrl+r")
    return await wait_for_result(pilot)


def result_text(screen: ResultScreen) -> str:
    return screen_text(screen.app)


# ---------------------------------------------------------------- menu, help, theme, size


def test_menu_theme_footer_and_size(folder):
    async def scenario(app, pilot):
        assert app.theme == THEME_NAME and app.size == SIZE
        theme = app.current_theme
        assert (theme.primary, theme.accent, theme.background, theme.surface) == (
            "#DC2626", "#F87171", "#0F0A0A", "#1C1212",
        )  # fmt: skip
        assert (theme.foreground, theme.success, theme.warning) == ("#F5EDED", "#4ADE80", "#FBBF24")
        await wait_until(pilot, lambda: "q Quit" in footer(app))  # slow machines draw later
        text = screen_text(app)
        for tool in TOOLS:
            assert tool.title in text
        last = text.splitlines()[-1]
        assert "q Quit" in last and "? Help" in last
        assert len(text.splitlines()) <= 24

    run_tui(scenario, folder)


def test_help_opens_and_closes_with_the_keyboard(folder):
    async def scenario(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert "Choosing files" in screen_text(app) and len(screen_text(app).splitlines()) <= 24
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)

    run_tui(scenario, folder)


def test_q_quits_from_the_menu_and_esc_does_nothing_there(folder):
    async def scenario(app, pilot):
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen) and not app._exit
        await pilot.press("q")
        await pilot.pause()
        assert app._exit

    run_tui(scenario, folder)


# ---------------------------------------------------------------- file picker


def test_picker_lists_only_matching_files_and_folders(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        text = screen_text(app)
        assert "alpha.pdf" in text and "รายงาน ประจำปี.pdf" in text and "▸ sub/" in text
        assert "notes.txt" not in text and "one.png" not in text
        await pilot.press("escape")
        await open_tool(pilot, "img2pdf")
        text = screen_text(app)
        assert "one.png" in text and "two.jpg" in text and "alpha.pdf" not in text

    run_tui(scenario, folder)


def test_picker_navigation_multi_select_across_folders_and_filter(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "merge")
        picker = app.screen
        await tick(pilot, ["alpha.pdf"])
        # enter the sub folder with the keyboard, tick a file there, come back with backspace
        picker.query_one("#files").highlighted = next(
            i for i, r in enumerate(picker.rows) if r.kind == "dir"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert picker.cwd.name == "sub" and "inner.pdf" in screen_text(app)
        await tick(pilot, ["inner.pdf"])
        await pilot.press("backspace")
        await pilot.pause()
        assert picker.cwd == folder
        assert [p.name for p in picker.selected] == ["alpha.pdf", "inner.pdf"]
        assert "[ok] 2 selected" in screen_text(app)

        # typing a filter narrows the list; esc clears it
        await pilot.press("slash")
        await pilot.press(*"phot")
        await pilot.pause()
        names = [r.path.name for r in picker.rows if r.kind == "file"]
        assert names == ["photos.pdf"]
        await pilot.press("escape")
        await pilot.pause()
        assert picker.filter == "" and len([r for r in picker.rows if r.kind == "file"]) == 4
        assert isinstance(app.screen, PickerScreen)  # esc cleared the filter, did not leave

        await pilot.press("ctrl+a")
        await pilot.pause()
        assert len(picker.selected) == 5  # 2 earlier + the 3 other readable-looking PDFs here

    run_tui(scenario, folder)


def test_picker_goto_path_and_parent_row(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        await pilot.press("ctrl+l")
        box = picker.query_one("#pathbar", Input)
        assert box.display and box.has_focus
        box.value = str(folder / "sub")
        await pilot.press("enter")
        await pilot.pause()
        assert picker.cwd == folder / "sub"
        picker.query_one("#files").highlighted = 0  # the ".." row
        await pilot.press("enter")
        await pilot.pause()
        assert picker.cwd == folder
        await pilot.press("ctrl+l")
        picker.query_one("#pathbar", Input).value = str(folder / "nope")
        await pilot.press("enter")
        await pilot.pause()
        assert "[error]" in screen_text(app)

    run_tui(scenario, folder)


def test_picker_needs_enough_files(folder):
    async def scenario(app, pilot):
        await go(pilot, "merge", ["alpha.pdf"])
        assert isinstance(app.screen, PickerScreen)
        assert "[error] Merge needs at least 2 files (ticked: 1)" in screen_text(app)
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)

    run_tui(scenario, folder)


def test_esc_goes_back_one_screen_at_a_time(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        assert isinstance(app.screen, OptionsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)

    run_tui(scenario, folder)


# ---------------------------------------------------------------- one end-to-end path per tool


def test_compress_flow(folder):
    src = folder / "photos.pdf"
    snap = Snapshot(src)

    async def scenario(app, pilot):
        await go(pilot, "compress", ["photos.pdf"])
        await choose(pilot, "level", "medium")
        result = await start_and_wait(pilot)
        text = result_text(result)
        assert (
            "[ok] Done: 1 file processed." in text
            and "[ok]  photos.pdf → photos-compressed.pdf" in text
        )
        assert "smaller)" in text and "Images: 2 of 2 recompressed" in text
        out = folder / "photos-compressed.pdf"
        assert out.exists() and out.stat().st_size < src.stat().st_size
        assert page_text(0) in extract_text(out, 0)

    run_tui(scenario, folder)
    snap.assert_unchanged()


def test_compress_target_and_output_folder(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["photos.pdf"])
        await set_value(pilot, "target", "100")
        await choose(pilot, "target-unit", "KB")
        await set_value(pilot, "output", str(folder / "out"))
        result = await start_and_wait(pilot)
        assert "[ok]  " in result_text(result)
        assert (folder / "out" / "photos-compressed.pdf").stat().st_size <= 100 * 1024

    run_tui(scenario, folder)


def test_merge_flow_with_keyboard_ordering(folder):
    async def scenario(app, pilot):
        await go(pilot, "merge", ["alpha.pdf", "รายงาน ประจำปี.pdf"])
        order = app.screen
        assert isinstance(order, OrderScreen)
        assert [p.name for p in order.files] == ["alpha.pdf", "รายงาน ประจำปี.pdf"]
        await pilot.press("d")  # move alpha down
        await pilot.pause()
        assert [p.name for p in order.files] == ["รายงาน ประจำปี.pdf", "alpha.pdf"]
        assert "  1. รายงาน ประจำปี.pdf" in screen_text(app)
        await pilot.press("shift+up")  # and back again with the other key set
        await pilot.pause()
        assert [p.name for p in order.files] == ["alpha.pdf", "รายงาน ประจำปี.pdf"]
        await pilot.press("shift+down")
        await pilot.pause()
        assert [p.name for p in order.files] == ["รายงาน ประจำปี.pdf", "alpha.pdf"]
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        result = await start_and_wait(pilot)
        assert "[ok] Done" in result_text(result)
        out = folder / "รายงาน ประจำปี-merged.pdf"
        assert page_count(out) == 7
        assert extract_text(out, 0).startswith("B-") and "page 1" in extract_text(out, 3)

    run_tui(scenario, folder)


def test_split_flow(folder):
    async def scenario(app, pilot):
        await go(pilot, "split", ["alpha.pdf"])
        await set_value(pilot, "value", "2")
        result = await start_and_wait(pilot)
        assert "alpha.pdf → 2 files" in result_text(result)
        assert sorted(p.name for p in (folder / "alpha-split").iterdir()) == [
            "alpha-p1-2.pdf",
            "alpha-p3-4.pdf",
        ]

    run_tui(scenario, folder)


def test_split_by_ranges_and_bad_value(folder):
    async def scenario(app, pilot):
        await go(pilot, "split", ["alpha.pdf"])
        await choose(pilot, "mode", "ranges")
        await set_value(pilot, "value", "")
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        assert "[error] Page ranges is required." in screen_text(app)
        await set_value(pilot, "value", "1,3-")
        result = await start_and_wait(pilot)
        assert page_count(next((folder / "alpha-split").glob("*p3-4.pdf"))) == 2
        assert "[ok] Done" in result_text(result)

    run_tui(scenario, folder)


def test_extract_delete_rotate_flows(folder):
    async def scenario(app, pilot):
        await go(pilot, "extract", ["alpha.pdf"])
        await set_value(pilot, "pages", "2-3")
        await start_and_wait(pilot)
        assert page_count(folder / "alpha-extracted.pdf") == 2
        await pilot.press("enter")  # main menu
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen) and len(app.screen_stack) == 2

        await go(pilot, "delete", ["alpha.pdf"])
        await set_value(pilot, "pages", "1")
        await start_and_wait(pilot)
        assert page_count(folder / "alpha-removed.pdf") == 3
        await pilot.press("enter")
        await pilot.pause()

        await go(pilot, "rotate", ["alpha.pdf"])
        await choose(pilot, "angle", "180")
        await set_value(pilot, "pages", "1")
        await start_and_wait(pilot)
        with pikepdf.open(folder / "alpha-rotated.pdf") as pdf:
            assert [int(p.obj.get("/Rotate", 0)) for p in pdf.pages] == [180, 0, 0, 0]

    run_tui(scenario, folder)


def test_images_to_pdf_flow(folder):
    async def scenario(app, pilot):
        await go(pilot, "img2pdf", ["one.png", "two.jpg"])
        assert isinstance(app.screen, OrderScreen)
        await pilot.press("ctrl+n")
        await pilot.pause()
        await choose(pilot, "page_size", "a4")
        result = await start_and_wait(pilot)
        assert "[ok] Done" in result_text(result)
        assert page_count(folder / "one-images.pdf") == 2

    run_tui(scenario, folder)


def test_pdf_to_images_flow(folder):
    async def scenario(app, pilot):
        await go(pilot, "pdf2img", ["alpha.pdf"])
        await choose(pilot, "format", "jpg")
        await set_value(pilot, "dpi", "50")
        await set_value(pilot, "pages", "1-2")
        result = await start_and_wait(pilot)
        assert "alpha.pdf → 2 files" in result_text(result)
        assert sorted(p.name for p in (folder / "alpha-pages").iterdir()) == [
            "alpha-p1.jpg",
            "alpha-p2.jpg",
        ]

    run_tui(scenario, folder)


def test_lock_and_unlock_flows(folder):
    async def scenario(app, pilot):
        await go(pilot, "lock", ["alpha.pdf"])
        await set_value(pilot, "new_password", "pw 123")
        await set_value(pilot, "confirm", "different")
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert "[error] The two passwords do not match." in screen_text(app)
        await set_value(pilot, "confirm", "pw 123")
        result = await start_and_wait(pilot)
        assert "AES-256" in result_text(result)
        locked = folder / "alpha-locked.pdf"
        assert page_count(locked, "pw 123") == 4
        await pilot.press("enter")
        await pilot.pause()

        await open_tool(pilot, "unlock")
        await pilot.press("ctrl+l")
        app.screen.query_one("#pathbar", Input).value = str(folder)
        await pilot.press("enter")
        await pilot.pause()
        await tick(pilot, ["alpha-locked.pdf"])
        await pilot.press("ctrl+n")
        await pilot.pause()
        await set_value(pilot, "password", "wrong")
        result = await start_and_wait(pilot)
        text = result_text(result)
        assert "[warn] Finished with problems: 0 succeeded, 1 failed." in text
        assert "[error]" in text and "password is incorrect" in text
        await pilot.press("escape")  # back to the settings, same screen kept
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        await set_value(pilot, "password", "pw 123")
        await start_and_wait(pilot)
        assert page_count(folder / "alpha-locked-unlocked.pdf") == 4

    run_tui(scenario, folder)


def test_clean_and_info_flows(folder):
    with pikepdf.open(folder / "alpha.pdf", allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Author"] = "Alice Secret"
        pdf.save(folder / "alpha.pdf")

    async def scenario(app, pilot):
        await go(pilot, "clean", ["alpha.pdf"])
        result = await start_and_wait(pilot)
        assert "Removed document info: Author" in result_text(result)
        with pikepdf.open(folder / "alpha-clean.pdf") as pdf:
            assert "/Info" not in pdf.trailer
        await pilot.press("enter")
        await pilot.pause()

        await go(pilot, "info", ["alpha.pdf", "photos.pdf"])
        result = await start_and_wait(pilot)
        text = result_text(result)
        assert "Pages:       4" in text and "Pages:       2" in text and "Bookmarks:   yes" in text

    run_tui(scenario, folder)


# ---------------------------------------------------------------- problems, cancelling, bugs


def test_a_broken_file_does_not_stop_the_others(folder):
    async def scenario(app, pilot):
        await go(pilot, "rotate", ["alpha.pdf", "broken.pdf", "photos.pdf"])
        result = await start_and_wait(pilot)
        text = result_text(result)
        assert "[warn] Finished with problems: 2 succeeded, 1 failed." in text
        assert text.count("[ok]  ") == 2 and "[error]  broken.pdf" in text.replace(
            "  broken", "  broken"
        )
        assert "not a valid PDF" in text and "Traceback" not in text
        assert (folder / "alpha-rotated.pdf").exists() and (folder / "photos-rotated.pdf").exists()

    run_tui(scenario, folder)


def test_cancel_stops_the_job_and_the_screen_stays_responsive(folder, monkeypatch):
    started = threading.Event()

    def slow_compress(source, dest, *, cancel=None, progress=None, **kwargs):
        started.set()
        for _ in range(400):
            cancel.check()
            time.sleep(0.01)
        return OperationResult(command="compress", sources=[source])

    monkeypatch.setattr(tools_mod, "compress_pdf", slow_compress)

    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf", "photos.pdf"])
        await pilot.press("ctrl+r")
        while not started.is_set():
            await pilot.pause(0.02)
        assert isinstance(app.screen, RunScreen)
        await pilot.press("escape")  # the UI thread still answers keys while the worker runs
        result = await wait_for_result(pilot, timeout=10)
        text = result_text(result)
        assert "[warn] Cancelled" in text
        assert not list(folder.glob("*-compressed.pdf"))

    run_tui(scenario, folder)


def test_progress_shows_file_names_with_brackets_and_control_codes_as_they_are(folder, monkeypatch):
    """The progress line is not markup: "[draft] x.pdf" must not lose "[draft]"."""
    shown, release = threading.Event(), threading.Event()
    name = "[draft] report [Final].pdf"
    make_pdf(folder / name, pages=1)

    def reporting_compress(source, dest, *, cancel=None, progress=None, **kwargs):
        report(progress, 1, 2, f"Reading {source.name}\x1b]0;owned\x07")
        shown.set()
        release.wait(10)
        return OperationResult(command="compress", sources=[source])

    monkeypatch.setattr(tools_mod, "compress_pdf", reporting_compress)

    async def scenario(app, pilot):
        await go(pilot, "compress", [name])
        await pilot.press("ctrl+r")
        while not shown.is_set():
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        current = str(app.screen.query_one("#current").render())
        release.set()
        assert f"Reading {name}?]0;owned?" in current
        await wait_for_result(pilot)

    run_tui(scenario, folder)


def test_unexpected_errors_are_summarised_without_a_traceback(folder, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tools_mod, "compress_pdf", boom)

    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        result = await start_and_wait(pilot)
        text = result_text(result)
        assert "[error]  alpha.pdf" in text or "[error] Unexpected error: kaboom" in text
        assert "Traceback" not in text

    run_tui(scenario, folder)


def test_option_errors_stay_on_the_form(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        screen = app.screen
        screen.query_one("#f-grayscale").value = True
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert screen is app.screen
        assert "[error] Grayscale needs level low, medium or high" in screen_text(app)
        await set_value(pilot, "target", "abc")
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert "Cannot understand size" in screen_text(app)

    run_tui(scenario, folder)


# ---------------------------------------------------------------- 80x24, keyboard-only, no network


def test_every_screen_fits_80x24(folder):
    async def scenario(app, pilot):
        def check(name):
            lines = screen_text(app).splitlines()
            assert len(lines) <= 24, name
            assert all(len(line) <= 80 for line in lines), name
            footer = app.screen.query("Footer")
            if footer:
                assert footer.first().region.bottom == 24, name

        check("menu")
        for tool in TOOLS:
            await open_tool(pilot, tool.id)
            check(f"picker {tool.id}")
            names = (
                ["one.png", "two.jpg"] if tool.accepts == "image" else ["alpha.pdf", "photos.pdf"]
            )
            await tick(pilot, names)
            await pilot.press("ctrl+n")
            await pilot.pause()
            if isinstance(app.screen, OrderScreen):
                check(f"order {tool.id}")
                await pilot.press("ctrl+n")
                await pilot.pause()
            check(f"options {tool.id}")
            for button in app.screen.query(Button):
                assert button.region.bottom <= 23, f"start button off screen in {tool.id}"
            await pilot.press("escape")
            await pilot.press("escape")
            if isinstance(app.screen, PickerScreen):
                await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, MenuScreen), tool.id

    run_tui(scenario, folder)


def test_keyboard_only_path_with_real_keys(folder):
    """Menu -> picker -> options -> run -> result, touching nothing but the keyboard."""

    async def scenario(app, pilot):
        await pilot.press("down", "down", "down", "down", "down")  # Rotate
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down")  # first real entry after ".."
        # the list is: .., sub/, alpha.pdf, ... ; move to alpha.pdf and tick it
        picker = app.screen
        target = next(i for i, r in enumerate(picker.rows) if r.path.name == "alpha.pdf")
        for _ in range(target - picker.query_one("#files", OptionList).highlighted):
            await pilot.press("down")
        await pilot.press("space", "ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        await pilot.press("tab")  # angle -> pages
        await pilot.press("1")
        await pilot.press("ctrl+r")
        result = await wait_for_result(pilot)
        assert "[ok] Done" in result_text(result)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)

    run_tui(scenario, folder)
    with pikepdf.open(folder / "alpha-rotated.pdf") as pdf:
        assert [int(p.obj.get("/Rotate", 0)) for p in pdf.pages] == [90, 0, 0, 0]


def test_tui_makes_no_network_connections(folder, monkeypatch):
    """Run a whole job and record every socket connection / DNS lookup the process makes."""
    connections: list[object] = []
    lookups: list[object] = []
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def spy_connect(self, address):
        connections.append(address)
        return real_connect(self, address)

    def spy_getaddrinfo(host, *args, **kwargs):
        lookups.append(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket, "getaddrinfo", spy_getaddrinfo)

    async def scenario(app, pilot):
        await go(pilot, "compress", ["photos.pdf"])
        await choose(pilot, "level", "high")
        result = await start_and_wait(pilot)
        assert "[ok] Done" in result_text(result)

    run_tui(scenario, folder)
    # asyncio's own wake-up socket pair is loopback; anything else would be a leak.
    loopback = {"127.0.0.1", "::1", "localhost"}
    outside = [a for a in connections if not (isinstance(a, tuple) and a[0] in loopback)]
    assert outside == [], f"non-loopback connections: {outside}"
    assert [h for h in lookups if h not in loopback] == []


def test_cancel_token_type_is_what_the_tools_expect():
    assert isinstance(CancelToken(), CancelToken)


# ---------------------------------------------------------------- keyboard-only forms


def focus_id(app) -> str | None:
    return getattr(app.focused, "id", None)


def test_every_settings_form_can_be_walked_with_arrow_keys_only(folder):
    """Down reaches every field in order and then Start; up walks back. No mouse, no tab."""

    async def scenario(app, pilot):
        for tool in TOOLS:
            if not tool.fields:
                continue
            files = ["one.png", "two.jpg"] if tool.accepts == "image" else ["alpha.pdf"]
            if tool.min_files > 1 and tool.accepts == "pdf":
                files = ["alpha.pdf", "photos.pdf"]
            await go(pilot, tool.id, files)
            if isinstance(app.screen, OrderScreen):
                await pilot.press("ctrl+n")
                await pilot.pause()
            assert isinstance(app.screen, OptionsScreen), tool.id
            wanted = [
                wid
                for f in tool.fields
                for wid in (
                    [f"f-{f.key}", f"f-{f.key}-unit"] if f.kind == "size" else [f"f-{f.key}"]
                )
            ] + ["start"]
            seen = [focus_id(app)]
            for _ in wanted[1:]:
                await pilot.press("down")
                seen.append(focus_id(app))
            assert seen == wanted, f"{tool.id}: {seen}"
            for expected in reversed(wanted[:-1]):
                await pilot.press("up")
                assert focus_id(app) == expected, tool.id
            while not isinstance(app.screen, MenuScreen):
                await pilot.press("escape")
                await pilot.pause()

    run_tui(scenario, folder)


def test_compress_form_by_keyboard_only(folder):
    """The reported case: pick a level with the arrows, then reach target and grayscale."""

    async def scenario(app, pilot):
        await go(pilot, "compress", ["photos.pdf"])
        assert focus_id(app) == "f-level"
        await pilot.press("right", "right", "right")  # lossless -> low -> medium -> high
        assert app.screen.query_one("#f-level", Selector).value == "high"
        await pilot.press("down")
        assert focus_id(app) == "f-target"
        await pilot.press(*"1")  # just a number: the unit is picked, not typed
        await pilot.press("down")
        assert focus_id(app) == "f-target-unit"
        assert app.screen.query_one("#f-target-unit", Selector).value == "MB"  # the usual one
        await pilot.press("down")
        assert focus_id(app) == "f-grayscale"
        await pilot.press("space")
        assert app.screen.query_one("#f-grayscale").value is True
        assert app.screen.values()["target"] == "1MB"
        await pilot.press("up", "up", "up")
        assert focus_id(app) == "f-level"
        # enter moves on to the next field and changes nothing
        await pilot.press("enter")
        assert focus_id(app) == "f-target"
        assert app.screen.values()["level"] == "high" and app.screen.values()["target"] == "1MB"
        result = await start_and_wait(pilot)
        assert "Done" in result_text(result)

    run_tui(scenario, folder)


def test_start_button_works_with_enter(folder):
    async def scenario(app, pilot):
        await go(pilot, "info", ["alpha.pdf"])
        while focus_id(app) != "start":
            await pilot.press("down")
        await pilot.press("enter")
        result = await wait_for_result(pilot)
        assert "page" in result_text(result).lower()

    run_tui(scenario, folder)


def test_selector_marks_the_choice_without_colour_and_accepts_clicks(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        text = screen_text(app)
        assert "(●) lossless" in text and "( ) low" in text and "( ) medium" in text
        assert "( ) high" in text
        await pilot.press("right")
        text = screen_text(app)
        assert "( ) lossless" in text and "(●) low" in text
        selector = app.screen.query_one("#f-level", Selector)
        await pilot.click("#f-level", offset=(len("(●) lossless  ( ) low  ( ) medium  ") + 2, 0))
        assert selector.value == "high"

    run_tui(scenario, folder)


# ---------------------------------------------------------------- path bar


def test_parse_one_path_accepts_exactly_one_path(tmp_path):
    from pdfzi.tui.screens.picker import parse_one_path

    spaced = tmp_path / "my docs" / "a b.pdf"
    other = tmp_path / "c.pdf"
    assert parse_one_path(str(spaced)) == (spaced, None)  # spaces are fine, no quotes needed
    assert parse_one_path(f'"{spaced}"') == (spaced, None)  # Explorer's "Copy as path"
    assert parse_one_path(f"  '{spaced}'  \n") == (spaced, None)
    assert parse_one_path("   \n  ") == (None, None)  # nothing pasted: nothing to do
    for many in (f'"{spaced}" "{other}"', f'"{spaced}"\r\n"{other}"'):
        path, problem = parse_one_path(many)
        assert path is None and "one file path at a time" in problem


def test_enter_on_a_pasted_file_goes_straight_to_the_settings(folder):
    """Paste, Enter, and you are on the settings page: no ticking, no ctrl+n."""

    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        assert focus_id(app) == "pathbar"  # the path bar is where you start
        target = folder / "sub" / "inner.pdf"
        app.post_message(events.Paste(f'"{target}"'))
        await pilot.pause()
        bar = picker.query_one("#pathbar", Input)
        assert bar.has_focus and bar.value == str(target)
        assert isinstance(app.screen, PickerScreen)  # a paste alone does nothing yet
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        assert app.screen.files == [target]
        assert "Compress: 1 file" in screen_text(app) and "inner.pdf" in screen_text(app)
        result = await start_and_wait(pilot)
        assert "[ok] Done" in result_text(result)
        assert page_count(folder / "sub" / "inner-compressed.pdf") == 2

    run_tui(scenario, folder)


def test_typing_a_path_works_the_same_as_pasting(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "info")
        await pilot.press("ctrl+l")
        app.screen.query_one("#pathbar", Input).value = str(folder / "alpha.pdf")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        await pilot.press("escape")  # back to the picker, now in that file's folder
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen) and app.screen.cwd == folder

    run_tui(scenario, folder)


def test_tools_that_need_several_files_tick_the_pasted_file_instead(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "merge")
        picker = app.screen
        for name in ("alpha.pdf", "photos.pdf"):
            app.post_message(events.Paste(str(folder / name)))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PickerScreen)  # stays: merge needs at least two
        assert [p.name for p in picker.selected] == ["alpha.pdf", "photos.pdf"]
        assert "[ok] 2 selected" in screen_text(app)
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, OrderScreen)

    run_tui(scenario, folder)


def test_a_folder_path_opens_the_folder(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        app.post_message(events.Paste(str(folder / "sub")))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert picker.cwd == folder / "sub" and "inner.pdf" in screen_text(app)
        assert isinstance(app.focused, OptionList)  # ready to browse with the arrows

    run_tui(scenario, folder)


def test_path_bar_refuses_more_than_one_file_and_bad_input_in_words(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        a, b = folder / "alpha.pdf", folder / "photos.pdf"
        # several paths: refused at once, nothing is put in the bar
        for text in (f'"{a}" "{b}"', f'"{a}"\r\n"{b}"'):
            before = picker.query_one("#pathbar", Input).value
            app.post_message(events.Paste(text))
            await pilot.pause()
            assert "one file path at a time" in screen_text(app)
            assert picker.query_one("#pathbar", Input).value == before
        # one path, but unusable: explained when Enter is pressed
        for text, message in [
            (str(folder / "nope.pdf"), "File not found"),
            (str(folder / "notes.txt"), "not a PDF file"),
        ]:
            app.post_message(events.Paste(text))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            shown = screen_text(app)
            assert "[error]" in shown and message in shown, shown
            assert isinstance(app.screen, PickerScreen)
        assert picker.selected == []

    run_tui(scenario, folder)


def test_the_path_bar_is_selected_first_and_typing_replaces_the_folder_shown(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        bar = picker.query_one("#pathbar", Input)
        assert focus_id(app) == "pathbar" and bar.value == str(folder)
        await pilot.press(*"ab")  # no need to clear the folder first
        assert bar.value == "ab"
        await pilot.press("escape")  # first esc undoes the typing and stays on the bar
        assert bar.value == str(folder) and focus_id(app) == "pathbar"
        assert isinstance(app.screen, PickerScreen)
        await pilot.press("escape")  # the next one goes back, as everywhere else
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)

    run_tui(scenario, folder)


def test_keyboard_moves_between_path_bar_and_list(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        assert focus_id(app) == "pathbar"
        await pilot.press("down")  # arrow down leaves the text box
        assert isinstance(app.focused, OptionList)
        await pilot.press("tab")  # tab goes round: list -> path bar
        assert focus_id(app) == "pathbar"
        await pilot.press("down", "ctrl+l")  # ctrl+l still jumps to the bar
        assert focus_id(app) == "pathbar"

    run_tui(scenario, folder)


# ---------------------------------------------------------------- look


def test_menu_shows_the_logo_when_there_is_room(folder):
    async def scenario(app, pilot):
        text = screen_text(app)
        assert "██████╗" in text and "╚═╝     ╚═════╝" in text  # the big logo
        assert all(t.title in text for t in TOOLS)  # and every tool still fits at 80x24
        assert len(text.splitlines()) <= 24

    run_tui(scenario, folder)


def test_menu_uses_a_smaller_logo_on_short_terminals(folder):
    async def scenario(app, pilot):
        text = screen_text(app)
        assert "╔═╗╔╦╗╔═╗╔═╗╦" in text and "██████╗" not in text

    run_tui(scenario, folder, size=(80, 20))


def test_arrows_still_move_between_fields_when_the_form_must_scroll(folder):
    """A short window makes the form scroll; the arrows must keep moving the focus."""

    async def scenario(app, pilot):
        await go(pilot, "lock", ["alpha.pdf"])
        tool = next(t for t in TOOLS if t.id == "lock")
        wanted = [f"f-{f.key}" for f in tool.fields] + ["start"]
        seen = [focus_id(app)]
        for _ in wanted[1:]:
            await pilot.press("down")
            seen.append(focus_id(app))
        assert seen == wanted
        await wait_until(pilot, lambda: "↑↓" in footer(app))  # the footer redraws after focus
        assert "↑↓" in footer(app)  # and the footer says so

    run_tui(scenario, folder, size=(70, 12))


# ---------------------------------------------------------------- ctrl+v and arrow up


def fake_clipboard(monkeypatch, text):
    from pdfzi.tui import clipboard

    monkeypatch.setattr(clipboard, "read_text", lambda: text)


def test_ctrl_v_pastes_from_the_system_clipboard_with_the_list_focused(folder, monkeypatch):
    target = folder / "รายงาน ประจำปี.pdf"
    fake_clipboard(monkeypatch, str(target))

    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        await pilot.press("down")  # leave the bar: now the list has focus
        assert isinstance(app.focused, OptionList)
        await pilot.press("ctrl+v")
        bar = app.screen.query_one("#pathbar", Input)
        assert bar.has_focus and bar.value == str(target)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen) and app.screen.files == [target]

    run_tui(scenario, folder)


def test_ctrl_v_in_the_path_bar_replaces_what_is_there(folder, monkeypatch):
    """Textual's own ctrl+v would paste its private clipboard (empty): ours must win."""
    target = folder / "alpha.pdf"
    fake_clipboard(monkeypatch, f'"{target}"')  # quoted, as "Copy as path" gives it

    async def scenario(app, pilot):
        await open_tool(pilot, "info")
        await pilot.press("ctrl+l")
        bar = app.screen.query_one("#pathbar", Input)
        assert bar.has_focus and bar.value == str(folder)
        await pilot.press("ctrl+v")
        assert bar.value == str(target)

    run_tui(scenario, folder)


def test_ctrl_v_explains_an_unreadable_or_empty_clipboard(folder, monkeypatch):
    async def scenario(app, pilot):
        await open_tool(pilot, "info")
        for value, message in [(None, "cannot read the clipboard"), ("  ", "no text or file")]:
            fake_clipboard(monkeypatch, value)
            await pilot.press("ctrl+v")
            await pilot.pause()
            shown = screen_text(app)
            assert "[error]" in shown and message in shown, shown
        assert app.screen.query_one("#pathbar", Input).value == str(folder)  # untouched

    run_tui(scenario, folder)


def test_ctrl_v_with_several_files_copied_is_refused(folder, monkeypatch):
    fake_clipboard(monkeypatch, f"{folder / 'alpha.pdf'}\n{folder / 'photos.pdf'}")

    async def scenario(app, pilot):
        await open_tool(pilot, "info")
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert "one file path at a time" in screen_text(app)

    run_tui(scenario, folder)


def test_arrow_up_from_the_top_of_the_list_reaches_the_path_bar(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        listing = app.screen.query_one("#files", OptionList)
        await pilot.press("down")  # from the path bar into the list
        assert listing.highlighted == 1  # first real file; row 0 is ".."
        await pilot.press("up")
        assert listing.highlighted == 0 and isinstance(app.focused, OptionList)
        await pilot.press("up")  # nothing above the first row: on to the path bar
        assert focus_id(app) == "pathbar"
        await pilot.press(*"x")
        assert app.screen.query_one("#pathbar", Input).value == "x"  # you can type now
        await pilot.press("backspace", "down")  # and down returns to the list
        assert isinstance(app.focused, OptionList) and listing.highlighted == 0

    run_tui(scenario, folder)


def test_arrow_up_in_the_middle_of_the_list_just_moves_up(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        listing = app.screen.query_one("#files", OptionList)
        await pilot.press("down", "down", "down")  # bar -> list, then two rows down
        before = listing.highlighted
        await pilot.press("up")
        assert listing.highlighted == before - 1 and isinstance(app.focused, OptionList)

    run_tui(scenario, folder)


def test_target_size_is_a_number_plus_a_chosen_unit(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        form = app.screen
        assert form.values()["target"] == ""  # nothing typed: no target
        text = screen_text(app)
        assert "Target size" in text and "(●) MB" in text and "( ) KB" in text
        await set_value(pilot, "target", "1.5")
        assert form.values()["target"] == "1.5MB"
        await choose(pilot, "target-unit", "KB")
        assert form.values()["target"] == "1.5KB"
        await set_value(pilot, "target", "")
        assert form.values()["target"] == ""  # a unit alone is not a target

    run_tui(scenario, folder)


# ---------------------------------------------------------------- space ticks, enter goes on


def test_space_ticks_a_file_and_enter_never_does(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "merge")  # needs two files, so enter cannot start anything
        picker = app.screen
        await pilot.press("down")  # from the path bar into the list
        alpha = next(i for i, r in enumerate(picker.rows) if r.path.name == "alpha.pdf")
        picker.query_one("#files").highlighted = alpha
        await pilot.press("space")
        assert [p.name for p in picker.selected] == ["alpha.pdf"]
        await pilot.press("space")  # space again unticks
        assert picker.selected == []
        await pilot.press("enter")  # enter on a file is not a tick
        await pilot.pause()
        assert picker.selected == [] and isinstance(app.screen, PickerScreen)
        assert "needs at least 2 files" in screen_text(app)

    run_tui(scenario, folder)


def test_enter_continues_with_the_ticked_files(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "merge")
        picker = app.screen
        await tick(pilot, ["alpha.pdf", "photos.pdf"])
        other = next(i for i, r in enumerate(picker.rows) if r.path.name == "broken.pdf")
        picker.query_one("#files").highlighted = other  # an UNticked file is highlighted
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, OrderScreen)  # went on with the two ticked ones
        assert [p.name for p in app.screen.files] == ["alpha.pdf", "photos.pdf"]
        await pilot.press("enter")  # and enter goes on from the order screen too
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)

    run_tui(scenario, folder)


def test_enter_on_a_file_with_nothing_ticked_uses_that_file(folder):
    """The quick path for one file: highlight it, press enter, you are at the settings."""

    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        await pilot.press("down")
        picker.query_one("#files").highlighted = next(
            i for i, r in enumerate(picker.rows) if r.path.name == "alpha.pdf"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)
        assert [p.name for p in app.screen.files] == ["alpha.pdf"]

    run_tui(scenario, folder)


def test_enter_still_opens_folders_and_the_parent_row(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        await pilot.press("down")
        picker.query_one("#files").highlighted = next(
            i for i, r in enumerate(picker.rows) if r.kind == "dir"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert picker.cwd == folder / "sub" and isinstance(app.screen, PickerScreen)
        picker.query_one("#files").highlighted = 0  # the ".." row
        await pilot.press("enter")
        await pilot.pause()
        assert picker.cwd == folder

    run_tui(scenario, folder)


def test_in_the_settings_only_space_ticks_and_changes_and_enter_moves_on(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        form = app.screen
        # a choice: space moves to the next one; enter moves to the next FIELD, changing nothing
        assert form.query_one("#f-level", Selector).value == "lossless"
        await pilot.press("space")
        assert form.query_one("#f-level", Selector).value == "low"
        await pilot.press("enter")
        assert focus_id(app) == "f-target" and form.query_one("#f-level", Selector).value == "low"
        # a tick box: space toggles; enter does not, it moves on
        form.query_one("#f-grayscale").focus()
        await pilot.press("space")
        assert form.query_one("#f-grayscale").value is True
        await pilot.press("enter")
        assert form.query_one("#f-grayscale").value is True  # still ticked: enter did nothing to it
        assert focus_id(app) == "f-password"
        form.query_one("#f-grayscale").focus()
        await pilot.press("space")  # and space unticks it again
        assert form.query_one("#f-grayscale").value is False

    run_tui(scenario, folder)


def test_enter_walks_through_every_field_and_on_start_it_runs(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        form = app.screen
        seen = [focus_id(app)]
        while focus_id(app) != "start":
            await pilot.press("enter")
            seen.append(focus_id(app))
            assert len(seen) < 12
        assert seen == ["f-level", "f-target", "f-target-unit", "f-grayscale", "f-password",
                        "f-output", "start"]  # fmt: skip
        assert form is app.screen  # nothing ran on the way
        await pilot.press("enter")  # on the Start button
        result = await wait_for_result(pilot)
        assert "[ok]" in result_text(result)

    run_tui(scenario, folder)


def test_a_mistake_in_a_field_is_shown_when_start_is_pressed(folder):
    async def scenario(app, pilot):
        await go(pilot, "extract", ["alpha.pdf"])
        form = app.screen
        assert focus_id(app) == "f-pages"
        await pilot.press("enter")  # empty field: on to the next one, nothing runs
        assert focus_id(app) == "f-password" and form is app.screen
        await pilot.press("ctrl+r")  # start with the pages still empty
        await pilot.pause()
        assert form is app.screen and "[error]" in screen_text(app)
        form.query_one("#f-pages").focus()
        await pilot.press(*"1-2")
        await pilot.press("ctrl+r")
        result = await wait_for_result(pilot)
        assert page_count(folder / "alpha-extracted.pdf") == 2
        assert "[ok]" in result_text(result)

    run_tui(scenario, folder)


# ---------------------------------------------------------------- hints on the settings screen


def footer(app) -> str:
    return screen_text(app).splitlines()[-1]


def test_the_footer_names_the_keys_for_the_focused_field(folder):
    """Nothing is left to guessing: the bottom bar lists what works on the focused field."""

    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        form = app.screen
        expected = {  # field -> keys that must be named in the footer
            "f-level": ["←→ Change", "Next"],
            "f-target": ["Next"],
            "f-target-unit": ["←→ Change", "Next"],
            "f-grayscale": ["space Tick", "Next"],
            "f-password": ["Next"],
            "f-output": ["Next"],
            "start": ["Run"],
        }
        for wid, words in expected.items():
            form.query_one(f"#{wid}").focus()
            await wait_until(pilot, lambda words=words: all(w in footer(app) for w in words))
            bar = footer(app)
            for word in [*words, "esc Back", "^r Start"]:
                assert word in bar, (wid, word, bar)
            if wid != "start":
                assert "↑↓ Field" in bar, (wid, bar)
        # and no key help is written into the body any more
        body = "\n".join(screen_text(app).splitlines()[:-1])
        assert "enter: next field" not in body and "space: tick" not in body

    run_tui(scenario, folder)


def level_row(app) -> str:
    return next(line for line in screen_text(app).splitlines() if line.strip().startswith("Level"))


def test_compress_notes_each_level_beside_the_options_and_follows_the_choice(folder):
    from pdfzi.core.compress import PROFILES

    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        # a short dim note sits on the same row as the options, for the chosen one only
        row = level_row(app)
        assert "(●) lossless" in row and "images as is" in row and "dpi" not in row
        # and the sentence under the form says more, with the real numbers
        assert "Images untouched; only the file structure is tidied" in screen_text(app)
        for level, verdict in [
            ("low", "Hard to tell from the original"),
            ("medium", "Same look on screen"),
            ("high", "Smallest; scans look soft"),
        ]:
            await pilot.press("right")
            profile = PROFILES[level]
            row = level_row(app)
            assert f"(●) {level}" in row and f"{profile.dpi} dpi, q{profile.quality}" in row
            assert f"Sharper images scaled to {profile.dpi} dpi" in screen_text(app)
            assert verdict in screen_text(app)
        assert "images as is" not in row  # only the chosen option has a note
        assert all(len(line) <= 80 for line in screen_text(app).splitlines())

    run_tui(scenario, folder)


def test_the_body_holds_no_key_instructions_on_any_screen(folder):
    """Keys are named in the footer only; the body is not cluttered with instructions."""
    banned = ["enter: ", "space: ", "Press esc", "press enter", "↑↓ then enter", "space ticks"]

    async def scenario(app, pilot):
        texts = [screen_text(app)]
        await open_tool(pilot, "merge")
        texts.append(screen_text(app))  # picker
        await tick(pilot, ["alpha.pdf", "photos.pdf"])
        await pilot.press("ctrl+n")
        await pilot.pause()
        texts.append(screen_text(app))  # order
        await pilot.press("enter")
        await pilot.pause()
        texts.append(screen_text(app))  # settings
        for text in texts:
            body = "\n".join(text.splitlines()[:-1])
            assert not any(b in body for b in banned), body

    run_tui(scenario, folder)


CHOICE_CASES = [
    # tool, files, field, (note, sentence words) of the first choice, then of the next one
    ("pdf2img", ["alpha.pdf"], "format",
     ("exact, larger files", "keeps every pixel"), ("smaller, JPEG q90", "much smaller")),
    ("img2pdf", ["one.png", "two.jpg"], "page_size",
     ("as big as the picture", "no dpi on it: A4"), ("scaled to A4", "fit an A4 page")),
    ("split", ["alpha.pdf"], "mode",
     ("N pages per file", "every N pages"), ("one file per range", "such as 1-3,7,10-")),
]  # fmt: skip


def test_other_choice_fields_explain_their_choices_too(folder):
    async def scenario(app, pilot):
        for tool, files, field, first, second in CHOICE_CASES:
            await go(pilot, tool, files)
            if isinstance(app.screen, OrderScreen):
                await pilot.press("ctrl+n")
                await pilot.pause()
            app.screen.query_one(f"#f-{field}").focus()
            await pilot.pause()
            text = screen_text(app)
            assert all(w in text for w in first), (tool, text)
            await pilot.press("right")
            text = screen_text(app)
            assert all(w in text for w in second) and first[0] not in text, (tool, text)
            while not isinstance(app.screen, MenuScreen):
                await pilot.press("escape")
                await pilot.pause()

    run_tui(scenario, folder)


def test_tools_without_choices_have_no_notes(folder):
    async def scenario(app, pilot):
        await go(pilot, "lock", ["alpha.pdf"])
        assert not list(app.screen.query(".note"))

    run_tui(scenario, folder)


def test_tick_box_shows_its_key_in_the_footer(folder):
    async def scenario(app, pilot):
        await go(pilot, "compress", ["alpha.pdf"])
        app.screen.query_one("#f-grayscale").focus()
        await wait_until(pilot, lambda: "space Tick" in screen_text(app).splitlines()[-1])
        assert "space Tick" in screen_text(app).splitlines()[-1]

    run_tui(scenario, folder)


# ---------------------------------------------------------------- a long report can be scrolled


def test_a_long_result_can_be_scrolled_to_the_end(folder):
    """info on many files makes a report taller than a small terminal: it must scroll."""
    import shutil

    for i in range(25):
        shutil.copy(folder / "alpha.pdf", folder / f"copy-{i:02d}.pdf")

    async def scenario(app, pilot):
        await open_tool(pilot, "info")
        await pilot.press("down")
        await pilot.press("ctrl+a")  # tick every PDF in the folder
        await pilot.press("ctrl+n")
        await pilot.pause()
        result = await start_and_wait(pilot)
        scroll = result.query_one("VerticalScroll")
        assert scroll.max_scroll_y > 0, "the report fits: make it longer"
        top = await settled_screen(pilot)
        last = "รายงาน ประจำปี.pdf"  # sorts last: it is at the very end of the report
        assert last not in top  # the end is off screen at first
        assert "↑↓ PgUp PgDn Scroll" in footer(app)  # and the footer says how to get there
        await pilot.press("end")
        await wait_until(pilot, lambda: last in screen_text(app))
        end = await settled_screen(pilot)
        assert last in end and end != top
        await pilot.press("home")
        await wait_until(pilot, lambda: screen_text(app) == top)
        assert screen_text(app) == top
        await pilot.press("pagedown")
        await wait_until(pilot, lambda: screen_text(app) != top)
        assert screen_text(app) != top  # a page at a time works too
        assert len(end.splitlines()) <= 24

    run_tui(scenario, folder)


def test_a_short_result_lists_no_scroll_keys(folder):
    async def scenario(app, pilot):
        await go(pilot, "info", ["alpha.pdf"])
        result = await start_and_wait(pilot)
        assert "Scroll" not in footer(app)  # nothing to scroll: no scroll keys listed
        assert result.query_one("VerticalScroll").max_scroll_y == 0

    run_tui(scenario, folder)


# ---------------------------------------------------------------- the mouse, and what is shown


def row_of(screen, name: str) -> int:
    return next(i for i, r in enumerate(screen.rows) if r.path.name.rstrip("/") == name)


def test_a_click_ticks_a_file_and_continue_goes_on(folder):
    """Clicking is not enter: it ticks, so the mouse can pick several files."""

    async def scenario(app, pilot):
        await open_tool(pilot, "merge")
        picker = app.screen
        await pilot.click("#files", offset=(4, row_of(picker, "alpha.pdf")))
        await pilot.click("#files", offset=(4, row_of(picker, "photos.pdf")))
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        assert [p.name for p in picker.selected] == ["alpha.pdf", "photos.pdf"]
        await pilot.click("#files", offset=(4, row_of(picker, "photos.pdf")))  # untick
        await pilot.pause()
        assert [p.name for p in picker.selected] == ["alpha.pdf"]
        await pilot.click("#continue")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)  # merge needs two: it says so
        assert "needs at least 2 files" in screen_text(app)
        await pilot.click("#files", offset=(4, row_of(picker, "photos.pdf")))
        await pilot.pause()  # the tick arrives as a message; let it land before Continue
        await pilot.click("#continue")
        await pilot.pause()
        assert isinstance(app.screen, OrderScreen)

    run_tui(scenario, folder)


def test_a_click_on_a_folder_opens_it(folder):
    async def scenario(app, pilot):
        await open_tool(pilot, "compress")
        picker = app.screen
        await pilot.click("#files", offset=(4, row_of(picker, "sub")))
        await pilot.pause()
        assert picker.cwd == folder.resolve() / "sub"
        await pilot.click("#files", offset=(4, 0))  # ".. (parent folder)"
        await pilot.pause()
        assert picker.cwd == folder.resolve()

    run_tui(scenario, folder)


def test_the_order_can_be_set_with_the_mouse(folder):
    async def scenario(app, pilot):
        await go(pilot, "merge", ["alpha.pdf", "photos.pdf", "รายงาน ประจำปี.pdf"])
        order = app.screen
        assert isinstance(order, OrderScreen)
        await pilot.click("#order", offset=(4, 2))  # a click picks a file; it does not go on
        await pilot.pause()
        assert isinstance(app.screen, OrderScreen)
        await pilot.click("#up")
        await pilot.pause()
        assert [p.name for p in order.files] == ["alpha.pdf", "รายงาน ประจำปี.pdf", "photos.pdf"]
        await pilot.click("#down")
        await pilot.click("#remove")  # the picked file, still photos.pdf... now last
        await pilot.pause()
        assert [p.name for p in order.files] == ["alpha.pdf", "photos.pdf"]
        assert "2 files" in screen_text(app)
        await pilot.click("#continue")
        await pilot.pause()
        assert isinstance(app.screen, OptionsScreen)

    run_tui(scenario, folder)


def test_one_picture_has_no_order_to_set(folder):
    async def scenario(app, pilot):
        await go(pilot, "img2pdf", ["one.png"])
        assert isinstance(app.screen, OptionsScreen)

    run_tui(scenario, folder)


def test_split_ranges_do_not_inherit_the_page_count(folder):
    """Choosing "ranges" must not leave the "1" of "every 1 page" in the box: that would
    quietly give a file with page 1 only."""

    def label(app) -> str:
        return next(
            line
            for line in screen_text(app).splitlines()
            if "Split by" not in line
            and line.startswith(" ")
            and ("Pages per file" in line or "Ranges" in line)
        )

    async def switch(pilot, mode: str) -> None:
        selector = pilot.app.screen.query_one("#f-mode", Selector)
        selector.focus()
        while selector.value != mode:  # right goes round the choices
            await pilot.press("right")
        await pilot.pause()

    async def scenario(app, pilot):
        await go(pilot, "split", ["alpha.pdf"])
        value = app.screen.query_one("#f-value", Input)
        assert value.value == "1" and "Pages per file" in label(app)
        await switch(pilot, "ranges")
        assert value.value == "" and label(app).startswith(" Ranges")
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert "[error] Page ranges is required." in screen_text(app)
        await set_value(pilot, "value", "2-3")
        await switch(pilot, "every")  # what the user typed stays
        assert value.value == "2-3" and "Pages per file" in label(app)
        await switch(pilot, "ranges")
        await set_value(pilot, "value", "")
        await switch(pilot, "every")  # an empty box gets the default back
        assert value.value == "1"

    run_tui(scenario, folder)


def test_the_settings_say_how_many_pages_the_files_have(folder):
    async def scenario(app, pilot):
        await go(pilot, "extract", ["alpha.pdf", "รายงาน ประจำปี.pdf"])
        await wait_until(pilot, lambda: "(4 pages)" in screen_text(app))
        assert "alpha.pdf (4 pages), รายงาน ประจำปี.pdf (3 pages)" in screen_text(app)

    run_tui(scenario, folder)


def test_a_merge_counts_every_file_and_only_compress_compares_sizes(folder):
    async def scenario(app, pilot):
        await go(pilot, "merge", ["alpha.pdf", "รายงาน ประจำปี.pdf"])
        await pilot.press("enter")
        await pilot.pause()
        text = result_text(await start_and_wait(pilot))
        assert "[ok] Done: 2 files processed." in text
        assert "2 files → alpha-merged.pdf" in text
        assert "7 pages · " in text and "smaller)" not in text and "larger)" not in text

    run_tui(scenario, folder)


def test_long_notes_wrap_under_their_own_text():
    import io

    from rich.console import Console

    from pdfzi.core.batch import BatchEntry
    from pdfzi.tui.screens.result import Outcome, describe

    note = "a note long enough to need a second line on a narrow screen, and a third too"
    ok = OperationResult(command="rotate", sources=[Path("a.pdf")], notes=[note])
    console = Console(width=40, file=io.StringIO(), record=True)
    console.print(describe(TOOLS[0], Outcome(entries=[BatchEntry(Path("a.pdf"), result=ok)])))
    lines = console.export_text().splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("    • a note"))
    assert lines[first + 1].startswith("      ") and lines[first + 1].strip()
