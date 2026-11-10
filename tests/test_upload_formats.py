"""Every upload format the UI promises: PDF (text and scanned), DOCX, MD, CSV, XLSX and images (OCR)."""
import io

import pytest

from sra.evidence.bundle import EvidenceBundle
from sra.evidence.model import Kind

RUNBOOK = ["Runbook: checkout-api", "Escalation: page the on-call SRE", "Rollback: redeploy the previous release"]


def _image(lines, fmt="JPEG") -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1000, 80 + 70 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default(size=34)
    for i, line in enumerate(lines):
        d.text((30, 30 + 70 * i), line, fill="black", font=font)
    b = io.BytesIO()
    img.save(b, fmt)
    return b.getvalue()


def _text_pdf(lines) -> bytes:
    """A minimal one-page PDF with real text (no PDF writer library needed)."""
    body = "BT /F1 14 Tf 40 760 Td " + " ".join(f"({s}) Tj 0 -22 Td" for s in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(body)} >>\nstream\n{body}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _ingest(name, data):
    b = EvidenceBundle()
    item = b.ingest(name, data)
    return b, item


def test_pdf_with_text():
    b, item = _ingest("checkout_runbook.pdf", _text_pdf(RUNBOOK))
    assert item and item.kind == Kind.DOCUMENT and not b.errors
    assert "on-call SRE" in item.text and not item.meta.get("images_read")
    assert item.doc_type.value == "runbook"


@pytest.mark.parametrize("name,fmt", [("dashboard.jpg", "JPEG"), ("dashboard.png", "PNG")])
def test_image_is_read_with_ocr(name, fmt):
    b, item = _ingest(name, _image(RUNBOOK, fmt))
    assert item and item.kind == Kind.DOCUMENT and item.meta.get("ocr") is True and not b.errors
    flat = item.text.replace(" ", "")  # the bundled OCR model sometimes drops a space between words
    assert "on-callSRE" in flat and "previousrelease" in flat
    assert item.text.index("Escalation") < item.text.index("Rollback")  # top to bottom


def test_scanned_pdf_is_read_with_ocr():
    from PIL import Image
    pdf = io.BytesIO()
    Image.open(io.BytesIO(_image(RUNBOOK))).save(pdf, "PDF")
    b, item = _ingest("scanned_runbook.pdf", pdf.getvalue())
    assert item and item.meta.get("images_read") == 1 and not b.errors
    assert "on-callSRE" in item.text.replace(" ", "")


def test_blank_image_is_kept_with_a_note():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, "JPEG")
    b, item = _ingest("photo.jpg", buf.getvalue())
    assert item is not None and item.text == ""
    assert b.errors and "no readable text" in b.errors[0]


def test_secrets_in_images_are_redacted():
    b, item = _ingest("config.png", _image(["password=Hunter2Secret99"], "PNG"))
    assert "Hunter2Secret99" not in item.text and item.meta["raw_secret_scan"]


def test_docx_md_csv_xlsx_still_accepted():
    import docx
    from openpyxl import Workbook
    d = docx.Document()
    for line in RUNBOOK:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    wb = Workbook()
    ws = wb.active
    ws.append(["number", "short_description", "opened_at", "priority"])
    ws.append(["INC0001", "checkout down", "2026-10-01 10:00:00", "2"])
    xbuf = io.BytesIO()
    wb.save(xbuf)
    files = {"runbook.docx": buf.getvalue(), "runbook.md": "\n".join(RUNBOOK).encode(),
             "incident.csv": b"number,short_description,opened_at,priority\nINC0001,checkout down,2026-10-01 10:00:00,2\n",
             "incident.xlsx": xbuf.getvalue()}
    b = EvidenceBundle()
    items = {n: b.ingest(n, data) for n, data in files.items()}
    assert not b.errors and all(items.values())
    assert items["runbook.docx"].kind == items["runbook.md"].kind == Kind.DOCUMENT
    assert items["incident.csv"].kind == items["incident.xlsx"].kind == Kind.SN_TABLE
    assert len(b.table("incident")) == 2


# ---------------------------------------------------------------- images embedded in documents
DIAGRAM = ["checkout-api", "Postgres primary", "Postgres replica"]


def _docx_with_pictures(*items) -> bytes:
    """items: str (a paragraph) or bytes (a picture), in order."""
    import docx
    from docx.shared import Inches
    d = docx.Document()
    for it in items:
        if isinstance(it, str):
            d.add_paragraph(it)
        else:
            d.add_picture(io.BytesIO(it), width=Inches(5))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _logo() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), "navy").save(buf, "PNG")
    return buf.getvalue()


def test_docx_pictures_are_read_in_place():
    data = _docx_with_pictures("Architecture overview", _image(DIAGRAM, "PNG"), "Failover is manual.", _logo())
    b, item = _ingest("architecture.docx", data)
    t = item.text
    assert not b.errors and item.meta["images_read"] == 1 and item.meta["images_small"] == 1
    flat = t.replace(" ", "")
    assert "Postgresreplica" in flat and "[Image image" in t and "[End of image]" in t
    assert t.index("Architecture overview") < t.index("[Image") < t.index("Failover is manual.")


def test_docx_duplicate_picture_read_once_and_limit(monkeypatch):
    pic = _image(DIAGRAM, "PNG")
    other = _image(["Grafana error rate 0.2%"], "PNG")
    b, item = _ingest("dup.docx", _docx_with_pictures(pic, "text", pic))
    assert item.meta["images_read"] == 1 and item.text.count("[Image ") == 1
    monkeypatch.setenv("SRA_OCR_MAX_IMAGES", "1")
    b, item = _ingest("limit.docx", _docx_with_pictures(pic, other))
    assert item.meta["images_read"] == 1 and item.meta["images_over_limit"] == 1
    assert "not read" in item.text and any("per-document limit" in e for e in b.errors)


def test_ocr_off_notes_images(monkeypatch):
    monkeypatch.setenv("SRA_OCR", "false")
    b, item = _ingest("off.docx", _docx_with_pictures("Intro", _image(DIAGRAM, "PNG")))
    assert "Postgres" not in item.text and "OCR is off" in item.text and not item.meta.get("images_read")


def test_vector_drawings_are_noted():
    from sra.evidence.parsers import ImageReader
    r = ImageReader()
    assert "vector drawing" in r.section("image3.emf", b"\x01\x00\x00\x00" * 100)
    assert any("cannot read" in n for n in r.meta()["image_notes"])


def test_embedded_visio_object_is_noted():
    import zipfile
    data = _docx_with_pictures("Topology below")
    src, out = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename == "word/document.xml":
                obj = (b'<w:p><w:r><w:object xmlns:o="urn:schemas-microsoft-com:office:office">'
                       b'<o:OLEObject Type="Embed" ProgID="Visio.Drawing.15" ShapeID="_x0000_i1025"/></w:object></w:r></w:p>')
                body = body.replace(b"<w:sectPr", obj + b"<w:sectPr", 1)
            z.writestr(info, body)
    b, item = _ingest("topology.docx", out.getvalue())
    assert "Visio.Drawing.15" in item.text and ".vsdx" in item.text and item.meta["embedded_objects"] == 1


def test_pdf_with_text_and_a_diagram_page():
    from PIL import Image
    from pypdf import PdfReader, PdfWriter
    img_pdf = io.BytesIO()
    Image.open(io.BytesIO(_image(DIAGRAM))).save(img_pdf, "PDF")
    w = PdfWriter()
    for src in (_text_pdf(RUNBOOK), img_pdf.getvalue()):
        for page in PdfReader(io.BytesIO(src)).pages:
            w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    b, item = _ingest("runbook_with_diagram.pdf", buf.getvalue())
    assert not b.errors and item.meta["images_read"] == 1
    assert "on-call SRE" in item.text and "Postgresreplica" in item.text.replace(" ", "")
    assert item.text.index("on-call SRE") < item.text.index("[Image page 2")


def test_ocr_time_budget(monkeypatch):
    monkeypatch.setenv("SRA_OCR_MAX_SECONDS", "0.001")   # the first image always uses it up
    b, item = _ingest("slow.docx", _docx_with_pictures(_image(DIAGRAM, "PNG"), _image(["Grafana error rate 0.2%"], "PNG")))
    assert item.meta["images_read"] == 1 and item.meta["images_over_limit"] == 1
    assert any("time limit" in e for e in b.errors)


@pytest.fixture(autouse=True)
def _private_ocr_cache(tmp_path, monkeypatch):
    """Each test gets an empty OCR cache in its own folder (the real one lives under SRA_DATA_DIR)."""
    from sra.evidence import parsers
    monkeypatch.setattr(parsers, "_MEM", {})
    monkeypatch.setattr(parsers, "_ocr_cache_dir", lambda: tmp_path / "ocr_cache")


def test_ocr_text_is_cached(monkeypatch, tmp_path):
    from sra.evidence import parsers
    pic = _image(DIAGRAM, "PNG")
    first = parsers.image_to_text(pic)
    assert list((tmp_path / "ocr_cache").glob("*.txt"))
    monkeypatch.setattr(parsers, "_MEM", {})                       # a restart: only the disk cache is left

    class Boom:
        def __call__(self, *_):
            raise AssertionError("OCR ran again")
    monkeypatch.setattr(parsers, "_OCR", Boom())
    assert parsers.image_to_text(pic) == first
