window.AppRuntimeRefresh = (function () {
  "use strict";

  var isRefreshing = false;

  function showModal(message) {
    var modalEl = document.getElementById("runtimeRefreshModal");
    var titleEl = document.getElementById("runtimeRefreshTitle");
    var bodyEl = document.getElementById("runtimeRefreshBody");
    if (titleEl) titleEl.textContent = "The application is restarting.";
    if (bodyEl) {
      bodyEl.textContent = message || "The server runtime changed. This page will reload automatically.";
    }
    if (modalEl && window.bootstrap) {
      bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
  }

  function triggerReload(message) {
    if (isRefreshing) return;
    isRefreshing = true;
    showModal(message);
    window.setTimeout(function () {
      window.location.reload();
    }, 1200);
  }

  function pollRuntimeState() {
    var cfg = window.__WEBAPP_RUNTIME_REFRESH__ || {};
    if (!cfg.endpoint) return;
    fetch(cfg.endpoint, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "X-Requested-With": "XMLHttpRequest"
      },
      credentials: "same-origin",
      cache: "no-store"
    })
      .then(function (response) {
        if (!response.ok) throw new Error("Runtime state unavailable");
        return response.json();
      })
      .then(function (payload) {
        var nextToken = String((payload && payload.refresh_token) || "");
        var currentToken = String(cfg.token || "");
        if (nextToken && currentToken && nextToken !== currentToken) {
          triggerReload(String((payload && payload.refresh_message) || ""));
        }
      })
      .catch(function () {
        return null;
      });
  }

  function initRuntimeRefresh() {
    var cfg = window.__WEBAPP_RUNTIME_REFRESH__ || {};
    if (!cfg.endpoint) return;
    var refreshBtn = document.getElementById("runtimeRefreshNowBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        window.location.reload();
      });
    }
    window.setInterval(pollRuntimeState, 4000);
  }

  return {
    initRuntimeRefresh: initRuntimeRefresh,
    triggerReload: triggerReload
  };
})();
