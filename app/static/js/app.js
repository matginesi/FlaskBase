/* App bootstrap: coordinates module initialization. */
(function () {
  "use strict";

  function safeInit(fn) {
    if (typeof fn !== "function") return;
    try {
      fn();
    } catch (err) {
      console.error("Initialization error:", err);
    }
  }

  safeInit(window.AppSidebarCookie?.initSidebar);
  safeInit(window.AppSidebarCookie?.initCookieBanner);
  safeInit(window.AppToast?.initFlashToasts);
  safeInit(window.AppConfirm?.initConfirmModal);
  safeInit(window.AppNavLoading?.initNavLoading);
  safeInit(window.AppRuntimeRefresh?.initRuntimeRefresh);
  safeInit(window.AppToast?.initToastDemoButtons);
  safeInit(window.AppChat?.initChat);
  safeInit(window.AppPasswordStrength?.initPasswordStrength);
})();
