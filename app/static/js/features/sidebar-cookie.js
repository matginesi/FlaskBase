/* Layout sync and cookie consent interactions. */
(function () {
  "use strict";

  function initSidebar() {
    const topbar = document.getElementById("topbar");
    const mobileTopnav = document.getElementById("mobileTopnav");

    function syncHeights() {
      if (!topbar) return;
      const topbarHeight = Math.round(topbar.getBoundingClientRect().height || 56);
      document.documentElement.style.setProperty("--topbar-h", topbarHeight + "px");
      if (mobileTopnav && window.innerWidth < 768) {
        const mobileHeight = Math.round(mobileTopnav.getBoundingClientRect().height || 0);
        document.documentElement.style.setProperty("--mobile-topnav-h", mobileHeight + "px");
      } else {
        document.documentElement.style.setProperty("--mobile-topnav-h", "0px");
      }
    }

    window.addEventListener("resize", syncHeights, { passive: true });
    syncHeights();
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
