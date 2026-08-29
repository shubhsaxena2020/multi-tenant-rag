/*
 * RAG Service — embeddable chat widget loader (v9-3, hardened in PHASE C).
 *
 * Drop this on a customer site:
 *   <script src="https://your-rag-host/widget.js"
 *           data-tenant="acme"
 *           data-api-key="pk_live_..."      publishable key, SAFE to embed (PHASE A/P1#9)
 *           data-base="https://your-rag-host"
 *           data-theme="light"              optional: light | dark
 *           data-accent="#6d28d9"           optional: brand accent color
 *           data-title="Acme Support"></script>
 *
 * It lazily injects a sandboxed <iframe> pointing at /widget.html (same origin as the
 * API), so the chat UI runs in an isolated browsing context. Cross-origin sites talk to
 * the widget only via window.postMessage — never direct DOM access (embedded-iframe
 * security best practice, 2026). Embedding is gated server-side by the
 * allowed_embed_origins CSP frame-ancestors header.
 *
 * SECURITY (PHASE C): the loader pushes the REAL api key (never a redacted placeholder).
 * It is a publishable pk_ key, scope-locked to query endpoints server-side, so even if
 * scraped from public HTML it cannot ingest/delete/rotate.
 */
(function () {
  "use strict";
  var scripts = document.querySelectorAll("script[data-api-key]");
  if (!scripts.length) return;
  var cfg = scripts[scripts.length - 1];
  var tenant = cfg.getAttribute("data-tenant") || "";
  var apiKey = cfg.getAttribute("data-api-key") || "";
  var base = cfg.getAttribute("data-base") || (location.origin + "/api/v1");
  var title = cfg.getAttribute("data-title") || "Chat";
  var theme = cfg.getAttribute("data-theme") || "dark";
  var accent = cfg.getAttribute("data-accent") || "";
  if (!tenant || !apiKey) {
    console.error("[rag-widget] data-tenant and data-api-key are required");
    return;
  }

  // Guardrail: warn loudly if a full-power secret key is embedded in public HTML.
  if (apiKey.indexOf("rk_") === 0) {
    console.error("[rag-widget] SECURITY: use a publishable key (pk_*) in public HTML, not the secret key (rk_*).");
  }

  var IFRAME_SRC = cfg.getAttribute("data-widget-url") || (location.origin + "/widget.html");
  var iframe = document.createElement("iframe");
  iframe.id = "rag-widget-frame";
  iframe.title = title;
  iframe.style.cssText = "position:fixed;bottom:20px;right:20px;width:380px;height:560px;border:0;" +
    "border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.25);z-index:2147483647;display:none;";
  iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
  iframe.src = IFRAME_SRC;
  document.body.appendChild(iframe);

  function show() { iframe.style.display = "block"; }
  function hide() { iframe.style.display = "none"; }

  window.addEventListener("message", function (e) {
    if (e.origin !== location.origin) return;
    var d = e.data || {};
    if (d.type === "rag:ready") {
      iframe.contentWindow.postMessage({
        type: "rag:config",
        tenant: tenant,
        apiKey: apiKey,
        base: base,
        title: title,
        theme: theme,
        accent: accent,
      }, e.origin);
    }
    if (d.type === "rag:toggle") { d.open ? show() : hide(); }
  });

  window.RagWidget = {
    open: show,
    close: hide,
    toggle: function () { iframe.style.display === "none" ? show() : hide(); },
  };
  iframe.addEventListener("load", function () { /* wait for rag:ready from frame */ });
})();
