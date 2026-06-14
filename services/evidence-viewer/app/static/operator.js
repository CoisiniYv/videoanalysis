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
let algorithms = [];
let currentZones = [];
let currentRules = [];
let runtimeOverview = null;

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
const runtimeContainerTableEl = document.getElementById("runtime-container-table");
const refreshRuntimeOverviewBtn = document.getElementById("refresh-runtime-overview");
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
const peopleEl = document.getElementById("people");
const peopleSearchEl = document.getElementById("people-search");
const faceRegistrationForm = document.getElementById("face-registration-form");
const personProfileEl = document.getElementById("person-profile");
const personDetailEl = document.getElementById("person-detail");
const galleryEl = document.getElementById("gallery");
const faceRegistrationSummaryEl = document.getElementById("face-registration-summary");
const faceRegistrationResultEl = document.getElementById("face-registration-result");
const previewDeleteSelectedPersonBtn = document.getElementById("preview-delete-selected-person");
const THEME_STORAGE_KEY = "operator-theme";

/* ---- API URL display ---- */
apiUrlEl.textContent = window.location.origin + API;

/* ---- Templates (9 algorithm templates) ---- */
const templates = {
  "face.observation": {
    rule_id: "rule_face_observation",
    algorithm_id: "face.observation",
    enabled: true,
    config: {
      min_face_confidence: 0.6,
      min_face_size: 40,
      min_quality: 0.6,
      reid_min_interval_ms: 1000,
      retention_days: 30,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "face.watchlist": {
    rule_id: "rule_watchlist",
    algorithm_id: "face.watchlist",
    enabled: true,
    config: {
      threshold: 0.75,
      cooldown_s: 60,
      camera_scope: [],
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "face.live_search": {
    rule_id: "rule_live_search_config",
    algorithm_id: "face.live_search",
    enabled: false,
    config: {
      default_threshold: 0.75,
      default_expires_minutes: 60,
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
  "behavior.loitering": {
    rule_id: "rule_loitering",
    algorithm_id: "behavior.loitering",
    enabled: true,
    config: {
      zone_id: "perimeter",
      min_duration_s: 30,
      max_avg_speed_px_s: 25,
      max_motion_range_px: 120,
      cooldown_s: 60,
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
      zone_id: "plaza",
      min_person_count: 5,
      min_duration_s: 10,
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
      min_height_drop_ratio: 0.35,
      horizontal_pose_ratio: 1.4,
      min_static_s: 3,
      min_keypoint_confidence: 0.3,
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
      min_speed_px_s: 180,
      min_duration_ms: 800,
      cooldown_s: 30,
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
      min_chase_speed_px_s: 140,
      min_duration_ms: 1200,
      cooldown_s: 60,
    },
    evidence_policy: {
      snapshot_required: true,
      clip_required: true,
      pre_seconds: 5,
      post_seconds: 5,
    },
  },
  "behavior.wall_climb_suspicious": {
    rule_id: "rule_wall_climb_suspicious",
    algorithm_id: "behavior.wall_climb_suspicious",
    enabled: true,
    config: {
      line_id: "wall_line_01",
      direction: "outside_to_inside",
      min_crossing_height_change_px: 40,
      min_keypoint_confidence: 0.3,
      max_event_duration_ms: 4000,
      cooldown_s: 60,
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
  "behavior.crowd_gathering",
  "behavior.running",
  "behavior.chasing",
  "behavior.fall",
  "behavior.wall_climb_suspicious",
  "face.observation",
  "face.watchlist",
  "face.live_search",
];

const quickAlgorithmLabels = {
  "behavior.intrusion": "入侵",
  "behavior.loitering": "徘徊",
  "behavior.crowd_gathering": "聚集",
  "behavior.running": "奔跑",
  "behavior.chasing": "追逐",
  "behavior.fall": "跌倒",
  "behavior.wall_climb_suspicious": "翻越",
  "face.observation": "人脸观察",
  "face.watchlist": "名单命中",
  "face.live_search": "实时检索",
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

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
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
    throw new Error(body.error?.message || body.error?.detail || `HTTP ${response.status}`);
  }
  return body.data ?? body;
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

function ruleForAlgorithm(algorithmId) {
  return currentRules.find((rule) => (
    rule.algorithm_id === algorithmId ||
    rule.algorithm_type === algorithmId ||
    rule.rule_id === ruleIdForAlgorithm(algorithmId)
  ));
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

function renderZones(zones) {
  currentZones = zones || [];
  zonesEl.innerHTML = "";
  for (const z of currentZones) {
    const item = document.createElement("div");
    item.className = "zone-item";
    item.innerHTML =
      `<strong>${z.zone_id}</strong>` +
      `<div>${z.zone_type} | ${z.enabled ? "已启用" : "已停用"}</div>` +
      `<div class="muted">${z.zone_name || ""}</div>` +
      `<div class="item-actions">` +
        `<button class="sm" data-action="edit-zone" data-zone-id="${z.zone_id}">编辑</button>` +
        `<button class="sm danger" data-action="delete-zone" data-zone-id="${z.zone_id}">删除</button>` +
      `</div>`;
    item.querySelector('[data-action="edit-zone"]').addEventListener("click", (e) => {
      e.stopPropagation();
      zoneJson.value = JSON.stringify({
        zone_id: z.zone_id,
        zone_name: z.zone_name || "",
        zone_type: z.zone_type,
        coordinate_space: z.coordinate_space || "pixel",
        points: z.points || [],
        enabled: z.enabled !== false,
      }, null, 2);
    });
    item.querySelector('[data-action="delete-zone"]').addEventListener("click", (e) => {
      e.stopPropagation();
      deleteZone(z.zone_id);
    });
    zonesEl.appendChild(item);
  }
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
    const policy = rule.evidence_policy || {};
    const zoneText = rule.zone_id || rule.config?.zone_id || rule.config?.zone || "";
    const lineText = rule.line_id || rule.config?.line_id || "";
    item.innerHTML =
      `<strong>${rule.rule_id}</strong>` +
      `<div>${rule.algorithm_id} <span class="badge ${badgeClass}">${categoryText}</span></div>` +
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
  for (const definition of algorithms) {
    if (definition.algorithm_id?.startsWith("behavior.")) {
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
  renderQuickAlgorithmControls();
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

function renderQuickAlgorithmControls() {
  if (!algorithmControlsEl) return;
  if (!selectedCameraId) {
    algorithmControlsEl.innerHTML = `<div class="muted">请选择摄像头。</div>`;
    return;
  }
  const polygonZones = currentZones.filter((z) => z.zone_type === "polygon");
  const lineZones = currentZones.filter((z) => ["line", "direction_line"].includes(z.zone_type));
  algorithmControlsEl.innerHTML = "";
  for (const algorithmId of quickAlgorithmIds) {
    const rule = ruleForAlgorithm(algorithmId);
    const policy = evidencePolicyFor(rule, algorithmId);
    const selectedZone = rule?.zone_id || rule?.config?.zone_id || rule?.config?.zone || polygonZones[0]?.zone_id || "";
    const selectedLine = rule?.line_id || rule?.config?.line_id || lineZones[0]?.zone_id || "";
    const row = document.createElement("div");
    row.className = "algorithm-control-item";
    row.dataset.algorithmId = algorithmId;
    const zoneControl = algorithmNeedsZone(algorithmId)
      ? `<label>区域<select data-control="zone_id">` +
          `<option value="">未绑定</option>` +
          polygonZones.map((zone) => {
            const id = zone.zone_id || zone.zone_name;
            const selected = id === selectedZone ? " selected" : "";
            return `<option value="${escapeHtml(id)}"${selected}>${escapeHtml(zone.zone_name && zone.zone_name !== id ? `${zone.zone_name} (${id})` : id)}</option>`;
          }).join("") +
        `</select></label>`
      : "";
    const lineControl = algorithmNeedsLine(algorithmId)
      ? `<label>检测线<select data-control="line_id">` +
          `<option value="">未绑定</option>` +
          lineZones.map((zone) => {
            const id = zone.zone_id || zone.zone_name;
            const selected = id === selectedLine ? " selected" : "";
            return `<option value="${escapeHtml(id)}"${selected}>${escapeHtml(zone.zone_name && zone.zone_name !== id ? `${zone.zone_name} (${id})` : id)}</option>`;
          }).join("") +
        `</select></label>`
      : "";
    row.innerHTML =
      `<div class="algorithm-control-main">` +
        `<label class="inline algorithm-toggle">` +
          `<input data-control="enabled" type="checkbox" ${rule?.enabled !== false ? "checked" : ""} />` +
          `<span>${escapeHtml(algorithmLabel(algorithmId))}</span>` +
        `</label>` +
        `<span class="muted">${escapeHtml(algorithmId)}</span>` +
      `</div>` +
      zoneControl +
      lineControl +
      `<label>前录秒数<input data-control="pre_seconds" type="number" min="0" max="300" value="${Number(policy.pre_seconds ?? 5)}" /></label>` +
      `<label>后录秒数<input data-control="post_seconds" type="number" min="0" max="300" value="${Number(policy.post_seconds ?? 5)}" /></label>` +
      `<button class="sm" data-action="edit-quick-rule" type="button">高级</button>`;
    row.querySelector('[data-action="edit-quick-rule"]').addEventListener("click", () => {
      fillRuleForm(rule || defaultRuleForAlgorithm(algorithmId));
      switchCameraTab("rules");
      ruleForm?.scrollIntoView({ block: "start", behavior: "smooth" });
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

function renderRuntimeOverview() {
  if (!runtimeHealthSummaryEl || !runtimeSourceTableEl || !runtimeContainerTableEl) return;
  const overview = runtimeOverview || {};
  const metrics = overview.metrics || {};
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
  renderRuntimeContainerTable(containers);
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

function renderRuntimeContainerTable(containers) {
  if (!runtimeContainerTableEl) return;
  const fixed = containers.fixed || {};
  const rows = Object.entries(fixed).map(([role, item]) => (
    `<tr class="${item.present && item.state !== "running" ? "warn-row" : ""}">` +
      `<td>${escapeHtml(role)}</td>` +
      `<td>${escapeHtml(item.name || "--")}</td>` +
      `<td>${item.present ? escapeHtml(item.state || "--") : "missing"}</td>` +
      `<td>${escapeHtml(item.health || "--")}</td>` +
      `<td>${formatInteger(item.restart_count)}</td>` +
    `</tr>`
  ));
  for (const source of containers.dynamic_sources || []) {
    rows.push(
      `<tr class="${source.state !== "running" ? "warn-row" : ""}">` +
        `<td>dynamic_source</td>` +
        `<td>${escapeHtml(source.name || "--")}</td>` +
        `<td>${escapeHtml(source.state || "--")}</td>` +
        `<td>--</td>` +
        `<td>--</td>` +
      `</tr>`
    );
  }
  runtimeContainerTableEl.innerHTML =
    `<table class="runtime-table">` +
      `<thead><tr><th>role</th><th>container</th><th>state</th><th>health</th><th>restarts</th></tr></thead>` +
      `<tbody>${rows.join("")}</tbody>` +
    `</table>`;
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
  const normalized = ["people", "evidence", "maintenance", "runtime"].includes(view) ? view : "cameras";
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
  }
  if (normalized === "runtime") {
    loadRuntimeOverview().catch((e) => showError(e.message));
  }
  if (normalized === "maintenance" && window.operatorMaintenance) {
    window.operatorMaintenance.init().catch((e) => showError(e.message));
  }
}

function openMaintenanceWithRequest(request = {}) {
  activateTopView("maintenance", true);
  if (window.operatorMaintenance?.prepareDelete) {
    window.operatorMaintenance.prepareDelete(request).catch((e) => showError(e.message));
  }
}

/* ---- API operations ---- */

async function loadCameras() {
  clearMessages();
  if (!algorithms.length) {
    await loadAlgorithms();
  }
  const data = await request(`${API}/cameras`);
  cameras = Array.isArray(data) ? data : (data.cameras || []);
  if (!selectedCameraId && cameras[0]) selectedCameraId = cameras[0].id;
  renderCameras();
  if (selectedCameraId) await selectCamera(selectedCameraId);
  setStatus("就绪");
}

async function loadAlgorithms() {
  const data = await request(`${API}/algorithms`);
  algorithms = data.algorithms || [];
  renderAlgorithms();
  if (ruleAlgorithmEl && !ruleAlgorithmEl.value && algorithms[0]) {
    fillRuleForm(defaultRuleForAlgorithm(algorithms[0].algorithm_id));
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
  setStatus("人员就绪");
}

async function loadRuntimeOverview() {
  const data = await request(`${API}/runtime/overview`);
  runtimeOverview = data || {};
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
  personDetailEl.value = JSON.stringify(data.person, null, 2);
  renderPersonProfile(data.person);
  renderGallery(data.gallery || []);
  fillRegistrationForPerson(data.person);
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
    `<div class="muted">人员编号：${person.external_person_id || "未设置"}</div>` +
    `<div class="muted">状态：${person.is_active ? "有效" : "停用"}</div>` +
    `<div class="muted">${person.description || "暂无描述"}</div>`;
}

function fillRegistrationForPerson(person) {
  if (!person || !faceRegistrationForm) return;
  faceRegistrationForm.elements.person_id.value = person.person_id || "";
  faceRegistrationForm.elements.external_person_id.value = person.external_person_id || "";
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
      `<div class="gallery-meta-row">登记质量 ${row.quality ?? "-"}</div>` +
      `<div class="gallery-meta-row">` +
        `<span class="badge ${row.is_primary ? "success" : "neutral"}">${row.is_primary ? "主图" : "备选图"}</span> ` +
        `<span class="badge ${row.is_active ? "success" : "neutral"}">${row.is_active ? "有效" : "停用"}</span> ` +
      `</div>`;
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
    const fd = new FormData(faceRegistrationForm);
    if (!fd.get("person_id")) {
      fd.delete("person_id");
    }
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

async function selectCamera(cameraId) {
  clearMessages();
  selectedCameraId = cameraId;
  const data = await request(`${API}/cameras/${cameraId}/config`);
  fillCamera(data.camera);
  renderCameras();
  renderZones(data.zones || []);
  await loadAlgorithmRules(cameraId, data.rules || []);
  fullConfigEl.value = JSON.stringify(data, null, 2);
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

function runtimeApplyMessage(data) {
  const started = data.dynamic_sources_started || [];
  const composeStarted = data.compose_sources_started || [];
  const restarted = data.savant_restarted || "Savant";
  const replay = data.replay_restarted || "Replay";
  return `运行时已应用：${composeStarted.length} 个固定源、${started.length} 个动态源，${replay} / ${restarted} 已重启`;
}

function sourceApplyMessage(data) {
  const started = data.dynamic_sources_started || [];
  const recreated = data.dynamic_sources_recreated || [];
  const stopped = data.dynamic_sources_stopped || [];
  const kept = data.dynamic_sources_kept || [];
  return `摄像头源已应用：启动 ${started.length} 个、重建 ${recreated.length} 个、停止 ${stopped.length} 个、保持 ${kept.length} 个`;
}

async function applyCameraSources({ context = "" } = {}) {
  const data = await request(`${API}/cameras/runtime/sources/apply`, { method: "POST" });
  const message = sourceApplyMessage(data);
  showSuccess(context ? `${context}；${message}` : message);
  return data;
}

async function applyRuntime({ context = "" } = {}) {
  const data = await request(`${API}/cameras/runtime/apply`, { method: "POST" });
  const message = runtimeApplyMessage(data);
  showSuccess(context ? `${context}；${message}` : message);
  return data;
}

function runtimeRestartMessage(data) {
  const composeStarted = data.compose_sources_started || [];
  const started = data.dynamic_sources_started || [];
  const workers = data.workers_restarted || [];
  return `运行时已受控重启：${composeStarted.length} 个固定源、${started.length} 个动态源、${workers.length} 个 worker 已恢复`;
}

async function restartRuntime() {
  if (!window.confirm("确认受控重启推理与录像链路？8090 管理端会保持在线。")) {
    return null;
  }
  const data = await request(`${API}/cameras/runtime/restart`, { method: "POST" });
  showSuccess(runtimeRestartMessage(data));
  return data;
}

async function applyCameraSourcesAfterChange(context) {
  try {
    await applyCameraSources({ context });
  } catch (e) {
    showError(`${context}，但摄像头源应用失败：${e.message}`);
  }
}

async function applyRuntimeAfterChange(context) {
  try {
    await applyRuntime({ context });
  } catch (e) {
    showError(`${context}，但运行时应用失败：${e.message}`);
  }
}

async function saveCamera() {
  clearMessages();
  const camera = formToCamera();
  const exists = cameras.some((c) => c.id === camera.id);
  if (exists) {
    const { id, ...body } = camera;
    await request(`${API}/cameras/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  } else {
    await request(`${API}/cameras`, { method: "POST", body: JSON.stringify(camera) });
  }
  selectedCameraId = camera.id;
  showSuccess("摄像头已保存；配置并启用算法规则后才会产生告警和证据");
  await loadCameras();
  await applyCameraSourcesAfterChange("摄像头已保存");
}

async function setCameraEnabled(enabled) {
  clearMessages();
  const camera = formToCamera();
  if (!camera.id) return;
  await request(`${API}/cameras/${encodeURIComponent(camera.id)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  await loadCameras();
  await applyCameraSourcesAfterChange(`摄像头已${enabled ? "启用" : "停用"}`);
}

async function saveZone() {
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
  const path = exists
    ? `${API}/cameras/${selectedCameraId}/zones/${encodeURIComponent(body.zone_id)}`
    : `${API}/cameras/${selectedCameraId}/zones`;
  await request(path, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`区域 ${body.zone_id} 已保存`);
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
  const path = exists
    ? `${API}/cameras/${selectedCameraId}/algorithm-rules/${encodeURIComponent(ruleId)}`
    : `${API}/cameras/${selectedCameraId}/algorithm-rules`;
  await request(path, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`规则 ${ruleId} 已保存`);
}

async function deleteRule(ruleId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/rules/${encodeURIComponent(ruleId)}`, {
    method: "DELETE",
  });
  await selectCamera(selectedCameraId);
  await applyRuntimeAfterChange(`规则 ${ruleId} 已删除`);
}

async function setRuleEnabled(ruleId, enabled) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/algorithm-rules/${encodeURIComponent(ruleId)}/${enabled ? "enable" : "disable"}`, {
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

async function saveQuickAlgorithmControls() {
  if (!selectedCameraId) {
    showError("未选择摄像头");
    return;
  }
  clearMessages();
  const cards = Array.from(algorithmControlsEl?.querySelectorAll(".algorithm-control-item") || []);
  let savedCount = 0;
  for (const card of cards) {
    const { body, existing } = quickRuleBodyFromCard(card);
    if (!body.enabled && !existing) {
      continue;
    }
    const path = existing
      ? `${API}/cameras/${selectedCameraId}/algorithm-rules/${encodeURIComponent(existing.rule_id)}`
      : `${API}/cameras/${selectedCameraId}/algorithm-rules`;
    await request(path, {
      method: existing ? "PUT" : "POST",
      body: JSON.stringify(body),
    });
    savedCount += 1;
  }
  await selectCamera(selectedCameraId);
  await applyRuntime({ context: `${savedCount} 个算法配置已保存` });
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
document.getElementById("open-rules-panel").addEventListener("click", () => {
  if (!selectedCameraId && !cameraForm.elements.id.value) {
    showError("请先选择或保存摄像头");
    return;
  }
  clearMessages();
  switchCameraTab("rules");
  ruleForm?.scrollIntoView({ block: "start", behavior: "smooth" });
});
document.getElementById("open-recording-settings").addEventListener("click", () => {
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
  restartRuntime().catch((e) => showError(`运行时受控重启失败：${e.message}`));
});
themeToggleBtn?.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark", { persist: true });
});
saveQuickAlgorithmsBtn?.addEventListener("click", () => {
  saveQuickAlgorithmControls().catch((e) => showError(e.message));
});

document.getElementById("add-zone-polygon").addEventListener("click", () => {
  zoneJson.value = JSON.stringify({
    zone_id: "perimeter",
    zone_name: "周界区域",
    zone_type: "polygon",
    coordinate_space: "pixel",
    points: [[100, 300], [900, 300], [900, 700], [100, 700]],
    enabled: true,
  }, null, 2);
});

document.getElementById("add-zone-line").addEventListener("click", () => {
  zoneJson.value = JSON.stringify({
    zone_id: "tripwire_01",
    zone_name: "翻墙检测线",
    zone_type: "line",
    coordinate_space: "pixel",
    points: [[0, 400], [1920, 400]],
    enabled: true,
  }, null, 2);
});

document.getElementById("save-zone").addEventListener("click", () => {
  saveZone().catch((e) => showError(e.message));
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
previewDeleteSelectedPersonBtn?.addEventListener("click", () => {
  if (!selectedPersonId) {
    showError("未选择人员");
    return;
  }
  openMaintenanceWithRequest({ kind: "person", person_ids: [selectedPersonId] });
});
refreshRuntimeOverviewBtn?.addEventListener("click", () => {
  clearMessages();
  loadRuntimeOverview().catch((e) => showError(`运行状态刷新失败：${e.message}`));
});

document.querySelectorAll("[data-template]").forEach((button) => {
  button.addEventListener("click", () => {
    const template = templates[button.dataset.template];
    if (template) {
      fillRuleForm(template);
    }
  });
});

/* ---- Init ---- */
applyTheme(currentTheme());
loadCameras()
  .then(() => {
    if (window.location.hash === "#people") {
      activateTopView("people", false);
    } else if (window.location.hash === "#runtime") {
      activateTopView("runtime", false);
    } else if (window.location.hash === "#evidence") {
      activateTopView("evidence", false);
    } else if (window.location.hash === "#maintenance") {
      activateTopView("maintenance", false);
    }
  })
  .catch((e) => showError(e.message));
