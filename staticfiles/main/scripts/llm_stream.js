(function () {
  if (!("WebSocket" in window)) return;

  const conversationElement = document.getElementById("llmConversation") || document.querySelector(".llm-response");
  const inputElement = document.getElementById("llmPromptInput") || document.querySelector(".llm-input input");
  const sendButton = document.getElementById("llmSendButton") || document.querySelector(".llm-input button");
  const initialExampleButtons = Array.from(document.querySelectorAll("[data-llm-example]"));
  const initialTextElement = document.getElementById("LlmResponse");

  if (!conversationElement || !inputElement || !sendButton) return;

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/llm/`;
  const maxReconnectAttempts = 8;
  const historyStorageKey = "su_science_llm_chat_history_v1";
  const maxHistoryMessages = 14;
  const maxContextMessages = 10;
  const examplePrompts = initialExampleButtons
    .map((button) => String(button.getAttribute("data-llm-example") || "").trim())
    .filter(Boolean);

  let socket = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;
  let pendingPrompt = null;
  let inProgress = false;
  let examplesDismissed = false;
  let streamingAssistantNode = null;
  let streamingAssistantText = "";

  const defaultAssistantText = (initialTextElement && initialTextElement.textContent
    ? initialTextElement.textContent
    : "Задавайте любые вопросы! Я помогу...").trim();

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

  function sanitizeMessage(item) {
    if (!item || typeof item !== "object") return null;
    const role = String(item.role || "").trim().toLowerCase();
    const content = String(item.content || "").trim();
    if (!content || (role !== "user" && role !== "assistant")) {
      return null;
    }
    return { role, content: content.slice(0, 3000) };
  }

  function loadHistory() {
    try {
      const raw = window.localStorage.getItem(historyStorageKey);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.map(sanitizeMessage).filter(Boolean).slice(-maxHistoryMessages);
    } catch (_error) {
      return [];
    }
  }

  function saveHistory(history) {
    try {
      const normalized = history.map(sanitizeMessage).filter(Boolean).slice(-maxHistoryMessages);
      window.localStorage.setItem(historyStorageKey, JSON.stringify(normalized));
      return normalized;
    } catch (_error) {
      return history.map(sanitizeMessage).filter(Boolean).slice(-maxHistoryMessages);
    }
  }

  let history = loadHistory();

  function setBusy(isBusy) {
    inProgress = isBusy;
    inputElement.disabled = isBusy;
    sendButton.disabled = isBusy;
    const exampleButtons = conversationElement.querySelectorAll("[data-llm-example]");
    exampleButtons.forEach((button) => {
      button.disabled = isBusy;
    });
  }

  function scrollConversation() {
    conversationElement.scrollTop = conversationElement.scrollHeight;
  }

  function appendExamplesNode() {
    if (!examplePrompts.length || examplesDismissed || conversationElement.querySelector("#llmExamples")) {
      return;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "llm-examples";
    wrapper.id = "llmExamples";
    wrapper.setAttribute("aria-label", "Примеры запросов");

    examplePrompts.forEach((prompt) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "llm-example-btn";
      button.setAttribute("data-llm-example", prompt);
      button.textContent = prompt;
      wrapper.appendChild(button);
    });

    conversationElement.appendChild(wrapper);
    scrollConversation();
  }

  function dismissExamplesNode() {
    examplesDismissed = true;
    const wrapper = conversationElement.querySelector("#llmExamples");
    if (wrapper) {
      wrapper.remove();
    }
  }

  function createMessageNode(role, text) {
    const wrapper = document.createElement("div");
    wrapper.className = `llm-message llm-message-${role}`;

    const roleNode = document.createElement("div");
    roleNode.className = "llm-message-role";
    roleNode.textContent = role === "user" ? "Вы" : "Ассистент";

    const textNode = document.createElement("div");
    textNode.className = "llm-message-text";
    setMessageContent(textNode, text || "");

    wrapper.appendChild(roleNode);
    wrapper.appendChild(textNode);
    conversationElement.appendChild(wrapper);

    scrollConversation();
    return textNode;
  }

  function renderHistory() {
    conversationElement.innerHTML = "";
    if (!history.length) {
      createMessageNode("assistant", defaultAssistantText);
      appendExamplesNode();
      return;
    }
    history.forEach((item) => {
      createMessageNode(item.role, item.content);
    });
  }

  function pushHistory(role, content) {
    const normalized = sanitizeMessage({ role, content });
    if (!normalized) return;
    history.push(normalized);
    history = saveHistory(history);
  }

  function contextMessages() {
    return history.slice(-maxContextMessages).map((item) => ({ role: item.role, content: item.content }));
  }

  function beginAssistantStream() {
    streamingAssistantText = "";
    streamingAssistantNode = createMessageNode("assistant", "");
  }

  function setAssistantStreamText(text) {
    streamingAssistantText = text || "";
    if (!streamingAssistantNode) {
      beginAssistantStream();
    }
    setMessageContent(streamingAssistantNode, streamingAssistantText);
    scrollConversation();
  }

  function appendAssistantStream(text) {
    if (!text) return;
    setAssistantStreamText(streamingAssistantText + text);
  }

  function completeAssistantStream(finalText) {
    const content = (finalText || streamingAssistantText || "").trim();
    if (!content) {
      const wrapper = streamingAssistantNode ? streamingAssistantNode.closest(".llm-message") : null;
      if (wrapper) wrapper.remove();
      streamingAssistantNode = null;
      streamingAssistantText = "";
      return;
    }
    setAssistantStreamText(content);
    pushHistory("assistant", content);
    streamingAssistantNode = null;
    streamingAssistantText = "";
  }

  function failAssistantStream(message) {
    const text = `Ошибка: ${message || "ошибка генерации"}`;
    if (!streamingAssistantNode) {
      beginAssistantStream();
    }
    setAssistantStreamText(text);
    pushHistory("assistant", text);
    streamingAssistantNode = null;
    streamingAssistantText = "";
  }

  function scheduleReconnect() {
    if (reconnectAttempts >= maxReconnectAttempts) {
      if (pendingPrompt) {
        failAssistantStream("не удалось подключиться к LLM WebSocket");
        pendingPrompt = null;
      }
      setBusy(false);
      return;
    }

    const delayMs = Math.min(15000, 700 * Math.pow(2, reconnectAttempts));
    reconnectAttempts += 1;
    reconnectTimer = setTimeout(connect, delayMs);
  }

  function sendAsk(prompt) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      pendingPrompt = prompt;
      connect();
      return;
    }
    socket.send(
      JSON.stringify({
        action: "ask",
        prompt: prompt,
        messages: contextMessages(),
      })
    );
  }

  function handlePayload(payload) {
    if (!payload || typeof payload !== "object") return;

    if (payload.type === "llm_started") {
      setAssistantStreamText("");
      return;
    }

    if (payload.type === "llm_delta") {
      appendAssistantStream(payload.delta || "");
      return;
    }

    if (payload.type === "llm_done") {
      completeAssistantStream(payload.text || streamingAssistantText);
      setBusy(false);
      inputElement.focus();
      return;
    }

    if (payload.type === "llm_error") {
      failAssistantStream(payload.message || "ошибка генерации");
      setBusy(false);
    }
  }

  function connect() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }

    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      reconnectAttempts = 0;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (pendingPrompt) {
        const prompt = pendingPrompt;
        pendingPrompt = null;
        sendAsk(prompt);
      }
    };

    socket.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data || "{}");
      } catch (_error) {
        return;
      }
      handlePayload(payload);
    };

    socket.onclose = () => {
      if (inProgress) {
        failAssistantStream("соединение с LLM закрыто. Попробуйте повторить запрос");
        setBusy(false);
      }
      scheduleReconnect();
    };

    socket.onerror = () => {
      if (socket) {
        socket.close();
      }
    };
  }

  function submitPrompt(promptOverride = null) {
    const prompt = String(promptOverride ?? inputElement.value ?? "").trim();
    if (!prompt || inProgress) {
      return;
    }

    pendingPrompt = null;
    setBusy(true);
    createMessageNode("user", prompt);
    pushHistory("user", prompt);
    beginAssistantStream();
    setAssistantStreamText("Подключение к модели...");
    sendAsk(prompt);
    inputElement.value = "";
  }

  sendButton.addEventListener("click", submitPrompt);
  inputElement.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    submitPrompt();
  });
  conversationElement.addEventListener("click", (event) => {
    const button = event.target && event.target.closest ? event.target.closest("[data-llm-example]") : null;
    if (!button || !conversationElement.contains(button)) {
      return;
    }
    if (inProgress) {
      return;
    }
    const prompt = String(button.getAttribute("data-llm-example") || "").trim();
    if (!prompt) {
      return;
    }
    dismissExamplesNode();
    inputElement.value = prompt;
    submitPrompt(prompt);
  });

  renderHistory();
  connect();
})();
