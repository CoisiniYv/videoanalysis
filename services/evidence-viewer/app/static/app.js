"use strict";

const NS_PER_SECOND = 1000000000;
const ALERT_REDS = new Set(["#D50000", "#FF0000", "#E53935", "#FF1744"]);
const DEFAULT_SOURCE_WIDTH = 1920;
const DEFAULT_SOURCE_HEIGHT = 1080;
const DEFAULT_SHOW_PERSON_BOXES = true;
const DEFAULT_SHOW_UNKNOWN_FACES = true;
const FACE_OVERLAY_POLICY = "sparse_observation";
const ROLE_RENDER_WINDOW_MS = {
  behavior_event: 1500,
  person_context: 1000,
  matched_face: 500,
  unknown_face: 500,
  default: 500
};

const state = {
  bundles: [],
  selectedEventId: null,
  manifest: null,
  annotations: [],
  sinkRecords: [],
  preparedAnnotations: [],
  annotationSource: "auto",
  annotationPayload: null,
  firstVideoFramePts: null,
  sourceWidth: DEFAULT_SOURCE_WIDTH,
  sourceHeight: DEFAULT_SOURCE_HEIGHT,
  warnings: new Set(),
  timeOffsetFallbackUsed: false,
  frameDurationMs: null
};

const dom = {
  healthStatus: document.getElementById("healthStatus"),
  bundleCount: document.getElementById("bundleCount"),
  clipWarning: document.getElementById("clipWarning"),
  bundleList: document.getElementById("bundleList"),
  refreshBundles: document.getElementById("refreshBundles"),
  video: document.getElementById("video"),
  canvas: document.getElementById("overlay"),
  annotationSourceBanner: document.getElementById("annotationSourceBanner"),
  annotationSource: document.getElementById("annotationSource"),
  showPersons: document.getElementById("showPersons"),
  showMatched: document.getElementById("showMatched"),
  showUnknown: document.getElementById("showUnknown"),
  showLandmarks: document.getElementById("showLandmarks"),
  showLabels: document.getElementById("showLabels"),
  holdMs: document.getElementById("holdMs"),
  toleranceMs: document.getElementById("toleranceMs"),
  renderPolicy: document.getElementById("renderPolicy"),
  activePersonContext: document.getElementById("activePersonContext"),
  activeMatchedFaces: document.getElementById("activeMatchedFaces"),
  activeBehaviorEvents: document.getElementById("activeBehaviorEvents"),
  faceOverlayPolicy: document.getElementById("faceOverlayPolicy"),
  matchedFaceWindowMs: document.getElementById("matchedFaceWindowMs"),
  personContextWindowMs: document.getElementById("personContextWindowMs"),
  warningList: document.getElementById("warningList")
};

const ctx = dom.canvas.getContext("2d");

function numberOrNull(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function addWarning(message) {
  if (message) {
    state.warnings.add(message);
  }
  renderWarnings();
}

function annotationSourceLabel(source) {
  const labels = {
    auto: "Auto",
    sidecar: "Production Sidecar",
    sidecar_preview: "Preview Sidecar",
    legacy: "Legacy Debug",
    unavailable: "Auto unavailable"
  };
  return labels[source] || source || "unknown";
}

function annotationSourceKind(payload = {}) {
  if (payload.annotation_source_kind) {
    return payload.annotation_source_kind;
  }
  const kinds = {
    sidecar: "production_sidecar",
    sidecar_preview: "preview_debug",
    legacy: "legacy_debug",
    unavailable: "unavailable"
  };
  return kinds[payload.annotation_source] || "unavailable";
}

function updateAnnotationSourceBanner() {
  if (!dom.annotationSourceBanner) return;
  const payload = state.annotationPayload || {};
  const kind = annotationSourceKind(payload);
  dom.annotationSourceBanner.textContent = [
    `annotation_source=${kind}`,
    payload.production_ready === false ? "production_ready=false" : "",
    payload.reason ? `reason=${payload.reason}` : "",
    payload.fallback_used ? "fallback=true" : ""
  ].filter(Boolean).join(" | ");
  dom.annotationSourceBanner.className = `source-banner source-banner--${kind}`;
}

function applyAnnotationSourceLabels() {
  if (!dom.annotationSource) return;
  for (const option of dom.annotationSource.options) {
    option.textContent = annotationSourceLabel(option.value);
  }
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} returned ${response.status}`);
  }
  return response.json();
}

function filterValue(id) {
  return document.getElementById(id).value.trim();
}

function bundleQueryString() {
  const params = new URLSearchParams();
  const filters = {
    event_type: filterValue("filterEventType"),
    source_id: filterValue("filterSourceId"),
    camera_id: filterValue("filterCameraId"),
    person: filterValue("filterPerson"),
    clip_status: filterValue("filterClipStatus"),
    event_id: filterValue("filterEventId")
  };
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  params.set("limit", "200");
  return params.toString();
}

async function loadHealth() {
  try {
    const health = await fetchJson("/health");
    dom.healthStatus.textContent = `health: ${health.status}`;
  } catch (err) {
    dom.healthStatus.textContent = "health: degraded";
    addWarning(`health_check_failed:${err.message}`);
  }
}

async function loadBundles() {
  const data = await fetchJson(`/api/bundles?${bundleQueryString()}`);
  state.bundles = Array.isArray(data.bundles) ? data.bundles : [];
  dom.bundleCount.textContent = `bundles: ${state.bundles.length}`;
  renderBundleList();
  if (!state.selectedEventId && state.bundles.length) {
    await selectBundle(state.bundles[0].event_id);
  }
}

function renderBundleList() {
  dom.bundleList.innerHTML = "";
  if (!state.bundles.length) {
    const empty = document.createElement("div");
    empty.className = "bundle-sub";
    empty.textContent = "No evidence bundles found.";
    dom.bundleList.appendChild(empty);
    return;
  }
  for (const bundle of state.bundles) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "bundle-item";
    if (bundle.event_id === state.selectedEventId) {
      button.classList.add("active");
    }
    const main = document.createElement("span");
    main.className = "bundle-main";
    main.textContent = bundle.event_id || "unknown_event";
    const sub = document.createElement("span");
    sub.className = "bundle-sub";
    sub.textContent = [
      bundle.event_type || "event",
      bundle.source_id || "source",
      bundle.clip_status || "clip",
      `ann ${bundle.default_annotation_source || "none"}`,
      `faces ${bundle.matched_objects || 0}/${bundle.unknown_objects || 0}`
    ].join(" | ");
    button.append(main, sub);
    button.addEventListener("click", () => selectBundle(bundle.event_id));
    dom.bundleList.appendChild(button);
  }
}

async function selectBundle(eventId, options = {}) {
  const preserveVideo = Boolean(options.preserveVideo);
  const previousVideoSrc = dom.video.currentSrc || dom.video.src || "";
  const previousVideoTime = Number.isFinite(dom.video.currentTime) ? dom.video.currentTime : 0;
  const previousPaused = dom.video.paused;
  state.selectedEventId = eventId;
  state.warnings = new Set();
  renderBundleList();

  const annotationSource = dom.annotationSource?.value || state.annotationSource || "auto";
  state.annotationSource = annotationSource;
  const annotationParams = new URLSearchParams({ source: annotationSource });
  const [manifest, annotationsPayload, sinkPayload] = await Promise.all([
    fetchJson(`/api/bundles/${encodeURIComponent(eventId)}`),
    fetchJson(`/api/bundles/${encodeURIComponent(eventId)}/annotations?${annotationParams.toString()}`),
    fetchJson(`/api/bundles/${encodeURIComponent(eventId)}/sink-metadata`)
  ]);

  state.manifest = manifest;
  state.annotationPayload = annotationsPayload;
  state.annotations = annotationsPayload.records || annotationsPayload.annotations || [];
  state.sinkRecords = sinkPayload.records || [];
  for (const warning of [
    ...(manifest.warnings || []),
    ...(annotationsPayload.warnings || []),
    ...(sinkPayload.warnings || [])
  ]) {
    addWarning(warning);
  }
  if (annotationsPayload.legacy_warning) {
    addWarning(annotationsPayload.legacy_warning);
  }
  if (annotationsPayload.preview_warning) {
    addWarning(annotationsPayload.preview_warning);
  }

  const first = firstFrameWithPts(state.sinkRecords);
  state.firstVideoFramePts = first ? Number(first.pts) : null;
  state.frameDurationMs = inferFrameDurationMs(state.sinkRecords);
  state.sourceWidth = Number(first?.width || dom.video.videoWidth || DEFAULT_SOURCE_WIDTH);
  state.sourceHeight = Number(first?.height || dom.video.videoHeight || DEFAULT_SOURCE_HEIGHT);
  if (state.firstVideoFramePts === null) {
    addWarning("sink_metadata_first_pts_missing");
  }

  prepareAnnotations();
  const nextVideoSrc = manifest.raw_clip_url || "";
  if (!preserveVideo || previousVideoSrc !== new URL(nextVideoSrc, window.location.href).href) {
    dom.video.src = nextVideoSrc;
    dom.video.load();
  } else {
    dom.video.currentTime = previousVideoTime;
    if (!previousPaused) {
      dom.video.play().catch(err => addWarning(`video_resume_failed:${err.message}`));
    }
  }
  renderDetails();
  drawOverlay();
  renderWarnings();
}

function firstFrameWithPts(records) {
  return records.find(record => Number.isFinite(Number(record.pts))) || null;
}

function inferFrameDurationMs(records) {
  const ptsValues = (records || [])
    .map(record => numberOrNull(record.pts ?? record.frame_pts))
    .filter(value => value !== null)
    .sort((a, b) => a - b);
  const deltas = [];
  for (let index = 1; index < ptsValues.length; index += 1) {
    const delta = ptsValues[index] - ptsValues[index - 1];
    if (delta > 0) deltas.push(delta / 1000000);
  }
  if (!deltas.length) return null;
  deltas.sort((a, b) => a - b);
  const mid = Math.floor(deltas.length / 2);
  return deltas.length % 2 ? deltas[mid] : (deltas[mid - 1] + deltas[mid]) / 2;
}

function annotationTimeSeconds(annotation, firstVideoFramePts, frameDurationMs = null) {
  const tMs = numberOrNull(annotation.t_ms);
  const tS = numberOrNull(annotation.t_s);
  const framePts = numberOrNull(annotation.frame_pts);
  let framePtsTimeSec = null;
  if (framePts !== null && firstVideoFramePts !== null) {
    framePtsTimeSec = (framePts - firstVideoFramePts) / NS_PER_SECOND;
  }
  if (tMs !== null) {
    if (framePtsTimeSec !== null && Math.abs((tMs / 1000) - framePtsTimeSec) > 0.5) {
      addWarning("t_ms_frame_pts_disagreement");
    }
    return {
      timeSec: tMs / 1000,
      mode: "t_ms"
    };
  }
  if (tS !== null) {
    if (framePtsTimeSec !== null && Math.abs(tS - framePtsTimeSec) > 0.5) {
      addWarning("t_ms_frame_pts_disagreement");
    }
    return {
      timeSec: tS,
      mode: "t_s"
    };
  }
  const timeOffsetMs = numberOrNull(annotation.time_offset_ms);
  if (timeOffsetMs !== null) {
    return {
      timeSec: timeOffsetMs / 1000,
      mode: "time_offset_ms"
    };
  }
  const clipFrameIndex = numberOrNull(annotation.clip_frame_index);
  const safeFrameDurationMs = numberOrNull(annotation.clip_frame_duration_ms || annotation.frame_duration_ms || frameDurationMs);
  if (clipFrameIndex !== null && safeFrameDurationMs !== null && safeFrameDurationMs > 0) {
    return {
      timeSec: (clipFrameIndex * safeFrameDurationMs) / 1000,
      mode: "clip_frame_index"
    };
  }
  if (framePtsTimeSec !== null) {
    state.timeOffsetFallbackUsed = true;
    addWarning("frame_pts_fallback");
    return {
      timeSec: framePtsTimeSec,
      mode: "frame_pts_fallback"
    };
  }
  addWarning("annotation_missing_frame_pts_and_time_offset_ms");
  return { timeSec: null, mode: "missing_time" };
}

function prepareAnnotations() {
  state.timeOffsetFallbackUsed = false;
  state.preparedAnnotations = state.annotations
    .filter(line => line && line.displayable !== false)
    .map((line, index) => {
      const timing = annotationTimeSeconds(line, state.firstVideoFramePts, state.frameDurationMs);
      return {
        ...line,
        _index: index,
        _overlayTimeSec: timing.timeSec,
        _alignment: timing.mode,
        _framePts: numberOrNull(line.frame_pts)
      };
    })
    .filter(line => line._overlayTimeSec !== null)
    .sort((a, b) => a._overlayTimeSec - b._overlayTimeSec);
}

function normalizeBbox(bbox, sourceWidth, sourceHeight) {
  if (!bbox) {
    addWarning("bbox_missing");
    return null;
  }
  let format = String(bbox.format || "cxcywh").toLowerCase();
  let rawValues = null;
  if (Array.isArray(bbox.values)) {
    rawValues = bbox.values;
  } else if (Array.isArray(bbox.xyxy)) {
    rawValues = bbox.xyxy;
    format = "xyxy";
  } else if (Array.isArray(bbox.xywh)) {
    rawValues = bbox.xywh;
    format = "xywh";
  } else if (Array.isArray(bbox.cxcywh)) {
    rawValues = bbox.cxcywh;
    format = "cxcywh";
  }
  if (!rawValues || rawValues.length < 4) {
    addWarning("bbox_missing");
    return null;
  }
  const values = rawValues.slice(0, 4).map(Number);
  if (values.some(value => !Number.isFinite(value))) {
    addWarning("bbox_invalid_values");
    return null;
  }
  let x1;
  let y1;
  let x2;
  let y2;
  if (format.includes("cxcywh")) {
    const [cx, cy, w, h] = values;
    x1 = cx - w / 2;
    y1 = cy - h / 2;
    x2 = cx + w / 2;
    y2 = cy + h / 2;
  } else if (format.includes("xyxy")) {
    [x1, y1, x2, y2] = values;
  } else if (format === "xywh" || format.includes("xywh")) {
    const [x, y, w, h] = values;
    x1 = x;
    y1 = y;
    x2 = x + w;
    y2 = y + h;
  } else {
    addWarning(`bbox_unknown_format:${format}`);
    return null;
  }
  x1 = clamp(x1, 0, sourceWidth);
  y1 = clamp(y1, 0, sourceHeight);
  x2 = clamp(x2, 0, sourceWidth);
  y2 = clamp(y2, 0, sourceHeight);
  if (x2 <= x1 || y2 <= y1) {
    addWarning("bbox_empty_after_clamp");
    return null;
  }
  return { x1, y1, x2, y2 };
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function isMatchedObject(obj) {
  const identity = obj.identity || {};
  const label = obj.label || {};
  return Boolean(
    label.kind === "known_face" ||
    label.display_name ||
    label.external_person_id ||
    label.person_id !== null && label.person_id !== undefined ||
    identity.status === "matched" ||
    identity.match_status === "above_threshold" ||
    identity.external_person_id ||
    identity.person_id !== null && identity.person_id !== undefined
  );
}

function isLowSimilarityObject(obj) {
  const identity = obj.identity || {};
  return identity.status === "low_similarity_candidate" ||
    identity.match_status === "below_threshold" ||
    identity.match_status === "low_similarity_candidate";
}

function isBehaviorEventObject(obj) {
  const label = obj.label || {};
  const action = obj.action || {};
  return obj.object_type === "person" && (
    obj.annotation_role === "behavior_event" ||
    label.kind === "behavior_event" ||
    action.event_type === "intrusion" ||
    action.status === "event_triggered"
  );
}

function isPersonContextObject(obj) {
  const style = obj.style || {};
  const label = obj.label || {};
  return obj.object_type === "person" && (
    obj.annotation_role === "person_context" ||
    style.reason === "person_detection" ||
    label.kind === "person"
  );
}

function objectVisible(obj) {
  if (isBehaviorEventObject(obj)) return true;
  if (isPersonContextObject(obj)) return dom.showPersons.checked;
  if (isMatchedObject(obj)) return dom.showMatched.checked;
  return dom.showUnknown.checked;
}

function labelForObject(obj, line) {
  if (isBehaviorEventObject(obj)) {
    const label = obj.label || {};
    if (label.text) return String(label.text);
    const action = obj.action || {};
    const eventType = String(action.event_type || "Intrusion");
    if (eventType === "intrusion") return "Intrusion";
    return eventType.replace(/_/g, " ");
  }
  if (isPersonContextObject(obj)) {
    const trackId = obj.track_id || "";
    const detection = obj.detection || {};
    const confidence = Number(detection.confidence);
    const confidenceText = Number.isFinite(confidence) ? confidence.toFixed(2) : "";
    return ["Person", trackId ? `track ${trackId}` : "", confidenceText ? `conf ${confidenceText}` : ""]
      .filter(Boolean)
      .join(" | ");
  }
  const identity = obj.identity || {};
  const label = obj.label || {};
  const trackId = obj.track_id || "";
  const timestamp = line.timestamp_ms || "";
  if (!isMatchedObject(obj)) {
    return ["Unknown face", trackId ? `track ${trackId}` : "", timestamp ? `ts ${timestamp}` : ""]
      .filter(Boolean)
      .join(" | ");
  }
  const name = label.display_name || identity.display_name || label.external_person_id || identity.external_person_id || "Matched face";
  const similarity = Number(label.similarity ?? identity.similarity);
  const similarityText = Number.isFinite(similarity) ? similarity.toFixed(3) : "n/a";
  return [`${name} ${similarityText}`, trackId ? `track ${trackId}` : "", timestamp ? `ts ${timestamp}` : ""]
    .filter(Boolean)
    .join(" | ");
}

function styleForObject(obj) {
  const style = obj.style || {};
  if (isPersonContextObject(obj)) {
    const color = style.bbox_color || style.color || "#00C853";
    return {
      bboxColor: color,
      labelColor: style.label_color || color,
      lineWidth: Number(style.line_width || 2),
      reason: style.reason || "person_detection",
      priority: style.priority ?? "context"
    };
  }
  if (isBehaviorEventObject(obj)) {
    const color = style.bbox_color || style.color || "#FF6D00";
    const action = obj.action || {};
    return {
      bboxColor: color,
      labelColor: style.label_color || color,
      lineWidth: Number(style.line_width || 3),
      reason: style.reason || action.event_type || "behavior_event",
      priority: style.priority ?? "warning"
    };
  }
  if (isMatchedObject(obj)) {
    const color = style.bbox_color || style.color || "#D50000";
    return {
      bboxColor: color,
      labelColor: style.label_color || color,
      lineWidth: Number(style.line_width || 3),
      reason: style.reason || "identity_match",
      priority: style.priority ?? 50
    };
  }
  if (isLowSimilarityObject(obj)) {
    const styleColor = style.bbox_color && !ALERT_REDS.has(String(style.bbox_color).toUpperCase())
      ? style.bbox_color
      : "#FFD166";
    return {
      bboxColor: styleColor,
      labelColor: styleColor,
      lineWidth: Number(style.line_width || 2),
      reason: "low_similarity_candidate",
      priority: style.priority ?? 30
    };
  }
  let color = style.bbox_color || style.color || "#00B0FF";
  if (ALERT_REDS.has(String(color).toUpperCase())) {
    color = "#00B0FF";
    addWarning("unknown_style_overridden_from_event_alert");
  }
  return {
    bboxColor: color,
    labelColor: color,
    lineWidth: Number(style.line_width || 2),
    reason: "unknown_face",
    priority: style.priority ?? 10
  };
}

function objectsForLine(line) {
  if (!line) return [];
  const objects = Array.isArray(line.objects)
    ? line.objects.filter(obj => obj && typeof obj === "object")
    : [];
  const isTopLevelObject = line.record_type === "object_annotation" ||
    (!line.record_type && typeof line.object_type === "string" && line.bbox);
  if (isTopLevelObject) {
    const obj = {};
    for (const key of ["object_type", "object_id", "annotation_role", "track_id", "bbox", "identity", "label", "action", "style", "landmarks", "pose", "detection", "gate"]) {
      if (line[key] !== undefined) obj[key] = line[key];
    }
    if (Object.keys(obj).length > 0) {
      objects.push(obj);
    }
  }
  return objects;
}

function linesAtOverlayTime(overlayTimeSec) {
  return state.preparedAnnotations.filter(line => (
    Math.abs(line._overlayTimeSec - overlayTimeSec) <= 0.001
  ));
}

function lineRole(line) {
  const obj = objectsForLine(line)[0];
  if (!obj) return "default";
  if (isBehaviorEventObject(obj)) return "behavior_event";
  if (isPersonContextObject(obj)) return "person_context";
  if (isMatchedObject(obj)) return "matched_face";
  return "unknown_face";
}

function lineTrackKey(line) {
  const obj = objectsForLine(line)[0] || {};
  const type = obj.object_type || line.object_type || "obj";
  const id =
    obj.track_id ?? line.track_id ?? obj.object_id ?? line.object_id ?? line._index;
  return `${type}:${id}`;
}

function findActiveAnnotations(currentTime) {
  if (!state.preparedAnnotations.length) {
    return { line: null, lines: [], targetMs: Math.round(currentTime * 1000), mode: "none", matchedMs: null };
  }
  const userToleranceMs = Math.max(0, Number(dom.toleranceMs.value || 0));
  const userHoldMs = Math.max(0, Number(dom.holdMs.value || 0));
  const targetMs = Math.round(currentTime * 1000);
  const byTrack = new Map();

  for (const line of state.preparedAnnotations) {
    if (line._overlayTimeSec === null || line._overlayTimeSec === undefined) continue;
    const role = lineRole(line);
    const baseWindowMs = ROLE_RENDER_WINDOW_MS[role] || ROLE_RENDER_WINDOW_MS.default;
    const holdMs = baseWindowMs + userHoldMs;
    const aheadMs = baseWindowMs * 0.5 + userToleranceMs;
    const dtMs = (currentTime - line._overlayTimeSec) * 1000;
    if (dtMs < -aheadMs || dtMs > holdMs) continue;
    const key = lineTrackKey(line);
    const previous = byTrack.get(key);
    if (
      !previous ||
      Math.abs(line._overlayTimeSec - currentTime) <
        Math.abs(previous._overlayTimeSec - currentTime)
    ) {
      byTrack.set(key, line);
    }
  }

  const lines = [...byTrack.values()].sort(
    (a, b) => a._overlayTimeSec - b._overlayTimeSec
  );
  if (!lines.length) {
    return { line: null, lines: [], targetMs, mode: "none", matchedMs: null };
  }
  return {
    line: lines[0],
    lines,
    targetMs,
    mode: "per_track_hold",
    matchedMs: Math.round(lines[0]._overlayTimeSec * 1000)
  };
}

function resizeCanvas() {
  const videoRect = dom.video.getBoundingClientRect();
  const wrapRect = dom.video.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;

  // Position canvas over the video's actual displayed rectangle,
  // accounting for letterbox/centering within the .video-wrap container.
  const offsetX = videoRect.left - wrapRect.left;
  const offsetY = videoRect.top - wrapRect.top;

  dom.canvas.style.left = `${offsetX}px`;
  dom.canvas.style.top = `${offsetY}px`;
  dom.canvas.style.width = `${videoRect.width}px`;
  dom.canvas.style.height = `${videoRect.height}px`;
  dom.canvas.width = Math.max(1, Math.round(videoRect.width * dpr));
  dom.canvas.height = Math.max(1, Math.round(videoRect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function drawLandmarks(points, scaleX, scaleY, color, offsetX, offsetY) {
  if (!dom.showLandmarks.checked || !Array.isArray(points)) return;
  ctx.fillStyle = color;
  const ox = offsetX || 0;
  const oy = offsetY || 0;
  for (const point of points) {
    if (!Array.isArray(point) || point.length < 2) continue;
    const x = Number(point[0]) * scaleX + ox;
    const y = Number(point[1]) * scaleY + oy;
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    ctx.beginPath();
    ctx.arc(x, y, 2.8, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawLabel(text, x, y, color) {
  if (!dom.showLabels.checked) return;
  ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  const metrics = ctx.measureText(text);
  const width = metrics.width + 10;
  const height = 20;
  const boxY = Math.max(0, y - height - 3);
  ctx.fillStyle = "rgba(0, 0, 0, 0.76)";
  ctx.fillRect(x, boxY, width, height);
  ctx.fillStyle = color;
  ctx.fillText(text, x + 5, boxY + 14);
}

function drawOverlay() {
  resizeCanvas();
  const displayWidth = dom.canvas.clientWidth;
  const displayHeight = dom.canvas.clientHeight;
  ctx.clearRect(0, 0, displayWidth, displayHeight);

  const active = findActiveAnnotations(dom.video.currentTime);
  const overlayItems = active.lines.flatMap(line => (
    objectsForLine(line).map(obj => ({ obj, line }))
  )).filter(item => objectVisible(item.obj));
  const sourceWidth = dom.video.videoWidth || state.sourceWidth || DEFAULT_SOURCE_WIDTH;
  const sourceHeight = dom.video.videoHeight || state.sourceHeight || DEFAULT_SOURCE_HEIGHT;

  // Use uniform scale to handle object-fit: contain letterboxing.
  // The display rect has the video's intrinsic aspect ratio, but we
  // compute a uniform scale from the source-to-display ratio to prevent
  // any bbox distortion from non-uniform scaling.
  const scaleX = displayWidth / sourceWidth;
  const scaleY = displayHeight / sourceHeight;
  const uniformScale = Math.min(scaleX, scaleY);
  const offsetX_display = (displayWidth - sourceWidth * uniformScale) / 2;
  const offsetY_display = (displayHeight - sourceHeight * uniformScale) / 2;

  for (const { obj, line } of overlayItems) {
    const bbox = normalizeBbox(obj.bbox, sourceWidth, sourceHeight);
    if (!bbox) continue;
    const style = styleForObject(obj);
    const x = bbox.x1 * uniformScale + offsetX_display;
    const y = bbox.y1 * uniformScale + offsetY_display;
    const w = (bbox.x2 - bbox.x1) * uniformScale;
    const h = (bbox.y2 - bbox.y1) * uniformScale;
    ctx.strokeStyle = style.bboxColor;
    ctx.lineWidth = style.lineWidth;
    ctx.strokeRect(x, y, w, h);
    drawLandmarks(obj.landmarks?.points || [], uniformScale, uniformScale, style.bboxColor, offsetX_display, offsetY_display);
    drawLabel(labelForObject(obj, line), x, y, style.labelColor);
  }
  updateDebug(active, overlayItems.length);
  requestAnimationFrame(drawOverlay);
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = value === undefined || value === null || value === "" ? "-" : String(value);
}

function activeRoleCounts(lines) {
  const counts = {
    person_context: 0,
    matched_face: 0,
    behavior_event: 0
  };
  for (const line of lines || []) {
    const objects = objectsForLine(line);
    if (objects.length) {
      for (const obj of objects) {
        const role = lineRole({ ...line, ...obj });
        if (counts[role] !== undefined) {
          counts[role] += 1;
        }
      }
    } else {
      const role = lineRole(line);
      if (counts[role] !== undefined) {
        counts[role] += 1;
      }
    }
  }
  return counts;
}

function renderDetails() {
  const metadata = state.manifest?.metadata || {};
  const summary = state.manifest?.summary || {};
  const event = metadata.event || {};
  const media = metadata.media || {};
  const status = metadata.status || {};
  const validation = media.clip_validation || {};
  const clipStatus = status.clip_status || summary.clip_status || "unknown";
  const decodeWarnings = Number(validation.decode_error_count || 0);
  const corrupt = clipStatus === "generated_corrupt" || decodeWarnings > 0;

  setText("eventId", event.event_id || state.selectedEventId);
  setText("eventType", event.event_type || summary.event_type);
  setText("sourceId", event.source_id || summary.source_id);
  setText("cameraId", event.camera_id || summary.camera_id);
  setText("rawClipStatus", clipStatus);
  setText("clipValidation", corrupt ? `Evidence generated, source decode warnings observed (${decodeWarnings})` : "ok");
  setText("firstVideoPts", state.firstVideoFramePts);
  setText("sourceSize", `${state.sourceWidth}x${state.sourceHeight}`);
  setText("annotationLines", summary.annotation_lines ?? state.annotations.length);
  setText("personContextObjects", summary.person_context_count);
  setText("personContextFrames", summary.person_context_frame_count);
  setText("personContextTracks", summary.person_context_track_count);
  setText("faceObjects", summary.face_objects);
  setText("matchedObjects", summary.matched_objects);
  setText("unknownObjects", summary.unknown_objects);
  setText("colorsUsed", Array.isArray(summary.colors_used) ? summary.colors_used.join(", ") : "");

  dom.clipWarning.hidden = !corrupt;
  dom.clipWarning.textContent = corrupt
    ? "Evidence generated, source decode warnings observed"
    : "";
}

function updateDebug(active, objectCount) {
  const counts = activeRoleCounts(active?.lines || []);
  setText("currentTime", dom.video.currentTime.toFixed(3));
  setText("targetPts", active?.targetMs !== null && active?.targetMs !== undefined ? `${active.targetMs} ms` : "-");
  setText("matchedPts", active?.matchedMs !== null && active?.matchedMs !== undefined ? `${active.matchedMs} ms` : "-");
  setText("alignmentMode", active?.mode || "-");
  setText("activeObjects", objectCount);
  setText("renderPolicy", "mode=per_track_hold");
  setText("faceOverlayPolicy", FACE_OVERLAY_POLICY);
  const annotationPayload = state.annotationPayload || {};
  setText(
    "annotationSourceStatus",
    [
      annotationSourceKind(annotationPayload),
      annotationSourceLabel(annotationPayload.annotation_source || state.annotationSource),
      annotationPayload.fallback_used ? "fallback" : "",
      annotationPayload.reason ? `reason=${annotationPayload.reason}` : ""
    ].filter(Boolean).join(" | ")
  );
  setText("annotationFileStatus", annotationPayload.annotation_file || annotationPayload.source_hint);
  setText("matchedFaceWindowMs", ROLE_RENDER_WINDOW_MS.matched_face);
  setText("personContextWindowMs", ROLE_RENDER_WINDOW_MS.person_context);
  setText("activePersonContext", counts.person_context);
  setText("activeMatchedFaces", counts.matched_face);
  setText("activeBehaviorEvents", counts.behavior_event);
  updateAnnotationSourceBanner();
}

function renderWarnings() {
  dom.warningList.innerHTML = "";
  const warnings = Array.from(state.warnings).sort();
  if (!warnings.length) {
    const item = document.createElement("li");
    item.textContent = "none";
    dom.warningList.appendChild(item);
    return;
  }
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.textContent = warning;
    dom.warningList.appendChild(item);
  }
}

for (const input of [
  dom.showPersons,
  dom.showMatched,
  dom.showUnknown,
  dom.showLandmarks,
  dom.showLabels,
  dom.holdMs,
  dom.toleranceMs
]) {
  if (!input) continue;
  input.addEventListener("input", drawOverlay);
  input.addEventListener("change", drawOverlay);
}

dom.annotationSource?.addEventListener("change", () => {
  if (state.selectedEventId) {
    selectBundle(state.selectedEventId, { preserveVideo: true }).catch(err => addWarning(`annotation_source_switch_failed:${err.message}`));
  } else {
    drawOverlay();
  }
});

dom.refreshBundles.addEventListener("click", () => {
  state.selectedEventId = null;
  loadBundles().catch(err => addWarning(`bundle_load_failed:${err.message}`));
});
window.addEventListener("resize", resizeCanvas);
dom.video.addEventListener("loadedmetadata", () => {
  if (dom.video.videoWidth && dom.video.videoHeight) {
    state.sourceWidth = dom.video.videoWidth;
    state.sourceHeight = dom.video.videoHeight;
  }
  resizeCanvas();
});

function applyOverlayDefaults() {
  dom.showPersons.checked = DEFAULT_SHOW_PERSON_BOXES;
  dom.showUnknown.checked = DEFAULT_SHOW_UNKNOWN_FACES;
  applyAnnotationSourceLabels();
}

async function init() {
  applyOverlayDefaults();
  await loadHealth();
  await loadBundles();
  resizeCanvas();
  requestAnimationFrame(drawOverlay);
}

init().catch(err => addWarning(`viewer_init_failed:${err.message}`));
