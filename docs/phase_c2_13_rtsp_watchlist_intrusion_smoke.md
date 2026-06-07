# Phase C2.13 - Real RTSP Watchlist + Intrusion Dual Algorithm Smoke

## Goal

C2.13 moves back to real RTSP input and checks whether the same runtime stream
can support both algorithm paths:

```text
RTSP stream
-> source-adapter / Savant
-> YOLO26-pose / intrusion
-> YOLOv8-Face / AdaFace / Reese-Finch watchlist
-> security.events / event-worker / DB / API where available
```

This phase is an algorithm and event-chain smoke. Visual evidence is optional.
Event-style Replay is not repaired or claimed passed.

## RTSP Source

Active C2 runtime stack:

- compose file: `infra/docker-compose.c2-post-savant-replay-poc.yml`
- source-adapter container: `c2-poc-source-adapter`
- Savant container: `c2-poc-savant`
- Redis container: `c2-poc-redis`
- PostgreSQL container: `phase0-postgres`

The active source-adapter environment contains:

- input_type: `rtsp`
- RTSP URL: `rtsp://10.37.57.112:8554/live/1080movie`
- source_id: `c2_post_savant_fps_probe`
- camera_id observed from runtime: `c2_post_savant_fps_probe`

The URL has no embedded credentials in the inspected configuration. The C2.13
tool still redacts credentials if they are present.

## Watchlist Setup

Registered C2.12A external persons:

- Reese:
  - `person_id=5`
  - `external_person_id=demo:f4_3:reese`
  - `gallery_embedding_id=4`
- Finch:
  - `person_id=6`
  - `external_person_id=demo:f4_3:finch`
  - `gallery_embedding_id=5`

Default threshold:

`0.65`

Watchlist rule contract:

`c2_13_rtsp_reese_finch_watchlist_rule`

The current C2 POC compose stack does not run a face-worker container, so the
tool records gallery-search diagnostics from runtime RTSP face observations and
reports an event-pipeline gap if a match exists but no real `watchlist_hit`
event is produced.

## Intrusion Setup

Savant module contains `YOLO26-pose` and `BehaviorRulesPyFunc`, and the camera
config contains an intrusion rule:

- algorithm_id: `behavior.intrusion`
- rule_id: `c2_13_rtsp_intrusion_rule`
- zone: full-frame polygon in `modules/savant_security/config/cameras.c1e_replay.yml`
- `min_inside_ms=1`
- `cooldown_s=60`

Current limitation:

The configured camera source is `c1e_rtsp_replay`, while the active runtime
source is `c2_post_savant_fps_probe`. `BehaviorRulesPyFunc` routes by source_id,
so this mismatch prevents the intrusion rule and person observation exporter
from binding to the active RTSP source. The C2.13 smoke reports this as an ROI /
runtime source binding gap rather than faking an intrusion event.

## Tooling

Tool:

`scripts/tools/run_c2_13_rtsp_watchlist_intrusion_probe.py`

Smoke:

`scripts/smoke/current/check_c2_13_rtsp_watchlist_intrusion.sh`

The tool:

1. Inspects current containers and active source-adapter environment.
2. Verifies whether input is real `rtsp://` or not.
3. Runs a bounded observation window.
4. Reads Redis streams non-destructively.
5. Summarizes DB state through psycopg.
6. Searches Reese/Finch gallery against sampled RTSP face observations.
7. Reports intrusion person/event metrics.
8. Writes an operator HTML report.
9. Scans outputs for embeddings and image/base64/crop bytes.

It does not start containers, does not restart containers, does not run Replay,
does not emit fake events, and does not write Redis/DB.

## Output

Output directory format:

`/data/video-analytics/media/evidence/c2_13_rtsp_watchlist_intrusion_YYYYMMDDTHHMMSS/`

Files:

- `runtime_status.json`
- `rtsp_source_config.json`
- `watchlist_metrics.json`
- `intrusion_metrics.json`
- `event_worker_metrics.json`
- `api_query_results.json`
- `unsafe_payload_scan.json`
- `decision_summary.json`
- `operator_rtsp_dual_algorithm_report.html`

If real events are present, the tool also writes:

- `watchlist_event.json`
- `intrusion_event.json`
- `persisted_event_rows.json`

## Decision Boundaries

Full PASS requires real RTSP input, real Reese/Finch `watchlist_hit`, real
intrusion event, DB/API queryability for both event types, and safe payloads.

Partial results are expected when:

- the active input is not real RTSP;
- no RTSP URL is configured;
- RTSP face observations exist but Reese/Finch do not match in the bounded
  window;
- a Reese/Finch match exists but face-worker/event-worker did not emit/persist
  events;
- intrusion ROI/rule is not bound to the runtime source;
- person/pose is observed but no intrusion condition triggers in the bounded
  window.

## Limitations

- This is not broad accuracy testing.
- This is not a long-running soak.
- Event-style Replay is still not passed.
- Visual evidence is optional in this stage.
- If no intrusion trigger occurs, the scene may not enter the configured ROI or
  the ROI may not be bound to the runtime source.
