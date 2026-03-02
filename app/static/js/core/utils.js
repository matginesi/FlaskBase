/* Shared browser-side helpers used across app modules. */
(function () {
  "use strict";

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function fmtSeconds(ms) {
    if (!ms || ms < 0) return null;
    return (ms / 1000).toFixed(ms >= 10000 ? 1 : 2) + "s";
  }

  function sanitizeUrl(url) {
    const raw = String(url || "").trim().replaceAll("&amp;", "&");
    if (/^(https?:\/\/|mailto:)/i.test(raw)) return raw;
    return "#";
  }

  window.AppUtils = {
    escapeHtml,
    fmtSeconds,
    sanitizeUrl,
  };
})();
