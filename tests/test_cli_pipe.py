"""Output piped into a program that stops reading (``pdfzi info *.pdf | head``)."""

from __future__ import annotations

import shutil
import subprocess
import sys

from pdfgen import make_pdf


def test_a_closed_pipe_ends_quietly(workdir):
    first = make_pdf(workdir / "f000.pdf", pages=2)
    names = [first.name]
    for i in range(1, 300):  # plenty of output: the pipe is still being written to
        shutil.copy(first, workdir / f"f{i:03d}.pdf")
        names.append(f"f{i:03d}.pdf")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pdfzi", "info", *names],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.readline()
    proc.stdout.close()  # the reader leaves, like `head` does
    err = proc.stderr.read()
    proc.wait(timeout=60)
    proc.stderr.close()
    assert err == "", err  # no "Unexpected error ... a bug in pdfzi"
