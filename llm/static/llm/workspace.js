(function () {
  function readSelection() {
  window.Asc.plugin.executeMethod("GetSelectionType", [], function (selectionType) {
    // Можно фильтровать только текстовое выделение
    window.Asc.plugin.executeMethod("GetSelectedText", [], function (text) {
      const normalized = (text || "").trim();

      sendSelectionToParent({
        selectionType,
        text: normalized,
        hasSelection: normalized.length > 0
      });
    });
  });
}


  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
      return;
    }
    fn();
  }

  function initProjectOnlyofficeEditor() {
    var editorContainer = document.getElementById("projectOnlyofficeEditor");
    if (!editorContainer) {
      return;
    }
    if (editorContainer.dataset.initialized === "1") {
      return;
    }

    var configUrl = editorContainer.dataset.configUrl || "";
    var fallback = document.getElementById("projectOnlyofficeFallback");
    if (!configUrl) {
      if (fallback) {
        fallback.classList.remove("hidden");
        fallback.textContent = "Не удалось загрузить конфигурацию редактора.";
      }
      return;
    }
    if (!window.DocsAPI || typeof window.DocsAPI.DocEditor !== "function") {
      if (fallback) {
        fallback.classList.remove("hidden");
        fallback.textContent = "OnlyOffice API не загружен. Проверьте DOCK_EDITOR_URL.";
      }
      return;
    }

    fetch(configUrl, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(async function (response) {
        if (!response.ok) {
          var detail = "";
          try {
            var errorPayload = await response.json();
            detail = (errorPayload && errorPayload.detail) || "";
          } catch (jsonError) {
            detail = "";
          }
          throw new Error(detail || "Failed to load editor config");
        }
        return response.json();
      })
      .then(function (config) {
        if (!config || typeof config !== "object") {
          throw new Error("Invalid editor config payload");
        }
        if (!config.document || !config.editorConfig) {
          throw new Error("Incomplete editor config payload");
        }
        editorContainer.dataset.initialized = "1";
        var editorInstance = new window.DocsAPI.DocEditor("projectOnlyofficeEditor", config);
        var workspaceSidebar = document.getElementById("projectWorkspaceRightSidebar");
        // Сохраняем текущий doc_key для агента
        if (config.document && config.document.key) {
          if (workspaceSidebar) {
            workspaceSidebar.dataset.currentDocKey = config.document.key;
          }
          window.__currentDocKey = config.document.key;
        }
        window.__currentDocKey = config.document.key || "";


        window.__projectOnlyofficeEditorInstance = editorInstance;
        if (fallback) {
          fallback.classList.add("hidden");
          fallback.textContent = "";
        }
      })
      .catch(function (error) {
        console.error(error);
        if (fallback) {
          fallback.classList.remove("hidden");
          fallback.textContent = "Ошибка загрузки редактора. Проверьте ONLYOFFICE_JWT_SECRET и доступность DOCK_EDITOR_URL.";
        }
      });
  }

  function reloadProjectOnlyofficeEditor() {
    var editorContainer = document.getElementById("projectOnlyofficeEditor");
    if (!editorContainer) {
      return;
    }
    var editorInstance = window.__projectOnlyofficeEditorInstance;
    if (editorInstance && typeof editorInstance.destroyEditor === "function") {
      try {
        editorInstance.destroyEditor();
      } catch (error) {
        // ignore destroy errors
      }
    }
    window.__projectOnlyofficeEditorInstance = null;
    editorContainer.dataset.initialized = "0";
    initProjectOnlyofficeEditor();
  }
  window.__reloadProjectOnlyofficeEditor = reloadProjectOnlyofficeEditor;

  function initWorkspacePanelToggles() {
    var shell = document.getElementById("projectWorkspaceShell");
    var backdrop = document.getElementById("projectWorkspaceBackdrop");
    var leftSidebar = document.getElementById("projectWorkspaceLeftSidebar");
    var rightSidebar = document.getElementById("projectWorkspaceRightSidebar");
    var grid = document.getElementById("projectWorkspaceGrid");
    var leftResizer = document.querySelector(".js-workspace-resizer-left");
    var rightResizer = document.querySelector(".js-workspace-resizer-right");
    if (!shell || !leftSidebar || !rightSidebar || !grid) {
      return;
    }
    shell.dataset.workspaceBound = "1";

    var leftToggleButtons = Array.prototype.slice.call(document.querySelectorAll(".js-workspace-left-toggle"));
    var rightToggleButtons = Array.prototype.slice.call(document.querySelectorAll(".js-workspace-right-toggle"));
    if (!leftToggleButtons.length && !rightToggleButtons.length) {
      return;
    }

    var state = {
      leftCollapsed: false,
      rightCollapsed: false,
      leftMobileOpen: false,
      rightMobileOpen: false,
      leftWidth: 320,
      rightWidth: 360,
    };
    var desktopQuery = window.matchMedia("(min-width: 1024px)");
    var storageKey = "suScholar.projectWorkspace.panelState.v2";
    var legacyStorageKey = "suScholar.projectWorkspace.panelState.v1";
    var defaultLeftWidth = 320;
    var defaultRightWidth = 360;
    var minMainWidth = 360;
    var minPanelWidth = 100;
    var autoCloseThreshold = 100;
    var resizerHalfWidth = 7;
    var activeResize = null;
    var editorResizeRaf = null;

    try {
      var savedStateRaw = window.localStorage.getItem(storageKey) || window.localStorage.getItem(legacyStorageKey);
      if (savedStateRaw) {
        var savedState = JSON.parse(savedStateRaw);
        if (savedState && typeof savedState === "object") {
          state.leftCollapsed = Boolean(savedState.leftCollapsed);
          state.rightCollapsed = Boolean(savedState.rightCollapsed);
          if (Number(savedState.leftWidth) > 0) {
            state.leftWidth = Math.round(Number(savedState.leftWidth));
          }
          if (Number(savedState.rightWidth) > 0) {
            state.rightWidth = Math.round(Number(savedState.rightWidth));
          }
        }
      }
    } catch (error) {
      console.warn("Unable to restore workspace panel state", error);
    }

    function persistDesktopState() {
      try {
        window.localStorage.setItem(
          storageKey,
          JSON.stringify({
            leftCollapsed: state.leftCollapsed,
            rightCollapsed: state.rightCollapsed,
            leftWidth: Math.max(minPanelWidth, Math.round(state.leftWidth || defaultLeftWidth)),
            rightWidth: Math.max(minPanelWidth, Math.round(state.rightWidth || defaultRightWidth)),
          })
        );
      } catch (error) {
        console.warn("Unable to persist workspace panel state", error);
      }
    }

    function setButtonText(buttons, expanded, side) {
      buttons.forEach(function (button) {
        button.textContent = "";
        if (side === "left") {
          button.setAttribute("aria-label", expanded ? "Скрыть левую панель" : "Показать левую панель");
        } else {
          button.setAttribute("aria-label", expanded ? "Скрыть чат" : "Показать чат");
        }
        button.setAttribute("aria-expanded", expanded ? "true" : "false");
      });
    }

    function closeMobilePanels() {
      state.leftMobileOpen = false;
      state.rightMobileOpen = false;
    }

    function normalizeDesktopWidths() {
      var totalWidth = Math.round(grid.clientWidth || shell.clientWidth || 0);
      if (!totalWidth) {
        return;
      }

      var left = Math.max(minPanelWidth, Math.round(state.leftWidth || defaultLeftWidth));
      var right = Math.max(minPanelWidth, Math.round(state.rightWidth || defaultRightWidth));
      var leftActive = !state.leftCollapsed;
      var rightActive = !state.rightCollapsed;

      var effectiveLeft = leftActive ? left : 0;
      var effectiveRight = rightActive ? right : 0;
      var mainSpace = totalWidth - effectiveLeft - effectiveRight;

      if (mainSpace < minMainWidth) {
        var deficit = minMainWidth - mainSpace;
        if (leftActive && rightActive) {
          if (effectiveLeft >= effectiveRight) {
            var leftReduce = Math.min(deficit, effectiveLeft - minPanelWidth);
            effectiveLeft -= leftReduce;
            deficit -= leftReduce;
            if (deficit > 0) {
              effectiveRight = Math.max(minPanelWidth, effectiveRight - deficit);
            }
          } else {
            var rightReduce = Math.min(deficit, effectiveRight - minPanelWidth);
            effectiveRight -= rightReduce;
            deficit -= rightReduce;
            if (deficit > 0) {
              effectiveLeft = Math.max(minPanelWidth, effectiveLeft - deficit);
            }
          }
        } else if (leftActive) {
          effectiveLeft = Math.max(minPanelWidth, effectiveLeft - deficit);
        } else if (rightActive) {
          effectiveRight = Math.max(minPanelWidth, effectiveRight - deficit);
        }
      }

      if (leftActive) {
        state.leftWidth = effectiveLeft;
      }
      if (rightActive) {
        state.rightWidth = effectiveRight;
      }
    }

    function setResizerState(isDesktop, effectiveLeft, effectiveRight) {
      if (leftResizer) {
        var showLeft = isDesktop && !state.leftCollapsed;
        leftResizer.style.display = showLeft ? "block" : "none";
        leftResizer.setAttribute("aria-hidden", showLeft ? "false" : "true");
        if (showLeft) {
          leftResizer.style.left = Math.max(0, Math.round(effectiveLeft - resizerHalfWidth)) + "px";
        }
      }

      if (rightResizer) {
        var showRight = isDesktop && !state.rightCollapsed;
        rightResizer.style.display = showRight ? "block" : "none";
        rightResizer.setAttribute("aria-hidden", showRight ? "false" : "true");
        if (showRight) {
          rightResizer.style.right = Math.max(0, Math.round(effectiveRight - resizerHalfWidth)) + "px";
        }
      }
    }

    function setTogglePositions(isDesktop, effectiveLeft, effectiveRight) {
      if (!isDesktop) {
        leftToggleButtons.forEach(function (button) {
          button.style.removeProperty("left");
          button.style.removeProperty("right");
        });
        rightToggleButtons.forEach(function (button) {
          button.style.removeProperty("left");
          button.style.removeProperty("right");
        });
        return;
      }

      leftToggleButtons.forEach(function (button) {
        var nextLeft = state.leftCollapsed ? 22 : Math.max(14, Math.round(effectiveLeft - 20));
        button.style.left = nextLeft + "px";
        button.style.removeProperty("right");
      });

      rightToggleButtons.forEach(function (button) {
        var nextRight = state.rightCollapsed ? 22 : Math.max(14, Math.round(effectiveRight - 20));
        button.style.right = nextRight + "px";
        button.style.removeProperty("left");
      });
    }

    function scheduleEditorResize() {
      if (editorResizeRaf) {
        return;
      }
      editorResizeRaf = window.requestAnimationFrame(function () {
        editorResizeRaf = null;
        try {
          var editorContainer = document.getElementById("projectOnlyofficeEditor");
          var editor = window.__projectOnlyofficeEditorInstance;
          if (editor && typeof editor.resize === "function") {
            if (editorContainer) {
              var width = Math.max(1, Math.round(editorContainer.clientWidth || 0));
              var height = Math.max(1, Math.round(editorContainer.clientHeight || 0));
              editor.resize(width + "px", height + "px");
            } else {
              editor.resize();
            }
            return;
          }
        } catch (error) {
          // ignore runtime resize errors
        }
      });
    }

    function applyDesktopColumns() {
      normalizeDesktopWidths();
      var effectiveLeft = state.leftCollapsed ? 0 : Math.max(minPanelWidth, Math.round(state.leftWidth || defaultLeftWidth));
      var effectiveRight = state.rightCollapsed ? 0 : Math.max(minPanelWidth, Math.round(state.rightWidth || defaultRightWidth));

      grid.style.gridTemplateColumns = effectiveLeft + "px minmax(0, 1fr) " + effectiveRight + "px";
      shell.style.setProperty("--workspace-left-size", effectiveLeft + "px");
      shell.style.setProperty("--workspace-right-size", effectiveRight + "px");
      setResizerState(true, effectiveLeft, effectiveRight);
      setTogglePositions(true, effectiveLeft, effectiveRight);
    }

    function clearDesktopColumns() {
      grid.style.removeProperty("grid-template-columns");
      shell.style.removeProperty("--workspace-left-size");
      shell.style.removeProperty("--workspace-right-size");
      setResizerState(false, 0, 0);
      setTogglePositions(false, 0, 0);
    }

    function setBodyScrollLock(lock) {
      document.body.classList.toggle("overflow-hidden", lock);
    }

    function applyLayout(shouldPersistDesktopState) {
      var isDesktop = desktopQuery.matches;

      shell.classList.toggle("is-left-collapsed", isDesktop && state.leftCollapsed);
      shell.classList.toggle("is-right-collapsed", isDesktop && state.rightCollapsed);

      if (isDesktop) {
        closeMobilePanels();
        applyDesktopColumns();
      } else {
        clearDesktopColumns();
      }

      shell.classList.toggle("is-left-open-mobile", !isDesktop && state.leftMobileOpen);
      shell.classList.toggle("is-right-open-mobile", !isDesktop && state.rightMobileOpen);

      var hasMobileSidebar = !isDesktop && (state.leftMobileOpen || state.rightMobileOpen);
      shell.classList.toggle("has-mobile-sidebar", hasMobileSidebar);
      setBodyScrollLock(hasMobileSidebar);

      leftSidebar.setAttribute("aria-hidden", !isDesktop && !state.leftMobileOpen ? "true" : "false");
      rightSidebar.setAttribute("aria-hidden", !isDesktop && !state.rightMobileOpen ? "true" : "false");

      var leftExpanded = isDesktop ? !state.leftCollapsed : state.leftMobileOpen;
      var rightExpanded = isDesktop ? !state.rightCollapsed : state.rightMobileOpen;
      setButtonText(leftToggleButtons, leftExpanded, "left");
      setButtonText(rightToggleButtons, rightExpanded, "right");

      if (isDesktop && shouldPersistDesktopState) {
        persistDesktopState();
      }
      if (isDesktop) {
        scheduleEditorResize();
      }
    }

    function beginResize(side, event) {
      if (!desktopQuery.matches) {
        return;
      }
      if (typeof event.button === "number" && event.button !== 0) {
        return;
      }
      event.preventDefault();
      activeResize = { side: side };
      document.body.classList.add("workspace-resizing");
      window.addEventListener("pointermove", onResizeMove);
      window.addEventListener("pointerup", endResize);
      window.addEventListener("pointercancel", endResize);
    }

    function onResizeMove(event) {
      if (!activeResize || !desktopQuery.matches) {
        return;
      }
      var rect = grid.getBoundingClientRect();
      var totalWidth = Math.round(rect.width);
      if (!totalWidth) {
        return;
      }

      if (activeResize.side === "left") {
        var rawLeft = event.clientX - rect.left;
        var effectiveRight = state.rightCollapsed ? 0 : Math.max(minPanelWidth, Math.round(state.rightWidth || defaultRightWidth));
        var maxLeft = Math.max(minPanelWidth, totalWidth - effectiveRight - minMainWidth);
        if (rawLeft < autoCloseThreshold) {
          state.leftCollapsed = true;
        } else {
          state.leftCollapsed = false;
          state.leftWidth = Math.min(Math.max(minPanelWidth, Math.round(rawLeft)), maxLeft);
        }
      } else if (activeResize.side === "right") {
        var rawRight = rect.right - event.clientX;
        var effectiveLeft = state.leftCollapsed ? 0 : Math.max(minPanelWidth, Math.round(state.leftWidth || defaultLeftWidth));
        var maxRight = Math.max(minPanelWidth, totalWidth - effectiveLeft - minMainWidth);
        if (rawRight < autoCloseThreshold) {
          state.rightCollapsed = true;
        } else {
          state.rightCollapsed = false;
          state.rightWidth = Math.min(Math.max(minPanelWidth, Math.round(rawRight)), maxRight);
        }
      }

      applyLayout(false);
    }

    function endResize() {
      if (!activeResize) {
        return;
      }
      activeResize = null;
      document.body.classList.remove("workspace-resizing");
      window.removeEventListener("pointermove", onResizeMove);
      window.removeEventListener("pointerup", endResize);
      window.removeEventListener("pointercancel", endResize);
      applyLayout(true);
    }

    leftToggleButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        if (desktopQuery.matches) {
          state.leftCollapsed = !state.leftCollapsed;
          if (!state.leftCollapsed && (!state.leftWidth || state.leftWidth < minPanelWidth)) {
            state.leftWidth = defaultLeftWidth;
          }
          applyLayout(true);
        } else {
          state.leftMobileOpen = !state.leftMobileOpen;
          state.rightMobileOpen = false;
          applyLayout(false);
        }
      });
    });

    rightToggleButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        if (desktopQuery.matches) {
          state.rightCollapsed = !state.rightCollapsed;
          if (!state.rightCollapsed && (!state.rightWidth || state.rightWidth < minPanelWidth)) {
            state.rightWidth = defaultRightWidth;
          }
          applyLayout(true);
        } else {
          state.rightMobileOpen = !state.rightMobileOpen;
          state.leftMobileOpen = false;
          applyLayout(false);
        }
      });
    });

    if (leftResizer) {
      leftResizer.addEventListener("pointerdown", function (event) {
        beginResize("left", event);
      });
    }
    if (rightResizer) {
      rightResizer.addEventListener("pointerdown", function (event) {
        beginResize("right", event);
      });
    }

    if (backdrop) {
      backdrop.addEventListener("click", function () {
        closeMobilePanels();
        applyLayout(false);
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && (state.leftMobileOpen || state.rightMobileOpen)) {
        closeMobilePanels();
        applyLayout(false);
      }
    });

    if (desktopQuery.addEventListener) {
      desktopQuery.addEventListener("change", function () {
        applyLayout(true);
      });
    } else if (desktopQuery.addListener) {
      desktopQuery.addListener(function () {
        applyLayout(true);
      });
    }

    window.addEventListener(
      "resize",
      function () {
        applyLayout(false);
      },
      { passive: true }
    );

    applyLayout(false);
  }

  function initWorkspaceAutoHeight() {
    var shell = document.getElementById("projectWorkspaceShell");
    var grid = document.getElementById("projectWorkspaceGrid");
    if (!shell || !grid) {
      return;
    }
    if (shell.dataset.heightBound === "1") {
      return;
    }
    shell.dataset.heightBound = "1";

    function applyHeight() {
      var viewportHeight = window.innerHeight || document.documentElement.clientHeight || 900;
      var rect = shell.getBoundingClientRect();
      var isMobile = window.innerWidth < 1024;
      var minHeight = isMobile ? 520 : 620;
      var bottomGap = 8;
      var available = Math.floor(viewportHeight - rect.top - bottomGap);
      var finalHeight = Math.max(minHeight, available);

      shell.style.height = finalHeight + "px";
      grid.style.height = "100%";
    }

    applyHeight();
    window.addEventListener("resize", applyHeight, { passive: true });
    window.addEventListener("orientationchange", applyHeight);
    window.addEventListener("load", applyHeight);
    window.setTimeout(applyHeight, 0);
  }

  function initProjectFileModal() {
    var modal = document.getElementById("projectFileModal");
    if (!modal) {
      return;
    }

    var openButtons = Array.prototype.slice.call(document.querySelectorAll("#projectAddFileButton, .js-project-add-file"));
    var closeButtons = Array.prototype.slice.call(modal.querySelectorAll("[data-modal-close='1']"));
    var typeButtons = Array.prototype.slice.call(modal.querySelectorAll(".project-file-type-btn"));
    var typeInput = document.getElementById("projectFileTypeInput");
    var titleInput = document.getElementById("projectFileTitleInput");

    if (!typeInput || !titleInput || !typeButtons.length) {
      return;
    }

    function selectType(fileType) {
      var selectedButton = null;
      typeButtons.forEach(function (button) {
        var selected = button.dataset.fileType === fileType;
        if (selected) {
          selectedButton = button;
        }
        button.classList.toggle("border-slate-900", selected);
        button.classList.toggle("bg-slate-900", selected);
        button.classList.toggle("text-white", selected);
        button.classList.toggle("border-slate-300", !selected);
        button.classList.toggle("bg-white", !selected);
        button.classList.toggle("text-slate-700", !selected);
      });

      typeInput.value = fileType;
      if (selectedButton) {
        var defaultTitle = selectedButton.dataset.defaultTitle || "";
        if (!titleInput.value.trim()) {
          titleInput.value = defaultTitle;
        }
        titleInput.placeholder = defaultTitle;
      }
    }

    function openModal() {
      modal.classList.remove("hidden");
      modal.classList.add("flex");
      modal.setAttribute("aria-hidden", "false");
      document.body.classList.add("overflow-hidden");
      if (!titleInput.value.trim()) {
        titleInput.value = titleInput.placeholder || "";
      }
      titleInput.focus();
    }

    function closeModal() {
      modal.classList.remove("flex");
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      document.body.classList.remove("overflow-hidden");
    }

    openButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        openModal();
      });
    });

    closeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        closeModal();
      });
    });

    modal.addEventListener("click", function (event) {
      if (event.target === modal) {
        closeModal();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.classList.contains("hidden")) {
        closeModal();
      }
    });

    typeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        var fileType = button.dataset.fileType || "docx";
        selectType(fileType);
      });
    });

    selectType(typeInput.value || "docx");
  }

  function initProjectPublicationModal() {
    var modal = document.getElementById("projectPublicationModal");
    if (!modal) {
      return;
    }

    var openButtons = Array.prototype.slice.call(
      document.querySelectorAll("#projectAddPublicationButton, .js-project-add-publication")
    );
    var closeButtons = Array.prototype.slice.call(
      modal.querySelectorAll("[data-publication-modal-close='1']")
    );
    var titleInput = document.getElementById("projectPublicationTitleInput");

    function openModal() {
      modal.classList.remove("hidden");
      modal.classList.add("flex");
      modal.setAttribute("aria-hidden", "false");
      document.body.classList.add("overflow-hidden");
      if (titleInput) {
        titleInput.focus();
      }
    }

    function closeModal() {
      modal.classList.remove("flex");
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      document.body.classList.remove("overflow-hidden");
    }

    openButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        openModal();
      });
    });

    closeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        closeModal();
      });
    });

    modal.addEventListener("click", function (event) {
      if (event.target === modal) {
        closeModal();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.classList.contains("hidden")) {
        closeModal();
      }
    });
  }

  function initProjectPublicationFileModal() {
    var modal = document.getElementById("projectPublicationFileModal");
    if (!modal) {
      return;
    }

    var openButtons = Array.prototype.slice.call(
      document.querySelectorAll(".js-publication-file-modal-trigger")
    );
    var closeButtons = Array.prototype.slice.call(
      modal.querySelectorAll("[data-publication-file-modal-close='1']")
    );
    var createPublicationIdInput = document.getElementById("projectPublicationFileCreatePublicationId");
    var uploadPublicationIdInput = document.getElementById("projectPublicationFileUploadPublicationId");
    var modalTitle = document.getElementById("projectPublicationFileModalTitle");
    var fileTypeInput = document.getElementById("projectPublicationFileTypeInput");
    var descriptionInput = document.getElementById("projectPublicationFileDescriptionInput");
    var typeButtons = Array.prototype.slice.call(
      modal.querySelectorAll(".project-publication-file-type-btn")
    );

    if (!createPublicationIdInput || !uploadPublicationIdInput || !fileTypeInput || !descriptionInput) {
      return;
    }

    function selectType(fileType) {
      var selectedButton = null;
      typeButtons.forEach(function (button) {
        var selected = button.dataset.fileType === fileType;
        if (selected) {
          selectedButton = button;
        }
        button.classList.toggle("border-slate-900", selected);
        button.classList.toggle("bg-slate-900", selected);
        button.classList.toggle("text-white", selected);
        button.classList.toggle("border-slate-300", !selected);
        button.classList.toggle("bg-white", !selected);
        button.classList.toggle("text-slate-700", !selected);
      });

      fileTypeInput.value = fileType;
      if (selectedButton) {
        var defaultTitle = selectedButton.dataset.defaultTitle || "";
        if (!descriptionInput.value.trim()) {
          descriptionInput.value = defaultTitle;
        }
        descriptionInput.placeholder = defaultTitle;
      }
    }

    function openModal(publicationId, publicationTitle) {
      createPublicationIdInput.value = publicationId || "";
      uploadPublicationIdInput.value = publicationId || "";
      if (modalTitle) {
        modalTitle.textContent = publicationTitle || "—";
      }
      modal.classList.remove("hidden");
      modal.classList.add("flex");
      modal.setAttribute("aria-hidden", "false");
      document.body.classList.add("overflow-hidden");
      if (!descriptionInput.value.trim()) {
        descriptionInput.value = descriptionInput.placeholder || "";
      }
      descriptionInput.focus();
    }

    function closeModal() {
      modal.classList.remove("flex");
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      document.body.classList.remove("overflow-hidden");
    }

    openButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        openModal(button.dataset.publicationId || "", button.dataset.publicationTitle || "");
      });
    });

    closeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        closeModal();
      });
    });

    modal.addEventListener("click", function (event) {
      if (event.target === modal) {
        closeModal();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.classList.contains("hidden")) {
        closeModal();
      }
    });

    typeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        selectType(button.dataset.fileType || "docx");
      });
    });

    selectType(fileTypeInput.value || "docx");
  }

  function initProjectAgentChat() {
    var sidebar = document.getElementById("projectWorkspaceRightSidebar");
    var messagesContainer = document.getElementById("projectAgentMessages");
    var input = document.getElementById("projectAgentInput");
    var sendButton = document.getElementById("projectAgentSendButton");
    var modeSelect = document.getElementById("projectAgentThinkingMode");
    var accessSelect = document.getElementById("projectAgentAccessScope");
    var interactionModeSelect = document.getElementById("projectAgentInteractionMode");
    var statusNode = document.getElementById("projectAgentStatus");
    if (!sidebar || !messagesContainer || !input || !sendButton) {
      console.warn("Agent chat elements not found, skipping initialization.");
      console.warn("Expected elements:", { sidebar, messagesContainer, input, sendButton });
      return;
    }
    if (sidebar.dataset.agentChatBound === "1") {
      return;
    }
    sidebar.dataset.agentChatBound = "1";

    var sessionUrl = sidebar.dataset.sessionUrl || "";
    var askUrl = sidebar.dataset.askUrl || "";
    var cancelUrl = sidebar.dataset.cancelUrl || "";
    var sendButtonIdleHtml = (sendButton.innerHTML || "").trim() || '<i class="bi bi-send"></i>';
    var sendButtonStopHtml = '<i class="bi bi-stop-fill"></i>';
    var projectId = String(sidebar.dataset.projectId || "").trim();
    var modeStorageKey = "suScholar.projectAgent.thinkingMode." + (projectId || "default");
    var agentEnabled = true;
    if (!sessionUrl || !askUrl) {
      agentEnabled = false;
      console.warn("Project agent endpoints missing, agent disabled.", { sessionUrl: sessionUrl, askUrl: askUrl });
      if (statusNode) {
        statusNode.textContent = "Агент недоступен на сервере.";
        statusNode.classList.remove("hidden");
        statusNode.classList.add("text-slate-500");
      }
    }

    var sessionId = sidebar.dataset.sessionId || "";
    var documentId = sidebar.dataset.documentId || "";
    var publicationId = sidebar.dataset.publicationId || "";
    var publicationFileId = sidebar.dataset.publicationFileId || "";
    var thinkingMode = "middle";
    var accessScope = "active_project";
    var interactionMode = "ask";
    var loading = false;
    var activeRequestController = null;
    var activeTaskId = "";
    var cancelRequested = false;
    var suppressAgentEvents = false;

    function normalizeThinkingMode(rawValue) {
      var value = String(rawValue || "").toLowerCase().trim();
      if (value === "fast" || value === "middle" || value === "high") {
        return value;
      }
      return "middle";
    }

    function normalizeAccessScope(rawValue) {
      var value = String(rawValue || "").toLowerCase().trim();
      if (value === "active_document" || value === "active_project" || value === "full_access") {
        return value;
      }
      if (value === "high" || value === "full" || value === "all" || value === "global") {
        return "full_access";
      }
      return "active_project";
    }

    function normalizeInteractionMode(rawValue) {
      var value = String(rawValue || "").toLowerCase().trim();
      if (value === "autonomous" || value === "ask") {
        return value;
      }
      return "ask";
    }

    function persistThinkingMode(nextMode) {
      try {
        window.localStorage.setItem(modeStorageKey, nextMode);
      } catch (error) {
        // ignore storage failures
      }
    }

    function readStoredThinkingMode() {
      try {
        return window.localStorage.getItem(modeStorageKey) || "";
      } catch (error) {
        return "";
      }
    }

    function syncThinkingMode(nextMode) {
      thinkingMode = normalizeThinkingMode(nextMode);
      sidebar.dataset.thinkingMode = thinkingMode;
      if (modeSelect && modeSelect.value !== thinkingMode) {
        modeSelect.value = thinkingMode;
      }
      persistThinkingMode(thinkingMode);
    }

    function syncAccessScope(nextScope) {
      accessScope = normalizeAccessScope(nextScope);
      sidebar.dataset.accessScope = accessScope;
      if (accessSelect && accessSelect.value !== accessScope) {
        accessSelect.value = accessScope;
      }
    }

    function syncInteractionMode(nextMode) {
      interactionMode = normalizeInteractionMode(nextMode);
      sidebar.dataset.interactionMode = interactionMode;
      if (interactionModeSelect && interactionModeSelect.value !== interactionMode) {
        interactionModeSelect.value = interactionMode;
      }
    }

    syncThinkingMode(
      modeSelect && modeSelect.value
        ? modeSelect.value
        : (sidebar.dataset.thinkingMode || readStoredThinkingMode() || "middle")
    );

    if (modeSelect) {
      modeSelect.addEventListener("change", function () {
        syncThinkingMode(modeSelect.value);
      });
    }

    syncAccessScope(
      accessSelect && accessSelect.value
        ? accessSelect.value
        : (sidebar.dataset.accessScope || "active_project")
    );

    if (accessSelect) {
      accessSelect.addEventListener("change", function () {
        syncAccessScope(accessSelect.value);
      });
    }

    syncInteractionMode(
      interactionModeSelect && interactionModeSelect.value
        ? interactionModeSelect.value
        : (sidebar.dataset.interactionMode || "ask")
    );

    if (interactionModeSelect) {
      interactionModeSelect.addEventListener("change", function () {
        syncInteractionMode(interactionModeSelect.value);
      });
    }

    function getCsrfToken() {
      var name = "csrftoken=";
      var parts = document.cookie ? document.cookie.split(";") : [];
      for (var i = 0; i < parts.length; i += 1) {
        var cookie = parts[i].trim();
        if (cookie.indexOf(name) === 0) {
          return decodeURIComponent(cookie.substring(name.length));
        }
      }
      var csrfInput = document.querySelector("input[name='csrfmiddlewaretoken']");
      if (csrfInput && csrfInput.value) {
        return csrfInput.value;
      }
      return "";
    }

    function clearStatus() {
      if (!statusNode) {
        return;
      }
      statusNode.textContent = "";
      statusNode.classList.add("hidden");
      statusNode.classList.remove("text-rose-600");
      statusNode.classList.remove("text-emerald-700");
      statusNode.classList.remove("text-slate-500");
    }

    function setStatus(text, tone) {
      if (!statusNode) {
        return;
      }
      statusNode.textContent = text || "";
      statusNode.classList.remove("hidden");
      statusNode.classList.remove("text-rose-600");
      statusNode.classList.remove("text-emerald-700");
      statusNode.classList.remove("text-slate-500");
      statusNode.classList.add(tone || "text-slate-500");
    }

    function setLoading(nextState) {
      loading = !!nextState;
      sendButton.disabled = false;
      sendButton.classList.toggle("opacity-70", loading);
      sendButton.classList.toggle("btn-primary", !loading);
      sendButton.classList.toggle("btn-danger", loading);
      sendButton.innerHTML = loading ? sendButtonStopHtml : sendButtonIdleHtml;
      sendButton.title = loading ? "Остановить" : "Отправить";
      sendButton.setAttribute("aria-label", loading ? "Остановить" : "Отправить");
      input.disabled = loading;
      if (modeSelect) {
        modeSelect.disabled = loading;
      }
      if (accessSelect) {
        accessSelect.disabled = loading;
      }
      if (interactionModeSelect) {
        interactionModeSelect.disabled = loading;
      }
    }

    function requestWithTimeout(url, options, timeoutMs) {
      var hasTimeout = typeof timeoutMs === "number" && timeoutMs > 0;
      if (!window.AbortController || !hasTimeout) {
        return fetch(url, options);
      }
      var controller = new AbortController();
      var timer = window.setTimeout(function () {
        controller.abort();
      }, timeoutMs);
      var requestOptions = Object.assign({}, options || {}, { signal: controller.signal });
      return fetch(url, requestOptions).finally(function () {
        window.clearTimeout(timer);
      });
    }

    function createTaskId() {
      return "agent_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 10);
    }

    function cancelActiveRequest() {
      if (!loading) {
        return;
      }
      cancelRequested = true;
      suppressAgentEvents = true;
      window.__projectAgentSuppressEvents = true;
      setStatus("Останавливаю задачу...", "text-slate-500");
      if (typeof window.clearAgentProcessingStatus === "function") {
        window.clearAgentProcessingStatus("Останавливаю задачу...", false);
      }

      if (activeRequestController) {
        try {
          activeRequestController.abort();
        } catch (error) {
          // ignore abort errors
        }
      }

      if (cancelUrl && activeTaskId) {
        fetch(cancelUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRFToken": getCsrfToken(),
          },
          body: JSON.stringify({
            task_id: activeTaskId,
            session_id: sessionId || null,
          }),
        }).catch(function (error) {
          console.warn("Agent cancel request failed", error);
        });
      }

      setLoading(false);
      input.focus();
    }

    async function parseJsonSafe(response) {
      var text = await response.text();
      if (!text) {
        return {};
      }
      try {
        return JSON.parse(text);
      } catch (error) {
        return { detail: String(text).slice(0, 240) };
      }
    }

    function scrollMessages() {
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }

    function createMessageBubble(role, content) {
      var row = document.createElement("div");
      row.className = role === "user" ? "flex justify-end" : "flex justify-start";

      var bubble = document.createElement("div");
      if (role === "user") {
        bubble.className = "max-w-[92%] rounded-2xl bg-blue-600 px-4 py-3 text-sm text-white shadow-md";
      } else {
        bubble.className = "max-w-[92%] rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-800 shadow-sm";
      }

      var title = document.createElement("div");
      title.className = "mb-1 text-xs font-semibold";
      title.textContent = role === "user" ? "Вы" : "SU Scholar";
      title.classList.add(role === "user" ? "text-slate-200" : "text-slate-500");

      var body = document.createElement("div");
      body.className = "whitespace-pre-wrap break-words";
      body.textContent = content || "";

      bubble.appendChild(title);
      bubble.appendChild(body);
      row.appendChild(bubble);
      messagesContainer.appendChild(row);
      scrollMessages();
      return body;
    }

    function renderDefaultAgentHistory() {
      var defaultMessages = [
      {
        role: "user",
        content: "Подготовь раздел про IoT: основные принципы работы, преимущества и ключевые технологии.",
      },
      {
        role: "assistant",
        content:
          "Собрал базовый черновик: добавил определение Интернета вещей, структуру системы и преимущества автоматизации процессов.",
      },
      {
        role: "assistant",
        content:
          "Применён patch #1: добавлены фрагменты про датчики, контроллеры, беспроводные сети и обмен данными в реальном времени.",
      },
      {
        role: "user",
        content: "Раскрой отдельно вопросы безопасности IoT и добавь абзац про будущее с AI.",
      },
      {
        role: "assistant",
        content:
          "Применён patch #2: добавлен блок про шифрование данных, аутентификацию устройств, защиту сети и обновление прошивок.",
      },
      {
        role: "assistant",
        content:
          "Применён patch #3: финальный абзац про умные города, автономные устройства, AI-анализ данных и адаптивное управление в реальном времени.",
      },
    ];
      messagesContainer.innerHTML = "";
      defaultMessages.forEach(function (message) {
        createMessageBubble(message.role, message.content);
      });
    }

    function renderMessages(messages) {
      messagesContainer.innerHTML = "";
      if (!Array.isArray(messages) || !messages.length) {
        return;
      }
      messages.forEach(function (message) {
        if (!message || typeof message !== "object") {
          return;
        }
        var role = String(message.role || "").toLowerCase() === "assistant" ? "assistant" : "user";
        createMessageBubble(role, String(message.content || ""));
      });
    }

    async function loadSession() {
      
      if (!sessionUrl) {
        return;
      }
      try {
        var search = documentId ? "?document_id=" + encodeURIComponent(documentId) : "";
        var response = await requestWithTimeout(
          sessionUrl + search,
          {
          method: "GET",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          },
          12000
        );
        var data = await parseJsonSafe(response);
        if (!response.ok) {
          throw new Error((data && data.detail) || "Не удалось загрузить историю чата.");
        }
        sessionId = data.session_id || sessionId || "";
        renderMessages(data.messages || []);
        // renderDefaultAgentHistory();
        clearStatus();
      } catch (error) {
        console.error(error);
        setStatus(error.message || "Ошибка загрузки истории чата.", "text-rose-600");
      }
    }

    async function sendMessage() {
      var message = (input.value || "").trim();
      if (!message || loading) {
        return;
      }
      activeTaskId = createTaskId();
      window.__projectAgentActiveTaskId = activeTaskId;
      cancelRequested = false;
      suppressAgentEvents = false;
      window.__projectAgentSuppressEvents = false;
      activeRequestController = window.AbortController ? new AbortController() : null;
      var requestTaskId = activeTaskId;
      var requestController = activeRequestController;
      syncThinkingMode(modeSelect ? modeSelect.value : thinkingMode);
      syncAccessScope(accessSelect ? accessSelect.value : accessScope);
      syncInteractionMode(interactionModeSelect ? interactionModeSelect.value : interactionMode);

      clearStatus();
      input.value = "";
      createMessageBubble("user", message);
      if (typeof window.appendStatus === "function") {
        window.appendStatus("Обрабатываю запрос...");
      }
      setLoading(true);

      if (!agentEnabled) {
        // Agent endpoints are not configured on server — show local fallback reply
        createMessageBubble("assistant", "Агент недоступен на сервере. Попробуйте позже.");
        setStatus("Агент недоступен.", "text-rose-600");
        setLoading(false);
        input.focus();
        return;
      }

      try {
        var response = await requestWithTimeout(
          askUrl,
          {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRFToken": getCsrfToken(),
          },
          body: JSON.stringify({
            message: message,
            session_id: sessionId || null,
            document_id: documentId || null,
            publication_id: publicationId || null,
            publication_file_id: publicationFileId || null,
            thinking_mode: thinkingMode,
            access_scope: accessScope,
            interaction_mode: interactionMode,
            doc_key: window.__currentDocKey || null,
            task_id: requestTaskId,
          }),
          signal: requestController ? requestController.signal : undefined,
          },
          0
        );

        var data = await parseJsonSafe(response);
        if (cancelRequested || (data.task_id && requestTaskId && data.task_id !== requestTaskId)) {
          return;
        }
        if (!response.ok) {
          throw new Error((data && data.detail) || "Не удалось получить ответ агента.");
        }

        sessionId = data.session_id || sessionId || "";
        sidebar.dataset.sessionId = sessionId ? String(sessionId) : "";
        var assistantText = String(data.assistant_message || "").trim();
        if (!window.__projectAgentWsConnected && assistantText) {
          if (data.mode === "error" && typeof window.appendError === "function") {
            window.appendError(assistantText);
          } else if (typeof window.appendAgentComplete === "function") {
            window.appendAgentComplete(data);
          } else {
            createMessageBubble("assistant", assistantText);
          }
        }

        if (data.document_updated) {
          setStatus("Документ обновлён агентом. Чтобы увидеть правки в редакторе, обновите страницу.", "text-emerald-700");
        } else if (Array.isArray(data.warnings) && data.warnings.length) {
          setStatus(data.warnings[0], "text-slate-500");
        } else {
          clearStatus();
        }
      } catch (error) {
        console.error(error);
        if (cancelRequested || (error && error.name === "AbortError")) {
          if (activeTaskId === requestTaskId || window.__projectAgentSuppressEvents) {
            setStatus("Задача остановлена.", "text-slate-500");
            if (typeof window.clearAgentProcessingStatus === "function") {
              window.clearAgentProcessingStatus("Задача остановлена.", false);
            }
          }
          return;
        }
        var errorText = error.message || "Не удалось обработать запрос.";
        if (typeof window.appendError === "function") {
          window.appendError(errorText);
        } else {
          createMessageBubble("assistant", errorText);
        }
        setStatus(errorText, "text-rose-600");
      } finally {
        if (activeRequestController === requestController) {
          activeRequestController = null;
        }
        if (activeTaskId === requestTaskId) {
          window.__projectAgentActiveTaskId = "";
          activeTaskId = "";
        }
        if (!activeTaskId) {
          cancelRequested = false;
          setLoading(false);
          input.focus();
        }
      }
    }

    sendButton.addEventListener("click", function () {
      if (loading) {
        cancelActiveRequest();
        return;
      }
      sendMessage();
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    loadSession(); 
  }

  function initWorkspaceFallback() {
    var shell = document.getElementById("projectWorkspaceShell");
    var backdrop = document.getElementById("projectWorkspaceBackdrop");
    var leftSidebar = document.getElementById("projectWorkspaceLeftSidebar");
    var rightSidebar = document.getElementById("projectWorkspaceRightSidebar");
    var leftToggleButtons = Array.prototype.slice.call(document.querySelectorAll(".js-workspace-left-toggle"));
    var rightToggleButtons = Array.prototype.slice.call(document.querySelectorAll(".js-workspace-right-toggle"));
    if (!shell || !leftSidebar || !rightSidebar || (!leftToggleButtons.length && !rightToggleButtons.length)) {
      return;
    }
    shell.dataset.workspaceBound = "1";
    if (shell.dataset.fallbackBound === "1") {
      return;
    }

    shell.dataset.fallbackBound = "1";

    function isDesktop() {
      return window.innerWidth >= 1024;
    }

    function setButtonText(buttons, expanded, side) {
      buttons.forEach(function (button) {
        button.textContent = "";
        if (side === "left") {
          button.setAttribute("aria-label", expanded ? "Скрыть левую панель" : "Показать левую панель");
        } else {
          button.setAttribute("aria-label", expanded ? "Скрыть чат" : "Показать чат");
        }
        button.setAttribute("aria-expanded", expanded ? "true" : "false");
      });
      
    }

    function closeMobilePanels() {
      shell.classList.remove("is-left-open-mobile");
      shell.classList.remove("is-right-open-mobile");
      shell.classList.remove("has-mobile-sidebar");
      document.body.classList.remove("overflow-hidden");
      leftSidebar.setAttribute("aria-hidden", "true");
      rightSidebar.setAttribute("aria-hidden", "true");
    }

    function applyDesktopClasses() {
      if (!isDesktop()) {
        return;
      }
      closeMobilePanels();
      var leftCollapsed = shell.classList.contains("is-left-collapsed");
      var rightCollapsed = shell.classList.contains("is-right-collapsed");
      setButtonText(leftToggleButtons, !leftCollapsed, "left");
      setButtonText(rightToggleButtons, !rightCollapsed, "right");
      leftSidebar.setAttribute("aria-hidden", "false");
      rightSidebar.setAttribute("aria-hidden", "false");
      
    }

    leftToggleButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        if (isDesktop()) {
          shell.classList.toggle("is-left-collapsed");
          applyDesktopClasses();
          return;
        }
        var isOpen = shell.classList.toggle("is-left-open-mobile");
        shell.classList.remove("is-right-open-mobile");
        shell.classList.toggle("has-mobile-sidebar", isOpen);
        document.body.classList.toggle("overflow-hidden", isOpen);
        setButtonText(leftToggleButtons, isOpen, "left");
        setButtonText(rightToggleButtons, false, "right");
        leftSidebar.setAttribute("aria-hidden", isOpen ? "false" : "true");
        rightSidebar.setAttribute("aria-hidden", "true");
      });
    });

    rightToggleButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        if (isDesktop()) {
          shell.classList.toggle("is-right-collapsed");
          applyDesktopClasses();
          return;
        }
        var isOpen = shell.classList.toggle("is-right-open-mobile");
        shell.classList.remove("is-left-open-mobile");
        shell.classList.toggle("has-mobile-sidebar", isOpen);
        document.body.classList.toggle("overflow-hidden", isOpen);
        setButtonText(rightToggleButtons, isOpen, "right");
        setButtonText(leftToggleButtons, false, "left");
        rightSidebar.setAttribute("aria-hidden", isOpen ? "false" : "true");
        leftSidebar.setAttribute("aria-hidden", "true");
      });
    });

    if (backdrop) {
      backdrop.addEventListener("click", closeMobilePanels);
    }

    window.addEventListener("resize", applyDesktopClasses);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeMobilePanels();
      }
    });

    applyDesktopClasses();
  }

  function safeInit(fn, name) {
    try {
      fn();
      return true;
    } catch (error) {
      console.error("Workspace init failed:", name, error);
      return false;
    }
  }
  ready(function initWorkspace() {
    safeInit(initWorkspaceAutoHeight, "initWorkspaceAutoHeight");
    var panelsReady = safeInit(initWorkspacePanelToggles, "initWorkspacePanelToggles");
    

    if (!panelsReady) {
      safeInit(initWorkspaceFallback, "initWorkspaceFallback");
    }
    safeInit(initProjectOnlyofficeEditor, "initProjectOnlyofficeEditor");
    safeInit(initProjectFileModal, "initProjectFileModal");
    safeInit(initProjectPublicationModal, "initProjectPublicationModal");
    safeInit(initProjectPublicationFileModal, "initProjectPublicationFileModal");
    safeInit(initProjectAgentChat, "initProjectAgentChat"); 
  });
})();
