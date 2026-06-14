"use strict";

const MAINTENANCE_API = "/api/v1/maintenance";
const NO_AUTO_REGENERATE = "删除后不会自动重新生成证据";

const maintenanceState = {
  initialized: false,
  preview: null,
  facePreview: null,
  facePreviewKind: null
};

const maintenanceDom = {
  refresh: document.getElementById("refresh-maintenance"),
  mediaFree: document.getElementById("maintenance-media-free"),
  mediaUsed: document.getElementById("maintenance-media-used"),
  evidenceBytes: document.getElementById("maintenance-evidence-bytes"),
  evidenceCount: document.getElementById("maintenance-evidence-count"),
  faceBytes: document.getElementById("maintenance-face-bytes"),
  faceCount: document.getElementById("maintenance-face-count"),
  trashBytes: document.getElementById("maintenance-trash-bytes"),
  trashCount: document.getElementById("maintenance-trash-count"),
  executeStatus: document.getElementById("maintenance-execute-status"),
  evidenceForm: document.getElementById("maintenance-evidence-form"),
  previewEvidenceDelete: document.getElementById("preview-evidence-delete"),
  previewResult: document.getElementById("maintenance-preview-result"),
  reason: document.getElementById("maintenance-delete-reason"),
  executeEvidenceDelete: document.getElementById("execute-evidence-delete"),
  previewPeopleDelete: document.getElementById("preview-people-delete"),
  previewGalleryDelete: document.getElementById("preview-gallery-delete"),
  previewFaceOrphans: document.getElementById("preview-face-orphans"),
  faceForm: document.getElementById("maintenance-face-form"),
  faceResult: document.getElementById("maintenance-face-result"),
  faceReason: document.getElementById("maintenance-face-delete-reason"),
  executeFaceDelete: document.getElementById("execute-face-delete"),
  jobId: document.getElementById("maintenance-job-id"),
  loadJob: document.getElementById("load-maintenance-job"),
  jobDetail: document.getElementById("maintenance-job-detail")
};

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = n;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function localIso(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

function csvValues(value) {
  return String(value || "")
    .split(",")
    .map(item => item.trim())
    .filter(Boolean);
}

function csvNumbers(value) {
  return csvValues(value)
    .map(item => Number(item))
    .filter(item => Number.isInteger(item));
}

function positiveNumberOrNull(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function setButtonBusy(button, busy, busyText) {
  if (!button) return () => {};
  const previousText = button.textContent;
  const previousDisabled = button.disabled;
  button.disabled = Boolean(busy);
  if (busy && busyText) {
    button.textContent = busyText;
  }
  return () => {
    button.textContent = previousText;
    button.disabled = previousDisabled;
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function maintenanceRequest(path, options = {}) {
  if (typeof request === "function") {
    return request(`${MAINTENANCE_API}${path}`, options);
  }
  const response = await fetch(`${MAINTENANCE_API}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const body = await response.json();
  if (!response.ok || body.error) {
    throw new Error(body.error?.message || `HTTP ${response.status}`);
  }
  return body.data ?? body;
}

function evidencePreviewBody() {
  const fd = new FormData(maintenanceDom.evidenceForm);
  const eventIds = csvValues(fd.get("event_ids"));
  const body = {
    event_ids: eventIds,
    time_from: localIso(fd.get("time_from")),
    time_to: localIso(fd.get("time_to")),
    event_category: fd.get("event_category") || null,
    camera_id: fd.get("camera_id") || null,
    allow_stale_pending_tasks: fd.get("allow_stale_pending_tasks") === "on",
    delete_mode: fd.get("delete_mode") || "trash",
    max_items: Number(fd.get("max_items") || 1000),
    operator: "operator"
  };
  Object.keys(body).forEach((key) => {
    if (body[key] === null || body[key] === "") delete body[key];
  });
  return body;
}

function faceFormBody() {
  const fd = new FormData(maintenanceDom.faceForm);
  return {
    person_ids: csvNumbers(fd.get("person_ids")),
    external_person_ids: csvValues(fd.get("external_person_ids")),
    gallery_embedding_ids: csvNumbers(fd.get("gallery_embedding_ids")),
    older_than_days: positiveNumberOrNull(fd.get("older_than_days"))
  };
}

function renderPreview(preview, target = maintenanceDom.previewResult) {
  const expiresAt = preview.preview_expires_at ? new Date(preview.preview_expires_at) : null;
  const expired = expiresAt && expiresAt.getTime() <= Date.now();
  const deletableCount = Number(preview.deletable_count || 0);
  const skipped = Array.isArray(preview.skipped) ? preview.skipped : [];
  const skippedLines = skipped.slice(0, 5).map(item => {
    const targetId = escapeHtml(item.target_id || "-");
    const reason = escapeHtml(item.reason || "skipped");
    return `<li>${targetId}：${reason}</li>`;
  });
  const lines = [
    `<strong>预览已生成</strong>`,
    `<div>候选 ${preview.candidate_count || 0}，可删除 ${preview.deletable_count || 0}，跳过 ${preview.skipped_count || 0}</div>`,
    `<div>预计释放空间：${formatBytes(preview.estimated_bytes)}</div>`,
    `<div>过期时间：${expiresAt ? expiresAt.toLocaleString() : "-"}</div>`,
    `<div>${NO_AUTO_REGENERATE}</div>`,
    `<div>预览 ID：${escapeHtml(preview.preview_id || "-")}</div>`,
    skippedLines.length ? `<ul>${skippedLines.join("")}</ul>` : ""
  ];
  target.innerHTML = lines.join("");
  if (target === maintenanceDom.previewResult) {
    maintenanceDom.executeEvidenceDelete.disabled = Boolean(expired || !preview.preview_id || deletableCount <= 0);
  }
  if (target === maintenanceDom.faceResult) {
    maintenanceDom.executeFaceDelete.disabled = Boolean(expired || !preview.preview_id || deletableCount <= 0);
  }
}

async function loadMaintenanceSummary() {
  const summary = await maintenanceRequest("/storage/summary");
  maintenanceDom.mediaFree.textContent = formatBytes(summary.media_root?.free_bytes);
  maintenanceDom.mediaUsed.textContent = `已用空间 ${formatBytes(summary.media_root?.used_bytes)} / ${formatBytes(summary.media_root?.total_bytes)}`;
  maintenanceDom.evidenceBytes.textContent = formatBytes(summary.evidence?.total_bytes);
  maintenanceDom.evidenceCount.textContent = `证据 ${summary.evidence?.bundle_count || 0}`;
  const faceBytes = Number(summary.face_media?.upload_bytes || 0) + Number(summary.face_media?.registration_bytes || 0);
  maintenanceDom.faceBytes.textContent = formatBytes(faceBytes);
  maintenanceDom.faceCount.textContent = `引用 ${summary.face_media?.referenced_file_count || 0} / 未引用 ${summary.face_media?.orphan_file_count || 0}`;
  maintenanceDom.trashBytes.textContent = formatBytes(summary.trash?.bytes);
  maintenanceDom.trashCount.textContent = `对象 ${summary.trash?.item_count || 0}`;
  const executeEnabled = summary.contract?.execute_enabled === true;
  maintenanceDom.executeStatus.textContent = executeEnabled
    ? "删除执行已开启。请先生成预览，确认候选对象后再执行删除。"
    : "Midterm 当前只允许统计和预览，执行删除处于关闭状态。";
}

async function previewEvidenceDelete() {
  maintenanceState.preview = null;
  maintenanceDom.executeEvidenceDelete.disabled = true;
  maintenanceDom.previewResult.innerHTML = "<strong>正在生成预览</strong>";
  const restore = setButtonBusy(maintenanceDom.previewEvidenceDelete, true, "预览中");
  try {
    const preview = await maintenanceRequest("/evidence/delete-preview", {
      method: "POST",
      body: JSON.stringify(evidencePreviewBody())
    });
    maintenanceState.preview = preview;
    maintenanceDom.jobId.value = preview.preview_id || "";
    renderPreview(preview);
  } finally {
    restore();
  }
}

async function executeEvidenceDelete() {
  const preview = maintenanceState.preview;
  if (!preview?.preview_id) {
    throw new Error("删除必须先 preview");
  }
  const reason = maintenanceDom.reason.value.trim();
  if (!reason) {
    throw new Error("请填写删除原因");
  }
  const confirmed = window.confirm(`确认删除预览中的证据？${NO_AUTO_REGENERATE}`);
  if (!confirmed) return;
  const restore = setButtonBusy(maintenanceDom.executeEvidenceDelete, true, "删除中");
  try {
    const result = await maintenanceRequest("/evidence/delete", {
      method: "POST",
      body: JSON.stringify({
        preview_id: preview.preview_id,
        confirm_token: preview.confirm_token,
        candidate_hash: preview.candidate_hash,
        delete_mode: preview.delete_mode || evidencePreviewBody().delete_mode,
        reason,
        operator: "operator"
      })
    });
    const resultPayload = result.result || {};
    maintenanceDom.previewResult.innerHTML = [
      `<strong>执行结果：${escapeHtml(result.status || "-")}</strong>`,
      `<div>已处理 ${resultPayload.completed_count ?? "-"}，跳过 ${resultPayload.skipped_count ?? "-"}，失败 ${resultPayload.failed_count ?? "-"}</div>`,
      `<div>Job ID：${escapeHtml(result.job_id || preview.preview_id || "-")}</div>`,
      `<div>${NO_AUTO_REGENERATE}</div>`
    ].join("");
    maintenanceDom.executeEvidenceDelete.disabled = true;
    maintenanceState.preview = null;
    await loadMaintenanceSummary();
    if (window.operatorEvidence?.reload) {
      await window.operatorEvidence.reload();
    }
  } finally {
    restore();
    maintenanceDom.executeEvidenceDelete.disabled = true;
  }
}

async function previewPeopleDelete() {
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = "people";
  maintenanceDom.executeFaceDelete.disabled = true;
  const body = faceFormBody();
  const preview = await maintenanceRequest("/people/delete-preview", {
    method: "POST",
    body: JSON.stringify({
      person_ids: body.person_ids,
      external_person_ids: body.external_person_ids,
      include_gallery: true,
      older_than_days: body.older_than_days,
      operator: "operator"
    })
  });
  maintenanceState.facePreview = preview;
  renderPreview(preview, maintenanceDom.faceResult);
}

async function previewGalleryDelete() {
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = "gallery";
  maintenanceDom.executeFaceDelete.disabled = true;
  const body = faceFormBody();
  const preview = await maintenanceRequest("/people/gallery-delete-preview", {
    method: "POST",
    body: JSON.stringify({
      gallery_embedding_ids: body.gallery_embedding_ids,
      person_ids: body.person_ids,
      older_than_days: body.older_than_days,
      operator: "operator"
    })
  });
  maintenanceState.facePreview = preview;
  renderPreview(preview, maintenanceDom.faceResult);
}

async function previewFaceOrphans() {
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = "face_orphans";
  maintenanceDom.executeFaceDelete.disabled = true;
  const body = faceFormBody();
  const preview = await maintenanceRequest("/face-media/orphans-preview", {
    method: "POST",
    body: JSON.stringify({
      older_than_days: body.older_than_days ?? 7,
      allow_inactive_reference_cleanup: false,
      delete_mode: "trash"
    })
  });
  maintenanceState.facePreview = preview;
  renderPreview(preview, maintenanceDom.faceResult);
}

async function executeFaceDelete() {
  const preview = maintenanceState.facePreview;
  if (!preview?.preview_id || !maintenanceState.facePreviewKind) {
    throw new Error("删除必须先 preview");
  }
  const reason = maintenanceDom.faceReason.value.trim();
  if (!reason) {
    throw new Error("请填写删除原因");
  }
  const confirmed = window.confirm(`确认删除预览中的对象？${NO_AUTO_REGENERATE}`);
  if (!confirmed) return;

  const commonBody = {
    preview_id: preview.preview_id,
    confirm_token: preview.confirm_token,
    candidate_hash: preview.candidate_hash,
    reason,
    operator: "operator"
  };
  const endpoints = {
    people: "/people/delete",
    gallery: "/people/gallery-delete",
    face_orphans: "/face-media/orphans-cleanup"
  };
  const body = maintenanceState.facePreviewKind === "face_orphans"
    ? { ...commonBody, delete_mode: preview.delete_mode || "trash" }
    : commonBody;
  const result = await maintenanceRequest(endpoints[maintenanceState.facePreviewKind], {
    method: "POST",
    body: JSON.stringify(body)
  });
  maintenanceDom.faceResult.innerHTML =
    `<strong>执行结果：${result.status}</strong><div>${NO_AUTO_REGENERATE}</div>`;
  maintenanceDom.executeFaceDelete.disabled = true;
}

async function loadJobDetail() {
  const jobId = maintenanceDom.jobId.value.trim();
  if (!jobId) throw new Error("请输入 Job ID");
  const detail = await maintenanceRequest(`/jobs/${encodeURIComponent(jobId)}`);
  const job = detail.job || {};
  maintenanceDom.jobDetail.innerHTML = [
    `<strong>${job.job_type || "job"}：${job.status || "-"}</strong>`,
    `<div>候选 hash：${job.candidate_hash || "-"}</div>`,
    `<div>预览过期：${job.preview_expires_at || "-"}</div>`,
    `<div>${detail.no_auto_regenerate_message || NO_AUTO_REGENERATE}</div>`
  ].join("");
}

function bindMaintenanceEvents() {
  maintenanceDom.refresh?.addEventListener("click", () => loadMaintenanceSummary().catch((e) => showError(e.message)));
  maintenanceDom.previewEvidenceDelete?.addEventListener("click", () => previewEvidenceDelete().catch((e) => showError(e.message)));
  maintenanceDom.executeEvidenceDelete?.addEventListener("click", () => executeEvidenceDelete().catch((e) => showError(e.message)));
  maintenanceDom.previewPeopleDelete?.addEventListener("click", () => previewPeopleDelete().catch((e) => showError(e.message)));
  maintenanceDom.previewGalleryDelete?.addEventListener("click", () => previewGalleryDelete().catch((e) => showError(e.message)));
  maintenanceDom.previewFaceOrphans?.addEventListener("click", () => previewFaceOrphans().catch((e) => showError(e.message)));
  maintenanceDom.executeFaceDelete?.addEventListener("click", () => executeFaceDelete().catch((e) => showError(e.message)));
  maintenanceDom.loadJob?.addEventListener("click", () => loadJobDetail().catch((e) => showError(e.message)));
}

async function initMaintenance() {
  if (!maintenanceState.initialized) {
    bindMaintenanceEvents();
    maintenanceState.initialized = true;
  }
  await loadMaintenanceSummary();
}

async function prepareDelete(request = {}) {
  await initMaintenance();
  if (request.kind === "evidence") {
    maintenanceDom.evidenceForm.elements.event_ids.value = csvValues(request.event_ids || []).join(",");
    maintenanceDom.reason.value = "";
    await previewEvidenceDelete();
    return;
  }
  if (request.kind === "person") {
    maintenanceDom.faceForm.elements.person_ids.value = csvValues(request.person_ids || []).join(",");
    maintenanceDom.faceForm.elements.external_person_ids.value = csvValues(request.external_person_ids || []).join(",");
    maintenanceDom.faceReason.value = "";
    await previewPeopleDelete();
  }
}

window.operatorMaintenance = {
  init: initMaintenance,
  previewEvidenceDelete,
  prepareDelete,
  loadMaintenanceSummary
};
