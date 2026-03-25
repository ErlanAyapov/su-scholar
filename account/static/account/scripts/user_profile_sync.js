(function () {
  "use strict";

  const modalElement = document.getElementById("userProfileSyncModal");
  if (!modalElement || typeof bootstrap === "undefined") {
    return;
  }

  const form = document.getElementById("userProfileSyncForm");
  const saveButton = document.getElementById("profileSyncSaveButton");
  const startButton = document.getElementById("profileSyncStartButton");
  const hintNode = document.getElementById("profileSyncRequirementHint");
  const logNode = document.getElementById("profileSyncLog");
  const summaryNode = document.getElementById("profileSyncSummary");
  const availabilityNode = document.getElementById("profile-sync-availability");
  const satbayevInput = document.getElementById("id_satbayev_profile_url");
  const syncButton = document.getElementById("userProfileSynchButton");

  if (!form || !saveButton || !startButton || !hintNode || !logNode || !summaryNode) {
    return;
  }

  const fieldsUrl = modalElement.dataset.syncFieldsUrl || "";
  const startUrl = modalElement.dataset.syncStartUrl || "";
  const statusUrlTemplate = modalElement.dataset.syncStatusUrlTemplate || "";
  if (!fieldsUrl || !startUrl || !statusUrlTemplate) {
    return;
  }

  const modal = new bootstrap.Modal(modalElement);
  const inputMap = {
    scopus_id: document.getElementById("id_scopus_id"),
    wos_id: document.getElementById("id_wos_id"),
    google_scholar: document.getElementById("id_google_scholar"),
    researchgate: document.getElementById("id_researchgate"),
    satbayev_profile_url: document.getElementById("id_satbayev_profile_url"),
  };

  let currentAvailability = {};
  let activeTaskId = "";
  let activeTaskSatbayevOnly = false;
  let pollingTimer = null;
  let syncing = false;
  let lastPolledState = "";

  function getCsrfToken() {
    const tokenInput = form.querySelector("input[name='csrfmiddlewaretoken']");
    return tokenInput ? tokenInput.value : "";
  }

  function appendLog(message, level) {
    if (!message) {
      return;
    }
    const emptyNode = logNode.querySelector(".profile-sync-log-empty");
    if (emptyNode) {
      emptyNode.remove();
    }
    const row = document.createElement("div");
    row.className = `profile-sync-log-row ${String(level || "info").toLowerCase()}`;
    const timestamp = new Date().toLocaleTimeString();
    row.textContent = `[${timestamp}] ${message}`;
    logNode.appendChild(row);
    logNode.scrollTop = logNode.scrollHeight;
  }

  function readFieldValues() {
    return {
      scopus_id: (inputMap.scopus_id && inputMap.scopus_id.value || "").trim(),
      wos_id: (inputMap.wos_id && inputMap.wos_id.value || "").trim(),
      google_scholar: (inputMap.google_scholar && inputMap.google_scholar.value || "").trim(),
      researchgate: (inputMap.researchgate && inputMap.researchgate.value || "").trim(),
      satbayev_profile_url: (inputMap.satbayev_profile_url && inputMap.satbayev_profile_url.value || "").trim(),
    };
  }

  function buildAvailability(values, hasOrcid) {
    const hasScopus = Boolean(values.scopus_id);
    const hasWos = Boolean(values.wos_id);
    const hasScholar = Boolean(values.google_scholar);
    const hasResearchgate = Boolean(values.researchgate);
    const hasSatbayev = Boolean(values.satbayev_profile_url);
    const hasAny = hasScopus || hasWos || hasScholar || hasResearchgate || hasSatbayev;
    const onlySatbayev = hasSatbayev && !hasScopus && !hasWos && !hasScholar && !hasResearchgate;
    const importReady = hasScopus || hasWos || hasScholar || Boolean(hasOrcid);
    return {
      has_any: hasAny,
      only_satbayev: onlySatbayev,
      import_ready: importReady,
      has_scopus: hasScopus,
      has_wos: hasWos,
      has_scholar: hasScholar,
      has_researchgate: hasResearchgate,
      has_satbayev: hasSatbayev,
      has_orcid: Boolean(hasOrcid),
      values,
    };
  }

  function setButtonsDisabled(value) {
    const disabled = Boolean(value);
    saveButton.disabled = disabled;
    startButton.disabled = disabled;
  }

  function renderHint() {
    const availability = currentAvailability || {};
    hintNode.classList.remove("alert-danger", "alert-warning", "alert-info", "alert-success");

    if (!availability.has_any) {
      hintNode.classList.add("alert-danger");
      hintNode.textContent = "Укажите минимум одно поле для синхронизации.";
      startButton.disabled = true;
      return;
    }

    if (availability.only_satbayev) {
      hintNode.classList.add("alert-warning");
      hintNode.textContent = "Заполнен только Satbayev profile URL. После сохранения запустится автоматическая синхронизация Satbayev.";
      startButton.disabled = true;
      return;
    }

    if (!availability.import_ready) {
      hintNode.classList.add("alert-warning");
      hintNode.textContent = "Поля заполнены, но импорт публикаций недоступен. Укажите ORCID, Scopus ID, WoS ID или Google Scholar.";
      startButton.disabled = true;
      return;
    }

    hintNode.classList.add("alert-success");
    hintNode.textContent = "Данные заполнены. Можно запускать полный цикл синхронизации.";
    if (!syncing) {
      startButton.disabled = false;
    }
  }

  function renderSummary(result) {
    const payload = result || {};
    summaryNode.classList.remove("d-none");
    const status = String(payload.status || "").toLowerCase();
    const statusLabel = status === "ok" ? "Успешно" : "С предупреждениями";
    const created = Number(payload.created_total || 0);
    const updated = Number(payload.updated_total || 0);
    const errors = Number(payload.error_count || 0);
    const publications = Number(payload.publication_count || 0);
    summaryNode.innerHTML = [
      `<div><strong>Статус:</strong> ${statusLabel}</div>`,
      `<div><strong>Создано:</strong> ${created}</div>`,
      `<div><strong>Обновлено:</strong> ${updated}</div>`,
      `<div><strong>Публикаций в профиле:</strong> ${publications}</div>`,
      `<div><strong>Ошибок:</strong> ${errors}</div>`,
      `<div><strong>Email отчет:</strong> ${payload.email_sent ? "отправлен" : "не отправлен"}</div>`,
    ].join("");
  }

  function applyServerFields(fields) {
    if (!fields || typeof fields !== "object") {
      return;
    }
    Object.keys(inputMap).forEach((fieldName) => {
      if (!Object.prototype.hasOwnProperty.call(fields, fieldName)) {
        return;
      }
      if (!inputMap[fieldName]) {
        return;
      }
      inputMap[fieldName].value = String(fields[fieldName] || "").trim();
    });
  }

  async function saveFields(options) {
    const opts = options || {};
    const payload = readFieldValues();
    const response = await fetch(fieldsUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = data.detail || "Не удалось сохранить поля синхронизации.";
      appendLog(detail, "error");
      throw new Error(detail);
    }

    applyServerFields(data.fields || {});
    currentAvailability = data.availability || buildAvailability(readFieldValues(), currentAvailability.has_orcid);
    renderHint();
    if (!opts.silent) {
      appendLog("Поля синхронизации сохранены.", "success");
    }
    return data;
  }

  function buildStatusUrl(taskId) {
    return statusUrlTemplate.replace("__TASK_ID__", encodeURIComponent(taskId));
  }

  function stopPolling() {
    if (pollingTimer) {
      window.clearInterval(pollingTimer);
      pollingTimer = null;
    }
    syncing = false;
    lastPolledState = "";
    setButtonsDisabled(false);
    renderHint();
  }

  function startPolling() {
    if (!activeTaskId) {
      return;
    }

    const pollOnce = async () => {
      try {
        const response = await fetch(buildStatusUrl(activeTaskId), {
          method: "GET",
          headers: {
            "X-Requested-With": "XMLHttpRequest",
          },
          credentials: "same-origin",
        });
        const data = await response.json();
        if (!response.ok) {
          appendLog(data.detail || "Ошибка получения статуса синхронизации.", "error");
          stopPolling();
          return;
        }

        const state = String(data.state || "").toUpperCase();
        if (state && state !== lastPolledState) {
          appendLog(`Статус задачи: ${state}`, "info");
          lastPolledState = state;
        }

        if (!data.ready) {
          return;
        }

        if (data.ok && data.result) {
          renderSummary(data.result);
          appendLog("Синхронизация завершена.", "success");
          stopPolling();

          const resultSatbayevOnly = Boolean(data.result.satbayev_only);
          if (activeTaskSatbayevOnly || resultSatbayevOnly) {
            const currentUrl = new URL(window.location.href);
            currentUrl.searchParams.set("sync_open", "1");
            window.location.assign(currentUrl.toString());
          }
          return;
        }

        appendLog(data.error || "Синхронизация завершилась с ошибкой.", "error");
        stopPolling();
      } catch (error) {
        appendLog("Ошибка связи с сервером при проверке статуса.", "error");
        stopPolling();
      }
    };

    pollOnce();
    pollingTimer = window.setInterval(pollOnce, 2500);
  }

  async function startSync(options) {
    const opts = options || {};
    const satbayevOnly = Boolean(opts.satbayevOnly);
    const mode = satbayevOnly ? "satbayev_only" : "full";

    const response = await fetch(startUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "same-origin",
      body: JSON.stringify({ mode, force: false }),
    });
    const data = await response.json();
    if (!response.ok) {
      appendLog(data.detail || "Не удалось запустить синхронизацию.", "error");
      throw new Error(data.detail || "Start failed");
    }

    activeTaskId = String(data.task_id || "");
    activeTaskSatbayevOnly = Boolean(data.satbayev_only);
    syncing = true;
    setButtonsDisabled(true);
    renderHint();
    appendLog(`Задача синхронизации поставлена в очередь. Task ID: ${activeTaskId}`, "info");
    startPolling();
    return data;
  }

  async function handleStartFullSync() {
    if (syncing) {
      return;
    }
    summaryNode.classList.add("d-none");

    try {
      setButtonsDisabled(true);
      await saveFields({ silent: true });
      if (!currentAvailability.has_any) {
        appendLog("Укажите минимум одно поле для запуска синхронизации.", "warning");
        return;
      }
      if (currentAvailability.only_satbayev) {
        appendLog("Заполнен только Satbayev profile URL. Будет запущен Satbayev-only режим.", "warning");
        await startSync({ satbayevOnly: true });
        return;
      }
      if (!currentAvailability.import_ready) {
        appendLog("Для полного импорта не хватает ORCID/Scopus/WoS/Google Scholar.", "warning");
        return;
      }
      await startSync({ satbayevOnly: false });
    } catch (_error) {
      // handled by logger
    } finally {
      if (!syncing) {
        setButtonsDisabled(false);
        renderHint();
      }
    }
  }

  async function handleSaveFields(autoStartWhenOnlySatbayev) {
    if (syncing) {
      return;
    }

    try {
      setButtonsDisabled(true);
      const data = await saveFields({ silent: false });
      if (autoStartWhenOnlySatbayev && data.availability && data.availability.only_satbayev) {
        appendLog("Обнаружен режим только Satbayev. Автоматический запуск.", "warning");
        await startSync({ satbayevOnly: true });
      }
    } catch (_error) {
      // handled by logger
    } finally {
      if (!syncing) {
        setButtonsDisabled(false);
        renderHint();
      }
    }
  }

  function hydrateInitialAvailability() {
    if (!availabilityNode) {
      currentAvailability = buildAvailability(readFieldValues(), false);
      renderHint();
      return;
    }

    try {
      currentAvailability = JSON.parse(availabilityNode.textContent || "{}");
    } catch (_error) {
      currentAvailability = buildAvailability(readFieldValues(), false);
    }
    currentAvailability = currentAvailability || {};
    if (!currentAvailability.values) {
      currentAvailability = buildAvailability(readFieldValues(), currentAvailability.has_orcid);
    }
    renderHint();
  }

  function wireWebsocketLogs() {
    window.addEventListener("app:websocket-message", function (event) {
      const payload = event && event.detail ? event.detail : null;
      if (!payload || payload.type !== "sync_log") {
        return;
      }
      const runId = String(payload.sync_run_id || "");
      if (activeTaskId && runId && runId !== activeTaskId) {
        return;
      }
      if (!activeTaskId && runId) {
        return;
      }
      appendLog(payload.message || "Обновление статуса синхронизации.", payload.level || "info");
    });
  }

  saveButton.addEventListener("click", function () {
    handleSaveFields(true);
  });

  startButton.addEventListener("click", function () {
    handleStartFullSync();
  });

  if (satbayevInput) {
    satbayevInput.addEventListener("change", function () {
      const values = readFieldValues();
      const availability = buildAvailability(values, currentAvailability.has_orcid);
      if (availability.only_satbayev) {
        handleSaveFields(true);
      }
    });
  }

  if (syncButton) {
    syncButton.addEventListener("click", function () {
      summaryNode.classList.add("d-none");
    });
  }

  hydrateInitialAvailability();
  wireWebsocketLogs();

  if (modalElement.dataset.openOnLoad === "1") {
    modal.show();
    const currentUrl = new URL(window.location.href);
    currentUrl.searchParams.delete("sync_open");
    window.history.replaceState({}, "", currentUrl.toString());
  }
})();
