"""v10.12 — file upload parsing (PDF / Markdown / code / text), not just URL scraping.

Covers the real extraction paths:
  * PDF parsed by pypdf -> plain text ingested + queryable (proves the PDF path works,
    not just the missing-parser 400 path).
  * code file auto-detected by extension -> stored as content_type 'code'.
  * HTML upload stripped to text.
  * docx with python-docx missing -> clear actionable 400 (not 500).
  * re-upload of the same bytes (no doc_id) replaces via content-hash dedup (v10.10).
  * unit-level checks on detect_content_type / extract_text for routing correctness.
"""
import io

import pytest

V = "/api/v1"
ADMIN = {"Admin-Key": "test-admin-key-for-tests"}


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _make_tenant(client, name="up"):
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# Build a REAL, valid PDF (with a defined font) whose text pypdf can extract — so we
# exercise the genuine extraction path, not just the missing-parser 400 branch.
def _make_pdf_text() -> bytes:
    import io

    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject()
    font.update(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    res = DictionaryObject()
    res_fonts = DictionaryObject()
    res_fonts.update({NameObject("/F1"): font_ref})
    res.update({NameObject("/Font"): res_fonts})
    page[NameObject("/Resources")] = res
    stream = DecodedStreamObject()
    data = b"BT /F1 12 Tf 72 720 Td (Quarterly revenue grew to 4.2M this quarter.) Tj ET"
    stream.set_data(data)
    stream.update({NameObject("/Length"): NumberObject(len(data))})
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


_PDF_BYTES = _make_pdf_text()

_CODE = b"def add(a, b):\n    return a + b  # simple helper\n"

_HTML = b"<html><head><title>T</title></head><body><h1>Hello</h1><p>World content here.</p></body></html>"


def test_upload_pdf_parsed_and_queryable(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = client.post(
        f"{V}/{t['tenant_id']}/documents/upload", headers=auth,
        files={"file": ("report.pdf", _PDF_BYTES, "application/pdf")},
        data={"title": "Q3 report"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content_type"] == "pdf"
    assert body["chunk_count"] >= 1

    q = client.post(
        f"{V}/{t['tenant_id']}/query", headers=auth,
        json={"question": "revenue", "generate": False, "top_k": 5},
    )
    snippets = " ".join(h["text"] for h in q.json()["results"])
    assert "4.2M" in snippets, f"PDF text not retrieved: {snippets[:200]}"


def test_upload_code_file_detected(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = client.post(
        f"{V}/{t['tenant_id']}/documents/upload", headers=auth,
        files={"file": ("helper.py", _CODE, "text/x-python")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "code"
    assert r.json()["chunk_count"] >= 1


def test_upload_html_stripped(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = client.post(
        f"{V}/{t['tenant_id']}/documents/upload", headers=auth,
        files={"file": ("page.html", _HTML, "text/html")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "html"
    # The <h1>/<p> text must be present; tags stripped.
    q = client.post(
        f"{V}/{t['tenant_id']}/query", headers=auth,
        json={"question": "hello", "generate": False, "top_k": 5},
    )
    snippets = " ".join(h["text"] for h in q.json()["results"])
    assert "Hello" in snippets and "World" in snippets, snippets[:200]


def test_upload_docx_missing_parser_clear_400(client, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    docx = b"PK\x03\x04 fake docx bytes"
    r = client.post(
        f"{V}/{t['tenant_id']}/documents/upload", headers=auth,
        files={"file": ("memo.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r.status_code == 400, r.text
    assert "python-docx" in r.text.lower()


def test_upload_same_bytes_reupload_dedups_without_doc_id(client):
    """Re-uploading identical bytes (no doc_id) must replace, not duplicate (v10.10)."""
    import app.ingestion.files as files

    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    txt = b"Onboarding runbook: step one create workspace, step two invite team.\n"

    r1 = client.post(
        f"{V}/{t['tenant_id']}/documents/upload", headers=auth,
        files={"file": ("runbook.txt", txt, "text/plain")},
    )
    assert r1.status_code == 201, r1.text
    doc_id = r1.json()["doc_id"]

    r2 = client.post(
        f"{V}/{t['tenant_id']}/documents/upload", headers=auth,
        files={"file": ("runbook.txt", txt, "text/plain")},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["doc_id"] == doc_id, "same bytes must reuse doc_id (content-hash dedup)"

    cat = client.get(f"{V}/{t['tenant_id']}/documents", headers=auth).json()
    rows = [d for d in cat if d["doc_id"] == doc_id]
    assert len(rows) == 1, f"duplicate rows: {rows}"


def test_detect_content_type_unit():
    import app.ingestion.files as files

    assert files.detect_content_type("a.md", None) == "markdown"
    assert files.detect_content_type("a.txt", None) == "text"
    assert files.detect_content_type("a.pdf", "application/pdf") == "pdf"
    assert files.detect_content_type("a.py", None) == "code"
    assert files.detect_content_type("a.html", "text/html") == "html"
    # explicit pipeline type wins
    assert files.detect_content_type("weirdname", "markdown") == "markdown"
    # unknown -> text default
    assert files.detect_content_type("data.bin", "application/octet-stream") == "text"


def test_extract_pdf_unit():
    import app.ingestion.files as files

    text = files.extract_text("report.pdf", _PDF_BYTES, "pdf")
    assert "4.2M" in text
