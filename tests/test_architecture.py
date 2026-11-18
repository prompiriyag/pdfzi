"""Static guards for the project's hard rules (no network, layering, license hygiene)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "pdfzi"

NETWORK_MODULES = {
    "socket", "ssl", "urllib", "urllib3", "http", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc", "socketserver", "requests", "httpx", "aiohttp", "websockets",
    "webbrowser", "asyncio.streams",
}  # fmt: skip
BANNED_LICENSE_MODULES = {"fitz", "pymupdf"}  # AGPL; see README "Dependency licenses"
# img2pdf is LGPL-3.0: it may only be imported from this one file.
IMG2PDF_ALLOWED = {Path("core/imgtopdf.py")}


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def all_sources():
    files = sorted(SRC.rglob("*.py"))
    assert files, "no sources found - SRC path is wrong"
    return files


def top(name: str) -> str:
    return name.split(".")[0]


def test_no_network_imports_in_pdfzi():
    offenders = {
        str(f.relative_to(SRC)): sorted(n for n in imports_of(f) if top(n) in NETWORK_MODULES)
        for f in all_sources()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"network modules imported: {offenders}"


def test_core_does_not_import_ui_libraries():
    offenders = []
    for f in (SRC / "core").rglob("*.py"):
        offenders += [
            f"{f.name}: {n}"
            for n in imports_of(f)
            if top(n) in {"textual", "typer", "click", "rich"}
        ]
        offenders += [
            f"{f.name}: {n}"
            for n in imports_of(f)
            if n in {"pdfzi.cli"} or n.startswith("pdfzi.tui")
        ]
    assert not offenders, offenders


def test_no_agpl_gpl_pdf_libraries():
    offenders = [
        f"{f.relative_to(SRC)}: {n}"
        for f in all_sources()
        for n in imports_of(f)
        if top(n) in BANNED_LICENSE_MODULES
    ]
    assert not offenders, offenders


def test_img2pdf_only_imported_from_imgtopdf():
    offenders = [
        str(f.relative_to(SRC))
        for f in all_sources()
        if "img2pdf" in {top(n) for n in imports_of(f)}
        and f.relative_to(SRC) not in IMG2PDF_ALLOWED
    ]
    assert not offenders, offenders
