# Phase C2.13P - RTSP Pose / Person Observation Repair

## Why C2.13R Intrusion Was Partial

C2.13R proved RTSP watchlist matching, but intrusion remained partial:

- active RTSP source: `c2_post_savant_fps_probe`
- active RTSP camera: `c2_post_savant_fps_probe`
- `security.person_observations`: absent before/after
- intrusion events: `0`

The C2.13P inspection found that the Savant module includes YOLO26 pose and
BehaviorRulesPyFunc, and runtime logs show person metadata on the active RTSP
source. The problem is not the pose model. The running C2 POC container mounts
`/tmp/c2-fps-probe-module` as `/opt/savant/src/module`, and that mounted config
copy only contained the stale `c1e_rtsp_replay` camera entry. BehaviorRulesPyFunc
therefore dropped `c2_post_savant_fps_probe` as an unknown source before it could
export person observations or intrusion events.

## Pose / Person Path

Active module path:

`modules/savant_security/module.yml`

Relevant elements:

- `yolo26_pose`: YOLO26 pose/person detector
- `tracker`: nvtracker assigns person track IDs
- `behavior_rules`: evaluates intrusion and exports person observations
- face recognition elements remain unchanged

BehaviorRulesPyFunc:

- builds person/pose observations from Savant frame metadata
- exports accepted person bbox observations to `security.person_observations`
- directly exports intrusion events to `security.events`
- does not serialize embeddings or image bytes

## Repair

C2.13P adds:

- `scripts/tools/run_c2_13p_rtsp_pose_intrusion_probe.py`
- `scripts/smoke/current/check_c2_13p_rtsp_pose_intrusion.sh`
- `harness/tests/test_c2_13p_rtsp_pose_intrusion_probe.py`

The probe:

1. verifies real RTSP input;
2. inspects the module pose/person path;
3. compares repo camera config with the mounted Savant runtime config;
4. syncs the repo camera config into the mounted runtime module copy only when
   the runtime copy lacks the active source;
5. restarts only `c2-poc-savant` and `c2-poc-source-adapter` when sync occurs;
6. runs a bounded RTSP window;
7. checks `security.person_observations` and real intrusion events;
8. persists real runtime events through a bounded local event-worker consumer.

No fake person observations or intrusion events are created.

## Intrusion Rule

The active-source ROI/rule from C2.13R remains valid:

- source_id: `c2_post_savant_fps_probe`
- camera_id: `c2_post_savant_fps_probe`
- algorithm_id: `behavior.intrusion`
- rule_id: `c2_13r_rtsp_intrusion_rule`
- ROI: full-frame `1920x1080`
- min_inside_ms: `1`
- min_person_confidence: `0.25`
- min_person_width: `20`
- min_person_height: `40`
- min_visible_keypoints: `0`

## Output

Output directory format:

`/data/video-analytics/media/evidence/c2_13p_rtsp_pose_intrusion_YYYYMMDDTHHMMSS/`

Files:

- `savant_pose_metadata_probe.json`
- `person_observation_stream_probe.json`
- `intrusion_rule_input_probe.json`
- `intrusion_metrics.json`
- `event_worker_metrics.json`
- `runtime_status.json`
- `runtime_config_probe.json`
- `sync_report.json`
- `api_query_results.json`
- `unsafe_payload_scan.json`
- `decision_summary.json`
- `operator_rtsp_intrusion_report.html`

If a real intrusion event is produced:

- `intrusion_event.json`
- `persisted_intrusion_event_row.json`

## Limitations

- Bounded runtime only, not a long-running soak.
- Not broad intrusion accuracy testing.
- Event-style Replay still has not passed and is not claimed.
- Visual evidence is optional in this phase.
- If no person appears in the RTSP scene during the bounded window, the correct
  result is `PARTIAL_C2_13P_NO_PERSON_IN_SCENE`.
