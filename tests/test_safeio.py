from __future__ import annotations

import os
import stat
import sys

import pytest

from pdfgen import Snapshot, make_pdf, page_count
from pdfzi.core import safeio
from pdfzi.core.errors import OperationCancelledError, OutputWriteError
from pdfzi.core.pdfio import open_pdf, save_pdf
from pdfzi.core.progress import CancelToken
from pdfzi.core.safeio import safe_output


def leftovers(folder):
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_success_publishes_and_cleans_tmp(workdir):
    src = make_pdf(workdir / "ต้นฉบับ.pdf", pages=2)
    snap = Snapshot(src)
    with open_pdf(src) as pdf, safe_output(workdir / "ต้นฉบับ-new.pdf") as out:
        assert out.tmp.parent == workdir
        save_pdf(pdf, out.tmp)
    assert out.path == workdir / "ต้นฉบับ-new.pdf"
    assert page_count(out.path) == 2
    assert leftovers(workdir) == []
    snap.assert_unchanged()


def test_failure_leaves_no_output_and_no_tmp(workdir):
    dest = workdir / "out.pdf"
    with pytest.raises(RuntimeError), safe_output(dest) as out:
        out.tmp.write_bytes(b"partial")
        raise RuntimeError("boom")
    assert not dest.exists()
    assert leftovers(workdir) == []


def test_failure_does_not_touch_existing_destination(workdir):
    dest = make_pdf(workdir / "keep.pdf", pages=1)
    snap = Snapshot(dest)
    with pytest.raises(RuntimeError), safe_output(dest, force=True) as out:
        out.tmp.write_bytes(b"partial")
        raise RuntimeError("boom")
    snap.assert_unchanged()
    assert leftovers(workdir) == []


def test_cancellation_cleans_up(workdir):
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelledError), safe_output(workdir / "o.pdf") as out:
        out.tmp.write_bytes(b"partial")
        token.check()
    assert not (workdir / "o.pdf").exists()
    assert leftovers(workdir) == []


def test_existing_destination_is_never_replaced_without_force(workdir):
    dest = make_pdf(workdir / "o.pdf", pages=1)
    snap = Snapshot(dest)
    new = make_pdf(workdir / "new.pdf", pages=2)
    with safe_output(dest) as out:
        out.tmp.write_bytes(new.read_bytes())
    snap.assert_unchanged()
    assert out.path == workdir / "o-2.pdf"
    assert page_count(out.path) == 2


def test_force_replaces(workdir):
    dest = make_pdf(workdir / "o.pdf", pages=1)
    new = make_pdf(workdir / "new.pdf", pages=2)
    with safe_output(dest, force=True) as out:
        out.tmp.write_bytes(new.read_bytes())
    assert out.path == dest
    assert page_count(dest) == 2


def test_missing_parent_folders_are_created(workdir):
    with safe_output(workdir / "a" / "b" / "o.pdf") as out:
        out.tmp.write_bytes(b"x")
    assert out.path.read_bytes() == b"x"


def test_destination_that_is_a_folder_is_rejected(workdir):
    with pytest.raises(OutputWriteError, match="is a folder"), safe_output(workdir):
        pass


def test_replace_failure_is_friendly_and_cleans_up(workdir, monkeypatch):
    def deny(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(safeio, "_place_new", deny)
    with (
        pytest.raises(OutputWriteError, match="another program"),
        safe_output(workdir / "o.pdf") as out,
    ):
        out.tmp.write_bytes(b"x")
    assert leftovers(workdir) == []
    assert not (workdir / "o.pdf").exists()


def test_tmp_is_in_destination_folder_so_replace_is_atomic(workdir):
    sub = workdir / "sub"
    with safe_output(sub / "o.pdf") as out:
        assert out.tmp.parent.samefile(sub)


def test_temp_names_are_unique_and_the_file_starts_empty(workdir):
    (first, _), (second, _) = safeio._new_temp_file(workdir), safeio._new_temp_file(workdir)
    assert first != second
    assert first.parent == workdir and first.stat().st_size == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_temp_file_is_created_private_not_made_private_afterwards(workdir, monkeypatch):
    # Someone who opens a file in the moment it is readable keeps reading it through
    # that handle, whatever its mode becomes later.
    modes = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        modes.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    umask = os.umask(0)  # the most permissive umask
    try:
        path, usual = safeio._new_temp_file(workdir)
    finally:
        os.umask(umask)
    assert modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert usual == 0o666


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_result_gets_the_mode_a_new_file_normally_gets(workdir):
    umask = os.umask(0o022)  # read the current umask (and set a known one for this test)
    try:
        with safe_output(workdir / "out.pdf") as out:
            out.tmp.write_bytes(b"%PDF-1.7")
    finally:
        os.umask(umask)
    assert stat.S_IMODE(out.path.stat().st_mode) == 0o644  # not the private 0600 of mkstemp
