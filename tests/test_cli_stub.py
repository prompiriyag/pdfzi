from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from pdfzi import __version__, cli
from pdfzi.core.errors import PasswordRequiredError

runner = CliRunner()


def test_version():
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"pdfzi {__version__}"


def test_user_errors_print_one_line_not_a_traceback(monkeypatch, capsys, tmp_path):
    def fail():
        raise PasswordRequiredError(tmp_path / "ลับ.pdf")

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ลับ.pdf") or "password-protected" in err
    assert "Traceback" not in err


def test_unexpected_errors_are_summarised(monkeypatch, capsys):
    def fail():
        raise ValueError("kaboom")

    monkeypatch.setattr(cli, "app", fail)
    monkeypatch.delenv("PDFZI_DEBUG", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "kaboom" in err
    assert "Traceback" not in err


def test_no_arguments_opens_the_tui_on_a_terminal(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("pdfzi.tui.app.run", lambda *a, **k: opened.append(True))
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0 and opened == [True]


def test_no_arguments_without_a_terminal_shows_help(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr("pdfzi.tui.app.run", lambda *a, **k: opened.append(True))
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0 and opened == []
    # On GitHub Actions Typer forces colour, so drop the ANSI codes before comparing text.
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "Usage: pdfzi" in text and "needs a terminal" in text


def test_cli_commands_do_not_load_the_tui_library():
    """`pdfzi compress ...` should start fast: Textual is only imported for the TUI."""
    import subprocess
    import sys

    code = "import sys, pdfzi.cli; print('textual' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"
