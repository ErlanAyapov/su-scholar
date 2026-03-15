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
    textNode.textContent = text || "";

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
    streamingAssistantNode.textContent = streamingAssistantText;
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
