from __future__ import annotations

import pytest

from pdfzi.core.errors import OperationCancelledError
from pdfzi.core.fmt import format_change, format_size, plural
from pdfzi.core.progress import CancelToken, Progress, report
from pdfzi.core.results import OperationResult


@pytest.mark.parametrize(
    ("n", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"), (5 * 1024**2, "5.0 MB")],
)
def test_format_size(n, text):
    assert format_size(n) == text


def test_format_change():
    assert format_change(4 * 1024**2, 1024**2) == "4.0 MB → 1.0 MB (75% smaller)"
    assert format_change(1000, 1100) == "1000 B → 1.1 KB (10% larger)"
    assert format_change(1000, 1000) == "1000 B → 1000 B (unchanged)"
    assert format_change(0, 0).endswith("(unchanged)")


def test_report_and_cancel():
    seen: list[Progress] = []
    token = CancelToken()
    report(seen.append, 1, 4, "working", token)
    assert seen == [Progress(1, 4, "working")]
    assert seen[0].fraction == 0.25
    assert Progress(0, 0).fraction is None
    token.cancel()
    with pytest.raises(OperationCancelledError):
        report(seen.append, 2, 4, "more", token)
    assert len(seen) == 1  # nothing reported after cancellation


def test_report_without_listeners_is_a_noop():
    report(None, 1, 1)


def test_result_to_dict_is_json_friendly(tmp_path):
    import json

    r = OperationResult(
        command="merge", sources=[tmp_path / "ก.pdf"], outputs=[tmp_path / "o.pdf"], bytes_in=10
    )
    data = json.loads(json.dumps(r.to_dict(), ensure_ascii=False))
    assert data["command"] == "merge"
    assert data["sources"][0].endswith("ก.pdf")
    assert r.size_summary is None


def test_plural():
    assert plural(1, "page") == "1 page" and plural(0, "page") == "0 pages"
    assert plural(3, "entry", "entries") == "3 entries"
    assert plural(1, "picture has", "pictures have") == "1 picture has"
