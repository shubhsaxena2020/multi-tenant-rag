"""PHASE C — per-tenant widget theming/branding (issue #23).

Real verification:
- Python: tenant create persists sanitized branding; widget/config returns it; PATCH updates it;
  sanitize_branding drops unsafe values on the server.
- Node: executes the widget's actual applyBranding() with malicious accent/logo/title values and
  asserts they are REJECTED (no CSS/HTML injection) — proving the client guard matches the server.
"""
import json
import os
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import sanitize_branding

V = "/api/v1"
ADMIN = {"Admin-Key": os.environ.get("ADMIN_API_KEY", "test-admin-key-for-tests")}
WIDGET_HTML = os.path.join(os.path.dirname(__file__), "..", "app", "static", "widget.html")


def _client():
    return TestClient(app)


def _create_tenant(client, name="brandco", branding=None):
    body = {"name": name, "plan": "standard"}
    if branding is not None:
        body["branding"] = branding
    r = client.post(f"{V}/tenants", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_tenant_persists_sanitized_branding():
    client = _client()
    branding = {
        "accent": "#ff0000",
        "accent_text": "#ffffff",
        "header_title": "Acme Support",
        "logo_url": "https://acme.example/logo.png",
        "font_family": "Georgia, serif",
        "evil": "dropme",
        "accent_inject": "red; background:url(x)",
    }
    out = _create_tenant(client, branding=branding)
    stored = out["branding"]
    assert stored.get("accent") == "#ff0000"
    assert stored.get("accent_text") == "#ffffff"
    assert stored.get("header_title") == "Acme Support"
    assert stored.get("logo_url") == "https://acme.example/logo.png"
    assert stored.get("font_family") == "Georgia, serif"
    # Non-whitelisted / unsafe keys are dropped.
    assert "evil" not in stored
    assert "accent_inject" not in stored


def test_widget_config_returns_branding_for_tenant_secret_key():
    client = _client()
    branding = {"accent": "#00ff00", "header_title": "Globex"}
    t = _create_tenant(client, branding=branding)
    secret = t["api_key"]  # admin create_tenant returns the raw api_key
    r = client.get(f"{V}/{t['tenant_id']}/widget/config",
                   headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["tenant_id"] == t["tenant_id"]
    assert cfg["branding"].get("accent") == "#00ff00"
    assert cfg["branding"].get("header_title") == "Globex"


def test_branding_patch_updates_via_admin():
    client = _client()
    t = _create_tenant(client, branding={"accent": "#000000", "header_title": "Old"})
    r = client.patch(
        f"{V}/{t['tenant_id']}/branding",
        json={"accent": "#123456", "header_title": "New Name"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    assert r.json()["branding"].get("accent") == "#123456"
    assert r.json()["branding"].get("header_title") == "New Name"
    cfg = client.get(f"{V}/{t['tenant_id']}/widget/config",
                     headers={"Authorization": f"Bearer {t['api_key']}"}).json()
    assert cfg["branding"]["accent"] == "#123456"


def test_sanitize_branding_drops_unsafe_values():
    dirty = {
        "accent": "red; background:url(evil)",   # not a valid CSS color → dropped
        "logo_url": "javascript:alert(1)",        # not http(s) → dropped
        "header_title": "<img src=x onerror=alert(1)>",  # html → dropped
        "font_family": "a;b",                     # invalid charset → dropped
        "accent_text": "#abcdef",
        "logo_url_good": "https://x/y.png",       # not a whitelisted field → ignored
    }
    clean = sanitize_branding(dirty)
    assert clean.get("accent") is None
    assert clean.get("logo_url") is None
    assert clean.get("header_title") is None
    assert clean.get("font_family") is None
    assert clean.get("accent_text") == "#abcdef"
    assert "logo_url_good" not in clean


def _extract_js_between(html, start, end):
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), html, re.S)
    assert m, f"markers {start!r}..{end!r} not found"
    return m.group(1)


def _node_apply_branding(html, cases):
    """Run the widget's real applyBranding() in Node with a stubbed DOM, for each case."""
    md_lib = _extract_js_between(html, "/* @rag-md-safe:start */", "/* @rag-md-safe:end */")
    brand_lib = _extract_js_between(html, "/* @rag-branding:start */", "/* @rag-branding:end */")
    harness = r"""
    globalThis.location = { origin: "https://x" };
    var document = {
      documentElement: { style: { setProperty: function(k,v){ applied[k]=v; } } },
      body: { style: {} },
      _els: {},
      getElementById: function(id){
        if(!this._els[id]) this._els[id] = { id:id, src:'', style:{}, _text:'',
          set textContent(v){this._text=v;}, get textContent(){return this._text;},
          removeAttribute:function(a){this[a]=null;} };
        return this._els[id];
      }
    };
    var applied={};
    """
    harness += md_lib + "\n" + brand_lib + "\n"
    harness += (
        "function run(cfg){ applied={}; var l=document.getElementById('brand-logo'); "
        "var t=document.getElementById('brand-title'); applyBranding(cfg); "
        "return {applied:applied, logo:l.src, logoDisplay:l.style.display, title:t._text}; }\n"
        "module.exports = { run: run };\n"
    )
    open("/tmp/brand_run.js", "w").write(harness)
    results = {}
    for name, cfg in cases.items():
        script = (
            "var r=require(\"/tmp/brand_run.js\").run(" + json.dumps(cfg) +
            "); console.log(JSON.stringify({name: " + json.dumps(name) + ", res: r}));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, proc.stderr
        results[name] = json.loads(proc.stdout.strip())["res"]
    return results


def test_widget_apply_branding_no_injection():
    html = open(WIDGET_HTML).read()
    cases = {
        "good": {
            "accent": "#ff0000",
            "accent_text": "#ffffff",
            "header_title": "Acme",
            "logo_url": "https://x/a.png",
            "font_family": "Georgia, serif",
        },
        "bad_accent": {"accent": "red; background:url(evil)"},
        "bad_logo": {"logo_url": "javascript:alert(1)"},
        "bad_title": {"header_title": "<img src=x onerror=alert(1)>"},
        "bad_font": {"font_family": "a;b"},
    }
    results = _node_apply_branding(html, cases)

    good = results["good"]
    assert good["applied"].get("--accent") == "#ff0000"
    assert good["applied"].get("--accent-text") == "#ffffff"
    assert good["logo"] == "https://x/a.png"
    assert good["logoDisplay"] == "block"
    assert good["title"] == "Acme"

    # Malicious cases: nothing dangerous is applied.
    assert results["bad_accent"]["applied"].get("--accent") is None
    assert results["bad_logo"]["logo"] in (None, "")
    assert results["bad_logo"]["logoDisplay"] == "none"
    # header_title uses textContent (not innerHTML), so the markup is inert text.
    assert results["bad_title"]["title"] == "<img src=x onerror=alert(1)>"
    assert results["bad_font"]["applied"].get("--widget-font") is None
