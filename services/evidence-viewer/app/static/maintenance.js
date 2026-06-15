"use strict";

const MAINTENANCE_API = "/api/v1/maintenance";
const NO_AUTO_REGENERATE = "删除后不会自动重新生成证据";

const maintenanceState = {
  initialized: false,
  preview: null,
  facePreview: null,
  facePreviewKind: null,
  activeDelete: null
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
  activePane: document.getElementById("maintenance-active-delete-pane"),
  activeTitle: document.getElementById("maintenance-active-delete-title"),
  activeSubtitle: document.getElementById("maintenance-active-delete-subtitle"),
  activeTarget: document.getElementById("maintenance-active-delete-target"),
  activeResult: document.getElementById("maintenance-active-delete-result"),
  activeReason: document.getElementById("maintenance-active-delete-reason"),
  activeExecute: document.getElementById("execute-active-delete"),
  activeCancel: document.getElementById("maintenance-active-delete-cancel"),
  evidencePane: document.querySelector(".maintenance-preview-pane"),
  facePane: document.querySelector(".maintenance-face-pane"),
  jobPane: document.querySelector(".maintenance-job-pane"),
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

function setDeleteButtonDisabled(button, disabled, reason = "") {
  if (!button) return;
  button.disabled = Boolean(disabled);
  button.title = disabled && reason ? reason : "";
}

function deleteKindLabel(request = {}) {
  if (request.kind === "evidence") return "证据";
  if (request.kind === "person") return "人员";
  if (request.kind === "gallery") return "图库照片";
  return "对象";
}

function defaultDeleteReason(request = {}) {
  if (request.default_reason) return request.default_reason;
  if (request.kind === "evidence") return `operator_delete_evidence:${csvValues(request.event_ids || []).join(",")}`;
  if (request.kind === "person") return `operator_delete_person:${csvValues(request.person_ids || []).join(",")}`;
  if (request.kind === "gallery") return `operator_delete_gallery:${csvValues(request.gallery_embedding_ids || []).join(",")}`;
  return "operator_delete";
}

function renderTargetSummary(request = {}) {
  const target = request.target || {};
  const title = target.title || `删除${deleteKindLabel(request)}`;
  const fields = Array.isArray(target.fields) ? target.fields : [];
  const fallbackFields = [];
  if (request.event_ids?.length) fallbackFields.push({ label: "事件 ID", value: csvValues(request.event_ids).join(", ") });
  if (request.person_ids?.length) fallbackFields.push({ label: "人员 ID", value: csvValues(request.person_ids).join(", ") });
  if (request.external_person_ids?.length) fallbackFields.push({ label: "人员编号", value: csvValues(request.external_person_ids).join(", ") });
  if (request.gallery_embedding_ids?.length) fallbackFields.push({ label: "图库 ID", value: csvValues(request.gallery_embedding_ids).join(", ") });
  const displayFields = fields.length ? fields : fallbackFields;
  const fieldHtml = displayFields.map((field) => {
    const tag = /id|编号/i.test(String(field.label || "")) ? "code" : "b";
    return `<div><span>${escapeHtml(field.label || "目标")}</span><${tag}>${escapeHtml(field.value || "-")}</${tag}></div>`;
  }).join("");
  maintenanceDom.activeTarget.innerHTML = [
    `<strong>${escapeHtml(title)}</strong>`,
    fieldHtml ? `<div class="maintenance-target-grid">${fieldHtml}</div>` : ""
  ].join("");
}

function showActiveDelete(request = {}) {
  maintenanceState.activeDelete = request;
  maintenanceDom.activePane.hidden = false;
  maintenanceDom.evidencePane.hidden = true;
  maintenanceDom.facePane.hidden = true;
  maintenanceDom.jobPane.hidden = true;
  maintenanceDom.activeTitle.textContent = `删除${deleteKindLabel(request)}`;
  maintenanceDom.activeSubtitle.textContent = "系统已自动带入你刚才选择的对象，不需要再填写人员 ID 或图库 ID。";
  maintenanceDom.activeExecute.textContent = `删除${deleteKindLabel(request)}`;
  renderTargetSummary(request);
  maintenanceDom.activeReason.value = defaultDeleteReason(request);
  maintenanceDom.activeResult.innerHTML = "<strong>正在生成预览</strong>";
  setDeleteButtonDisabled(maintenanceDom.activeExecute, true, "正在生成预览");
  maintenanceDom.activePane.scrollIntoView({ block: "start", behavior: "smooth" });
}

function hideActiveDelete() {
  maintenanceState.activeDelete = null;
  maintenanceDom.activePane.hidden = true;
  maintenanceDom.evidencePane.hidden = false;
  maintenanceDom.facePane.hidden = false;
  maintenanceDom.jobPane.hidden = false;
  maintenanceDom.activeResult.innerHTML = "正在生成删除预览。";
  maintenanceDom.activeReason.value = "";
  maintenanceDom.activeExecute.textContent = "确认删除";
  setDeleteButtonDisabled(maintenanceDom.activeExecute, true);
}

function previewDisabledReason(preview = {}) {
  const expiresAt = preview.preview_expires_at ? new Date(preview.preview_expires_at) : null;
  const expired = expiresAt && expiresAt.getTime() <= Date.now();
  const deletableCount = Number(preview.deletable_count || 0);
  if (expired) return "预览已过期，请重新生成";
  if (!preview.preview_id) return "预览缺少 ID";
  if (deletableCount <= 0) return "本次预览没有可删除对象";
  return "";
}

function requireDeleteTargets(values, label) {
  if (values.length) return values;
  throw new Error(`没有拿到${label}，请刷新人员页后重试`);
}

function activePreviewBody(deleteRequest = {}) {
  if (deleteRequest.kind === "evidence") {
    return {
      event_ids: requireDeleteTargets(csvValues(deleteRequest.event_ids || []), "事件 ID"),
      delete_mode: deleteRequest.delete_mode || "trash",
      allow_stale_pending_tasks: Boolean(deleteRequest.allow_stale_pending_tasks),
      max_items: Number(deleteRequest.max_items || 1000),
      operator: "operator"
    };
  }
  if (deleteRequest.kind === "person") {
    return {
      person_ids: csvNumbers(deleteRequest.person_ids || []),
      external_person_ids: csvValues(deleteRequest.external_person_ids || []),
      include_gallery: true,
      older_than_days: positiveNumberOrNull(deleteRequest.older_than_days),
      operator: "operator"
    };
  }
  if (deleteRequest.kind === "gallery") {
    return {
      gallery_embedding_ids: requireDeleteTargets(
        csvNumbers(deleteRequest.gallery_embedding_ids || []),
        "图库 ID"
      ),
      person_ids: csvNumbers(deleteRequest.person_ids || []),
      older_than_days: positiveNumberOrNull(deleteRequest.older_than_days),
      operator: "operator"
    };
  }
  throw new Error(`不支持的删除类型：${deleteRequest.kind}`);
}

async function previewActiveDelete(deleteRequest = {}) {
  maintenanceState.preview = null;
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = null;
  setDeleteButtonDisabled(maintenanceDom.activeExecute, true, "正在生成预览");
  maintenanceDom.activeResult.innerHTML = "<strong>正在生成预览</strong>";
  if (deleteRequest.kind === "evidence") {
    const preview = await maintenanceRequest("/evidence/delete-preview", {
      method: "POST",
      body: JSON.stringify(activePreviewBody(deleteRequest))
    });
    maintenanceState.preview = preview;
    maintenanceDom.jobId.value = preview.preview_id || "";
    renderPreview(preview, maintenanceDom.activeResult);
    return;
  }
  if (deleteRequest.kind === "person") {
    const preview = await maintenanceRequest("/people/delete-preview", {
      method: "POST",
      body: JSON.stringify(activePreviewBody(deleteRequest))
    });
    maintenanceState.facePreview = preview;
    maintenanceState.facePreviewKind = "people";
    renderPreview(preview, maintenanceDom.activeResult);
    return;
  }
  if (deleteRequest.kind === "gallery") {
    const preview = await maintenanceRequest("/people/gallery-delete-preview", {
      method: "POST",
      body: JSON.stringify(activePreviewBody(deleteRequest))
    });
    maintenanceState.facePreview = preview;
    maintenanceState.facePreviewKind = "gallery";
    renderPreview(preview, maintenanceDom.activeResult);
  }
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
  const disabledReason = previewDisabledReason(preview);
  const skipped = Array.isArray(preview.skipped) ? preview.skipped : [];
  const skippedLines = skipped.slice(0, 5).map(item => {
    const targetId = escapeHtml(item.target_id || "-");
    const reason = escapeHtml(item.reason || "skipped");
    return `<li>${targetId}：${reason}</li>`;
  });
  const lines = [
    `<strong>预览已生成</strong>`,
    `<div>候选 ${preview.candidate_count || 0}，可删除 ${preview.deletable_count || 0}，跳过 ${preview.skipped_count || 0}</div>`,
    disabledReason ? `<div class="delete-disabled-reason">无法删除：${escapeHtml(disabledReason)}</div>` : "",
    `<div>预计释放空间：${formatBytes(preview.estimated_bytes)}</div>`,
    `<div>过期时间：${expiresAt ? expiresAt.toLocaleString() : "-"}</div>`,
    `<div>${NO_AUTO_REGENERATE}</div>`,
    `<div>预览 ID：${escapeHtml(preview.preview_id || "-")}</div>`,
    skippedLines.length ? `<ul>${skippedLines.join("")}</ul>` : ""
  ];
  target.innerHTML = lines.join("");
  if (target === maintenanceDom.previewResult) {
    setDeleteButtonDisabled(maintenanceDom.executeEvidenceDelete, Boolean(disabledReason), disabledReason);
  }
  if (target === maintenanceDom.faceResult) {
    setDeleteButtonDisabled(maintenanceDom.executeFaceDelete, Boolean(disabledReason), disabledReason);
  }
  if (target === maintenanceDom.activeResult) {
    setDeleteButtonDisabled(maintenanceDom.activeExecute, Boolean(disabledReason), disabledReason);
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

async function previewEvidenceDelete(options = {}) {
  const target = options.target || maintenanceDom.previewResult;
  const previewButton = options.button === undefined ? maintenanceDom.previewEvidenceDelete : options.button;
  maintenanceState.preview = null;
  if (target === maintenanceDom.previewResult) {
    setDeleteButtonDisabled(maintenanceDom.executeEvidenceDelete, true, "正在生成预览");
  }
  if (target === maintenanceDom.activeResult) {
    setDeleteButtonDisabled(maintenanceDom.activeExecute, true, "正在生成预览");
  }
  target.innerHTML = "<strong>正在生成预览</strong>";
  const restore = setButtonBusy(previewButton, true, "预览中");
  try {
    const preview = await maintenanceRequest("/evidence/delete-preview", {
      method: "POST",
      body: JSON.stringify(evidencePreviewBody())
    });
    maintenanceState.preview = preview;
    maintenanceDom.jobId.value = preview.preview_id || "";
    renderPreview(preview, target);
  } finally {
    restore();
  }
}

async function executeEvidenceDelete(options = {}) {
  const preview = maintenanceState.preview;
  if (!preview?.preview_id) {
    throw new Error("删除必须先 preview");
  }
  const reasonInput = options.reasonInput || maintenanceDom.reason;
  const resultTarget = options.resultTarget || maintenanceDom.previewResult;
  const executeButton = options.executeButton || maintenanceDom.executeEvidenceDelete;
  const reason = reasonInput.value.trim();
  if (!reason) {
    throw new Error("请填写删除原因");
  }
  if (options.confirm !== false) {
    const confirmed = window.confirm(`确认删除预览中的证据？${NO_AUTO_REGENERATE}`);
    if (!confirmed) return;
  }
  const restore = setButtonBusy(executeButton, true, "删除中");
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
    resultTarget.innerHTML = [
      `<strong>执行结果：${escapeHtml(result.status || "-")}</strong>`,
      `<div>已处理 ${resultPayload.completed_count ?? "-"}，跳过 ${resultPayload.skipped_count ?? "-"}，失败 ${resultPayload.failed_count ?? "-"}</div>`,
      `<div>Job ID：${escapeHtml(result.job_id || preview.preview_id || "-")}</div>`,
      `<div>${NO_AUTO_REGENERATE}</div>`
    ].join("");
    setDeleteButtonDisabled(executeButton, true);
    maintenanceState.preview = null;
    await loadMaintenanceSummary();
    if (window.operatorEvidence?.reload) {
      await window.operatorEvidence.reload();
    }
  } finally {
    restore();
    setDeleteButtonDisabled(executeButton, true);
  }
}

async function previewPeopleDelete(options = {}) {
  const target = options.target || maintenanceDom.faceResult;
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = "people";
  if (target === maintenanceDom.faceResult) {
    setDeleteButtonDisabled(maintenanceDom.executeFaceDelete, true, "正在生成预览");
  }
  if (target === maintenanceDom.activeResult) {
    setDeleteButtonDisabled(maintenanceDom.activeExecute, true, "正在生成预览");
  }
  target.innerHTML = "<strong>正在生成预览</strong>";
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
  renderPreview(preview, target);
}

async function previewGalleryDelete(options = {}) {
  const target = options.target || maintenanceDom.faceResult;
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = "gallery";
  if (target === maintenanceDom.faceResult) {
    setDeleteButtonDisabled(maintenanceDom.executeFaceDelete, true, "正在生成预览");
  }
  if (target === maintenanceDom.activeResult) {
    setDeleteButtonDisabled(maintenanceDom.activeExecute, true, "正在生成预览");
  }
  target.innerHTML = "<strong>正在生成预览</strong>";
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
  renderPreview(preview, target);
}

async function previewFaceOrphans() {
  maintenanceState.facePreview = null;
  maintenanceState.facePreviewKind = "face_orphans";
  setDeleteButtonDisabled(maintenanceDom.executeFaceDelete, true, "正在生成预览");
  maintenanceDom.faceResult.innerHTML = "<strong>正在生成预览</strong>";
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

async function executeFaceDelete(options = {}) {
  const preview = maintenanceState.facePreview;
  if (!preview?.preview_id || !maintenanceState.facePreviewKind) {
    throw new Error("删除必须先 preview");
  }
  const reasonInput = options.reasonInput || maintenanceDom.faceReason;
  const resultTarget = options.resultTarget || maintenanceDom.faceResult;
  const executeButton = options.executeButton || maintenanceDom.executeFaceDelete;
  const reason = reasonInput.value.trim();
  if (!reason) {
    throw new Error("请填写删除原因");
  }
  if (options.confirm !== false) {
    const confirmed = window.confirm(`确认删除预览中的对象？${NO_AUTO_REGENERATE}`);
    if (!confirmed) return;
  }

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
  const restore = setButtonBusy(executeButton, true, "删除中");
  try {
    const result = await maintenanceRequest(endpoints[maintenanceState.facePreviewKind], {
      method: "POST",
      body: JSON.stringify(body)
    });
    resultTarget.innerHTML =
      `<strong>执行结果：${escapeHtml(result.status)}</strong><div>${NO_AUTO_REGENERATE}</div>`;
    setDeleteButtonDisabled(executeButton, true);
    maintenanceState.facePreview = null;
    await loadMaintenanceSummary();
    if (typeof loadPeople === "function") {
      await loadPeople();
    }
  } finally {
    restore();
    setDeleteButtonDisabled(executeButton, true);
  }
}

async function executeActiveDelete() {
  const request = maintenanceState.activeDelete;
  if (!request) {
    throw new Error("没有待确认的删除对象");
  }
  if (request.kind === "evidence") {
    await executeEvidenceDelete({
      reasonInput: maintenanceDom.activeReason,
      resultTarget: maintenanceDom.activeResult,
      executeButton: maintenanceDom.activeExecute,
      confirm: false
    });
    return;
  }
  if (request.kind === "person" || request.kind === "gallery") {
    await executeFaceDelete({
      reasonInput: maintenanceDom.activeReason,
      resultTarget: maintenanceDom.activeResult,
      executeButton: maintenanceDom.activeExecute,
      confirm: false
    });
    return;
  }
  throw new Error(`不支持的删除类型：${request.kind}`);
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
  maintenanceDom.previewEvidenceDelete?.addEventListener("click", () => {
    hideActiveDelete();
    previewEvidenceDelete().catch((e) => showError(e.message));
  });
  maintenanceDom.executeEvidenceDelete?.addEventListener("click", () => executeEvidenceDelete().catch((e) => showError(e.message)));
  maintenanceDom.previewPeopleDelete?.addEventListener("click", () => {
    hideActiveDelete();
    previewPeopleDelete().catch((e) => showError(e.message));
  });
  maintenanceDom.previewGalleryDelete?.addEventListener("click", () => {
    hideActiveDelete();
    previewGalleryDelete().catch((e) => showError(e.message));
  });
  maintenanceDom.previewFaceOrphans?.addEventListener("click", () => {
    hideActiveDelete();
    previewFaceOrphans().catch((e) => showError(e.message));
  });
  maintenanceDom.executeFaceDelete?.addEventListener("click", () => executeFaceDelete().catch((e) => showError(e.message)));
  maintenanceDom.activeExecute?.addEventListener("click", () => executeActiveDelete().catch((e) => showError(e.message)));
  maintenanceDom.activeCancel?.addEventListener("click", hideActiveDelete);
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
  showActiveDelete(request);
  if (request.kind === "evidence") {
    maintenanceDom.evidenceForm.elements.event_ids.value = csvValues(request.event_ids || []).join(",");
    maintenanceDom.reason.value = defaultDeleteReason(request);
    await previewActiveDelete(request);
    return;
  }
  if (request.kind === "person") {
    maintenanceDom.faceForm.elements.person_ids.value = csvValues(request.person_ids || []).join(",");
    maintenanceDom.faceForm.elements.external_person_ids.value = csvValues(request.external_person_ids || []).join(",");
    maintenanceDom.faceForm.elements.gallery_embedding_ids.value = "";
    maintenanceDom.faceReason.value = defaultDeleteReason(request);
    await previewActiveDelete(request);
    return;
  }
  if (request.kind === "gallery") {
    maintenanceDom.faceForm.elements.person_ids.value = csvValues(request.person_ids || []).join(",");
    maintenanceDom.faceForm.elements.external_person_ids.value = "";
    maintenanceDom.faceForm.elements.gallery_embedding_ids.value = csvValues(request.gallery_embedding_ids || []).join(",");
    maintenanceDom.faceReason.value = defaultDeleteReason(request);
    await previewActiveDelete(request);
  }
}

window.operatorMaintenance = {
  init: initMaintenance,
  previewEvidenceDelete,
  prepareDelete,
  loadMaintenanceSummary
};
