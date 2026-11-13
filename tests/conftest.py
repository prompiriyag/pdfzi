from __future__ import annotations

from pathlib import Path

import pytest

# Names that exercise Unicode (Thai), spaces, and brackets (glob metacharacters).
TRICKY_NAMES = ["รายงาน ประจำปี.pdf", "my file (1).pdf", "report[1].pdf", "ñandú ü.pdf"]


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "งาน เอกสาร"  # Unicode + space in the folder too
    d.mkdir()
    return d
