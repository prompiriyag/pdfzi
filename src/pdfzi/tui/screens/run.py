"""Runs a job in a worker thread, shows progress, and can be cancelled."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, ProgressBar, Static

from pdfzi.core.errors import OperationCancelledError, PdfziError
from pdfzi.core.progress import CancelToken, Progress
from pdfzi.tui.screens.base import BaseScreen, TitleBar
from pdfzi.tui.screens.result import Outcome, ResultScreen
from pdfzi.tui.tools import Runner, Tool


class RunScreen(BaseScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, tool: Tool, files: list[Path], runner: Runner) -> None:
        super().__init__()
        self.tool = tool
        self.files = files
        self.runner = runner
        self.cancel_token = CancelToken()
        self._last_update = 0.0

    def compose(self) -> ComposeResult:
        yield TitleBar()
        n = len(self.files)
        yield Static(
            f"{self.tool.title}: working on {n} file{'s' if n != 1 else ''}", classes="title"
        )
        yield Static("Starting...", id="current", classes="hint")
        yield ProgressBar(total=100, show_eta=False, id="bar")
        yield Static("Nothing is overwritten: results are new files.", id="note", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.set_title(f"{self.tool.title}: running")
        self.call_after_refresh(self._start)

    def _start(self) -> None:
        self.run_worker(self._work, thread=True, name="job", exit_on_error=False)

    # ---------------------------------------------------------------- worker thread

    def _work(self) -> None:
        started = time.monotonic()
        try:
            entries = self.runner(self.files, self._progress, self.cancel_token)
            outcome = Outcome(entries=entries, cancelled=self.cancel_token.cancelled)
        except OperationCancelledError:
            outcome = Outcome(cancelled=True)
        except PdfziError as exc:
            outcome = Outcome(fatal=str(exc))
        except Exception as exc:  # a bug: show a short message, never a traceback
            outcome = Outcome(fatal=f"Unexpected error: {exc}. This is a bug in pdfzi.", bug=True)
        outcome.seconds = time.monotonic() - started
        with contextlib.suppress(RuntimeError):  # the app may already be shutting down
            self.app.call_from_thread(self._finished, outcome)

    def _progress(self, p: Progress) -> None:
        now = time.monotonic()
        if now - self._last_update < 0.05 and p.done != p.total:
            return  # keep the screen responsive: at most ~20 updates a second
        self._last_update = now
        with contextlib.suppress(RuntimeError):
            self.app.call_from_thread(self._show, p)

    # ---------------------------------------------------------------- UI thread

    def _show(self, p: Progress) -> None:
        self.query_one("#current", Static).update(p.message or "Working...")
        self.query_one("#bar", ProgressBar).update(total=p.total or 100, progress=p.done)

    def _finished(self, outcome: Outcome) -> None:
        self.app.switch_screen(ResultScreen(self.tool, self.files, outcome))

    def action_cancel(self) -> None:
        if not self.cancel_token.cancelled:
            self.cancel_token.cancel()
            self.query_one("#note", Static).update("Cancelling... finishing the current step.")

    def action_quit_app(self) -> None:
        self.cancel_token.cancel()
        self.app.exit()
