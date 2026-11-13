"""Warnings about what rewriting a PDF cannot keep: digital signatures and PDF/A."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name

from pdfgen import make_pdf
from pdfzi.core.clean import clean_pdf
from pdfzi.core.compress import compress_pdf
from pdfzi.core.fidelity import PDFA_NOTE, SIGNED_NOTE, claims_pdfa, fidelity_notes, has_signature
from pdfzi.core.merge import merge_pdfs
from pdfzi.core.protect import lock_pdf
from pdfzi.core.rotate import rotate_pdf
from pdfzi.core.subset import delete_pages, extract_pages, split_pdf

XMP = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" pdfaid:part="1" '
    b'pdfaid:conformance="B"/></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def signed(path: Path, *, flags_only: bool = False) -> Path:
    """A PDF with a (fake, unverifiable) signature field holding a value."""
    src = make_pdf(path.with_name("plain-" + path.name), pages=3, images_per_page=1)
    with pikepdf.open(src) as pdf:
        if flags_only:
            pdf.Root.AcroForm = Dictionary(Fields=[], SigFlags=3)
        else:
            sig = pdf.make_indirect(
                Dictionary(Type=Name.Sig, Filter=Name.Adobe_PPKLite, ByteRange=[0, 1, 2, 3])
            )
            field = pdf.make_indirect(
                Dictionary(FT=Name.Sig, T=pikepdf.String("Signature1"), V=sig)
            )
            parent = pdf.make_indirect(Dictionary(T=pikepdf.String("group"), Kids=[field]))
            pdf.Root.AcroForm = Dictionary(Fields=[parent])  # nested, as real forms are
        pdf.docinfo["/Author"] = "someone"  # so that clean has something to remove
        pdf.save(path)
    return path


def pdfa(path: Path) -> Path:
    src = make_pdf(path.with_name("plain-" + path.name), pages=3, images_per_page=1)
    with pikepdf.open(src) as pdf:
        pdf.Root.Metadata = pdf.make_stream(XMP, Type=Name.Metadata, Subtype=Name.XML)
        pdf.docinfo["/Author"] = "someone"  # so that clean has something to remove
        pdf.save(path)
    return path


def test_detection(tmp_path):
    plain = make_pdf(tmp_path / "plain.pdf", pages=2)
    with pikepdf.open(plain) as pdf:
        assert fidelity_notes(pdf) == []
    with pikepdf.open(signed(tmp_path / "s.pdf")) as pdf:
        assert has_signature(pdf) and fidelity_notes(pdf) == [SIGNED_NOTE]
    with pikepdf.open(signed(tmp_path / "f.pdf", flags_only=True)) as pdf:
        assert has_signature(pdf)  # SigFlags says signatures exist
    with pikepdf.open(pdfa(tmp_path / "a.pdf")) as pdf:
        assert claims_pdfa(pdf) and fidelity_notes(pdf) == [PDFA_NOTE]


def test_an_empty_signature_field_is_not_a_signature(tmp_path):
    src = make_pdf(tmp_path / "blank.pdf", pages=1)
    with pikepdf.open(src) as pdf:
        field = pdf.make_indirect(Dictionary(FT=Name.Sig, T=pikepdf.String("Sign here")))
        pdf.Root.AcroForm = Dictionary(Fields=[field])
        assert has_signature(pdf) is False  # nothing is signed yet


def test_a_form_tree_that_loops_does_not_hang(tmp_path):
    src = make_pdf(tmp_path / "loop.pdf", pages=1)
    with pikepdf.open(src) as pdf:
        a = pdf.make_indirect(Dictionary(T=pikepdf.String("a")))
        a.Kids = [a]
        pdf.Root.AcroForm = Dictionary(Fields=[a])
        assert has_signature(pdf) is False


OPERATIONS = {
    "compress": lambda s, d: compress_pdf(s, d, level="medium"),
    "rotate": lambda s, d: rotate_pdf(s, 90, None, d),
    "extract": lambda s, d: extract_pages(s, "1-2", d),
    "delete": lambda s, d: delete_pages(s, "1", d),
    "split": lambda s, d: split_pdf(s, every=1, out_dir=d.with_suffix("")),
    "merge": lambda s, d: merge_pdfs([s, s], d),
    "lock": lambda s, d: lock_pdf(s, "pw", d),
    "clean": lambda s, d: clean_pdf(s, d),
}


@pytest.mark.parametrize("op", sorted(OPERATIONS))
def test_every_operation_warns_about_a_signature(tmp_path, op):
    r = OPERATIONS[op](signed(tmp_path / "s.pdf"), tmp_path / "out.pdf")
    assert SIGNED_NOTE in r.notes, (op, r.notes)


@pytest.mark.parametrize("op", sorted(OPERATIONS))
def test_every_operation_warns_about_pdfa(tmp_path, op):
    r = OPERATIONS[op](pdfa(tmp_path / "a.pdf"), tmp_path / "out.pdf")
    assert PDFA_NOTE in r.notes, (op, r.notes)


@pytest.mark.parametrize("op", sorted(OPERATIONS))
def test_ordinary_files_get_no_warning(tmp_path, op):
    src = make_pdf(tmp_path / "plain.pdf", pages=3, images_per_page=1)
    r = OPERATIONS[op](src, tmp_path / "out.pdf")
    assert SIGNED_NOTE not in r.notes and PDFA_NOTE not in r.notes


# ------------------------------------------------------------------ PDF/A keeps its structure


def uncompressed(path: Path) -> Path:
    """Re-save without stream compression or object streams, so there is something to gain."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.Padding = pdf.make_stream(b"compressible text " * 4000)  # something to squeeze
        pdf.save(
            path,
            compress_streams=False,
            stream_decode_level=pikepdf.StreamDecodeLevel.generalized,  # really uncompressed
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
            fix_metadata_version=False,
        )
    return path


def pdfa_with_version_field(path: Path) -> Path:
    """A PDF/A-marked file whose XMP also has the pdf:PDFVersion field pikepdf likes to fix."""
    xmp = XMP.replace(
        b"</rdf:RDF>",
        b'<rdf:Description xmlns:pdf="http://ns.adobe.com/pdf/1.3/">'
        b"<pdf:PDFVersion>1.3</pdf:PDFVersion></rdf:Description></rdf:RDF>",
    )
    make_pdf(path, pages=30)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.Metadata = pdf.make_stream(xmp, Type=Name.Metadata, Subtype=Name.XML)
        pdf.Root.Padding = pdf.make_stream(b"compressible text " * 4000)
        pdf.save(
            path,
            compress_streams=False,
            stream_decode_level=pikepdf.StreamDecodeLevel.generalized,  # really uncompressed
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
            fix_metadata_version=False,
        )
    return path


def test_compress_writes_pdfa_without_object_streams(tmp_path):
    archival = pdfa_with_version_field(tmp_path / "a.pdf")
    ordinary = uncompressed(make_pdf(tmp_path / "o.pdf", pages=30))
    a = compress_pdf(archival, tmp_path / "a-out.pdf").outputs[0]
    o = compress_pdf(ordinary, tmp_path / "o-out.pdf").outputs[0]
    assert b"/ObjStm" not in a.read_bytes() and b"/XRef" not in a.read_bytes()  # PDF/A-1 forbids
    assert b"/ObjStm" in o.read_bytes()  # an ordinary file still gets the smaller layout
    assert a.stat().st_size < archival.stat().st_size  # and it was still made smaller


def test_the_xmp_packet_is_left_byte_for_byte_alone(tmp_path):
    src = pdfa_with_version_field(tmp_path / "a.pdf")
    with pikepdf.open(src) as pdf:
        original = bytes(pdf.Root.Metadata.read_bytes())
    results = {
        "compress": compress_pdf(src, tmp_path / "c.pdf"),
        "rotate": rotate_pdf(src, 90, None, tmp_path / "r.pdf"),
        "extract-all": clean_pdf(src, tmp_path / "x.pdf"),  # clean removes XMP on purpose
    }
    for name in ("compress", "rotate"):
        with pikepdf.open(results[name].outputs[0]) as pdf:
            assert bytes(pdf.Root.Metadata.read_bytes()) == original, name
    # clean is the one operation that is meant to remove it
    with pikepdf.open(results["extract-all"].outputs[0]) as pdf:
        assert "/Metadata" not in pdf.Root
