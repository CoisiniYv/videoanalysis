const api = "/api/v1";

let cameras = [];
let selectedCameraId = "";

const statusEl = document.getElementById("status");
const camerasEl = document.getElementById("cameras");
const zonesEl = document.getElementById("zones");
const rulesEl = document.getElementById("rules");
const cameraForm = document.getElementById("camera-form");
const zoneJson = document.getElementById("zone-json");
const ruleJson = document.getElementById("rule-json");

const templates = {
  "behavior.intrusion": {
    rule_id: "rule_intrusion",
    algorithm_id: "behavior.intrusion",
    enabled: true,
    config: { zone_id: "perimeter", min_inside_ms: 1000, cooldown_s: 30 },
  },
  "behavior.loitering": {
    rule_id: "rule_loitering",
    algorithm_id: "behavior.loitering",
    enabled: true,
    config: { zone_id: "perimeter", min_duration_s: 60, cooldown_s: 60 },
  },
  "behavior.crowd_gathering": {
    rule_id: "rule_crowd_gathering",
    algorithm_id: "behavior.crowd_gathering",
    enabled: true,
    config: { zone_id: "perimeter", min_person_count: 5, min_duration_s: 10, cooldown_s: 60 },
  },
  "behavior.fall": {
    rule_id: "rule_fall",
    algorithm_id: "behavior.fall",
    enabled: true,
    config: { min_down_ms: 1500, cooldown_s: 60 },
  },
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
    config: { threshold: 0.75, cooldown_s: 60, camera_scope: ["cam_c1e_rtsp_replay"] },
  },
  "face.live_search": {
    rule_id: "rule_live_search",
    algorithm_id: "face.live_search",
    enabled: false,
    config: { min_similarity: 0.75, active_jobs_only: true },
  },
};

function setStatus(text) {
  statusEl.textContent = text;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok || body.error) {
    throw new Error(body.error?.message || `HTTP ${response.status}`);
  }
  return body.data ?? body;
}

function formToCamera() {
  const fd = new FormData(cameraForm);
  return {
    id: fd.get("id"),
    name: fd.get("name"),
    source_id: fd.get("source_id"),
    rtsp_url: fd.get("rtsp_url"),
    site_id: fd.get("site_id") || null,
    location: fd.get("location") || null,
    gpu_id: Number(fd.get("gpu_id") || 0),
    enabled: fd.get("enabled") === "on",
    input_type: "rtsp",
    rtsp_transport: fd.get("rtsp_transport") || "tcp",
    fps_policy: { max_fps: "8/1", min_fps: "2/1" },
    alert_policy: {
      global_alert_cooldown_s: Number(fd.get("global_alert_cooldown_s") || 0),
      store_suppressed_events: true,
      suppress_record_request: true,
      critical_bypass: false,
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
  cameraForm.elements.rtsp_transport.value = camera.rtsp_transport || "tcp";
  cameraForm.elements.enabled.checked = camera.enabled !== false;
  cameraForm.elements.global_alert_cooldown_s.value =
    camera.alert_policy?.global_alert_cooldown_s ?? 30;
}

function renderCameras() {
  camerasEl.innerHTML = "";
  for (const camera of cameras) {
    const item = document.createElement("div");
    item.className = `camera-item ${camera.id === selectedCameraId ? "active" : ""}`;
    item.innerHTML = `<strong>${camera.id}</strong><div>${camera.name || ""}</div><div class="muted">${camera.enabled ? "enabled" : "disabled"} | ${camera.source_id || ""}</div>`;
    item.addEventListener("click", () => selectCamera(camera.id));
    camerasEl.appendChild(item);
  }
}

function renderZones(zones) {
  zonesEl.innerHTML = zones.map((z) => (
    `<div class="zone-item"><strong>${z.zone_id}</strong><div>${z.zone_type} | ${z.enabled ? "enabled" : "disabled"}</div></div>`
  )).join("");
}

function renderRules(rules) {
  rulesEl.innerHTML = "";
  for (const rule of rules) {
    const item = document.createElement("div");
    item.className = "rule-item";
    item.innerHTML = `<strong>${rule.rule_id}</strong><div>${rule.algorithm_id}</div><div class="muted">${rule.enabled ? "enabled" : "disabled"} | ${rule.rule_category}</div>`;
    item.addEventListener("click", () => {
      ruleJson.value = JSON.stringify({
        rule_id: rule.rule_id,
        algorithm_id: rule.algorithm_id,
        enabled: rule.enabled,
        config: rule.config || {},
      }, null, 2);
    });
    rulesEl.appendChild(item);
  }
}

async function loadCameras() {
  const data = await request(`${api}/cameras`);
  cameras = data.cameras || [];
  if (!selectedCameraId && cameras[0]) selectedCameraId = cameras[0].id;
  renderCameras();
  if (selectedCameraId) await selectCamera(selectedCameraId);
  setStatus("Ready");
}

async function selectCamera(cameraId) {
  selectedCameraId = cameraId;
  const data = await request(`${api}/cameras/${cameraId}/config`);
  fillCamera(data.camera);
  renderCameras();
  renderZones(data.zones || []);
  renderRules(data.rules || []);
}

async function saveCamera() {
  const camera = formToCamera();
  const exists = cameras.some((c) => c.id === camera.id);
  if (exists) {
    const { id, ...body } = camera;
    await request(`${api}/cameras/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  } else {
    await request(`${api}/cameras`, { method: "POST", body: JSON.stringify(camera) });
  }
  selectedCameraId = camera.id;
  await loadCameras();
}

async function setCameraEnabled(enabled) {
  const camera = formToCamera();
  if (!camera.id) return;
  await request(`${api}/cameras/${encodeURIComponent(camera.id)}/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  await loadCameras();
}

async function saveZone() {
  if (!selectedCameraId) return;
  const body = JSON.parse(zoneJson.value);
  const config = await request(`${api}/cameras/${selectedCameraId}/config`);
  const exists = (config.zones || []).some((z) => z.zone_id === body.zone_id);
  const path = exists
    ? `${api}/cameras/${selectedCameraId}/zones/${encodeURIComponent(body.zone_id)}`
    : `${api}/cameras/${selectedCameraId}/zones`;
  await request(path, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  await selectCamera(selectedCameraId);
}

async function saveRule() {
  if (!selectedCameraId) return;
  const body = JSON.parse(ruleJson.value);
  const config = await request(`${api}/cameras/${selectedCameraId}/config`);
  const exists = (config.rules || []).some((r) => r.rule_id === body.rule_id);
  const path = exists
    ? `${api}/cameras/${selectedCameraId}/rules/${encodeURIComponent(body.rule_id)}`
    : `${api}/cameras/${selectedCameraId}/rules`;
  await request(path, { method: exists ? "PUT" : "POST", body: JSON.stringify(body) });
  await selectCamera(selectedCameraId);
}

document.getElementById("new-camera").addEventListener("click", () => {
  fillCamera({
    id: "cam_c1g1_test",
    name: "C1G1 Test Camera",
    source_id: "c1e_rtsp_replay",
    rtsp_url: "rtsp://10.37.57.112:8554/live/1080movie",
    site_id: "test_site",
    location: "test_location",
    enabled: true,
    rtsp_transport: "tcp",
    alert_policy: { global_alert_cooldown_s: 30 },
  });
});

document.getElementById("save-camera").addEventListener("click", () => saveCamera().catch((e) => setStatus(e.message)));
document.getElementById("enable-camera").addEventListener("click", () => setCameraEnabled(true).catch((e) => setStatus(e.message)));
document.getElementById("disable-camera").addEventListener("click", () => setCameraEnabled(false).catch((e) => setStatus(e.message)));
document.getElementById("add-zone").addEventListener("click", () => {
  zoneJson.value = JSON.stringify({
    zone_id: "perimeter",
    zone_name: "周界区域",
    zone_type: "polygon",
    coordinate_space: "pixel",
    points: [[100, 300], [900, 300], [900, 700], [100, 700]],
    enabled: true,
  }, null, 2);
});
document.getElementById("save-zone").addEventListener("click", () => saveZone().catch((e) => setStatus(e.message)));
document.getElementById("save-rule").addEventListener("click", () => saveRule().catch((e) => setStatus(e.message)));

document.querySelectorAll("[data-template]").forEach((button) => {
  button.addEventListener("click", () => {
    const template = templates[button.dataset.template];
    ruleJson.value = JSON.stringify(template, null, 2);
  });
});

loadCameras().catch((e) => setStatus(e.message));
