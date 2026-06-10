#!/usr/bin/env python3
"""Generate a static HTML preview for an evidence overlay bundle.

The generated page reads only files inside the evidence bundle:
``raw_clip.mov``, ``metadata.json``, ``sink_metadata.json``,
``annotations.jsonl``, and ``summary.json``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_EVIDENCE_ROOT = Path("/data/video-analytics/media/evidence")
REQUIRED_FILES = (
    "raw_clip.mov",
    "metadata.json",
    "sink_metadata.json",
    "annotations.jsonl",
    "summary.json",
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} invalid JSONL: {exc}") from exc
            if isinstance(data, dict):
                rows.append(data)
    return rows


def _load_sink_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    return _load_jsonl(path)


def _first_sink_frame(path: Path) -> dict[str, Any]:
    records = _load_sink_records(path)
    for record in records:
        if record.get("pts") is not None:
            return record
    raise ValueError(f"{path} has no frame record with pts")


def _latest_bundle() -> Path | None:
    if not DEFAULT_EVIDENCE_ROOT.is_dir():
        return None
    candidates = []
    for path in DEFAULT_EVIDENCE_ROOT.iterdir():
        if not path.is_dir():
            continue
        if all((path / name).is_file() for name in REQUIRED_FILES):
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _validate_bundle(bundle_dir: Path) -> dict[str, Any]:
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle dir not found: {bundle_dir}")

    missing = [name for name in REQUIRED_FILES if not (bundle_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"bundle missing required files: {', '.join(missing)}"
        )

    metadata = _load_json(bundle_dir / "metadata.json")
    summary = _load_json(bundle_dir / "summary.json")
    annotations = _load_jsonl(bundle_dir / "annotations.jsonl")
    first_frame = _first_sink_frame(bundle_dir / "sink_metadata.json")

    if not annotations:
        raise ValueError("annotations.jsonl contains no annotation lines")

    return {
        "metadata": metadata,
        "summary": summary,
        "first_frame": first_frame,
        "annotation_lines": len(annotations),
    }


def _html_template() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>C1F.4b Evidence Overlay Preview</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #101418;
      color: #edf2f7;
    }
    body {
      margin: 0;
      min-height: 100vh;
      background: #101418;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid #28313c;
      background: #151b21;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .status {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 13px;
      color: #cbd5e1;
    }
    .badge {
      border: 1px solid #3a4654;
      border-radius: 6px;
      padding: 4px 7px;
      background: #1c242d;
      white-space: nowrap;
    }
    .warning {
      color: #ffd166;
      border-color: #876a1d;
      background: #332a13;
    }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 330px;
      gap: 16px;
      padding: 16px;
    }
    .stage {
      min-width: 0;
    }
    .video-wrap {
      position: relative;
      width: 100%;
      background: #05070a;
      border: 1px solid #28313c;
      border-radius: 8px;
      overflow: hidden;
    }
    video {
      display: block;
      width: 100%;
      height: auto;
      background: #05070a;
    }
    canvas {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }
    aside {
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
    }
    .panel {
      border: 1px solid #28313c;
      border-radius: 8px;
      background: #151b21;
      padding: 12px;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 14px;
      font-weight: 650;
      color: #f8fafc;
    }
    label {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 8px 0;
      font-size: 13px;
      color: #d7dee8;
    }
    input[type="checkbox"] {
      width: 16px;
      height: 16px;
    }
    input[type="number"] {
      width: 86px;
      color: #edf2f7;
      background: #0f141a;
      border: 1px solid #3a4654;
      border-radius: 6px;
      padding: 5px 6px;
    }
    dl {
      display: grid;
      grid-template-columns: 132px minmax(0, 1fr);
      gap: 7px 10px;
      margin: 0;
      font-size: 12px;
    }
    dt {
      color: #94a3b8;
    }
    dd {
      margin: 0;
      color: #e2e8f0;
      overflow-wrap: anywhere;
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    @media (max-width: 920px) {
      main {
        grid-template-columns: 1fr;
      }
      aside {
        order: -1;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Evidence Overlay Preview</h1>
    <div class="status">
      <span class="badge" id="eventBadge">event: loading</span>
      <span class="badge" id="sourceBadge">source: loading</span>
      <span class="badge" id="clipBadge">clip: loading</span>
      <span class="badge warning" id="warningBadge" hidden>decode warning</span>
    </div>
  </header>
  <main>
    <section class="stage">
      <div class="video-wrap" id="videoWrap">
        <video id="video" src="./raw_clip.mov" controls preload="metadata"></video>
        <canvas id="overlay"></canvas>
      </div>
    </section>
    <aside>
      <section class="panel">
        <h2>Overlay Controls</h2>
        <label><input id="showMatched" type="checkbox" checked> show matched faces</label>
        <label><input id="showUnknown" type="checkbox" checked> show unknown faces</label>
        <label><input id="showLandmarks" type="checkbox" checked> show landmarks</label>
        <label><input id="showLabels" type="checkbox" checked> show labels</label>
        <label>tolerance ms <input id="toleranceMs" type="number" min="0" step="25" value="150"></label>
        <label>hold ms <input id="holdMs" type="number" min="0" step="50" value="750"></label>
      </section>
      <section class="panel">
        <h2>Debug</h2>
        <dl>
          <dt>currentTime</dt><dd class="mono" id="currentTime">0.000</dd>
          <dt>target_pts</dt><dd class="mono" id="targetPts">-</dd>
          <dt>matched frame_pts</dt><dd class="mono" id="matchedPts">-</dd>
          <dt>alignment</dt><dd id="alignmentMode">-</dd>
          <dt>active objects</dt><dd id="activeObjects">0</dd>
          <dt>video size</dt><dd id="videoSize">-</dd>
          <dt>first frame pts</dt><dd class="mono" id="firstPts">-</dd>
          <dt>framerate</dt><dd id="framerate">-</dd>
          <dt>decode warnings</dt><dd id="decodeWarnings">0</dd>
        </dl>
      </section>
      <section class="panel">
        <h2>Bundle</h2>
        <dl>
          <dt>annotation lines</dt><dd id="annotationLines">-</dd>
          <dt>face objects</dt><dd id="faceObjects">-</dd>
          <dt>matched objects</dt><dd id="matchedObjects">-</dd>
          <dt>unknown objects</dt><dd id="unknownObjects">-</dd>
          <dt>colors</dt><dd id="colorsUsed">-</dd>
        </dl>
      </section>
    </aside>
  </main>
  <script>
    "use strict";

    const FILES = {
      metadata: "./metadata.json",
      sinkMetadata: "./sink_metadata.json",
      annotations: "./annotations.jsonl",
      summary: "./summary.json"
    };
    const NS_PER_SECOND = 1000000000;

    const state = {
      metadata: null,
      sinkRecords: [],
      summary: null,
      annotations: [],
      firstVideoFramePts: null,
      sourceWidth: 1920,
      sourceHeight: 1080,
      framerate: "",
      timeOffsetFallbackUsed: false,
      activeLine: null,
      activeObjects: []
    };

    const video = document.getElementById("video");
    const canvas = document.getElementById("overlay");
    const ctx = canvas.getContext("2d");
    const controls = {
      showMatched: document.getElementById("showMatched"),
      showUnknown: document.getElementById("showUnknown"),
      showLandmarks: document.getElementById("showLandmarks"),
      showLabels: document.getElementById("showLabels"),
      toleranceMs: document.getElementById("toleranceMs"),
      holdMs: document.getElementById("holdMs")
    };

    async function fetchText(path) {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`failed to fetch ${path}: ${response.status}`);
      }
      return response.text();
    }

    async function loadJson(path) {
      return JSON.parse(await fetchText(path));
    }

    async function loadJsonLines(path) {
      const text = await fetchText(path);
      return text.split(/\r?\n/)
        .map(line => line.trim())
        .filter(Boolean)
        .map(line => JSON.parse(line));
    }

    async function loadSinkMetadata(path) {
      const text = await fetchText(path);
      const trimmed = text.trim();
      if (!trimmed) return [];
      try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) return parsed;
        if (parsed && typeof parsed === "object") return [parsed];
      } catch (_err) {
        return trimmed.split(/\r?\n/)
          .map(line => line.trim())
          .filter(Boolean)
          .map(line => JSON.parse(line));
      }
      return [];
    }

    function toNumber(value, fallback = null) {
      const n = Number(value);
      return Number.isFinite(n) ? n : fallback;
    }

    function firstFrameWithPts(records) {
      return records.find(record => Number.isFinite(Number(record.pts))) || null;
    }

    function prepareAnnotations(lines) {
      state.timeOffsetFallbackUsed = false;
      state.annotations = lines.map((line, index) => {
        const framePts = toNumber(line.frame_pts);
        const timeOffsetMs = toNumber(line.time_offset_ms);
        let overlayTimeSec = null;
        let alignment = "frame_pts";
        if (framePts !== null && state.firstVideoFramePts !== null) {
          overlayTimeSec = (framePts - state.firstVideoFramePts) / NS_PER_SECOND;
        } else if (timeOffsetMs !== null) {
          overlayTimeSec = timeOffsetMs / 1000;
          alignment = "time_offset_ms_fallback";
          state.timeOffsetFallbackUsed = true;
        }
        return { ...line, _index: index, _framePts: framePts, _overlayTimeSec: overlayTimeSec, _alignment: alignment };
      }).filter(line => line._overlayTimeSec !== null)
        .sort((a, b) => a._overlayTimeSec - b._overlayTimeSec);
    }

    function bboxToRect(bbox) {
      if (!bbox || !Array.isArray(bbox.values) || bbox.values.length < 4) return null;
      const values = bbox.values.map(Number);
      if (values.some(v => !Number.isFinite(v))) return null;
      const format = String(bbox.format || "cxcywh").toLowerCase();
      if (format === "cxcywh" || format.includes("cxcywh")) {
        const [cx, cy, w, h] = values;
        return { x: cx - w / 2, y: cy - h / 2, w, h };
      }
      if (format === "xyxy" || format.includes("xyxy")) {
        const [x1, y1, x2, y2] = values;
        return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
      }
      if (format === "xywh") {
        const [x, y, w, h] = values;
        return { x, y, w, h };
      }
      const [cx, cy, w, h] = values;
      return { x: cx - w / 2, y: cy - h / 2, w, h };
    }

    function findActiveAnnotation(currentTime) {
      if (!state.annotations.length || state.firstVideoFramePts === null) {
        return null;
      }
      const toleranceNs = Math.max(0, toNumber(controls.toleranceMs.value, 150)) * 1000000;
      const holdSec = Math.max(0, toNumber(controls.holdMs.value, 750)) / 1000;
      const targetPts = state.firstVideoFramePts + currentTime * NS_PER_SECOND;
      let nearest = null;
      let nearestDelta = Infinity;
      let latestPrior = null;
      for (const line of state.annotations) {
        if (line._framePts !== null) {
          const delta = Math.abs(line._framePts - targetPts);
          if (delta < nearestDelta) {
            nearest = line;
            nearestDelta = delta;
          }
        }
        if (line._overlayTimeSec <= currentTime) {
          latestPrior = line;
        }
      }
      if (nearest && nearestDelta <= toleranceNs) {
        return { line: nearest, targetPts, mode: "frame_pts", matchedPts: nearest._framePts };
      }
      if (latestPrior && currentTime - latestPrior._overlayTimeSec <= holdSec) {
        return { line: latestPrior, targetPts, mode: `${latestPrior._alignment}_held`, matchedPts: latestPrior._framePts };
      }
      return { line: null, targetPts, mode: "none", matchedPts: null };
    }

    function resizeCanvas() {
      const rect = video.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;
      canvas.width = Math.max(1, Math.round(rect.width * dpr));
      canvas.height = Math.max(1, Math.round(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function objectVisible(obj) {
      const status = obj.identity?.status || "unknown";
      if (status === "matched") return controls.showMatched.checked;
      return controls.showUnknown.checked;
    }

    function drawLandmarks(points, scaleX, scaleY, color) {
      if (!controls.showLandmarks.checked || !Array.isArray(points)) return;
      ctx.fillStyle = color;
      for (const point of points) {
        if (!Array.isArray(point) || point.length < 2) continue;
        const x = Number(point[0]) * scaleX;
        const y = Number(point[1]) * scaleY;
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
        ctx.beginPath();
        ctx.arc(x, y, 2.8, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function labelForObject(obj, line) {
      const styleLabel = obj.style?.label || obj.identity?.display_name || "Face";
      const similarity = Number.isFinite(Number(obj.identity?.similarity))
        ? Number(obj.identity.similarity).toFixed(2)
        : "n/a";
      const trackId = obj.track_id || "";
      const timestamp = line.timestamp_ms || "";
      return `${styleLabel} | sim ${similarity} | track ${trackId} | ts ${timestamp}`;
    }

    function drawLabel(text, x, y, color) {
      if (!controls.showLabels.checked) return;
      ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
      const metrics = ctx.measureText(text);
      const width = metrics.width + 10;
      const height = 20;
      const boxY = Math.max(0, y - height - 3);
      ctx.fillStyle = "rgba(0, 0, 0, 0.72)";
      ctx.fillRect(x, boxY, width, height);
      ctx.fillStyle = color;
      ctx.fillText(text, x + 5, boxY + 14);
    }

    function drawOverlay() {
      resizeCanvas();
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      ctx.clearRect(0, 0, width, height);
      const active = findActiveAnnotation(video.currentTime);
      const line = active?.line || null;
      const objects = line ? (line.objects || []).filter(objectVisible) : [];
      state.activeLine = line;
      state.activeObjects = objects;
      const scaleX = width / state.sourceWidth;
      const scaleY = height / state.sourceHeight;

      for (const obj of objects) {
        const rect = bboxToRect(obj.bbox);
        if (!rect) continue;
        const color = obj.style?.bbox_color || "#9E9E9E";
        const x = rect.x * scaleX;
        const y = rect.y * scaleY;
        const w = rect.w * scaleX;
        const h = rect.h * scaleY;
        ctx.strokeStyle = color;
        ctx.lineWidth = Number(obj.style?.line_width || 2);
        ctx.strokeRect(x, y, w, h);
        drawLandmarks(obj.landmarks?.points || [], scaleX, scaleY, color);
        drawLabel(labelForObject(obj, line), x, y, obj.style?.label_color || color);
      }
      updateDebug(active, objects.length);
      requestAnimationFrame(drawOverlay);
    }

    function updateDebug(active, objectCount) {
      document.getElementById("currentTime").textContent = video.currentTime.toFixed(3);
      document.getElementById("targetPts").textContent = active?.targetPts ? Math.round(active.targetPts).toString() : "-";
      document.getElementById("matchedPts").textContent = active?.matchedPts ? Math.round(active.matchedPts).toString() : "-";
      document.getElementById("alignmentMode").textContent = active?.mode || "-";
      document.getElementById("activeObjects").textContent = String(objectCount);
      document.getElementById("videoSize").textContent = `${state.sourceWidth}x${state.sourceHeight}`;
      document.getElementById("firstPts").textContent = state.firstVideoFramePts !== null ? String(state.firstVideoFramePts) : "-";
      document.getElementById("framerate").textContent = state.framerate || "-";
    }

    function updateHeader() {
      const event = state.metadata?.event || {};
      const media = state.metadata?.media || {};
      const status = state.metadata?.status || {};
      const validation = media.clip_validation || {};
      const decodeCount = Number(validation.decode_error_count || 0);
      const clipStatus = status.clip_status || "unknown";
      document.getElementById("eventBadge").textContent = `event: ${event.event_id || "unknown"}`;
      document.getElementById("sourceBadge").textContent = `source: ${event.source_id || "unknown"}`;
      document.getElementById("clipBadge").textContent = `clip: ${clipStatus}`;
      document.getElementById("decodeWarnings").textContent = String(decodeCount);
      const warningBadge = document.getElementById("warningBadge");
      const warning = clipStatus === "generated_corrupt" || decodeCount > 0;
      warningBadge.hidden = !warning;
      warningBadge.textContent = warning ? `warning: decode issues ${decodeCount}` : "";
    }

    function updateSummary() {
      document.getElementById("annotationLines").textContent = String(state.summary?.annotation_lines ?? state.annotations.length);
      document.getElementById("faceObjects").textContent = String(state.summary?.face_objects ?? "-");
      document.getElementById("matchedObjects").textContent = String(state.summary?.matched_objects ?? "-");
      document.getElementById("unknownObjects").textContent = String(state.summary?.unknown_objects ?? "-");
      document.getElementById("colorsUsed").textContent = Array.isArray(state.summary?.colors_used)
        ? state.summary.colors_used.join(", ")
        : "-";
    }

    async function init() {
      const [metadata, sinkRecords, annotations, summary] = await Promise.all([
        loadJson(FILES.metadata),
        loadSinkMetadata(FILES.sinkMetadata),
        loadJsonLines(FILES.annotations),
        loadJson(FILES.summary)
      ]);
      state.metadata = metadata;
      state.sinkRecords = sinkRecords;
      state.summary = summary;
      const first = firstFrameWithPts(sinkRecords);
      if (!first) throw new Error("sink_metadata.json has no pts frame");
      state.firstVideoFramePts = Number(first.pts);
      state.sourceWidth = Number(first.width || 1920);
      state.sourceHeight = Number(first.height || 1080);
      state.framerate = first.framerate || "";
      prepareAnnotations(annotations);
      updateHeader();
      updateSummary();
      resizeCanvas();
      requestAnimationFrame(drawOverlay);
    }

    for (const input of Object.values(controls)) {
      input.addEventListener("change", drawOverlay);
      input.addEventListener("input", drawOverlay);
    }
    window.addEventListener("resize", resizeCanvas);
    video.addEventListener("loadedmetadata", resizeCanvas);
    init().catch(err => {
      console.error(err);
      document.getElementById("warningBadge").hidden = false;
      document.getElementById("warningBadge").textContent = `preview load failed: ${err.message}`;
    });
  </script>
</body>
</html>
"""


def _container_media_path(path: Path) -> str | None:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        resolved = path
    media_root = Path("/data/video-analytics/media")
    try:
        rel = resolved.relative_to(media_root)
    except ValueError:
        return None
    return "/media/" + rel.as_posix()


def _write_preview(output: Path, html: str) -> None:
    try:
        output.write_text(html, encoding="utf-8")
        return
    except PermissionError:
        container_path = _container_media_path(output)
        if container_path is None:
            raise

    cmd = [
        "docker",
        "exec",
        "-i",
        "c1-official-media-worker",
        "sh",
        "-c",
        f"cat > {shlex.quote(container_path)}",
    ]
    try:
        subprocess.run(
            cmd,
            input=html,
            text=True,
            check=True,
            capture_output=True,
        )
    except Exception as exc:
        raise PermissionError(
            f"cannot write {output}; direct write failed and docker fallback failed"
        ) from exc


def generate_preview(bundle_dir: Path) -> Path:
    _validate_bundle(bundle_dir)
    output = bundle_dir / "preview.html"
    _write_preview(output, _html_template())
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate static HTML preview for an evidence overlay bundle."
    )
    parser.add_argument(
        "--bundle-dir",
        default=None,
        help="Evidence bundle directory. Defaults to latest compatible bundle.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle_dir = Path(args.bundle_dir).resolve() if args.bundle_dir else _latest_bundle()
    if bundle_dir is None:
        print(
            f"ERROR: no compatible bundle found under {DEFAULT_EVIDENCE_ROOT}",
            file=sys.stderr,
        )
        return 2
    try:
        info = _validate_bundle(bundle_dir)
        preview = generate_preview(bundle_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    first = info["first_frame"]
    metadata = info["metadata"]
    summary = info["summary"]
    print(f"preview_html={preview}")
    print(f"bundle_dir={bundle_dir}")
    print(f"raw_clip={bundle_dir / 'raw_clip.mov'}")
    print(f"width={first.get('width', '')}")
    print(f"height={first.get('height', '')}")
    print(f"framerate={first.get('framerate', '')}")
    print(f"first_frame_pts={first.get('pts', '')}")
    print(f"raw_clip_duration={metadata.get('media', {}).get('raw_clip_duration', '')}")
    print(f"clip_status={metadata.get('status', {}).get('clip_status', '')}")
    print(f"annotation_lines={summary.get('annotation_lines', info['annotation_lines'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
