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
let selectedPersonRequestId = 0;
let pendingOpenPersonId = "";
let faceRegistrationMode = "new";
let algorithms = [];
let algorithmSupportMatrix = [];
let currentZones = [];
let currentRules = [];
let runtimeOverview = null;
let runtimeControl = null;
let runtimePerformance = null;
let runtimeTopology = null;
let runtimeTopologyApplyStatus = null;
let runtimeLatency = null;
let runtimeLoadErrors = {};
let runtimeActionInFlight = "";
let runtimeApplyPollTimer = 0;
let quickRuntimeManualAssignments = {};
let quickRuntimeSelectedSourceIds = new Set();
let quickRuntimeSelectionInitialized = false;
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
const runtimeDecisionCopyEl = document.getElementById("runtime-decision-copy");
const runtimeDriftSummaryEl = document.getElementById("runtime-drift-summary");
const runtimePerformanceForm = document.getElementById("runtime-performance-form");
const runtimePerformanceStatusEl = document.getElementById("runtime-performance-status");
const runtimePerformanceDiffEl = document.getElementById("runtime-performance-diff");
const runtimeTopologyForm = document.getElementById("runtime-topology-form");
const runtimeTopologyStatusEl = document.getElementById("runtime-topology-status");
const runtimeTopologyPlanEl = document.getElementById("runtime-topology-plan");
const runtimeTopologyAssignmentsEl = document.getElementById("runtime-topology-assignments");
const quickRuntimeProfileEl = document.getElementById("quick-runtime-profile");
const quickRuntimeShardStrategyEl = document.getElementById("quick-runtime-shard-strategy");
const quickRuntimeCapacityEl = document.getElementById("quick-runtime-capacity");
const quickRuntimeAssignmentsEl = document.getElementById("quick-runtime-assignments");
const quickRuntimeResultEl = document.getElementById("quick-runtime-result");
const quickRuntimeProgressEl = document.getElementById("quick-runtime-progress");
const quickRuntimeProgressPhaseEl = document.getElementById("quick-runtime-progress-phase");
const quickRuntimeProgressPercentEl = document.getElementById("quick-runtime-progress-percent");
const quickRuntimeProgressElapsedEl = document.getElementById("quick-runtime-progress-elapsed");
const quickRuntimeProgressTrackEl = document.getElementById("quick-runtime-progress-track");
const quickRuntimeProgressBarEl = document.getElementById("quick-runtime-progress-bar");
const quickRuntimeProgressMessageEl = document.getElementById("quick-runtime-progress-message");
const quickRuntimeRollingProgressEl = document.getElementById("quick-runtime-rolling-progress");
const quickRuntimeRollingRemainingEl = document.getElementById("quick-runtime-rolling-remaining");
const quickRuntimeStartBtn = document.getElementById("quick-runtime-start");
const quickRuntimeStopBtn = document.getElementById("quick-runtime-stop");
const quickRuntimeManageCamerasBtn = document.getElementById("quick-runtime-manage-cameras");
const quickRuntimeSelectRequiredBtn = document.getElementById("quick-runtime-select-required");
const quickRuntimeClearSelectionBtn = document.getElementById("quick-runtime-clear-selection");
const refreshRuntimeOverviewBtn = document.getElementById("refresh-runtime-overview");
const refreshRuntimeLatencyBtn = document.getElementById("refresh-runtime-latency");
const runtimeLatencySummaryEl = document.getElementById("runtime-latency-summary");
const runtimeLatencyDetailEl = document.getElementById("runtime-latency-detail");
const startSingleRuntimeBtn = document.getElementById("start-single-runtime");
const stopSingleRuntimeBtn = document.getElementById("stop-single-runtime");
const restartSingleRuntimeBtn = document.getElementById("restart-single-runtime");
const stopDualRuntimeBtn = document.getElementById("stop-dual-runtime");
const recoverRuntimeSourcesBtn = document.getElementById("recover-runtime-sources");
const applySavedRuntimeTopologyBtn = document.getElementById("apply-saved-runtime-topology");
const saveRuntimePerformanceBtn = document.getElementById("save-runtime-performance");
const applyRuntimePerformanceBtn = document.getElementById("apply-runtime-performance");
const saveRuntimeTopologyBtn = document.getElementById("save-runtime-topology");
const applyRuntimeTopologyBtn = document.getElementById("apply-runtime-topology");
const toggleRuntimeAdvancedBtn = document.getElementById("toggle-runtime-advanced");
const runtimeTopologyBranchSettingsEl = document.getElementById("runtime-topology-branch-settings");
const runtimeTopologyBranchBEl = document.getElementById("runtime-topology-branch-b");
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
const faceRegistrationFileCountEl = document.getElementById("face-registration-file-count");
const personProfileEl = document.getElementById("person-profile");
const personDetailEl = document.getElementById("person-detail");
const galleryEl = document.getElementById("gallery");
const faceRegistrationSummaryEl = document.getElementById("face-registration-summary");
const faceRegistrationResultEl = document.getElementById("face-registration-result");
const registerNewPersonBtn = document.getElementById("register-new-person");
const appendSelectedPersonBtn = document.getElementById("append-selected-person");
const registrationModeStatusEl = document.getElementById("registration-mode-status");
const previewDeleteSelectedPersonBtn = document.getElementById("preview-delete-selected-person");
const findSelectedPersonBtn = document.getElementById("find-selected-person");
const THEME_STORAGE_KEY = "operator-theme";
const ACTIVE_VIEW_STORAGE_KEY = "operator-active-view";
const EVIDENCE_COUNT_STORAGE_KEY = "operator-evidence-count";
const TOP_VIEWS = new Set(["cameras", "people", "trajectory", "evidence", "maintenance", "runtime"]);
const PRIMARY_TOP_VIEW_BY_VIEW = {
  cameras: "cameras",
  people: "cameras",
  evidence: "evidence",
  trajectory: "evidence",
  runtime: "runtime",
  maintenance: "runtime",
};
const RUNTIME_ADVANCED_PANE_IDS = [
  "runtime-performance-pane",
  "runtime-topology-pane",
  "runtime-container-pane",
];
const QUICK_RUNTIME_PROFILE_META = {
  production_t4_40: { label: "生产 T4 40 路", expected: 40 },
  local_4090_60: { label: "本机 4090 60 路", expected: 60 },
};
let runtimeAdvancedVisible = false;

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
      clip_required: false,
      evidence_mode: "image_only",
      playback_kind: "image",
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
  "behavior.loitering": {
    rule_id: "rule_loitering",
    algorithm_id: "behavior.loitering",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_duration_s: 60,
      max_avg_speed_px_s: 20,
      cooldown_s: 60,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "behavior.running": {
    rule_id: "rule_running",
    algorithm_id: "behavior.running",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_speed_px_s: 250,
      min_duration_ms: 500,
      cooldown_s: 20,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "behavior.crowd_gathering": {
    rule_id: "rule_crowd_gathering",
    algorithm_id: "behavior.crowd_gathering",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_person_count: 5,
      exit_person_count: 3,
      min_duration_s: 2,
      eps_px: 180,
      require_in_zone: true,
      cooldown_s: 60,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "behavior.fall": {
    rule_id: "rule_fall",
    algorithm_id: "behavior.fall",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_down_ms: 1500,
      cooldown_s: 60,
      require_transition: true,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "behavior.chasing": {
    rule_id: "rule_chasing",
    algorithm_id: "behavior.chasing",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_speed_px_s: 120,
      max_distance_px: 220,
      min_pair_duration_s: 1.5,
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
  "behavior.loitering",
  "behavior.running",
  "behavior.crowd_gathering",
  "behavior.fall",
  "behavior.chasing",
  "face.watchlist",
];

const quickAlgorithmLabels = {
  "behavior.intrusion": "入侵检测",
  "behavior.loitering": "徘徊",
  "behavior.running": "奔跑",
  "behavior.crowd_gathering": "聚集",
  "behavior.fall": "摔倒",
  "behavior.chasing": "追逐",
  "face.watchlist": "名单命中",
};

const operatorAlgorithmMeta = {
  "behavior.intrusion": {
    title: "入侵检测",
    subtitle: "区域入侵事件与证据",
  },
  "behavior.loitering": {
    title: "徘徊",
    subtitle: "低速停留事件",
  },
  "behavior.running": {
    title: "奔跑",
    subtitle: "轨迹速度事件",
  },
  "behavior.crowd_gathering": {
    title: "聚集",
    subtitle: "多人密集事件",
  },
  "behavior.fall": {
    title: "摔倒",
    subtitle: "姿态跌倒事件",
  },
  "behavior.chasing": {
    title: "追逐",
    subtitle: "多轨迹追逐事件",
  },
  "face.watchlist": {
    title: "名单命中",
    subtitle: "按摄像头名单、图片轨迹与最近位置",
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

function updateFaceRegistrationFileCount() {
  if (!faceRegistrationFileCountEl || !faceRegistrationForm) return;
  const files = Array.from(faceRegistrationForm.elements.images?.files || []);
  if (!files.length) {
    faceRegistrationFileCountEl.textContent = "尚未选择图片";
    return;
  }
  const names = files.slice(0, 3).map((file) => file.name).join("、");
  const remainder = files.length > 3 ? ` 等 ${files.length} 张` : "";
  faceRegistrationFileCountEl.textContent = `已选择 ${files.length} 张：${names}${remainder}`;
}

function clearFaceRegistrationFiles() {
  if (!faceRegistrationForm?.elements.images) return;
  faceRegistrationForm.elements.images.value = "";
  updateFaceRegistrationFileCount();
}

function clearFaceRegistrationIdentityFields() {
  if (!faceRegistrationForm) return;
  clearFaceRegistrationFiles();
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

function primaryTopView(view) {
  return PRIMARY_TOP_VIEW_BY_VIEW[normalizedTopView(view)] || "cameras";
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
    const validationDetail = Array.isArray(body.detail)
      ? body.detail.map((item) => {
          const loc = Array.isArray(item.loc) ? item.loc.join(".") : "";
          return `${loc}: ${item.msg || JSON.stringify(item)}`;
        }).join("; ")
      : (typeof body.detail === "string" ? body.detail : "");
    const error = new Error(body.error?.message || body.error?.detail || validationDetail || `HTTP ${response.status}`);
    error.status = response.status;
    error.details = body.error?.details || body.detail || null;
    throw error;
  }
  return body.data ?? body;
}

function apiErrorMessage(error) {
  const details = error?.details || {};
  if (details.blocked && details.active_count) {
    const count = formatInteger(details.active_count);
    const taskSummary = (details.tasks || []).slice(0, 3).map((task) => {
      const label = runtimeEvidenceStateLabel(task.blocking_state || task.status || task.materialization_status);
      const source = task.source_id || task.camera_id || "--";
      const eventType = task.event_type || "--";
      return `${eventType}/${source}/${label}`;
    }).join("；");
    return `仍有 ${count} 个证据任务在生成中，已阻止重启以避免证据丢失${taskSummary ? `；${taskSummary}` : ""}`;
  }
  if (details.guard_unavailable) {
    return "无法确认是否存在生成中的证据任务，已阻止重启";
  }
  const raw = error?.message || String(error || "未知错误");
  if (/failed to fetch|networkerror|load failed/i.test(raw)) return "无法连接服务，请检查网络后重试";
  if (/timeout|timed out/i.test(raw)) return "操作等待超时，请稍后刷新状态";
  if (/HTTP\s*5\d\d/i.test(raw)) return "服务暂时不可用，请稍后重试";
  if (/HTTP\s*4\d\d/i.test(raw)) return "请求未被接受，请检查填写内容";
  return raw;
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
  if (isRuntimeConfigSyncResult(lastRuntimeApplyResult)) return [];
  if (!lastRuntimeApplyResult) return [];
  return [
    ...(lastRuntimeApplyResult.applied_rules || []),
    ...(lastRuntimeApplyResult.skipped_rules || []),
    ...(lastRuntimeApplyResult.unsupported_rules || []),
  ];
}

function isRuntimeConfigSyncResult(result) {
  return result?.runtime_action === "module_config_sync";
}

function latestApplyStateForAlgorithm(algorithmId, rule) {
  if (!lastRuntimeApplyResult || isRuntimeConfigSyncResult(lastRuntimeApplyResult)) {
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
    validZoneRows(currentZones)
      .map(zoneOptionId)
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

  for (const zone of validZoneRows(currentZones)) {
    const points = (Array.isArray(zone?.points) ? zone.points : [])
      .map((point) => sourcePointToDisplay(point, zone?.coordinate_space || "pixel"))
      .filter(Boolean);
    if (points.length < 2) continue;
    const closed = zone?.zone_type === "polygon";
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
    setRoiStatus(`${roiTypeLabel()} ${zoneOptionId(zone)}：${(zone.points || []).length} 个点`);
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
  const polygonZones = validZoneRows(zones).filter((zone) => zone?.zone_type === "polygon");
  if (!polygonZones.length) return null;
  const byId = new Map(
    polygonZones.map((zone) => [zoneOptionId(zone), zone]).filter(([zoneId]) => zoneId)
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
  if (roiZoneIdEl) roiZoneIdEl.value = zoneOptionId(zone) || defaultRoiZoneId(zone?.zone_type);
  const type = zone?.zone_type === "line" || zone?.zone_type === "direction_line" ? "line" : "polygon";
  const source = roiSourceDimensions();
  const points = (Array.isArray(zone?.points) ? zone.points : []).map((point) => {
    const xy = normalizeRoiPoint(point);
    if (!xy) return null;
    if ((zone?.coordinate_space || "pixel") === "normalized") {
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
    setRoiStatus(`最终检测区域：${zoneOptionId(finalZone)}`);
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
  const enabledCount = cameras.filter((camera) => camera.enabled !== false).length;
  if (cameraCountEl) {
    cameraCountEl.textContent = String(cameras.length);
  }
  if (enabledCameraCountEl) {
    enabledCameraCountEl.textContent = String(enabledCount);
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
  if (runtimeTopology) {
    renderRuntimeTopologyAssignments(runtimeTopology.saved_config || {}, runtimeTopology.plan || {});
  }
}

function zoneTypeLabel(zone) {
  return ["line", "direction_line"].includes(zone?.zone_type) ? "检测线" : "区域";
}

function validZoneRows(zones = []) {
  return (Array.isArray(zones) ? zones : []).filter(
    (zone) => zone && typeof zone === "object"
  );
}

function zoneOptionId(zone) {
  return String(zone?.zone_id || zone?.zone_name || "");
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
  const zoneId = zoneOptionId(zone);
  const zoneName = String(zone?.zone_name || "");
  const typeLabel = zoneTypeLabel(zone);
  const points = Array.isArray(zone?.points) ? zone.points : [];
  const pointUnit = typeLabel === "检测线" ? "端点" : "点";
  const bounds = zonePointBounds(points);
  const bindings = zoneRuleBindings(zone);
  const isFinalZone = typeLabel === "区域" && zoneId && zoneId === finalZoneId;
  item.className = `zone-item zone-item-${typeLabel === "检测线" ? "line" : "polygon"}`;
  item.innerHTML =
      `<div class="zone-item-header">` +
      `<div class="zone-item-main">` +
        `<strong>${escapeHtml(zoneId)}</strong>` +
        `<div class="muted">${escapeHtml(zoneName && zoneName !== zoneId ? zoneName : typeLabel)}</div>` +
      `</div>` +
      `<div class="zone-badges">` +
        `<span class="zone-chip">${escapeHtml(typeLabel)}</span>` +
        `<span class="zone-chip ${zone?.enabled === false ? "disabled" : "enabled"}">${zone?.enabled === false ? "停用" : "启用"}</span>` +
        `${isFinalZone ? `<span class="zone-chip final">最终检测区域</span>` : ""}` +
      `</div>` +
    `</div>` +
    `<div class="zone-meta-grid">` +
      `<span>${points.length} 个${pointUnit}</span>` +
      `<span>${escapeHtml(zone?.coordinate_space || "pixel")}</span>` +
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
      zone_id: zoneId,
      zone_name: zoneName,
      zone_type: zone?.zone_type || "polygon",
      coordinate_space: zone?.coordinate_space || "pixel",
      points,
      enabled: zone?.enabled !== false,
    }, null, 2);
    loadZoneIntoRoiEditor(zone);
  });
  item.querySelector('[data-action="delete-zone"]').addEventListener("click", (e) => {
    e.stopPropagation();
    if (zoneId) deleteZone(zoneId);
  });
  return item;
}

function appendZoneGroup({ title, rows, emptyText, finalZoneId }) {
  const safeRows = validZoneRows(rows);
  const group = document.createElement("section");
  group.className = "zone-list-group";
  const header = document.createElement("div");
  header.className = "zone-list-title";
  header.innerHTML = `<h3>${escapeHtml(title)}</h3><span>${safeRows.length}</span>`;
  group.appendChild(header);
  const list = document.createElement("div");
  list.className = "zone-list-items";
  if (safeRows.length) {
    safeRows.forEach((zone) => list.appendChild(createZoneListItem(zone, finalZoneId)));
  } else {
    list.innerHTML = `<div class="zone-list-empty">${escapeHtml(emptyText)}</div>`;
  }
  group.appendChild(list);
  zonesEl.appendChild(group);
}

function renderZones(zones) {
  currentZones = validZoneRows(zones);
  zonesEl.innerHTML = "";
  const finalZone = preferredFinalRoiZone(currentZones, currentRules);
  const finalZoneId = zoneOptionId(finalZone);
  const polygonZones = currentZones.filter((zone) => zone?.zone_type === "polygon");
  const lineZones = currentZones.filter((zone) => ["line", "direction_line"].includes(zone?.zone_type));
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
  const polygonZones = validZoneRows(currentZones).filter((z) => z?.zone_type === "polygon");
  const lineZones = validZoneRows(currentZones).filter((z) => ["line", "direction_line"].includes(z?.zone_type));
  const renderOptions = (select, rows, emptyLabel) => {
    select.innerHTML = `<option value="">${emptyLabel}</option>`;
    for (const zone of rows) {
      const id = zoneOptionId(zone);
      if (!id) continue;
      const zoneName = String(zone?.zone_name || "");
      const option = document.createElement("option");
      option.value = id;
      option.textContent = zoneName && zoneName !== id ? `${zoneName} (${id})` : id;
      select.appendChild(option);
    }
  };
  renderOptions(ruleZoneEl, polygonZones, "不绑定区域");
  renderOptions(ruleLineEl, lineZones, "不绑定检测线");
  if (previousZone && polygonZones.some((zone) => zoneOptionId(zone) === previousZone)) {
    ruleZoneEl.value = previousZone;
  }
  if (previousLine && lineZones.some((zone) => zoneOptionId(zone) === previousLine)) {
    ruleLineEl.value = previousLine;
  }
  renderQuickAlgorithmControls();
}

function algorithmNumberField(label, field, value, disabledAttr, options = {}) {
  const min = options.min ?? 0;
  const step = options.step ?? 1;
  const kind = options.kind || (String(step).includes(".") ? "float" : "int");
  const fallback = options.defaultValue ?? 0;
  const numberValue = Number(value ?? fallback);
  return `<label class="algorithm-field">${escapeHtml(label)}` +
    `<input data-config-field="${escapeHtml(field)}" data-config-kind="${escapeHtml(kind)}" ` +
    `type="number" min="${escapeHtml(min)}" step="${escapeHtml(step)}" ` +
    `value="${Number.isFinite(numberValue) ? numberValue : Number(fallback) || 0}"${disabledAttr} />` +
    `</label>`;
}

function algorithmCheckboxField(label, field, value, disabledAttr) {
  return `<label class="algorithm-field inline algorithm-check-field">` +
    `<input data-config-field="${escapeHtml(field)}" data-config-kind="bool" type="checkbox" ` +
    `${value !== false ? "checked" : ""}${disabledAttr} />` +
    `<span>${escapeHtml(label)}</span>` +
    `</label>`;
}

function renderBehaviorAlgorithmControls(algorithmId, config, disabledAttr) {
  if (algorithmId === "behavior.intrusion") {
    return algorithmNumberField("停留毫秒", "min_inside_ms", config.min_inside_ms ?? 1000, disabledAttr, { min: 1 }) +
      algorithmNumberField("冷却秒数", "cooldown_s", config.cooldown_s ?? 30, disabledAttr, { min: 0 });
  }
  if (algorithmId === "behavior.loitering") {
    return algorithmNumberField("停留秒数", "min_duration_s", config.min_duration_s ?? 60, disabledAttr, { min: 1 }) +
      algorithmNumberField("最高均速", "max_avg_speed_px_s", config.max_avg_speed_px_s ?? 20, disabledAttr, { min: 0, step: 1 }) +
      algorithmNumberField("最大位移", "max_displacement_px", config.max_displacement_px ?? 0, disabledAttr, { min: 0, step: 1 }) +
      algorithmNumberField("冷却秒数", "cooldown_s", config.cooldown_s ?? 60, disabledAttr, { min: 0 });
  }
  if (algorithmId === "behavior.running") {
    return algorithmNumberField("最低速度", "min_speed_px_s", config.min_speed_px_s ?? 250, disabledAttr, { min: 0, step: 1 }) +
      algorithmNumberField("持续毫秒", "min_duration_ms", config.min_duration_ms ?? 500, disabledAttr, { min: 1 }) +
      algorithmNumberField("冷却秒数", "cooldown_s", config.cooldown_s ?? 20, disabledAttr, { min: 0 });
  }
  if (algorithmId === "behavior.crowd_gathering") {
    return algorithmNumberField("触发人数", "min_person_count", config.min_person_count ?? 5, disabledAttr, { min: 1 }) +
      algorithmNumberField("退出人数", "exit_person_count", config.exit_person_count ?? 3, disabledAttr, { min: 0 }) +
      algorithmNumberField("持续秒数", "min_duration_s", config.min_duration_s ?? 2, disabledAttr, { min: 0, step: 0.5, kind: "float" }) +
      algorithmNumberField("聚集半径", "eps_px", config.eps_px ?? 180, disabledAttr, { min: 1, step: 1 }) +
      algorithmNumberField("冷却秒数", "cooldown_s", config.cooldown_s ?? 60, disabledAttr, { min: 0 });
  }
  if (algorithmId === "behavior.fall") {
    return algorithmNumberField("倒地毫秒", "min_down_ms", config.min_down_ms ?? 1500, disabledAttr, { min: 1 }) +
      algorithmCheckboxField("要求姿态变化", "require_transition", config.require_transition ?? true, disabledAttr) +
      algorithmNumberField("冷却秒数", "cooldown_s", config.cooldown_s ?? 60, disabledAttr, { min: 0 });
  }
  if (algorithmId === "behavior.chasing") {
    return algorithmNumberField("最低速度", "min_speed_px_s", config.min_speed_px_s ?? 120, disabledAttr, { min: 0, step: 1 }) +
      algorithmNumberField("最大距离", "max_distance_px", config.max_distance_px ?? 220, disabledAttr, { min: 1, step: 1 }) +
      algorithmNumberField("持续秒数", "min_pair_duration_s", config.min_pair_duration_s ?? 1.5, disabledAttr, { min: 0, step: 0.5, kind: "float" }) +
      algorithmNumberField("冷却秒数", "cooldown_s", config.cooldown_s ?? 30, disabledAttr, { min: 0 });
  }
  return "";
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
  const polygonZones = validZoneRows(currentZones).filter((z) => z?.zone_type === "polygon");
  algorithmControlsEl.innerHTML = "";
  for (const algorithmId of quickAlgorithmIds) {
    const support = supportForAlgorithm(algorithmId);
    const supportStatus = support?.status || "config_only";
    const meta = operatorAlgorithmMeta[algorithmId] || {
      title: algorithmLabel(algorithmId),
      subtitle: algorithmId,
    };
    const rule = ruleForAlgorithm(algorithmId);
    const policy = evidencePolicyFor(rule, algorithmId);
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
            if (!id) return "";
            const zoneName = String(zone?.zone_name || "");
            const selected = id === selectedZone ? " selected" : "";
            return `<option value="${escapeHtml(id)}"${selected}>${escapeHtml(zoneName && zoneName !== id ? `${zoneName} (${id})` : id)}</option>`;
          }).join("") +
        `</select></label>`
      : "";
    const behaviorControls = renderBehaviorAlgorithmControls(algorithmId, config, disabledAttr);
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
          `<span class="support-badge support-${escapeHtml(supportStatus)}">${escapeHtml(supportStatusLabel(supportStatus))}</span>` +
          `<span class="apply-badge ${escapeHtml(applyBadge.className)}">${escapeHtml(applyBadge.label)}</span>` +
          `<button class="sm primary" data-action="save-quick-rule" type="button"${disabledAttr}>保存并应用</button>` +
          `<button class="sm" data-action="edit-quick-rule" type="button"${disabledAttr}>高级</button>` +
        `</div>` +
      `</div>` +
      `<div class="algorithm-control-fields">` +
        zoneControl +
        behaviorControls +
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
  if (ok === true) return "正常";
  if (ok === false) return "异常";
  return "待确认";
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

function formatDate(value) {
  if (value === undefined || value === null || value === "") return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatAge(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  if (num < 60) return `${formatInteger(num)}s`;
  return `${formatNumber(num / 60)}m`;
}

function cameraForSourceId(sourceId) {
  const id = String(sourceId || "");
  if (!id) return null;
  return (cameras || []).find((camera) => (
    String(camera.source_id || "") === id ||
    String(camera.id || "") === id ||
    String(camera.camera_id || "") === id
  )) || null;
}

function sourceDisplayName(sourceId) {
  const id = String(sourceId || "");
  const camera = cameraForSourceId(id);
  return camera?.name || id || "--";
}

function sourceCellHtml(sourceId) {
  const id = String(sourceId || "");
  const name = sourceDisplayName(id);
  const showId = id && name !== id;
  return `<div class="runtime-source-cell">` +
    `<strong>${escapeHtml(name)}</strong>` +
    (showId ? `<small>${escapeHtml(id)}</small>` : "") +
  `</div>`;
}

function isPressureSourceId(sourceId) {
  const id = String(sourceId || "");
  return id.startsWith("pressure") || id.startsWith("forwarder");
}

function runtimeIssueLabel(issue) {
  const labels = {
    compose_source_not_running: "固定视频源未运行；如果该摄像头已停用，可忽略",
    savant_metrics_unavailable: "推理指标不可用",
    analysis_forwarder_metrics_unavailable: "分析限流指标不可用",
    forwarder_metrics_unavailable: "分析限流指标不可用",
    source_convergence_unhealthy: "视频源生成配置与运行态未收敛",
    no_active_sources: "当前没有活跃视频源",
    source_count_mismatch: "活跃视频源数量与配置不一致",
    evidence_metrics_unavailable: "证据生成指标不可用",
    container_restart_warning: "有后台服务近期频繁重启",
    profile_enabled_source_count: "启用摄像头数量符合所选运行预设",
    full_pipeline_roi_worker_container_present: "人脸识别服务已准备",
    full_pipeline_event_worker_container_present: "告警处理服务已准备",
    full_pipeline_media_worker_container_present: "证据生成服务已准备",
    full_pipeline_cuda_mps_container_present: "图形计算服务已准备",
  };
  if (!issue) return "--";
  return labels[issue] || String(issue).replace(/_/g, " ");
}

function containerStateText(containerOrState) {
  const state = typeof containerOrState === "string"
    ? containerOrState
    : (containerOrState?.present === false ? "missing" : (containerOrState?.running ? "running" : containerOrState?.state));
  const labels = {
    running: "运行中",
    exited: "已退出",
    stopped: "已停止",
    missing: "未创建",
    created: "已创建",
    restarting: "重启中",
    dead: "异常退出",
    paused: "已暂停",
  };
  return labels[state] || state || "--";
}

function healthText(value) {
  const labels = {
    healthy: "健康",
    unhealthy: "异常",
    starting: "启动中",
    none: "无健康检查",
    missing: "未创建",
  };
  return labels[value] || value || "--";
}

function containerRoleLabel(role) {
  const labels = {
    api: "管理接口",
    postgres: "数据存储",
    redis: "高速数据服务",
    savant: "视频识别",
    source_adapter: "固定视频源",
    compose_source: "固定视频源（固定源模式）",
    dynamic_source: "动态视频源",
    analysis_forwarder: "分析限流",
    event_worker: "事件处理",
    clip_worker: "证据调度",
    media_worker: "证据视频生成",
    evidence_viewer: "8090 管理端",
    replay: "录像取证",
    runtime_supervisor: "运行监测",
    watchdog: "故障监测",
  };
  return labels[role] || String(role || "--").replace(/_/g, " ");
}

function topologyModeLabel(mode) {
  const labels = {
    auto: "自动选择",
    single: "单分支",
    dual_auto: "自动双分支",
    dual_same_gpu: "双分支同卡",
    dual_dual_gpu: "双分支双卡",
  };
  return labels[mode] || mode || "--";
}

function branchLabel(branchId) {
  const id = String(branchId || "").toUpperCase();
  return id ? `分支 ${id}` : "--";
}

function eventTypeLabel(eventType) {
  const labels = {
    watchlist_hit: "名单命中",
    intrusion: "入侵检测",
    line_crossing: "越线检测",
    loitering: "徘徊",
    crowding: "聚集",
    abandoned_object: "遗留物",
    unknown: "未知事件",
  };
  return labels[eventType] || eventType || "--";
}

function runtimeEvidenceStateLabel(state) {
  const labels = {
    pending: "待处理",
    waiting_proof: "等待帧证明",
    queued: "排队",
    materializing: "生成中",
    materialization_skipped: "已跳过",
    materialized: "可查看",
    generated_unverified: "待复核",
    materialization_failed: "生成失败",
    materialization_deadline_expired: "生成超时",
    replaying: "录像生成中",
    finalizing: "生成中",
    ready: "完成",
    failed: "失败",
    not_implemented: "未实现",
  };
  return labels[state] || state || "--";
}

function runtimeEvidenceStateIsFailure(state) {
  return [
    "failed",
    "materialization_failed",
    "materialization_deadline_expired",
    "generated_corrupt",
    "expired",
  ].includes(String(state || ""));
}

function runtimeEvidenceReasonLabel(reason) {
  const value = String(reason || "").toLowerCase();
  if (!value) return "--";
  if (value.includes("deadline") || value.includes("expired") || value.includes("timeout")) return "生成超时";
  if (value.includes("missing") || value.includes("not_found")) return "所需录像或标注数据缺失";
  if (value.includes("duration")) return "录像时长需复核";
  if (value.includes("annotation") || value.includes("bbox")) return "画面标注需复核";
  if (value.includes("queue") || value.includes("busy")) return "当前任务较多，仍在排队";
  return "生成过程异常，请查看系统日志";
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

function isSourceAdapterContainer(item = {}) {
  const name = String(item?.name || "");
  return name.includes("source-adapter") || name.startsWith("video-analytics-source-");
}

function runtimeActualTopology() {
  const control = runtimeControl || {};
  const singleItems = Array.isArray(control.single) ? control.single : [];
  const singleCore = containerGroupCounts(singleItems.filter((item) => !isSourceAdapterContainer(item)));
  const dual = containerGroupCounts(control.dual);
  if (dual.running > 0 && singleCore.running > 0) {
    return {
      key: "mixed",
      label: "单路与双路混合运行",
      detail: `单路核心 ${singleCore.running}/${singleCore.total || "--"}，双路扩展 ${dual.running}/${dual.total || "--"}`,
    };
  }
  if (dual.running > 0) {
    if (dual.total > 0 && dual.running < dual.total) {
      return {
        key: "dual_partial",
        label: "双分支仅部分运行",
        detail: `双路扩展 ${dual.running}/${dual.total} 运行`,
      };
    }
    return {
      key: "dual",
      label: "双分支正在运行",
      detail: `双路扩展 ${dual.running}/${dual.total || "--"} 运行`,
    };
  }
  if (singleCore.running > 0) {
    return {
      key: "single",
      label: "基础单路正在运行",
      detail: `单路核心 ${singleCore.running}/${singleCore.total || "--"} 运行`,
    };
  }
  return {
    key: "stopped",
    label: "推理链路未运行",
    detail: "未检测到正在运行的单路或双路核心服务",
  };
}

function desiredRuntimeTopology() {
  const plan = runtimeTopology?.plan || {};
  const saved = runtimeTopology?.saved_config || {};
  const mode = String(plan.effective_mode || saved.topology_mode || "");
  return {
    mode,
    label: topologyModeLabel(mode),
    dual: plan.dual === true || mode.startsWith("dual"),
  };
}

function sourceConvergenceSummary() {
  const convergence = runtimeOverview?.supervisor?.source_convergence;
  if (!convergence || typeof convergence !== "object") {
    return {
      known: false,
      healthy: null,
      enabled: [],
      running: [],
      stopped: [],
      missing: [],
      stale: [],
    };
  }
  const sourceStates = Array.isArray(convergence.source_states) ? convergence.source_states : [];
  const enabled = sourceStates.filter((source) => source?.enabled !== false);
  const running = enabled.filter((source) => source?.running === true || source?.actual_state === "running");
  const stopped = enabled.filter((source) => !running.includes(source));
  return {
    known: typeof convergence.healthy === "boolean" || sourceStates.length > 0,
    healthy: convergence.healthy === true,
    enabled,
    running,
    stopped,
    missing: Array.isArray(convergence.missing_adapters) ? convergence.missing_adapters : [],
    stale: Array.isArray(convergence.stale_adapters) ? convergence.stale_adapters : [],
  };
}

function sourceConvergenceName(source) {
  const readable = sourceDisplayName(source?.source_id);
  return source?.camera_name || (readable !== "--" ? readable : "") || source?.source_id || "未命名摄像头";
}

function runtimeTopologyDrift() {
  const actual = runtimeActualTopology();
  const desired = desiredRuntimeTopology();
  if (!desired.mode) {
    return { present: false, actual, desired };
  }
  const present = desired.dual ? actual.key !== "dual" : actual.key !== "single";
  return { present, actual, desired };
}

function runtimePreflightLabel(preflight) {
  if (preflight?.ok === true) return "结构满足";
  if (preflight?.ok === false) return "结构不满足";
  return "未获取";
}

function renderRuntimeDecision() {
  if (!runtimeDecisionCopyEl || !runtimeDriftSummaryEl) return;
  const health = runtimeOverview?.health || {};
  const sources = sourceConvergenceSummary();
  const drift = runtimeTopologyDrift();
  const actual = drift.actual;
  const desired = drift.desired;
  const sourceNames = sources.stopped.slice(0, 3).map(sourceConvergenceName);
  const sourceIssue = sources.known && (
    sources.healthy === false || sources.stopped.length > 0 || sources.missing.length > 0 || sources.stale.length > 0
  );
  const loadFailures = Object.keys(runtimeLoadErrors || {});
  let copy = "实际运行态与保存的配置一致，可以按需查看性能和明细。";
  if (runtimeActionInFlight) {
    copy = `正在${runtimeActionLabel(runtimeActionInFlight)}；请等待状态刷新后确认是否已收敛。`;
  } else if (sourceIssue) {
    copy = `${sourceNames.length ? sourceNames.join("、") : "启用摄像头"} 尚未全部连接；请先恢复视频源，再判断是否需要切换运行方式。`;
  } else if (drift.present) {
    copy = `已保存设置为${desired.label}，当前实际为${actual.label}。如需保留当前状态，请更新保存设置；否则应用已保存设置。`;
  } else if (health.ok === false) {
    copy = "运行态仍有异常；请先查看下方故障摘要，不要直接执行整链路重启。";
  }
  if (loadFailures.length) {
    copy = `部分状态未加载（${loadFailures.map(runtimeLoadErrorLabel).join("、")}）；保留已获取的数据，刷新后再操作。`;
  }
  runtimeDecisionCopyEl.textContent = copy;

  const sourceState = sources.known
    ? `${sources.running.length}/${sources.enabled.length} 路运行`
    : "未获取";
  const sourceDetail = sourceIssue
    ? (sourceNames.length ? `待恢复：${sourceNames.join("、")}` : "部分摄像头连接状态异常")
    : "启用的摄像头均已连接";
  const driftState = !desired.mode
    ? "未获取保存设置"
    : (drift.present ? "配置与实际不一致" : "配置与实际一致");
  runtimeDriftSummaryEl.innerHTML =
    `<div class="runtime-drift-item ${["stopped", "mixed", "dual_partial"].includes(actual.key) ? "warn" : "ok"}">` +
      `<span>实际链路</span><strong>${escapeHtml(actual.label)}</strong><small>${escapeHtml(actual.detail)}</small>` +
    `</div>` +
    `<div class="runtime-drift-item ${sourceIssue ? "warn" : "ok"}">` +
      `<span>视频源</span><strong>${escapeHtml(sourceState)}</strong><small>${escapeHtml(sourceDetail)}</small>` +
    `</div>` +
    `<div class="runtime-drift-item ${drift.present ? "warn" : "ok"}">` +
      `<span>保存设置</span><strong>${escapeHtml(desired.label || "未获取")}</strong><small>${escapeHtml(driftState)}</small>` +
    `</div>`;

  if (recoverRuntimeSourcesBtn) {
    recoverRuntimeSourcesBtn.hidden = !sourceIssue;
    recoverRuntimeSourcesBtn.disabled = Boolean(runtimeActionInFlight);
    recoverRuntimeSourcesBtn.textContent = sourceNames.length
      ? `恢复 ${sourceNames.slice(0, 2).join("、")} 视频源`
      : "同步并恢复视频源";
  }
  if (applySavedRuntimeTopologyBtn) {
    applySavedRuntimeTopologyBtn.hidden = !drift.present;
    applySavedRuntimeTopologyBtn.disabled = Boolean(runtimeActionInFlight);
    applySavedRuntimeTopologyBtn.textContent = `应用已保存的${desired.label}`;
  }
}

function runtimeLoadErrorLabel(key) {
  const labels = {
    overview: "运行总览",
    control: "控制状态",
    performance: "性能配置",
    topology: "处理能力设置",
  };
  return labels[key] || key;
}

function renderRuntimeControlStatus() {
  if (!runtimeControlStatusEl) return;
  const control = runtimeControl || {};
  const management = containerGroupCounts(control.management);
  const actual = runtimeActualTopology();
  const sources = sourceConvergenceSummary();
  const desired = desiredRuntimeTopology();
  const sourceText = sources.known
    ? `${sources.running.length}/${sources.enabled.length} 运行`
    : "未获取";
  runtimeControlStatusEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>实际链路</span><strong>${escapeHtml(actual.label)}</strong></div>` +
      `<div><span>视频源</span><strong>${escapeHtml(sourceText)}</strong></div>` +
      `<div><span>保存目标</span><strong>${escapeHtml(desired.label || "未获取")}</strong></div>` +
      `<div><span>管理面</span><strong>${management.running}/${management.total || "--"} 运行</strong></div>` +
    `</div>`;
}

function renderRuntimePerformanceConfig() {
  if (!runtimePerformanceForm || !runtimePerformanceStatusEl || !runtimePerformanceDiffEl) return;
  const data = runtimePerformance || {};
  const config = data.saved_config || {};
  for (const [key, value] of Object.entries(config)) {
    const field = runtimePerformanceForm.elements[key];
    if (!field) continue;
    if (field.type === "checkbox") {
      field.checked = value === true;
    } else {
      field.value = value ?? "";
    }
  }
  const runtime = data.runtime || {};
  const forwarder = runtime.forwarder || {};
  const savant = runtime.savant || {};
  const diff = Array.isArray(data.diff) ? data.diff : [];
  const pending = diff.filter((item) => item.pending);
  runtimePerformanceStatusEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>保存来源</span><strong>${escapeHtml(performanceSourceLabel(data.source))}</strong></div>` +
      `<div><span>待应用</span><strong>${formatInteger(pending.length)}</strong></div>` +
      `<div><span>帧率控制</span><strong>${escapeHtml(containerStateLabel(forwarder))}</strong></div>` +
      `<div><span>识别服务</span><strong>${escapeHtml(containerStateLabel(savant))}</strong></div>` +
    `</div>`;
  renderRuntimePerformanceDiff(diff);
}

function performanceSourceLabel(source) {
  const labels = {
    file: "已保存",
    runtime: "运行中",
    default: "默认值",
  };
  return labels[source] || source || "--";
}

function containerStateLabel(container) {
  return containerStateText(container);
}

function renderRuntimePerformanceDiff(diff) {
  if (!runtimePerformanceDiffEl) return;
  if (!Array.isArray(diff) || !diff.length) {
    runtimePerformanceDiffEl.innerHTML = `<div class="empty-state">暂无性能配置。</div>`;
    return;
  }
  const rows = diff.map((item) =>
    `<tr class="${item.pending ? "warn-row" : ""}">` +
      `<td>${escapeHtml(item.label || item.key)}</td>` +
      `<td>${escapeHtml(item.target || "--")}</td>` +
      `<td>${escapeHtml(item.env || "--")}</td>` +
      `<td>${escapeHtml(formatPerformanceValue(item.saved_value))}</td>` +
      `<td>${escapeHtml(formatPerformanceValue(item.runtime_value))}</td>` +
      `<td>${item.pending ? "待应用" : "已生效"}</td>` +
    `</tr>`
  ).join("");
  runtimePerformanceDiffEl.innerHTML =
    `<table class="runtime-table runtime-performance-table">` +
      `<thead><tr><th>参数</th><th>作用范围</th><th>配置来源</th><th>保存值</th><th>运行值</th><th>状态</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
}

function formatPerformanceValue(value) {
  if (value === true) return "开启";
  if (value === false) return "关闭";
  if (value == null) return "--";
  return String(value);
}

function renderRuntimeTopologyConfig() {
  if (!runtimeTopologyForm || !runtimeTopologyStatusEl || !runtimeTopologyPlanEl) return;
  const data = runtimeTopology || {};
  const config = data.saved_config || {};
  for (const [key, value] of Object.entries(config)) {
    if (key === "branches" || key === "manual_assignments") continue;
    const control = runtimeTopologyForm.elements[key];
    if (control) control.value = value ?? "";
  }
  const branches = config.branches || {};
  for (const branchId of ["a", "b"]) {
    const branch = branches[branchId] || {};
    for (const [key, value] of Object.entries(branch)) {
      const control = runtimeTopologyForm.elements[`${branchId}.${key}`];
      if (control) control.value = value ?? "";
    }
  }
  const plan = data.plan || {};
  const preflight = data.preflight || {};
  const pipeline = data.runtime?.pipeline || {};
  const actual = runtimeActualTopology();
  runtimeTopologyStatusEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>运行方案</span><strong>${escapeHtml(QUICK_RUNTIME_PROFILE_META[config.runtime_profile]?.label || (config.runtime_profile === "custom" ? "自定义" : "未选择"))}</strong></div>` +
      `<div><span>链路范围</span><strong>${config.pipeline_mode === "full_evidence" ? "完整证据链" : "仅推理"}</strong></div>` +
      `<div><span>计划方式</span><strong>${escapeHtml(topologyModeLabel(plan.effective_mode || config.topology_mode))}</strong></div>` +
      `<div><span>实际链路</span><strong>${escapeHtml(actual.label)}</strong></div>` +
      `<div><span>启用摄像头</span><strong>${formatInteger(plan.enabled_source_count || 0)}</strong></div>` +
      `<div><span>启动检查</span><strong>${escapeHtml(runtimePreflightLabel(preflight))}</strong></div>` +
      `<div><span>完整链路</span><strong>${pipeline.ready === true ? "已就绪" : (pipeline.ready === false ? "未就绪" : "不适用")}</strong></div>` +
    `</div>` +
    `<div class="runtime-note">完整分析要求识别、人员轨迹、录像缓存和证据生成服务同时正常运行。</div>`;
  updateRuntimeTopologyFormVisibility({ plan });
  renderRuntimeTopologyAssignments(config, plan);
  renderRuntimeTopologyPlan(plan, data.runtime || {}, preflight);
}

function quickRuntimeRegisteredCameras() {
  return (cameras || []).filter((camera) => String(camera.source_id || "").trim());
}

function quickRuntimeSelectedCameras() {
  return quickRuntimeRegisteredCameras().filter((camera) => (
    quickRuntimeSelectedSourceIds.has(String(camera.source_id || ""))
  ));
}

function quickRuntimeExpectedCount(profile) {
  const preset = runtimeTopology?.profile_presets?.[profile] || {};
  return Number(preset.expected_source_count || QUICK_RUNTIME_PROFILE_META[profile]?.expected || 0);
}

function quickRuntimeBranchCounts(selectedCameras, strategy) {
  if (strategy !== "manual") {
    return {
      a: Math.ceil(selectedCameras.length / 2),
      b: Math.floor(selectedCameras.length / 2),
    };
  }
  return selectedCameras.reduce((counts, camera, index) => {
    const sourceId = String(camera.source_id || "");
    const branch = String(quickRuntimeManualAssignments[sourceId] || (index % 2 ? "b" : "a"));
    counts[branch === "b" ? "b" : "a"] += 1;
    return counts;
  }, { a: 0, b: 0 });
}

function renderQuickRuntimeAssignments(registeredCameras) {
  if (!quickRuntimeAssignmentsEl || !quickRuntimeShardStrategyEl) return;
  const manual = quickRuntimeShardStrategyEl.value === "manual";
  quickRuntimeAssignmentsEl.hidden = false;
  if (!registeredCameras.length) {
    quickRuntimeAssignmentsEl.innerHTML = `<div class="empty-state">请先添加摄像头并配置视频地址。</div>`;
    return;
  }
  const saved = runtimeTopology?.saved_config?.manual_assignments || {};
  const rows = registeredCameras.map((camera, index) => {
    const sourceId = String(camera.source_id || "");
    const selectedForRun = quickRuntimeSelectedSourceIds.has(sourceId);
    const selected = String(
      quickRuntimeManualAssignments[sourceId] || saved[sourceId] || (index % 2 ? "b" : "a")
    );
    quickRuntimeManualAssignments[sourceId] = selected;
    return `<tr>` +
      `<td class="runtime-camera-check"><input type="checkbox" data-quick-runtime-select="${escapeHtml(sourceId)}"${selectedForRun ? " checked" : ""} aria-label="选择 ${escapeHtml(camera.name || sourceId)}" /></td>` +
      `<td>${escapeHtml(camera.name || sourceId || "未命名摄像头")}</td>` +
      `<td>${escapeHtml(sourceId || "--")}</td>` +
      `<td>${manual
        ? `<select data-quick-runtime-source="${escapeHtml(sourceId)}"${selectedForRun ? "" : " disabled"}>` +
            `<option value="a"${selected === "a" ? " selected" : ""}>分支 A</option>` +
            `<option value="b"${selected === "b" ? " selected" : ""}>分支 B</option>` +
          `</select>`
        : `<span class="muted">系统自动均分</span>`}</td>` +
      `<td><span class="badge ${camera.enabled !== false ? "success" : "neutral"}">${camera.enabled !== false ? "当前已启用" : "当前未启用"}</span></td>` +
    `</tr>`;
  }).join("");
  quickRuntimeAssignmentsEl.innerHTML =
    `<table class="runtime-table">` +
      `<thead><tr><th>参与</th><th>摄像头</th><th>视频源 ID</th><th>目标分支</th><th>当前状态</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
  quickRuntimeAssignmentsEl.querySelectorAll("[data-quick-runtime-select]").forEach((control) => {
    control.addEventListener("change", () => {
      const sourceId = control.dataset.quickRuntimeSelect || "";
      if (control.checked) quickRuntimeSelectedSourceIds.add(sourceId);
      else quickRuntimeSelectedSourceIds.delete(sourceId);
      renderQuickRuntimeStart();
    });
  });
  quickRuntimeAssignmentsEl.querySelectorAll("[data-quick-runtime-source]").forEach((control) => {
    control.addEventListener("change", () => {
      quickRuntimeManualAssignments[control.dataset.quickRuntimeSource || ""] = control.value;
      renderQuickRuntimeStart();
    });
  });
}

function renderQuickRuntimeStart() {
  if (!quickRuntimeProfileEl || !quickRuntimeShardStrategyEl || !quickRuntimeCapacityEl) return;
  const saved = runtimeTopology?.saved_config || {};
  const savedProfile = String(saved.runtime_profile || "");
  if (!quickRuntimeProfileEl.dataset.initialized) {
    quickRuntimeProfileEl.value = QUICK_RUNTIME_PROFILE_META[savedProfile]
      ? savedProfile
      : "production_t4_40";
    quickRuntimeShardStrategyEl.value = saved.shard_strategy === "manual" ? "manual" : "balanced";
    quickRuntimeManualAssignments = { ...(saved.manual_assignments || {}) };
    quickRuntimeProfileEl.dataset.initialized = "true";
  }
  const registered = quickRuntimeRegisteredCameras();
  if (!quickRuntimeSelectionInitialized && registered.length) {
    quickRuntimeSelectedSourceIds = new Set(
      registered
        .filter((camera) => camera.enabled !== false)
        .map((camera) => String(camera.source_id || ""))
    );
    quickRuntimeSelectionInitialized = true;
  }
  const profile = quickRuntimeProfileEl.value;
  const strategy = quickRuntimeShardStrategyEl.value;
  const expected = quickRuntimeExpectedCount(profile);
  const selected = quickRuntimeSelectedCameras();
  const counts = quickRuntimeBranchCounts(selected, strategy);
  const countMatches = expected > 0 && selected.length === expected;
  quickRuntimeCapacityEl.className = `runtime-quick-capacity ${countMatches ? "ok" : "warn"}`;
  quickRuntimeCapacityEl.innerHTML = countMatches
    ? `已登记 <strong>${registered.length}</strong> 路，已选择 <strong>${selected.length}</strong> 路，计划 A/B：<strong>${counts.a}/${counts.b}</strong>`
    : `已登记 <strong>${registered.length}</strong> 路；方案需要 <strong>${expected}</strong> 路，当前已选择 <strong>${selected.length}</strong> 路`;
  if (quickRuntimeStartBtn) {
    const applyRunning = runtimeTopologyApplyStatus?.status === "running";
    quickRuntimeStartBtn.disabled = !countMatches || Boolean(runtimeActionInFlight) || applyRunning;
    quickRuntimeStartBtn.textContent = runtimeActionInFlight === "quick_full_start"
      ? "完整链路启动中…"
      : "启动完整双分支";
  }
  if (quickRuntimeStopBtn) {
    const dualRunning = runtimeTopology?.plan?.dual === true && (
      runtimeTopology?.runtime?.branches || []
    ).some((branch) => branch.containers?.savant?.running === true);
    quickRuntimeStopBtn.disabled = !dualRunning || Boolean(runtimeActionInFlight);
  }
  renderQuickRuntimeAssignments(registered);
}

function latencyText(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "--";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 120) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

function latencyClass(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "neutral";
  if (seconds <= 10) return "ok";
  if (seconds <= 60) return "warn";
  return "critical";
}

function renderRuntimeLatency() {
  if (!runtimeLatencySummaryEl || !runtimeLatencyDetailEl) return;
  const data = runtimeLatency || {};
  const annotation = data.annotation || {};
  const database = data.database || {};
  const branches = Array.isArray(data.branches) ? data.branches : [];
  const mediaLag = annotation.media_lag_s;
  const eventLag = database.event_media_lag_s;
  const bundleLag = database.bundle_event_lag_s;
  const indexAge = database.bundle_write_age_s;
  const queueText = branches.length
    ? branches.map((branch) => `${String(branch.branch_id || "").toUpperCase()} ${formatInteger(branch.queue_depth)}`).join(" / ")
    : "--";
  const maxQueue = branches.reduce((max, branch) => Math.max(max, Number(branch.queue_depth || 0)), 0);
  runtimeLatencySummaryEl.innerHTML =
    `<div class="runtime-latency-card ${latencyClass(mediaLag)}"><span>分析画面延迟</span><strong>${latencyText(mediaLag)}</strong><small>${escapeHtml(sourceDisplayName(annotation.source_id) || "最新分析画面")}</small></div>` +
    `<div class="runtime-latency-card ${latencyClass(eventLag)}"><span>告警延迟</span><strong>${latencyText(eventLag)}</strong><small>最近写入 ${latencyText(database.event_write_age_s)} 前</small></div>` +
    `<div class="runtime-latency-card ${latencyClass(bundleLag)}"><span>证据生成延迟</span><strong>${latencyText(bundleLag)}</strong><small>最近更新 ${latencyText(indexAge)} 前</small></div>` +
    `<div class="runtime-latency-card ${maxQueue >= 7000 ? "critical" : (maxQueue >= 2000 ? "warn" : "ok")}"><span>待分析画面</span><strong>${escapeHtml(queueText)}</strong><small>待生成 ${formatInteger(database.materialization_pending)} / 生成中 ${formatInteger(database.materializing)}</small></div>`;
  runtimeLatencyDetailEl.textContent =
    `最后刷新 ${formatDate(data.generated_at)}；画面延迟按最新分析画面的拍摄时间计算，不包含网页刷新耗时。`;
}

async function loadRuntimeLatency({ silent = false } = {}) {
  try {
    runtimeLatency = await request(`${API}/runtime/latency`);
    renderRuntimeLatency();
    return runtimeLatency;
  } catch (error) {
    if (!silent) showError(`延迟查询失败：${apiErrorMessage(error)}`);
    throw error;
  }
}

const RUNTIME_APPLY_PHASE_LABELS = {
  queued: "任务已排队",
  validation: "读取配置",
  preflight: "启动前预检",
  camera_selection: "锁定摄像头",
  pipeline_workers: "启动基础服务",
  branch_a: "初始化分支 A",
  branch_b: "初始化分支 B",
  sources: "启动摄像头",
  source_convergence: "等待摄像头收敛",
  rolling_cache_ready: "检查录像缓存",
  rolling_cache_prefill: "准备录像缓存",
  evidence_activation: "启用证据生成",
  complete: "启动完成",
  failed: "启动失败",
  stopped: "已停止",
};

function runtimeApplyStageGroup(phase) {
  if (["queued", "validation", "preflight", "camera_selection", "pipeline_workers"].includes(phase)) return "preflight";
  if (["branch_a", "branch_b"].includes(phase)) return "branches";
  if (["sources", "source_convergence"].includes(phase)) return "sources";
  if (["rolling_cache_ready", "rolling_cache_prefill"].includes(phase)) return "rolling";
  if (["evidence_activation", "complete"].includes(phase)) return "evidence";
  return "";
}

function runtimeApplyElapsedSeconds(status) {
  const started = Date.parse(status?.started_at || "");
  if (!Number.isFinite(started)) return 0;
  const finished = Date.parse(status?.finished_at || "");
  return Math.max(0, Math.round(((Number.isFinite(finished) ? finished : Date.now()) - started) / 1000));
}

function renderRuntimeTopologyApplyProgress() {
  if (!quickRuntimeProgressEl) return;
  const status = runtimeTopologyApplyStatus || {};
  const state = String(status.status || "idle");
  if (state === "idle") {
    quickRuntimeProgressEl.hidden = true;
    return;
  }
  const phase = String(status.phase || "queued");
  const percent = Math.max(0, Math.min(100, Number(status.percent || 0)));
  quickRuntimeProgressEl.hidden = false;
  quickRuntimeProgressEl.className = `runtime-start-progress ${state}`;
  if (quickRuntimeProgressPhaseEl) quickRuntimeProgressPhaseEl.textContent = RUNTIME_APPLY_PHASE_LABELS[phase] || phase;
  if (quickRuntimeProgressPercentEl) quickRuntimeProgressPercentEl.textContent = `${Math.round(percent)}%`;
  if (quickRuntimeProgressElapsedEl) quickRuntimeProgressElapsedEl.textContent = `已用时 ${runtimeApplyElapsedSeconds(status)} 秒`;
  if (quickRuntimeProgressTrackEl) quickRuntimeProgressTrackEl.setAttribute("aria-valuenow", String(Math.round(percent)));
  if (quickRuntimeProgressBarEl) quickRuntimeProgressBarEl.style.width = `${percent}%`;
  if (quickRuntimeProgressMessageEl) {
    quickRuntimeProgressMessageEl.textContent = state === "failed"
      ? "启动未完成，请检查摄像头连接和运行状态"
      : `${RUNTIME_APPLY_PHASE_LABELS[phase] || "正在启动分析服务"}，请稍候`;
  }

  const rolling = status.rolling_cache || null;
  if (quickRuntimeRollingProgressEl) quickRuntimeRollingProgressEl.hidden = !rolling;
  if (quickRuntimeRollingRemainingEl && rolling) {
    const remaining = Number(rolling.remaining_seconds || 0);
    quickRuntimeRollingRemainingEl.textContent = remaining > 0
      ? `还需约 ${remaining} 秒（总预热 ${Number(rolling.prefill_seconds || 0)} 秒）`
      : "预热完成";
  }

  const activeGroup = runtimeApplyStageGroup(phase);
  const groups = ["preflight", "branches", "sources", "rolling", "evidence"];
  const activeIndex = groups.indexOf(activeGroup);
  quickRuntimeProgressEl.querySelectorAll("[data-runtime-stage]").forEach((item) => {
    const index = groups.indexOf(item.dataset.runtimeStage || "");
    item.classList.toggle("active", index === activeIndex && state === "running");
    item.classList.toggle("done", state === "succeeded" || (activeIndex >= 0 && index < activeIndex));
    item.classList.toggle("failed", state === "failed" && index === Math.max(0, activeIndex));
  });

  runtimeActionInFlight = state === "running" ? "quick_full_start" : "";
  if (state === "running") ensureRuntimeApplyPolling();
  else stopRuntimeApplyPolling();
  renderQuickRuntimeStart();
}

function stopRuntimeApplyPolling() {
  if (runtimeApplyPollTimer) window.clearInterval(runtimeApplyPollTimer);
  runtimeApplyPollTimer = 0;
}

function ensureRuntimeApplyPolling() {
  if (runtimeApplyPollTimer) return;
  runtimeApplyPollTimer = window.setInterval(() => {
    pollRuntimeTopologyApplyStatus().catch((error) => {
      if (quickRuntimeProgressMessageEl) {
        quickRuntimeProgressMessageEl.textContent = `进度刷新失败，启动任务仍在后台运行：${apiErrorMessage(error)}`;
      }
    });
  }, 1000);
}

async function pollRuntimeTopologyApplyStatus() {
  const previous = runtimeTopologyApplyStatus?.status;
  runtimeTopologyApplyStatus = await request(`${API}/runtime/topology-config/apply-status`);
  renderRuntimeTopologyApplyProgress();
  const current = runtimeTopologyApplyStatus?.status;
  if (previous === "running" && current === "succeeded") {
    showSuccess("识别、轨迹和证据生成均已就绪");
    await loadRuntimeOverview({ silent: true });
  } else if (previous === "running" && current === "failed") {
    showError("完整分析启动失败，请检查摄像头连接和运行状态");
    await loadRuntimeOverview({ silent: true });
  }
  return runtimeTopologyApplyStatus;
}

function quickRuntimeRequestBody() {
  const profile = String(quickRuntimeProfileEl?.value || "production_t4_40");
  const strategy = String(quickRuntimeShardStrategyEl?.value || "balanced");
  const selected = quickRuntimeSelectedCameras();
  const manualAssignments = {};
  if (strategy === "manual") {
    selected.forEach((camera, index) => {
      const sourceId = String(camera.source_id || "");
      if (!sourceId) return;
      manualAssignments[sourceId] = String(
        quickRuntimeManualAssignments[sourceId] || (index % 2 ? "b" : "a")
      );
    });
  }
  return {
    runtime_profile: profile,
    pipeline_mode: "full_evidence",
    topology_mode: "dual_same_gpu",
    shard_strategy: strategy,
    manual_assignments: manualAssignments,
    source_ids: selected.map((camera) => String(camera.source_id || "")),
    disable_unselected: true,
  };
}

async function quickStartFullRuntime() {
  const body = quickRuntimeRequestBody();
  const expected = quickRuntimeExpectedCount(body.runtime_profile);
  const selected = quickRuntimeSelectedCameras();
  if (selected.length !== expected) {
    throw new Error(`所选方案需要选择 ${expected} 路摄像头，当前为 ${selected.length} 路`);
  }
  const counts = quickRuntimeBranchCounts(selected, body.shard_strategy);
  const profileLabel = QUICK_RUNTIME_PROFILE_META[body.runtime_profile]?.label || body.runtime_profile;
  if (!window.confirm(
    `确认启动${profileLabel}？\n本次选择 ${selected.length} 路，两个处理组分别为 ${counts.a}/${counts.b} 路。\n确认后将统一启用所选摄像头，并启动识别、人员轨迹和证据录像。`
  )) {
    return null;
  }
  clearMessages();
  runtimeActionInFlight = "quick_full_start";
  renderQuickRuntimeStart();
  if (quickRuntimeResultEl) {
    quickRuntimeResultEl.className = "runtime-quick-result";
    quickRuntimeResultEl.textContent = "正在保存分配并启动分析服务，首次启动可能需要数分钟，请勿重复点击。";
  }
  try {
    const { source_ids: sourceIds, disable_unselected: disableUnselected, ...topologyBody } = body;
    const saved = await request(`${API}/runtime/topology-config`, {
      method: "PUT",
      body: JSON.stringify(topologyBody),
    });
    runtimeTopology = saved || runtimeTopology;
    const applyJob = await request(`${API}/runtime/topology-config/apply-async`, {
      method: "POST",
      body: JSON.stringify({ source_ids: sourceIds, disable_unselected: disableUnselected }),
    });
    const selectedSet = new Set(sourceIds);
    cameras = cameras.map((camera) => ({
      ...camera,
      enabled: selectedSet.has(String(camera.source_id || "")),
    }));
    renderCameras();
    updateSummary();
    runtimeTopologyApplyStatus = applyJob;
    renderRuntimeTopologyApplyProgress();
    if (quickRuntimeResultEl) {
      quickRuntimeResultEl.className = "runtime-quick-result";
      quickRuntimeResultEl.textContent = "启动任务已进入后台；可以刷新或切换页面，进度不会丢失。";
    }
    showSuccess("完整链路启动任务已进入后台");
    return applyJob;
  } catch (error) {
    if (quickRuntimeResultEl) {
      quickRuntimeResultEl.className = "runtime-quick-result warn";
      quickRuntimeResultEl.textContent = `启动失败：${apiErrorMessage(error)}`;
    }
    throw error;
  } finally {
    if (runtimeTopologyApplyStatus?.status !== "running") runtimeActionInFlight = "";
    renderQuickRuntimeStart();
  }
}

function updateRuntimeTopologyFormVisibility({ plan = runtimeTopology?.plan || {} } = {}) {
  if (!runtimeTopologyForm) return;
  const requested = String(runtimeTopologyForm.elements.topology_mode?.value || "auto");
  const showBranchB = requested !== "single" && (requested !== "auto" || plan.dual === true);
  if (runtimeTopologyBranchBEl) {
    runtimeTopologyBranchBEl.hidden = !showBranchB;
  }
  if (runtimeTopologyBranchSettingsEl && !showBranchB && requested === "single") {
    runtimeTopologyBranchSettingsEl.open = false;
  }
  const profile = String(runtimeTopologyForm.elements.runtime_profile?.value || "custom");
  const presetLocked = profile !== "custom";
  for (const control of runtimeTopologyForm.elements) {
    if (!control?.name || control.name === "runtime_profile") continue;
    if (
      control.name === "pipeline_mode" ||
      control.name === "topology_mode" ||
      control.name === "streams_per_branch" ||
      control.name.startsWith("a.") ||
      control.name.startsWith("b.")
    ) {
      control.disabled = presetLocked;
    }
  }
}

function applyRuntimeTopologyPreset(profile) {
  if (!runtimeTopologyForm || profile === "custom") {
    updateRuntimeTopologyFormVisibility();
    return;
  }
  const preset = runtimeTopology?.profile_presets?.[profile];
  if (!preset) return;
  runtimeTopologyForm.elements.pipeline_mode.value = preset.pipeline_mode || "full_evidence";
  runtimeTopologyForm.elements.topology_mode.value = "dual_same_gpu";
  runtimeTopologyForm.elements.shard_strategy.value = "balanced";
  runtimeTopologyForm.elements.streams_per_branch.value = preset.streams_per_branch ?? "";
  for (const branchId of ["a", "b"]) {
    for (const [key, value] of Object.entries(preset.branch || {})) {
      const control = runtimeTopologyForm.elements[`${branchId}.${key}`];
      if (control) control.value = value ?? "";
    }
  }
  updateRuntimeTopologyFormVisibility();
}

function renderRuntimeTopologyAssignments(config, plan) {
  if (!runtimeTopologyAssignmentsEl) return;
  const strategy = String(config?.shard_strategy || "balanced");
  if (strategy !== "manual") {
    const labels = {
      balanced: "按数量均分",
      gpu_id: "按摄像头 GPU",
    };
    runtimeTopologyAssignmentsEl.innerHTML =
      `<div class="runtime-note">当前分片策略为“${escapeHtml(labels[strategy] || strategy)}”。仅在选择“手动覆盖”后显示每路摄像头的分支分配。</div>`;
    return;
  }
  const enabledCameras = (cameras || []).filter((camera) => camera.enabled !== false);
  if (!enabledCameras.length) {
    runtimeTopologyAssignmentsEl.innerHTML = `<div class="empty-state">暂无启用摄像头。</div>`;
    return;
  }
  const plannedBranch = new Map();
  for (const branch of plan?.branches || []) {
    const branchId = String(branch.branch_id || "");
    for (const sourceId of branch.source_ids || []) {
      plannedBranch.set(String(sourceId), branchId);
    }
  }
  const manual = config?.manual_assignments || {};
  const rows = enabledCameras.map((camera) => {
    const sourceId = String(camera.source_id || "");
    const selected = String(manual[sourceId] || plannedBranch.get(sourceId) || "a");
    const planned = plannedBranch.get(sourceId) || "--";
    return `<tr>` +
      `<td>${escapeHtml(camera.name || sourceId || "--")}</td>` +
      `<td>${escapeHtml(sourceId || "--")}</td>` +
      `<td>${escapeHtml(branchLabel(planned))}</td>` +
      `<td>` +
        `<select name="manual.${escapeHtml(sourceId)}" data-topology-branch="${escapeHtml(sourceId)}">` +
          `<option value="a"${selected === "a" ? " selected" : ""}>分支 A</option>` +
          `<option value="b"${selected === "b" ? " selected" : ""}>分支 B</option>` +
        `</select>` +
      `</td>` +
    `</tr>`;
  }).join("");
  runtimeTopologyAssignmentsEl.innerHTML =
    `<div class="runtime-subtitle">手动分配将覆盖自动分片结果。</div>` +
    `<table class="runtime-table runtime-topology-assignment-table">` +
      `<thead><tr><th>摄像头</th><th>视频源 ID</th><th>计划分支</th><th>手动分支</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
}

function renderRuntimeTopologyPlan(plan, runtime, preflight) {
  if (!runtimeTopologyPlanEl) return;
  const branches = Array.isArray(plan?.branches) ? plan.branches : [];
  if (!branches.length) {
    runtimeTopologyPlanEl.innerHTML = `<div class="empty-state">暂无拓扑计划。</div>`;
    return;
  }
  const runtimeBranches = new Map((runtime.branches || []).map((item) => [String(item.branch_id), item]));
  const rows = branches.map((branch) => {
    const branchId = String(branch.branch_id || "");
    const live = runtimeBranches.get(branchId) || {};
    const metrics = live.metrics || {};
    const savant = metrics.savant || {};
    const forwarder = metrics.forwarder || {};
    const sourceIds = Array.isArray(branch.source_ids) ? branch.source_ids : [];
    const containers = live.containers || {};
    const savantState = containerStateText(containers.savant);
    const forwarderState = containerStateText(containers.forwarder);
    const sendFailures = (forwarder.sources || []).reduce((sum, item) => sum + Number(item.savant_send_failures_total || 0), 0);
    return `<tr>` +
      `<td>${escapeHtml(branchLabel(branchId))}</td>` +
      `<td>${escapeHtml(String(branch.gpu_id ?? "--"))}</td>` +
      `<td>${formatInteger(branch.source_count || 0)}</td>` +
      `<td>${formatInteger(savant.global?.va_savant_sources_active ?? (savant.sources || []).length)}</td>` +
      `<td>${escapeHtml(savantState)}</td>` +
      `<td>${escapeHtml(forwarderState)}</td>` +
      `<td>${formatNumber(forwarder.global?.queue_depth)}</td>` +
      `<td>${formatInteger(sendFailures)}</td>` +
      `<td>${escapeHtml(sourceIds.slice(0, 6).map(sourceDisplayName).join(", "))}${sourceIds.length > 6 ? " ..." : ""}</td>` +
    `</tr>`;
  }).join("");
  const checks = (preflight?.checks || []).map((item) => {
    const detail = item.container || (
      item.expected_source_count != null
        ? `${formatInteger(item.source_count)}/${formatInteger(item.expected_source_count)}`
        : (item.gpu_id ?? "")
    );
    return (
    `<tr class="${item.ok ? "" : "warn-row"}">` +
      `<td>${escapeHtml(runtimeIssueLabel(item.name || ""))}</td>` +
      `<td>${item.ok ? "满足" : "不满足"}</td>` +
      `<td>${escapeHtml(detail)}</td>` +
    `</tr>`
    );
  }).join("");
  runtimeTopologyPlanEl.innerHTML =
    `<table class="runtime-table runtime-topology-table">` +
      `<thead><tr><th>分支</th><th>GPU</th><th>计划路数</th><th>推理路数</th><th>推理容器</th><th>限流容器</th><th>队列</th><th>累计发送失败</th><th>摄像头</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>` +
    `<table class="runtime-table runtime-topology-table">` +
      `<thead><tr><th>预检项</th><th>状态</th><th>对象</th></tr></thead>` +
      `<tbody>${checks || `<tr><td colspan="3">暂无预检项</td></tr>`}</tbody>` +
    `</table>`;
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
  const sourceConvergence = sourceConvergenceSummary();
  const topologyRuntime = runtimeTopology?.runtime || {};
  const topologyPlan = runtimeTopology?.plan || {};
  const topologyBranches = Array.isArray(topologyRuntime.branches) ? topologyRuntime.branches : [];
  const dualReady = topologyPlan.dual === true && topologyRuntime.pipeline?.ready === true;
  const dualSavantSources = topologyBranches.flatMap((branch) => branch.metrics?.savant?.sources || []);
  const dualForwarderSources = topologyBranches.flatMap((branch) => branch.metrics?.forwarder?.sources || []);
  const dualActiveSources = topologyBranches.reduce((sum, branch) => (
    sum + Number(branch.metrics?.savant?.global?.va_savant_sources_active || 0)
  ), 0);
  const dualQueueDepth = topologyBranches.reduce((sum, branch) => (
    sum + Number(branch.metrics?.forwarder?.global?.queue_depth || 0)
  ), 0);
  const effectiveHealthOk = dualReady ? true : health.ok;
  const issueText = dualReady
    ? `两个处理组均已就绪，共 ${dualActiveSources}/${Number(topologyPlan.enabled_source_count || 0)} 路，录像缓存正常`
    : (issues.length ? issues.map(runtimeIssueLabel).join("；") : "无已知异常");
  const effectiveForwarder = dualReady
    ? {
        available: true,
        global: { queue_depth: dualQueueDepth, running: topologyBranches.length },
        sources: dualForwarderSources,
      }
    : forwarder;

  runtimeHealthSummaryEl.innerHTML =
    `<div class="summary-card runtime-health-card ${effectiveHealthOk === true ? "ok" : (effectiveHealthOk === false ? "warn" : "")}">` +
      `<span>整体状态</span>` +
      `<strong>${dualReady ? "分析服务就绪" : statusText(health.ok)}</strong>` +
      `<small>${escapeHtml(issueText)}</small>` +
    `</div>` +
    `<div class="summary-card">` +
      `<span>推理指标</span>` +
      `<strong>${dualReady ? "两个处理组可用" : (metrics.available ? "可用" : "不可用")}</strong>` +
      `<small>${dualReady ? "处理组 A / 处理组 B" : "识别服务状态"}</small>` +
    `</div>` +
    `<div class="summary-card">` +
      `<span>当前活跃视频源</span>` +
      `<strong>${formatInteger(dualReady ? dualActiveSources : (metrics.sources_active ?? health.source_count))}</strong>` +
      `<small>${dualReady ? `计划 ${formatInteger(topologyPlan.enabled_source_count)} 路，两个处理组均已连接` : `状态中包含 ${formatInteger((metrics.sources || []).length)} 路记录`}</small>` +
    `</div>` +
    `<div class="summary-card">` +
      `<span>标注延迟</span>` +
      `<strong>${annotationAge == null ? "--" : `${formatInteger(annotationAge)}s`}</strong>` +
      `<small>${supervisorEnabled ? "自动监测已启用" : "自动监测未启用"}</small>` +
    `</div>`;

  runtimeSupervisorSummaryEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>识别处理组</span><strong>${dualReady ? "处理组 A / 处理组 B" : (supervisor.savant_container ? "基础处理组" : "--")}</strong></div>` +
      `<div><span>模块状态</span><strong>${dualReady ? "双分支运行中" : escapeHtml(supervisor.savant_module_status || "--")}</strong></div>` +
      `<div><span>冷却中</span><strong>${supervisor.in_cooldown ? "是" : "否"}</strong></div>` +
      `<div><span>视频源收敛</span><strong>${dualReady ? "已收敛" : (!sourceConvergence.known ? "未获取" : (sourceConvergence.healthy ? "已收敛" : "待恢复"))}</strong></div>` +
    `</div>` +
    `<div class="runtime-note">状态表可能保留历史摄像头记录；请以当前启用摄像头的连接状态为准。</div>`;

  renderRuntimeSourceTable(dualReady ? dualSavantSources : (metrics.sources || []));
  renderRuntimeForwarderTable(effectiveForwarder);
  renderRuntimeEvidenceTable(overview.evidence || {});
  renderRuntimeContainerTable(containers);
  renderRuntimeControlStatus();
  renderRuntimePerformanceConfig();
  renderRuntimeTopologyConfig();
  renderQuickRuntimeStart();
  renderRuntimeTopologyApplyProgress();
  renderRuntimeDecision();
}

function renderRuntimeSourceTable(sources) {
  if (!runtimeSourceTableEl) return;
  if (!sources.length) {
    runtimeSourceTableEl.innerHTML = `<div class="empty-state">暂无按摄像头拆分的性能指标。</div>`;
    return;
  }
  const rows = sources.map((source) => {
    const age = Number(source.last_frame_age_seconds);
    const pressureHistory = isPressureSourceId(source.source_id);
    const camera = cameraForSourceId(source.source_id);
    const unknownSource = !camera;
    const stale = pressureHistory || unknownSource || (Number.isFinite(age) && age > 30);
    const enabled = camera?.enabled !== false;
    const stateText = pressureHistory ? "压测历史" : (
      unknownSource ? "未登记/历史" : (stale ? "历史指标" : (enabled ? "最近有帧" : "摄像头已停用"))
    );
    return `<tr class="${stale ? "warn-row" : ""}">` +
      `<td>${sourceCellHtml(source.source_id)}</td>` +
      `<td><span class="runtime-state-chip ${stale ? "warn" : "ok"}">${escapeHtml(stateText)}</span></td>` +
      `<td>${formatNumber(source.effective_fps)}</td>` +
      `<td>${formatNumber(source.pose_stage_fps)}</td>` +
      `<td>${formatNumber(source.face_stage_fps)}</td>` +
      `<td>${formatNumber(source.adaface_embedding_fps)}</td>` +
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
        `<th>摄像头</th><th>运行判断</th><th>有效帧率</th><th>人体识别帧率</th><th>人脸检测帧率</th><th>人脸特征/秒</th><th>画面延迟</th><th>已处理帧</th>` +
        `<th>标注帧</th><th>人体</th><th>人脸</th><th>人脸特征</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
}

function renderRuntimeForwarderTable(forwarder) {
  if (!runtimeForwarderTableEl) return;
  const sources = forwarder.sources || [];
  if (!forwarder.available) {
    runtimeForwarderTableEl.innerHTML = `<div class="empty-state">暂无分析限流指标。</div>`;
    return;
  }
  const global = forwarder.global || {};
  const rows = sources.map((source) => {
    const seen = Number(source.frames_seen_total);
    const dropped = Number(source.frames_dropped_total);
    const dropRatio = Number.isFinite(seen) && seen > 0 && Number.isFinite(dropped)
      ? `${formatNumber((dropped / seen) * 100)}%`
      : "--";
    return `<tr>` +
      `<td>${sourceCellHtml(source.source_id)}</td>` +
      `<td>${formatInteger(source.frames_seen_total)}</td>` +
      `<td>${formatInteger(source.frames_forwarded_total)}</td>` +
      `<td>${formatInteger(source.frames_dropped_total)}</td>` +
      `<td>${dropRatio}</td>` +
      `<td>${formatInteger(source.savant_send_failures_total)}</td>` +
    `</tr>`;
  }).join("");
  runtimeForwarderTableEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>待分析画面</span><strong>${formatInteger(global.queue_depth)}</strong></div>` +
      `<div><span>运行状态</span><strong>${global.running === 1 ? "运行中" : "未运行"}</strong></div>` +
    `</div>` +
    `<div class="runtime-note">发送失败为本次运行的累计值，不单独代表当前故障；请结合待分析画面数量和刷新后的变化判断。</div>` +
    `<table class="runtime-table">` +
      `<thead><tr>` +
        `<th>摄像头</th><th>收到帧</th><th>转发帧</th><th>丢弃帧</th><th>丢弃比例</th><th>累计发送失败</th>` +
      `</tr></thead>` +
      `<tbody>${rows}</tbody>` +
    `</table>`;
}

function renderRuntimeEvidenceTable(evidence) {
  if (!runtimeEvidenceTableEl) return;
  if (!evidence.available) {
    runtimeEvidenceTableEl.innerHTML =
      `<div class="empty-state">暂时无法获取证据生成状态，请稍后刷新。</div>`;
    return;
  }
  const counts = evidence.state_counts || [];
  const recent = evidence.recent || [];
  const failures = evidence.recent_failures || [];
  const countHtml = counts.length
    ? counts.map((row) =>
        `<div><span>${escapeHtml(runtimeEvidenceStateLabel(row.state))}</span><strong>${formatInteger(row.count)}</strong></div>`
      ).join("")
    : `<div><span>最近 3 小时</span><strong>0</strong></div>`;
  const rows = recent.map((row) => {
    const warn = runtimeEvidenceStateIsFailure(row.evidence_state) ||
      runtimeEvidenceStateIsFailure(row.task_status) ||
      row.task_status && row.task_status !== row.evidence_state;
    return `<tr class="${warn ? "warn-row" : ""}">` +
      `<td>${escapeHtml(eventTypeLabel(row.event_type))}</td>` +
      `<td>${sourceCellHtml(row.source_id)}</td>` +
      `<td>${escapeHtml(runtimeEvidenceStateLabel(row.evidence_state))}</td>` +
      `<td>${escapeHtml(runtimeEvidenceStateLabel(row.task_status))}</td>` +
      `<td>${formatAge(row.age_seconds)}</td>` +
      `<td>${escapeHtml(runtimeEvidenceReasonLabel(row.evidence_reason))}</td>` +
    `</tr>`;
  }).join("");
  const failureRows = failures.slice(0, 5).map((row) =>
    `<li><strong>${escapeHtml(sourceDisplayName(row.source_id))}</strong> ` +
    `${escapeHtml(eventTypeLabel(row.event_type))} / ${escapeHtml(runtimeEvidenceReasonLabel(row.evidence_reason))}</li>`
  ).join("");
  runtimeEvidenceTableEl.innerHTML =
    `<div class="runtime-kv-grid evidence-state-grid">${countHtml}</div>` +
    (failureRows ? `<ul class="runtime-failure-list">${failureRows}</ul>` : "") +
    `<table class="runtime-table">` +
      `<thead><tr>` +
        `<th>告警类型</th><th>摄像头</th><th>证据状态</th><th>处理状态</th><th>耗时</th><th>说明</th>` +
      `</tr></thead>` +
      `<tbody>${rows || `<tr><td colspan="6">暂无最近证据事件。</td></tr>`}</tbody>` +
    `</table>`;
}

function renderRuntimeContainerTable(containers) {
  if (!runtimeContainerTableEl) return;
  const fixed = containers.fixed || {};
  const sourceSummary = sourceConvergenceSummary();
  const dynamicSourceRoute = sourceSummary.enabled.some((source) => source?.dynamic_source === true) &&
    !sourceSummary.enabled.some((source) => source?.compose_source === true);
  const rows = Object.entries(fixed).map(([role, item]) => {
    const legacyComposeSource = role === "compose_source" && dynamicSourceRoute;
    const warn = !legacyComposeSource && (
      (item.present && item.state !== "running") || item.restart_warning === true
    );
    const state = legacyComposeSource ? "动态源模式下未使用" : containerStateText(item);
    return `<tr class="${warn ? "warn-row" : ""}">` +
      `<td>${escapeHtml(containerRoleLabel(role))}</td>` +
      `<td>${escapeHtml(item.name || "--")}</td>` +
      `<td>${escapeHtml(state)}</td>` +
      `<td>${escapeHtml(healthText(item.health))}</td>` +
      `<td>${formatInteger(item.restart_count)}</td>` +
      `<td>${formatNumber(item.restart_rate_per_min)}</td>` +
    `</tr>`;
  });
  for (const source of containers.dynamic_sources || []) {
    const warn = source.state !== "running" || source.restart_warning === true;
    rows.push(
      `<tr class="${warn ? "warn-row" : ""}">` +
        `<td>${escapeHtml(containerRoleLabel("dynamic_source"))}</td>` +
        `<td>${escapeHtml(source.name || "--")}</td>` +
        `<td>${escapeHtml(containerStateText(source.state))}</td>` +
        `<td>--</td>` +
        `<td>${formatInteger(source.restart_count)}</td>` +
        `<td>${formatNumber(source.restart_rate_per_min)}</td>` +
      `</tr>`
    );
  }
  runtimeContainerTableEl.innerHTML =
    `<table class="runtime-table">` +
      `<thead><tr><th>服务</th><th>实例</th><th>状态</th><th>健康</th><th>重启次数</th><th>每分钟重启</th></tr></thead>` +
      `<tbody>${rows.join("")}</tbody>` +
    `</table>`;
}

function renderRuntimeApplyResult() {
  if (!runtimeApplyResultEl) return;
  if (!lastRuntimeApplyResult) {
    runtimeApplyResultEl.innerHTML = `<div class="muted">配置尚未应用。</div>`;
    return;
  }
  if (isRuntimeConfigSyncResult(lastRuntimeApplyResult)) {
    runtimeApplyResultEl.innerHTML =
      `<div class="runtime-kv-grid">` +
        `<div><span>配置同步</span><strong>已保存</strong></div>` +
        `<div><span>分析服务重启</span><strong>0</strong></div>` +
        `<div><span>视频源变更</span><strong>0</strong></div>` +
      `</div>` +
      `<div class="runtime-note">算法和区域配置已保存，分析服务无需重启。</div>`;
    return;
  }
  const selectedRows = latestApplyRows().filter((row) => (
    !selectedCameraId || String(row.camera_id || "") === String(selectedCameraId)
  ));
  const applied = selectedRows.filter((row) => row.runtime_apply_state === "applied");
  const skipped = selectedRows.filter((row) => row.runtime_apply_state === "skipped");
  const unsupported = selectedRows.filter((row) => row.runtime_apply_state === "unsupported");
  const sourceIds = (lastRuntimeApplyResult.source_ids || []).filter(Boolean);
  const warningRows = [...unsupported, ...skipped].slice(0, 8);
  const warningHtml = warningRows.length
    ? `<ul class="runtime-warning-list">` + warningRows.map((row) =>
        `<li><strong>${escapeHtml(row.rule_id || row.algorithm_id || "--")}</strong> ` +
        `未能应用，请检查区域和算法设置</li>`
      ).join("") + `</ul>`
    : `<div class="muted">当前摄像头的配置没有异常提示。</div>`;
  runtimeApplyResultEl.innerHTML =
    `<div class="runtime-kv-grid">` +
      `<div><span>摄像头</span><strong>${formatInteger((lastRuntimeApplyResult.camera_ids || []).length)}</strong></div>` +
      `<div><span>视频源</span><strong>${formatInteger(sourceIds.length)}</strong></div>` +
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

document.querySelectorAll(".context-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    activateTopView(btn.dataset.view, true);
  });
});

function setRuntimeAdvancedVisible(visible) {
  runtimeAdvancedVisible = Boolean(visible);
  RUNTIME_ADVANCED_PANE_IDS.forEach((id) => {
    const pane = document.getElementById(id);
    if (pane) pane.hidden = !runtimeAdvancedVisible;
  });
  if (toggleRuntimeAdvancedBtn) {
    toggleRuntimeAdvancedBtn.setAttribute("aria-expanded", runtimeAdvancedVisible ? "true" : "false");
    toggleRuntimeAdvancedBtn.textContent = runtimeAdvancedVisible ? "收起高级运维" : "高级运维";
  }
}

function activateTopView(view, updateHash = false) {
  const normalized = normalizedTopView(view);
  const primary = primaryTopView(normalized);
  persistTopView(normalized);
  document.querySelectorAll(".top-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === primary);
  });
  document.querySelectorAll(".workspace-context").forEach((context) => {
    context.hidden = context.dataset.primaryView !== primary;
  });
  document.querySelectorAll(".context-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === normalized);
  });
  document.getElementById("camera-view").hidden = normalized !== "cameras";
  document.getElementById("people-view").hidden = normalized !== "people";
  document.getElementById("trajectory-view").hidden = normalized !== "trajectory";
  document.getElementById("runtime-view").hidden = normalized !== "runtime";
  document.getElementById("evidence-view").hidden = normalized !== "evidence";
  document.getElementById("maintenance-view").hidden = normalized !== "maintenance";
  if (updateHash) {
    window.history.replaceState(null, "", `#${normalized}`);
  }
  if (normalized === "people") {
    loadPeople().catch((e) => showError(e.message));
  }
  if (normalized === "trajectory" && window.operatorTrajectory) {
    window.operatorTrajectory.init().catch((e) => showError(e.message));
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
    const data = await request(`${API}/evidence/bundles?limit=1&offset=0`);
    setEvidenceCount(data.total);
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
  if (!selectedStillVisible && pendingOpenPersonId && String(selectedPersonId) === String(pendingOpenPersonId)) {
    // Keep deep-linked selections even when the current list page/search does not contain that person.
  } else if (!selectedStillVisible) {
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

async function loadRuntimeOverview({ silent = false } = {}) {
  const results = await Promise.allSettled([
    request(`${API}/runtime/overview`),
    request(`${API}/runtime/control`),
    request(`${API}/runtime/performance-config`),
    request(`${API}/runtime/topology-config`),
    request(`${API}/runtime/topology-config/apply-status`),
    request(`${API}/runtime/latency`),
  ]);
  const keys = ["overview", "control", "performance", "topology", "topology_apply", "latency"];
  const values = [
    (value) => { runtimeOverview = value || {}; },
    (value) => { runtimeControl = value || {}; },
    (value) => { runtimePerformance = value || {}; },
    (value) => { runtimeTopology = value || {}; },
    (value) => { runtimeTopologyApplyStatus = value || {}; },
    (value) => { runtimeLatency = value || {}; },
  ];
  runtimeLoadErrors = {};
  results.forEach((result, index) => {
    const key = keys[index];
    if (result.status === "fulfilled") {
      values[index](result.value);
    } else {
      runtimeLoadErrors[key] = result.reason?.message || "请求失败";
    }
  });
  renderRuntimeOverview();
  renderRuntimeLatency();
  const failed = Object.keys(runtimeLoadErrors);
  if (failed.length) {
    const message = `运行状态部分刷新失败：${failed.map(runtimeLoadErrorLabel).join("、")}`;
    if (!silent) showError(message);
    setStatus("运行状态部分已刷新");
  } else {
    setStatus("运行状态已刷新");
  }
  return { ok: failed.length === 0, failed };
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
  const requestId = ++selectedPersonRequestId;
  selectedPersonId = String(personId);
  selectedPerson = null;
  personDetailEl.value = "";
  const data = await request(`${API}/people/${encodeURIComponent(personId)}`);
  if (requestId !== selectedPersonRequestId || String(personId) !== String(selectedPersonId)) {
    return;
  }
  selectedPerson = data.person || null;
  personDetailEl.value = JSON.stringify(data.person, null, 2);
  renderPersonProfile(data.person);
  renderGallery(data.gallery || []);
  if (appendSelectedPersonBtn) {
    appendSelectedPersonBtn.disabled = false;
  }
  if (findSelectedPersonBtn) {
    findSelectedPersonBtn.disabled = false;
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
  setStatus(`人员 ${personId} 已加载`);
}

function renderPersonProfile(person) {
  if (!personProfileEl || !person) return;
  personProfileEl.innerHTML =
    `<strong>${person.name || "未命名人员"}</strong>` +
    `<div class="muted">人员编号：${person.external_person_id || "未设置"}</div>` +
    `<div class="muted">状态：${person.is_active ? "有效" : "停用"}</div>` +
    `<div class="muted">${person.description || "暂无描述"}</div>`;
}

async function openPersonById(personId, options = {}) {
  const targetId = String(personId || "").trim();
  if (!targetId) {
    showError("未找到可跳转的人员 ID。");
    return;
  }
  pendingOpenPersonId = targetId;
  selectedPersonId = targetId;
  if (peopleSearchEl) peopleSearchEl.value = "";
  activateTopView("people", true);
  try {
    await selectPerson(targetId);
    if (options.openTrajectory && window.operatorTrajectory?.openForPerson) {
      await window.operatorTrajectory.openForPerson(targetId);
    }
  } finally {
    pendingOpenPersonId = "";
  }
}

window.operatorPeople = {
  openPersonById,
};

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
  clearFaceRegistrationFiles();
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
    const selectedFiles = Array.from(faceRegistrationForm.elements.images?.files || []);
    if (!selectedFiles.length) {
      throw new Error("请先选择至少一张人脸图片");
    }
    const fd = prepareFaceRegistrationFormData();
    const result = await request(`${API}/people/register-faces`, {
      method: "POST",
      body: fd,
    });
    faceRegistrationResultEl.value = JSON.stringify(result, null, 2);
    const registeredCount = Number(result.registered_count || 0);
    const failedCount = Number(result.failed_count || 0);
    const itemRows = (result.items || []).map((item) => {
      const succeeded = item.status === "REGISTERED";
      const detail = succeeded
        ? `已登记${item.is_primary ? "（主图）" : ""}`
        : `${item.error_code || "FAILED"}${item.error_message ? `：${item.error_message}` : ""}`;
      return `<div class="gallery-meta-row"><strong>${escapeHtml(item.filename || "图片")}</strong>：${escapeHtml(detail)}</div>`;
    }).join("");
    if (faceRegistrationSummaryEl) {
      faceRegistrationSummaryEl.innerHTML =
        `<strong>${registeredCount ? "批量人脸注册完成" : "批量人脸注册未成功"}</strong>` +
        `<div class="muted">人员编号：${escapeHtml(result.external_person_id || "-")}</div>` +
        `<div class="muted">姓名：${escapeHtml(result.name || "-")}</div>` +
        `<div class="muted">成功 ${registeredCount} 张，失败 ${failedCount} 张</div>` +
        itemRows;
    }
    if (!registeredCount) {
      showError("没有图片通过注册校验，请查看逐张结果");
      return;
    }
    selectedPersonId = String(result.person_id || "");
    showSuccess(
      failedCount
        ? `已注册 ${registeredCount} 张，${failedCount} 张未通过`
        : `已注册 ${registeredCount} 张人脸图片`
    );
    clearFaceRegistrationFiles();
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

function setRuntimeButtonsBusy(buttons, busy) {
  for (const button of buttons) {
    if (button) button.disabled = busy;
  }
}

function runtimeDestructiveButtons() {
  return [
    startSingleRuntimeBtn,
    stopSingleRuntimeBtn,
    restartSingleRuntimeBtn,
    stopDualRuntimeBtn,
    quickRuntimeStopBtn,
    recoverRuntimeSourcesBtn,
    applySavedRuntimeTopologyBtn,
    applyRuntimePerformanceBtn,
    applyRuntimeTopologyBtn,
  ];
}

function setRuntimeDestructiveBusy(action, busy) {
  runtimeActionInFlight = busy ? action : "";
  setRuntimeButtonsBusy(runtimeDestructiveButtons(), busy);
  renderRuntimeDecision();
}

function runtimeActionLabel(action) {
  const labels = {
    source_recovery: "恢复视频源",
    topology_apply: "应用处理设置",
    performance_apply: "应用性能配置",
    single_start: "启动基础单路链路",
    single_stop: "停止基础单路链路",
    single_restart: "重启基础单路链路",
    dual_stop: "停止双路扩展",
  };
  return labels[action] || "执行运行操作";
}

function runtimePerformanceFormBody() {
  if (!runtimePerformanceForm) return {};
  const fields = runtimePerformance?.fields || [];
  const body = {};
  for (const field of fields) {
    const key = field.key;
    const control = runtimePerformanceForm.elements[key];
    if (!key || !control) continue;
    if (field.kind === "bool" || control.type === "checkbox") {
      body[key] = control.checked === true;
    } else if (field.kind === "int") {
      body[key] = asInt(control.value, field.default ?? 0);
    } else {
      body[key] = String(control.value || "").trim();
    }
  }
  return body;
}

async function saveRuntimePerformanceConfig({ apply = false } = {}) {
  clearMessages();
  const body = runtimePerformanceFormBody();
  if (apply && !window.confirm(
    "确认保存并应用性能配置？相关分析服务会短暂重启；如果仍有证据正在生成，系统会自动阻止本次操作。"
  )) {
    setStatus("性能配置未保存");
    return null;
  }
  if (apply) {
    setRuntimeButtonsBusy([saveRuntimePerformanceBtn], true);
    setRuntimeDestructiveBusy("performance_apply", true);
  } else {
    setRuntimeButtonsBusy([saveRuntimePerformanceBtn, applyRuntimePerformanceBtn], true);
  }
  try {
    const saved = await request(`${API}/runtime/performance-config`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    runtimePerformance = saved || {};
    renderRuntimePerformanceConfig();
    if (!apply) {
      showSuccess("性能配置已保存");
      setStatus("性能配置已保存");
      return saved;
    }
    const applied = await request(`${API}/runtime/performance-config/apply`, { method: "POST" });
    runtimePerformance = applied.status || runtimePerformance;
    renderRuntimePerformanceConfig();
    showSuccess(runtimePerformanceApplyMessage(applied));
    await loadRuntimeOverview({ silent: true });
    return applied;
  } finally {
    if (apply) {
      setRuntimeButtonsBusy([saveRuntimePerformanceBtn], false);
      setRuntimeDestructiveBusy("", false);
    } else {
      setRuntimeButtonsBusy([saveRuntimePerformanceBtn, applyRuntimePerformanceBtn], false);
    }
  }
}

function runtimePerformanceApplyMessage(data) {
  if (!data?.changed) return "性能配置已保存，运行中配置无需变更";
  const actions = Array.isArray(data.actions) ? data.actions : [];
  const recreated = actions.filter((item) => item.action === "recreated").map((item) => item.container);
  return `性能配置已应用，${recreated.length} 个相关服务已更新`;
}

function runtimeTopologyFormBody() {
  if (!runtimeTopologyForm) return {};
  const branchKeys = [
    "gpu_id",
    "savant_batch_size",
    "pose_batch_size",
    "face_detector_batch_size",
    "face_embedding_batch_size",
    "face_infer_interval",
    "max_parallel_streams",
    "analysis_fps",
    "analysis_min_fps",
    "savant_max_fps",
    "savant_min_fps",
    "batched_push_timeout",
  ];
  const body = {
    runtime_profile: runtimeTopologyForm.elements.runtime_profile?.value || "custom",
    pipeline_mode: runtimeTopologyForm.elements.pipeline_mode?.value || "inference_only",
    topology_mode: runtimeTopologyForm.elements.topology_mode?.value || "auto",
    shard_strategy: runtimeTopologyForm.elements.shard_strategy?.value || "balanced",
    streams_per_branch: asInt(runtimeTopologyForm.elements.streams_per_branch?.value, 30),
    branches: { a: {}, b: {} },
    manual_assignments: {},
  };
  if (body.shard_strategy !== "manual") {
    body.manual_assignments = {
      ...(runtimeTopology?.saved_config?.manual_assignments || {}),
    };
  }
  for (const branchId of ["a", "b"]) {
    for (const key of branchKeys) {
      const control = runtimeTopologyForm.elements[`${branchId}.${key}`];
      if (!control) continue;
      if (control.type === "number") {
        body.branches[branchId][key] = asInt(control.value, 0);
      } else {
        body.branches[branchId][key] = String(control.value || "").trim();
      }
    }
  }
  if (body.shard_strategy === "manual") {
    for (const control of runtimeTopologyForm.querySelectorAll("[data-topology-branch]")) {
      const sourceId = control.dataset.topologyBranch || "";
      const branchId = String(control.value || "").trim();
      if (sourceId && ["a", "b"].includes(branchId)) {
        body.manual_assignments[sourceId] = branchId;
      }
    }
  }
  return body;
}

async function saveRuntimeTopologyConfig({ apply = false } = {}) {
  clearMessages();
  const body = runtimeTopologyFormBody();
  if (apply && !window.confirm(
    "确认保存并应用处理能力设置？识别、人员轨迹和证据录像服务将按新设置切换；如果仍有证据正在生成，系统会自动阻止本次操作。"
  )) {
    setStatus("处理能力设置未保存");
    return null;
  }
  if (apply) {
    setRuntimeButtonsBusy([saveRuntimeTopologyBtn], true);
    setRuntimeDestructiveBusy("topology_apply", true);
  } else {
    setRuntimeButtonsBusy([saveRuntimeTopologyBtn, applyRuntimeTopologyBtn], true);
  }
  try {
    const saved = await request(`${API}/runtime/topology-config`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    runtimeTopology = saved || {};
    renderRuntimeTopologyConfig();
    if (!apply) {
      showSuccess("处理能力设置已保存");
      setStatus("处理能力设置已保存");
      return saved;
    }
    const applied = await request(`${API}/runtime/topology-config/apply`, { method: "POST" });
    runtimeTopology = applied.status || runtimeTopology;
    renderRuntimeTopologyConfig();
    showSuccess(runtimeTopologyApplyMessage(applied));
    await loadRuntimeOverview({ silent: true });
    return applied;
  } finally {
    if (apply) {
      setRuntimeButtonsBusy([saveRuntimeTopologyBtn], false);
      setRuntimeDestructiveBusy("", false);
    } else {
      setRuntimeButtonsBusy([saveRuntimeTopologyBtn, applyRuntimeTopologyBtn], false);
    }
  }
}

function runtimeTopologyApplyMessage(data) {
  const mode = data?.mode || data?.status?.plan?.effective_mode || "--";
  const actions = Array.isArray(data?.actions) ? data.actions.length : 0;
  const sources = Array.isArray(data?.source_lifecycle) ? data.source_lifecycle.length : 0;
  const pipeline = data?.pipeline_convergence || {};
  const pipelineText = pipeline.mode === "full_evidence"
    ? (pipeline.ready ? "，完整分析服务已就绪" : "，完整分析服务仍在启动")
    : "";
  return `处理设置已应用：${topologyModeLabel(mode)}，更新 ${actions} 个服务、${sources} 路摄像头${pipelineText}`;
}

async function applySavedRuntimeTopology() {
  const desired = desiredRuntimeTopology();
  if (!desired.mode) {
    throw new Error("尚未获取已保存的处理设置，请先刷新状态");
  }
  const confirmText = `确认应用已保存的${desired.label}？识别、人员轨迹和证据录像服务将按保存设置切换；正在生成的证据会受到保护。`;
  if (!window.confirm(confirmText)) {
    return null;
  }
  clearMessages();
  setRuntimeDestructiveBusy("topology_apply", true);
  try {
    const applied = await request(`${API}/runtime/topology-config/apply`, { method: "POST" });
    runtimeTopology = applied.status || runtimeTopology;
    await loadRuntimeOverview({ silent: true });
    showSuccess(runtimeTopologyApplyMessage(applied));
    return applied;
  } finally {
    setRuntimeDestructiveBusy("", false);
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
    dual_stop: "摄像头采集与识别已停止，已产生的证据会继续完成",
  };
  const label = labels[data?.runtime_action] || "运行控制命令已发送";
  return `${label}：成功 ${ok} 个，缺失 ${missing} 个，失败 ${failed} 个`;
}

async function runRuntimeControlAction(path, action, confirmText = "") {
  if (confirmText && !window.confirm(confirmText)) {
    return null;
  }
  clearMessages();
  setRuntimeDestructiveBusy(action, true);
  try {
    const data = await request(path, { method: "POST" });
    runtimeControl = data.status || runtimeControl;
    renderRuntimeControlStatus();
    showSuccess(runtimeControlActionMessage(data));
    await loadRuntimeOverview({ silent: true });
    return data;
  } finally {
    setRuntimeDestructiveBusy("", false);
  }
}

async function startSingleRuntime() {
  return runRuntimeControlAction(
    `${API}/runtime/control/single/start`,
    "single_start",
    "确认启动基础分析服务？系统将启动摄像头、识别、告警、人员轨迹和证据生成。若需要多路完整分析，请使用上方快速启动。"
  );
}

async function stopSingleRuntime() {
  return runRuntimeControlAction(
    `${API}/runtime/control/single/stop`,
    "single_stop",
    "确认停止基础单路链路？视频源、推理、事件处理和证据生成都会中断；8090 操作台会保持在线。"
  );
}

async function restartSingleRuntime() {
  return runRuntimeControlAction(
    `${API}/runtime/control/single/restart`,
    "single_restart",
    "确认重启基础单路链路？视频源、推理和证据链路会短暂中断；生成中的证据任务会由服务端保护。"
  );
}

async function stopDualRuntime() {
  const result = await runRuntimeControlAction(
    `${API}/runtime/control/dual/stop`,
    "dual_stop",
    "确认停止当前完整分析？所有已启用摄像头和识别服务会停止；证据生成会继续运行一段时间，完成已经产生的证据。管理页面和已有证据不受影响。"
  );
  if (result) {
    cameras = cameras.map((camera) => ({ ...camera, enabled: false }));
    runtimeTopologyApplyStatus = result.apply_status || runtimeTopologyApplyStatus;
    updateSummary();
    renderCameras();
    renderRuntimeTopologyApplyProgress();
  }
  return result;
}

function runtimeApplyMessage(data) {
  const started = data.dynamic_sources_started || [];
  const composeStarted = data.compose_sources_started || [];
  const appliedRules = (data.applied_rules || []).length;
  const skippedRules = (data.skipped_rules || []).length;
  const unsupportedRules = (data.unsupported_rules || []).length;
  return `运行设置已应用：${composeStarted.length + started.length} 路摄像头已更新；规则已应用 ${appliedRules}、跳过 ${skippedRules}、未支持 ${unsupportedRules}`;
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

async function recoverRuntimeSources() {
  const sources = sourceConvergenceSummary();
  const names = sources.stopped.slice(0, 4).map(sourceConvergenceName);
  const target = names.length ? names.join("、") : "启用摄像头";
  if (!window.confirm(
    `确认同步并恢复 ${target} 的视频源？此操作会重新连接对应摄像头，不会主动重启识别服务。`
  )) {
    return null;
  }
  clearMessages();
  setRuntimeDestructiveBusy("source_recovery", true);
  try {
    const data = await request(`${API}/cameras/runtime/sources/apply`, { method: "POST" });
    await loadRuntimeOverview({ silent: true });
    const after = sourceConvergenceSummary();
    const message = after.known && after.healthy
      ? `${sourceApplyMessage(data)}；视频源已收敛`
      : `${sourceApplyMessage(data)}；恢复命令已完成，视频源仍在收敛或需要检查状态`;
    showSuccess(message);
    return data;
  } finally {
    setRuntimeDestructiveBusy("", false);
  }
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

function runtimeConfigSyncMessage(data) {
  return "运行设置已同步，分析服务无需重启";
}

async function syncRuntimeConfig({ context = "" } = {}) {
  const data = await request(`${API}/cameras/runtime/config/sync`, { method: "POST" });
  lastRuntimeApplyResult = data;
  renderRuntimeApplyResult();
  renderQuickAlgorithmControls();
  if (selectedCameraId) {
    await loadSelectedRuntimeConfig(selectedCameraId);
  }
  const message = runtimeConfigSyncMessage(data);
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
  return `分析服务已安全重启：${composeStarted.length + started.length} 路摄像头、${workers.length} 个后台服务已恢复；规则已应用 ${appliedRules}、跳过 ${skippedRules}、未支持 ${unsupportedRules}`;
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
    await syncRuntimeConfig({ context });
  } catch (e) {
    showError(`${context}，但运行配置同步失败：${apiErrorMessage(e)}`);
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
  const exists = validZoneRows(config.zones).some((z) => zoneOptionId(z) === body.zone_id);
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
  if (!selectedCameraId || !zoneId) return;
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
  for (const control of card.querySelectorAll("[data-config-field]")) {
    const field = String(control.dataset.configField || "").trim();
    if (!field) continue;
    const kind = control.dataset.configKind || "string";
    const fallback = config[field];
    if (kind === "bool") {
      config[field] = control.checked === true;
    } else if (kind === "int") {
      config[field] = asInt(control.value, asInt(fallback, 0));
    } else if (kind === "float") {
      config[field] = asFloat(control.value, asFloat(fallback, 0));
    } else {
      config[field] = String(control.value || "").trim();
    }
  }
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
  await syncRuntimeConfig({
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
  await syncRuntimeConfig({
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
findSelectedPersonBtn?.addEventListener("click", () => {
  if (!selectedPersonId) {
    showError("请先选择人员。");
    return;
  }
  if (!window.operatorTrajectory?.openForPerson) {
    showError("轨迹页面尚未加载，请刷新页面后重试。");
    return;
  }
  window.operatorTrajectory.openForPerson(selectedPersonId)
    .catch((e) => showError(e.message));
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
    enabled: false,
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
faceRegistrationForm?.elements.images?.addEventListener("change", updateFaceRegistrationFileCount);
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
toggleRuntimeAdvancedBtn?.addEventListener("click", () => {
  setRuntimeAdvancedVisible(!runtimeAdvancedVisible);
});
quickRuntimeManageCamerasBtn?.addEventListener("click", () => {
  activateTopView("cameras", true);
  document.getElementById("camera-view")?.scrollIntoView({ behavior: "smooth", block: "start" });
});

quickRuntimeProfileEl?.addEventListener("change", () => {
  renderQuickRuntimeStart();
});
quickRuntimeShardStrategyEl?.addEventListener("change", () => {
  renderQuickRuntimeStart();
});
quickRuntimeSelectRequiredBtn?.addEventListener("click", () => {
  const expected = quickRuntimeExpectedCount(quickRuntimeProfileEl?.value || "production_t4_40");
  const registered = quickRuntimeRegisteredCameras();
  quickRuntimeSelectedSourceIds = new Set(
    registered.slice(0, expected).map((camera) => String(camera.source_id || ""))
  );
  quickRuntimeSelectionInitialized = true;
  renderQuickRuntimeStart();
});
quickRuntimeClearSelectionBtn?.addEventListener("click", () => {
  quickRuntimeSelectedSourceIds = new Set();
  quickRuntimeSelectionInitialized = true;
  renderQuickRuntimeStart();
});
quickRuntimeStartBtn?.addEventListener("click", () => {
  quickStartFullRuntime().catch((error) => showError(`完整链路启动失败：${apiErrorMessage(error)}`));
});
quickRuntimeStopBtn?.addEventListener("click", () => {
  stopDualRuntime().catch((error) => showError(`完整链路停止失败：${apiErrorMessage(error)}`));
});
refreshRuntimeLatencyBtn?.addEventListener("click", () => {
  loadRuntimeLatency().catch(() => {});
});
saveRuntimePerformanceBtn?.addEventListener("click", () => {
  saveRuntimePerformanceConfig().catch((e) => showError(`性能配置保存失败：${apiErrorMessage(e)}`));
});
applyRuntimePerformanceBtn?.addEventListener("click", () => {
  saveRuntimePerformanceConfig({ apply: true }).catch((e) => showError(`性能配置应用失败：${apiErrorMessage(e)}`));
});
saveRuntimeTopologyBtn?.addEventListener("click", () => {
  saveRuntimeTopologyConfig().catch((e) => showError(`处理能力设置保存失败：${apiErrorMessage(e)}`));
});
applyRuntimeTopologyBtn?.addEventListener("click", () => {
  saveRuntimeTopologyConfig({ apply: true }).catch((e) => showError(`处理能力设置应用失败：${apiErrorMessage(e)}`));
});
recoverRuntimeSourcesBtn?.addEventListener("click", () => {
  recoverRuntimeSources().catch((e) => showError(`视频源恢复失败：${apiErrorMessage(e)}`));
});
applySavedRuntimeTopologyBtn?.addEventListener("click", () => {
  applySavedRuntimeTopology().catch((e) => showError(`保存设置应用失败：${apiErrorMessage(e)}`));
});
runtimeTopologyForm?.elements.topology_mode?.addEventListener("change", () => {
  updateRuntimeTopologyFormVisibility();
});
runtimeTopologyForm?.elements.runtime_profile?.addEventListener("change", (event) => {
  applyRuntimeTopologyPreset(String(event.target?.value || "custom"));
});
runtimeTopologyForm?.elements.shard_strategy?.addEventListener("change", () => {
  const config = {
    ...(runtimeTopology?.saved_config || {}),
    shard_strategy: runtimeTopologyForm.elements.shard_strategy.value,
  };
  renderRuntimeTopologyAssignments(config, runtimeTopology?.plan || {});
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
window.setInterval(() => {
  const runtimeView = document.getElementById("runtime-view");
  if (runtimeView && !runtimeView.hidden) {
    loadRuntimeLatency({ silent: true }).catch(() => {});
  }
}, 5000);
