#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
METRICS_HOST_PORT="${SAVANT_METRICS_HOST_PORT:-18080}"
METRICS_URL="${SAVANT_METRICS_URL:-http://127.0.0.1:${METRICS_HOST_PORT}/metrics}"
SAVANT_CONTAINER="${SAVANT_CONTAINER:-video-analytics-midterm-savant}"
REDIS_CONTAINER="${REDIS_CONTAINER:-video-analytics-midterm-redis}"
ANNOTATION_STREAM="${ANNOTATION_STREAM:-security.frame_annotations}"
SOURCES_CONFIG="${SOURCES_CONFIG:-${ROOT_DIR}/infra/generated/sources.generated.yml}"
SAMPLE_SECONDS="${SAVANT_PERF_SAMPLE_SECONDS:-5}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "check=savant_perf_observability"
echo "metrics_url=${METRICS_URL}"
echo "savant_container=${SAVANT_CONTAINER}"
echo "annotation_stream=${ANNOTATION_STREAM}"
echo "sources_config=${SOURCES_CONFIG}"

curl --noproxy '*' -fsS "${METRICS_URL}" >"${TMP_DIR}/metrics.before"
restart_before="$(docker inspect -f '{{.RestartCount}}' "${SAVANT_CONTAINER}")"
sleep "${SAMPLE_SECONDS}"
curl --noproxy '*' -fsS "${METRICS_URL}" >"${TMP_DIR}/metrics.after"
restart_after="$(docker inspect -f '{{.RestartCount}}' "${SAVANT_CONTAINER}")"

if [[ "${restart_before}" != "${restart_after}" ]]; then
  echo "ERROR: savant restart count changed before=${restart_before} after=${restart_after}" >&2
  exit 1
fi
echo "savant_restart_count=${restart_after}"

python - "${SOURCES_CONFIG}" >"${TMP_DIR}/enabled_sources" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import yaml

doc = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
sources = doc.get("sources") if isinstance(doc, dict) else {}
for source in sources.values() if isinstance(sources, dict) else []:
    if not isinstance(source, dict):
        continue
    uri = str(source.get("uri") or "")
    if (
        source.get("enabled") is True
        and source.get("adapter_type") == "gstreamer"
        and uri.startswith(("rtsp://", "rtsps://"))
    ):
        source_id = str(source.get("source_id") or "")
        if source_id:
            print(source_id)
PY

if [[ ! -s "${TMP_DIR}/enabled_sources" ]]; then
  echo "ERROR: no enabled RTSP sources found in ${SOURCES_CONFIG}" >&2
  exit 1
fi

python - "$TMP_DIR/metrics.before" "$TMP_DIR/metrics.after" "$TMP_DIR/enabled_sources" <<'PY'
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

REQUIRED_BY_SOURCE = [
    "va_savant_frames_seen_total",
    "va_savant_frame_annotations_exported_total",
    "va_savant_effective_fps",
    "va_savant_last_frame_age_seconds",
    "va_savant_pose_stage_frames_total",
    "va_savant_pose_frames_with_person_total",
    "va_savant_pose_objects_total",
    "va_savant_face_stage_frames_total",
    "va_savant_face_frames_with_face_total",
    "va_savant_face_objects_total",
    "va_savant_adaface_embeddings_total",
    "va_savant_person_observations_exported_total",
    "va_savant_face_observations_exported_total",
]
MUST_ADVANCE = [
    "va_savant_frames_seen_total",
    "va_savant_frame_annotations_exported_total",
    "va_savant_pose_stage_frames_total",
    "va_savant_face_stage_frames_total",
]
GLOBAL_REQUIRED = ["va_savant_sources_active"]
SAMPLE_RE = re.compile(r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?$")
LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse(path: str) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        sample = parts[0]
        match = SAMPLE_RE.match(sample)
        if match is None:
            continue
        try:
            value = float(parts[1])
        except ValueError:
            continue
        if math.isfinite(value):
            labels = {key: value for key, value in LABEL_RE.findall(match.group("labels") or "")}
            values[(match.group("name"), labels.get("source_id", ""))] = value
    return values


before = parse(sys.argv[1])
after = parse(sys.argv[2])
sources = [
    line.strip()
    for line in Path(sys.argv[3]).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
missing: list[str] = []
not_advancing: list[str] = []
for source_id in sources:
    for metric in REQUIRED_BY_SOURCE:
        if (metric, source_id) not in after:
            missing.append(f"{metric}{{source_id={source_id}}}")
    for metric in MUST_ADVANCE:
        key = (metric, source_id)
        if key in before and key in after and after[key] <= before[key]:
            not_advancing.append(
                f"{metric}{{source_id={source_id}}} before={before[key]} after={after[key]}"
            )
for metric in GLOBAL_REQUIRED:
    if (metric, "") not in after:
        missing.append(metric)
if missing:
    print("ERROR: missing required va_savant metrics:", file=sys.stderr)
    for item in missing:
        print(f"  {item}", file=sys.stderr)
    raise SystemExit(1)
if not_advancing:
    print("ERROR: required va_savant counters did not advance:", file=sys.stderr)
    for item in not_advancing:
        print(f"  {item}", file=sys.stderr)
    raise SystemExit(1)
for source_id in sources:
    frames = after[("va_savant_frames_seen_total", source_id)] - before.get(
        ("va_savant_frames_seen_total", source_id),
        0.0,
    )
    print(f"va_savant_source={source_id} frames_seen_delta={frames:g}")
PY

docker exec "${REDIS_CONTAINER}" \
  redis-cli --raw XREVRANGE "${ANNOTATION_STREAM}" + - COUNT 5000 \
  >"${TMP_DIR}/frame_annotations"

while IFS= read -r source_id; do
  if ! grep -Fq "${source_id}" "${TMP_DIR}/frame_annotations"; then
    echo "ERROR: no recent frame annotation sample for enabled source_id=${source_id}" >&2
    exit 1
  fi
  echo "frame_flow_source=${source_id}"
done <"${TMP_DIR}/enabled_sources"

if command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits \
    >"${TMP_DIR}/nvidia_smi" 2>/dev/null; then
    echo "gpu_metrics=available_host"
  else
    echo "gpu_metrics=unavailable_host"
  fi
elif docker exec "${SAVANT_CONTAINER}" sh -lc 'command -v nvidia-smi >/dev/null 2>&1' \
  >/dev/null 2>&1; then
  docker exec "${SAVANT_CONTAINER}" \
    nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits \
    >"${TMP_DIR}/nvidia_smi" || true
  if [[ -s "${TMP_DIR}/nvidia_smi" ]]; then
    echo "gpu_metrics=available_container"
  else
    echo "gpu_metrics=unavailable_container"
  fi
else
  echo "gpu_metrics=unavailable"
fi

echo "PASS_SAVANT_PERF_OBSERVABILITY_READY"
