(function () {
  var fullscreenToggleButton = document.getElementById("toggleAiFullscreen");
  var workspaceRootContainer = document.querySelector(".rms-content");
  var siteHeaderElement = document.querySelector("header");
  var siteFooterElement = document.querySelector("footer");
  if (!fullscreenToggleButton || !workspaceRootContainer) {
    return;
  } 
  var fullscreenQueryParamName = "full_screan";
  var isWorkspaceFullscreenEnabled =
    new URL(window.location.href).searchParams.get(fullscreenQueryParamName) === "true";

  function setFullscreenEnterIcon() {
    fullscreenToggleButton.innerHTML = '<i class="bi bi-fullscreen"></i>';
    fullscreenToggleButton.setAttribute("title", "Полный экран");
    fullscreenToggleButton.setAttribute("aria-label", "Переключить полный экран");
  }

  function setFullscreenExitIcon() {
    fullscreenToggleButton.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
    fullscreenToggleButton.setAttribute("title", "Выйти из полного экрана");
    fullscreenToggleButton.setAttribute("aria-label", "Переключить полный экран");
  }

  function updateWorkspaceUrlFullscreenState() {
    var currentPageUrl = new URL(window.location.href);
    if (isWorkspaceFullscreenEnabled) {
      currentPageUrl.searchParams.set(fullscreenQueryParamName, "true");
    } else {
      currentPageUrl.searchParams.delete(fullscreenQueryParamName);
    }
    window.history.replaceState({}, "", currentPageUrl);
  }

  function updateProjectDocumentLinksFullscreenState() {
    var projectDocumentLinks = document.querySelectorAll('a[href*="?file="], a[href*="&file="]');
    projectDocumentLinks.forEach(function (projectDocumentLink) {
      var projectDocumentUrl = new URL(projectDocumentLink.href, window.location.origin);
      if (isWorkspaceFullscreenEnabled) {
        projectDocumentUrl.searchParams.set(fullscreenQueryParamName, "true");
      } else {
        projectDocumentUrl.searchParams.delete(fullscreenQueryParamName);
      }
      projectDocumentLink.href = projectDocumentUrl.toString();
    });
  }

  function triggerWorkspaceRelayout() {
    window.dispatchEvent(new Event("resize"));
  }

  function applyWorkspaceFullscreenMode() {
    isWorkspaceFullscreenEnabled = true;
    document.body.classList.add("ai-fullscreen-mode");
    workspaceRootContainer.classList.add("ai-fullscreen-target");

    if (siteHeaderElement) {
      siteHeaderElement.style.display = "none";
    }
    if (siteFooterElement) {
      siteFooterElement.style.display = "none";
    }

    setFullscreenExitIcon();
    updateWorkspaceUrlFullscreenState();
    updateProjectDocumentLinksFullscreenState();
    triggerWorkspaceRelayout();
  }

  function removeWorkspaceFullscreenMode() {
    isWorkspaceFullscreenEnabled = false;
    document.body.classList.remove("ai-fullscreen-mode");
    workspaceRootContainer.classList.remove("ai-fullscreen-target");

    if (siteHeaderElement) {
      siteHeaderElement.style.display = "";
    }
    if (siteFooterElement) {
      siteFooterElement.style.display = "";
    }

    setFullscreenEnterIcon();
    updateWorkspaceUrlFullscreenState();
    updateProjectDocumentLinksFullscreenState();
    triggerWorkspaceRelayout();
  }

  function toggleWorkspaceFullscreenMode() {
    if (isWorkspaceFullscreenEnabled) {
      removeWorkspaceFullscreenMode();
    } else {
      applyWorkspaceFullscreenMode();
    }
  }

  fullscreenToggleButton.addEventListener("click", toggleWorkspaceFullscreenMode);
  if (isWorkspaceFullscreenEnabled) {
    applyWorkspaceFullscreenMode();
  } else {
    removeWorkspaceFullscreenMode();
  }
})();

(function () {
  var sidebar = document.getElementById("projectWorkspaceRightSidebar");
  var messagesContainer = document.getElementById("projectAgentMessages");
  var statusNode = document.getElementById("projectAgentStatus");
  if (!sidebar || !messagesContainer) {
    return;
  }

  var projectId = parseInt(sidebar.dataset.projectId || "", 10);
  var sessionId = String(sidebar.dataset.sessionId || "").trim();
  var statusRow = null;
  var socket = null;
  var lastAgentMessageText = "";
  var lastAgentMessageTs = 0;
  var lastSuggestionKey = "";
  var lastSuggestionTs = 0;
  var docEditSocket = null;
  var confirmTimer = null;
  var docEditConfirmRow = null;
  var docEditTimerNode = null;
  var docEditActionButtons = [];
  var docEditModal = null;
  window.__projectAgentWsConnected = false;

  function DocEditSocket(projectIdValue, sessionIdValue) {
    this.projectId = String(projectIdValue || "").trim();
    this.sessionId = String(sessionIdValue || "").trim();
    this.ws = null;
    this.onEvent = null;
    this._reconnectTimer = null;
    this._closedManually = false;
  }

  DocEditSocket.prototype._buildUrl = function () {
    var protocol = window.location.protocol === "https:" ? "wss" : "ws";
    return protocol + "://" + window.location.host + "/ws/doc-edit/" + this.projectId + "/" + this.sessionId + "/";
  };

  DocEditSocket.prototype.connect = function () {
    var self = this;
    if (!this.projectId || !this.sessionId) {
      return;
    }
    this._closedManually = false;
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) {
      return;
    }
    try {
      this.ws = new WebSocket(this._buildUrl());
    } catch (error) {
      this._scheduleReconnect();
      return;
    }
    this.ws.onmessage = function (messageEvent) {
      var payload = {};
      try {
        payload = JSON.parse(messageEvent.data || "{}");
      } catch (error) {
        payload = {};
      }
      if (typeof self.onEvent === "function") {
        self.onEvent(payload || {});
      }
    };
    this.ws.onclose = function () {
      self.ws = null;
      if (!self._closedManually) {
        self._scheduleReconnect();
      }
    };
    this.ws.onerror = function () {
      // wait for onclose and reconnect there
    };
  };

  DocEditSocket.prototype._scheduleReconnect = function () {
    var self = this;
    if (this._closedManually || this._reconnectTimer) {
      return;
    }
    this._reconnectTimer = window.setTimeout(function () {
      self._reconnectTimer = null;
      self.connect();
    }, 3000);
  };

  DocEditSocket.prototype.send = function (action) {
    if (!action) {
      return;
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }
    this.ws.send(JSON.stringify({ action: String(action) }));
  };

  DocEditSocket.prototype.disconnect = function () {
    this._closedManually = true;
    if (this._reconnectTimer) {
      window.clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch (error) {
        // ignore close errors
      }
      this.ws = null;
    }
  };

  function ensureSpinnerStyles() {
    if (document.getElementById("projectAgentSpinnerStyles")) {
      return;
    }
    var style = document.createElement("style");
    style.id = "projectAgentSpinnerStyles";
    style.textContent =
      "@keyframes projectAgentSpinnerRotate { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }" +
      ".project-agent-spinner {" +
      "display:inline-block;width:12px;height:12px;border:2px solid #cbd5e1;border-top-color:#0f172a;" +
      "border-radius:9999px;animation:projectAgentSpinnerRotate 0.75s linear infinite;flex-shrink:0;" +
      "}";
    document.head.appendChild(style);
  }

  function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function setFooterStatus(text, isError) {
    if (!statusNode) {
      return;
    }
    statusNode.textContent = text || "";
    statusNode.classList.toggle("hidden", !text);
    statusNode.classList.remove("text-rose-600");
    statusNode.classList.remove("text-slate-500");
    if (text) {
      statusNode.classList.add(isError ? "text-rose-600" : "text-slate-500");
    }
  }

  function removeStatusRow() {
    if (statusRow && statusRow.parentNode) {
      statusRow.parentNode.removeChild(statusRow);
    }
    statusRow = null;
  }

  function clearDocEditConfirmPrompt() {
    if (confirmTimer) {
      window.clearInterval(confirmTimer);
      confirmTimer = null;
    }
    docEditTimerNode = null;
    docEditActionButtons = [];
    if (docEditConfirmRow && docEditConfirmRow.parentNode) {
      docEditConfirmRow.parentNode.removeChild(docEditConfirmRow);
    }
    docEditConfirmRow = null;
  }

  function showDocEditProgress(message) {
    if (typeof window.appendStatus === "function") {
      window.appendStatus(message || "Применяю изменения...");
      return;
    }
    setFooterStatus(message || "Применяю изменения...", false);
  }

  function hideDocEditProgress() {
    removeStatusRow();
    setFooterStatus("", false);
  }

  function setDocEditButtonsDisabled(disabled) {
    docEditActionButtons.forEach(function (button) {
      button.disabled = !!disabled;
      if (disabled) {
        button.classList.add("opacity-60", "cursor-not-allowed");
      } else {
        button.classList.remove("opacity-60", "cursor-not-allowed");
      }
    });
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatOperationText(operation) {
    var op = operation && typeof operation === "object" ? operation : {};
    var opType = String(op.op || "").trim().toLowerCase();
    if (opType === "insert") {
      return (
        "<b>Вставка</b> после §" +
        escapeHtml(op.after_paragraph) +
        ": «" +
        escapeHtml(truncateText(op.text || "", 120)) +
        "»"
      );
    }
    if (opType === "replace") {
      return (
        "<b>Замена</b> в §" +
        escapeHtml(op.paragraph) +
        ": «" +
        escapeHtml(truncateText(op.old_text || "", 80)) +
        "» → «" +
        escapeHtml(truncateText(op.new_text || "", 80)) +
        "»"
      );
    }
    if (opType === "delete_paragraph") {
      return "<b>Удаление</b> §" + escapeHtml(op.paragraph);
    }
    return escapeHtml(JSON.stringify(op));
  }

  function showDocEditConfirmModal(eventData) {
    if (!docEditSocket) {
      return;
    }
    var payload = eventData && typeof eventData === "object" ? eventData : {};
    var plan = payload.plan && typeof payload.plan === "object" ? payload.plan : {};
    var operations = Array.isArray(plan.operations) ? plan.operations : [];
    var timeoutSeconds = 120;
    var remaining = timeoutSeconds;
    var visibleOperations = operations.slice(0, 4);
    var hiddenOperationsCount = Math.max(0, operations.length - visibleOperations.length);
    var summaryText = String(plan.summary || payload.message || "").trim();

    clearDocEditConfirmPrompt();

    var row = document.createElement("div");
    row.className = "flex justify-start";
    row.setAttribute("data-doc-edit-confirm", "1");

    var bubble = document.createElement("div");
    bubble.className =
      "max-w-[94%] rounded-2xl border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-slate-800 shadow-sm";

    var header = document.createElement("div");
    header.className = "mb-1 text-xs font-semibold text-slate-600";
    header.textContent = "SU Scholar · Подтвердите правки";

    var summary = document.createElement("div");
    summary.className = "text-xs leading-5 text-slate-700";
    summary.textContent = summaryText || "LLM предлагает изменения в активном документе.";
    bubble.appendChild(header);
    bubble.appendChild(summary);

    if (visibleOperations.length) {
      var operationsList = document.createElement("ul");
      operationsList.className = "mt-2 list-disc space-y-1 pl-5 text-xs text-slate-700";
      visibleOperations.forEach(function (operation) {
        var item = document.createElement("li");
        item.innerHTML = formatOperationText(operation);
        operationsList.appendChild(item);
      });
      if (hiddenOperationsCount > 0) {
        var moreItem = document.createElement("li");
        moreItem.className = "list-none pl-0 text-slate-500";
        moreItem.textContent = "Еще " + hiddenOperationsCount + " изменений";
        operationsList.appendChild(moreItem);
      }
      bubble.appendChild(operationsList);
    }

    var footer = document.createElement("div");
    footer.className = "mt-2 flex flex-wrap items-center gap-2";

    var confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.className =
      "inline-flex rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:opacity-90";
    confirmButton.textContent = "Применить";

    var rejectButton = document.createElement("button");
    rejectButton.type = "button";
    rejectButton.className =
      "inline-flex rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100";
    rejectButton.textContent = "Отклонить";

    docEditTimerNode = document.createElement("span");
    docEditTimerNode.className = "ml-auto text-[11px] text-slate-500";
    docEditTimerNode.textContent = "Автоотклонение через " + remaining + " с";

    footer.appendChild(confirmButton);
    footer.appendChild(rejectButton);
    footer.appendChild(docEditTimerNode);
    bubble.appendChild(footer);

    row.appendChild(bubble);
    messagesContainer.appendChild(row);
    docEditConfirmRow = row;
    docEditActionButtons = [confirmButton, rejectButton];
    scrollToBottom();

    confirmButton.addEventListener("click", function () {
      setDocEditButtonsDisabled(true);
      if (docEditTimerNode) {
        docEditTimerNode.textContent = "Подтверждено";
      }
      docEditSocket.send("confirm");
      clearDocEditConfirmPrompt();
    });

    rejectButton.addEventListener("click", function () {
      setDocEditButtonsDisabled(true);
      if (docEditTimerNode) {
        docEditTimerNode.textContent = "Отклонено";
      }
      docEditSocket.send("reject");
      clearDocEditConfirmPrompt();
    });

    confirmTimer = window.setInterval(function () {
      remaining -= 1;
      if (docEditTimerNode) {
        docEditTimerNode.textContent = "Автоотклонение через " + Math.max(remaining, 0) + " с";
      }
      if (remaining <= 0) {
        clearDocEditConfirmPrompt();
      }
    }, 1000);
    return;

    clearDocEditConfirmPrompt();
    if (docEditSummaryNode) {
      var summaryText = String(plan.summary || payload.message || "").trim();
      docEditSummaryNode.textContent = summaryText || "LLM предлагает изменения. Применить?";
    }

    if (docEditOpsListNode) {
      docEditOpsListNode.innerHTML = "";
      operations.forEach(function (operation) {
        var li = document.createElement("li");
        li.innerHTML = formatOperationText(operation);
        docEditOpsListNode.appendChild(li);
      });
      if (!operations.length) {
        var emptyLi = document.createElement("li");
        emptyLi.textContent = "Нет детализированного списка операций.";
        docEditOpsListNode.appendChild(emptyLi);
      }
    }

    if (docEditTimerNode) {
      docEditTimerNode.textContent = "Автоотклонение через " + remaining + " с";
    }

    if (confirmTimer) {
      window.clearInterval(confirmTimer);
      confirmTimer = null;
    }
    confirmTimer = window.setInterval(function () {
      remaining -= 1;
      if (docEditTimerNode) {
        docEditTimerNode.textContent = "Автоотклонение через " + Math.max(remaining, 0) + " с";
      }
      if (remaining <= 0) {
        clearDocEditConfirmPrompt();
      }
    }, 1000);

    if (docEditConfirmButton) {
      docEditConfirmButton.onclick = function () {
        clearDocEditConfirmPrompt();
        docEditSocket.send("confirm");
      };
    }
    if (docEditRejectButton) {
      docEditRejectButton.onclick = function () {
        clearDocEditConfirmPrompt();
        docEditSocket.send("reject");
      };
    }

    docEditModal.classList.add("visible");
    docEditModal.setAttribute("aria-hidden", "false");
    document.body.classList.add("overflow-hidden");
  }

  if (docEditModal) {
    var modalCloseNodes = docEditModal.querySelectorAll("[data-doc-edit-close='1']");
    modalCloseNodes.forEach(function (node) {
      node.addEventListener("click", clearDocEditConfirmPrompt);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && docEditModal.classList.contains("visible")) {
        clearDocEditConfirmPrompt();
      }
    });
  }

  function appendRow(sender, text, isError) {
    var normalizedSender = sender === "user" ? "user" : "agent";
    var row = document.createElement("div");
    row.className = normalizedSender === "user" ? "flex justify-end" : "flex justify-start";

    var bubble = document.createElement("div");
    if (normalizedSender === "user") {
      bubble.className = "max-w-[92%] rounded-2xl bg-blue-600 px-4 py-3 text-sm text-white shadow-md";
    } else if (isError) {
      bubble.className =
        "max-w-[92%] rounded-2xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-700 shadow-sm";
    } else {
      bubble.className =
        "max-w-[92%] rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-800 shadow-sm";
    }

    var title = document.createElement("div");
    title.className = "mb-1 text-xs font-semibold";
    title.textContent = normalizedSender === "user" ? "Вы" : "SU Scholar";
    title.classList.add(normalizedSender === "user" ? "text-slate-200" : "text-slate-500");

    var body = document.createElement("div");
    body.className = "whitespace-pre-wrap break-words";
    body.textContent = text || "";

    bubble.appendChild(title);
    bubble.appendChild(body);
    row.appendChild(bubble);
    messagesContainer.appendChild(row);
    scrollToBottom();
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
    return csrfInput && csrfInput.value ? csrfInput.value : "";
  }

  function truncateText(value, limit) {
    var text = String(value || "").trim();
    if (!text || text.length <= limit) {
      return text;
    }
    return text.slice(0, Math.max(0, limit - 1)).trim() + "…";
  }

  async function addPublicationReference(button) {
    if (!button || button.disabled) {
      return;
    }
    var addUrl = sidebar.dataset.referenceAddUrl || "";
    var targetPublicationId = button.getAttribute("data-target-publication-id") || "";
    var referencedPublicationId = button.getAttribute("data-referenced-publication-id") || "";
    if (!addUrl || !targetPublicationId || !referencedPublicationId) {
      setFooterStatus("Не удалось определить публикацию для добавления reference.", true);
      return;
    }

    var initialText = button.textContent || "Добавить как reference";
    button.disabled = true;
    button.textContent = "Добавляю...";

    try {
      var response = await fetch(addUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "X-CSRFToken": getCsrfToken(),
        },
        body: JSON.stringify({
          target_publication_id: targetPublicationId,
          referenced_publication_id: referencedPublicationId,
        }),
      });
      var responseText = await response.text();
      var payload = {};
      try {
        payload = responseText ? JSON.parse(responseText) : {};
      } catch (error) {
        payload = {};
      }
      if (!response.ok) {
        throw new Error((payload && payload.detail) || "Не удалось добавить PublicationReference.");
      }

      button.textContent = payload.status === "already_exists" ? "Уже добавлено" : "Добавлено";
      button.classList.remove("bg-slate-900", "text-white");
      button.classList.add("border", "border-emerald-300", "bg-emerald-50", "text-emerald-700");
      setFooterStatus("PublicationReference добавлен в список литературы.", false);
    } catch (error) {
      button.disabled = false;
      button.textContent = initialText;
      setFooterStatus(error.message || "Не удалось добавить PublicationReference.", true);
    }
  }

  function appendReferenceSuggestions(items, targetTitle) {
    if (!Array.isArray(items) || !items.length) {
      return;
    }

    var suggestionKey =
      String(targetTitle || "") +
      "|" +
      items
        .map(function (item) {
          return String(item.id || "");
        })
        .join(",");
    var now = Date.now();
    if (suggestionKey && suggestionKey === lastSuggestionKey && now - lastSuggestionTs < 3000) {
      return;
    }
    lastSuggestionKey = suggestionKey;
    lastSuggestionTs = now;

    var row = document.createElement("div");
    row.className = "flex justify-start";

    var bubble = document.createElement("div");
    bubble.className =
      "max-w-[92%] rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-800 shadow-sm";

    var header = document.createElement("div");
    header.className = "mb-1 text-xs font-semibold text-slate-500";
    header.textContent = "Релевантные работы";

    var subtitle = document.createElement("div");
    subtitle.className = "mb-3 text-xs text-slate-500";
    subtitle.textContent = targetTitle
      ? "Можно добавить в список литературы публикации: " + targetTitle
      : "";

    var list = document.createElement("div");
    list.className = "space-y-3";

    items.forEach(function (item) {
      var card = document.createElement("div");
      card.className = "rounded-2xl border border-slate-200 bg-slate-50 p-3";

      var cardTitle = document.createElement("div");
      cardTitle.className = "text-sm font-semibold text-slate-900";
      cardTitle.textContent = (item.rank ? item.rank + ". " : "") + String(item.title || "Публикация");
      card.appendChild(cardTitle);

      var metaParts = [];
      if (item.year) {
        metaParts.push(String(item.year));
      }
      if (item.venue) {
        metaParts.push(String(item.venue));
      }
      if (metaParts.length) {
        var meta = document.createElement("div");
        meta.className = "mt-1 text-xs text-slate-500";
        meta.textContent = metaParts.join(" · ");
        card.appendChild(meta);
      }

      if (item.abstract) {
        var abstractNode = document.createElement("div");
        abstractNode.className = "mt-2 text-sm text-slate-700";
        abstractNode.textContent = truncateText(item.abstract, 280);
        card.appendChild(abstractNode);
      }

      var actions = document.createElement("div");
      actions.className = "mt-3 flex flex-wrap items-center gap-2";

      if (item.detail_url) {
        var link = document.createElement("a");
        link.href = item.detail_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className =
          "inline-flex rounded-xl border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100";
        link.textContent = "Открыть";
        actions.appendChild(link);
      }

      var button = document.createElement("button");
      button.type = "button";
      button.className =
        "inline-flex rounded-xl bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:opacity-90";
      button.setAttribute("data-target-publication-id", String(item.target_publication_id || ""));
      button.setAttribute("data-referenced-publication-id", String(item.id || ""));

      if (item.already_added) {
        button.disabled = true;
        button.textContent = "Уже добавлено";
        button.classList.remove("bg-slate-900", "text-white");
        button.classList.add("border", "border-emerald-300", "bg-emerald-50", "text-emerald-700");
      } else if (item.can_add_reference) {
        button.textContent = "Добавить как reference";
        button.addEventListener("click", function () {
          addPublicationReference(button);
        });
      } else {
        button.disabled = true;
        button.textContent = "Добавление недоступно";
        button.classList.remove("bg-slate-900", "text-white");
        button.classList.add("border", "border-slate-300", "bg-slate-100", "text-slate-500");
      }
      actions.appendChild(button);

      if (item.disabled_reason && !item.can_add_reference && !item.already_added) {
        var hint = document.createElement("div");
        hint.className = "basis-full text-xs text-slate-500";
        hint.textContent = String(item.disabled_reason);
        actions.appendChild(hint);
      }

      card.appendChild(actions);
      list.appendChild(card);
    });

    bubble.appendChild(header);
    bubble.appendChild(subtitle);
    bubble.appendChild(list);
    row.appendChild(bubble);
    messagesContainer.appendChild(row);
    scrollToBottom();
  }

  window.appendChatMessage = function (sender, text) {
    var normalizedSender = sender === "user" ? "user" : "agent";
    var normalizedText = String(text || "").trim();
    if (normalizedSender === "agent" && normalizedText) {
      var now = Date.now();
      if (normalizedText === lastAgentMessageText && now - lastAgentMessageTs < 3000) {
        return;
      }
      lastAgentMessageText = normalizedText;
      lastAgentMessageTs = now;
    }
    removeStatusRow();
    setFooterStatus("", false);
    appendRow(normalizedSender, normalizedText || "", false);
  };

  window.appendStatus = function (text) {
    var message = String(text || "").trim();
    if (!message) {
      return;
    }
    ensureSpinnerStyles();
    removeStatusRow();

    statusRow = document.createElement("div");
    statusRow.className = "flex justify-start";
    statusRow.setAttribute("data-agent-status", "1");

    var bubble = document.createElement("div");
    bubble.className =
      "max-w-[92%] rounded-2xl border border-slate-200 bg-slate-100 px-4 py-3 text-sm text-slate-700 shadow-sm";

    var header = document.createElement("div");
    header.className = "mb-1 text-xs font-semibold text-slate-500";
    header.textContent = "SU Scholar";

    var body = document.createElement("div");
    body.className = "flex items-center gap-2 whitespace-pre-wrap break-words";

    var spinner = document.createElement("span");
    spinner.className = "project-agent-spinner";

    var textNode = document.createElement("span");
    textNode.textContent = message;

    body.appendChild(spinner);
    body.appendChild(textNode);
    bubble.appendChild(header);
    bubble.appendChild(body);
    statusRow.appendChild(bubble);
    messagesContainer.appendChild(statusRow);
    setFooterStatus(message, false);
    scrollToBottom();
  };

  window.clearAgentProcessingStatus = function (text, isError) {
    removeStatusRow();
    setFooterStatus(String(text || ""), !!isError);
  };

  window.appendPlan = function (steps) {
    if (!Array.isArray(steps) || !steps.length) {
      window.appendStatus("Формирую план...");
      return;
    }
    var lines = steps
      .map(function (step, index) {
        return index + 1 + ". " + String(step || "").trim();
      })
      .filter(function (line) {
        return !!line;
      });
    window.appendStatus(lines.join("\n") || "Формирую план...");
  };

  window.appendError = function (text) {
    var message = String(text || "Ошибка обработки запроса.").trim();
    removeStatusRow();
    setFooterStatus(message, true);
    appendRow("agent", message, true);
  };

  window.appendAgentComplete = function (payload) {
    var data = payload && typeof payload === "object" ? payload : {};
    var responseText = String(data.assistant_message || data.response_text || "").trim();
    if (responseText) {
      window.appendChatMessage("agent", responseText);
    }
    if (Array.isArray(data.reference_suggestions) && data.reference_suggestions.length) {
      appendReferenceSuggestions(
        data.reference_suggestions,
        data.reference_target_publication_title || sidebar.dataset.publicationTitle || ""
      );
    }
  };

  if (!projectId || !sessionId) {
    return;
  }

  docEditSocket = new DocEditSocket(projectId, sessionId);
  window.__projectDocEditSocket = docEditSocket;
  docEditSocket.onEvent = function (eventData) {
    if (window.__projectAgentSuppressEvents) {
      return;
    }
    var event = eventData && typeof eventData === "object" ? eventData : {};
    var stage = String(event.stage || "").trim().toLowerCase();
    switch (stage) {
      case "plan_ready":
        showDocEditConfirmModal(event);
        break;
      case "confirmed":
      case "applying":
        clearDocEditConfirmPrompt();
        showDocEditProgress(event.message || "Применяю изменения...");
        break;
      case "done":
        clearDocEditConfirmPrompt();
        hideDocEditProgress();
        if (event.message) {
          setFooterStatus(String(event.message), false);
        }
        if (event.reload_editor && typeof window.__reloadProjectOnlyofficeEditor === "function") {
          try {
            window.__reloadProjectOnlyofficeEditor();
          } catch (error) {
            // ignore editor reload errors
          }
        }
        break;
      case "rejected":
        clearDocEditConfirmPrompt();
        hideDocEditProgress();
        setFooterStatus(event.message || "Изменения отклонены.", false);
        break;
      case "timeout":
        clearDocEditConfirmPrompt();
        hideDocEditProgress();
        setFooterStatus(event.message || "Время подтверждения истекло. Изменения не применены.", true);
        break;
      case "error":
        clearDocEditConfirmPrompt();
        hideDocEditProgress();
        setFooterStatus(event.message || "Ошибка document_operation.", true);
        break;
      default:
        break;
    }
  };
  docEditSocket.connect();

  var wsScheme = window.location.protocol === "https:" ? "wss" : "ws";
  var wsUrl = wsScheme + "://" + window.location.host + "/ws/project_agent/" + projectId + "/" + sessionId + "/";
  if (window.__projectAgentWsSocket && window.__projectAgentWsSocket.readyState <= 1) {
    try {
      window.__projectAgentWsSocket.close();
    } catch (error) {
      // ignore close errors for stale sockets
    }
  }
  socket = new WebSocket(wsUrl);
  window.__projectAgentWsSocket = socket;

  socket.onopen = function () {
    window.__projectAgentWsConnected = true;
  };

  socket.onmessage = function (event) {
    var data = {};
    try {
      data = JSON.parse(event.data || "{}");
    } catch (error) {
      return;
    }
    if (window.__projectAgentSuppressEvents && data.type !== "connection_ready") {
      return;
    }
    if (
      data.type === "complete" &&
      data.task_id &&
      window.__projectAgentActiveTaskId &&
      String(data.task_id) !== String(window.__projectAgentActiveTaskId)
    ) {
      return;
    }

    switch (data.type) {
      case "connection_ready":
        if (data.message) {
          console.log(data.message);
        }
        break;
      case "status":
      case "step_started":
      case "step_completed":
        window.appendStatus(data.message || "Обрабатываю запрос...");
        break;
      case "plan":
        window.appendPlan(data.steps || []);
        break;
      case "complete":
        if (typeof window.appendAgentComplete === "function") {
          window.appendAgentComplete(data);
        } else {
          window.appendChatMessage("agent", data.assistant_message || data.response_text || "Готово");
        }
        break;
      case "error":
        window.appendError(data.message || "Ошибка");
        break;
      default:
        break;
    }
  };

  socket.onerror = function () {
    window.__projectAgentWsConnected = false;
  };

  socket.onclose = function () {
    window.__projectAgentWsConnected = false;
  };
})();
