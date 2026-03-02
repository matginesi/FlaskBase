/* Layout sync and cookie consent interactions. */
(function () {
  "use strict";

  function initSidebar() {
    // CSS defaults keep the layout stable without mutating inline styles,
    // which would violate the current CSP.
    return;
  }

  function initCookieBanner() {
    const cookieBanner = document.getElementById("cookieBanner");
    const cookieAccept = document.getElementById("cookieAccept");

    if (!cookieBanner) return;

    if (localStorage.getItem("cookie_accepted")) {
      cookieBanner.classList.add("hidden");
    }

    cookieAccept && cookieAccept.addEventListener("click", () => {
      localStorage.setItem("cookie_accepted", "1");
      cookieBanner.classList.add("hidden");
    });
  }

  window.AppSidebarCookie = {
    initSidebar,
    initCookieBanner,
  };
})();
