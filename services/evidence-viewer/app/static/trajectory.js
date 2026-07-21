/* ------------------------------------------------------------------ */
/*  Registered-person trajectory page                                 */
/*  Persisted, DB-backed hits only; images render inline on the page.   */
/* ------------------------------------------------------------------ */

const TRAJECTORY_PAGE_SIZE = 50;
const TRAJECTORY_MIN_SIMILARITY = 0.6;

const trajectoryDom = {
  view: document.getElementById("trajectory-view"),
  form: document.getElementById("trajectory-search-form"),
  personId: document.getElementById("trajectory-person-id"),
  personOptions: document.getElementById("trajectory-person-options"),
  cameraId: document.getElementById("trajectory-camera-id"),
  startTime: document.getElementById("trajectory-start-time"),
  endTime: document.getElementById("trajectory-end-time"),
  search: document.getElementById("trajectory-search"),
  reset: document.getElementById("trajectory-reset"),
  refresh: document.getElementById("trajectory-refresh"),
  previous: document.getElementById("trajectory-previous"),
  next: document.getElementById("trajectory-next"),
  pageStatus: document.getElementById("trajectory-page-status"),
  summary: document.getElementById("trajectory-search-summary"),
  list: document.getElementById("trajectory-list"),
  detailImage: document.getElementById("trajectory-detail-image"),
  detailEmpty: document.getElementById("trajectory-detail-empty"),
  detail: document.getElementById("trajectory-detail"),
};

const trajectoryState = {
  initialized: false,
  initPromise: null,
  eventsBound: false,
  people: [],
  cameras: [],
  rows: [],
  personId: "",
  offset: 0,
  hasMore: false,
  selectedIndex: -1,
  requestId: 0,
};

function trajectoryEscapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function trajectoryReportError(error) {
  if (typeof showError === "function") {
    showError(error?.message || String(error || "轨迹查询失败"));
  }
}

function trajectorySetStatus(text) {
  if (typeof setStatus === "function") setStatus(text);
}

async function trajectoryRequest(path) {
  if (typeof request === "function") return request(path);
  const response = await fetch(path, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok || body?.error) {
    throw new Error(body?.error?.message || `HTTP ${response.status}`);
  }
  return body?.data ?? body;
}

function trajectoryTimestamp(row) {
  return row?.event_ts_ms ?? row?.observation_timestamp_ms ?? row?.event_created_at ?? "";
}

function trajectoryFormatTime(value) {
  if (value === undefined || value === null || value === "") return "--";
  const numeric = Number(value);
  const date = Number.isFinite(numeric) ? new Date(numeric) : new Date(String(value));
  return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString();
}

function trajectoryFormatPercent(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${Math.round(numeric * 100)}%` : "--";
}

function trajectoryThumbnailUrl(row) {
  return row?.trajectory_thumbnail_url || row?.face_crop_url ||
    row?.annotated_frame_url || row?.full_frame_url || "";
}

function trajectoryPreviewUrl(row) {
  return row?.annotated_frame_url || row?.full_frame_url ||
    row?.face_crop_url || row?.trajectory_thumbnail_url || "";
}

function trajectorySourceLabel(source) {
  const labels = {
    watchlist_event: "名单命中",
    gallery_observation: "图库轨迹",
    person_search_observation: "人脸观察",
    live_search_hit: "一键找人",
  };
  return labels[source] || source || "轨迹";
}

function trajectoryCameraName(row) {
  return row?.camera_name || row?.source_id || row?.camera_id || "未知摄像头";
}

function trajectoryPersonByInput(rawValue) {
  const value = String(rawValue || "").trim();
  if (!value) return null;
  return trajectoryState.people.find((person) => (
    String(person.person_id) === value ||
    String(person.external_person_id || "") === value
  )) || null;
}

function resolveTrajectoryPersonId(rawValue) {
  const value = String(rawValue || "").trim();
  const matched = trajectoryPersonByInput(value);
  if (matched) return String(matched.person_id);
  if (/^[1-9]\d*$/.test(value)) return value;
  throw new Error("请输入有效的人员编号，或从候选项中选择。");
}

function trajectoryPersonLabel(personId) {
  const person = trajectoryState.people.find((item) => String(item.person_id) === String(personId));
  if (!person) return `人员 ${personId}`;
  const identity = person.name || person.external_person_id || `人员 ${personId}`;
  return `${identity}（ID ${personId}）`;
}

function renderTrajectoryLookupOptions() {
  if (trajectoryDom.personOptions) {
    const options = [];
    for (const person of trajectoryState.people) {
      const personId = String(person.person_id || "");
      const name = person.name || "未命名人员";
      const externalId = String(person.external_person_id || "");
      options.push(
        `<option value="${trajectoryEscapeHtml(personId)}" label="${trajectoryEscapeHtml(`${name} / ${externalId || "无人员编号"}`)}"></option>`
      );
      if (externalId) {
        options.push(
          `<option value="${trajectoryEscapeHtml(externalId)}" label="${trajectoryEscapeHtml(`${name} / 人员编号 ${externalId}`)}"></option>`
        );
      }
    }
    trajectoryDom.personOptions.innerHTML = options.join("");
  }

  if (trajectoryDom.cameraId) {
    const selected = trajectoryDom.cameraId.value;
    const options = trajectoryState.cameras.map((camera) => {
      const cameraId = String(camera.id || camera.camera_id || "");
      const sourceId = String(camera.source_id || "");
      const name = camera.name || sourceId || cameraId || "未命名摄像头";
      const suffix = sourceId && sourceId !== name ? ` / ${sourceId}` : "";
      return `<option value="${trajectoryEscapeHtml(cameraId)}">${trajectoryEscapeHtml(name + suffix)}</option>`;
    });
    trajectoryDom.cameraId.innerHTML =
      `<option value="">全部摄像头</option>${options.join("")}`;
    if ([...trajectoryDom.cameraId.options].some((option) => option.value === selected)) {
      trajectoryDom.cameraId.value = selected;
    }
  }
}

async function loadTrajectoryLookups() {
  const [peopleData, cameraData] = await Promise.all([
    trajectoryRequest("/api/v1/people?limit=200"),
    trajectoryRequest("/api/v1/cameras"),
  ]);
  trajectoryState.people = Array.isArray(peopleData)
    ? peopleData
    : (peopleData.people || []);
  trajectoryState.cameras = Array.isArray(cameraData)
    ? cameraData
    : (cameraData.cameras || []);
  renderTrajectoryLookupOptions();
}

function trajectoryEpochMs(input, label) {
  const value = String(input?.value || "").trim();
  if (!value) return null;
  const epoch = new Date(value).getTime();
  if (!Number.isFinite(epoch)) throw new Error(`${label}不是有效时间。`);
  return Math.trunc(epoch);
}

function trajectoryQuery(offset) {
  const personId = resolveTrajectoryPersonId(trajectoryDom.personId?.value);
  const startTsMs = trajectoryEpochMs(trajectoryDom.startTime, "开始时间");
  const endTsMs = trajectoryEpochMs(trajectoryDom.endTime, "结束时间");
  if (startTsMs !== null && endTsMs !== null && startTsMs > endTsMs) {
    throw new Error("开始时间不能晚于结束时间。");
  }

  const params = new URLSearchParams({
    min_similarity: String(TRAJECTORY_MIN_SIMILARITY),
    include_unregistered_sources: "true",
    limit: String(TRAJECTORY_PAGE_SIZE),
    offset: String(offset),
  });
  const cameraId = String(trajectoryDom.cameraId?.value || "").trim();
  if (cameraId) params.set("camera_id", cameraId);
  if (startTsMs !== null) params.set("start_ts_ms", String(startTsMs));
  if (endTsMs !== null) params.set("end_ts_ms", String(endTsMs));
  return { personId, cameraId, startTsMs, endTsMs, params };
}

function clearTrajectoryDetail(message = "请选择一条轨迹记录") {
  trajectoryState.selectedIndex = -1;
  if (trajectoryDom.detailImage) {
    trajectoryDom.detailImage.hidden = true;
    trajectoryDom.detailImage.removeAttribute("src");
    delete trajectoryDom.detailImage.dataset.fallbackUrl;
    delete trajectoryDom.detailImage.dataset.fallbackTried;
  }
  if (trajectoryDom.detailEmpty) {
    trajectoryDom.detailEmpty.hidden = false;
    trajectoryDom.detailEmpty.textContent = message;
  }
  if (trajectoryDom.detail) trajectoryDom.detail.textContent = "暂无选中的轨迹记录。";
}

function renderTrajectoryDetail(row, index) {
  if (!row) {
    clearTrajectoryDetail();
    return;
  }
  trajectoryState.selectedIndex = index;
  trajectoryDom.list?.querySelectorAll(".trajectory-result-card").forEach((card) => {
    card.classList.toggle("active", Number(card.dataset.index) === index);
  });

  const previewUrl = trajectoryPreviewUrl(row);
  const thumbnailUrl = trajectoryThumbnailUrl(row);
  if (trajectoryDom.detailImage && previewUrl) {
    trajectoryDom.detailImage.hidden = false;
    trajectoryDom.detailImage.alt = `${trajectoryCameraName(row)} 轨迹画面`;
    trajectoryDom.detailImage.dataset.fallbackUrl = previewUrl !== thumbnailUrl ? thumbnailUrl : "";
    trajectoryDom.detailImage.dataset.fallbackTried = "false";
    trajectoryDom.detailImage.src = previewUrl;
    if (trajectoryDom.detailEmpty) trajectoryDom.detailEmpty.hidden = true;
  } else {
    if (trajectoryDom.detailImage) {
      trajectoryDom.detailImage.hidden = true;
      trajectoryDom.detailImage.removeAttribute("src");
    }
    if (trajectoryDom.detailEmpty) {
      trajectoryDom.detailEmpty.hidden = false;
      trajectoryDom.detailEmpty.textContent = "该记录没有可显示的轨迹图片";
    }
  }

  if (trajectoryDom.detail) {
    const personName = row.person_name || trajectoryPersonLabel(row.person_id || trajectoryState.personId);
    trajectoryDom.detail.innerHTML =
      `<h3>${trajectoryEscapeHtml(personName)}</h3>` +
      `<dl class="trajectory-detail-grid">` +
        `<dt>系统编号</dt><dd>${trajectoryEscapeHtml(row.person_id || trajectoryState.personId || "--")}</dd>` +
        `<dt>人员编号</dt><dd>${trajectoryEscapeHtml(row.external_person_id || "--")}</dd>` +
        `<dt>摄像头</dt><dd>${trajectoryEscapeHtml(trajectoryCameraName(row))}</dd>` +
        `<dt>出现时间</dt><dd>${trajectoryEscapeHtml(trajectoryFormatTime(trajectoryTimestamp(row)))}</dd>` +
        `<dt>匹配相似度</dt><dd>${trajectoryEscapeHtml(trajectoryFormatPercent(row.similarity))}</dd>` +
        `<dt>轨迹来源</dt><dd>${trajectoryEscapeHtml(trajectorySourceLabel(row.trajectory_source))}</dd>` +
      `</dl>`;
  }
}

function renderTrajectoryRows() {
  const rows = trajectoryState.rows;
  if (!trajectoryDom.list) return;
  if (!rows.length) {
    trajectoryDom.list.innerHTML = `<div class="empty-state">当前筛选条件下没有轨迹记录。</div>`;
    clearTrajectoryDetail("当前筛选条件下没有轨迹图片");
    return;
  }

  trajectoryDom.list.innerHTML = rows.map((row, index) => {
    const thumbnailUrl = trajectoryThumbnailUrl(row);
    const thumbnail = thumbnailUrl
      ? `<img src="${trajectoryEscapeHtml(thumbnailUrl)}" alt="轨迹缩略图" loading="lazy" decoding="async" />`
      : `<span class="trajectory-result-thumb-empty">无图片</span>`;
    const personName = row.person_name || trajectoryPersonLabel(row.person_id || trajectoryState.personId);
    return (
      `<button type="button" class="trajectory-result-card" data-index="${index}">` +
        `<span class="trajectory-result-thumb">${thumbnail}</span>` +
        `<span class="trajectory-result-copy">` +
          `<strong>${trajectoryEscapeHtml(trajectoryCameraName(row))}</strong>` +
          `<span>${trajectoryEscapeHtml(trajectoryFormatTime(trajectoryTimestamp(row)))}</span>` +
          `<span>${trajectoryEscapeHtml(personName)} · ${trajectoryEscapeHtml(trajectoryFormatPercent(row.similarity))}</span>` +
          `<small>${trajectoryEscapeHtml(trajectorySourceLabel(row.trajectory_source))}</small>` +
        `</span>` +
      `</button>`
    );
  }).join("");

  trajectoryDom.list.querySelectorAll(".trajectory-result-card").forEach((card) => {
    card.addEventListener("click", () => {
      renderTrajectoryDetail(rows[Number(card.dataset.index)], Number(card.dataset.index));
    });
  });
  renderTrajectoryDetail(rows[0], 0);
}

function renderTrajectoryPagination() {
  const first = trajectoryState.rows.length ? trajectoryState.offset + 1 : 0;
  const last = trajectoryState.offset + trajectoryState.rows.length;
  if (trajectoryDom.pageStatus) {
    trajectoryDom.pageStatus.textContent = trajectoryState.rows.length
      ? `第 ${first}-${last} 条`
      : "无查询结果";
  }
  if (trajectoryDom.previous) trajectoryDom.previous.disabled = trajectoryState.offset <= 0;
  if (trajectoryDom.next) trajectoryDom.next.disabled = !trajectoryState.hasMore;
}

function renderTrajectorySummary(query) {
  if (!trajectoryDom.summary) return;
  const filters = [trajectoryPersonLabel(query.personId)];
  if (query.cameraId) {
    const camera = trajectoryState.cameras.find((item) => (
      String(item.id || item.camera_id || "") === query.cameraId
    ));
    filters.push(camera?.name || query.cameraId);
  }
  if (query.startTsMs !== null) filters.push(`从 ${trajectoryFormatTime(query.startTsMs)}`);
  if (query.endTsMs !== null) filters.push(`到 ${trajectoryFormatTime(query.endTsMs)}`);
  trajectoryDom.summary.textContent = `${filters.join(" · ")}；本页 ${trajectoryState.rows.length} 条。`;
}

async function searchTrajectory({ offset = 0 } = {}) {
  const normalizedOffset = Math.max(0, Number.parseInt(offset, 10) || 0);
  const query = trajectoryQuery(normalizedOffset);
  const requestId = ++trajectoryState.requestId;
  if (trajectoryDom.search) trajectoryDom.search.disabled = true;
  if (trajectoryDom.refresh) trajectoryDom.refresh.disabled = true;
  if (trajectoryDom.summary) trajectoryDom.summary.textContent = "正在查询轨迹…";
  try {
    const data = await trajectoryRequest(
      `/api/v1/people/${encodeURIComponent(query.personId)}/trajectory?${query.params.toString()}`
    );
    if (requestId !== trajectoryState.requestId) return;
    trajectoryState.rows = Array.isArray(data.trajectory) ? data.trajectory : [];
    trajectoryState.personId = query.personId;
    trajectoryState.offset = normalizedOffset;
    trajectoryState.hasMore = data.has_more === true ||
      (data.has_more === undefined && trajectoryState.rows.length === TRAJECTORY_PAGE_SIZE);
    renderTrajectoryRows();
    renderTrajectoryPagination();
    renderTrajectorySummary(query);
    trajectorySetStatus(trajectoryState.rows.length ? "轨迹查询完成" : "未找到轨迹");
  } catch (error) {
    if (requestId === trajectoryState.requestId && trajectoryDom.summary) {
      trajectoryDom.summary.textContent = `轨迹查询失败：${error?.message || "未知错误"}`;
    }
    throw error;
  } finally {
    if (requestId === trajectoryState.requestId) {
      if (trajectoryDom.search) trajectoryDom.search.disabled = false;
      if (trajectoryDom.refresh) trajectoryDom.refresh.disabled = false;
    }
  }
}

function resetTrajectoryPage() {
  trajectoryState.requestId += 1;
  trajectoryState.rows = [];
  trajectoryState.personId = "";
  trajectoryState.offset = 0;
  trajectoryState.hasMore = false;
  trajectoryDom.form?.reset();
  if (trajectoryDom.list) {
    trajectoryDom.list.innerHTML = `<div class="empty-state">请先按人员编号查询轨迹。</div>`;
  }
  if (trajectoryDom.pageStatus) trajectoryDom.pageStatus.textContent = "尚未查询";
  if (trajectoryDom.previous) trajectoryDom.previous.disabled = true;
  if (trajectoryDom.next) trajectoryDom.next.disabled = true;
  if (trajectoryDom.search) trajectoryDom.search.disabled = false;
  if (trajectoryDom.refresh) trajectoryDom.refresh.disabled = false;
  if (trajectoryDom.summary) {
    trajectoryDom.summary.textContent = "输入人员编号后查询，系统会按时间倒序显示轨迹画面。";
  }
  clearTrajectoryDetail();
}

function bindTrajectoryEvents() {
  if (trajectoryState.eventsBound) return;
  trajectoryState.eventsBound = true;
  trajectoryDom.form?.addEventListener("submit", (event) => {
    event.preventDefault();
    searchTrajectory({ offset: 0 }).catch(trajectoryReportError);
  });
  trajectoryDom.reset?.addEventListener("click", resetTrajectoryPage);
  trajectoryDom.refresh?.addEventListener("click", () => {
    searchTrajectory({ offset: trajectoryState.offset }).catch(trajectoryReportError);
  });
  trajectoryDom.previous?.addEventListener("click", () => {
    searchTrajectory({ offset: Math.max(0, trajectoryState.offset - TRAJECTORY_PAGE_SIZE) })
      .catch(trajectoryReportError);
  });
  trajectoryDom.next?.addEventListener("click", () => {
    searchTrajectory({ offset: trajectoryState.offset + TRAJECTORY_PAGE_SIZE })
      .catch(trajectoryReportError);
  });
  trajectoryDom.detailImage?.addEventListener("error", () => {
    const fallbackUrl = trajectoryDom.detailImage.dataset.fallbackUrl || "";
    const tried = trajectoryDom.detailImage.dataset.fallbackTried === "true";
    if (fallbackUrl && !tried) {
      trajectoryDom.detailImage.dataset.fallbackTried = "true";
      trajectoryDom.detailImage.src = fallbackUrl;
      return;
    }
    trajectoryDom.detailImage.hidden = true;
    if (trajectoryDom.detailEmpty) {
      trajectoryDom.detailEmpty.hidden = false;
      trajectoryDom.detailEmpty.textContent = "轨迹图片加载失败，请刷新后重试";
    }
  });
}

async function runTrajectoryInit() {
  bindTrajectoryEvents();
  await loadTrajectoryLookups();
  trajectoryState.initialized = true;
}

async function initTrajectoryPage() {
  if (trajectoryState.initialized) return;
  if (trajectoryState.initPromise) return trajectoryState.initPromise;
  trajectoryState.initPromise = runTrajectoryInit().finally(() => {
    trajectoryState.initPromise = null;
  });
  return trajectoryState.initPromise;
}

async function openTrajectoryForPerson(personId, options = {}) {
  const targetId = String(personId || "").trim();
  if (!targetId) throw new Error("未找到可查询的人员编号。");
  if (typeof activateTopView === "function") activateTopView("trajectory", true);
  await initTrajectoryPage();
  trajectoryDom.personId.value = targetId;
  if (options.startTime) trajectoryDom.startTime.value = options.startTime;
  if (options.endTime) trajectoryDom.endTime.value = options.endTime;
  if (options.cameraId) trajectoryDom.cameraId.value = options.cameraId;
  await searchTrajectory({ offset: 0 });
}

window.operatorTrajectory = {
  init: initTrajectoryPage,
  openForPerson: openTrajectoryForPerson,
  reload: async function reload() {
    await loadTrajectoryLookups();
    if (trajectoryDom.personId?.value.trim()) {
      await searchTrajectory({ offset: trajectoryState.offset });
    }
  },
};

if (window.location.hash === "#trajectory" || trajectoryDom.view?.hidden === false) {
  initTrajectoryPage().catch(trajectoryReportError);
}
