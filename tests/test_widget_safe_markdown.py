"""PHASE A (#22) — regression test for safe Markdown rendering in the embeddable widget.

The widget renders untrusted LLM answers (which can echo poisoned retrieved docs) as formatted
Markdown. This test proves the renderer is XSS-safe by construction:

  1. `node --check` on the widget's inline <script> — the page must still parse (no syntax break).
  2. Extract the @rag-md-safe block and execute renderMarkdown() under a minimal DOM/location stub:
       - benign Markdown renders real tags (<strong>,<em>,<code>,<a>,<h1>,<ul><li>);
       - malicious Markdown (script/img-onerror/javascript: links) is neutralised — no <script>,
         no onerror=, no javascript: survives, and raw '<' is escaped.

If a future edit makes the renderer unsafe, this test fails (it would pass on the safe renderer).
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

WIDGET = Path(__file__).resolve().parent.parent / "app" / "static" / "widget.html"
NODE = "node"


def _require_node():
    try:
        subprocess.run([NODE, "--version"], check=True, capture_output=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"node not available: {e}")


def test_widget_script_syntax_valid():
    _require_node()
    html = WIDGET.read_text(encoding="utf-8")
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>")
    script = html[start:end]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        res = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        assert res.returncode == 0, f"widget script syntax error:\n{res.stderr}"
    finally:
        Path(path).unlink(missing_ok=True)


def test_rendermarkdown_safe_and_formats():
    _require_node()
    html = WIDGET.read_text(encoding="utf-8")
    block = html.split("/* @rag-md-safe:start */", 1)[1].split("/* @rag-md-safe:end */", 1)[0]

    harness = (
        "global.location = { origin: 'https://widget.example' };\n"
        + block
        + "\nmodule.exports = { renderMarkdown, escapeHtml, safeHref };\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        path = f.name
    try:
        # Node check program (kept in a separate file to avoid quoting pitfalls in the
        # Python->node bridge). It imports the renderer and asserts benign + malicious cases.
        check_js = (
            "const m = require(process.argv[2]);\n"
            "function assert(c, msg){ if(!c){ console.error('FAIL: '+msg); process.exit(1); } }\n"
            "const benign = '**bold** and *em* and `code` and [link](https://e.com)\\n\\n"
            "# Head\\n\\n- a\\n- b';\n"
            "const b = m.renderMarkdown(benign);\n"
            "assert(b.includes('<strong>bold</strong>'), 'bold');\n"
            "assert(b.includes('<em>em</em>'), 'em');\n"
            "assert(b.includes('<code>code</code>'), 'code');\n"
            "assert(b.includes('href=\"https://e.com/\"'), 'link href');\n"
            "assert(b.includes('<h1>Head</h1>'), 'h1');\n"
            "assert(b.includes('<ul><li>a</li><li>b</li></ul>'), 'ul/li');\n"
            "const evil = '<script>alert(1)</script><img src=x onerror=alert(1)>[x](javascript:alert(1))';\n"
            "const e = m.renderMarkdown(evil);\n"
            "assert(!e.includes('<script'), 'no script tag');\n"
            "assert(!e.includes('<img'), 'no raw img tag (escaped to inert text)');\n"
            "assert(!e.toLowerCase().includes('href=\"javascript'), 'no javascript: link emitted');\n"
            "assert(e.includes('&lt;img'), 'img tag escaped');\n"
            "assert(m.renderMarkdown('a < b').includes('a &lt; b'), 'raw < escaped');\n"
            "console.log('PASS');\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as c:
            c.write(check_js)
            cpath = c.name
        res = subprocess.run([NODE, cpath, path], capture_output=True, text=True)
        msg = f"stdout={res.stdout}\nstderr={res.stderr}"
        assert res.returncode == 0, msg
        assert "PASS" in res.stdout, msg
    finally:
        Path(path).unlink(missing_ok=True)
        Path(cpath).unlink(missing_ok=True)
