(function () {
  if (!("WebSocket" in window)) return;

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/updates/`;
  const reconnectDelayMs = 3000;
  let socket = null;
  let heartbeatTimer = null;

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
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
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

    socket.onclose = () => {
      stopHeartbeat();
      setTimeout(connect, reconnectDelayMs);
    };

    socket.onerror = () => {
      if (socket) socket.close();
    };
  }

  connect();
})();
