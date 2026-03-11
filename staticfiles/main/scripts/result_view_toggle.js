(function () {
  function initViewGroup(groupRoot) {
    const groupName = groupRoot.dataset.viewGroup;
    if (!groupName) return;

    const defaultView = groupRoot.dataset.defaultView || "list";
    const buttons = Array.from(groupRoot.querySelectorAll("[data-view-target]"));
    if (!buttons.length) return;

    const panels = Array.from(document.querySelectorAll("[data-view-panel]")).filter(
      (panel) => (panel.dataset.viewPanel || "").startsWith(`${groupName}:`)
    );
    if (!panels.length) return;

    function resolveView(candidate) {
      const wanted = (candidate || "").trim();
      const hasWanted = panels.some((panel) => panel.dataset.viewPanel === `${groupName}:${wanted}`);
      if (hasWanted) return wanted;
      const hasDefault = panels.some((panel) => panel.dataset.viewPanel === `${groupName}:${defaultView}`);
      if (hasDefault) return defaultView;
      const firstPanel = panels[0].dataset.viewPanel || "";
      return firstPanel.split(":")[1] || "list";
    }

    function applyView(view) {
      const resolved = resolveView(view);
      panels.forEach((panel) => {
        const isActive = panel.dataset.viewPanel === `${groupName}:${resolved}`;
        panel.classList.toggle("d-none", !isActive);
      });
      buttons.forEach((button) => {
        const isActive = button.dataset.viewTarget === resolved;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
      });
    }

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        applyView(button.dataset.viewTarget);
      });
    });

    applyView(defaultView);
  }

  document.querySelectorAll("[data-view-group]").forEach(initViewGroup);
})();
