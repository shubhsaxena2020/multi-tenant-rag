"""Issue #10 — PDF/Markdown/file upload parsing.

Covers the upload pipeline end-to-end through the TestClient: extraction correctness,
idempotent re-upload (issue #4 per-file doc_key), empty-file guard, and the clear error
surfaced when a binary parser (pypdf) is missing.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.ingestion import files as fmod

V = "/api/v1"


def _make_tenant(client, name="acme"):
    headers = {"Admin-Key": "test-admin-key-for-tests"}
    r = client.post(f"{V}/tenants", json={"name": name, "plan": "standard"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _pdf_bytes(text: str = "Alpha beta gamma. Retrieval quality matters.") -> bytes:
    """Build a minimal dependency-free one-page PDF whose text is extractable by pypdf.

    We construct the raw PDF by hand (no reportlab needed) so the test never depends on a
    PDF-generation library — only on the parser (`pypdf`), which is what we're testing.
    """
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 50 750 Td ({esc}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return out


def _upload(client, auth, fname, data, **extra):
    return client.post(
        f"{V}/acme/documents/upload",
        headers=auth,
        files={"file": (fname, data)},
        data=extra,
    )


def test_upload_markdown_ingests_and_is_queryable(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    md = "# Onboarding\nUpload a markdown file to index knowledge.\n"
    r = _upload(client, auth, "guide.md", md.encode())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content_type"] == "markdown"
    assert body["chunk_count"] >= 1
    # The extracted text must be retrievable.
    q = client.post(f"{V}/acme/query", headers=auth,
                    json={"question": "how do I onboard?", "top_k": 3})
    assert any("onboard" in (h.get("text") or "").lower() for h in q.json()["results"]), q.json()


def test_upload_code_file(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    code = "def add(a, b):\n    return a + b\n"
    r = _upload(client, auth, "calc.py", code.encode())
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "code"
    assert r.json()["chunk_count"] >= 1


def test_upload_pdf_extracts_real_text(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = _upload(client, auth, "whitepaper.pdf", _pdf_bytes())
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "text"
    assert r.json()["chunk_count"] >= 1
    q = client.post(f"{V}/acme/query", headers=auth,
                    json={"question": "what matters for retrieval quality?", "top_k": 3})
    assert any("retrieval" in (h.get("text") or "").lower() for h in q.json()["results"]), q.json()


def test_upload_html_strips_tags(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    html = b"<html><head><title>x</title></head><body><p>Visible body text here.</p></body></html>"
    r = _upload(client, auth, "page.html", html)
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "html"
    q = client.post(f"{V}/acme/query", headers=auth,
                    json={"question": "what is the visible body text?", "top_k": 3})
    assert any("visible body text" in (h.get("text") or "").lower() for h in q.json()["results"]), q.json()


def test_upload_empty_file_is_422(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = _upload(client, auth, "empty.txt", b"")
    assert r.status_code == 422, r.text


def test_upload_missing_file_is_422(client):
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    r = client.post(f"{V}/acme/documents/upload", headers=auth, data={})
    assert r.status_code == 422, r.text


def test_upload_pdf_without_parser_surfaces_clear_400(client, monkeypatch):
    """If pypdf import fails, the endpoint must 400 with an actionable message, not 500."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    # Make `pypdf` unimportable so `_extract_pdf`'s `from pypdf import PdfReader` fails with
    # ImportError, which the parser converts into a clear, actionable ValueError (HTTP 400).
    monkeypatch.setitem(__import__("sys").modules, "pypdf", None)
    r = _upload(client, auth, "doc.pdf", _pdf_bytes())
    assert r.status_code == 400, r.text
    assert "pypdf" in r.text.lower()


def _client():
    from app.main import app

    return TestClient(app)


def test_reupload_same_file_replaces_not_duplicates(client):
    """Re-uploading the identical file REPLACES prior chunks (issue #4), not duplicates."""
    t = _make_tenant(client)
    auth = _auth(t["api_key"])
    md = "# Guide\nStable content for idempotency check.\n"
    r1 = _upload(client, auth, "guide.md", md.encode())
    assert r1.status_code == 201, r1.text
    d1 = r1.json()["doc_id"]

    r2 = _upload(client, auth, "guide.md", md.encode())
    assert r2.status_code == 201, r2.text
    d2 = r2.json()["doc_id"]

    # Same file -> same doc_id (idempotent replace).
    assert d2 == d1, f"re-upload should reuse doc_id: {d1} != {d2}"
    # Catalog lists exactly one doc for this tenant (not two).
    cat = client.get(f"{V}/acme/documents", headers=auth).json()
    uploads = [d for d in cat if d["title"] == "guide.md"]
    assert len(uploads) == 1, f"re-upload duplicated: {uploads}"


def test_detect_content_type_precedence():
    assert fmod.detect_content_type("a.pdf", None) == "pdf"
    assert fmod.detect_content_type("a.md", None) == "markdown"
    assert fmod.detect_content_type("a.py", None) == "code"
    assert fmod.detect_content_type(None, "application/pdf") == "pdf"
    assert fmod.detect_content_type(None, "text/plain") == "text"
    # Explicit pipeline type wins over extension.
    assert fmod.detect_content_type("a.md", "text") == "text"
    # Unknown -> text default.
    assert fmod.detect_content_type("a.unknownext", None) == "text"
