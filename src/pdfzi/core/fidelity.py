"""Things a rewritten PDF can lose that the user should be told about.

Every operation writes a new file. Two properties of the *original* cannot survive that:

* a digital signature (it signs the exact bytes of the original), and
* PDF/A conformance (an archival profile; rewriting is not guaranteed to keep it).

:func:`fidelity_notes` looks at the opened source and returns sentences for the result.
"""

from __future__ import annotations

import pikepdf

SIGNED_NOTE = (
    "This PDF is digitally signed. The new file is a changed copy, so the signature "
    "is not valid in it."
)
PDFA_NOTE = (
    "This PDF declares PDF/A (archival) conformance. The new file is not guaranteed to "
    "still conform."
)
_MAX_FIELDS = 100_000  # a safety stop for absurd form trees


def has_signature(pdf: pikepdf.Pdf) -> bool:
    """True if the document contains a filled-in digital signature field."""
    try:
        acro = pdf.Root.get("/AcroForm")
        if acro is None:
            return False
        if int(acro.get("/SigFlags", 0)) & 1:  # bit 1: signatures exist
            return True
        stack = list(acro.get("/Fields", []))
        seen: set[tuple[int, int]] = set()
        while stack and len(seen) < _MAX_FIELDS:
            field = stack.pop()
            if field.objgen != (0, 0):
                if field.objgen in seen:
                    continue
                seen.add(field.objgen)
            if field.get("/FT") == "/Sig" and field.get("/V") is not None:
                return True
            stack.extend(field.get("/Kids", []))
    except (pikepdf.PdfError, ValueError, TypeError, AttributeError):
        return False
    return False


def claims_pdfa(pdf: pikepdf.Pdf) -> bool:
    """True if the XMP metadata carries a PDF/A identification (``pdfaid:part``)."""
    try:
        metadata = pdf.Root.get("/Metadata")
        return metadata is not None and b"pdfaid:part" in bytes(metadata.read_bytes())
    except (pikepdf.PdfError, ValueError, TypeError, AttributeError):
        return False


def fidelity_notes(pdf: pikepdf.Pdf) -> list[str]:
    """Warnings for the result of rewriting ``pdf`` (empty for an ordinary document)."""
    notes = []
    if has_signature(pdf):
        notes.append(SIGNED_NOTE)
    if claims_pdfa(pdf):
        notes.append(PDFA_NOTE)
    return notes
