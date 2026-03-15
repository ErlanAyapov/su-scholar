(() => {
  const buttons = Array.from(document.querySelectorAll("[data-dashboard-target]"));
  const sections = Array.from(document.querySelectorAll("[data-dashboard-section]"));

  if (!buttons.length || !sections.length) {
    return;
  }

  const activate = (target) => {
    buttons.forEach((button) => {
      const isActive = button.dataset.dashboardTarget === target;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", isActive ? "true" : "false");
    });

    sections.forEach((section) => {
      const isActive = section.dataset.dashboardSection === target;
      section.classList.toggle("is-active", isActive);
    });
  };

  buttons.forEach((button) => {
    button.addEventListener("click", () => activate(button.dataset.dashboardTarget));
  });

  activate("university");
})();
