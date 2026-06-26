/* ------------------------------------------------------------------ */
/*  Midterm Operator Frontend — app.js                                 */
/*  Pure vanilla JS, no framework. Uses real API endpoints only.      */
/* ------------------------------------------------------------------ */

const API = "/api/v1";

/* ---- State ---- */
let cameras = [];
let selectedCameraId = "";
let people = [];
let selectedPersonId = "";
let selectedPerson = null;
let faceRegistrationMode = "new";
let algorithms = [];
let algorithmSupportMatrix = [];
let currentZones = [];
let currentRules = [];
let runtimeOverview = null;
let runtimeControl = null;
let lastRuntimeApplyResult = null;
let selectedRuntimeConfig = null;
let roiPreviewObjectUrl = "";
let roiPreviewRequestId = 0;
const roiState = {
  points: [],
  sourceWidth: 0,
  sourceHeight: 0,
  imageLoaded: false,
  lastPointerAt: 0,
  lastPointerX: Number.NaN,
  lastPointerY: Number.NaN,
};

/* ---- DOM refs ---- */
const statusEl = document.getElementById("status");
const themeToggleBtn = document.getElementById("theme-toggle");
const themeToggleIconEl = document.getElementById("theme-toggle-icon");
const themeToggleLabelEl = document.getElementById("theme-toggle-label");
const errorBox = document.getElementById("error-box");
const successBox = document.getElementById("success-box");
const apiUrlEl = document.getElementById("api-url");
const cameraCountEl = document.getElementById("camera-count");
const enabledCameraCountEl = document.getElementById("enabled-camera-count");
const peopleCountEl = document.getElementById("people-count");
const galleryCountEl = document.getElementById("gallery-count");
const evidenceCountEl = document.getElementById("evidence-count");
const runtimeHealthSummaryEl = document.getElementById("runtime-health-summary");
const runtimeSupervisorSummaryEl = document.getElementById("runtime-supervisor-summary");
const runtimeSourceTableEl = document.getElementById("runtime-source-table");
const runtimeForwarderTableEl = document.getElementById("runtime-forwarder-table");
const runtimeEvidenceTableEl = document.getElementById("runtime-evidence-table");
const runtimeContainerTableEl = document.getElementById("runtime-container-table");
const runtimeControlStatusEl = document.getElementById("runtime-control-status");
const refreshRuntimeOverviewBtn = document.getElementById("refresh-runtime-overview");
const startSingleRuntimeBtn = document.getElementById("start-single-runtime");
const stopSingleRuntimeBtn = document.getElementById("stop-single-runtime");
const restartSingleRuntimeBtn = document.getElementById("restart-single-runtime");
const stopDualRuntimeBtn = document.getElementById("stop-dual-runtime");
const camerasEl = document.getElementById("cameras");
const zonesEl = document.getElementById("zones");
const rulesEl = document.getElementById("rules");
const cameraForm = document.getElementById("camera-form");
const fpsForm = document.getElementById("fps-form");
const alertForm = document.getElementById("alert-form");
const ruleForm = document.getElementById("rule-form");
const ruleAlgorithmEl = document.getElementById("rule-algorithm");
const ruleZoneEl = document.getElementById("rule-zone");
const ruleLineEl = document.getElementById("rule-line");
const algorithmControlsEl = document.getElementById("algorithm-controls");
const saveQuickAlgorithmsBtn = document.getElementById("save-quick-algorithms");
const zoneJson = document.getElementById("zone-json");
const ruleJson = document.getElementById("rule-json");
const fullConfigEl = document.getElementById("full-config");
const runtimeApplyResultEl = document.getElementById("runtime-apply-result");
const generatedRuntimeConfigEl = document.getElementById("generated-runtime-config");
const refreshRuntimeConfigBtn = document.getElementById("refresh-runtime-config");
const roiZoneIdEl = document.getElementById("roi-zone-id");
const roiZoneTypeEl = document.getElementById("roi-zone-type");
const roiPreviewImageEl = document.getElementById("roi-preview-image");
const roiCanvasWrapEl = document.getElementById("roi-canvas-wrap");
const roiCanvasEl = document.getElementById("roi-canvas");
const roiPreviewEmptyEl = document.getElementById("roi-preview-empty");
const roiEditorStatusEl = document.getElementById("roi-editor-status");
const peopleEl = document.getElementById("people");
const peopleSearchEl = document.getElementById("people-search");
const faceRegistrationForm = document.getElementById("face-registration-form");
const personProfileEl = document.getElementById("person-profile");
const personDetailEl = document.getElementById("person-detail");
const galleryEl = document.getElementById("gallery");
const faceRegistrationSummaryEl = document.getElementById("face-registration-summary");
const faceRegistrationResultEl = document.getElementById("face-registration-result");
const registerNewPersonBtn = document.getElementById("register-new-person");
const appendSelectedPersonBtn = document.getElementById("append-selected-person");
const registrationModeStatusEl = document.getElementById("registration-mode-status");
const previewDeleteSelectedPersonBtn = document.getElementById("preview-delete-selected-person");
const THEME_STORAGE_KEY = "operator-theme";
const ACTIVE_VIEW_STORAGE_KEY = "operator-active-view";
const EVIDENCE_COUNT_STORAGE_KEY = "operator-evidence-count";
const TOP_VIEWS = new Set(["cameras", "people", "evidence", "maintenance", "runtime"]);

/* ---- API URL display ---- */
apiUrlEl.textContent = window.location.origin + API;

/* ---- Operator-facing algorithm templates ---- */
const templates = {
  "face.watchlist": {
    rule_id: "rule_watchlist",
    algorithm_id: "face.watchlist",
    enabled: true,
    config: {
      threshold: 0.75,
      cooldown_s: 60,
      target_person_ids: [],
      target_external_person_ids: [],
      target_names: [],
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "behavior.intrusion": {
    rule_id: "rule_intrusion",
    algorithm_id: "behavior.intrusion",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_inside_ms: 1000,
      cooldown_s: 30,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
};

const quickAlgorithmIds = [
  "behavior.intrusion",
  "face.watchlist",
];

const quickAlgorithmLabels = {
  "behavior.intrusion": "入侵检测",
  "face.watchlist": "名单命中",
};

const operatorAlgorithmMeta = {
  "behavior.intrusion": {
    title: "入侵检测",
    subtitle: "区域入侵事件与证据",
    statusLabel: "运行时生效",
    statusClass: "support-production_ready",
  },
  "face.watchlist": {
    title: "名单命中",
    subtitle: "按摄像头名单",
    statusLabel: "运行时生效",
    statusClass: "support-production_ready",
  },
};

const supportStatusLabels = {
  production_ready: "生产可用",
  event_only: "仅事件",
  config_only: "仅配置",
  unsupported: "未支持",
  deferred: "已延期",
};

const applyStateLabels = {
  applied: "已应用",
  skipped: "已跳过",
  unsupported: "未支持",
  disabled: "未启用",
  pending: "尚未应用",
  not_configured: "未配置",
  missing: "未出现在本次应用",
};

/* ---- Helpers ---- */

function setStatus(text) {
  statusEl.textContent = text;
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.hidden = false;
  successBox.hidden = true;
  setStatus("错误");
}

function showSuccess(msg) {
  successBox.textContent = msg;
  successBox.hidden = false;
  errorBox.hidden = true;
  setTimeout(() => { successBox.hidden = true; }, 3000);
}

function clearMessages() {
  errorBox.hidden = true;
  successBox.hidden = true;
}

function normalizedPersonNumber(value) {
  return String(value || "").trim();
}

function prepareFaceRegistrationFormData() {
  const fd = new FormData(faceRegistrationForm);
  const selectedExternalId = normalizedPersonNumber(
    faceRegistrationForm.elements.external_person_id.dataset.selectedExternalPersonId
  );
  const submittedExternalId = normalizedPersonNumber(fd.get("external_person_id"));
  if (
    fd.get("person_id") &&
    selectedExternalId &&
    submittedExternalId &&
    selectedExternalId !== submittedExternalId
  ) {
    fd.delete("person_id");
  }
  if (!fd.get("person_id")) {
    fd.delete("person_id");
  }
  return fd;
}

function clearFaceRegistrationIdentityFields() {
  if (!faceRegistrationForm) return;
  faceRegistrationForm.elements.person_id.value = "";
  faceRegistrationForm.elements.external_person_id.value = "";
  faceRegistrationForm.elements.external_person_id.dataset.selectedExternalPersonId = "";
  faceRegistrationForm.elements.name.value = "";
  faceRegistrationForm.elements.description.value = "";
}

function setFaceRegistrationMode(mode) {
  faceRegistrationMode = mode === "append" ? "append" : "new";
  registerNewPersonBtn?.classList.toggle("active", faceRegistrationMode === "new");
  appendSelectedPersonBtn?.classList.toggle("active", faceRegistrationMode === "append");
  if (appendSelectedPersonBtn) {
    appendSelectedPersonBtn.disabled = !selectedPerson;
  }
  if (faceRegistrationMode === "append" && selectedPerson) {
    fillRegistrationForPerson(selectedPerson);
    if (registrationModeStatusEl) {
      registrationModeStatusEl.textContent =
        `追加到当前人员：${selectedPerson.name || selectedPerson.external_person_id || selectedPerson.person_id}`;
    }
    return;
  }
  clearFaceRegistrationIdentityFields();
  if (registrationModeStatusEl) {
    registrationModeStatusEl.textContent = "新人员注册：请填写人员编号和姓名。";
  }
}

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function normalizedTopView(view) {
  return TOP_VIEWS.has(view) ? view : "cameras";
}

function topViewFromHash() {
  const hash = String(window.location.hash || "").replace(/^#/, "").split("?")[0];
  return TOP_VIEWS.has(hash) ? hash : "";
}

function storedTopView() {
  try {
    return normalizedTopView(window.localStorage.getItem(ACTIVE_VIEW_STORAGE_KEY));
  } catch (_err) {
    return "cameras";
  }
}

function initialTopView() {
  return topViewFromHash() || storedTopView();
}

function persistTopView(view) {
  try {
    window.localStorage.setItem(ACTIVE_VIEW_STORAGE_KEY, normalizedTopView(view));
  } catch (_err) {}
}

function applyTheme(theme, options = {}) {
  const normalized = theme === "dark" ? "dark" : "light";
  if (normalized === "dark") {
    document.documentElement.dataset.theme = "dark";
  } else {
    delete document.documentElement.dataset.theme;
  }
  if (themeToggleBtn) {
    themeToggleBtn.setAttribute("aria-pressed", normalized === "dark" ? "true" : "false");
    themeToggleBtn.setAttribute(
      "aria-label",
      normalized === "dark" ? "切换浅色模式" : "切换黑夜模式"
    );
    themeToggleBtn.title = normalized === "dark" ? "切换浅色模式" : "切换黑夜模式";
  }
  if (themeToggleIconEl) {
    themeToggleIconEl.textContent = normalized === "dark" ? "☀" : "☾";
  }
  if (themeToggleLabelEl) {
    themeToggleLabelEl.textContent = normalized === "dark" ? "浅色" : "黑夜";
  }
  if (options.persist) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, normalized);
    } catch (_err) {}
  }
}

async function request(path, options = {}) {
  const headers = options.body instanceof FormData
    ? { ...(options.headers || {}) }
    : { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(path, {
    headers,
    ...options,
  });
  const text = await response.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`接口返回不是有效 JSON：${text.slice(0, 200)}`);
  }
  if (!response.ok || body.error) {
    const error = new Error(body.error?.message || body.error?.detail || `HTTP ${response.status}`);
    error.status = response.status;
    error.details = body.error?.details || null;
    throw error;
  }
  return body.data ?? body;
}

function apiErrorMessage(error) {
  const details = error?.details || {};
  if (details.blocked && details.active_count) {
    const count = formatInteger(details.active_count);
    const taskSummary = (details.tasks || []).slice(0, 3).map((task) => {
      const label = evidenceStateLabel(task.blocking_state || task.status || task.materialization_status);
      const source = task.source_id || task.camera_id || "--";
      const eventType = task.event_type || "--";
      return `${eventType}/${source}/${label}`;
    }).join("；");
    return `仍有 ${count} 个证据任务在生成中，已阻止重启以避免证据丢失${taskSummary ? `；${taskSummary}` : ""}`;
  }
  if (details.guard_unavailable) {
    return "无法确认是否存在生成中的证据任务，已阻止重启";
  }
  return error?.message || String(error || "未知错误");
}

function parseJsonTextarea(textarea, label) {
  try {
    return JSON.parse(textarea.value);
  } catch (e) {
    throw new Error(`${label} 不是有效 JSON：${e.message}`);
  }
}

function asInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function asFloat(value, fallback) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function ruleEvidencePolicyFromForm() {
  return {
    snapshot_required: ruleForm.elements.snapshot_required.checked,
    clip_required: ruleForm.elements.clip_required.checked,
    pre_seconds: asInt(ruleForm.elements.pre_seconds.value, 5),
    post_seconds: asInt(ruleForm.elements.post_seconds.value, 5),
  };
}

function ruleIdForAlgorithm(algorithmId) {
  return `rule_${String(algorithmId || "algorithm").replaceAll(".", "_")}`;
}

function algorithmLabel(algorithmId) {
  const definition = algorithms.find((item) => item.algorithm_id === algorithmId);
  return quickAlgorithmLabels[algorithmId] || definition?.display_name || algorithmId;
}

function algorithmDebugModeEnabled() {
  try {
    return new URLSearchParams(window.location.search).get("algorithm_debug") === "1";
  } catch (_err) {
    return false;
  }
}

function supportForAlgorithm(algorithmId) {
  return algorithmSupportMatrix.find((item) => item.algorithm_id === algorithmId) || null;
}

function supportStatusLabel(status) {
  return supportStatusLabels[status] || status || "未知";
}

function applyStateLabel(state) {
  return applyStateLabels[state] || state || "未知";
}

function isAlgorithmBlockedSupport(support) {
  return support && ["unsupported", "deferred"].includes(support.status);
}

function isAlgorithmBlocked(algorithmId) {
  return isAlgorithmBlockedSupport(supportForAlgorithm(algorithmId));
}

function latestApplyRows() {
  if (!lastRuntimeApplyResult) return [];
  return [
    ...(lastRuntimeApplyResult.applied_rules || []),
    ...(lastRuntimeApplyResult.skipped_rules || []),
    ...(lastRuntimeApplyResult.unsupported_rules || []),
  ];
}

function latestApplyStateForAlgorithm(algorithmId, rule) {
  if (!lastRuntimeApplyResult) {
    if (rule?.enabled === false) {
      return { state: "disabled", label: applyStateLabel("disabled"), reason: "" };
    }
    return {
      state: rule ? "pending" : "not_configured",
      label: applyStateLabel(rule ? "pending" : "not_configured"),
      reason: "",
    };
  }
  const ruleId = rule?.rule_id || ruleIdForAlgorithm(algorithmId);
  const row = latestApplyRows().find((item) => (
    String(item.camera_id || "") === String(selectedCameraId || "") &&
    (String(item.rule_id || "") === String(ruleId) || item.algorithm_id === algorithmId)
  ));
  if (!row) {
    return {
      state: rule?.enabled === false ? "disabled" : "missing",
      label: applyStateLabel(rule?.enabled === false ? "disabled" : "missing"),
      reason: "",
    };
  }
  const state = row.runtime_apply_state || "missing";
  return {
    state,
    label: applyStateLabel(state),
    reason: row.runtime_skip_reason || row.support_status_reason || "",
  };
}

function ruleForAlgorithm(algorithmId) {
  return currentRules.find((rule) => (
    rule.algorithm_id === algorithmId ||
    rule.algorithm_type === algorithmId ||
    rule.rule_id === ruleIdForAlgorithm(algorithmId)
  ));
}

function upsertCurrentRule(rule) {
  if (!rule || !rule.rule_id) return;
  const index = currentRules.findIndex((item) => item.rule_id === rule.rule_id);
  if (index >= 0) {
    currentRules[index] = rule;
  } else {
    currentRules.push(rule);
  }
  renderRules(currentRules);
  renderQuickAlgorithmControls();
}

function algorithmNeedsZone(algorithmId) {
  return algorithmId.startsWith("behavior.") && algorithmId !== "behavior.wall_climb_suspicious";
}

function algorithmNeedsLine(algorithmId) {
  return algorithmId === "behavior.wall_climb_suspicious";
}

function evidencePolicyFor(rule, algorithmId) {
  return rule?.evidence_policy || templates[algorithmId]?.evidence_policy || {
    snapshot_required: true,
    clip_required: true,
    pre_seconds: 5,
    post_seconds: 5,
  };
}

function listValue(value) {
  if (Array.isArray(value)) return value;
  if (value == null || value === "") return [];
  return [value];
}

function normalizedTargetPersonIds(config = {}) {
  return new Set(
    listValue(config.target_person_ids || config.person_ids)
      .map((value) => String(value))
      .filter(Boolean)
  );
}

function normalizedTargetExternalIds(config = {}) {
  return new Set(
    listValue(config.target_external_person_ids || config.external_person_ids)
      .map((value) => String(value).trim())
      .filter(Boolean)
  );
}

function normalizedTargetNames(config = {}) {
  return new Set(
    listValue(config.target_names || config.names)
      .map((value) => String(value).trim())
      .filter(Boolean)
  );
}

function isWatchlistPersonSelected(person, config = {}) {
  const personIds = normalizedTargetPersonIds(config);
  const externalIds = normalizedTargetExternalIds(config);
  const targetNames = normalizedTargetNames(config);
  return personIds.has(String(person.person_id)) ||
    (person.external_person_id && externalIds.has(String(person.external_person_id))) ||
    (person.name && targetNames.has(String(person.name)));
}

function renderWatchlistTargets(config = {}, disabledAttr = "") {
  const selectedCount = people.filter((person) => isWatchlistPersonSelected(person, config)).length;
  if (!people.length) {
    return `<div class="algorithm-watchlist-targets">` +
      `<div class="algorithm-watchlist-target-header"><span>目标人员</span><strong>0</strong></div>` +
      `<div class="muted">暂无已注册人员。</div>` +
    `</div>`;
  }
  const rows = people.map((person) => {
    const checked = isWatchlistPersonSelected(person, config) ? " checked" : "";
    const disabled = disabledAttr ? " disabled" : "";
    const externalId = person.external_person_id || "";
    return `<label class="watchlist-target-item">` +
      `<input data-control="target_person" type="checkbox"` +
        ` data-person-id="${escapeHtml(person.person_id)}"` +
        ` data-external-person-id="${escapeHtml(externalId)}"` +
        ` data-person-name="${escapeHtml(person.name || "")}"${checked}${disabled} />` +
      `<span><strong>${escapeHtml(person.name || "未命名人员")}</strong>` +
      `<small>${escapeHtml(externalId || `ID ${person.person_id}`)}</small></span>` +
    `</label>`;
  }).join("");
  return `<div class="algorithm-watchlist-targets">` +
    `<div class="algorithm-watchlist-target-header"><span>目标人员</span><strong>已选 ${selectedCount}</strong></div>` +
    `<div class="watchlist-target-list">${rows}</div>` +
  `</div>`;
}

function defaultRuleForAlgorithm(algorithmId) {
  const template = templates[algorithmId];
  if (template) {
    return JSON.parse(JSON.stringify(template));
  }
  const definition = algorithms.find((item) => item.algorithm_id === algorithmId) || {};
  return {
    rule_id: ruleIdForAlgorithm(algorithmId),
    algorithm_id: algorithmId,
    enabled: true,
    severity: "medium",
    config: definition.default_config || {},
    evidence_policy: definition.evidence_policy || {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  };
}

function fillRuleForm(rule) {
  if (!ruleForm) return;
  const body = {
    enabled: true,
    severity: "medium",
    config: {},
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
    ...rule,
  };
  const config = body.config || {};
  const policy = body.evidence_policy || {};
  ruleForm.elements.algorithm_id.value = body.algorithm_id || "";
  ruleForm.elements.rule_id.value = body.rule_id || ruleIdForAlgorithm(body.algorithm_id);
  ruleForm.elements.zone_id.value = body.zone_id || config.zone_id || config.zone || "";
  ruleForm.elements.line_id.value = body.line_id || config.line_id || "";
  ruleForm.elements.severity.value = body.severity || config.severity || "medium";
  ruleForm.elements.enabled.checked = body.enabled !== false;
  ruleForm.elements.snapshot_required.checked = policy.snapshot_required !== false;
  ruleForm.elements.clip_required.checked = policy.clip_required !== false;
  ruleForm.elements.pre_seconds.value = policy.pre_seconds ?? 5;
  ruleForm.elements.post_seconds.value = policy.post_seconds ?? 5;
  ruleJson.value = JSON.stringify(config, null, 2);
}

function makeCameraId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return "00000000-0000-4000-8000-" + Date.now().toString().padStart(12, "0").slice(-12);
}

function selectedCamera() {
  return cameras.find((camera) => String(camera.id) === String(selectedCameraId)) || null;
}

function roiPointLimit() {
  return roiZoneTypeEl?.value === "line" ? 2 : 10;
}

function roiPointMinimum() {
  return roiZoneTypeEl?.value === "line" ? 2 : 3;
}

function roiTypeLabel() {
  return roiZoneTypeEl?.value === "line" ? "检测线" : "多边形";
}

function defaultRoiZoneId(type = roiZoneTypeEl?.value) {
  return type === "line" ? "tripwire_01" : "perimeter";
}

function nextRoiZoneId(type = roiZoneTypeEl?.value) {
  const prefix = type === "line" ? "tripwire" : "roi";
  const existing = new Set(
    (currentZones || [])
      .map((zone) => String(zone.zone_id || zone.zone_name || ""))
      .filter(Boolean)
  );
  for (let index = 1; index <= 99; index += 1) {
    const candidate = `${prefix}_${String(index).padStart(2, "0")}`;
    if (!existing.has(candidate)) return candidate;
  }
  return `${prefix}_${Date.now().toString().slice(-6)}`;
}

function setRoiStatus(text) {
  if (!roiEditorStatusEl) return;
  roiEditorStatusEl.textContent = text;
}

function roiSourceDimensions() {
  const width = roiState.sourceWidth || roiPreviewImageEl?.naturalWidth || 1920;
  const height = roiState.sourceHeight || roiPreviewImageEl?.naturalHeight || 1080;
  return { width: Math.max(Number(width) || 1, 1), height: Math.max(Number(height) || 1, 1) };
}

function roiDisplayDimensions() {
  const rect = roiCanvasWrapEl?.getBoundingClientRect();
  return {
    width: Math.max(rect?.width || roiPreviewImageEl?.clientWidth || 1, 1),
    height: Math.max(rect?.height || roiPreviewImageEl?.clientHeight || 1, 1),
  };
}

function roiImageDisplayRect() {
  const display = roiDisplayDimensions();
  const source = roiSourceDimensions();
  const sourceAspect = source.width / source.height;
  const displayAspect = display.width / display.height;
  if (displayAspect > sourceAspect) {
    const height = display.height;
    const width = height * sourceAspect;
    return { left: (display.width - width) / 2, top: 0, width, height };
  }
  const width = display.width;
  const height = width / sourceAspect;
  return { left: 0, top: (display.height - height) / 2, width, height };
}

function normalizeRoiPoint(point) {
  if (!Array.isArray(point) || point.length !== 2) return null;
  const x = Number(point[0]);
  const y = Number(point[1]);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return [x, y];
}

function sourcePointToDisplay(point, coordinateSpace = "pixel") {
  const imageRect = roiImageDisplayRect();
  const source = roiSourceDimensions();
  const xy = normalizeRoiPoint(point);
  if (!xy) return null;
  const x = coordinateSpace === "normalized"
    ? imageRect.left + xy[0] * imageRect.width
    : imageRect.left + (xy[0] / source.width) * imageRect.width;
  const y = coordinateSpace === "normalized"
    ? imageRect.top + xy[1] * imageRect.height
    : imageRect.top + (xy[1] / source.height) * imageRect.height;
  return [x, y];
}

function displayPointToSource(x, y) {
  const imageRect = roiImageDisplayRect();
  const source = roiSourceDimensions();
  const relativeX = Math.max(0, Math.min(imageRect.width, x - imageRect.left));
  const relativeY = Math.max(0, Math.min(imageRect.height, y - imageRect.top));
  return [
    Math.round(Math.max(0, Math.min(source.width, (relativeX / imageRect.width) * source.width))),
    Math.round(Math.max(0, Math.min(source.height, (relativeY / imageRect.height) * source.height))),
  ];
}

function syncRoiCanvasSize() {
  if (!roiCanvasEl || !roiCanvasWrapEl) return null;
  const rect = roiCanvasWrapEl.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (roiCanvasEl.width !== width) roiCanvasEl.width = width;
  if (roiCanvasEl.height !== height) roiCanvasEl.height = height;
  const ctx = roiCanvasEl.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

function drawZonePath(ctx, points, { closed, stroke, fill, width = 2, dash = [] }) {
  if (!ctx || !Array.isArray(points) || points.length < 2) return;
  ctx.save();
  ctx.beginPath();
  ctx.setLineDash(dash);
  ctx.lineWidth = width;
  ctx.strokeStyle = stroke;
  ctx.fillStyle = fill;
  ctx.moveTo(points[0][0], points[0][1]);
  for (const point of points.slice(1)) {
    ctx.lineTo(point[0], point[1]);
  }
  if (closed) {
    ctx.closePath();
    if (fill) ctx.fill();
  }
  ctx.stroke();
  ctx.restore();
}

function drawRoiCanvas() {
  const ctx = syncRoiCanvasSize();
  if (!ctx || !roiCanvasWrapEl) return;
  const display = roiDisplayDimensions();
  ctx.clearRect(0, 0, display.width, display.height);
  if (!roiState.imageLoaded) return;

  for (const zone of currentZones || []) {
    const points = (zone.points || [])
      .map((point) => sourcePointToDisplay(point, zone.coordinate_space || "pixel"))
      .filter(Boolean);
    if (points.length < 2) continue;
    const closed = zone.zone_type === "polygon";
    drawZonePath(ctx, points, {
      closed,
      stroke: "rgba(45, 212, 191, 0.7)",
      fill: closed ? "rgba(45, 212, 191, 0.12)" : "",
      width: 2,
      dash: [8, 6],
    });
  }

  const draftPoints = roiState.points
    .map((point) => sourcePointToDisplay(point, "pixel"))
    .filter(Boolean);
  if (draftPoints.length >= 2) {
    drawZonePath(ctx, draftPoints, {
      closed: roiZoneTypeEl?.value === "polygon" && draftPoints.length >= 3,
      stroke: "rgba(248, 113, 113, 0.96)",
      fill: roiZoneTypeEl?.value === "polygon" && draftPoints.length >= 3
        ? "rgba(248, 113, 113, 0.16)"
        : "",
      width: 3,
    });
  }
  ctx.save();
  ctx.fillStyle = "#ffffff";
  ctx.strokeStyle = "rgba(194, 65, 12, 0.96)";
  ctx.lineWidth = 2;
  draftPoints.forEach((point, index) => {
    ctx.beginPath();
    ctx.arc(point[0], point[1], 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "rgba(24, 34, 48, 0.92)";
    ctx.font = "11px sans-serif";
    ctx.fillText(String(index + 1), point[0] + 8, point[1] - 8);
    ctx.fillStyle = "#ffffff";
  });
  ctx.restore();
}

function roiZoneFromDraft() {
  const zoneType = roiZoneTypeEl?.value === "line" ? "line" : "polygon";
  const zoneId = String(roiZoneIdEl?.value || defaultRoiZoneId(zoneType)).trim();
  if (!zoneId) {
    throw new Error("区域 ID 不能为空");
  }
  if (roiState.points.length < (zoneType === "line" ? 2 : 3)) {
    throw new Error(`${zoneType === "line" ? "检测线" : "多边形"} 点位不足`);
  }
  if (zoneType === "polygon" && roiState.points.length > 10) {
    throw new Error("多边形最多 10 个点");
  }
  return {
    zone_id: zoneId,
    zone_name: zoneId,
    zone_type: zoneType,
    coordinate_space: "pixel",
    points: roiState.points.map((point) => [Math.round(point[0]), Math.round(point[1])]),
    enabled: true,
  };
}

function writeRoiZoneJson({ allowIncomplete = false } = {}) {
  if (!zoneJson) return null;
  try {
    const zone = roiZoneFromDraft();
    zoneJson.value = JSON.stringify(zone, null, 2);
    setRoiStatus(`${roiTypeLabel()} ${zone.zone_id}：${zone.points.length} 个点`);
    return zone;
  } catch (e) {
    if (!allowIncomplete) throw e;
    const zoneType = roiZoneTypeEl?.value === "line" ? "line" : "polygon";
    const zoneId = String(roiZoneIdEl?.value || defaultRoiZoneId(zoneType)).trim();
    zoneJson.value = JSON.stringify({
      zone_id: zoneId,
      zone_name: zoneId,
      zone_type: zoneType,
      coordinate_space: "pixel",
      points: roiState.points,
      enabled: true,
    }, null, 2);
    const min = roiPointMinimum();
    setRoiStatus(`${roiTypeLabel()}：${roiState.points.length}/${min} 最少点位`);
    return null;
  }
}

function startRoiDraft(type = "polygon", points = []) {
  if (roiZoneTypeEl) roiZoneTypeEl.value = type === "line" ? "line" : "polygon";
  if (roiZoneIdEl && !roiZoneIdEl.value) roiZoneIdEl.value = defaultRoiZoneId(type);
  roiState.points = points.map(normalizeRoiPoint).filter(Boolean);
  writeRoiZoneJson({ allowIncomplete: true });
  drawRoiCanvas();
}

function startNewRoiDraft(type = "polygon") {
  const normalized = type === "line" ? "line" : "polygon";
  if (roiZoneIdEl) roiZoneIdEl.value = nextRoiZoneId(normalized);
  startRoiDraft(normalized, []);
  setRoiStatus(`${roiTypeLabel()}：点击画面添加点`);
}

function rulePolygonZoneId(rule) {
  const config = rule?.config || {};
  return String(rule?.zone_id || config.zone_id || config.zone || "").trim();
}

function preferredFinalRoiZone(zones = [], rules = []) {
  const polygonZones = (zones || []).filter((zone) => zone.zone_type === "polygon");
  if (!polygonZones.length) return null;
  const byId = new Map(
    polygonZones.map((zone) => [String(zone.zone_id || zone.zone_name || ""), zone])
  );
  const enabledRules = (rules || []).filter((rule) => rule.enabled !== false);
  const intrusionRule = enabledRules.find((rule) => rule.algorithm_id === "behavior.intrusion");
  const intrusionZone = byId.get(rulePolygonZoneId(intrusionRule));
  if (intrusionZone) return intrusionZone;
  for (const rule of enabledRules) {
    const zone = byId.get(rulePolygonZoneId(rule));
    if (zone) return zone;
  }
  return polygonZones[0];
}

function loadZoneIntoRoiEditor(zone) {
  if (!zone) return;
  if (roiZoneIdEl) roiZoneIdEl.value = zone.zone_id || zone.zone_name || defaultRoiZoneId(zone.zone_type);
  const type = zone.zone_type === "line" || zone.zone_type === "direction_line" ? "line" : "polygon";
  const source = roiSourceDimensions();
  const points = (zone.points || []).map((point) => {
    const xy = normalizeRoiPoint(point);
    if (!xy) return null;
    if ((zone.coordinate_space || "pixel") === "normalized") {
      return [xy[0] * source.width, xy[1] * source.height];
    }
    return xy;
  }).filter(Boolean);
  startRoiDraft(type, points);
}

function prepareRoiEditorForCamera(zones = [], rules = currentRules) {
  roiPreviewEmptyEl?.classList.remove("hidden");
  roiState.imageLoaded = false;
  const finalZone = preferredFinalRoiZone(zones, rules);
  if (finalZone) {
    loadZoneIntoRoiEditor(finalZone);
    setRoiStatus(`最终检测区域：${finalZone.zone_id || finalZone.zone_name}`);
    return;
  }
  startNewRoiDraft("polygon");
  setRoiStatus(selectedCameraId ? "画面加载中，点击画面添加最终检测区域" : "未选择摄像头");
}

async function refreshRoiPreview({ silent = false } = {}) {
  if (!selectedCameraId) {
    setRoiStatus("未选择摄像头");
    return;
  }
  const camera = selectedCamera();
  if (!camera?.rtsp_url && !cameraForm?.elements.rtsp_url.value) {
    setRoiStatus("摄像头地址为空");
    return;
  }
  const requestId = ++roiPreviewRequestId;
  if (!silent) clearMessages();
  setRoiStatus("画面加载中");
  const response = await fetch(
    `${API}/cameras/${encodeURIComponent(selectedCameraId)}/preview.jpg?max_width=1280&_=${Date.now()}`,
    { headers: { Accept: "image/jpeg" } }
  );
  if (requestId !== roiPreviewRequestId) return;
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      message = body.error?.message || body.detail || message;
    } catch (_err) {}
    roiState.imageLoaded = false;
    roiPreviewEmptyEl?.classList.remove("hidden");
    drawRoiCanvas();
    throw new Error(message);
  }
  roiState.sourceWidth = Number(response.headers.get("X-Camera-Source-Width")) || 0;
  roiState.sourceHeight = Number(response.headers.get("X-Camera-Source-Height")) || 0;
  const blob = await response.blob();
  if (roiPreviewObjectUrl) URL.revokeObjectURL(roiPreviewObjectUrl);
  roiPreviewObjectUrl = URL.createObjectURL(blob);
  roiPreviewImageEl.src = roiPreviewObjectUrl;
}

async function saveRoiZone() {
  if (!selectedCameraId) {
    showError("未选择摄像头");
    return;
  }
  writeRoiZoneJson();
  await saveZone({ bindRules: true });
}

function ensureRoiImageReadyForInput() {
  if (roiState.imageLoaded) return true;
  if (roiPreviewImageEl?.complete && roiPreviewImageEl.naturalWidth > 0) {
    roiState.imageLoaded = true;
    if (!roiState.sourceWidth) roiState.sourceWidth = roiPreviewImageEl.naturalWidth || 0;
    if (!roiState.sourceHeight) roiState.sourceHeight = roiPreviewImageEl.naturalHeight || 0;
    roiPreviewEmptyEl?.classList.add("hidden");
    drawRoiCanvas();
    return true;
  }
  return false;
}

function roiInputPoint(event) {
  const rect = roiCanvasWrapEl?.getBoundingClientRect();
  return {
    x: Number(event.clientX || 0) - Number(rect?.left || 0),
    y: Number(event.clientY || 0) - Number(rect?.top || 0),
  };
}

function shouldIgnoreRoiInput(event) {
  const now = Date.now();
  const point = roiInputPoint(event);
  const dx = Number.isFinite(roiState.lastPointerX) ? point.x - roiState.lastPointerX : Infinity;
  const dy = Number.isFinite(roiState.lastPointerY) ? point.y - roiState.lastPointerY : Infinity;
  const sameSpot = Math.hypot(dx, dy) <= 10;
  if (sameSpot && now - roiState.lastPointerAt < 600) {
    return true;
  }
  roiState.lastPointerX = point.x;
  roiState.lastPointerY = point.y;
  roiState.lastPointerAt = now;
  return false;
}

function addRoiPointFromEvent(event) {
  if (shouldIgnoreRoiInput(event)) return;
  if (!ensureRoiImageReadyForInput()) {
    setRoiStatus("请先刷新画面");
    return;
  }
  const limit = roiPointLimit();
  if (roiState.points.length >= limit) {
    setRoiStatus(`${roiTypeLabel()}最多 ${limit} 个点`);
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const point = roiInputPoint(event);
  roiState.points.push(displayPointToSource(point.x, point.y));
  writeRoiZoneJson({ allowIncomplete: true });
  drawRoiCanvas();
}

function addRoiCenterPoint() {
  if (!ensureRoiImageReadyForInput()) {
    setRoiStatus("请先刷新画面");
    return;
  }
  const limit = roiPointLimit();
  if (roiState.points.length >= limit) {
    setRoiStatus(`${roiTypeLabel()}最多 ${limit} 个点`);
    return;
  }
  const source = roiSourceDimensions();
  roiState.points.push([Math.round(source.width / 2), Math.round(source.height / 2)]);
  writeRoiZoneJson({ allowIncomplete: true });
  drawRoiCanvas();
}

function updateSummary() {
  if (cameraCountEl) {
    cameraCountEl.textContent = String(cameras.length);
  }
  if (enabledCameraCountEl) {
    enabledCameraCountEl.textContent = String(cameras.filter((camera) => camera.enabled !== false).length);
  }
  if (peopleCountEl) {
    peopleCountEl.textContent = people.length ? String(people.length) : "--";
  }
  if (galleryCountEl) {
    const total = people.reduce((sum, person) => sum + Number(person.active_gallery_count || 0), 0);
    galleryCountEl.textContent = people.length ? String(total) : "--";
  }
  if (evidenceCountEl && evidenceCountEl.textContent === "") {
    evidenceCountEl.textContent = "--";
  }
}

function setEvidenceCount(value) {
  if (!evidenceCountEl) return;
  const count = Number(value);
  if (Number.isFinite(count)) {
    evidenceCountEl.textContent = String(count);
    try {
      window.localStorage.setItem(EVIDENCE_COUNT_STORAGE_KEY, String(count));
    } catch (_err) {}
    return;
  }
  if (!evidenceCountEl.textContent || evidenceCountEl.textContent === "--") {
    evidenceCountEl.textContent = storedEvidenceCount() || "--";
  }
}

function storedEvidenceCount() {
  try {
    const raw = window.localStorage.getItem(EVIDENCE_COUNT_STORAGE_KEY);
    if (!raw) return "";
    const count = Number(raw);
    return Number.isFinite(count) ? String(count) : "";
  } catch (_err) {
    return "";
  }
}

function restoreEvidenceCount() {
  if (!evidenceCountEl) return;
  const count = storedEvidenceCount();
  if (count) {
    evidenceCountEl.textContent = count;
  }
}

function initials(name) {
  const text = String(name || "人员").trim();
  return text.slice(0, 2).toUpperCase();
}

function personPreviewUrl(person) {
  return person.primary_registered_crop_url || person.primary_source_image_url || "";
}

/* ---- Camera form serialization ---- */

function formToCamera() {
  const fd = new FormData(cameraForm);
  const fpsFd = new FormData(fpsForm);
  const alertFd = new FormData(alertForm);
  return {
    id: fd.get("id"),
    name: fd.get("name"),
    source_id: fd.get("source_id"),
    rtsp_url: fd.get("rtsp_url"),
    site_id: fd.get("site_id") || null,
    location: fd.get("location") || null,
    gpu_id: Number(fd.get("gpu_id") || 0),
    enabled: fd.get("enabled") === "on",
    input_type: fd.get("input_type") || "rtsp",
    rtsp_transport: fd.get("rtsp_transport") || "tcp",
    fps_policy: {
      max_fps: fpsFd.get("max_fps") || "8/1",
      min_fps: fpsFd.get("min_fps") || "2/1",
    },
    alert_policy: {
      global_alert_cooldown_s: Number(alertFd.get("global_alert_cooldown_s") || 0),
      store_suppressed_events: alertFd.get("store_suppressed_events") === "on",
      suppress_record_request: alertFd.get("suppress_record_request") === "on",
      critical_bypass: alertFd.get("critical_bypass") === "on",
    },
  };
}

function fillCamera(camera) {
  cameraForm.elements.id.value = camera.id || "";
  cameraForm.elements.name.value = camera.name || "";
  cameraForm.elements.source_id.value = camera.source_id || "";
  cameraForm.elements.rtsp_url.value = camera.rtsp_url || "";
  cameraForm.elements.site_id.value = camera.site_id || "";
  cameraForm.elements.location.value = camera.location || "";
  cameraForm.elements.gpu_id.value = camera.gpu_id ?? 0;
  cameraForm.elements.input_type.value = camera.input_type || "rtsp";
  cameraForm.elements.rtsp_transport.value = camera.rtsp_transport || "tcp";
  cameraForm.elements.enabled.checked = camera.enabled !== false;

  const fps = camera.fps_policy || {};
  fpsForm.elements.max_fps.value = fps.max_fps || "8/1";
  fpsForm.elements.min_fps.value = fps.min_fps || "2/1";

  const alert = camera.alert_policy || {};
  alertForm.elements.global_alert_cooldown_s.value = alert.global_alert_cooldown_s ?? 30;
  alertForm.elements.store_suppressed_events.checked = alert.store_suppressed_events !== false;
  alertForm.elements.suppress_record_request.checked = alert.suppress_record_request !== false;
  alertForm.elements.critical_bypass.checked = alert.critical_bypass === true;
}

/* ---- Rendering ---- */

function renderCameras() {
  camerasEl.innerHTML = "";
  for (const camera of cameras) {
    const item = document.createElement("div");
    item.className = `camera-item ${camera.id === selectedCameraId ? "active" : ""}`;
    const cooldown = camera.alert_policy?.global_alert_cooldown_s ?? "-";
    const location = [camera.site_id, camera.location].filter(Boolean).join(" / ") || "未填写位置";
    item.innerHTML =
      `<strong>${camera.name || "未命名摄像头"}</strong>` +
      `<div class="muted">${location}</div>` +
      `<div class="person-metrics">` +
        `<span class="metric-chip">${camera.enabled ? "已启用" : "已停用"}</span>` +
        `<span class="metric-chip">冷却 ${cooldown}s</span>` +
      `</div>`;
    item.addEventListener("click", () => selectCamera(camera.id));
    camerasEl.appendChild(item);
  }
  updateSummary();
}

function zoneTypeLabel(zone) {
  return ["line", "direction_line"].includes(zone?.zone_type) ? "检测线" : "区域";
}

function zonePointBounds(points = []) {
  const normalized = points.map(normalizeRoiPoint).filter(Boolean);
  if (!normalized.length) return "";
  const xs = normalized.map((point) => point[0]);
  const ys = normalized.map((point) => point[1]);
  return `X ${Math.min(...xs)}-${Math.max(...xs)} / Y ${Math.min(...ys)}-${Math.max(...ys)}`;
}

function zoneRuleBindings(zone) {
  const zoneId = String(zone?.zone_id || zone?.zone_name || "");
  if (!zoneId) return [];
  const isLine = ["line", "direction_line"].includes(zone?.zone_type);
  return (currentRules || [])
    .filter((rule) => {
      const config = rule?.config || {};
      const ref = isLine
        ? String(rule?.line_id || config.line_id || "")
        : String(rule?.zone_id || config.zone_id || config.zone || "");
      return ref === zoneId;
    })
    .map((rule) => {
      const label = algorithmLabel(rule.algorithm_id || rule.rule_type || rule.rule_id);
      return `${label}${rule.enabled === false ? "（停用）" : ""}`;
    });
}

function createZoneListItem(zone, finalZoneId) {
  const item = document.createElement("div");
  const zoneId = zone.zone_id || zone.zone_name || "";
  const typeLabel = zoneTypeLabel(zone);
  const points = zone.points || [];
  const pointUnit = typeLabel === "检测线" ? "端点" : "点";
  const bounds = zonePointBounds(points);
  const bindings = zoneRuleBindings(zone);
  const isFinalZone = typeLabel === "区域" && zoneId && zoneId === finalZoneId;
  item.className = `zone-item zone-item-${typeLabel === "检测线" ? "line" : "polygon"}`;
  item.innerHTML =
    `<div class="zone-item-header">` +
      `<div class="zone-item-main">` +
        `<strong>${escapeHtml(zoneId)}</strong>` +
        `<div class="muted">${escapeHtml(zone.zone_name && zone.zone_name !== zoneId ? zone.zone_name : typeLabel)}</div>` +
      `</div>` +
      `<div class="zone-badges">` +
        `<span class="zone-chip">${escapeHtml(typeLabel)}</span>` +
        `<span class="zone-chip ${zone.enabled === false ? "disabled" : "enabled"}">${zone.enabled === false ? "停用" : "启用"}</span>` +
        `${isFinalZone ? `<span class="zone-chip final">最终检测区域</span>` : ""}` +
      `</div>` +
    `</div>` +
    `<div class="zone-meta-grid">` +
      `<span>${points.length} 个${pointUnit}</span>` +
      `<span>${escapeHtml(zone.coordinate_space || "pixel")}</span>` +
      `<span>${escapeHtml(bounds || "未记录坐标")}</span>` +
    `</div>` +
    `<div class="zone-binding-list">` +
      (bindings.length
        ? bindings.map((label) => `<span>${escapeHtml(label)}</span>`).join("")
        : `<span class="muted">未绑定算法</span>`) +
    `</div>` +
    `<div class="item-actions">` +
      `<button class="sm" data-action="edit-zone" data-zone-id="${escapeHtml(zoneId)}">编辑</button>` +
      `<button class="sm danger" data-action="delete-zone" data-zone-id="${escapeHtml(zoneId)}">删除</button>` +
    `</div>`;
  item.querySelector('[data-action="edit-zone"]').addEventListener("click", (e) => {
    e.stopPropagation();
    zoneJson.value = JSON.stringify({
      zone_id: zone.zone_id,
      zone_name: zone.zone_name || "",
      zone_type: zone.zone_type,
      coordinate_space: zone.coordinate_space || "pixel",
      points: zone.points || [],
      enabled: zone.enabled !== false,
    }, null, 2);
    loadZoneIntoRoiEditor(zone);
  });
  item.querySelector('[data-action="delete-zone"]').addEventListener("click", (e) => {
    e.stopPropagation();
    deleteZone(zone.zone_id);
  });
  return item;
}

function appendZoneGroup({ title, rows, emptyText, finalZoneId }) {
  const group = document.createElement("section");
  group.className = "zone-list-group";
  const header = document.createElement("div");
  header.className = "zone-list-title";
  header.innerHTML = `<h3>${escapeHtml(title)}</h3><span>${rows.length}</span>`;
  group.appendChild(header);
  const list = document.createElement("div");
  list.className = "zone-list-items";
  if (rows.length) {
    rows.forEach((zone) => list.appendChild(createZoneListItem(zone, finalZoneId)));
  } else {
    list.innerHTML = `<div class="zone-list-empty">${escapeHtml(emptyText)}</div>`;
  }
  group.appendChild(list);
  zonesEl.appendChild(group);
}

function renderZones(zones) {
  currentZones = zones || [];
  zonesEl.innerHTML = "";
  const finalZone = preferredFinalRoiZone(currentZones, currentRules);
  const finalZoneId = finalZone ? String(finalZone.zone_id || finalZone.zone_name || "") : "";
  const polygonZones = currentZones.filter((zone) => zone.zone_type === "polygon");
  const lineZones = currentZones.filter((zone) => ["line", "direction_line"].includes(zone.zone_type));
  appendZoneGroup({
    title: "已保存区域",
    rows: polygonZones,
    emptyText: "暂无已保存区域",
    finalZoneId,
  });
  appendZoneGroup({
    title: "已保存检测线",
    rows: lineZones,
    emptyText: "暂无已保存检测线",
    finalZoneId,
  });
  renderZoneSelectors();
}

function renderRules(rules) {
  currentRules = rules || [];
  rulesEl.innerHTML = "";
  for (const rule of currentRules) {
    const item = document.createElement("div");
    item.className = "rule-item";
    const cat = rule.rule_category || (rule.is_alert_rule ? "alert" : "observation");
    const badgeClass = cat === "observation" ? "observation" : "alert";
    const categoryText = cat === "observation" ? "观察" : cat === "alert" ? "告警" : "配置";
    const support = supportForAlgorithm(rule.algorithm_id);
    const supportStatus = support?.status || "config_only";
    const policy = rule.evidence_policy || {};
    const zoneText = rule.zone_id || rule.config?.zone_id || rule.config?.zone || "";
    const lineText = rule.line_id || rule.config?.line_id || "";
    item.innerHTML =
      `<strong>${rule.rule_id}</strong>` +
      `<div>${rule.algorithm_id} ` +
        `<span class="badge ${badgeClass}">${categoryText}</span> ` +
        `<span class="support-badge support-${escapeHtml(supportStatus)}">${escapeHtml(supportStatusLabel(supportStatus))}</span>` +
      `</div>` +
      `<div class="muted">${rule.enabled ? "已启用" : "已停用"} · 录像 ${policy.pre_seconds ?? 5}s/${policy.post_seconds ?? 5}s</div>` +
      `<div class="muted">${zoneText ? `区域 ${zoneText}` : ""}${lineText ? ` 检测线 ${lineText}` : ""}</div>` +
      `<div class="item-actions">` +
        `<button class="sm" data-action="edit-rule" data-rule-id="${rule.rule_id}">编辑</button>` +
        `<button class="sm" data-action="toggle-rule" data-rule-id="${rule.rule_id}" data-enabled="${rule.enabled}">${rule.enabled ? "停用" : "启用"}</button>` +
        `<button class="sm danger" data-action="delete-rule" data-rule-id="${rule.rule_id}">删除</button>` +
      `</div>`;
    item.querySelector('[data-action="edit-rule"]').addEventListener("click", (e) => {
      e.stopPropagation();
      fillRuleForm(rule);
    });
    item.querySelector('[data-action="toggle-rule"]').addEventListener("click", (e) => {
      e.stopPropagation();
      setRuleEnabled(rule.rule_id, !rule.enabled);
    });
    item.querySelector('[data-action="delete-rule"]').addEventListener("click", (e) => {
      e.stopPropagation();
      deleteRule(rule.rule_id);
    });
    rulesEl.appendChild(item);
  }
  if (zonesEl) renderZones(currentZones);
  renderQuickAlgorithmControls();
}

function renderAlgorithms() {
  if (!ruleAlgorithmEl) return;
  const previous = ruleAlgorithmEl.value;
  const knownById = new Map();
  for (const algorithmId of quickAlgorithmIds) {
    knownById.set(algorithmId, {
      algorithm_id: algorithmId,
      display_name: algorithmLabel(algorithmId),
    });
  }
  if (algorithmDebugModeEnabled()) {
    for (const definition of algorithms) {
      knownById.set(definition.algorithm_id, definition);
    }
  }
  const known = Array.from(knownById.values());
  ruleAlgorithmEl.innerHTML = "";
  for (const definition of known) {
    const option = document.createElement("option");
    option.value = definition.algorithm_id;
    option.textContent = definition.display_name
      ? `${definition.display_name} (${definition.algorithm_id})`
      : definition.algorithm_id;
    ruleAlgorithmEl.appendChild(option);
  }
  if (previous && known.some((definition) => definition.algorithm_id === previous)) {
    ruleAlgorithmEl.value = previous;
  }
  renderAlgorithmTemplateButtons();
  renderQuickAlgorithmControls();
}

function renderAlgorithmTemplateButtons() {
  document.querySelectorAll("[data-template]").forEach((button) => {
    const algorithmId = button.dataset.template;
    const support = supportForAlgorithm(algorithmId);
    const blocked = isAlgorithmBlockedSupport(support) && !algorithmDebugModeEnabled();
    button.disabled = blocked;
    button.title = blocked
      ? `${supportStatusLabel(support.status)}: ${support.status_reason || ""}`
      : "";
  });
}

function renderZoneSelectors() {
  if (!ruleZoneEl || !ruleLineEl) return;
  const previousZone = ruleZoneEl.value;
  const previousLine = ruleLineEl.value;
  const polygonZones = currentZones.filter((z) => z.zone_type === "polygon");
  const lineZones = currentZones.filter((z) => ["line", "direction_line"].includes(z.zone_type));
  const renderOptions = (select, rows, emptyLabel) => {
    select.innerHTML = `<option value="">${emptyLabel}</option>`;
    for (const zone of rows) {
      const id = zone.zone_id || zone.zone_name;
      const option = document.createElement("option");
      option.value = id;
      option.textContent = zone.zone_name && zone.zone_name !== id ? `${zone.zone_name} (${id})` : id;
      select.appendChild(option);
    }
  };
  renderOptions(ruleZoneEl, polygonZones, "不绑定区域");
  renderOptions(ruleLineEl, lineZones, "不绑定检测线");
  if (previousZone && polygonZones.some((zone) => (zone.zone_id || zone.zone_name) === previousZone)) {
    ruleZoneEl.value = previousZone;
  }
  if (previousLine && lineZones.some((zone) => (zone.zone_id || zone.zone_name) === previousLine)) {
    ruleLineEl.value = previousLine;
  }
  renderQuickAlgorithmControls();
}

function zoneOptionId(zone) {
  return zone.zone_id || zone.zone_name || "";
}

function operatorApplyBadge(algorithmId, rule, applyState) {
  if (algorithmId === "face.watchlist") {
    if (rule?.enabled === false) {
      return { label: "未启用", className: "apply-disabled" };
    }
    return rule
      ? { label: "已配置", className: "apply-applied" }
      : { label: "未配置", className: "apply-not_configured" };
  }
  return {
    label: applyState.label,
    className: `apply-${applyState.state}`,
  };
}

function renderQuickAlgorithmControls() {
  if (!algorithmControlsEl) return;
  if (!selectedCameraId) {
    algorithmControlsEl.innerHTML = `<div class="muted">请选择摄像头。</div>`;
    return;
  }
  const polygonZones = currentZones.filter((z) => z.zone_type === "polygon");
  algorithmControlsEl.innerHTML = "";
  for (const algorithmId of quickAlgorithmIds) {
    const meta = operatorAlgorithmMeta[algorithmId] || {
      title: algorithmLabel(algorithmId),
      subtitle: algorithmId,
      statusLabel: supportStatusLabel(supportForAlgorithm(algorithmId)?.status),
      statusClass: `support-${supportForAlgorithm(algorithmId)?.status || "config_only"}`,
    };
    const rule = ruleForAlgorithm(algorithmId);
    const policy = evidencePolicyFor(rule, algorithmId);
    const support = supportForAlgorithm(algorithmId);
    const supportStatus = support?.status || "config_only";
    const blocked = isAlgorithmBlockedSupport(support) && !algorithmDebugModeEnabled();
    const applyState = latestApplyStateForAlgorithm(algorithmId, rule);
    const applyBadge = operatorApplyBadge(algorithmId, rule, applyState);
    const disabledAttr = blocked ? " disabled" : "";
    const checked = rule
      ? rule.enabled !== false
      : (templates[algorithmId]?.enabled !== false && !blocked);
    const config = { ...(templates[algorithmId]?.config || {}), ...(rule?.config || {}) };
    const selectedZone = rule?.zone_id || config.zone_id || config.zone || zoneOptionId(polygonZones[0]) || "";
    const row = document.createElement("div");
    row.className = `algorithm-control-item${blocked ? " blocked" : ""}`;
    row.dataset.algorithmId = algorithmId;
    const zoneControl = algorithmNeedsZone(algorithmId)
      ? `<label class="algorithm-field">区域<select data-control="zone_id"${disabledAttr}>` +
          `<option value="">未绑定</option>` +
          polygonZones.map((zone) => {
            const id = zoneOptionId(zone);
            const selected = id === selectedZone ? " selected" : "";
            return `<option value="${escapeHtml(id)}"${selected}>${escapeHtml(zone.zone_name && zone.zone_name !== id ? `${zone.zone_name} (${id})` : id)}</option>`;
          }).join("") +
        `</select></label>`
      : "";
    const intrusionControls = algorithmId === "behavior.intrusion"
      ? `<label class="algorithm-field">停留毫秒<input data-control="min_inside_ms" type="number" min="1" value="${Number(config.min_inside_ms ?? 1000)}"${disabledAttr} /></label>` +
        `<label class="algorithm-field">冷却秒数<input data-control="cooldown_s" type="number" min="0" value="${Number(config.cooldown_s ?? 30)}"${disabledAttr} /></label>`
      : "";
    const watchlistControls = algorithmId === "face.watchlist"
      ? `<label class="algorithm-field">匹配阈值<input data-control="threshold" type="number" min="0" max="1" step="0.01" value="${Number(config.threshold ?? 0.75)}"${disabledAttr} /></label>` +
        `<label class="algorithm-field">冷却秒数<input data-control="cooldown_s" type="number" min="0" value="${Number(config.cooldown_s ?? 60)}"${disabledAttr} /></label>`
      : "";
    const watchlistTargetsHtml = algorithmId === "face.watchlist"
      ? renderWatchlistTargets(config, disabledAttr)
      : "";
    row.innerHTML =
      `<div class="algorithm-control-header">` +
        `<div class="algorithm-control-main">` +
          `<label class="inline algorithm-toggle">` +
            `<input data-control="enabled" type="checkbox" ${checked ? "checked" : ""}${disabledAttr} />` +
            `<span>${escapeHtml(meta.title)}</span>` +
          `</label>` +
          `<span class="muted">${escapeHtml(meta.subtitle)}</span>` +
        `</div>` +
        `<div class="algorithm-status-stack">` +
          `<span class="support-badge ${escapeHtml(meta.statusClass)}">${escapeHtml(meta.statusLabel)}</span>` +
          `<span class="apply-badge ${escapeHtml(applyBadge.className)}">${escapeHtml(applyBadge.label)}</span>` +
          `<button class="sm primary" data-action="save-quick-rule" type="button"${disabledAttr}>保存并应用</button>` +
          `<button class="sm" data-action="edit-quick-rule" type="button"${disabledAttr}>高级</button>` +
        `</div>` +
      `</div>` +
      `<div class="algorithm-control-fields">` +
        zoneControl +
        intrusionControls +
        watchlistControls +
        `<label class="algorithm-field">前录秒数<input data-control="pre_seconds" type="number" min="0" max="300" value="${Number(policy.pre_seconds ?? 5)}"${disabledAttr} /></label>` +
        `<label class="algorithm-field">后录秒数<input data-control="post_seconds" type="number" min="0" max="300" value="${Number(policy.post_seconds ?? 5)}"${disabledAttr} /></label>` +
      `</div>` +
      watchlistTargetsHtml;
    row.querySelector('[data-action="edit-quick-rule"]').addEventListener("click", () => {
      if (blocked) {
        showError(`${algorithmLabel(algorithmId)} 当前为 ${supportStatusLabel(supportStatus)}，不能保存或应用`);
        return;
      }
      fillRuleForm(rule || defaultRuleForAlgorithm(algorithmId));
      switchCameraTab("rules");
      ruleForm?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
    row.querySelector('[data-action="save-quick-rule"]').addEventListener("click", () => {
        saveQuickAlgorithmCard(row).catch((e) => showError(apiErrorMessage(e)));
    });
    algorithmControlsEl.appendChild(row);
  }
}

function statusText(ok) {
  return ok ? "正常" : "异常";
}

function formatNumber(value, digits = 1) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return num.toFixed(digits).replace(/\.0+$/, "");
}

function formatInteger(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return String(Math.trunc(num));
}

function formatAge(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  if (num < 60) return `${formatInteger(num)}s`;
  return `${formatNumber(num / 60)}m`;
}

function evidenceStateLabel(state) {
  const labels = {
    pending: "待处理",
    waiting_proof: "等待帧证明",
    queued: "排队",
    replaying: "Replay 中",
    finalizing: "生成中",
    ready: "完成",
    failed: "失败",
    not_implemented: "未实现",
  };
  return labels[state] || state || "--";
}

function containerGroupCounts(group = []) {
  const items = Array.isArray(group) ? group : [];
  return {
    total: items.length,
    running: items.filter((item) => item.running || item.state === "running").length,
    missing: items.filter((item) => !item.present || item.state === "missing").length,
    restarting: items.filter((item) => item.restarting || item.state === "restarting").length,
  };
}

function renderRuntimeControlStatus() {
  if (!runtimeControlStatusEl) return;
  const control = runtimeControl || {};
  const single = containerGroupCounts(control.single);
  const dual = containerGroupCounts(control.dual);
  const management = containerGroupCounts(control.management);
  const dualText = dual.running > 0 ? `${dual.running} 个仍在运行` : "已关闭";
  runtimeControlStatusEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>单路主链路</span><strong>${single.running}/${single.total || "--"} 运行</strong></div>` +
      `<div><span>双路扩展</span><strong>${escapeHtml(dualText)}</strong></div>` +
      `<div><span>管理面</span><strong>${management.running}/${management.total || "--"} 运行</strong></div>` +
      `<div><span>重启中</span><strong>${formatInteger(single.restarting + dual.restarting)}</strong></div>` +
    `</div>`;
}

function renderRuntimeOverview() {
  if (!runtimeHealthSummaryEl || !runtimeSourceTableEl || !runtimeContainerTableEl) return;
  const overview = runtimeOverview || {};
  const metrics = overview.metrics || {};
  const forwarder = overview.forwarder || {};
  const health = overview.health || {};
  const containers = overview.containers || {};
  const supervisor = overview.supervisor || {};
  const issues = Array.isArray(health.issues) ? health.issues : [];
  const supervisorEnabled = supervisor.enabled === true;
  const annotationAge = supervisor.annotation_age_s;

  runtimeHealthSummaryEl.innerHTML =
    `<div class="summary-card runtime-health-card ${health.ok ? "ok" : "warn"}">` +
      `<span>整体状态</span>` +
      `<strong>${statusText(health.ok)}</strong>` +
      `<small>${issues.length ? escapeHtml(issues.join(", ")) : "无已知异常"}</small>` +
    `</div>` +
    `<div class="summary-card">` +
      `<span>Savant metrics</span>` +
      `<strong>${metrics.available ? "可用" : "不可用"}</strong>` +
      `<small>${escapeHtml(overview.metrics_url || "")}</small>` +
    `</div>` +
    `<div class="summary-card">` +
      `<span>活跃 source</span>` +
      `<strong>${formatInteger(metrics.sources_active ?? health.source_count)}</strong>` +
      `<small>per-source 指标 ${formatInteger((metrics.sources || []).length)} 路</small>` +
    `</div>` +
    `<div class="summary-card">` +
      `<span>annotation age</span>` +
      `<strong>${annotationAge == null ? "--" : `${formatInteger(annotationAge)}s`}</strong>` +
      `<small>${supervisorEnabled ? "supervisor 已启用" : "supervisor 未启用"}</small>` +
    `</div>`;

  runtimeSupervisorSummaryEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>Savant 容器</span><strong>${escapeHtml(supervisor.savant_container || "--")}</strong></div>` +
      `<div><span>模块状态</span><strong>${escapeHtml(supervisor.savant_module_status || "--")}</strong></div>` +
      `<div><span>冷却中</span><strong>${supervisor.in_cooldown ? "是" : "否"}</strong></div>` +
      `<div><span>source convergence</span><strong>${supervisor.source_convergence?.healthy === false ? "异常" : "正常"}</strong></div>` +
    `</div>`;

  renderRuntimeSourceTable(metrics.sources || []);
  renderRuntimeForwarderTable(forwarder);
  renderRuntimeEvidenceTable(overview.evidence || {});
  renderRuntimeContainerTable(containers);
  renderRuntimeControlStatus();
}

function renderRuntimeSourceTable(sources) {
  if (!runtimeSourceTableEl) return;
  if (!sources.length) {
    runtimeSourceTableEl.innerHTML = `<div class="empty-state">暂无 per-source 性能指标。</div>`;
    return;
  }
  const rows = sources.map((source) => {
    const age = Number(source.last_frame_age_seconds);
    const stale = Number.isFinite(age) && age > 30;
    return `<tr class="${stale ? "warn-row" : ""}">` +
      `<td>${escapeHtml(source.source_id)}</td>` +
      `<td>${formatNumber(source.effective_fps)}</td>` +
      `<td>${formatNumber(source.last_frame_age_seconds)}</td>` +
      `<td>${formatInteger(source.frames_seen_total)}</td>` +
      `<td>${formatInteger(source.frame_annotations_exported_total)}</td>` +
      `<td>${formatInteger(source.pose_objects_total)}</td>` +
      `<td>${formatInteger(source.face_objects_total)}</td>` +
      `<td>${formatInteger(source.adaface_embeddings_total)}</td>` +
    `</tr>`;
  }).join("");
  runtimeSourceTableEl.innerHTML =
    `<table class="runtime-table">` +
      `<thead><tr>` +
        `<th>source</th><th>FPS</th><th>frame age(s)</th><th>frames</th>` +
        `<th>annotations</th><th>person</th><th>face</th><th>AdaFace</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
}

function renderRuntimeForwarderTable(forwarder) {
  if (!runtimeForwarderTableEl) return;
  const sources = forwarder.sources || [];
  if (!forwarder.available) {
    runtimeForwarderTableEl.innerHTML = `<div class="empty-state">暂无 analysis-forwarder 指标。</div>`;
    return;
  }
  const global = forwarder.global || {};
  const rows = sources.map((source) => {
    const seen = Number(source.frames_seen_total);
    const dropped = Number(source.frames_dropped_total);
    const dropRatio = Number.isFinite(seen) && seen > 0 && Number.isFinite(dropped)
      ? `${formatNumber((dropped / seen) * 100)}%`
      : "--";
    const failed = Number(source.savant_send_failures_total);
    const warn = Number.isFinite(failed) && failed > 0;
    return `<tr class="${warn ? "warn-row" : ""}">` +
      `<td>${escapeHtml(source.source_id)}</td>` +
      `<td>${formatInteger(source.frames_seen_total)}</td>` +
      `<td>${formatInteger(source.frames_forwarded_total)}</td>` +
      `<td>${formatInteger(source.frames_dropped_total)}</td>` +
      `<td>${dropRatio}</td>` +
      `<td>${formatInteger(source.savant_send_failures_total)}</td>` +
    `</tr>`;
  }).join("");
  runtimeForwarderTableEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>queue depth</span><strong>${formatInteger(global.queue_depth)}</strong></div>` +
      `<div><span>running</span><strong>${global.running === 1 ? "是" : "否"}</strong></div>` +
    `</div>` +
    `<table class="runtime-table">` +
      `<thead><tr>` +
        `<th>source</th><th>seen</th><th>forwarded</th><th>dropped</th><th>drop %</th><th>send failures</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
}

function renderRuntimeEvidenceTable(evidence) {
  if (!runtimeEvidenceTableEl) return;
  if (!evidence.available) {
    runtimeEvidenceTableEl.innerHTML =
      `<div class="empty-state">暂无证据状态指标。${escapeHtml(evidence.error || "")}</div>`;
    return;
  }
  const counts = evidence.state_counts || [];
  const recent = evidence.recent || [];
  const failures = evidence.recent_failures || [];
  const countHtml = counts.length
    ? counts.map((row) =>
        `<div><span>${escapeHtml(evidenceStateLabel(row.state))}</span><strong>${formatInteger(row.count)}</strong></div>`
      ).join("")
    : `<div><span>最近 3 小时</span><strong>0</strong></div>`;
  const rows = recent.map((row) => {
    const warn = row.evidence_state === "failed" || row.task_status && row.task_status !== row.evidence_state;
    return `<tr class="${warn ? "warn-row" : ""}">` +
      `<td>${escapeHtml(row.event_type || "--")}</td>` +
      `<td>${escapeHtml(row.source_id || "--")}</td>` +
      `<td>${escapeHtml(evidenceStateLabel(row.evidence_state))}</td>` +
      `<td>${escapeHtml(row.task_status || "--")}</td>` +
      `<td>${formatAge(row.age_seconds)}</td>` +
      `<td>${escapeHtml(row.evidence_reason || "--")}</td>` +
    `</tr>`;
  }).join("");
  const failureRows = failures.slice(0, 5).map((row) =>
    `<li><strong>${escapeHtml(row.source_id || "--")}</strong> ` +
    `${escapeHtml(row.event_type || "--")} / ${escapeHtml(row.evidence_reason || "failed")}</li>`
  ).join("");
  runtimeEvidenceTableEl.innerHTML =
    `<div class="runtime-kv-grid evidence-state-grid">${countHtml}</div>` +
    (failureRows ? `<ul class="runtime-failure-list">${failureRows}</ul>` : "") +
    `<table class="runtime-table">` +
      `<thead><tr>` +
        `<th>event</th><th>source</th><th>evidence</th><th>task</th><th>age</th><th>reason</th>` +
      `</tr></thead>` +
      `<tbody>${rows || `<tr><td colspan="6">暂无最近证据事件。</td></tr>`}</tbody>` +
    `</table>`;
}

function renderRuntimeContainerTable(containers) {
  if (!runtimeContainerTableEl) return;
  const fixed = containers.fixed || {};
  const rows = Object.entries(fixed).map(([role, item]) => {
    const warn = (item.present && item.state !== "running") || item.restart_warning === true;
    return `<tr class="${warn ? "warn-row" : ""}">` +
      `<td>${escapeHtml(role)}</td>` +
      `<td>${escapeHtml(item.name || "--")}</td>` +
      `<td>${item.present ? escapeHtml(item.state || "--") : "missing"}</td>` +
      `<td>${escapeHtml(item.health || "--")}</td>` +
      `<td>${formatInteger(item.restart_count)}</td>` +
      `<td>${formatNumber(item.restart_rate_per_min)}</td>` +
    `</tr>`;
  });
  for (const source of containers.dynamic_sources || []) {
    const warn = source.state !== "running" || source.restart_warning === true;
    rows.push(
      `<tr class="${warn ? "warn-row" : ""}">` +
        `<td>dynamic_source</td>` +
        `<td>${escapeHtml(source.name || "--")}</td>` +
        `<td>${escapeHtml(source.state || "--")}</td>` +
        `<td>--</td>` +
        `<td>${formatInteger(source.restart_count)}</td>` +
        `<td>${formatNumber(source.restart_rate_per_min)}</td>` +
      `</tr>`
    );
  }
  runtimeContainerTableEl.innerHTML =
    `<table class="runtime-table">` +
      `<thead><tr><th>role</th><th>container</th><th>state</th><th>health</th><th>restarts</th><th>restarts/min</th></tr></thead>` +
      `<tbody>${rows.join("")}</tbody>` +
    `</table>`;
}

function renderRuntimeApplyResult() {
  if (!runtimeApplyResultEl) return;
  if (!lastRuntimeApplyResult) {
    runtimeApplyResultEl.innerHTML = `<div class="muted">尚未应用运行时。</div>`;
    return;
  }
  const selectedRows = latestApplyRows().filter((row) => (
    !selectedCameraId || String(row.camera_id || "") === String(selectedCameraId)
  ));
  const applied = selectedRows.filter((row) => row.runtime_apply_state === "applied");
  const skipped = selectedRows.filter((row) => row.runtime_apply_state === "skipped");
  const unsupported = selectedRows.filter((row) => row.runtime_apply_state === "unsupported");
  const epoch = lastRuntimeApplyResult.runtime_epoch_id ||
    lastRuntimeApplyResult.runtime_epoch?.runtime_epoch_id || "--";
  const sourceIds = (lastRuntimeApplyResult.source_ids || []).filter(Boolean);
  const warningRows = [...unsupported, ...skipped].slice(0, 8);
  const warningHtml = warningRows.length
    ? `<ul class="runtime-warning-list">` + warningRows.map((row) =>
        `<li><strong>${escapeHtml(row.rule_id || row.algorithm_id || "--")}</strong> ` +
        `${escapeHtml(row.runtime_skip_reason || row.support_status || "skipped")} ` +
        `<span>${escapeHtml(row.support_status_reason || "")}</span></li>`
      ).join("") + `</ul>`
    : `<div class="muted">当前选中摄像头没有运行时应用警告。</div>`;
  runtimeApplyResultEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>runtime epoch</span><strong>${escapeHtml(epoch)}</strong></div>` +
      `<div><span>摄像头</span><strong>${formatInteger((lastRuntimeApplyResult.camera_ids || []).length)}</strong></div>` +
      `<div><span>source</span><strong>${formatInteger(sourceIds.length)}</strong></div>` +
      `<div><span>已应用规则</span><strong>${formatInteger(applied.length)}</strong></div>` +
      `<div><span>已跳过</span><strong>${formatInteger(skipped.length)}</strong></div>` +
      `<div><span>未支持</span><strong>${formatInteger(unsupported.length)}</strong></div>` +
    `</div>` +
    warningHtml;
}

function renderSelectedRuntimeConfig(data = selectedRuntimeConfig) {
  if (!generatedRuntimeConfigEl) return;
  if (!data) {
    generatedRuntimeConfigEl.value = "";
    return;
  }
  const preview = {
    camera_id: data.camera_id,
    generated_at: data.generated_at,
    paths: data.paths,
    cameras_midterm_yml: data.cameras_midterm_yml,
    algorithm_runtime_config: data.algorithm_runtime_config,
    export_summary: data.export_summary,
  };
  generatedRuntimeConfigEl.value = JSON.stringify(preview, null, 2);
}

function renderSelectedRuntimeConfigError(message) {
  if (!generatedRuntimeConfigEl) return;
  generatedRuntimeConfigEl.value = `生成配置加载失败：${message}`;
}

function switchCameraTab(tab) {
  const normalized = tab === "rules" ? "rules" : "zones";
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === normalized);
  });
  document.getElementById("tab-zones").hidden = normalized !== "zones";
  document.getElementById("tab-rules").hidden = normalized !== "rules";
}

/* ---- Tab switching ---- */

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    switchCameraTab(btn.dataset.tab);
  });
});

document.querySelectorAll(".top-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    activateTopView(btn.dataset.view, true);
  });
});

function activateTopView(view, updateHash = false) {
  const normalized = normalizedTopView(view);
  persistTopView(normalized);
  document.querySelectorAll(".top-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === normalized);
  });
  document.getElementById("camera-view").hidden = normalized !== "cameras";
  document.getElementById("people-view").hidden = normalized !== "people";
  document.getElementById("runtime-view").hidden = normalized !== "runtime";
  document.getElementById("evidence-view").hidden = normalized !== "evidence";
  document.getElementById("maintenance-view").hidden = normalized !== "maintenance";
  if (updateHash) {
    window.history.replaceState(null, "", `#${normalized}`);
  }
  if (normalized === "people") {
    loadPeople().catch((e) => showError(e.message));
  }
  if (normalized === "evidence" && window.operatorEvidence) {
    window.operatorEvidence.init().catch((e) => showError(e.message));
  } else if (window.operatorEvidence?.pause) {
    window.operatorEvidence.pause();
  }
  if (normalized === "runtime") {
    loadRuntimeOverview().catch((e) => showError(e.message));
  }
  if (normalized === "maintenance" && window.operatorMaintenance) {
    window.operatorMaintenance.init().catch((e) => showError(e.message));
  }
}

function openMaintenanceWithRequest(request = {}) {
  if (window.operatorMaintenance?.openDeleteDialog) {
    window.operatorMaintenance.openDeleteDialog(request).catch((e) => showError(e.message));
  }
}

/* ---- API operations ---- */

async function loadCameras() {
  clearMessages();
  if (!algorithms.length) {
    await loadAlgorithms();
  }
  await loadWatchlistTargetPeople();
  const data = await request(`${API}/cameras`);
  cameras = Array.isArray(data) ? data : (data.cameras || []);
  if (!selectedCameraId && cameras[0]) selectedCameraId = cameras[0].id;
  renderCameras();
  if (selectedCameraId) await selectCamera(selectedCameraId);
  setStatus("就绪");
}

async function loadEvidenceCount() {
  try {
    const data = await request(`${API}/maintenance/storage/summary`);
    setEvidenceCount(data.evidence?.bundle_count);
  } catch (_err) {
    setEvidenceCount(null);
  }
}

async function loadAlgorithms() {
  const [data, supportData] = await Promise.all([
    request(`${API}/algorithms`),
    request(`${API}/algorithms/support-matrix`).catch(() => ({ algorithms: [] })),
  ]);
  algorithms = data.algorithms || [];
  algorithmSupportMatrix = supportData.algorithms || [];
  renderAlgorithms();
  if (ruleAlgorithmEl && !ruleAlgorithmEl.value && algorithms[0]) {
    fillRuleForm(defaultRuleForAlgorithm(algorithms[0].algorithm_id));
  }
}

async function loadWatchlistTargetPeople() {
  try {
    const data = await request(`${API}/people?limit=200`);
    people = data.people || [];
    updateSummary();
  } catch (_err) {
    people = people || [];
  }
}

async function loadPeople() {
  clearMessages();
  const params = new URLSearchParams();
  const q = peopleSearchEl.value.trim();
  if (q) params.set("q", q);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const data = await request(`${API}/people${suffix}`);
  people = data.people || [];
  const selectedStillVisible = people.some((person) => String(person.person_id) === String(selectedPersonId));
  if (!selectedStillVisible) {
    selectedPersonId = "";
    selectedPerson = null;
  }
  if (!selectedPersonId && people[0]) {
    const previewPerson = people.find((person) => personPreviewUrl(person));
    selectedPersonId = String((previewPerson || people[0]).person_id);
  }
  renderPeople();
  if (selectedPersonId) {
    await selectPerson(selectedPersonId);
  }
  if (previewDeleteSelectedPersonBtn) {
    previewDeleteSelectedPersonBtn.disabled = !selectedPersonId;
  }
  updateSummary();
  renderQuickAlgorithmControls();
  setStatus("人员就绪");
}

async function loadRuntimeOverview() {
  const [overviewData, controlData] = await Promise.all([
    request(`${API}/runtime/overview`),
    request(`${API}/runtime/control`),
  ]);
  runtimeOverview = overviewData || {};
  runtimeControl = controlData || {};
  renderRuntimeOverview();
  setStatus("运行状态已刷新");
}

function renderPeople() {
  peopleEl.innerHTML = "";
  for (const person of people) {
    const item = document.createElement("div");
    item.className = `person-item ${String(person.person_id) === String(selectedPersonId) ? "active" : ""}`;
    const previewUrl = personPreviewUrl(person);
    const avatar = previewUrl
      ? `<img class="person-avatar person-avatar-img" src="${previewUrl}" alt="${person.name} 人脸图" loading="lazy" />`
      : `<div class="person-avatar">${initials(person.name)}</div>`;
    item.innerHTML =
      avatar +
      `<div class="person-copy">` +
        `<strong>${person.name}</strong>` +
        `<div class="muted">${person.external_person_id || "未设置人员编号"}</div>` +
        `<div class="person-metrics">` +
          `<span class="metric-chip">ID ${escapeHtml(person.person_id)}</span>` +
          `<span class="metric-chip">照片 ${person.active_gallery_count || 0}</span>` +
          `<span class="metric-chip">${person.is_active ? "有效" : "停用"}</span>` +
        `</div>` +
      `</div>`;
    item.addEventListener("click", () => selectPerson(person.person_id));
    peopleEl.appendChild(item);
  }
  updateSummary();
}

async function selectPerson(personId) {
  clearMessages();
  selectedPersonId = String(personId);
  const data = await request(`${API}/people/${encodeURIComponent(personId)}`);
  selectedPerson = data.person || null;
  personDetailEl.value = JSON.stringify(data.person, null, 2);
  renderPersonProfile(data.person);
  renderGallery(data.gallery || []);
  if (appendSelectedPersonBtn) {
    appendSelectedPersonBtn.disabled = false;
  }
  if (faceRegistrationMode === "append") {
    fillRegistrationForPerson(data.person);
  } else if (registrationModeStatusEl) {
    registrationModeStatusEl.textContent =
      `已选择人员：${data.person?.name || data.person?.external_person_id || personId}。点击“追加到当前人员”后再上传新照片。`;
  }
  renderPeople();
  if (previewDeleteSelectedPersonBtn) {
    previewDeleteSelectedPersonBtn.disabled = false;
  }
  setStatus(`人员 ${personId}`);
}

function renderPersonProfile(person) {
  if (!personProfileEl || !person) return;
  personProfileEl.innerHTML =
    `<strong>${person.name || "未命名人员"}</strong>` +
    `<div class="muted">系统 ID：${person.person_id || selectedPersonId || "-"}</div>` +
    `<div class="muted">人员编号：${person.external_person_id || "未设置"}</div>` +
    `<div class="muted">状态：${person.is_active ? "有效" : "停用"}</div>` +
    `<div class="muted">${person.description || "暂无描述"}</div>`;
}

function selectedPersonDeleteRequest() {
  const person = selectedPerson || people.find((item) => String(item.person_id) === String(selectedPersonId)) || {};
  const personId = String(person.person_id || selectedPersonId || "");
  const name = person.name || "未命名人员";
  const externalId = person.external_person_id || "未设置";
  return {
    kind: "person",
    person_ids: [personId],
    default_reason: `operator_delete_person:${personId}`,
    target: {
      title: `人员：${name}`,
      fields: [
        { label: "系统 ID", value: personId },
        { label: "人员编号", value: externalId },
        { label: "图库照片", value: String(person.active_gallery_count ?? "-") }
      ]
    }
  };
}

function galleryDeleteRequest(row = {}) {
  const person = selectedPerson || people.find((item) => String(item.person_id) === String(selectedPersonId)) || {};
  const personId = String(person.person_id || selectedPersonId || "");
  const galleryId = String(row.gallery_embedding_id || "");
  return {
    kind: "gallery",
    person_ids: personId ? [personId] : [],
    gallery_embedding_ids: galleryId ? [galleryId] : [],
    default_reason: `operator_delete_gallery:${galleryId}`,
    target: {
      title: `人脸照片：${person.name || "未命名人员"}`,
      fields: [
        { label: "图库 ID", value: galleryId },
        { label: "系统 ID", value: personId || "-" },
        { label: "人员编号", value: person.external_person_id || "未设置" }
      ]
    }
  };
}

function fillRegistrationForPerson(person) {
  if (!person || !faceRegistrationForm) return;
  faceRegistrationForm.elements.person_id.value = person.person_id || "";
  faceRegistrationForm.elements.external_person_id.value = person.external_person_id || "";
  faceRegistrationForm.elements.external_person_id.dataset.selectedExternalPersonId = person.external_person_id || "";
  faceRegistrationForm.elements.name.value = person.name || "";
  faceRegistrationForm.elements.description.value = person.description || "";
}

function renderGallery(gallery) {
  galleryEl.innerHTML = "";
  for (const row of gallery) {
    const item = document.createElement("div");
    item.className = "gallery-item";
    const previewUrl = row.registered_crop_url || row.source_image_url;
    if (previewUrl) {
      const image = document.createElement("img");
      image.className = "gallery-thumb";
      image.src = previewUrl;
      image.alt = `图库图片 ${row.gallery_embedding_id}`;
      image.loading = "lazy";
      item.appendChild(image);
    }
    const meta = document.createElement("div");
    meta.className = "gallery-meta";
    meta.innerHTML =
      `<strong>人脸照片</strong>` +
      `<div class="gallery-meta-row">图库 ID ${row.gallery_embedding_id}</div>` +
      `<div class="gallery-meta-row">登记质量 ${row.quality ?? "-"}</div>` +
      `<div class="gallery-meta-row">` +
        `<span class="badge ${row.is_primary ? "success" : "neutral"}">${row.is_primary ? "主图" : "备选图"}</span> ` +
        `<span class="badge ${row.is_active ? "success" : "neutral"}">${row.is_active ? "有效" : "停用"}</span> ` +
      `</div>`;
    const actions = document.createElement("div");
    actions.className = "gallery-actions";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "sm danger";
    deleteButton.textContent = row.is_active ? "删除照片" : "已删除";
    deleteButton.disabled = !row.gallery_embedding_id || !row.is_active;
    deleteButton.addEventListener("click", () => openMaintenanceWithRequest(galleryDeleteRequest(row)));
    actions.appendChild(deleteButton);
    meta.appendChild(actions);
    item.appendChild(meta);
    galleryEl.appendChild(item);
  }
  if (!gallery.length) {
    galleryEl.innerHTML = `<div class="gallery-item"><div class="muted">当前人员暂无图库图片。</div></div>`;
  }
}

async function submitFaceRegistration() {
  clearMessages();
  const submit = document.getElementById("submit-face-registration");
  submit.disabled = true;
  try {
    const fd = prepareFaceRegistrationFormData();
    const result = await request(`${API}/people/register-face`, {
      method: "POST",
      body: fd,
    });
    faceRegistrationResultEl.value = JSON.stringify(result, null, 2);
    if (faceRegistrationSummaryEl) {
      faceRegistrationSummaryEl.innerHTML =
        `<strong>人脸已注册</strong>` +
        `<div class="muted">人员编号：${result.external_person_id || "-"}</div>` +
        `<div class="muted">姓名：${result.name || "-"}</div>`;
    }
    selectedPersonId = String(result.person_id || "");
    showSuccess("人脸已注册");
    await loadPeople();
    if (result.person_id) {
      await selectPerson(result.person_id);
    }
  } finally {
    submit.disabled = false;
  }
}

async function selectCamera(cameraId, { clear = true } = {}) {
  if (clear) clearMessages();
  selectedCameraId = cameraId;
  const data = await request(`${API}/cameras/${encodeURIComponent(cameraId)}/config`);
  fillCamera(data.camera);
  renderCameras();
  currentRules = data.rules || [];
  renderZones(data.zones || []);
  await loadAlgorithmRules(cameraId, data.rules || []);
  prepareRoiEditorForCamera(data.zones || [], currentRules);
  fullConfigEl.value = JSON.stringify(data, null, 2);
  await loadSelectedRuntimeConfig(cameraId);
  renderRuntimeApplyResult();
  renderQuickAlgorithmControls();
  refreshRoiPreview({ silent: true }).catch((e) => setRoiStatus(`画面不可用：${e.message}`));
  setStatus(data.camera?.name || "摄像头就绪");
}

async function loadAlgorithmRules(cameraId, fallbackRules = []) {
  try {
    const data = await request(`${API}/cameras/${cameraId}/algorithm-rules`);
    renderRules(data.rules || []);
  } catch (e) {
    renderRules(fallbackRules || []);
    showError(`算法规则加载失败：${e.message}`);
  }
}

async function loadSelectedRuntimeConfig(cameraId) {
  if (!generatedRuntimeConfigEl || !cameraId) return;
  try {
    const data = await request(`${API}/cameras/${encodeURIComponent(cameraId)}/runtime-config`);
    selectedRuntimeConfig = data;
    renderSelectedRuntimeConfig(data);
  } catch (e) {
    selectedRuntimeConfig = null;
    renderSelectedRuntimeConfigError(e.message);
  }
}

function setRuntimeControlButtonsBusy(busy) {
  for (const button of [
    startSingleRuntimeBtn,
    stopSingleRuntimeBtn,
    restartSingleRuntimeBtn,
    stopDualRuntimeBtn,
    refreshRuntimeOverviewBtn,
  ]) {
    if (button) button.disabled = busy;
  }
}

function runtimeControlActionMessage(data) {
  const actions = Array.isArray(data?.actions) ? data.actions : [];
  const ok = actions.filter((item) => item.ok).length;
  const missing = actions.filter((item) => item.missing).length;
  const failed = actions.length - ok - missing;
  const labels = {
    single_start: "单路链路启动命令已发送",
    single_stop: "单路链路停止命令已发送",
    single_restart: "单路链路重启命令已发送",
    dual_stop: "双路扩展停止命令已发送",
  };
  const label = labels[data?.runtime_action] || "运行控制命令已发送";
  return `${label}：成功 ${ok} 个，缺失 ${missing} 个，失败 ${failed} 个`;
}

async function runRuntimeControlAction(path, confirmText = "") {
  if (confirmText && !window.confirm(confirmText)) {
    return null;
  }
  clearMessages();
  setRuntimeControlButtonsBusy(true);
  try {
    const data = await request(path, { method: "POST" });
    runtimeControl = data.status || runtimeControl;
    renderRuntimeControlStatus();
    showSuccess(runtimeControlActionMessage(data));
    await loadRuntimeOverview();
    return data;
  } finally {
    setRuntimeControlButtonsBusy(false);
  }
}

async function startSingleRuntime() {
  return runRuntimeControlAction(`${API}/runtime/control/single/start`);
}

async function stopSingleRuntime() {
  return runRuntimeControlAction(
    `${API}/runtime/control/single/stop`,
    "确认停止单路推理与录像链路？8090 操作台会保持在线。"
  );
}

async function restartSingleRuntime() {
  return runRuntimeControlAction(
    `${API}/runtime/control/single/restart`,
    "确认重启单路推理与录像链路？8090 操作台会保持在线。"
  );
}

async function stopDualRuntime() {
  return runRuntimeControlAction(
    `${API}/runtime/control/dual/stop`,
    "确认关闭双路扩展容器？单路主链路和 8090 操作台会保持在线。"
  );
}

function runtimeApplyMessage(data) {
  const started = data.dynamic_sources_started || [];
  const composeStarted = data.compose_sources_started || [];
  const restarted = data.savant_restarted || "Savant";
  const replay = data.replay_restarted || "Replay";
  const appliedRules = (data.applied_rules || []).length;
  const skippedRules = (data.skipped_rules || []).length;
  const unsupportedRules = (data.unsupported_rules || []).length;
  return `运行时已应用：${composeStarted.length} 个固定源、${started.length} 个动态源，${replay} / ${restarted} 已重启；规则已应用 ${appliedRules}、跳过 ${skippedRules}、未支持 ${unsupportedRules}`;
}

function sourceApplyMessage(data) {
  const started = data.dynamic_sources_started || [];
  const recreated = data.dynamic_sources_recreated || [];
  const stopped = data.dynamic_sources_stopped || [];
  const kept = data.dynamic_sources_kept || [];
  return `摄像头源已应用：启动 ${started.length} 个、重建 ${recreated.length} 个、停止 ${stopped.length} 个、保持 ${kept.length} 个`;
}

function sourceApplyPayloadStatus(cameraResponse) {
  const payload = cameraResponse?.runtime_source_apply;
  if (!payload) return { ok: true, message: "" };
  if (payload.ok && payload.result) {
    return { ok: true, message: sourceApplyMessage(payload.result) };
  }
  const reason = payload.error || payload.skipped || "未知原因";
  return { ok: false, message: `摄像头源未应用：${reason}` };
}

function showCameraSourceApplyResult(context, cameraResponse) {
  const status = sourceApplyPayloadStatus(cameraResponse);
  if (!status.message) {
    showSuccess(context);
  } else if (status.ok) {
    showSuccess(`${context}；${status.message}`);
  } else {
    showError(`${context}，但${status.message}`);
  }
}

async function applyCameraSources({ context = "" } = {}) {
  const data = await request(`${API}/cameras/runtime/sources/apply`, { method: "POST" });
  const message = sourceApplyMessage(data);
  showSuccess(context ? `${context}；${message}` : message);
  return data;
}

async function applyRuntime({ context = "" } = {}) {
  const data = await request(`${API}/cameras/runtime/apply`, { method: "POST" });
  lastRuntimeApplyResult = data;
  renderRuntimeApplyResult();
  renderQuickAlgorithmControls();
  if (selectedCameraId) {
    await loadSelectedRuntimeConfig(selectedCameraId);
  }
  const message = runtimeApplyMessage(data);
  showSuccess(context ? `${context}；${message}` : message);
  return data;
}

function runtimeRestartMessage(data) {
  const composeStarted = data.compose_sources_started || [];
  const started = data.dynamic_sources_started || [];
  const workers = data.workers_restarted || [];
  const appliedRules = (data.applied_rules || []).length;
  const skippedRules = (data.skipped_rules || []).length;
  const unsupportedRules = (data.unsupported_rules || []).length;
  return `运行时已受控重启：${composeStarted.length} 个固定源、${started.length} 个动态源、${workers.length} 个 worker 已恢复；规则已应用 ${appliedRules}、跳过 ${skippedRules}、未支持 ${unsupportedRules}`;
}

async function restartRuntime() {
  if (!window.confirm("确认受控重启推理与录像链路？8090 管理端会保持在线。")) {
    return null;
  }
  const data = await request(`${API}/cameras/runtime/restart`, { method: "POST" });
  lastRuntimeApplyResult = data;
  renderRuntimeApplyResult();
  renderQuickAlgorithmControls();
  if (selectedCameraId) {
    await loadSelectedRuntimeConfig(selectedCameraId);
  }
  showSuccess(runtimeRestartMessage(data));
  return data;
}

async function applyRuntimeAfterChange(context) {
  try {
    await applyRuntime({ context });
  } catch (e) {
    showError(`${context}，但运行时应用失败：${apiErrorMessage(e)}`);
  }
}

async function saveCamera() {
  clearMessages();
  const camera = formToCamera();
  const exists = cameras.some((c) => c.id === camera.id);
  let savedCamera;
  if (exists) {
    const { id, ...body } = camera;
    savedCamera = await request(`${API}/cameras/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  } else {
    savedCamera = await request(`${API}/cameras`, { method: "POST", body: JSON.stringify(camera) });
  }
  selectedCameraId = camera.id;
  await loadCameras();
  showCameraSourceApplyResult("摄像头已保存；配置并启用算法规则后才会产生告警和证据", savedCamera);
}

async function setCameraEnabled(enabled) {
  clearMessages();
  const camera = formToCamera();
  if (!camera.id) return;
  const savedCamera = await request(`${API}/cameras/${encodeURIComponent(camera.id)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  await loadCameras();
  showCameraSourceApplyResult(`摄像头已${enabled ? "启用" : "停用"}`, savedCamera);
}

async function saveZone({ bindRules = false } = {}) {
  if (!selectedCameraId) { showError("未选择摄像头"); return; }
  clearMessages();
  let body;
  try {
    body = parseJsonTextarea(zoneJson, "区域配置");
  } catch (e) {
    showError(e.message);
    return;
  }
  const config = await request(`${API}/cameras/${selectedCameraId}/config`);
  const exists = (config.zones || []).some((z) => z.zone_id === body.zone_id);
  const bindRuleRefs = bindRules && body.zone_type === "polygon";
  const bindQuery = bindRuleRefs ? "?bind_rules=true" : "";
  const path = exists
    ? `${API}/cameras/${selectedCameraId}/zones/${encodeURIComponent(body.zone_id)}`
    : `${API}/cameras/${selectedCameraId}/zones`;
  await request(`${path}${bindQuery}`, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(bindRuleRefs ? `最终检测区域 ${body.zone_id} 已保存` : `区域 ${body.zone_id} 已保存`);
}

async function deleteZone(zoneId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/zones/${encodeURIComponent(zoneId)}`, {
    method: "DELETE",
  });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`区域 ${zoneId} 已删除`);
}

async function saveRule() {
  if (!selectedCameraId) { showError("未选择摄像头"); return; }
  clearMessages();
  let config;
  try {
    config = parseJsonTextarea(ruleJson, "算法参数");
  } catch (e) {
    showError(e.message);
    return;
  }
  const fd = new FormData(ruleForm);
  const algorithmId = String(fd.get("algorithm_id") || "").trim();
  if (!algorithmId) {
    showError("请选择算法");
    return;
  }
  const support = supportForAlgorithm(algorithmId);
  if (isAlgorithmBlockedSupport(support) && !algorithmDebugModeEnabled()) {
    showError(`${algorithmLabel(algorithmId)} 当前为 ${supportStatusLabel(support.status)}，不能保存或应用`);
    return;
  }
  const ruleId = String(fd.get("rule_id") || ruleIdForAlgorithm(algorithmId)).trim();
  const zoneId = String(fd.get("zone_id") || config.zone_id || config.zone || "").trim();
  const lineId = String(fd.get("line_id") || config.line_id || "").trim();
  const severity = String(fd.get("severity") || config.severity || "medium");
  const body = {
    algorithm_id: algorithmId,
    rule_id: ruleId,
    enabled: fd.get("enabled") === "on",
    severity,
    config,
    evidence_policy: ruleEvidencePolicyFromForm(),
  };
  if (zoneId) body.zone_id = zoneId;
  if (lineId) body.line_id = lineId;
  const exists = currentRules.some((r) => r.rule_id === ruleId);
  const encodedCameraId = encodeURIComponent(selectedCameraId);
  const path = exists
    ? `${API}/cameras/${encodedCameraId}/algorithm-rules/${encodeURIComponent(ruleId)}`
    : `${API}/cameras/${encodedCameraId}/algorithm-rules`;
  await request(path, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`规则 ${ruleId} 已保存`);
}

async function deleteRule(ruleId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${encodeURIComponent(selectedCameraId)}/rules/${encodeURIComponent(ruleId)}`, {
    method: "DELETE",
  });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`规则 ${ruleId} 已删除`);
}

async function setRuleEnabled(ruleId, enabled) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${encodeURIComponent(selectedCameraId)}/algorithm-rules/${encodeURIComponent(ruleId)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`规则 ${ruleId} 已${enabled ? "启用" : "停用"}`);
}

function quickRuleBodyFromCard(card) {
  const algorithmId = card.dataset.algorithmId;
  const existing = ruleForAlgorithm(algorithmId);
  const enabled = card.querySelector('[data-control="enabled"]')?.checked === true;
  const preSeconds = asInt(card.querySelector('[data-control="pre_seconds"]')?.value, 5);
  const postSeconds = asInt(card.querySelector('[data-control="post_seconds"]')?.value, 5);
  const base = defaultRuleForAlgorithm(algorithmId);
  const config = { ...(base.config || {}), ...(existing?.config || {}) };
  const cooldownControl = card.querySelector('[data-control="cooldown_s"]');
  if (cooldownControl) {
    config.cooldown_s = asInt(cooldownControl.value, asInt(config.cooldown_s, 30));
  }
  const minInsideControl = card.querySelector('[data-control="min_inside_ms"]');
  if (minInsideControl) {
    config.min_inside_ms = asInt(minInsideControl.value, asInt(config.min_inside_ms, 1000));
  }
  const thresholdControl = card.querySelector('[data-control="threshold"]');
  if (thresholdControl) {
    const fallback = Number(config.threshold ?? 0.75);
    config.threshold = asFloat(thresholdControl.value, Number.isFinite(fallback) ? fallback : 0.75);
  }
  const targetControls = Array.from(card.querySelectorAll('[data-control="target_person"]'));
  if (targetControls.length > 0) {
    const selectedTargets = targetControls.filter((control) => control.checked);
    config.target_person_ids = selectedTargets
      .map((control) => Number.parseInt(control.dataset.personId || "", 10))
      .filter((value) => Number.isFinite(value));
    config.target_external_person_ids = selectedTargets
      .map((control) => String(control.dataset.externalPersonId || "").trim())
      .filter(Boolean);
    config.target_names = selectedTargets
      .map((control) => String(control.dataset.personName || "").trim())
      .filter(Boolean);
    delete config.camera_scope;
    delete config.person_ids;
    delete config.external_person_ids;
    delete config.names;
    delete config.targets;
  }
  const body = {
    algorithm_id: algorithmId,
    rule_id: existing?.rule_id || base.rule_id || ruleIdForAlgorithm(algorithmId),
    enabled,
    severity: existing?.severity || config.severity || "medium",
    config,
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: preSeconds,
      post_seconds: postSeconds,
    },
  };
  if (algorithmNeedsZone(algorithmId)) {
    const zoneId = String(card.querySelector('[data-control="zone_id"]')?.value || "").trim();
    if (enabled && !zoneId) {
      throw new Error(`${algorithmLabel(algorithmId)} 需要绑定区域`);
    }
    if (zoneId) {
      body.zone_id = zoneId;
      body.config.zone_id = zoneId;
      body.config.zone = zoneId;
    }
  }
  if (algorithmNeedsLine(algorithmId)) {
    const lineId = String(card.querySelector('[data-control="line_id"]')?.value || "").trim();
    if (enabled && !lineId) {
      throw new Error(`${algorithmLabel(algorithmId)} 需要绑定检测线`);
    }
    if (lineId) {
      body.line_id = lineId;
      body.config.line_id = lineId;
    }
  }
  return { body, existing };
}

async function persistQuickAlgorithmCard(card) {
  const algorithmId = card.dataset.algorithmId || "";
  const support = supportForAlgorithm(algorithmId);
  if (isAlgorithmBlockedSupport(support) && !algorithmDebugModeEnabled()) {
    return { saved: false, skippedBlocked: true, body: null };
  }
  const { body, existing } = quickRuleBodyFromCard(card);
  if (!body.enabled && !existing) {
    return { saved: false, skippedBlocked: false, body };
  }
  const encodedCameraId = encodeURIComponent(selectedCameraId);
  const path = existing
    ? `${API}/cameras/${encodedCameraId}/algorithm-rules/${encodeURIComponent(existing.rule_id)}`
    : `${API}/cameras/${encodedCameraId}/algorithm-rules`;
  const savedRule = await request(path, {
    method: existing ? "PUT" : "POST",
    body: JSON.stringify(body),
  });
  upsertCurrentRule(savedRule);
  return { saved: true, skippedBlocked: false, body, rule: savedRule };
}

async function saveQuickAlgorithmCard(card) {
  if (!selectedCameraId) {
    showError("未选择摄像头");
    return;
  }
  clearMessages();
  const cameraId = selectedCameraId;
  const result = await persistQuickAlgorithmCard(card);
  if (!result.saved) {
    if (result.skippedBlocked) {
      showSuccess("未保存：该算法当前为未支持或已延期状态");
    } else {
      showSuccess("未保存算法配置");
    }
    renderRuntimeApplyResult();
    return;
  }
  await applyRuntime({
    context: `${algorithmLabel(result.body.algorithm_id)} 配置已保存`,
  });
  if (selectedCameraId === cameraId) {
    await selectCamera(cameraId, { clear: false });
  }
}

async function saveQuickAlgorithmControls() {
  if (!selectedCameraId) {
    showError("未选择摄像头");
    return;
  }
  clearMessages();
  const cameraId = selectedCameraId;
  const cards = Array.from(algorithmControlsEl?.querySelectorAll(".algorithm-control-item") || []);
  let savedCount = 0;
  let skippedBlocked = 0;
  for (const card of cards) {
    const result = await persistQuickAlgorithmCard(card);
    if (result.skippedBlocked) {
      skippedBlocked += 1;
      continue;
    }
    if (result.saved) savedCount += 1;
  }
  if (savedCount === 0) {
    showSuccess(
      skippedBlocked
        ? `未保存可运行算法；跳过 ${skippedBlocked} 个未支持或已延期算法`
        : "未保存算法配置"
    );
    renderRuntimeApplyResult();
    return;
  }
  await applyRuntime({
    context: `${savedCount} 个算法配置已保存${skippedBlocked ? `，跳过 ${skippedBlocked} 个未支持或已延期算法` : ""}`,
  });
  if (selectedCameraId === cameraId) {
    await selectCamera(cameraId, { clear: false });
  }
}

/* ---- Event listeners ---- */

document.getElementById("refresh-cameras").addEventListener("click", () => {
  loadCameras().catch((e) => showError(e.message));
});
document.getElementById("refresh-people").addEventListener("click", () => {
  loadPeople().catch((e) => showError(e.message));
});
peopleSearchEl.addEventListener("input", () => {
  loadPeople().catch((e) => showError(e.message));
});

document.getElementById("new-camera").addEventListener("click", () => {
  clearMessages();
  const cameraId = makeCameraId();
  fillCamera({
    id: cameraId,
    name: "",
    source_id: `source_${cameraId}`,
    rtsp_url: "",
    site_id: "",
    location: "",
    gpu_id: 0,
    input_type: "rtsp",
    rtsp_transport: "tcp",
    enabled: true,
    fps_policy: { max_fps: "8/1", min_fps: "2/1" },
    alert_policy: { global_alert_cooldown_s: 30, store_suppressed_events: true, suppress_record_request: true, critical_bypass: false },
  });
  zonesEl.innerHTML = "";
  rulesEl.innerHTML = "";
  currentZones = [];
  currentRules = [];
  renderZoneSelectors();
  fullConfigEl.value = "";
  ruleJson.value = "";
  zoneJson.value = "";
  prepareRoiEditorForCamera([]);
  if (ruleAlgorithmEl?.value) {
    fillRuleForm(defaultRuleForAlgorithm(ruleAlgorithmEl.value));
  }
  setStatus("新建摄像头");
});

document.getElementById("save-camera").addEventListener("click", () => {
  saveCamera().catch((e) => showError(e.message));
});
document.getElementById("enable-camera").addEventListener("click", () => {
  setCameraEnabled(true).catch((e) => showError(e.message));
});
document.getElementById("disable-camera").addEventListener("click", () => {
  setCameraEnabled(false).catch((e) => showError(e.message));
});
document.getElementById("open-rules-panel")?.addEventListener("click", () => {
  if (!selectedCameraId && !cameraForm.elements.id.value) {
    showError("请先选择或保存摄像头");
    return;
  }
  clearMessages();
  switchCameraTab("rules");
  ruleForm?.scrollIntoView({ block: "start", behavior: "smooth" });
});
document.getElementById("open-recording-settings")?.addEventListener("click", () => {
  if (!selectedCameraId && !cameraForm.elements.id.value) {
    showError("请先选择或保存摄像头");
    return;
  }
  clearMessages();
  switchCameraTab("rules");
  ruleForm?.elements.pre_seconds?.focus();
});
document.getElementById("apply-runtime").addEventListener("click", () => {
  clearMessages();
  applyCameraSources().catch((e) => showError(`摄像头源应用失败：${e.message}`));
});
document.getElementById("restart-runtime").addEventListener("click", () => {
  clearMessages();
  restartRuntime().catch((e) => showError(`运行时受控重启失败：${apiErrorMessage(e)}`));
});
themeToggleBtn?.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark", { persist: true });
});
saveQuickAlgorithmsBtn?.addEventListener("click", () => {
  saveQuickAlgorithmControls().catch((e) => showError(apiErrorMessage(e)));
});
roiPreviewImageEl?.addEventListener("load", () => {
  roiState.imageLoaded = true;
  if (!roiState.sourceWidth) roiState.sourceWidth = roiPreviewImageEl.naturalWidth || 0;
  if (!roiState.sourceHeight) roiState.sourceHeight = roiPreviewImageEl.naturalHeight || 0;
  roiPreviewEmptyEl?.classList.add("hidden");
  drawRoiCanvas();
  const source = roiSourceDimensions();
  setRoiStatus(`画面已加载：${Math.round(source.width)} x ${Math.round(source.height)}`);
});
roiPreviewImageEl?.addEventListener("error", () => {
  roiState.imageLoaded = false;
  roiPreviewEmptyEl?.classList.remove("hidden");
  drawRoiCanvas();
  setRoiStatus("画面加载失败");
});
for (const target of [roiCanvasEl, roiCanvasWrapEl]) {
  target?.addEventListener("pointerdown", addRoiPointFromEvent);
  target?.addEventListener("mousedown", addRoiPointFromEvent);
  target?.addEventListener("click", addRoiPointFromEvent);
}
roiZoneTypeEl?.addEventListener("change", () => {
  const type = roiZoneTypeEl.value === "line" ? "line" : "polygon";
  if (roiZoneIdEl && (!roiZoneIdEl.value || ["perimeter", "tripwire_01"].includes(roiZoneIdEl.value))) {
    roiZoneIdEl.value = nextRoiZoneId(type);
  }
  if (type === "line" && roiState.points.length > 2) {
    roiState.points = roiState.points.slice(0, 2);
  }
  writeRoiZoneJson({ allowIncomplete: true });
  drawRoiCanvas();
});
roiZoneIdEl?.addEventListener("input", () => {
  writeRoiZoneJson({ allowIncomplete: true });
});
window.addEventListener("resize", () => {
  drawRoiCanvas();
});
document.getElementById("refresh-roi-preview")?.addEventListener("click", () => {
  refreshRoiPreview().catch((e) => setRoiStatus(`画面不可用：${e.message}`));
});
document.getElementById("add-roi-center-point")?.addEventListener("click", () => {
  addRoiCenterPoint();
});
document.getElementById("undo-roi-point")?.addEventListener("click", () => {
  roiState.points.pop();
  writeRoiZoneJson({ allowIncomplete: true });
  drawRoiCanvas();
});
document.getElementById("clear-roi-points")?.addEventListener("click", () => {
  roiState.points = [];
  writeRoiZoneJson({ allowIncomplete: true });
  drawRoiCanvas();
});
document.getElementById("save-roi-zone")?.addEventListener("click", () => {
  saveRoiZone().catch((e) => showError(e.message));
});

document.getElementById("add-zone-polygon").addEventListener("click", () => {
  startNewRoiDraft("polygon");
});

document.getElementById("add-zone-line").addEventListener("click", () => {
  startNewRoiDraft("line");
});

document.getElementById("save-zone").addEventListener("click", () => {
  saveZone({ bindRules: true }).catch((e) => showError(e.message));
});
document.getElementById("save-rule").addEventListener("click", () => {
  saveRule().catch((e) => showError(e.message));
});
ruleAlgorithmEl?.addEventListener("change", () => {
  fillRuleForm(defaultRuleForAlgorithm(ruleAlgorithmEl.value));
});
document.getElementById("submit-face-registration").addEventListener("click", () => {
  submitFaceRegistration().catch((e) => showError(e.message));
});
registerNewPersonBtn?.addEventListener("click", () => {
  setFaceRegistrationMode("new");
});
appendSelectedPersonBtn?.addEventListener("click", () => {
  if (!selectedPerson) {
    showError("请先选择人员");
    return;
  }
  setFaceRegistrationMode("append");
});
previewDeleteSelectedPersonBtn?.addEventListener("click", () => {
  if (!selectedPersonId) {
    showError("未选择人员");
    return;
  }
  openMaintenanceWithRequest(selectedPersonDeleteRequest());
});
refreshRuntimeOverviewBtn?.addEventListener("click", () => {
  clearMessages();
  loadRuntimeOverview().catch((e) => showError(`运行状态刷新失败：${e.message}`));
});
refreshRuntimeConfigBtn?.addEventListener("click", () => {
  if (!selectedCameraId) {
    showError("未选择摄像头");
    return;
  }
  clearMessages();
  loadSelectedRuntimeConfig(selectedCameraId).catch((e) => showError(`生成配置刷新失败：${e.message}`));
});
startSingleRuntimeBtn?.addEventListener("click", () => {
  startSingleRuntime().catch((e) => showError(`单路链路启动失败：${e.message}`));
});
stopSingleRuntimeBtn?.addEventListener("click", () => {
  stopSingleRuntime().catch((e) => showError(`单路链路停止失败：${e.message}`));
});
restartSingleRuntimeBtn?.addEventListener("click", () => {
  restartSingleRuntime().catch((e) => showError(`单路链路重启失败：${apiErrorMessage(e)}`));
});
stopDualRuntimeBtn?.addEventListener("click", () => {
  stopDualRuntime().catch((e) => showError(`双路扩展关闭失败：${e.message}`));
});

document.querySelectorAll("[data-template]").forEach((button) => {
  button.addEventListener("click", () => {
    const algorithmId = button.dataset.template;
    const support = supportForAlgorithm(algorithmId);
    if (isAlgorithmBlockedSupport(support) && !algorithmDebugModeEnabled()) {
      showError(`${algorithmLabel(algorithmId)} 当前为 ${supportStatusLabel(support.status)}，不能保存或应用`);
      return;
    }
    const template = templates[algorithmId];
    if (template) {
      fillRuleForm(template);
    }
  });
});

/* ---- Init ---- */
applyTheme(currentTheme());
restoreEvidenceCount();
loadEvidenceCount();
const requestedTopView = initialTopView();
activateTopView(requestedTopView, requestedTopView !== "cameras" && !topViewFromHash());
loadCameras()
  .then(() => {
    activateTopView(requestedTopView, false);
  })
  .catch((e) => showError(e.message));
