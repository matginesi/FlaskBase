/* Toast utilities plus flash-message bridge. */
(function () {
  "use strict";

  function showToast(msg, type = "info", duration = 4000) {
    const stack = document.getElementById("toastStack");
    if (!stack) return;

    const escapeHtml = window.AppUtils?.escapeHtml || String;
    const id = "t" + Date.now();
    const icons = {
      success: "check-circle-fill",
      danger: "exclamation-triangle-fill",
      warning: "exclamation-circle-fill",
      info: "info-circle-fill",
    };
    const icon = icons[type] || "info-circle-fill";
    const el = document.createElement("div");
    el.className = "toast align-items-center text-bg-" + type + " border-0 show";
    el.id = id;
    el.setAttribute("role", "alert");
    el.innerHTML = `<div class="d-flex"><div class="toast-body d-flex align-items-center gap-2">
      <i class="bi bi-${icon}"></i>${escapeHtml(msg)}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" onclick="this.closest('.toast').remove()"></button></div>`;
    stack.appendChild(el);
    setTimeout(() => el.remove && el.remove(), duration);
  }

  function initFlashToasts() {
    const el = document.getElementById("flashMessagesData");
    if (!el) return;

    let entries = [];
    try {
      entries = JSON.parse(el.textContent || "[]");
    } catch (_) {
      entries = [];
    }

    const mapType = (raw) => {
      const t = String(raw || "").toLowerCase().trim();
      if (t === "error") return "danger";
      if (t === "message") return "info";
      if (t === "success" || t === "danger" || t === "warning" || t === "info") return t;
      return "info";
    };

    entries.forEach((item) => {
      if (!Array.isArray(item) || item.length < 2) return;
      const category = mapType(item[0]);
      const message = String(item[1] || "").trim();
      if (message) showToast(message, category);
    });

    el.remove();
  }

  function initToastDemoButtons() {
    document.querySelectorAll("[data-toast-msg]").forEach((button) => {
      button.addEventListener("click", () => {
        showToast(button.dataset.toastMsg, button.dataset.toastType || "info");
      });
    });
  }

  window.showToast = showToast;
  window.AppToast = {
    initFlashToasts,
    initToastDemoButtons,
  };
})();
