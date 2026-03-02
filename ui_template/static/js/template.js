(function () {
  "use strict";

  function initOverlays() {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
      new bootstrap.Tooltip(el);
    });
    document.querySelectorAll('[data-bs-toggle="popover"]').forEach((el) => {
      new bootstrap.Popover(el);
    });
  }

  function initTemplateDemos() {
    const logModalEl = document.getElementById("logDetailModal");
    const runtimeRefreshEl = document.getElementById("runtimeRefreshModal");
    const navLoadingEl = document.getElementById("navLoadingModal");
    const logModal = logModalEl ? bootstrap.Modal.getOrCreateInstance(logModalEl) : null;
    const runtimeRefreshModal = runtimeRefreshEl ? bootstrap.Modal.getOrCreateInstance(runtimeRefreshEl) : null;
    const navLoadingModal = navLoadingEl ? bootstrap.Modal.getOrCreateInstance(navLoadingEl) : null;

    document.querySelectorAll("[data-demo-confirm]").forEach((button) => {
      button.addEventListener("click", () => {
        if (typeof window.openConfirmModal !== "function") return;
        window.openConfirmModal({
          title: "Publish template action",
          message: "Run a demo UI action using the shared confirmation modal?",
          confirmLabel: "Run action",
          confirmClass: "btn-primary",
          onConfirm: () => {
            window.showToast && window.showToast("Confirmed through the shared modal.", "success");
          },
        });
      });
    });

    document.querySelectorAll("[data-demo-nav-loading]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!navLoadingModal) return;
        navLoadingModal.show();
        window.setTimeout(() => navLoadingModal.hide(), 1400);
      });
    });

    document.querySelectorAll("[data-demo-log-detail]").forEach((button) => {
      button.addEventListener("click", () => {
        logModal && logModal.show();
      });
    });

    document.querySelectorAll("[data-demo-runtime-refresh]").forEach((button) => {
      button.addEventListener("click", () => {
        runtimeRefreshModal && runtimeRefreshModal.show();
      });
    });

    document.querySelectorAll("[data-demo-reset-cookies]").forEach((button) => {
      button.addEventListener("click", () => {
        localStorage.removeItem("cookie_accepted");
        const banner = document.getElementById("cookieBanner");
        banner && banner.classList.remove("hidden");
      });
    });

    document.querySelectorAll("[data-demo-flash]").forEach((button) => {
      button.addEventListener("click", () => {
        window.showToast && window.showToast("Runtime settings applied.", "success");
        window.showToast && window.showToast("Add-on routes will mount after restart.", "warning");
      });
    });

    const refreshNowBtn = document.getElementById("runtimeRefreshNowBtn");
    refreshNowBtn && refreshNowBtn.addEventListener("click", () => {
      runtimeRefreshModal && runtimeRefreshModal.hide();
      window.showToast && window.showToast("In the real app this would reload the workspace.", "info");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initOverlays();
      initTemplateDemos();
    }, { once: true });
  } else {
    initOverlays();
    initTemplateDemos();
  }
})();
