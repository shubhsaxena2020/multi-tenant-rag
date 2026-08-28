/*
 * RAG Service — embeddable chat widget loader (v9-3).
 *
 * Drop this on a customer site:
 *   <script src="https://your-rag-host/widget.js"
 *           data-tenant="acme"
 *           data-api-key="rk_..."
 *           data-base="https://your-rag-host"></script>
 *
 * It lazily injects a sandboxed <iframe> pointing at /widget.html (same origin as the
 * API), so the chat UI runs in an isolated browsing context. Cross-origin sites talk to
 * the widget only via window.postMessage — never direct DOM access (didit.me
 * embedded-iframe-security best practice, 2026). Embedding is gated server-side by the
 * `allowed_embed_origins` CSP frame-ancestors header.
 */
(function () {
  "use strict";
  var scripts = document.querySelectorAll('script[data-api-key]');
  if (!scripts.length) return;
  var cfg = scripts[scripts.length - 1];
  var tenant = cfg.getAttribute("data-tenant") || "";
  var apiKey = cfg.getAttribute("data-api-key") || "";
  var base = cfg.getAttribute("data-base") || (location.origin + "/api/v1");
  var title = cfg.getAttribute("data-title") || "Chat";
  if (!tenant || !apiKey) { console.error("[rag-widget] data-tenant and data-api-key are required"); return; }

  var IFRAME_SRC = (cfg.getAttribute("data-widget-url") || (location.origin + "/widget.html"));
  var iframe = document.createElement("iframe");
  iframe.id = "rag-widget-frame";
  iframe.title = title;
  iframe.style.cssText = "position:fixed;bottom:20px;right:20px;width:380px;height:560px;border:0;" +
    "border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.25);z-index:2147483647;display:none;";
  // Sandbox: allow scripts (it's our own UI) but block same-origin so it can't touch
  // the parent's cookies/DOM; all comms go through postMessage.
  iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
  iframe.src = IFRAME_SRC;
  document.body.appendChild(iframe);

  function show() { iframe.style.display = "block"; }
  function hide() { iframe.style.display = "none"; }

  // Relay: parent <-> iframe via postMessage (origin-locked).
  window.addEventListener("message", function (e) {
    if (e.origin !== location.origin) return; // only trust our own widget frame
    var d = e.data || {};
    if (d.type === "rag:ready") { iframe.contentWindow.postMessage({ type: "rag:config", tenant: tenant, apiKey: apiKey, base: base }, e.origin); }
    if (d.type === "rag:toggle") { d.open ? show() : hide(); }
  });

  // Public toggle control for host pages.
  window.RagWidget = {
    open: show,
    close: hide,
    toggle: function () { iframe.style.display === "none" ? show() : hide(); }
  };
  // Auto-open after load (config is pushed on ready).
  iframe.addEventListener("load", function () { /* wait for rag:ready from frame */ });
})();
