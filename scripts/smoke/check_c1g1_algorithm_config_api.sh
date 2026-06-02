#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/infra/docker-compose.c1-official-replay-dev.yml}"
API_URL="${API_URL:-http://127.0.0.1:8000}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
MIGRATION="${REPO_ROOT}/db/migrations/010_c1g1_algorithm_config_api.sql"
ARTIFACT_DIR="${C1G1_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1g1}"
SUMMARY_PATH="${ARTIFACT_DIR}/c1g1_algorithm_config_api_summary.json"

note() { printf '[C1G1] %s\n' "$*"; }
fail() { printf 'FAIL_C1G1_ALGORITHM_CONFIG_API: %s\n' "$*" >&2; exit 1; }

cd "$REPO_ROOT"
mkdir -p "$ARTIFACT_DIR"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"

note "starting postgres, redis, api"
docker compose -f "$COMPOSE_FILE" up -d --build postgres redis api >/tmp/c1g1_compose_up.log

note "applying C1G.1 migration"
if command -v psql >/dev/null 2>&1; then
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$MIGRATION" >/tmp/c1g1_migration.log
else
  docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U video -d video_analytics -v ON_ERROR_STOP=1 < "$MIGRATION" >/tmp/c1g1_migration.log
fi

note "waiting for API health"
for _ in $(seq 1 60); do
  if curl --noproxy '*' -fsS "${API_URL}/health" >/tmp/c1g1_health.json 2>/dev/null; then
    break
  fi
  sleep 1
done
curl --noproxy '*' -fsS "${API_URL}/health" >/tmp/c1g1_health.json || fail "API health not ready"

note "exercising camera / ROI / rule APIs"
API_URL="$API_URL" SUMMARY_PATH="$SUMMARY_PATH" python3 - <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

api_url = os.environ["API_URL"].rstrip("/")
summary_path = Path(os.environ["SUMMARY_PATH"])


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
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"{method} {path} error: {parsed['error']}")
    return parsed.get("data", parsed)


camera_id = "cam_c1g1_test"
camera = {
    "id": camera_id,
    "name": "C1G1 Test Camera",
    "source_id": "c1e_rtsp_replay",
    "rtsp_url": "rtsp://10.37.57.112:8554/live/1080movie",
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
    created_camera = request("POST", "/api/v1/cameras", camera)
else:
    update_body = dict(camera)
    update_body.pop("id")
    created_camera = request("PUT", f"/api/v1/cameras/{camera_id}", update_body)

request("POST", f"/api/v1/cameras/{camera_id}/enable")
policy = request("PUT", f"/api/v1/cameras/{camera_id}/alert-policy", camera["alert_policy"])

config = request("GET", f"/api/v1/cameras/{camera_id}/config")
zone = {
    "zone_id": "perimeter",
    "zone_name": "周界区域",
    "zone_type": "polygon",
    "coordinate_space": "pixel",
    "points": [[100, 300], [900, 300], [900, 700], [100, 700]],
    "enabled": True,
}
if any(z["zone_id"] == zone["zone_id"] for z in config.get("zones", [])):
    request("PUT", f"/api/v1/cameras/{camera_id}/zones/{zone['zone_id']}", zone)
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
        "config": {
            "threshold": 0.75,
            "cooldown_s": 60,
            "camera_scope": [camera_id],
        },
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
existing_rules = {r["rule_id"] for r in config.get("rules", [])}
for rule in rules:
    if rule["rule_id"] in existing_rules:
        request("PUT", f"/api/v1/cameras/{camera_id}/rules/{rule['rule_id']}", rule)
    else:
        request("POST", f"/api/v1/cameras/{camera_id}/rules", rule)

full_config = request("GET", f"/api/v1/cameras/{camera_id}/config")
operator_req = Request(api_url + "/operator", method="GET")
with urlopen(operator_req, timeout=20) as resp:
    operator_status = resp.status
    operator_html = resp.read().decode("utf-8")

rules_by_algorithm = {r["algorithm_id"]: r for r in full_config["rules"]}
assert full_config["camera"]["id"] == camera_id
assert full_config["camera"]["rtsp_transport"] == "tcp"
assert full_config["alert_policy"]["global_alert_cooldown_s"] == 30
assert any(z["zone_id"] == "perimeter" for z in full_config["zones"])
assert rules_by_algorithm["face.observation"]["is_alert_rule"] is False
assert rules_by_algorithm["face.observation"]["rule_category"] == "observation"
assert rules_by_algorithm["face.watchlist"]["is_alert_rule"] is True
assert rules_by_algorithm["behavior.intrusion"]["is_alert_rule"] is True
assert operator_status == 200
assert "Camera Algorithm Configuration" in operator_html
assert "face.observation" in operator_html, "operator page missing face.observation template"
assert "behavior.running" in operator_html, "operator page missing behavior.running template"
assert "behavior.wall_climb_suspicious" in operator_html, "operator page missing behavior.wall_climb_suspicious template"
assert "8090" not in operator_html, "operator page must not reference 8090"
assert "global_alert_cooldown_s" in operator_html, "operator page missing global_alert_cooldown_s"

# C1G.1b: verify app.js serves and contains real API endpoints
app_js_req = Request(api_url + "/operator/static/app.js", method="GET")
with urlopen(app_js_req, timeout=20) as resp:
    app_js_status = resp.status
    app_js_text = resp.read().decode("utf-8")
assert app_js_status == 200
assert "/api/v1/cameras" in app_js_text, "app.js missing /api/v1/cameras"
assert "/zones" in app_js_text, "app.js missing /zones endpoint"
assert "/rules" in app_js_text, "app.js missing /rules endpoint"

summary = {
    "result": "PASS_C1G1_ALGORITHM_CONFIG_API_OPERATOR_PAGE",
    "api_url": api_url,
    "camera_id": camera_id,
    "camera_enabled": full_config["camera"]["enabled"],
    "rtsp_transport": full_config["camera"]["rtsp_transport"],
    "global_alert_cooldown_s": full_config["alert_policy"]["global_alert_cooldown_s"],
    "zones": [z["zone_id"] for z in full_config["zones"]],
    "rules": sorted(rules_by_algorithm),
    "face_observation_is_alert_rule": rules_by_algorithm["face.observation"]["is_alert_rule"],
    "face_watchlist_is_alert_rule": rules_by_algorithm["face.watchlist"]["is_alert_rule"],
    "operator_status": operator_status,
}
summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(summary, ensure_ascii=False))
PY

note "summary written to ${SUMMARY_PATH}"
printf 'PASS_C1G1_ALGORITHM_CONFIG_API_OPERATOR_PAGE\n'
