from __future__ import annotations

import pytest

from conftest import TRICKY_NAMES
from pdfzi.core.errors import InputNotFoundError, NotAFileError
from pdfzi.core.paths import (
    PDF_SUFFIXES,
    expand_inputs,
    natural_key,
    output_path_for,
    unique_path,
)


def touch(path):
    path.write_bytes(b"x")
    return path


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_explicit_tricky_names_resolve_literally(workdir, name):
    f = touch(workdir / name)
    assert expand_inputs([f]) == [f]
    assert expand_inputs([str(f)]) == [f]


def test_glob_is_expanded_by_us_and_naturally_sorted(workdir):
    for n in ("p10.pdf", "p2.pdf", "p1.pdf", "ไทย.pdf"):
        touch(workdir / n)
    touch(workdir / "notes.txt")
    found = expand_inputs([str(workdir / "*.pdf")], suffixes=PDF_SUFFIXES)
    assert [p.name for p in found[:3]] == ["p1.pdf", "p2.pdf", "p10.pdf"]
    assert "ไทย.pdf" in {p.name for p in found}
    assert "notes.txt" not in {p.name for p in found}


def test_glob_filter_is_case_insensitive_on_suffix(workdir):
    touch(workdir / "A.PDF")
    assert [p.name for p in expand_inputs([str(workdir / "*")], suffixes=PDF_SUFFIXES)] == ["A.PDF"]


def test_dedupes_across_arguments(workdir):
    f = touch(workdir / "a.pdf")
    assert expand_inputs([f, str(workdir / "*.pdf"), f]) == [f]


def test_no_glob_match_is_a_friendly_error(workdir):
    with pytest.raises(InputNotFoundError, match="No files match"):
        expand_inputs([str(workdir / "*.pdf")])


def test_missing_file(workdir):
    with pytest.raises(InputNotFoundError, match="File not found"):
        expand_inputs([workdir / "nope.pdf"])


def test_folder_is_rejected(workdir):
    with pytest.raises(NotAFileError):
        expand_inputs([workdir])


def test_natural_key():
    from pathlib import Path

    names = ["b2.pdf", "B10.pdf", "b1.pdf"]
    assert [p.name for p in sorted(map(Path, names), key=natural_key)] == [
        "b1.pdf",
        "b2.pdf",
        "B10.pdf",
    ]


def test_unique_path_numbers_without_overwriting(workdir):
    base = workdir / "รายงาน-compressed.pdf"
    assert unique_path(base) == base
    touch(base)
    assert unique_path(base) == workdir / "รายงาน-compressed-2.pdf"
    touch(workdir / "รายงาน-compressed-2.pdf")
    assert unique_path(base) == workdir / "รายงาน-compressed-3.pdf"


def test_output_path_default_is_sibling(workdir):
    src = workdir / "my file.pdf"
    assert output_path_for(src, "compressed") == workdir / "my file-compressed.pdf"
    assert output_path_for(src, "pages", ext=".png") == workdir / "my file-pages.png"


def test_output_path_into_existing_folder(workdir):
    out = workdir / "out"
    out.mkdir()
    assert output_path_for(workdir / "a.pdf", "merged", output=out) == out / "a-merged.pdf"


def test_output_path_trailing_separator_means_folder(workdir):
    for sep in ("/", "\\"):
        result = output_path_for(workdir / "a.pdf", "x", output=str(workdir / "new") + sep)
        assert result == workdir / "new" / "a-x.pdf"


def test_output_path_explicit_file(workdir):
    target = workdir / "final.pdf"
    assert output_path_for(workdir / "a.pdf", "x", output=target) == target
