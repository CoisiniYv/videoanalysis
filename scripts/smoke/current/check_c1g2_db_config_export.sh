#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/infra/docker-compose.c1-official-replay-dev.yml}"
API_URL="${API_URL:-http://127.0.0.1:8000}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
OUTPUT_DIR="${C1G2_OUTPUT_DIR:-/data/video-analytics/artifacts/c1g2/generated-config}"
SUMMARY_OUT="${C1G2_SUMMARY_OUT:-/data/video-analytics/artifacts/c1g2/c1g2_db_config_export_summary.json}"
MIGRATION="${REPO_ROOT}/db/migrations/010_c1g1_algorithm_config_api.sql"

note() { printf '[C1G2] %s\n' "$*"; }
fail() { printf 'FAIL_C1G2_DB_CONFIG_EXPORT: %s\n' "$*" >&2; exit 1; }

cd "$REPO_ROOT"
mkdir -p "$(dirname "$SUMMARY_OUT")" "$OUTPUT_DIR"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export DATABASE_URL

note "starting postgres, redis, api"
docker compose -f "$COMPOSE_FILE" up -d postgres redis api >/tmp/c1g2_compose_up.log

note "applying C1G.1 schema migration"
if command -v psql >/dev/null 2>&1; then
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$MIGRATION" >/tmp/c1g2_migration.log
else
  docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U video -d video_analytics -v ON_ERROR_STOP=1 < "$MIGRATION" >/tmp/c1g2_migration.log
fi

note "waiting for API health"
for _ in $(seq 1 60); do
  if curl --noproxy '*' -fsS "${API_URL}/health" >/tmp/c1g2_health.json 2>/dev/null; then
    break
  fi
  sleep 1
done
curl --noproxy '*' -fsS "${API_URL}/health" >/tmp/c1g2_health.json || fail "API health not ready"

note "creating deterministic test camera, ROI, and rules"
API_URL="$API_URL" python3 - <<'PY'
from __future__ import annotations

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

api_url = os.environ["API_URL"].rstrip("/")
camera_id = "cam_c1g2_export_test"


def request(method: str, path: str, body: dict | None = None, *, allow_404: bool = False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(
        api_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
        if allow_404 and status == 404:
            return None
    if status >= 400:
        raise RuntimeError(f"{method} {path} returned {status}: {raw}")
    parsed = json.loads(raw) if raw else {}
    if parsed.get("error"):
        raise RuntimeError(f"{method} {path} error: {parsed['error']}")
    return parsed.get("data")


camera = {
    "id": camera_id,
    "name": "C1G2 Export Test Camera",
    "source_id": "c1g2_export_test",
    "rtsp_url": "rtsp://c1g2:secret@10.37.57.112:8554/live/1080movie",
    "site_id": "test_site",
    "location": "test_location",
    "gpu_id": 0,
    "enabled": True,
    "input_type": "rtsp",
    "rtsp_transport": "tcp",
    "fps_policy": {"max_fps": "8/1", "min_fps": "2/1"},
    "alert_policy": {
        "global_alert_cooldown_s": 30,
        "store_suppressed_events": True,
        "suppress_record_request": True,
        "critical_bypass": False,
    },
}

existing = request("GET", f"/api/v1/cameras/{camera_id}", allow_404=True)
if existing is None:
    request("POST", "/api/v1/cameras", camera)
else:
    update = dict(camera)
    update.pop("id")
    request("PUT", f"/api/v1/cameras/{camera_id}", update)

zone = {
    "zone_id": "perimeter",
    "zone_name": "周界区域",
    "zone_type": "polygon",
    "coordinate_space": "pixel",
    "points": [[100, 300], [900, 300], [900, 700], [100, 700]],
    "enabled": True,
}
config = request("GET", f"/api/v1/cameras/{camera_id}/config")
if any(item["zone_id"] == "perimeter" for item in config.get("zones", [])):
    request("PUT", f"/api/v1/cameras/{camera_id}/zones/perimeter", zone)
else:
    request("POST", f"/api/v1/cameras/{camera_id}/zones", zone)

rules = [
    {
        "rule_id": "rule_face_observation",
        "algorithm_id": "face.observation",
        "enabled": True,
        "config": {
            "min_face_confidence": 0.6,
            "min_face_size": 40,
            "min_quality": 0.6,
            "reid_min_interval_ms": 1000,
            "retention_days": 30,
        },
    },
    {
        "rule_id": "rule_watchlist",
        "algorithm_id": "face.watchlist",
        "enabled": True,
        "config": {"threshold": 0.75, "cooldown_s": 60},
    },
    {
        "rule_id": "rule_intrusion",
        "algorithm_id": "behavior.intrusion",
        "enabled": True,
        "config": {
            "zone_id": "perimeter",
            "min_inside_ms": 1000,
            "cooldown_s": 30,
        },
    },
]
config = request("GET", f"/api/v1/cameras/{camera_id}/config")
existing_rules = {item["rule_id"] for item in config.get("rules", [])}
for rule in rules:
    if rule["rule_id"] in existing_rules:
        request("PUT", f"/api/v1/cameras/{camera_id}/rules/{rule['rule_id']}", rule)
    else:
        request("POST", f"/api/v1/cameras/{camera_id}/rules", rule)
PY

note "exporting runtime config"
python3 services/api/scripts/export_camera_runtime_config.py \
  --camera-id cam_c1g2_export_test \
  --output-dir "$OUTPUT_DIR" \
  --redact-secrets >/tmp/c1g2_export_stdout.json

note "validating generated files"
OUTPUT_DIR="$OUTPUT_DIR" SUMMARY_OUT="$SUMMARY_OUT" python3 - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

output_dir = Path(os.environ["OUTPUT_DIR"])
summary_out = Path(os.environ["SUMMARY_OUT"])
paths = {
    "cameras_generated_yml": output_dir / "cameras.generated.yml",
    "algorithm_runtime_config_json": output_dir / "algorithm_runtime_config.json",
    "export_summary_json": output_dir / "export_summary.json",
    "apply_plan_json": output_dir / "apply_plan.json",
}
for name, path in paths.items():
    if not path.exists():
        raise SystemExit(f"missing {name}: {path}")

cameras_doc = yaml.safe_load(paths["cameras_generated_yml"].read_text())
runtime_doc = json.loads(paths["algorithm_runtime_config_json"].read_text())
export_summary = json.loads(paths["export_summary_json"].read_text())
apply_plan = json.loads(paths["apply_plan_json"].read_text())

camera = cameras_doc["cameras"]["cam_c1g2_export_test"]
assert camera["camera_id"] == "cam_c1g2_export_test"
assert camera["source_id"] == "c1g2_export_test"
assert camera["input"]["rtsp_transport"] == "tcp"
assert camera["alert_policy"]["global_alert_cooldown_s"] == 30
assert "perimeter" in camera["zones"]
assert "rule_face_observation" in camera["rules"]
assert camera["rules"]["rule_face_observation"]["algorithm_id"] == "face.observation"
assert camera["rules"]["rule_face_observation"]["rule_kind"] == "observation"
assert camera["rules"]["rule_watchlist"]["algorithm_id"] == "face.watchlist"
assert camera["rules"]["rule_watchlist"]["rule_kind"] == "alert"
assert camera["rules"]["rule_intrusion"]["algorithm_id"] == "behavior.intrusion"
assert "c1g2:secret" in paths["cameras_generated_yml"].read_text()

summary_text = paths["export_summary_json"].read_text()
apply_text = paths["apply_plan_json"].read_text()
assert "c1g2:secret" not in summary_text
assert "rtsp://***:***@10.37.57.112:8554/live/1080movie" in summary_text
assert "c1g2:secret" not in apply_text

future_restart = next(
    action for action in apply_plan["actions"] if action["action"] == "future_restart"
)
assert future_restart["status"] == "not_executed"
assert future_restart["services"] == ["source-adapter", "savant-security"]

assert runtime_doc["schema_version"] == "c1g2.runtime_config.v1"
assert runtime_doc["cameras"][0]["camera_id"] == "cam_c1g2_export_test"

summary = {
    "result": "PASS_C1G2_DB_CONFIG_EXPORT",
    "output_dir": str(output_dir),
    "files": {name: str(path) for name, path in paths.items()},
    "camera_id": camera["camera_id"],
    "source_id": camera["source_id"],
    "rtsp_transport": camera["input"]["rtsp_transport"],
    "global_alert_cooldown_s": camera["alert_policy"]["global_alert_cooldown_s"],
    "rules": sorted(camera["rules"]),
    "zones": sorted(camera["zones"]),
    "summary_secret_redacted": "c1g2:secret" not in summary_text,
    "runtime_yaml_keeps_rtsp_url": "c1g2:secret" in paths["cameras_generated_yml"].read_text(),
    "apply_plan_restart_status": future_restart["status"],
    "include_disabled": export_summary["include_disabled"],
    "redact_secrets": export_summary["redact_secrets"],
}
summary_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(summary, ensure_ascii=False))
PY

note "summary written to ${SUMMARY_OUT}"
printf 'PASS_C1G2_DB_CONFIG_EXPORT\n'
