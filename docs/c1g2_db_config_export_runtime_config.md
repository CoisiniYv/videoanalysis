# C1G.2 DB Config Export to Generated Runtime Config

## Goal

C1G.2 turns the C1G.1 PostgreSQL configuration tables into generated runtime config files:

```text
PostgreSQL cameras / camera_zones / camera_rules
  -> cameras.generated.yml
  -> algorithm_runtime_config.json
  -> export_summary.json
  -> apply_plan.json
```

This phase does not change Savant inference, does not update `/operator`, does not touch the evidence viewer, and does not restart source-adapter or Savant.

## Source Of Truth

The database is the configuration source of truth for this phase:

```text
cameras
camera_zones
camera_rules
cameras.alert_policy
```

The exporter reads enabled cameras, zones, and rules by default. Disabled configuration is exported only when `--include-disabled` is passed.

## CLI

```bash
python services/api/scripts/export_camera_runtime_config.py \
  --camera-id cam_c1e_rtsp_replay \
  --output-dir /data/video-analytics/artifacts/c1g2/generated-config
```

Supported options:

```text
--camera-id <id>
--all-enabled
--include-disabled
--output-dir <path>
--redact-secrets / --no-redact-secrets
--dry-run
--database-url <postgres url>
```

Default output directory:

```text
/data/video-analytics/artifacts/c1g2/generated-config/
```

The writer is atomic: each file is written to `.tmp` first, then renamed.

## Output Files

`cameras.generated.yml` is the runtime camera document intended for Savant / behavior-rule consumers. It includes camera input, FPS policy, alert policy, zones, and rules.

`algorithm_runtime_config.json` is a diagnostics / worker-friendly JSON representation:

```json
{
  "schema_version": "c1g2.runtime_config.v1",
  "source": "postgres",
  "cameras": []
}
```

`export_summary.json` records counts, paths, selected cameras, redaction state, and validation status. It must not expose RTSP credentials by default.

`apply_plan.json` records future actions only. Restart and mount/copy actions are marked `not_executed`; controlled runtime apply is deferred to C1G.3.

## Rule Classification

The exporter uses:

```python
classify_algorithm_rule(algorithm_id: str) -> str
```

Classification:

```text
face.observation -> observation
face.watchlist -> alert
face.live_search -> alert_config
behavior.* -> alert
unknown -> validation failure
```

`face.observation` remains a fact/trajectory configuration and is not an alert rule. `face.watchlist` is an alert rule. `face.live_search` is exported as alert configuration only; live-search jobs are not implemented in this phase.

## Validation

Camera validation:

```text
camera_id non-empty
source_id non-empty
input_type=rtsp
rtsp_transport=tcp|udp
enabled is boolean
```

Zone validation:

```text
zone_id non-empty
zone_type=polygon|line|direction_line
polygon has at least 3 points
line/direction_line has at least 2 points
points are numeric [x, y] pairs
```

Rule validation:

```text
rule_id non-empty
algorithm_id allowlisted
config is a JSON object
behavior.intrusion / loitering / crowd_gathering reference an existing zone_id
behavior.wall_climb_suspicious line_id references a line or direction_line zone when present
face.watchlist threshold is numeric and in [0, 1]
```

Alert policy validation:

```text
global_alert_cooldown_s >= 0
store_suppressed_events is bool
suppress_record_request is bool
critical_bypass is bool
```

Validation failure exits non-zero and does not write final output files.

## Secret Redaction

By default, RTSP credentials are redacted in `export_summary.json`, `apply_plan.json`, and console output:

```text
rtsp://user:password@host/path
-> rtsp://***:***@host/path
```

`cameras.generated.yml` is a runtime config file and keeps the actual RTSP URL.

## Smoke

```bash
pytest -q harness/tests/test_c1g2_db_config_export.py
bash scripts/smoke/check_c1g2_db_config_export.sh
docker compose -f infra/docker-compose.c1-official-replay-dev.yml config
git diff --check
```

Smoke summary:

```text
/data/video-analytics/artifacts/c1g2/c1g2_db_config_export_summary.json
```

Generated config:

```text
/data/video-analytics/artifacts/c1g2/generated-config/
```

## Known Limits

C1G.2 does not mount generated files into Savant, does not restart source-adapter, does not restart Savant, and does not implement a live runtime apply mechanism. That controlled apply/restart flow is reserved for C1G.3.
