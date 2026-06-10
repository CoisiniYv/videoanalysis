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

/* ---- DOM refs ---- */
const statusEl = document.getElementById("status");
const errorBox = document.getElementById("error-box");
const successBox = document.getElementById("success-box");
const apiUrlEl = document.getElementById("api-url");
const cameraCountEl = document.getElementById("camera-count");
const enabledCameraCountEl = document.getElementById("enabled-camera-count");
const peopleCountEl = document.getElementById("people-count");
const galleryCountEl = document.getElementById("gallery-count");
const evidenceCountEl = document.getElementById("evidence-count");
const camerasEl = document.getElementById("cameras");
const zonesEl = document.getElementById("zones");
const rulesEl = document.getElementById("rules");
const cameraForm = document.getElementById("camera-form");
const fpsForm = document.getElementById("fps-form");
const alertForm = document.getElementById("alert-form");
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
  zonesEl.innerHTML = "";
  for (const z of zones) {
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
}

function renderRules(rules) {
  rulesEl.innerHTML = "";
  for (const rule of rules) {
    const item = document.createElement("div");
    item.className = "rule-item";
    const cat = rule.rule_category || (rule.is_alert_rule ? "alert" : "observation");
    const badgeClass = cat === "observation" ? "observation" : "alert";
    const categoryText = cat === "observation" ? "观察" : cat === "alert" ? "告警" : "配置";
    item.innerHTML =
      `<strong>${rule.rule_id}</strong>` +
      `<div>${rule.algorithm_id} <span class="badge ${badgeClass}">${categoryText}</span></div>` +
      `<div class="muted">${rule.enabled ? "已启用" : "已停用"}</div>` +
      `<div class="item-actions">` +
        `<button class="sm" data-action="edit-rule" data-rule-id="${rule.rule_id}">编辑</button>` +
        `<button class="sm" data-action="toggle-rule" data-rule-id="${rule.rule_id}" data-enabled="${rule.enabled}">${rule.enabled ? "停用" : "启用"}</button>` +
        `<button class="sm danger" data-action="delete-rule" data-rule-id="${rule.rule_id}">删除</button>` +
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

document.querySelectorAll(".top-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    activateTopView(btn.dataset.view, true);
  });
});

function activateTopView(view, updateHash = false) {
  const normalized = ["people", "evidence"].includes(view) ? view : "cameras";
  document.querySelectorAll(".top-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === normalized);
  });
  document.getElementById("camera-view").hidden = normalized !== "cameras";
  document.getElementById("people-view").hidden = normalized !== "people";
  document.getElementById("evidence-view").hidden = normalized !== "evidence";
  if (updateHash) {
    window.history.replaceState(null, "", `#${normalized}`);
  }
  if (normalized === "people") {
    loadPeople().catch((e) => showError(e.message));
  }
  if (normalized === "evidence" && window.operatorEvidence) {
    window.operatorEvidence.init().catch((e) => showError(e.message));
  }
}

/* ---- API operations ---- */

async function loadCameras() {
  clearMessages();
  const data = await request(`${API}/cameras`);
  cameras = Array.isArray(data) ? data : (data.cameras || []);
  if (!selectedCameraId && cameras[0]) selectedCameraId = cameras[0].id;
  renderCameras();
  if (selectedCameraId) await selectCamera(selectedCameraId);
  setStatus("就绪");
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
  updateSummary();
  setStatus("人员就绪");
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
  renderRules(data.rules || []);
  fullConfigEl.value = JSON.stringify(data, null, 2);
  setStatus(data.camera?.name || "摄像头就绪");
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
  showSuccess("摄像头已保存");
  await loadCameras();
}

async function setCameraEnabled(enabled) {
  clearMessages();
  const camera = formToCamera();
  if (!camera.id) return;
  await request(`${API}/cameras/${encodeURIComponent(camera.id)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  showSuccess(`摄像头已${enabled ? "启用" : "停用"}`);
  await loadCameras();
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
  showSuccess(`区域 ${body.zone_id} 已保存`);
  await selectCamera(selectedCameraId);
}

async function deleteZone(zoneId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/zones/${encodeURIComponent(zoneId)}`, {
    method: "DELETE",
  });
  showSuccess(`区域 ${zoneId} 已删除`);
  await selectCamera(selectedCameraId);
}

async function saveRule() {
  if (!selectedCameraId) { showError("未选择摄像头"); return; }
  clearMessages();
  let body;
  try {
    body = parseJsonTextarea(ruleJson, "规则配置");
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
  showSuccess(`规则 ${body.rule_id} 已保存`);
  await selectCamera(selectedCameraId);
}

async function deleteRule(ruleId) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/rules/${encodeURIComponent(ruleId)}`, {
    method: "DELETE",
  });
  showSuccess(`规则 ${ruleId} 已删除`);
  await selectCamera(selectedCameraId);
}

async function setRuleEnabled(ruleId, enabled) {
  if (!selectedCameraId) return;
  clearMessages();
  await request(`${API}/cameras/${selectedCameraId}/rules/${encodeURIComponent(ruleId)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  showSuccess(`规则 ${ruleId} 已${enabled ? "启用" : "停用"}`);
  await selectCamera(selectedCameraId);
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
  fullConfigEl.value = "";
  ruleJson.value = "";
  zoneJson.value = "";
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
document.getElementById("submit-face-registration").addEventListener("click", () => {
  submitFaceRegistration().catch((e) => showError(e.message));
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
loadCameras()
  .then(() => {
    if (window.location.hash === "#people") {
      activateTopView("people", false);
    } else if (window.location.hash === "#evidence") {
      activateTopView("evidence", false);
    }
  })
  .catch((e) => showError(e.message));
