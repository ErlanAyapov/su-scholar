(function () {
  if (!("WebSocket" in window)) return;

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/updates/`;
  const maxReconnectAttempts = 12;
  let socket = null;
  let heartbeatTimer = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;
  let disabled = false;

  function showToast(message, type = "info") {
    if (!message) return;
    if (window.AppFeedback && typeof window.AppFeedback.showToast === "function") {
      window.AppFeedback.showToast(message, { type, duration: 2500 });
      return;
    }
    console.info("[WS]", message);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function startHeartbeat() {
    stopHeartbeat();
    heartbeatTimer = setInterval(() => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ action: "ping" }));
      }
    }, 30000);
  }

  function connect() {
    if (disabled) return;
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      reconnectAttempts = 0;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (typeof window.CustomEvent === "function") {
        window.dispatchEvent(new CustomEvent("app:websocket-open", { detail: { url: wsUrl } }));
      }
      startHeartbeat();
    };

    socket.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data || "{}");
      } catch (error) {
        return;
      }

      if (payload && typeof window.CustomEvent === "function") {
        window.dispatchEvent(new CustomEvent("app:websocket-message", { detail: payload }));
      }

      if (payload.type === "notification" && payload.message) {
        showToast(payload.message, payload.level || "info");
      }
    };

    socket.onclose = (event) => {
      if (typeof window.CustomEvent === "function") {
        window.dispatchEvent(
          new CustomEvent("app:websocket-close", {
            detail: {
              code: event ? event.code : null,
              reason: event ? event.reason : "",
              attempts: reconnectAttempts,
            },
          })
        );
      }
      stopHeartbeat();

      if (disabled) return;
      if (reconnectAttempts >= maxReconnectAttempts) {
        disabled = true;
        if (typeof window.CustomEvent === "function") {
          window.dispatchEvent(
            new CustomEvent("app:websocket-giveup", {
              detail: { attempts: reconnectAttempts, url: wsUrl },
            })
          );
        }
        return;
      }

      const delayMs = Math.min(30000, 1000 * Math.pow(2, Math.min(reconnectAttempts, 5)));
      reconnectAttempts += 1;
      reconnectTimer = setTimeout(connect, delayMs);
    };

    socket.onerror = (event) => {
      if (typeof window.CustomEvent === "function") {
        window.dispatchEvent(new CustomEvent("app:websocket-error", { detail: { event } }));
      }
      if (socket) socket.close();
    };
  }

  connect();
})();
