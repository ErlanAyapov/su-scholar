"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const { io } = require("socket.io-client");
const jwt = require("jsonwebtoken");

const PORT = Number(process.env.NODE_PORT || 3000);
const DEFAULT_OO_SERVER = "http://192.168.1.3:8080";
const DEFAULT_JWT_SECRET = "onlyoffice-insecure-1123dv1h3_sm0b%=p3nza3$$s-=ot=p6k8epgh*49mptah9#q9-+toa7asd";
const DEFAULT_APP_BASE_URL = "http://web:8000";
const OO_SERVER = String(process.env.OO_SERVER || DEFAULT_OO_SERVER).trim();
const JWT_SECRET = String(process.env.OO_JWT_SECRET || DEFAULT_JWT_SECRET).trim();
const APP_BASE_URL = String(process.env.APP_BASE_URL || DEFAULT_APP_BASE_URL).trim().replace(/\/+$/, "");
const SOCKET_PATH = process.env.OO_SOCKET_PATH || "/doc/{key}/c";
const PROTOCOL_DEBUG = String(process.env.OO_DEBUG_PROTOCOL || "0") === "1";
const LIVE_PLUGIN_GUID = "asc.{8DFA4E54-52F2-4D8C-8AF1-A8B31C8A4D12}";
const LIVE_PLUGIN_ROOT = path.join(__dirname, "plugins", "llm-doc-editor");
const commandQueues = new Map();
const commandResults = new Map();

function normalizePermissions(rawPermissions) {
  if (!rawPermissions || typeof rawPermissions !== "object") {
    return {
      edit: true,
      download: true,
      print: true,
      comment: true,
      review: true,
    };
  }
  return {
    edit: !!rawPermissions.edit,
    download: !!rawPermissions.download,
    print: !!rawPermissions.print,
    comment: !!rawPermissions.comment,
    review: !!rawPermissions.review,
  };
}

function resolveDocumentType(fileFormat) {
  const normalized = String(fileFormat || "docx").trim().toLowerCase();
  if (["xlsx", "xls", "ods", "csv"].includes(normalized)) {
    return "cell";
  }
  if (["pptx", "ppt", "odp"].includes(normalized)) {
    return "slide";
  }
  return "word";
}

function buildToken({
  docKey,
  userId,
  userName,
  callbackUrl = "",
  fileUrl = "",
  fileTitle = "",
  fileFormat = "docx",
  lang = "ru",
  permissions = null,
}) {
  if (!JWT_SECRET) {
    throw new Error("OO_JWT_SECRET is not set");
  }

  const normalizedPermissions = normalizePermissions(permissions);
  const normalizedFileFormat = String(fileFormat || "docx").trim().toLowerCase() || "docx";
  const documentType = resolveDocumentType(normalizedFileFormat);

  const tokenPayload = {
    documentType,
    document: {
      title: fileTitle || `${docKey}.${normalizedFileFormat}`,
      url: fileUrl || "",
      fileType: normalizedFileFormat,
      key: docKey,
      permissions: normalizedPermissions,
    },
    editorConfig: {
      mode: normalizedPermissions.edit ? "edit" : "view",
      callbackUrl: callbackUrl || "",
      lang: lang || "ru",
      coEditing: {
        mode: "fast",
        change: true,
      },
      user: {
        id: String(userId),
        name: userName,
      },
      customization: {
        autosave: true,
        forcesave: true,
        uiTheme: "theme-light",
      },
    },
  };

  return jwt.sign(
    tokenPayload,
    JWT_SECRET,
    { algorithm: "HS256" }
  );
}

function decodeJwtPayload(token) {
  if (!token || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const raw = Buffer.from(parts[1], "base64url").toString("utf-8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (_err) {
    return null;
  }
}

function resolveDocKeyFromToken(token) {
  const payload = decodeJwtPayload(token);
  if (!payload || typeof payload !== "object") return "";
  const documentPart =
    payload.document && typeof payload.document === "object"
      ? payload.document
      : {};
  return String(documentPart.key || "").trim();
}

function isLikelyJwt(token) {
  const normalized = String(token || "").trim();
  if (!normalized) return false;
  return normalized.split(".").length === 3;
}

function normalizeUrl(urlValue) {
  return String(urlValue || "").trim();
}

function joinUrl(base, path) {
  const normalizedBase = String(base || "").trim().replace(/\/+$/, "");
  const normalizedPath = String(path || "").trim().replace(/^\/+/, "");
  return `${normalizedBase}/${normalizedPath}`;
}

function buildAutoConfigUrl(body = {}) {
  if (body.config_url) {
    return normalizeUrl(body.config_url);
  }

  const baseUrl = normalizeUrl(body.app_base_url || APP_BASE_URL);
  const documentId = Number(body.document_id || 0);
  if (documentId > 0) {
    return joinUrl(baseUrl, `onlyoffice/config/${documentId}/`);
  }

  const projectId = Number(body.project_id || 0);
  const publicationFileId = Number(body.publication_file_id || 0);
  if (projectId > 0 && publicationFileId > 0) {
    return joinUrl(
      baseUrl,
      `llm/project/${projectId}/publication-files/${publicationFileId}/config/`
    );
  }

  return "";
}

function buildConfigFetchHeaders(body = {}) {
  const headers = {
    Accept: "application/json",
  };

  if (body.config_cookie) {
    headers.Cookie = String(body.config_cookie);
  }
  if (body.config_bearer) {
    headers.Authorization = `Bearer ${String(body.config_bearer)}`;
  }
  if (body.config_csrf_token) {
    headers["X-CSRFToken"] = String(body.config_csrf_token);
  }
  if (body.config_headers && typeof body.config_headers === "object") {
    Object.entries(body.config_headers).forEach(([key, value]) => {
      if (!key) return;
      if (value == null) return;
      headers[String(key)] = String(value);
    });
  }

  return headers;
}

async function fetchOnlyOfficeConfig(configUrl, headers = {}) {
  const response = await fetch(configUrl, {
    method: "GET",
    headers,
  });

  const raw = await response.text();
  let payload = {};
  try {
    payload = raw ? JSON.parse(raw) : {};
  } catch (_err) {
    payload = {};
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === "object"
        ? payload.detail || payload.error || raw
        : raw;
    throw new Error(`Failed to fetch config (${response.status}): ${String(detail || "").slice(0, 300)}`);
  }

  if (!payload || typeof payload !== "object") {
    throw new Error("OnlyOffice config response is not JSON object");
  }

  const hasOnlyOfficeShape =
    payload.document &&
    typeof payload.document === "object" &&
    payload.editorConfig &&
    typeof payload.editorConfig === "object";
  if (!hasOnlyOfficeShape) {
    throw new Error(
      "OnlyOffice config payload is invalid (likely unauthorized request to config_url)."
    );
  }

  return payload;
}

async function resolveInsertPayload(body = {}) {
  let config = body.onlyoffice_config;
  if (!config || typeof config !== "object") {
    const configUrl = buildAutoConfigUrl(body);
    if (configUrl) {
      const headers = buildConfigFetchHeaders(body);
      config = await fetchOnlyOfficeConfig(configUrl, headers);
      console.log(`[agent] config resolved from ${configUrl}`);
    } else {
      config = {};
    }
  }

  const documentPart =
    config.document && typeof config.document === "object" ? config.document : {};
  const editorConfigPart =
    config.editorConfig && typeof config.editorConfig === "object"
      ? config.editorConfig
      : {};
  const editorUserPart =
    editorConfigPart.user && typeof editorConfigPart.user === "object"
      ? editorConfigPart.user
      : {};

  const resolvedToken = String(
    body.ws_token || body.jwt_open || config.token || ""
  ).trim();
  const resolvedDocKey = String(
    body.doc_key || documentPart.key || resolveDocKeyFromToken(resolvedToken) || ""
  ).trim();

  return {
    docKey: resolvedDocKey,
    text: String(body.text || "").trim(),
    userId: String(body.user_id || editorUserPart.id || "ai-001"),
    userName: String(body.user_name || editorUserPart.name || "AI Assistant"),
    wsToken: resolvedToken,
    jwtOpen: String(body.jwt_open || config.token || "").trim(),
    authDataToken: String(body.auth_data_token || "").trim(),
    callbackUrl: String(body.callback_url || editorConfigPart.callbackUrl || "").trim(),
    fileUrl: String(body.file_url || documentPart.url || "").trim(),
    fileTitle: String(body.file_title || documentPart.title || "").trim(),
    fileFormat: String(body.file_format || documentPart.fileType || "docx").trim().toLowerCase(),
    lang: String(body.lang || editorConfigPart.lang || "ru").trim(),
    permissions:
      body.permissions && typeof body.permissions === "object"
        ? body.permissions
        : documentPart.permissions,
    timezoneOffset:
      body.timezone_offset != null ? Number(body.timezone_offset) : -300,
    blockId: String(body.block_id || body.blockId || "904").trim() || "904",
    startPos:
      body.start_pos != null
        ? Number(body.start_pos)
        : body.startPos != null
          ? Number(body.startPos)
          : 0,
    sessionId: body.session_id != null ? String(body.session_id) : null,
    sessionTimeConnect:
      body.session_time_connect != null ? Number(body.session_time_connect) : null,
  };
}

function previewPayload(payload) {
  if (payload == null) return "";
  try {
    const normalized =
      typeof payload === "string" ? payload : JSON.stringify(payload);
    if (!normalized) return "";
    return normalized.length > 300
      ? `${normalized.slice(0, 300)}...`
      : normalized;
  } catch (_err) {
    return String(payload);
  }
}

function waitForMessageType(socket, type, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      socket.off("message", onMessage);
      reject(new Error(`Timeout waiting "${type}" (${timeoutMs}ms)`));
    }, timeoutMs);

    function onMessage(data) {
      if (data && typeof data === "object" && String(data.type) === String(type)) {
        clearTimeout(timer);
        socket.off("message", onMessage);
        resolve(data);
      }
    }

    socket.on("message", onMessage);
  });
}

function waitForAnyType(socket, types, timeoutMs = 8000) {
  const expected = new Set((Array.isArray(types) ? types : []).map((v) => String(v)));

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(
        new Error(
          `Timeout waiting one of: ${Array.from(expected).join(", ")} (${timeoutMs}ms)`
        )
      );
    }, timeoutMs);

    function cleanup() {
      clearTimeout(timer);
      socket.off("message", onMessage);
      socket.off("error", onError);
      if (typeof socket.offAny === "function") {
        socket.offAny(onAny);
      }
    }

    function done(event, payload) {
      cleanup();
      resolve({
        event,
        payload: payload && typeof payload === "object" ? payload : {},
      });
    }

    function onAny(event, ...args) {
      const payload = args[0];
      if (expected.has(String(event))) {
        done(String(event), payload);
        return;
      }

      const payloadType =
        payload && typeof payload === "object" && payload.type != null
          ? String(payload.type)
          : "";
      if (payloadType && expected.has(payloadType)) {
        done(payloadType, payload);
      }
    }

    function onMessage(data) {
      if (!data || typeof data !== "object") return;
      const payloadType = data.type != null ? String(data.type) : "";
      if (payloadType && expected.has(payloadType)) {
        done(payloadType, data);
      }
    }

    function onError(err) {
      cleanup();
      reject(new Error(`Socket error: ${err && err.message ? err.message : String(err)}`));
    }

    socket.on("message", onMessage);
    socket.on("error", onError);
    if (typeof socket.onAny === "function") {
      socket.onAny(onAny);
    }
  });
}

function attachProtocolDebug(socket) {
  if (!PROTOCOL_DEBUG || typeof socket.onAny !== "function") return;

  socket.onAny((event, ...args) => {
    const preview = previewPayload(args[0]);
    if (preview) {
      console.log(`[agent][ws] event=${event} payload=${preview}`);
      return;
    }
    console.log(`[agent][ws] event=${event}`);
  });
}

function buildSocketAuthData({
  docKey,
  userId,
  userName,
  wsToken = "",
  authDataToken = "",
  jwtOpen,
  callbackUrl = "",
  fileUrl = "",
  fileTitle = "",
  fileFormat = "docx",
  lang = "ru",
  permissions,
  timezoneOffset = -300,
  sessionId = null,
  sessionTimeConnect = null,
}) {
  const normalizedPermissions = normalizePermissions(permissions);
  const normalizedFileFormat = String(fileFormat || "docx").trim().toLowerCase() || "docx";
  const resolvedToken = String(wsToken || jwtOpen || "").trim();

  const openCmd = {
    c: "open",
    id: docKey,
    userid: String(userId),
    format: normalizedFileFormat,
    url: fileUrl || "",
    title: fileTitle || `${docKey}.${normalizedFileFormat}`,
    nobase64: true,
    outputformat: 8193,
    convertToOrigin: ".pdf.xps.oxps.djvu",
  };

  return {
    type: "auth",
    docid: docKey,
    documentCallbackUrl: callbackUrl || "",
    token: String(authDataToken || resolvedToken || "node-agent").trim(),
    jwt: resolvedToken,
    user: {
      id: String(userId),
      username: userName,
      firstname: null,
      lastname: null,
      indexUser: -1,
    },
    editorType: 0,
    lastOtherSaveTime: -1,
    block: [],
    sessionId: sessionId || null,
    sessionTimeConnect: sessionTimeConnect || null,
    sessionTimeIdle: 0,
    documentFormatSave: 65,
    isCloseCoAuthoring: false,
    openCmd,
    lang,
    mode: normalizedPermissions.edit ? "edit" : "view",
    permissions: normalizedPermissions,
    encrypted: false,
    IsAnonymousUser: false,
    timezoneOffset: Number.isFinite(Number(timezoneOffset))
      ? Number(timezoneOffset)
      : -300,
    headingsColor: null,
    coEditingMode: "fast",
    jwtOpen: String(jwtOpen || resolvedToken || "").trim(),
    time: Date.now(),
    supportAuthChangesAck: true,
  };
}

function connectToSession(docKey, userId, userName, options = {}) {
  return new Promise((resolve, reject) => {
    let resolvedDocKey = String(docKey || "").trim();
    const providedToken = String(options.wsToken || options.jwtOpen || "").trim();
    let wsToken = providedToken;

    const tokenDocKey = resolveDocKeyFromToken(providedToken);
    if (tokenDocKey && tokenDocKey !== resolvedDocKey) {
      console.log(
        `[agent] doc_key mismatch, using key from token: ${tokenDocKey} (instead of ${resolvedDocKey || "empty"})`
      );
      resolvedDocKey = tokenDocKey;
    }

    if (!resolvedDocKey) {
      reject(new Error("doc_key is empty and cannot be resolved from ws_token/jwt_open"));
      return;
    }

    if (!wsToken) {
      wsToken = buildToken({
        docKey: resolvedDocKey,
        userId,
        userName,
        callbackUrl: options.callbackUrl || "",
        fileUrl: options.fileUrl || "",
        fileTitle: options.fileTitle || "",
        fileFormat: options.fileFormat || "docx",
        lang: options.lang || "ru",
        permissions: options.permissions,
      });
    }

    const socketPath = SOCKET_PATH.replace("{key}", resolvedDocKey);
    const authMessage = buildSocketAuthData({
      docKey: resolvedDocKey,
      userId,
      userName,
      wsToken,
      authDataToken: options.authDataToken || "",
      jwtOpen: wsToken,
      callbackUrl: options.callbackUrl || "",
      fileUrl: options.fileUrl || "",
      fileTitle: options.fileTitle || "",
      fileFormat: options.fileFormat || "docx",
      lang: options.lang || "ru",
      permissions: options.permissions,
      timezoneOffset: options.timezoneOffset,
      sessionId: options.sessionId,
      sessionTimeConnect: options.sessionTimeConnect,
    });
    let lastServerError = "";

    console.log(`[agent] connect -> ${OO_SERVER} path=${socketPath}`);

    const socket = io(OO_SERVER, {
      path: socketPath,
      transports: ["websocket"],
      auth: {
        data: authMessage,
        token: wsToken,
      },
      reconnection: false,
      timeout: 8000,
      forceNew: true,
    });

    attachProtocolDebug(socket);

    socket.on("message", (data) => {
      if (!data || typeof data !== "object") return;
      if (String(data.type || "") === "error") {
        const msg = data.description || data.message || previewPayload(data);
        if (msg) lastServerError = String(msg);
      }
      if (String(data.type || "") === "disconnectReason") {
        const msg = data.description || data.message || previewPayload(data);
        if (msg) lastServerError = String(msg);
      }
    });

    socket.on("error", (err) => {
      const msg = err && err.message ? err.message : String(err || "");
      if (msg) lastServerError = msg;
    });

    socket.once("connect", async () => {
      console.log(`[agent] socket connected, id=${socket.id}`);

      try {
        socket.emit("message", authMessage);

        let authResponse = {};
        try {
          // Шаг 1: ждём первый ответ (может быть waitAuth)
          let authResult = await waitForAnyType(
            socket,
            ["auth", "authChanges", "waitAuth", "connectState", "disconnectReason", "error"],
            8000
          );

          // Шаг 2: если waitAuth — ждём настоящий authChanges
          if (
            authResult.event === "waitAuth" ||
            String(authResult.payload?.type || "") === "waitAuth"
          ) {
            console.log("[agent] waitAuth received, waiting for authChanges...");
            authResult = await waitForAnyType(
              socket,
              ["auth", "authChanges", "connectState", "disconnectReason", "error"],
              15000  // даём больше времени
            );
          }

          authResponse = authResult.payload || {};

          if (
            authResult.event === "disconnectReason" ||
            String(authResponse.type || "") === "disconnectReason"
          ) {
            const disconnectText =
              authResponse.description || authResponse.message || "disconnectReason";
            throw new Error(`OnlyOffice auth disconnected: ${disconnectText}`);
          }
          if (
            authResult.event === "error" ||
            String(authResponse.type || "") === "error"
          ) {
            const errorText = authResponse.description || authResponse.message || "error";
            throw new Error(`OnlyOffice auth error: ${errorText}`);
          }

          console.log(
            `[agent] auth phase ok, syncIndex=${authResponse.syncChangesIndex ?? 0}`
          );
          if (PROTOCOL_DEBUG) {
            console.log("[agent][DEBUG] auth response:", JSON.stringify(authResult.payload || {}));
          }
        } catch (authWaitError) {
          if (lastServerError) {
            throw new Error(`OnlyOffice auth rejected: ${lastServerError}`);
          }
          console.warn(
            `[agent] auth ack not received, continue with fallback: ${authWaitError.message}`
          );
        }

        resolve({
          socket,
          syncIndex: authResponse.syncChangesIndex ?? 0,
          jwt: String(authResponse.jwt || wsToken || "").trim(),
          docKey: resolvedDocKey,
          sessionId: authResponse.sessionId || null,
          sessionTimeConnect: authResponse.sessionTimeConnect || null,
          lastServerErrorRef: () => lastServerError,
        });
      } catch (err) {
        socket.disconnect();
        reject(err);
      }
    });

    socket.once("connect_error", (err) => {
      reject(new Error(`Socket.IO connect_error: ${err.message}`));
    });
  });
}

// Метаданные-шаблон (фрейм 1) — не содержит текст
const META_FRAME_B64 =
  "AgAAADEA//8BAHDTP7vCaQMApwAAAAEAAAABAAAAAQAAAAEAAAACAAAAAgAAABAAAAA5AC4AMwAuADEALgAxADAA";

function encodeInsertChar(blockId, position, char) {
  const buf = Buffer.alloc(35, 0);
  buf.writeUInt32LE(6, 0);                              // op_type = insert

  // block_id в UTF-16LE (3 символа = 6 байт)
  const bid = Buffer.from(blockId, "utf16le");
  bid.copy(buf, 4, 0, Math.min(bid.length, 6));

  buf.writeUInt16LE(1,        10);
  buf.writeUInt16LE(position, 12);                      // позиция курсора
  buf.writeUInt32LE(1,        14);
  buf.writeUInt32LE(1,        18);
  buf.writeUInt32LE(1,        22);
  buf.writeUInt32LE(char.codePointAt(0), 26);           // Unicode codepoint
  buf[30] = 0x00;
  buf[31] = 0x03;
  return buf;
}

function generateChanges(text, blockId = "904", startPos = 0) {
  const result = [];
  const meta = Buffer.from(META_FRAME_B64, "base64");
  result.push(`${meta.length};${meta.toString("base64")}`);

  for (let i = 0; i < text.length; i++) {
    const frame = encodeInsertChar(blockId, startPos + i, text[i]);
    result.push(`${frame.length};${frame.toString("base64")}`);
  }
  return result;
}

async function insertText(params) {
  const {
    docKey,
    text,
    userId,
    userName,
    wsToken,
    jwtOpen,
    authDataToken,
    callbackUrl,
    fileUrl,
    fileTitle,
    fileFormat,
    lang,
    permissions,
    timezoneOffset,
    blockId,
    startPos,
    sessionId,
    sessionTimeConnect,
  } = params || {};
  const connectOptions = {
    wsToken,
    jwtOpen,
    authDataToken,
    callbackUrl,
    fileUrl,
    fileTitle,
    fileFormat,
    lang,
    permissions,
    timezoneOffset,
    sessionId,
    sessionTimeConnect,
  };
  const {
    socket,
    syncIndex,
    jwt: sessionJwt,
    docKey: sessionDocKey,
    sessionId: sessionAuthId,
    sessionTimeConnect: sessionAuthTimeConnect,
    lastServerErrorRef,
  } = await connectToSession(docKey, userId, userName, connectOptions);

  try {
    socket.emit("message", {
      type: "isSaveLock",
      syncChangesIndex: Number.isFinite(Number(syncIndex)) ? Number(syncIndex) : 0,
      sessionId: sessionAuthId || null,
      sessionTimeConnect: sessionAuthTimeConnect || null,
    });

    let saveLockResult;
    try {
      saveLockResult = await waitForAnyType(
        socket,
        ["saveLock", "error", "disconnectReason"],
        10000
      );
    } catch (saveLockError) {
      const detail = typeof lastServerErrorRef === "function" ? lastServerErrorRef() : "";
      if (detail) {
        throw new Error(`isSaveLock failed: ${detail}`);
      }
      throw saveLockError;
    }

    if (saveLockResult && saveLockResult.event === "error") {
      throw new Error(`isSaveLock rejected: ${previewPayload(saveLockResult.payload)}`);
    }
    if (saveLockResult && saveLockResult.event === "disconnectReason") {
      throw new Error(`isSaveLock disconnected: ${previewPayload(saveLockResult.payload)}`);
    }
    if (
      saveLockResult &&
      saveLockResult.payload &&
      String(saveLockResult.payload.type || "") === "error"
    ) {
      const msg =
        saveLockResult.payload.description ||
        saveLockResult.payload.message ||
        previewPayload(saveLockResult.payload);
      throw new Error(`isSaveLock rejected: ${msg}`);
    }
    if (saveLockResult && saveLockResult.payload && saveLockResult.payload.saveLock === true) {
      throw new Error("Document is locked for save in OnlyOffice.");
    }
    console.log("[agent] isSaveLock ok");

    socket.emit("message", {
      type: "saveChanges",
      changes: JSON.stringify(
        generateChanges(
          text,
          String(blockId || "904"),
          Number.isFinite(Number(startPos)) ? Number(startPos) : 0
        )
      ),
      startSaveChanges: true,
      endSaveChanges: true,
      isCoAuthoring: false,
      isExcel: false,
      deleteIndex: null,
      excelAdditionalInfo: JSON.stringify({
        UserId: String(userId),
        UserShortId: "1",
        CursorInfo: "",
      }),
      unlock: false,
      releaseLocks: false,
      sessionId: sessionAuthId || null,
      sessionTimeConnect: sessionAuthTimeConnect || null,
    });

    const saveResult = await waitForAnyType(
      socket,
      ["unSaveLock", "error", "disconnectReason"],
      60000
    );
    if (saveResult.event === "error") {
      throw new Error(`saveChanges rejected: ${previewPayload(saveResult.payload)}`);
    }
    if (saveResult.event === "disconnectReason") {
      throw new Error(`saveChanges disconnected: ${previewPayload(saveResult.payload)}`);
    }
    const savePayload = saveResult.payload || {};
    if (String(savePayload.type || "") === "error") {
      const msg = savePayload.description || savePayload.message || previewPayload(savePayload);
      throw new Error(`saveChanges rejected: ${msg}`);
    }

    console.log(`[agent] saved ok, doc_key=${sessionDocKey}, newSyncIndex=${savePayload.syncChangesIndex}`);
    return {
      success: true,
      doc_key: sessionDocKey,
      syncChangesIndex: savePayload.syncChangesIndex,
      session_id: sessionAuthId || null,
    };
  } finally {
    socket.disconnect();
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(raw));
      } catch (_err) {
        reject(new Error("Invalid JSON"));
      }
    });
    req.on("error", reject);
  });
}

function createCommandId() {
  return `cmd_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function normalizeOperations(rawOperations) {
  if (!Array.isArray(rawOperations)) {
    return [];
  }
  return rawOperations
    .filter((item) => item && typeof item === "object" && item.op)
    .slice(0, 50)
    .map((item) => ({ ...item }));
}

function getQueue(docKey) {
  const normalized = String(docKey || "").trim();
  if (!normalized) {
    return [];
  }
  if (!commandQueues.has(normalized)) {
    commandQueues.set(normalized, []);
  }
  return commandQueues.get(normalized);
}

function queueEditorCommand(payload = {}) {
  const docKey = String(payload.docKey || "").trim();
  if (!docKey) {
    throw new Error("doc_key is required for live queue mode");
  }

  const operations = normalizeOperations(payload.operations);
  if (!operations.length) {
    throw new Error("operations are required for live queue mode");
  }

  const command = {
    id: createCommandId(),
    doc_key: docKey,
    target_type: String(payload.targetType || "").trim().toLowerCase(),
    target_id: Number(payload.targetId || 0),
    summary: String(payload.summary || "").trim(),
    operations,
    created_at: new Date().toISOString(),
    created_by: {
      user_id: String(payload.userId || ""),
      user_name: String(payload.userName || ""),
    },
  };

  getQueue(docKey).push(command);
  commandResults.set(command.id, {
    status: "queued",
    doc_key: docKey,
    target_type: command.target_type,
    target_id: command.target_id,
    created_at: command.created_at,
    updated_at: command.created_at,
    applied: 0,
    requested: operations.length,
    errors: [],
  });

  console.log(
    `[agent] queued live command id=${command.id} doc_key=${docKey} target=${command.target_type || "unknown"}:${command.target_id || 0} ops=${operations.length}`
  );

  return command;
}

function shiftNextCommand(docKey) {
  const queue = getQueue(docKey);
  if (!queue.length) {
    return null;
  }
  const nextCommand = queue.shift();
  const currentStatus = commandResults.get(nextCommand.id) || {};
  commandResults.set(nextCommand.id, {
    ...currentStatus,
    status: "delivered",
    updated_at: new Date().toISOString(),
  });
  console.log(
    `[agent] delivered live command id=${nextCommand.id} doc_key=${docKey} ops=${nextCommand.operations.length}`
  );
  return nextCommand;
}

function updateCommandResult(commandId, resultPayload = {}) {
  const normalized = String(commandId || "").trim();
  if (!normalized) {
    throw new Error("command_id is required");
  }
  const previous = commandResults.get(normalized) || {};
  const updated = {
    ...previous,
    ...resultPayload,
    status: String(resultPayload.status || previous.status || "applied").trim(),
    updated_at: new Date().toISOString(),
  };
  commandResults.set(normalized, updated);
  console.log(
    `[agent] live result id=${normalized} status=${updated.status} applied=${updated.applied || 0}/${updated.requested || 0}`
  );
  return updated;
}

function readCommandResult(commandId) {
  const normalized = String(commandId || "").trim();
  if (!normalized) {
    return null;
  }
  return commandResults.get(normalized) || null;
}

function buildPluginConfig(baseUrl, searchParams) {
  const pluginQuery = new URLSearchParams();
  pluginQuery.set("base_url", baseUrl);
  if (OO_SERVER) {
    pluginQuery.set("oo_server", OO_SERVER);
  }

  const docKey = String(searchParams.get("doc_key") || "").trim();
  const targetType = String(searchParams.get("target_type") || "").trim().toLowerCase();
  const targetId = String(searchParams.get("target_id") || "").trim();

  if (docKey) {
    pluginQuery.set("doc_key", docKey);
  }
  if (targetType) {
    pluginQuery.set("target_type", targetType);
  }
  if (targetId) {
    pluginQuery.set("target_id", targetId);
  }

  return {
    name: "SU Scholar Live Bridge",
    guid: LIVE_PLUGIN_GUID,
    baseUrl: `${baseUrl}/plugins/llm-doc-editor/`,
    variations: [
      {
        description: "Live editor bridge for SU Scholar",
        url: `index.html?${pluginQuery.toString()}`,
        icons: ["icon.svg"],
        isViewer: true,
        EditorsSupport: ["word"],
        isSystem: true,
        isVisual: false,
        initDataType: "none",
        initData: "",
        buttons: [],
      },
    ],
  };
}

function writeJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  });
  res.end(payload);
}

function writeText(res, status, body, contentType) {
  res.writeHead(status, {
    "Content-Type": contentType || "text/plain; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  });
  res.end(body);
}

async function writeStaticFile(res, filePath) {
  let contentType = "application/octet-stream";
  if (filePath.endsWith(".html")) {
    contentType = "text/html; charset=utf-8";
  } else if (filePath.endsWith(".js")) {
    contentType = "application/javascript; charset=utf-8";
  } else if (filePath.endsWith(".svg")) {
    contentType = "image/svg+xml; charset=utf-8";
  }

  const fileBuffer = await fs.promises.readFile(filePath);
  res.writeHead(200, {
    "Content-Type": contentType,
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Cache-Control": "no-store",
  });
  res.end(fileBuffer);
}

const server = http.createServer(async (req, res) => {
  const requestUrl = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type, Accept",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    });
    res.end();
    return;
  }

  if (req.method === "GET" && requestUrl.pathname === "/health") {
    return writeJson(res, 200, { status: "ok", service: "su-science-node-agent" });
  }

  if (req.method === "GET" && requestUrl.pathname === "/plugins/llm-doc-editor/config.json") {
    return writeJson(res, 200, buildPluginConfig(`${requestUrl.origin}`, requestUrl.searchParams));
  }

  if (req.method === "GET" && requestUrl.pathname === "/plugins/llm-doc-editor/next") {
    const docKey = String(requestUrl.searchParams.get("doc_key") || "").trim();
    if (!docKey) {
      return writeJson(res, 400, { error: "doc_key is required" });
    }
    const command = shiftNextCommand(docKey);
    return writeJson(res, 200, {
      success: true,
      command: command || null,
    });
  }

  if (req.method === "GET" && requestUrl.pathname === "/plugins/llm-doc-editor/result") {
    const commandId = String(requestUrl.searchParams.get("command_id") || "").trim();
    if (!commandId) {
      return writeJson(res, 400, { error: "command_id is required" });
    }
    const result = readCommandResult(commandId);
    if (!result) {
      return writeJson(res, 404, { error: "command result was not found" });
    }
    return writeJson(res, 200, { success: true, result });
  }

  if (req.method === "POST" && requestUrl.pathname === "/plugins/llm-doc-editor/result") {
    let body;
    try {
      body = await readBody(req);
    } catch (err) {
      return writeJson(res, 400, { error: err.message });
    }

    try {
      const commandId = String(body.command_id || "").trim();
      const rawResult = body.result && typeof body.result === "object" ? body.result : {};
      const updated = updateCommandResult(commandId, {
        status: String(rawResult.status || "applied").trim(),
        applied: Number(rawResult.applied || 0),
        requested: Number(rawResult.requested || 0),
        errors: Array.isArray(rawResult.errors) ? rawResult.errors : [],
        doc_key: String(body.doc_key || "").trim() || undefined,
        target_type: String(body.target_type || "").trim().toLowerCase() || undefined,
        target_id: Number(body.target_id || 0) || undefined,
      });
      return writeJson(res, 200, { success: true, result: updated });
    } catch (err) {
      console.error("[agent] plugin result update error:", err.message);
      return writeJson(res, 400, { error: err.message });
    }
  }

  if (
    req.method === "GET" &&
    ["/plugins/llm-doc-editor/index.html", "/plugins/llm-doc-editor/code.js", "/plugins/llm-doc-editor/icon.svg"].includes(requestUrl.pathname)
  ) {
    const relativePath = requestUrl.pathname.replace("/plugins/llm-doc-editor/", "");
    const absolutePath = path.join(LIVE_PLUGIN_ROOT, relativePath);
    try {
      await writeStaticFile(res, absolutePath);
      return;
    } catch (err) {
      return writeJson(res, 404, { error: "plugin asset not found" });
    }
  }

  if (req.method === "POST" && requestUrl.pathname === "/insert") {
    let body;
    try {
      body = await readBody(req);
    } catch (err) {
      return writeJson(res, 400, { error: err.message });
    }

    const liveOperations = normalizeOperations(body.operations);
    if (liveOperations.length) {
      let liveDocKey = String(body.doc_key || "").trim();
      if (!liveDocKey) {
        try {
          const resolvedLivePayload = await resolveInsertPayload(body);
          liveDocKey = String(resolvedLivePayload.docKey || "").trim();
        } catch (_err) {
          liveDocKey = "";
        }
      }

      if (!liveDocKey) {
        return writeJson(res, 400, {
          error: "doc_key is required for queued live editor operations",
        });
      }

      try {
        const queuedCommand = queueEditorCommand({
          docKey: liveDocKey,
          operations: liveOperations,
          summary: body.summary,
          userId: body.user_id,
          userName: body.user_name,
          targetType: body.target_type,
          targetId: body.target_id,
        });
        return writeJson(res, 200, {
          success: true,
          mode: "queued",
          command_id: queuedCommand.id,
          doc_key: liveDocKey,
          queued_commands: getQueue(liveDocKey).length,
        });
      } catch (err) {
        console.error("[agent] /insert queue error:", err.message);
        return writeJson(res, 400, { error: err.message });
      }
    }

    let payload;
    try {
      payload = await resolveInsertPayload(body);
    } catch (err) {
      return writeJson(res, 400, { error: err.message });
    }

    if (!payload.text) {
      return writeJson(res, 400, { error: "text is required" });
    }
    if (!payload.docKey && !payload.wsToken && !payload.jwtOpen) {
      return writeJson(res, 400, {
        error:
          "Cannot resolve document key/token. Provide one of: doc_key, ws_token(jwt), jwt_open(jwt), config_url(+auth), document_id(+auth).",
      });
    }

    if (payload.wsToken && !isLikelyJwt(payload.wsToken)) {
      return writeJson(res, 400, {
        error:
          "ws_token is not JWT. Pass config.token from /onlyoffice/config/... (not JWT secret and not random short token).",
      });
    }

    try {
      const result = await insertText(payload);
      return writeJson(res, 200, result);
    } catch (err) {
      console.error("[agent] /insert error:", err.message);
      return writeJson(res, 500, { error: err.message });
    }
  }

  if (req.method === "GET" && requestUrl.pathname === "/ping") {
    return writeJson(res, 200, {
      service: "su-science-node-agent",
      status: "ok",
      method: req.method,
      path: requestUrl.pathname,
    });
  }

  return writeJson(res, 404, { error: "not found" });
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[node-agent] listening on 0.0.0.0:${PORT}`);
  console.log(`[node-agent] OO_SERVER=${OO_SERVER}`);
  console.log(`[node-agent] APP_BASE_URL=${APP_BASE_URL}`);
  console.log(`[node-agent] SOCKET_PATH=${SOCKET_PATH}`);
  console.log(`[node-agent] JWT=${JWT_SECRET ? "configured" : "NOT SET"}`);
});

function shutdown(signal) {
  console.log(`[node-agent] ${signal} -> shutting down`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 60000).unref();
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
