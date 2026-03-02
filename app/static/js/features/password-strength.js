/* Password meter for settings form. */
(function () {
  "use strict";

  function initPasswordStrength() {
    const pwdInput = document.getElementById("new_password");
    const pwdBar = document.getElementById("pwdStrengthBar");
    if (!pwdInput || !pwdBar) return;

    pwdInput.addEventListener("input", () => {
      const value = pwdInput.value;
      let score = 0;

      if (value.length >= 8) score++;
      if (/[A-Z]/.test(value)) score++;
      if (/[0-9]/.test(value)) score++;
      if (/[^A-Za-z0-9]/.test(value)) score++;

      const colors = ["danger", "warning", "info", "success"];
      const labels = ["Debole", "Discreta", "Buona", "Forte"];

      pwdBar.style.width = (score * 25) + "%";
      pwdBar.className = "progress-bar bg-" + (colors[score - 1] || "secondary");
      pwdBar.setAttribute("aria-valuenow", score * 25);

      const label = document.getElementById("pwdStrengthLabel");
      if (label) label.textContent = labels[score - 1] || "";
    });
  }

  window.AppPasswordStrength = {
    initPasswordStrength,
  };
})();
