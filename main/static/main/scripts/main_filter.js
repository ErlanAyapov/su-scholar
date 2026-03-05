(function () {
  const filterBtn = document.getElementById("filterButton");
  const panel = document.getElementById("filterPanel");
  const closeBtn = document.getElementById("filterCloseBtn");
  const resetBtn = document.getElementById("resetFilters");
  const form = document.getElementById("filterForm");
  const chipsBox = document.getElementById("activeChips");
  const searchContainer = document.getElementById("searchContainer");
  if (!panel || !form) return;

  function openPanel() {
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    searchContainer.style.borderBottomLeftRadius = "0";
    searchContainer.style.borderBottomRightRadius = "0";
    searchContainer.style.borderTopLeftRadius = "20px";
    searchContainer.style.borderTopRightRadius = "20px";

  }

  function closePanel() {
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    searchContainer.style.borderBottomLeftRadius = "50px";
    searchContainer.style.borderBottomRightRadius = "50px";
    searchContainer.style.borderTopLeftRadius = "50px";
    searchContainer.style.borderTopRightRadius = "50px";
  }

  if (filterBtn) {
    filterBtn.type = "button";
    filterBtn.addEventListener("click", () => {
      if (panel.classList.contains("open")) closePanel();
      else openPanel();
    });
  }

  if (closeBtn) closeBtn.addEventListener("click", closePanel);

  function setBodyHeight(body, open) {
    if (!body) return;
    if (open) {
      body.style.height = body.scrollHeight + "px";
      body.addEventListener(
        "transitionend",
        function handler() {
          body.style.height = "auto";
          body.removeEventListener("transitionend", handler);
        },
        { once: true }
      );
    } else {
      body.style.height = body.scrollHeight + "px";
      requestAnimationFrame(() => {
        body.style.height = "0px";
      });
    }
  }

  document.querySelectorAll(".group-toggle").forEach((btn) => {
    const targetId = btn.getAttribute("data-target");
    const body = document.getElementById(targetId);
    const group = btn.closest(".filter-group");

    if (targetId === "grp-main") {
      group.classList.add("open");
      setBodyHeight(body, true);
    } else if (body) {
      body.style.height = "0px";
    }

    btn.addEventListener("click", () => {
      const isOpen = group.classList.contains("open");
      if (isOpen) {
        group.classList.remove("open");
        setBodyHeight(body, false);
      } else {
        group.classList.add("open");
        setBodyHeight(body, true);
      }
    });
  });

  function rebuildChips() {
    if (!chipsBox) return;
    chipsBox.innerHTML = "";

    const data = new FormData(form);
    const entries = [];
    for (const [k, v] of data.entries()) {
      if (!v) continue;
      if (k === "search") continue;
      entries.push([k, v]);
    }
    if (!entries.length) return;

    entries.forEach(([k, v]) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = `${k}: ${v}`;

      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "✕";
      x.addEventListener("click", () => {
        const fieldList = form.querySelectorAll(`[name="${CSS.escape(k)}"]`);
        fieldList.forEach((field) => {
          if (field.type === "checkbox" || field.type === "radio") {
            if (field.value === v) field.checked = false;
          } else {
            field.value = "";
          }
        });
        rebuildChips();
      });

      chip.appendChild(x);
      chipsBox.appendChild(chip);
    });
  }

  rebuildChips();
  form.addEventListener("change", rebuildChips);

  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      const search = form.querySelector('input[name="search"]')?.value || "";
      form.reset();
      const searchInput = form.querySelector('input[name="search"]');
      if (searchInput) searchInput.value = search;
      rebuildChips();
      form.submit();
    });
  }
})();
