(function () {
  const filterBtn = document.getElementById("filterButton");
  const panel = document.getElementById("filterPanel");
  const closeBtn = document.getElementById("filterCloseBtn");
  const resetBtn = document.getElementById("resetFilters");
  const form = document.getElementById("filterForm");
  const chipsBox = document.getElementById("activeChips");
  const searchContainer = document.getElementById("searchContainer");
  const searchForm = searchContainer?.querySelector("form");
  const searchInput = document.getElementById("searchInput");
  const suggestionsBox = document.getElementById("searchSuggestions");
  const suggestionsUrl = searchInput?.dataset?.suggestionsUrl || "";
  const SEARCH_STATE_KEY = "su_science_main_filters";
  const quickSearchPanel = document.getElementById("quickSearchPanel");

  if (!panel || !form) return;

  function saveSearchState(queryString) {
    try {
      localStorage.setItem(SEARCH_STATE_KEY, queryString || "");
    } catch (error) {
      // Ignore localStorage failures (private mode / browser restrictions)
    }
  }

  saveSearchState(window.location.search);

  function openPanel() {
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    searchContainer.style.borderBottomLeftRadius = "0";
    searchContainer.style.borderBottomRightRadius = "0";
    searchContainer.style.borderTopLeftRadius = "20px";
    searchContainer.style.borderTopRightRadius = "20px";
    quickSearchPanel.style.display = "none";

  }

  function closePanel() {
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    searchContainer.style.borderBottomLeftRadius = "50px";
    searchContainer.style.borderBottomRightRadius = "50px";
    searchContainer.style.borderTopLeftRadius = "50px";
    searchContainer.style.borderTopRightRadius = "50px";
    quickSearchPanel.style.display = "flex";
  }

  if (filterBtn) {
    filterBtn.type = "button";
    filterBtn.addEventListener("click", () => {
      if (panel.classList.contains("open")) closePanel();
      else openPanel();
    });
  }

  if (closeBtn) closeBtn.addEventListener("click", closePanel);

  if (searchForm) {
    searchForm.addEventListener("submit", () => {
      const params = new URLSearchParams(new FormData(searchForm));
      saveSearchState(params.toString() ? `?${params.toString()}` : "");
    });
  }

  let suggestionsDebounceTimer = null;
  let suggestionsAbortController = null;

  function hideSuggestions() {
    if (!suggestionsBox) return;
    suggestionsBox.classList.add("d-none");
    suggestionsBox.innerHTML = "";
  }

  function showSuggestions(items) {
    if (!suggestionsBox) return;
    if (!items.length) {
      hideSuggestions();
      return;
    }

    suggestionsBox.innerHTML = "";
    items.forEach((item) => {
      const suggestionBtn = document.createElement("button");
      suggestionBtn.type = "button";
      suggestionBtn.className = "search-suggestion-item";
      suggestionBtn.textContent = item.label || item.value || "";
      suggestionBtn.dataset.value = item.value || "";
      suggestionBtn.addEventListener("click", () => {
        if (searchInput) {
          searchInput.value = suggestionBtn.dataset.value || "";
        }
        hideSuggestions();
        if (searchForm) {
          if (typeof searchForm.requestSubmit === "function") {
            searchForm.requestSubmit();
          } else {
            const params = new URLSearchParams(new FormData(searchForm));
            saveSearchState(params.toString() ? `?${params.toString()}` : "");
            searchForm.submit();
          }
        }
      });
      suggestionsBox.appendChild(suggestionBtn);
    });

    suggestionsBox.classList.remove("d-none");
  }

  function requestSuggestions() {
    if (!searchInput || !suggestionsBox || !suggestionsUrl) return;

    const query = searchInput.value.trim();
    if (query.length < 3) {
      hideSuggestions();
      return;
    }

    if (suggestionsAbortController) {
      suggestionsAbortController.abort();
    }
    suggestionsAbortController = new AbortController();

    const params = new URLSearchParams({ q: query });
    const staffUser = searchForm?.querySelector('input[name="staff_user"]')?.value;
    if (staffUser) {
      params.set("staff_user", staffUser);
    }

    fetch(`${suggestionsUrl}?${params.toString()}`, {
      method: "GET",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
      },
      signal: suggestionsAbortController.signal,
    })
      .then((response) => (response.ok ? response.json() : { suggestions: [] }))
      .then((payload) => {
        if (!searchInput) return;
        if (searchInput.value.trim() !== query) return;
        const items = Array.isArray(payload?.suggestions) ? payload.suggestions : [];
        showSuggestions(items);
      })
      .catch((error) => {
        if (error.name !== "AbortError") {
          hideSuggestions();
        }
      });
  }

  if (searchInput && suggestionsBox) {
    searchInput.addEventListener("input", () => {
      clearTimeout(suggestionsDebounceTimer);
      suggestionsDebounceTimer = setTimeout(requestSuggestions, 220);
    });

    searchInput.addEventListener("focus", () => {
      if (searchInput.value.trim().length >= 3) {
        requestSuggestions();
      }
    });

    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        hideSuggestions();
      }
    });

    document.addEventListener("click", (event) => {
      if (searchContainer && !searchContainer.contains(event.target)) {
        hideSuggestions();
      }
    });
  }

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
      if (k === "show") continue;
      if (k === "staff_user") continue;
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
  form.addEventListener("submit", () => {
    const params = new URLSearchParams(new FormData(form));
    saveSearchState(params.toString() ? `?${params.toString()}` : "");
  });

  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      const search = form.querySelector('input[name="search"]')?.value || "";
      const show = form.querySelector('input[name="show"]')?.value || "";
      const staffUser = form.querySelector('input[name="staff_user"]')?.value || "";
      const tab = form.querySelector('input[name="tab"]')?.value || "";
      form.reset();
      const searchInput = form.querySelector('input[name="search"]');
      if (searchInput) searchInput.value = search;
      const showInput = form.querySelector('input[name="show"]');
      if (showInput) showInput.value = show;
      const staffUserInput = form.querySelector('input[name="staff_user"]');
      if (staffUserInput) staffUserInput.value = staffUser;
      const tabInput = form.querySelector('input[name="tab"]');
      if (tabInput) tabInput.value = tab;
      rebuildChips();
      const params = new URLSearchParams(new FormData(form));
      saveSearchState(params.toString() ? `?${params.toString()}` : "");
      hideSuggestions();
      form.submit();
    });
  }
})();
