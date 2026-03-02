/* Global confirm modal for forms that declare data-confirm-* attributes. */
(function () {
  "use strict";

  const escapeHtml = window.AppUtils?.escapeHtml || String;

  function initConfirmModal() {
    const confirmModalEl = document.getElementById("confirmActionModal");
    const confirmTitleEl = document.getElementById("confirmActionTitle");
    const confirmBodyEl = document.getElementById("confirmActionBody");
    const confirmOkBtn = document.getElementById("confirmActionOkBtn");
    const confirmModal = confirmModalEl ? new bootstrap.Modal(confirmModalEl) : null;

    let pendingConfirmAction = null;

    function getConfirmMessage(form) {
      const tpl = String(form.dataset.confirmMessage || "").trim();
      if (!tpl) return "";

      const valueSelector = String(form.dataset.confirmValueSelector || "").trim();
      if (!valueSelector) return tpl;

      const source = form.querySelector(valueSelector) || document.querySelector(valueSelector);
      const value = source ? String(source.value || "").trim() : "";
      return tpl.replaceAll("{value}", value || "-");
    }

    function openConfirmModal(opts) {
      if (!confirmModal || !opts || typeof opts.onConfirm !== "function") return;

      const title = String(opts.title || "Confirm action");
      const message = String(opts.message || "Confirm this action?");
      const confirmLabel = String(opts.confirmLabel || "Confirm");
      const confirmClass = String(opts.confirmClass || "btn-danger");

      if (confirmTitleEl) {
        confirmTitleEl.innerHTML = `<i class="bi bi-exclamation-circle me-2 text-warning"></i>${escapeHtml(title)}`;
      }
      if (confirmBodyEl) confirmBodyEl.textContent = message;

      if (confirmOkBtn) {
        confirmOkBtn.className = `btn ${confirmClass}`;
        confirmOkBtn.innerHTML = `<i class="bi bi-check2 me-1"></i>${escapeHtml(confirmLabel)}`;
        confirmOkBtn.disabled = false;
      }

      pendingConfirmAction = opts.onConfirm;
      confirmModal.show();
    }

    window.openConfirmModal = openConfirmModal;

    confirmOkBtn && confirmOkBtn.addEventListener("click", () => {
      if (typeof pendingConfirmAction !== "function") {
        confirmModal && confirmModal.hide();
        return;
      }

      const action = pendingConfirmAction;
      pendingConfirmAction = null;
      confirmOkBtn.disabled = true;
      try {
        action();
      } finally {
        confirmModal && confirmModal.hide();
        confirmOkBtn.disabled = false;
      }
    });

    document.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (!form.hasAttribute("data-confirm-message")) return;

      if (form.dataset.confirmBypass === "1") {
        form.dataset.confirmBypass = "";
        return;
      }

      event.preventDefault();
      openConfirmModal({
        title: form.dataset.confirmTitle || "Confirm action",
        message: getConfirmMessage(form) || "Confirm this action?",
        confirmLabel: form.dataset.confirmLabel || "Confirm",
        confirmClass: form.dataset.confirmClass || "btn-danger",
        onConfirm: () => {
          if (window.AppNavLoading && typeof window.AppNavLoading.showIfRagTarget === "function") {
            window.AppNavLoading.showIfRagTarget(form.action || "");
          }
          form.dataset.confirmBypass = "1";
          form.submit();
        },
      });
    });
  }

  window.AppConfirm = {
    initConfirmModal,
  };
})();
