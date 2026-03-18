(function () {
  if (!("WebSocket" in window)) {
    return;
  }

  const historyElement = document.getElementById("llmChatHistory");
  const messagesElement = document.getElementById("llmChatMessages");
  const formElement = document.getElementById("llmChatForm");
  const inputElement = document.getElementById("llmChatInput");
  const sendButton = document.getElementById("llmChatSendButton");
  const newChatButton = document.getElementById("llmNewChatButton");
  const stopButton = document.getElementById("llmStopButton");
  const deleteChatButton = document.getElementById("llmDeleteChatButton");

  if (
    !historyElement ||
    !messagesElement ||
    !formElement ||
    !inputElement ||
    !sendButton ||
    !newChatButton ||
    !stopButton ||
    !deleteChatButton
  ) {
    return;
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/llm/`;
  const maxReconnectAttempts = 8;
  const reconnectBaseMs = 700;
  const defaultAssistantText = "Start a new conversation.";

  let socket = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;
  let isBusy = false;
  let isChatEnabled = true;
  let hasLoadedSessions = false;
  let sessions = [];
  let activeSessionId = null;
  let promptAfterOpen = null;
  let pendingActions = [];
  let streamingTextNode = null;
  let streamingText = "";

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderInlineMarkdown(value) {
    return escapeHtml(value).replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>");
  }

  function isPipeRow(line) {
    const raw = String(line || "").trim();
    if (!raw || !raw.includes("|")) {
      return false;
    }
    const normalized = raw.replace(/^\|/, "").replace(/\|$/, "");
    return normalized.includes("|");
  }

  function isTableSeparatorRow(line) {
    const raw = String(line || "").trim();
    if (!raw) {
      return false;
    }
    const normalized = raw.replace(/^\|/, "").replace(/\|$/, "");
    const columns = normalized.split("|").map((item) => item.trim());
    if (!columns.length) {
      return false;
    }
    return columns.every((item) => /^:?-{3,}:?$/.test(item));
  }

  function splitTableRow(line) {
    const normalized = String(line || "")
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "");
    return normalized.split("|").map((item) => item.trim());
  }

  function parseHeadingLine(line) {
    const match = String(line || "").match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (!match) {
      return null;
    }
    return {
      level: Math.min(match[1].length, 6),
      text: match[2].trim(),
    };
  }

  function parseUnorderedListItem(line) {
    const match = String(line || "").match(/^\s*[-*]\s+(.+)$/);
    if (!match) {
      return null;
    }
    return match[1].trim();
  }

  function parseOrderedListItem(line) {
    const match = String(line || "").match(/^\s*\d+\.\s+(.+)$/);
    if (!match) {
      return null;
    }
    return match[1].trim();
  }

  function isHorizontalRuleLine(line) {
    return /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(String(line || ""));
  }

  function renderTable(tableLines) {
    const headerCells = splitTableRow(tableLines[0]);
    if (!headerCells.length) {
      return `<p>${renderInlineMarkdown(tableLines.join("\n")).replace(/\n/g, "<br>")}</p>`;
    }

    const columnCount = headerCells.length;
    const rows = tableLines.slice(2).map(splitTableRow);
    let html = '<div class="chat-md-table"><table><thead><tr>';
    headerCells.forEach((cell) => {
      html += `<th>${renderInlineMarkdown(cell)}</th>`;
    });
    html += "</tr></thead><tbody>";

    rows.forEach((cells) => {
      const normalized = cells.slice(0, columnCount);
      while (normalized.length < columnCount) {
        normalized.push("");
      }
      html += "<tr>";
      normalized.forEach((cell) => {
        html += `<td>${renderInlineMarkdown(cell)}</td>`;
      });
      html += "</tr>";
    });

    html += "</tbody></table></div>";
    return html;
  }

  function renderMessageHtml(text) {
    const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    const parts = [];
    let index = 0;

    while (index < lines.length) {
      if (!String(lines[index] || "").trim()) {
        index += 1;
        continue;
      }

      if (isHorizontalRuleLine(lines[index])) {
        parts.push('<hr class="chat-md-hr">');
        index += 1;
        continue;
      }

      if (isPipeRow(lines[index]) && index + 1 < lines.length && isTableSeparatorRow(lines[index + 1])) {
        const tableLines = [lines[index], lines[index + 1]];
        index += 2;
        while (index < lines.length && lines[index].trim() && isPipeRow(lines[index])) {
          tableLines.push(lines[index]);
          index += 1;
        }
        parts.push(renderTable(tableLines));
        continue;
      }

      const heading = parseHeadingLine(lines[index]);
      if (heading) {
        parts.push(
          `<h${heading.level} class="chat-md-heading chat-md-h${heading.level}">${renderInlineMarkdown(heading.text)}</h${heading.level}>`
        );
        index += 1;
        continue;
      }

      const firstUnorderedItem = parseUnorderedListItem(lines[index]);
      if (firstUnorderedItem !== null) {
        const items = [];
        while (index < lines.length) {
          const item = parseUnorderedListItem(lines[index]);
          if (item === null) {
            break;
          }
          items.push(item);
          index += 1;
        }
        const listHtml = items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("");
        parts.push(`<ul class="chat-md-list">${listHtml}</ul>`);
        continue;
      }

      const firstOrderedItem = parseOrderedListItem(lines[index]);
      if (firstOrderedItem !== null) {
        const items = [];
        while (index < lines.length) {
          const item = parseOrderedListItem(lines[index]);
          if (item === null) {
            break;
          }
          items.push(item);
          index += 1;
        }
        const listHtml = items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("");
        parts.push(`<ol class="chat-md-list chat-md-list-ordered">${listHtml}</ol>`);
        continue;
      }

      const textLines = [];
      while (index < lines.length) {
        if (!String(lines[index] || "").trim()) {
          break;
        }
        if (isHorizontalRuleLine(lines[index])) {
          break;
        }
        if (isPipeRow(lines[index]) && index + 1 < lines.length && isTableSeparatorRow(lines[index + 1])) {
          break;
        }
        if (parseHeadingLine(lines[index])) {
          break;
        }
        if (parseUnorderedListItem(lines[index]) !== null || parseOrderedListItem(lines[index]) !== null) {
          break;
        }
        textLines.push(lines[index]);
        index += 1;
      }
      const paragraph = textLines.map((line) => renderInlineMarkdown(line)).join("<br>");
      if (paragraph) {
        parts.push(`<p>${paragraph}</p>`);
      }
    }

    return parts.join("") || "<p></p>";
  }

  function setMessageContent(node, text) {
    node.innerHTML = renderMessageHtml(text);
  }

  function sessionExists(sessionId) {
    return sessions.some((item) => item.id === sessionId);
  }

  function formatSessionTime(isoValue) {
    if (!isoValue) return "";
    const dt = new Date(isoValue);
    if (Number.isNaN(dt.getTime())) return "";
    return dt.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function scrollMessagesToBottom() {
    messagesElement.scrollTop = messagesElement.scrollHeight;
  }

  function setBusy(nextBusy) {
    isBusy = !!nextBusy;
    const disabled = !isChatEnabled || isBusy;
    inputElement.disabled = disabled;
    sendButton.disabled = disabled;
    newChatButton.disabled = !isChatEnabled || isBusy;
    stopButton.disabled = !isChatEnabled || !isBusy;
    deleteChatButton.disabled = !isChatEnabled || isBusy || !activeSessionId;
  }

  function setChatEnabled(enabled) {
    isChatEnabled = !!enabled;
    setBusy(isBusy);
  }

  function clearMessages() {
    messagesElement.innerHTML = "";
  }

  function createMessageRow(role, text) {
    const row = document.createElement("div");
    row.className = `message-row ${role === "user" ? "user" : "assistant"}`;

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";

    const textNode = document.createElement("div");
    textNode.className = "message-content";
    setMessageContent(textNode, text || "");

    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = role === "user" ? "You" : "SU Scholar Assistant";

    bubble.appendChild(textNode);
    bubble.appendChild(meta);
    row.appendChild(bubble);
    messagesElement.appendChild(row);

    scrollMessagesToBottom();
    return textNode;
  }

  function renderMessages(messages) {
    clearMessages();
    streamingTextNode = null;
    streamingText = "";

    if (!Array.isArray(messages) || !messages.length) {
      createMessageRow("assistant", defaultAssistantText);
      return;
    }

    messages.forEach((item) => {
      const role = item && item.role === "user" ? "user" : "assistant";
      const content = item && typeof item.content === "string" ? item.content : "";
      createMessageRow(role, content);
    });
  }

  function beginAssistantStream() {
    streamingText = "";
    streamingTextNode = createMessageRow("assistant", "");
  }

  function setAssistantStreamText(nextText) {
    streamingText = String(nextText || "");
    if (!streamingTextNode) {
      beginAssistantStream();
    }
    setMessageContent(streamingTextNode, streamingText);
    scrollMessagesToBottom();
  }

  function appendAssistantStreamText(delta) {
    if (!delta) return;
    setAssistantStreamText(streamingText + String(delta));
  }

  function completeAssistantStream(text) {
    const finalText = String(text || streamingText || "").trim();
    if (!finalText) {
      const row = streamingTextNode ? streamingTextNode.closest(".message-row") : null;
      if (row) row.remove();
      streamingTextNode = null;
      streamingText = "";
      return;
    }
    setAssistantStreamText(finalText);
    streamingTextNode = null;
    streamingText = "";
  }

  function failAssistantStream(message) {
    const errorText = `Error: ${message || "generation failed"}`;
    if (!streamingTextNode) {
      beginAssistantStream();
    }
    setAssistantStreamText(errorText);
    streamingTextNode = null;
    streamingText = "";
  }

  function showAssistantInfo(text) {
    clearMessages();
    createMessageRow("assistant", text);
  }

  function upsertSession(session) {
    if (!session || typeof session.id !== "number") {
      return;
    }
    const index = sessions.findIndex((item) => item.id === session.id);
    if (index >= 0) {
      sessions[index] = { ...sessions[index], ...session };
    } else {
      sessions.unshift(session);
    }
    sessions.sort((a, b) => {
      const aTs = Date.parse(a.last_message_at || a.updated || a.created || 0) || 0;
      const bTs = Date.parse(b.last_message_at || b.updated || b.created || 0) || 0;
      return bTs - aTs;
    });
  }

  function removeSession(sessionId) {
    sessions = sessions.filter((item) => item.id !== sessionId);
  }

  function renderSessionList() {
    historyElement.innerHTML = "";

    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "chat-history-item";
      const title = document.createElement("div");
      title.className = "chat-history-item-title";
      title.textContent = "No chats yet";
      const time = document.createElement("div");
      time.className = "chat-history-item-time";
      time.textContent = "Create a new chat";
      empty.appendChild(title);
      empty.appendChild(time);
      historyElement.appendChild(empty);
      setBusy(isBusy);
      return;
    }

    sessions.forEach((session) => {
      const item = document.createElement("a");
      item.href = "#";
      item.className = "chat-history-item";
      if (session.id === activeSessionId) {
        item.classList.add("active");
      }
      item.dataset.sessionId = String(session.id);

      const title = document.createElement("div");
      title.className = "chat-history-item-title";
      title.textContent = session.title || "New dialog";

      const time = document.createElement("div");
      time.className = "chat-history-item-time";
      time.textContent = formatSessionTime(session.last_message_at || session.updated || session.created);

      item.appendChild(title);
      item.appendChild(time);
      historyElement.appendChild(item);
    });
    setBusy(isBusy);
  }

  function queueAction(actionPayload) {
    pendingActions.push(actionPayload);
    connect();
  }

  function sendAction(actionPayload) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(actionPayload));
      return;
    }
    queueAction(actionPayload);
  }

  function flushPendingActions() {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    while (pendingActions.length) {
      const payload = pendingActions.shift();
      socket.send(JSON.stringify(payload));
    }
  }

  function openSession(sessionId) {
    if (!sessionId) return;
    activeSessionId = sessionId;
    renderSessionList();
    sendAction({
      action: "session_open",
      session_id: sessionId,
    });
  }

  function createSession() {
    sendAction({
      action: "session_create",
    });
  }

  function stopGeneration() {
    if (!isChatEnabled || !isBusy) {
      return;
    }
    sendAction({
      action: "generation_stop",
      session_id: activeSessionId,
    });
  }

  function deleteActiveSession() {
    if (!isChatEnabled || isBusy || !activeSessionId) {
      return;
    }
    const confirmed = window.confirm("Delete current chat?");
    if (!confirmed) {
      return;
    }
    sendAction({
      action: "session_delete",
      session_id: activeSessionId,
    });
  }

  function submitPrompt(promptOverride) {
    const prompt = String(promptOverride || inputElement.value || "").trim();
    if (!prompt || isBusy || !isChatEnabled) {
      return;
    }

    if (!activeSessionId) {
      promptAfterOpen = prompt;
      createSession();
      return;
    }

    setBusy(true);
    createMessageRow("user", prompt);
    beginAssistantStream();
    setAssistantStreamText("Connecting to model...");

    sendAction({
      action: "ask",
      prompt,
      session_id: activeSessionId,
    });
    inputElement.value = "";
  }

  function scheduleReconnect() {
    if (reconnectAttempts >= maxReconnectAttempts) {
      if (isBusy) {
        failAssistantStream("WebSocket reconnection failed");
      }
      setBusy(false);
      return;
    }
    const delay = Math.min(15000, reconnectBaseMs * Math.pow(2, reconnectAttempts));
    reconnectAttempts += 1;
    reconnectTimer = window.setTimeout(connect, delay);
  }

  function handlePayload(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }

    if (payload.type === "llm_ready") {
      if (payload.authenticated === false) {
        setChatEnabled(false);
        showAssistantInfo("Sign in to use persistent chat history.");
        return;
      }
      setChatEnabled(true);
      if (!hasLoadedSessions) {
        hasLoadedSessions = true;
        sendAction({ action: "session_list", active_session_id: activeSessionId });
      }
      return;
    }

    if (payload.type === "llm_sessions") {
      sessions = Array.isArray(payload.sessions) ? payload.sessions.slice() : [];
      const preferredId = Number(payload.active_session_id || activeSessionId) || null;
      if (preferredId && sessionExists(preferredId)) {
        activeSessionId = preferredId;
      } else {
        activeSessionId = sessions.length ? sessions[0].id : null;
      }
      renderSessionList();
      if (!sessions.length) {
        createSession();
      } else if (activeSessionId) {
        openSession(activeSessionId);
      }
      return;
    }

    if (payload.type === "llm_session_created" && payload.session) {
      upsertSession(payload.session);
      activeSessionId = payload.session.id;
      renderSessionList();
      openSession(activeSessionId);
      return;
    }

    if (payload.type === "llm_session_opened" && payload.session) {
      upsertSession(payload.session);
      activeSessionId = payload.session.id;
      renderSessionList();
      renderMessages(payload.messages || []);

      if (promptAfterOpen) {
        const queuedPrompt = promptAfterOpen;
        promptAfterOpen = null;
        submitPrompt(queuedPrompt);
      }
      return;
    }

    if (payload.type === "llm_session_deleted") {
      const removedId = Number(payload.session_id) || null;
      if (removedId) {
        removeSession(removedId);
        if (activeSessionId === removedId) {
          activeSessionId = sessions.length ? sessions[0].id : null;
        }
      }
      renderSessionList();
      if (activeSessionId) {
        openSession(activeSessionId);
      } else {
        createSession();
      }
      return;
    }

    if (payload.type === "llm_session_renamed" && payload.session) {
      upsertSession(payload.session);
      renderSessionList();
      return;
    }

    if (payload.type === "llm_started") {
      if (payload.session) {
        upsertSession(payload.session);
        renderSessionList();
      }
      setAssistantStreamText("");
      return;
    }

    if (payload.type === "llm_stopping") {
      if (isBusy && !/Stopping\.\.\.$/m.test(streamingText)) {
        setAssistantStreamText(`${streamingText || ""}${streamingText ? "\n\n" : ""}Stopping...`);
      }
      return;
    }

    if (payload.type === "llm_delta") {
      appendAssistantStreamText(payload.delta || "");
      return;
    }

    if (payload.type === "llm_done") {
      const finalText =
        payload.text ||
        streamingText ||
        (payload.stopped ? `Stopped: ${payload.stop_reason || "Generation stopped"}` : "");
      completeAssistantStream(finalText);
      if (payload.session) {
        upsertSession(payload.session);
        renderSessionList();
      }
      setBusy(false);
      inputElement.focus();
      return;
    }

    if (payload.type === "llm_error") {
      failAssistantStream(payload.message || "Generation failed");
      setBusy(false);
    }
  }

  function connect() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }

    socket = new WebSocket(wsUrl);

    socket.onopen = function onopen() {
      reconnectAttempts = 0;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      flushPendingActions();
    };

    socket.onmessage = function onmessage(event) {
      let payload = null;
      try {
        payload = JSON.parse(event.data || "{}");
      } catch (_error) {
        return;
      }
      handlePayload(payload);
    };

    socket.onclose = function onclose() {
      if (isBusy) {
        failAssistantStream("Connection closed");
        setBusy(false);
      }
      scheduleReconnect();
    };

    socket.onerror = function onerror() {
      if (socket) {
        socket.close();
      }
    };
  }

  formElement.addEventListener("submit", function onSubmit(event) {
    event.preventDefault();
    submitPrompt();
  });

  inputElement.addEventListener("keydown", function onKeydown(event) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    submitPrompt();
  });

  historyElement.addEventListener("click", function onHistoryClick(event) {
    const link = event.target && event.target.closest ? event.target.closest(".chat-history-item[data-session-id]") : null;
    if (!link) {
      return;
    }
    event.preventDefault();
    if (isBusy) {
      return;
    }
    const nextSessionId = Number(link.dataset.sessionId) || null;
    if (!nextSessionId || nextSessionId === activeSessionId) {
      return;
    }
    openSession(nextSessionId);
  });

  newChatButton.addEventListener("click", function onNewChatClick() {
    if (!isChatEnabled || isBusy) {
      return;
    }
    promptAfterOpen = null;
    createSession();
  });

  stopButton.addEventListener("click", function onStopClick() {
    stopGeneration();
  });

  deleteChatButton.addEventListener("click", function onDeleteClick() {
    deleteActiveSession();
  });

  renderMessages([]);
  renderSessionList();
  connect();
})();
