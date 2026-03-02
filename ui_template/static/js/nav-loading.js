/* Lightweight nav loading modal for slow pages (RAG). */
(function () {
  "use strict";

  let navModal = null;

  function getNavModal() {
    if (navModal) return navModal;
    const modalEl = document.getElementById("navLoadingModal");
    if (!modalEl || !window.bootstrap || typeof bootstrap.Modal !== "function") return null;
    navModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    return navModal;
  }

  function shouldTrackLink(a) {
    if (!a || a.tagName !== "A") return false;
    if (a.target && a.target.toLowerCase() === "_blank") return false;
    if (a.hasAttribute("download")) return false;
    if (a.dataset.noNavLoading === "1") return false;
    return true;
  }

  function shouldTrackForm(form) {
    if (!form || form.tagName !== "FORM") return false;
    if (form.target && form.target.toLowerCase() === "_blank") return false;
    if (form.dataset.noNavLoading === "1") return false;
    return true;
  }

  function isRagHref(href) {
    try {
      const u = new URL(href, window.location.origin);
      return u.origin === window.location.origin && u.pathname.startsWith("/rag");
    } catch (_) {
      return false;
    }
  }

  function showIfRagTarget(href) {
    if (!isRagHref(href)) return false;
    const modal = getNavModal();
    if (!modal) return false;
    modal.show();
    return true;
  }

  function initNavLoading() {
    if (!getNavModal()) return;

    document.addEventListener("click", (ev) => {
      if (ev.defaultPrevented) return;
      if (ev.button !== 0) return;
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      const a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
      if (!shouldTrackLink(a)) return;
      showIfRagTarget(a.getAttribute("href") || "");
    }, true);

    document.addEventListener("submit", (ev) => {
      if (ev.defaultPrevented) return;
      const form = ev.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (!shouldTrackForm(form)) return;
      showIfRagTarget(form.action || "");
    });
  }

  window.AppNavLoading = {
    initNavLoading,
    showIfRagTarget,
  };
})();
