/* ------------------------------------------------------------------ */
/*  Registered-person trajectory page                                 */
/*  Persisted, DB-backed hits only; images render inline on the page.   */
/*  Person-centric layout: profile + route banner, a day-grouped        */
/*  sighting timeline and a sticky preview stage.                       */
/* ------------------------------------------------------------------ */

const TRAJECTORY_PAGE_SIZE = 50;
const TRAJECTORY_MIN_SIMILARITY = 0.6;
const TRAJECTORY_CAMERA_COLORS = 8;
const TRAJECTORY_ROUTE_MAX_STOPS = 12;
// A pause this long between two sightings is called out in the timeline.
const TRAJECTORY_GAP_NOTICE_MS = 30 * 60 * 1000;

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
  profile: document.getElementById("trajectory-profile"),
  list: document.getElementById("trajectory-list"),
  detailImage: document.getElementById("trajectory-detail-image"),
  detailEmpty: document.getElementById("trajectory-detail-empty"),
  stageTag: document.getElementById("trajectory-stage-tag"),
  newerItem: document.getElementById("trajectory-newer-item"),
  olderItem: document.getElementById("trajectory-older-item"),
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

const TRAJECTORY_PROFILE_PLACEHOLDER = trajectoryDom.profile?.innerHTML || "";

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

function trajectoryEpochOf(row) {
  const value = trajectoryTimestamp(row);
  if (value === undefined || value === null || value === "") return null;
  const numeric = Number(value);
  const epoch = Number.isFinite(numeric) ? numeric : new Date(String(value)).getTime();
  return Number.isFinite(epoch) ? epoch : null;
}

function trajectoryFormatTime(value) {
  if (value === undefined || value === null || value === "") return "--";
  const numeric = Number(value);
  const date = Number.isFinite(numeric) ? new Date(numeric) : new Date(String(value));
  return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString();
}

function trajectoryPad(number) {
  return String(number).padStart(2, "0");
}

function trajectoryClock(epoch) {
  if (epoch === null) return "--:--";
  const date = new Date(epoch);
  return `${trajectoryPad(date.getHours())}:${trajectoryPad(date.getMinutes())}:${trajectoryPad(date.getSeconds())}`;
}

function trajectoryShortDateTime(epoch) {
  if (epoch === null) return "--";
  const date = new Date(epoch);
  return `${date.getMonth() + 1}/${date.getDate()} ${trajectoryPad(date.getHours())}:${trajectoryPad(date.getMinutes())}`;
}

function trajectoryDayKey(epoch) {
  if (epoch === null) return "unknown";
  const date = new Date(epoch);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function trajectoryDayLabel(epoch) {
  if (epoch === null) return "时间未知";
  const date = new Date(epoch);
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  const weekday = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][date.getDay()];
  const label = `${date.getMonth() + 1}月${date.getDate()}日 ${weekday}`;
  if (trajectoryDayKey(epoch) === trajectoryDayKey(today.getTime())) return `今天 · ${label}`;
  if (trajectoryDayKey(epoch) === trajectoryDayKey(yesterday.getTime())) return `昨天 · ${label}`;
  return label;
}

function trajectoryDuration(ms) {
  const minutes = Math.max(0, Math.round(ms / 60000));
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
  const days = Math.floor(hours / 24);
  return `${days} 天 ${hours % 24} 小时`;
}

function trajectoryRelative(epoch) {
  if (epoch === null) return "";
  const delta = Date.now() - epoch;
  if (delta < 0) return "";
  if (delta < 60 * 1000) return "刚刚";
  return `${trajectoryDuration(delta)}前`;
}

function trajectoryRelativeShort(epoch) {
  // Timeline column variant: one unit only so it fits beside the clock.
  if (epoch === null) return "";
  const delta = Date.now() - epoch;
  if (delta < 0) return "";
  const minutes = Math.floor(delta / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function trajectoryFormatPercent(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${Math.round(numeric * 100)}%` : "--";
}

function trajectorySimilarityTone(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "low";
  if (numeric >= 0.8) return "high";
  if (numeric >= 0.7) return "mid";
  return "low";
}

function trajectorySimilarityBar(value, extraClass = "") {
  const numeric = Number(value);
  const pct = Number.isFinite(numeric) ? Math.max(0, Math.min(100, Math.round(numeric * 100))) : 0;
  return (
    `<span class="similarity-bar ${extraClass}" data-tone="${trajectorySimilarityTone(value)}">` +
      `<i style="width:${pct}%"></i>` +
    `</span>`
  );
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

function trajectorySourceTone(source) {
  const tones = {
    watchlist_event: "alert",
    live_search_hit: "observation",
    gallery_observation: "observation",
  };
  return tones[source] || "neutral";
}

function trajectoryCameraName(row) {
  return row?.camera_name || row?.source_id || row?.camera_id || "未知摄像头";
}

function trajectoryCameraKey(row) {
  return String(row?.camera_id || row?.source_id || row?.camera_name || "unknown");
}

function trajectoryCameraColor(row) {
  // Stable per-camera colour so a timeline node can be matched to its stop
  // on the route strip at a glance.
  const key = trajectoryCameraKey(row);
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return `var(--cam-${(hash % TRAJECTORY_CAMERA_COLORS) + 1})`;
}

function trajectoryPersonByInput(rawValue) {
  const value = String(rawValue || "").trim();
  if (!value) return null;
  return trajectoryState.people.find((person) => (
    String(person.person_id) === value ||
    String(person.external_person_id || "") === value
  )) || null;
}

function trajectoryPersonById(personId) {
  return trajectoryState.people.find((item) => String(item.person_id) === String(personId)) || null;
}

function resolveTrajectoryPersonId(rawValue) {
  const value = String(rawValue || "").trim();
  const matched = trajectoryPersonByInput(value);
  if (matched) return String(matched.person_id);
  if (/^[1-9]\d*$/.test(value)) return value;
  throw new Error("请输入有效的人员编号，或从候选项中选择。");
}

function trajectoryPersonLabel(personId) {
  const person = trajectoryPersonById(personId);
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

function updateTrajectoryStageNav() {
  const index = trajectoryState.selectedIndex;
  const count = trajectoryState.rows.length;
  if (trajectoryDom.newerItem) trajectoryDom.newerItem.disabled = index <= 0;
  if (trajectoryDom.olderItem) trajectoryDom.olderItem.disabled = index < 0 || index >= count - 1;
}

function clearTrajectoryDetail(message = "请选择一条轨迹记录") {
  trajectoryState.selectedIndex = -1;
  if (trajectoryDom.detailImage) {
    trajectoryDom.detailImage.hidden = true;
    trajectoryDom.detailImage.removeAttribute("src");
    delete trajectoryDom.detailImage.dataset.fallbackUrl;
    delete trajectoryDom.detailImage.dataset.fallbackTried;
  }
  if (trajectoryDom.stageTag) trajectoryDom.stageTag.hidden = true;
  if (trajectoryDom.detailEmpty) {
    trajectoryDom.detailEmpty.hidden = false;
    trajectoryDom.detailEmpty.textContent = message;
  }
  if (trajectoryDom.detail) trajectoryDom.detail.textContent = "暂无选中的轨迹记录。";
  updateTrajectoryStageNav();
}

function renderTrajectoryDetail(row, index, options = {}) {
  if (!row) {
    clearTrajectoryDetail();
    return;
  }
  trajectoryState.selectedIndex = index;
  trajectoryDom.list?.querySelectorAll(".trajectory-result-card").forEach((card) => {
    const active = Number(card.dataset.index) === index;
    card.classList.toggle("active", active);
    card.setAttribute("aria-current", active ? "true" : "false");
    if (active && options.scroll) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  updateTrajectoryStageNav();

  const epoch = trajectoryEpochOf(row);
  const cameraName = trajectoryCameraName(row);
  const previewUrl = trajectoryPreviewUrl(row);
  const thumbnailUrl = trajectoryThumbnailUrl(row);
  if (trajectoryDom.detailImage && previewUrl) {
    trajectoryDom.detailImage.hidden = false;
    trajectoryDom.detailImage.alt = `${cameraName} 轨迹画面`;
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

  if (trajectoryDom.stageTag) {
    trajectoryDom.stageTag.hidden = false;
    trajectoryDom.stageTag.style.setProperty("--cam-color", trajectoryCameraColor(row));
    trajectoryDom.stageTag.innerHTML =
      `<i aria-hidden="true"></i>` +
      `<span>${trajectoryEscapeHtml(cameraName)}</span>` +
      `<span class="trajectory-stage-tag-time">${trajectoryEscapeHtml(trajectoryShortDateTime(epoch))}</span>`;
  }

  if (trajectoryDom.detail) {
    const personName = row.person_name || trajectoryPersonLabel(row.person_id || trajectoryState.personId);
    const relative = trajectoryRelative(epoch);
    const face = thumbnailUrl
      ? `<img class="trajectory-detail-face" src="${trajectoryEscapeHtml(thumbnailUrl)}" alt="人脸" loading="lazy" decoding="async" />`
      : `<span class="trajectory-detail-face trajectory-detail-face--empty" aria-hidden="true"></span>`;
    trajectoryDom.detail.innerHTML =
      `<div class="trajectory-detail-head" style="--cam-color:${trajectoryCameraColor(row)}">` +
        face +
        `<div class="trajectory-detail-heading">` +
          `<h3>${trajectoryEscapeHtml(cameraName)}</h3>` +
          `<p>${trajectoryEscapeHtml(trajectoryFormatTime(trajectoryTimestamp(row)))}` +
            `${relative ? ` · ${trajectoryEscapeHtml(relative)}` : ""}</p>` +
        `</div>` +
        `<span class="badge ${trajectorySourceTone(row.trajectory_source)}">${trajectoryEscapeHtml(trajectorySourceLabel(row.trajectory_source))}</span>` +
      `</div>` +
      `<div class="similarity-meter">` +
        `<span>匹配相似度</span>` +
        trajectorySimilarityBar(row.similarity, "similarity-bar--large") +
        `<strong>${trajectoryEscapeHtml(trajectoryFormatPercent(row.similarity))}</strong>` +
      `</div>` +
      `<dl class="trajectory-detail-grid">` +
        `<dt>人员</dt><dd>${trajectoryEscapeHtml(personName)}</dd>` +
        `<dt>人员编号</dt><dd>${trajectoryEscapeHtml(row.external_person_id || "--")}</dd>` +
        `<dt>系统编号</dt><dd>${trajectoryEscapeHtml(row.person_id || trajectoryState.personId || "--")}</dd>` +
        `<dt>记录位置</dt><dd>第 ${trajectoryState.offset + index + 1} 条</dd>` +
      `</dl>`;
  }
}

function selectTrajectoryIndex(index, options = {}) {
  const rows = trajectoryState.rows;
  if (!rows.length) return;
  const bounded = Math.max(0, Math.min(rows.length - 1, index));
  renderTrajectoryDetail(rows[bounded], bounded, options);
}

function trajectoryTimelineItem(row, index) {
  const epoch = trajectoryEpochOf(row);
  const thumbnailUrl = trajectoryThumbnailUrl(row);
  const thumbnail = thumbnailUrl
    ? `<img src="${trajectoryEscapeHtml(thumbnailUrl)}" alt="轨迹缩略图" loading="lazy" decoding="async" />`
    : `<span class="trajectory-result-thumb-empty">无图片</span>`;
  return (
    `<button type="button" class="trajectory-result-card" data-index="${index}" style="--cam-color:${trajectoryCameraColor(row)}">` +
      `<span class="trajectory-result-time">` +
        `<strong>${trajectoryEscapeHtml(trajectoryClock(epoch))}</strong>` +
        `<small>${trajectoryEscapeHtml(trajectoryRelativeShort(epoch))}</small>` +
      `</span>` +
      `<span class="trajectory-node" aria-hidden="true"></span>` +
      `<span class="trajectory-result-thumb">${thumbnail}</span>` +
      `<span class="trajectory-result-copy">` +
        `<strong>${trajectoryEscapeHtml(trajectoryCameraName(row))}</strong>` +
        `<span class="similarity">${trajectorySimilarityBar(row.similarity)}${trajectoryEscapeHtml(trajectoryFormatPercent(row.similarity))}</span>` +
        `<small class="badge ${trajectorySourceTone(row.trajectory_source)}">${trajectoryEscapeHtml(trajectorySourceLabel(row.trajectory_source))}</small>` +
      `</span>` +
    `</button>`
  );
}

function renderTrajectoryRows() {
  const rows = trajectoryState.rows;
  if (!trajectoryDom.list) return;
  if (!rows.length) {
    trajectoryDom.list.innerHTML = `<div class="empty-state">当前筛选条件下没有轨迹记录。</div>`;
    clearTrajectoryDetail("当前筛选条件下没有轨迹图片");
    return;
  }

  const dayCounts = new Map();
  for (const row of rows) {
    const key = trajectoryDayKey(trajectoryEpochOf(row));
    dayCounts.set(key, (dayCounts.get(key) || 0) + 1);
  }

  const parts = [];
  let previousDay = null;
  let previousEpoch = null;
  rows.forEach((row, index) => {
    const epoch = trajectoryEpochOf(row);
    const day = trajectoryDayKey(epoch);
    if (day !== previousDay) {
      parts.push(
        `<div class="timeline-day"><span>${trajectoryEscapeHtml(trajectoryDayLabel(epoch))}</span>` +
        `<small>${dayCounts.get(day)} 次出现</small></div>`
      );
      previousDay = day;
    } else if (previousEpoch !== null && epoch !== null && previousEpoch - epoch >= TRAJECTORY_GAP_NOTICE_MS) {
      parts.push(`<div class="trajectory-gap">间隔 ${trajectoryEscapeHtml(trajectoryDuration(previousEpoch - epoch))}</div>`);
    }
    parts.push(trajectoryTimelineItem(row, index));
    previousEpoch = epoch;
  });
  trajectoryDom.list.innerHTML = parts.join("");

  trajectoryDom.list.querySelectorAll(".trajectory-result-card").forEach((card) => {
    card.addEventListener("click", () => {
      selectTrajectoryIndex(Number(card.dataset.index));
    });
  });
  renderTrajectoryDetail(rows[0], 0);
}

function trajectoryRouteStops(rows) {
  // Rows arrive newest first; the route reads oldest -> newest and merges
  // consecutive sightings at the same camera into one stop.
  const stops = [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index];
    const key = trajectoryCameraKey(row);
    const last = stops[stops.length - 1];
    if (last && last.key === key) {
      last.count += 1;
      last.lastIndex = index;
      last.row = row;
      continue;
    }
    stops.push({ key, row, count: 1, firstIndex: index, lastIndex: index });
  }
  return stops;
}

function renderTrajectoryProfile(query) {
  if (!trajectoryDom.profile) return;
  const rows = trajectoryState.rows;
  const person = trajectoryPersonById(query.personId);
  const firstRow = rows[0] || {};
  const name = person?.name || firstRow.person_name || `人员 ${query.personId}`;
  const externalId = person?.external_person_id || firstRow.external_person_id || "";
  const avatarUrl = person?.primary_registered_crop_url || trajectoryThumbnailUrl(firstRow);
  const avatar = avatarUrl
    ? `<img src="${trajectoryEscapeHtml(avatarUrl)}" alt="${trajectoryEscapeHtml(name)}" decoding="async" />`
    : trajectoryEscapeHtml(String(name).slice(0, 1));

  const epochs = rows.map(trajectoryEpochOf).filter((value) => value !== null);
  const newest = epochs.length ? Math.max(...epochs) : null;
  const oldest = epochs.length ? Math.min(...epochs) : null;
  const cameraCounts = new Map();
  for (const row of rows) {
    const key = trajectoryCameraKey(row);
    const entry = cameraCounts.get(key) || { name: trajectoryCameraName(row), count: 0 };
    entry.count += 1;
    cameraCounts.set(key, entry);
  }
  const busiest = [...cameraCounts.values()].sort((a, b) => b.count - a.count)[0];
  const span = newest !== null && oldest !== null
    ? `${trajectoryShortDateTime(oldest)} – ${trajectoryShortDateTime(newest)}`
    : "--";

  const stops = trajectoryRouteStops(rows);
  const hidden = Math.max(0, stops.length - TRAJECTORY_ROUTE_MAX_STOPS);
  const shown = stops.slice(-TRAJECTORY_ROUTE_MAX_STOPS);
  const routeItems = shown.map((stop, position) => {
    const epoch = trajectoryEpochOf(stop.row);
    const isLast = position === shown.length - 1;
    const isFirst = position === 0 && hidden === 0;
    const marker = isLast ? `<em>本页最新</em>` : (isFirst ? `<em>本页最早</em>` : "");
    return (
      (position ? `<li class="route-link" aria-hidden="true"></li>` : "") +
      `<li class="trajectory-route-step">` +
        `<button type="button" class="route-stop" data-index="${stop.lastIndex}" title="${trajectoryEscapeHtml(trajectoryCameraName(stop.row))}" style="--cam-color:${trajectoryCameraColor(stop.row)}">` +
          `<span class="route-dot" aria-hidden="true"></span>` +
          `<span class="route-stop-copy"><strong>${trajectoryEscapeHtml(trajectoryCameraName(stop.row))}</strong>` +
          `<small>${trajectoryEscapeHtml(trajectoryShortDateTime(epoch))}${stop.count > 1 ? ` · ${stop.count} 次` : ""}</small></span>` +
          marker +
        `</button>` +
      `</li>`
    );
  }).join("");

  trajectoryDom.profile.innerHTML =
    `<div class="trajectory-profile-grid">` +
      `<div class="trajectory-person">` +
        `<span class="trajectory-avatar">${avatar}</span>` +
        `<div class="trajectory-person-copy">` +
          `<h2 title="${trajectoryEscapeHtml(name)}">${trajectoryEscapeHtml(name)}</h2>` +
          `<p>${externalId ? `人员编号 ${trajectoryEscapeHtml(externalId)} · ` : ""}系统编号 ${trajectoryEscapeHtml(query.personId)}</p>` +
          (person?.description ? `<span class="badge alert">${trajectoryEscapeHtml(person.description)}</span>` : "") +
        `</div>` +
      `</div>` +
      `<div class="trajectory-stats">` +
        `<div class="trajectory-stat"><strong>${rows.length}</strong><span>本页出现次数</span></div>` +
        `<div class="trajectory-stat"><strong>${cameraCounts.size}</strong><span>本页经过摄像头</span></div>` +
        `<div class="trajectory-stat"><strong>${trajectoryEscapeHtml(busiest?.name || "--")}</strong><span>本页最常出现${busiest ? `（${busiest.count} 次）` : ""}</span></div>` +
        `<div class="trajectory-stat"><strong>${trajectoryEscapeHtml(newest !== null ? trajectoryRelative(newest) || trajectoryShortDateTime(newest) : "--")}</strong><span>本页最近一次出现</span></div>` +
      `</div>` +
      (rows.length
        ? `<div class="trajectory-route">` +
            `<div class="trajectory-route-label"><span>本页路线 · ${trajectoryEscapeHtml(span)}（按时间先后，相邻同一摄像头合并）</span>` +
            `${hidden ? `<small>本页更早还有 ${hidden} 站</small>` : ""}</div>` +
            `<ol class="trajectory-route-steps">${routeItems}</ol>` +
          `</div>`
        : "") +
    `</div>`;

  trajectoryDom.profile.querySelectorAll(".route-stop").forEach((button) => {
    button.addEventListener("click", () => {
      selectTrajectoryIndex(Number(button.dataset.index), { scroll: true });
    });
  });
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
    renderTrajectoryProfile(query);
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
  if (trajectoryDom.profile) trajectoryDom.profile.innerHTML = TRAJECTORY_PROFILE_PLACEHOLDER;
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

function trajectoryKeyboardTarget(event) {
  const target = event.target;
  if (!(target instanceof Element)) return true;
  return !target.closest("input, select, textarea, [contenteditable='true']");
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
  trajectoryDom.newerItem?.addEventListener("click", () => {
    selectTrajectoryIndex(trajectoryState.selectedIndex - 1, { scroll: true });
  });
  trajectoryDom.olderItem?.addEventListener("click", () => {
    selectTrajectoryIndex(trajectoryState.selectedIndex + 1, { scroll: true });
  });
  document.addEventListener("keydown", (event) => {
    if (trajectoryDom.view?.hidden || !trajectoryState.rows.length) return;
    if (event.altKey || event.ctrlKey || event.metaKey || !trajectoryKeyboardTarget(event)) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      selectTrajectoryIndex(trajectoryState.selectedIndex + step, { scroll: true });
    }
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
