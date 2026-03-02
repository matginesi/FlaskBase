/* Password meter for settings form. */
(function () {
  "use strict";

  function initPasswordStrength() {
    const pwdInput = document.getElementById("new_password");
    const pwdBar = document.getElementById("pwdStrengthBar");
    const label = document.getElementById("pwdStrengthLabel");
    if (!pwdInput || !pwdBar) return;

    pwdInput.addEventListener("input", () => {
      const value = pwdInput.value;
      let score = 0;

      if (value.length >= 8) score++;
      if (/[A-Z]/.test(value)) score++;
      if (/[0-9]/.test(value)) score++;
      if (/[^A-Za-z0-9]/.test(value)) score++;

      const colors = ["danger", "warning", "info", "success"];
      const labels = [
        label?.dataset.labelWeak || "Weak",
        label?.dataset.labelFair || "Fair",
        label?.dataset.labelGood || "Good",
        label?.dataset.labelStrong || "Strong",
      ];

      pwdBar.value = score * 25;
      pwdBar.className = "settings-progress-native bg-" + (colors[score - 1] || "secondary") + " is-" + (colors[score - 1] || "secondary");
      pwdBar.setAttribute("aria-valuenow", String(score * 25));

      if (label) label.textContent = labels[score - 1] || "";
    });
  }

  window.AppPasswordStrength = {
    initPasswordStrength,
  };
})();
