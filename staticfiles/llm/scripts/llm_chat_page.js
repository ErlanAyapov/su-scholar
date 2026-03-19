(function () {
  if (!("WebSocket" in window)) {
    return;
  }

  const chatPageElement = document.getElementById("llmChatPage");
  const historyElement = document.getElementById("llmChatHistory");
  const messagesElement = document.getElementById("llmChatMessages");
  const formElement = document.getElementById("llmChatForm");
  const inputElement = document.getElementById("llmChatInput");
  const sendButton = document.getElementById("llmChatSendButton");
  const newChatButton = document.getElementById("llmNewChatButton");
  const stopButton = document.getElementById("llmStopButton");
  const deleteChatButton = document.getElementById("llmDeleteChatButton");
  const shareButton = document.getElementById("llmShareChatButton");
  const shareStatusElement = document.getElementById("llmShareStatus");
  const shareDialogElement = document.getElementById("llmShareDialog");
  const shareDialogLinkInput = document.getElementById("llmShareDialogLink");
  const shareDialogOpenButton = document.getElementById("llmShareDialogOpenButton");
  const shareDialogCopyButton = document.getElementById("llmShareDialogCopyButton");

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
  const shareCreateUrl = (chatPageElement && chatPageElement.dataset.shareCreateUrl) || "/llm/share/create/";
  const initialSessionFromUrlRaw = Number(new URLSearchParams(window.location.search).get("session")) || null;
  const initialSessionId = initialSessionFromUrlRaw && initialSessionFromUrlRaw > 0 ? initialSessionFromUrlRaw : null;
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
  let activeSessionId = initialSessionId;
  let promptAfterOpen = null;
  let pendingActions = [];
  let streamingTextNode = null;
  let streamingText = "";
  let latestShareUrl = "";

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  const ALLOWED_INLINE_TAGS = new Set(["a", "strong", "em", "b", "i", "code", "br"]);
  const BLOCKED_INLINE_TAGS = new Set(["script", "style", "iframe", "object", "embed", "link", "meta", "base"]);
  const ALLOWED_INLINE_ATTRS = {
    a: new Set(["href", "title"]),
    strong: new Set([]),
    em: new Set([]),
    b: new Set([]),
    i: new Set([]),
    code: new Set([]),
    br: new Set([]),
  };

  function applyInlineFormatting(value) {
    let text = String(value || "");
    text = text.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>");
    return text;
  }

  function isSafeHref(href) {
    const normalized = String(href || "").trim();
    if (!normalized) {
      return false;
    }
    if (/^https?:\/\//i.test(normalized)) {
      return true;
    }
    return normalized.startsWith("/");
  }

  function unwrapNode(node) {
    const parent = node && node.parentNode;
    if (!parent) {
      return;
    }
    while (node.firstChild) {
      parent.insertBefore(node.firstChild, node);
    }
    parent.removeChild(node);
  }

  function sanitizeInlineNode(rootNode) {
    const children = Array.from(rootNode.childNodes || []);
    children.forEach((child) => {
      if (child.nodeType === Node.TEXT_NODE) {
        return;
      }

      if (child.nodeType !== Node.ELEMENT_NODE) {
        child.parentNode?.removeChild(child);
        return;
      }

      const tagName = String(child.tagName || "").toLowerCase();
      if (BLOCKED_INLINE_TAGS.has(tagName)) {
        child.parentNode?.removeChild(child);
        return;
      }

      if (!ALLOWED_INLINE_TAGS.has(tagName)) {
        unwrapNode(child);
        return;
      }

      const allowedAttrs = ALLOWED_INLINE_ATTRS[tagName] || new Set();
      Array.from(child.attributes || []).forEach((attr) => {
        const attrName = String(attr.name || "").toLowerCase();
        if (attrName.startsWith("on") || !allowedAttrs.has(attrName)) {
          child.removeAttribute(attr.name);
        }
      });

      if (tagName === "a") {
        const href = String(child.getAttribute("href") || "").trim();
        if (!isSafeHref(href)) {
          unwrapNode(child);
          return;
        }
        child.setAttribute("href", href);
        if (/^https?:\/\//i.test(href)) {
          child.setAttribute("target", "_blank");
          child.setAttribute("rel", "noopener noreferrer");
        } else {
          child.removeAttribute("target");
          child.removeAttribute("rel");
        }
      }

      sanitizeInlineNode(child);
    });
  }

  function sanitizeInlineHtml(value) {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = String(value || "");
    sanitizeInlineNode(wrapper);
    return wrapper.innerHTML;
  }

  function renderInlineMarkdown(value) {
    const source = String(value || "");
    const withMarkdown = applyInlineFormatting(source);
    return sanitizeInlineHtml(withMarkdown);
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

  function normalizeFollowUpLine(line) {
    let value = String(line || "").trim();
    value = value.replace(/^[-*•]\s+/, "");
    value = value.replace(/^\d+[.)]\s+/, "");
    return value.trim();
  }

  function isQuestionLikeLine(line) {
    const normalized = normalizeFollowUpLine(line);
    if (!normalized || normalized.length < 6 || normalized.length > 260) {
      return false;
    }
    return /[?？]$/.test(normalized);
  }

  function isFollowUpHeadingLine(line) {
    const normalized = String(line || "").trim().toLowerCase();
    if (!normalized) {
      return false;
    }
    return (
      normalized.includes("следующие вопросы") ||
      normalized.includes("вопросы") ||
      normalized.includes("что дальше") ||
      normalized.includes("дальше")
    );
  }

  function splitAssistantFollowUps(text) {
    const rawLines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    if (!rawLines.length) {
      return { body: String(text || ""), followUps: [] };
    }

    let index = rawLines.length - 1;
    while (index >= 0 && !String(rawLines[index] || "").trim()) {
      index -= 1;
    }
    if (index < 0) {
      return { body: "", followUps: [] };
    }

    const collected = [];
    let firstQuestionIndex = -1;
    while (index >= 0) {
      const line = String(rawLines[index] || "");
      if (isQuestionLikeLine(line)) {
        collected.unshift(normalizeFollowUpLine(line));
        firstQuestionIndex = index;
        index -= 1;
        continue;
      }
      if (!collected.length) {
        index -= 1;
        continue;
      }
      break;
    }

    if (collected.length < 2 || firstQuestionIndex < 0) {
      return { body: String(text || ""), followUps: [] };
    }

    let bodyEndIndex = firstQuestionIndex;
    let headingIndex = index;
    while (headingIndex >= 0) {
      const line = String(rawLines[headingIndex] || "").trim();
      if (!line) {
        bodyEndIndex = headingIndex;
        headingIndex -= 1;
        continue;
      }
      if (isFollowUpHeadingLine(line)) {
        bodyEndIndex = headingIndex;
      }
      break;
    }

    const uniqueFollowUps = [];
    for (const item of collected) {
      if (!item) {
        continue;
      }
      if (!uniqueFollowUps.includes(item)) {
        uniqueFollowUps.push(item);
      }
      if (uniqueFollowUps.length >= 4) {
        break;
      }
    }

    const bodyText = rawLines.slice(0, bodyEndIndex).join("\n").trim();
    return {
      body: bodyText || String(text || "").trim(),
      followUps: uniqueFollowUps,
    };
  }

  function createFollowUpButtonsNode(questions) {
    if (!Array.isArray(questions) || !questions.length) {
      return null;
    }

    const container = document.createElement("div");
    container.className = "chat-followup-buttons";
    questions.forEach((question) => {
      const text = String(question || "").trim();
      if (!text) {
        return;
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chat-followup-btn";
      button.textContent = text;
      button.addEventListener("click", function onFollowUpClick() {
        if (!isChatEnabled || isBusy) {
          return;
        }
        inputElement.value = text;
        submitPrompt(text);
      });
      container.appendChild(button);
    });

    if (!container.childNodes.length) {
      return null;
    }
    return container;
  }

  function setAssistantBubbleContent(textNode, text) {
    const bubble = textNode && textNode.closest ? textNode.closest(".message-bubble") : null;
    const parsed = splitAssistantFollowUps(text);
    const bodyText = parsed.body || "";
    const followUps = parsed.followUps || [];

    setMessageContent(textNode, bodyText);
    if (!bubble) {
      return;
    }

    const oldFollowUps = bubble.querySelectorAll(".chat-followup-buttons");
    oldFollowUps.forEach((item) => item.remove());

    if (!followUps.length) {
      return;
    }

    const followUpsNode = createFollowUpButtonsNode(followUps);
    if (!followUpsNode) {
      return;
    }

    const meta = bubble.querySelector(".message-meta");
    if (meta) {
      bubble.insertBefore(followUpsNode, meta);
      return;
    }
    bubble.appendChild(followUpsNode);
  }

  function setBubbleContentByRole(textNode, text) {
    const row = textNode && textNode.closest ? textNode.closest(".message-row") : null;
    if (row && row.classList.contains("assistant")) {
      setAssistantBubbleContent(textNode, text);
      return;
    }
    setMessageContent(textNode, text);
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

  function getCookie(name) {
    const cookieValue = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith(`${name}=`));
    if (!cookieValue) {
      return "";
    }
    return decodeURIComponent(cookieValue.split("=").slice(1).join("="));
  }

  function setShareStatus(text, isError) {
    if (!shareStatusElement) {
      return;
    }
    shareStatusElement.textContent = String(text || "");
    shareStatusElement.classList.remove("is-success", "is-error");
    if (!text) {
      return;
    }
    shareStatusElement.classList.add(isError ? "is-error" : "is-success");
  }

  function openShareDialog(shareUrl) {
    latestShareUrl = String(shareUrl || "").trim();
    if (!latestShareUrl) {
      return;
    }
    if (shareDialogLinkInput) {
      shareDialogLinkInput.value = latestShareUrl;
      shareDialogLinkInput.focus();
      shareDialogLinkInput.select();
    }
    if (shareDialogElement && typeof shareDialogElement.showModal === "function") {
      shareDialogElement.showModal();
      return;
    }
    window.prompt("Share link", latestShareUrl);
  }

  async function tryNativeShare(shareUrl) {
    if (!navigator.share || !window.isSecureContext) {
      return false;
    }
    try {
      await navigator.share({
        title: "Satbayev AI chat",
        text: "Shared chat from Satbayev AI",
        url: shareUrl,
      });
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return true;
      }
      return false;
    }
  }

  async function copyTextToClipboard(text) {
    if (!text) {
      return false;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_error) {
      // Fallback below.
    }
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "readonly");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    helper.setSelectionRange(0, helper.value.length);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_error) {
      copied = false;
    }
    document.body.removeChild(helper);
    return copied;
  }

  async function createShareLink() {
    if (!isChatEnabled || !activeSessionId || isBusy) {
      return;
    }

    setShareStatus("Preparing share link...", false);
    try {
      const response = await fetch(shareCreateUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: JSON.stringify({ session_id: activeSessionId }),
      });

      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }

      if (!response.ok) {
        const errorText = String(payload.detail || "Failed to create share link.");
        setShareStatus(errorText, true);
        return;
      }

      const shareUrl = String(payload.share_url || "").trim();
      if (!shareUrl) {
        setShareStatus("Share URL is empty.", true);
        return;
      }

      const nativeShared = await tryNativeShare(shareUrl);
      if (!nativeShared) {
        openShareDialog(shareUrl);
      }
      setShareStatus("", false);
    } catch (_error) {
      setShareStatus("Network error while creating share link.", true);
    }
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
    sendButton.classList.toggle("d-none", isBusy);
    stopButton.classList.toggle("d-none", !isBusy);
    deleteChatButton.disabled = !isChatEnabled || isBusy || !activeSessionId;
    if (shareButton) {
      shareButton.disabled = !isChatEnabled || isBusy || !activeSessionId;
    }
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

    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = role === "user" ? "You" : "SU Scholar Assistant";

    bubble.appendChild(textNode);
    bubble.appendChild(meta);
    row.appendChild(bubble);
    messagesElement.appendChild(row);

    setBubbleContentByRole(textNode, text || "");

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
    setBubbleContentByRole(streamingTextNode, streamingText);
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
    setShareStatus("", false);
    renderSessionList();
    sendAction({
      action: "session_open",
      session_id: sessionId,
    });
  }

  function createSession() {
    setShareStatus("", false);
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

  if (shareButton) {
    shareButton.addEventListener("click", function onShareClick() {
      createShareLink();
    });
  }

  if (shareDialogOpenButton) {
    shareDialogOpenButton.addEventListener("click", function onDialogOpenClick() {
      if (!latestShareUrl) {
        return;
      }
      window.open(latestShareUrl, "_blank", "noopener,noreferrer");
    });
  }

  if (shareDialogCopyButton) {
    shareDialogCopyButton.addEventListener("click", async function onDialogCopyClick() {
      if (!latestShareUrl) {
        return;
      }
      const copied = await copyTextToClipboard(latestShareUrl);
      if (copied) {
        setShareStatus("Link copied.", false);
      } else {
        setShareStatus("Copy failed. You can copy from the field.", true);
      }
    });
  }

  if (shareDialogElement) {
    shareDialogElement.addEventListener("click", function onDialogBackdropClick(event) {
      const rect = shareDialogElement.getBoundingClientRect();
      const inside =
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom;
      if (!inside) {
        shareDialogElement.close();
      }
    });
  }

  renderMessages([]);
  renderSessionList();
  connect();
})();
