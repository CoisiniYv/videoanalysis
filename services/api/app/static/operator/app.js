/* ------------------------------------------------------------------ */
/*  C1G.1b Operator Frontend — app.js                                 */
/*  Pure vanilla JS, no framework. Uses real API endpoints only.      */
/* ------------------------------------------------------------------ */

const API = "/api/v1";

/* ---- State ---- */
let cameras = [];
let selectedCameraId = "";

/* ---- DOM refs ---- */
const statusEl = document.getElementById("status");
const errorBox = document.getElementById("error-box");
const successBox = document.getElementById("success-box");
const apiUrlEl = document.getElementById("api-url");
const camerasEl = document.getElementById("cameras");
const zonesEl = document.getElementById("zones");
const rulesEl = document.getElementById("rules");
const cameraForm = document.getElementById("camera-form");
const fpsForm = document.getElementById("fps-form");
const alertForm = document.getElementById("alert-form");
const zoneJson = document.getElementById("zone-json");
const ruleJson = document.getElementById("rule-json");
const fullConfigEl = document.getElementById("full-config");

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
  },
  "face.live_search": {
    rule_id: "rule_live_search_config",
    algorithm_id: "face.live_search",
    enabled: false,
    config: {
      default_threshold: 0.75,
      default_expires_minutes: 60,
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
  },
};

/* ---- Helpers ---- */

function setStatus(text) {
  statusEl.textContent = text;
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.hidden = false;
  successBox.hidden = true;
  setStatus("Error");
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

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Invalid JSON response: ${text.slice(0, 200)}`);
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
    throw new Error(`Invalid JSON in ${label}: ${e.message}`);
  }
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
    const cooldown = camera.alert_policy?.global_alert_cooldown_s ?? "—";
    item.innerHTML =
      `<strong>${camera.id}</strong>` +
      `<div>${camera.name || ""}</div>` +
      `<div class="muted">${camera.enabled ? "enabled" : "disabled"} | ${camera.source_id || ""} | transport:${camera.rtsp_transport || "tcp"} | cooldown:${cooldown}s</div>`;
    item.addEventListener("click", () => selectCamera(camera.id));
    camerasEl.appendChild(item);
  }
}

function renderZones(zones) {
  zonesEl.innerHTML = "";
  for (const z of zones) {
    const item = document.createElement("div");
    item.className = "zone-item";
    item.innerHTML =
      `<strong>${z.zone_id}</strong>` +
      `<div>${z.zone_type} | ${z.enabled ? "enabled" : "disabled"}</div>` +
      `<div class="muted">${z.zone_name || ""}</div>` +
      `<div class="item-actions">` +
        `<button class="sm" data-action="edit-zone" data-zone-id="${z.zone_id}">Edit</button>` +
        `<button class="sm danger" data-action="delete-zone" data-zone-id="${z.zone_id}">Delete</button>` +
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
}

function renderRules(rules) {
  rulesEl.innerHTML = "";
  for (const rule of rules) {
    const item = document.createElement("div");
    item.className = "rule-item";
    const cat = rule.rule_category || (rule.is_alert_rule ? "alert" : "observation");
    const badgeClass = cat === "observation" ? "observation" : "alert";
    item.innerHTML =
      `<strong>${rule.rule_id}</strong>` +
      `<div>${rule.algorithm_id} <span class="badge ${badgeClass}">${cat}</span></div>` +
      `<div class="muted">${rule.enabled ? "enabled" : "disabled"}</div>` +
      `<div class="item-actions">` +
        `<button class="sm" data-action="edit-rule" data-rule-id="${rule.rule_id}">Edit</button>` +
        `<button class="sm" data-action="toggle-rule" data-rule-id="${rule.rule_id}" data-enabled="${rule.enabled}">${rule.enabled ? "Disable" : "Enable"}</button>` +
        `<button class="sm danger" data-action="delete-rule" data-rule-id="${rule.rule_id}">Delete</button>` +
      `</div>`;
    item.querySelector('[data-action="edit-rule"]').addEventListener("click", (e) => {
      e.stopPropagation();
      ruleJson.value = JSON.stringify({
        rule_id: rule.rule_id,
        algorithm_id: rule.algorithm_id,
        enabled: rule.enabled,
        config: rule.config || {},
      }, null, 2);
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
}

/* ---- Tab switching ---- */

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    document.getElementById("tab-zones").hidden = tab !== "zones";
    document.getElementById("tab-rules").hidden = tab !== "rules";
  });
});

/* ---- API operations ---- */

async function loadCameras() {
  clearMessages();
  const data = await request(`${API}/cameras`);
  cameras = Array.isArray(data) ? data : (data.cameras || []);
  if (!selectedCameraId && cameras[0]) selectedCameraId = cameras[0].id;
  renderCameras();
  if (selectedCameraId) await selectCamera(selectedCameraId);
  setStatus("Ready");
}

async function selectCamera(cameraId) {
  clearMessages();
  selectedCameraId = cameraId;
  const data = await request(`${API}/cameras/${cameraId}/config`);
  fillCamera(data.camera);
  renderCameras();
  renderZones(data.zones || []);
  renderRules(data.rules || []);
  fullConfigEl.value = JSON.stringify(data, null, 2);
  setStatus(`Camera: ${cameraId}`);
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
  showSuccess(`Camera ${camera.id} saved`);
  await loadCameras();
}

async function setCameraEnabled(enabled) {
  clearMessages();
  const camera = formToCamera();
  if (!camera.id) return;
  await request(`${API}/cameras/${encodeURIComponent(camera.id)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  showSuccess(`Camera ${camera.id} ${enabled ? "enabled" : "disabled"}`);
  await loadCameras();
}

async function saveZone() {
  if (!selectedCameraId) { showError("No camera selected"); return; }
  clearMessages();
  let body;
  try {
    body = parseJsonTextarea(zoneJson, "zone JSON");
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
  showSuccess(`Zone ${body.zone_id} saved`);
  await selectCamera(selectedCameraId);
}

async function deleteZone(zoneId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/zones/${encodeURIComponent(zoneId)}`, {
    method: "DELETE",
  });
  showSuccess(`Zone ${zoneId} deleted`);
  await selectCamera(selectedCameraId);
}

async function saveRule() {
  if (!selectedCameraId) { showError("No camera selected"); return; }
  clearMessages();
  let body;
  try {
    body = parseJsonTextarea(ruleJson, "rule JSON");
  } catch (e) {
    showError(e.message);
    return;
  }
  const config = await request(`${API}/cameras/${selectedCameraId}/config`);
  const exists = (config.rules || []).some((r) => r.rule_id === body.rule_id);
  const path = exists
    ? `${API}/cameras/${selectedCameraId}/rules/${encodeURIComponent(body.rule_id)}`
    : `${API}/cameras/${selectedCameraId}/rules`;
  await request(path, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  showSuccess(`Rule ${body.rule_id} saved`);
  await selectCamera(selectedCameraId);
}

async function deleteRule(ruleId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/rules/${encodeURIComponent(ruleId)}`, {
    method: "DELETE",
  });
  showSuccess(`Rule ${ruleId} deleted`);
  await selectCamera(selectedCameraId);
}

async function setRuleEnabled(ruleId, enabled) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/rules/${encodeURIComponent(ruleId)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  showSuccess(`Rule ${ruleId} ${enabled ? "enabled" : "disabled"}`);
  await selectCamera(selectedCameraId);
}

/* ---- Event listeners ---- */

document.getElementById("refresh-cameras").addEventListener("click", () => {
  loadCameras().catch((e) => showError(e.message));
});

document.getElementById("new-camera").addEventListener("click", () => {
  clearMessages();
  fillCamera({
    id: "",
    name: "",
    source_id: "",
    rtsp_url: "rtsp://",
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
  fullConfigEl.value = "";
  ruleJson.value = "";
  zoneJson.value = "";
  setStatus("New camera");
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

document.querySelectorAll("[data-template]").forEach((button) => {
  button.addEventListener("click", () => {
    const template = templates[button.dataset.template];
    if (template) {
      ruleJson.value = JSON.stringify(template, null, 2);
    }
  });
});

/* ---- Init ---- */
loadCameras().catch((e) => showError(e.message));
