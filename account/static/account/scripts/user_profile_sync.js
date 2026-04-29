(function () {
  "use strict";

  const modalElement = document.getElementById("userProfileSyncModal");
  if (!modalElement || typeof bootstrap === "undefined") {
    return;
  }

  const form = document.getElementById("userProfileSyncForm");
  const resetButton = document.getElementById("profileSyncResetButton");
  const saveButton = document.getElementById("profileSyncSaveButton");
  const startButton = document.getElementById("profileSyncStartButton");
  const hintNode = document.getElementById("profileSyncRequirementHint");
  const statusTextNode = document.getElementById("profileSyncStatusText");
  const spinnerNode = document.getElementById("profileSyncSpinner");
  const progressWrapNode = document.getElementById("profileSyncProgressWrap");
  const summaryNode = document.getElementById("profileSyncSummary");
  const availabilityNode = document.getElementById("profile-sync-availability");
  const satbayevInput = document.getElementById("id_satbayev_profile_url");
  const syncButton = document.getElementById("userProfileSynchButton");
  const publicationsListNode = document.getElementById("profileRecentPublicationsList");
  const metricNodes = {
    profileMetricPublicationsValue: document.getElementById("profileMetricPublicationsValue"),
    profileMetricPublicationsDelta: document.getElementById("profileMetricPublicationsDelta"),
    profileMetricCitationsValue: document.getElementById("profileMetricCitationsValue"),
    profileMetricCitationsDelta: document.getElementById("profileMetricCitationsDelta"),
    profileMetricGrantsValue: document.getElementById("profileMetricGrantsValue"),
    profileMetricGrantsDelta: document.getElementById("profileMetricGrantsDelta"),
    profileMetricFundingValue: document.getElementById("profileMetricFundingValue"),
    profileMetricFundingDelta: document.getElementById("profileMetricFundingDelta"),
    profileFinanceProjectsCount: document.getElementById("profileFinanceProjectsCount"),
    profileFinanceFundingTotal: document.getElementById("profileFinanceFundingTotal"),
    profileFinanceCollaboratorsCount: document.getElementById("profileFinanceCollaboratorsCount"),
    profileFinanceOpenAccessShare: document.getElementById("profileFinanceOpenAccessShare"),
  };

  if (!form || !saveButton || !startButton || !hintNode || !statusTextNode || !spinnerNode || !progressWrapNode || !summaryNode) {
    return;
  }

  const fieldsUrl = modalElement.dataset.syncFieldsUrl || "";
  const resetUrl = modalElement.dataset.syncResetUrl || "";
  const startUrl = modalElement.dataset.syncStartUrl || "";
  const statusUrlTemplate = modalElement.dataset.syncStatusUrlTemplate || "";
  const publicationsFragmentUrl = modalElement.dataset.publicationsFragmentUrl || "";
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
  let lastSatbayevAutoRunValue = (inputMap.satbayev_profile_url && inputMap.satbayev_profile_url.value || "").trim();

  function getCsrfToken() {
    const tokenInput = form.querySelector("input[name='csrfmiddlewaretoken']");
    return tokenInput ? tokenInput.value : "";
  }

  function normalizeStatusLevel(level) {
    const raw = String(level || "info").toLowerCase();
    if (raw === "success" || raw === "warning" || raw === "error") {
      return raw;
    }
    return "info";
  }

  function compactMessage(message) {
    const value = String(message || "").replace(/\s+/g, " ").trim();
    if (!value) {
      return "";
    }
    if (value.length <= 220) {
      return value;
    }
    return `${value.slice(0, 217)}...`;
  }

  function setStatus(message, level) {
    const nextLevel = normalizeStatusLevel(level);
    statusTextNode.classList.remove("is-info", "is-success", "is-warning", "is-error");
    statusTextNode.classList.add(`is-${nextLevel}`);
    statusTextNode.textContent = compactMessage(message) || "Ожидание запуска синхронизации.";
  }

  function setProgressPending(isPending) {
    const pending = Boolean(isPending);
    spinnerNode.classList.toggle("d-none", !pending);
    progressWrapNode.classList.toggle("d-none", !pending);
    progressWrapNode.setAttribute("aria-hidden", pending ? "false" : "true");
  }

  function getStateStatusMessage(state) {
    const normalizedState = String(state || "").toUpperCase();
    if (normalizedState === "PENDING" || normalizedState === "RECEIVED" || normalizedState === "RETRY") {
      return "Задача поставлена в очередь, ожидается запуск.";
    }
    if (normalizedState === "STARTED") {
      return "Синхронизация запущена, выполняется обработка.";
    }
    if (normalizedState === "PROGRESS") {
      return "Выполняется сбор и парсинг данных из источников.";
    }
    return "";
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
    if (resetButton) {
      resetButton.disabled = disabled;
    }
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
      hintNode.textContent = "Заполнен только Satbayev profile URL. После сохранения профиль автоматически заполнится из Satbayev, затем кнопка запуска станет доступна.";
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

  function renderResetSummary(stats) {
    const payload = stats || {};
    const publicationsDeleted = Number(payload.publications_deleted || 0);
    const authorsDeleted = Number(payload.authors_deleted || 0);
    const projectsDeleted = Number(payload.projects_deleted || 0);
    summaryNode.classList.remove("d-none");
    summaryNode.innerHTML = [
      "<div><strong>Сброс профиля завершен.</strong></div>",
      `<div><strong>Удалено публикаций:</strong> ${publicationsDeleted}</div>`,
      `<div><strong>Удалено соавторов:</strong> ${authorsDeleted}</div>`,
      `<div><strong>Удалено проектов:</strong> ${projectsDeleted}</div>`,
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

  function applyMetricTexts(metricTexts) {
    if (!metricTexts || typeof metricTexts !== "object") {
      return;
    }

    Object.keys(metricNodes).forEach((metricKey) => {
      const targetNode = metricNodes[metricKey];
      if (!targetNode || !Object.prototype.hasOwnProperty.call(metricTexts, metricKey)) {
        return;
      }
      const nextValue = metricTexts[metricKey];
      targetNode.textContent = typeof nextValue === "string" ? nextValue : String(nextValue == null ? "" : nextValue);
    });
  }

  async function refreshRecentPublications() {
    if (!publicationsFragmentUrl || !publicationsListNode) {
      return;
    }

    try {
      const response = await fetch(publicationsFragmentUrl, {
        method: "GET",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "same-origin",
      });
      const data = await response.json();
      if (!response.ok || !data || !data.ok || typeof data.html !== "string") {
        return;
      }
      publicationsListNode.innerHTML = data.html;
      applyMetricTexts(data.metric_texts || {});
    } catch (_error) {
      // Best effort: keep current list if refresh fails.
    }
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
      setStatus(detail, "error");
      throw new Error(detail);
    }

    applyServerFields(data.fields || {});
    currentAvailability = data.availability || buildAvailability(readFieldValues(), currentAvailability.has_orcid);
    renderHint();
    if (!opts.silent) {
      setStatus("Поля синхронизации сохранены.", "success");
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
    setProgressPending(false);
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
          setStatus(data.detail || "Ошибка получения статуса синхронизации.", "error");
          stopPolling();
          return;
        }

        const state = String(data.state || "").toUpperCase();
        if (state && state !== lastPolledState) {
          const stateMessage = getStateStatusMessage(state);
          if (stateMessage) {
            setStatus(stateMessage, "info");
          }
          lastPolledState = state;
        }

        if (!data.ready) {
          return;
        }

        if (data.ok && data.result) {
          renderSummary(data.result);
          await refreshRecentPublications();
          setStatus("Синхронизация завершена.", "success");
          stopPolling();

          const resultSatbayevOnly = Boolean(data.result.satbayev_only);
          if (activeTaskSatbayevOnly || resultSatbayevOnly) {
            const currentUrl = new URL(window.location.href);
            currentUrl.searchParams.set("sync_open", "1");
            window.location.assign(currentUrl.toString());
          }
          return;
        }

        setStatus(data.error || "Синхронизация завершилась с ошибкой.", "error");
        stopPolling();
      } catch (error) {
        setStatus("Ошибка связи с сервером при проверке статуса.", "error");
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
      setStatus(data.detail || "Не удалось запустить синхронизацию.", "error");
      throw new Error(data.detail || "Start failed");
    }

    activeTaskId = String(data.task_id || "");
    activeTaskSatbayevOnly = Boolean(data.satbayev_only);
    if (satbayevOnly) {
      lastSatbayevAutoRunValue = readFieldValues().satbayev_profile_url;
    }
    syncing = true;
    setButtonsDisabled(true);
    renderHint();
    setProgressPending(true);
    setStatus(
      satbayevOnly
        ? "Запущено обновление профиля из Satbayev. Ожидайте завершения."
        : "Запущен поиск и парсинг публикаций по источникам профиля.",
      "info"
    );
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
        setStatus("Укажите минимум одно поле для запуска синхронизации.", "warning");
        return;
      }
      if (currentAvailability.only_satbayev) {
        setStatus("Запущен режим Satbayev-only: сначала заполним профиль из Satbayev.", "warning");
        await startSync({ satbayevOnly: true });
        return;
      }
      if (!currentAvailability.import_ready) {
        setStatus("Для полного импорта не хватает ORCID/Scopus/WoS/Google Scholar.", "warning");
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

  async function handleSaveFields(autoStartSatbayev) {
    if (syncing) {
      return;
    }

    try {
      setButtonsDisabled(true);
      setStatus("Сохраняем поля синхронизации...", "info");
      const data = await saveFields({ silent: false });
      const savedSatbayevUrl = String(
        (data.fields && data.fields.satbayev_profile_url) || readFieldValues().satbayev_profile_url || ""
      ).trim();
      if (!savedSatbayevUrl) {
        lastSatbayevAutoRunValue = "";
      }

      const shouldAutoRunSatbayev = Boolean(
        autoStartSatbayev && savedSatbayevUrl && savedSatbayevUrl !== lastSatbayevAutoRunValue
      );
      if (shouldAutoRunSatbayev) {
        setStatus("Обнаружен Satbayev profile URL. Запускаем автообновление профиля.", "warning");
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

  async function handleResetProfileData() {
    if (!resetButton || !resetUrl) {
      return;
    }
    if (syncing) {
      setStatus("Нельзя выполнять сброс во время активной синхронизации.", "warning");
      return;
    }
    if (!window.confirm("Удалить данные профиля, публикации, проекты и соавторов? Действие необратимо.")) {
      return;
    }

    summaryNode.classList.add("d-none");
    try {
      setButtonsDisabled(true);
      setProgressPending(true);
      setStatus("Выполняется сброс данных профиля...", "warning");
      const response = await fetch(resetUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrfToken(),
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "same-origin",
        body: JSON.stringify({ confirm: true }),
      });
      const data = await response.json();
      if (!response.ok) {
        setStatus(data.detail || "Не удалось выполнить сброс профиля.", "error");
        return;
      }

      applyServerFields(data.fields || {});
      currentAvailability = data.availability || buildAvailability(readFieldValues(), false);
      lastSatbayevAutoRunValue = "";
      await refreshRecentPublications();
      renderHint();
      renderResetSummary(data.stats || {});
      setStatus("Данные профиля успешно очищены.", "success");
    } catch (_error) {
      setStatus("Ошибка связи с сервером при сбросе профиля.", "error");
    } finally {
      setProgressPending(false);
      setButtonsDisabled(false);
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
      setStatus(payload.message || "Обновление статуса синхронизации.", payload.level || "info");
    });
  }

  saveButton.addEventListener("click", function () {
    handleSaveFields(true);
  });

  if (resetButton && resetUrl) {
    resetButton.addEventListener("click", function () {
      handleResetProfileData();
    });
  }

  startButton.addEventListener("click", function () {
    handleStartFullSync();
  });

  if (satbayevInput) {
    satbayevInput.addEventListener("change", function () {
      const values = readFieldValues();
      if (values.satbayev_profile_url) {
        handleSaveFields(true);
      }
    });
  }

  if (syncButton) {
    syncButton.addEventListener("click", function () {
      summaryNode.classList.add("d-none");
      if (!syncing) {
        setProgressPending(false);
        setStatus("Ожидание запуска синхронизации.", "info");
      }
    });
  }

  setProgressPending(false);
  setStatus("Ожидание запуска синхронизации.", "info");
  hydrateInitialAvailability();
  wireWebsocketLogs();

  if (modalElement.dataset.openOnLoad === "1") {
    const currentUrl = new URL(window.location.href);
    if (currentUrl.searchParams.has("sync_open")) {
      setStatus(
        "Профиль обновлен из Satbayev. Проверьте поля и запустите поиск и парсинг публикаций.",
        "success"
      );
    }
    modal.show();
    currentUrl.searchParams.delete("sync_open");
    window.history.replaceState({}, "", currentUrl.toString());
  }
})();
