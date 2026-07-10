# Phase 0 Inventory

范围：当前工作树 `/home/user/video-analytics`。本文件只做事实清点，不做代码质量评价。由于工作树包含未提交/未跟踪文件，`代码行数` 和 `最后修改时间` 使用当前文件系统视图；统计排除 `archive/`、`__pycache__/`、`.pytest_cache/`。

## 目录树（服务/模块级）
```text
services
  analysis-forwarder
    app
  api
    app
      repositories
      routers
      schemas
      services
      static
    infra
      config
      generated
    libs
    modules
      savant_security
    scripts
  archive
    phase-only
      20260610
  clip-worker
    app
    archive
      phase-only
    infra
      config
  event-worker
    app
  evidence-viewer
    app
      static
    infra
      generated
    modules
      savant_security
  face-worker
    app
  media-worker
    app
  savant-watchdog
  source-adapter
modules
  archive
    phase-only
      20260602
  savant_replay
    archive
      phase-only
  savant_security
    archive
      module-variants
    config
      archive
    custom
      adapters
      converters
      filters
      geometry
      models
      pyfuncs
      rules
      services
    poc_deps
    savant_patches
      v0.6.0
infra
  archive
    phase-only
  config
  env
  generated
db
  migrations
    archive
scripts
  config
  debug
    archive
  maintenance
  model_inspect
  models
  runtime
    archive
  smoke
    archive
    current
  spikes
  tools
    archive
  util
    archive
harness
  smoke
    archive
  tests
    archive
```

## 顶层服务/模块入口、规模与最近修改
| 路径 | 入口文件/入口配置 | 源文件数 | 代码行数 | 最近 mtime | 最近修改文件 |
| --- | --- | --- | --- | --- | --- |
| services/analysis-forwarder | services/analysis-forwarder/app/main.py | 5 | 600 | 2026-07-05T15:34:49 | services/analysis-forwarder/app/main.py |
| services/api | services/api/app/main.py | 43 | 16920 | 2026-07-06T14:01:11 | services/api/app/routers/evidence.py |
| services/clip-worker | services/clip-worker/main.py | 9 | 6924 | 2026-07-05T01:16:04 | services/clip-worker/app/repository.py |
| services/event-worker | services/event-worker/main.py | 11 | 3442 | 2026-07-06T14:23:10 | services/event-worker/app/repository.py |
| services/evidence-viewer | services/evidence-viewer/app/main.py | 11 | 10430 | 2026-07-05T17:03:34 | services/evidence-viewer/app/static/operator.js |
| services/face-worker | services/face-worker/main.py | 28 | 7173 | 2026-06-29T21:55:37 | services/face-worker/app/qdrant_gallery_store.py |
| services/media-worker | services/media-worker/main.py | 21 | 16314 | 2026-07-06T17:34:32 | services/media-worker/app/worker.py |
| services/savant-watchdog | (no active entry file found) | 0 | 0 | (no source files) | (none) |
| services/source-adapter | (no active entry file found) | 0 | 0 | (no source files) | (none) |
| modules/savant_replay | modules/savant_replay/config.midterm.json | 9 | 432 | 2026-07-04T02:08:16 | modules/savant_replay/config.midterm.replay-f.json |
| modules/savant_security | modules/savant_security/module.yml | 66 | 20084 | 2026-07-06T19:29:26 | modules/savant_security/config/cameras.midterm.yml |
| libs/evidence_metadata | libs/evidence_metadata/validation.py | 5 | 708 | 2026-06-11T14:19:07 | libs/evidence_metadata/validation.py |
| libs/face_registration | libs/face_registration/image_face_registration.py | 7 | 1664 | 2026-06-26T13:27:48 | libs/face_registration/image_face_registration.py |
| infra | infra/docker-compose.midterm.yml, infra/env/midterm.env, infra/config/replay-shards.midterm.json | 4 | 1949 | 2026-07-06T19:29:26 | infra/generated/sources.generated.yml |
| db | db/migrations/*.sql | 25 | 1616 | 2026-07-06T14:21:34 | db/migrations/025_evidence_task_materialization_ready_at.sql |
| scripts | scripts/midterm_start.sh, scripts/runtime/run_midterm_pressure60.py, scripts/runtime/doctor_midterm.sh | 37 | 15977 | 2026-07-06T19:12:27 | scripts/runtime/run_midterm_pressure60.py |
| harness | harness/tests/conftest.py, harness/tests/test_*.py | 102 | 33914 | 2026-07-06T19:12:38 | harness/tests/test_midterm_pressure60_script.py |

## 服务子目录
| 服务 | 子目录 |
| --- | --- |
| services/analysis-forwarder | app |
| services/api | app, infra, libs, modules, scripts |
| services/clip-worker | app, archive, infra |
| services/event-worker | app |
| services/evidence-viewer | app, infra, modules |
| services/face-worker | app |
| services/media-worker | app |
| services/savant-watchdog | (no subdirs) |
| services/source-adapter | (no subdirs) |

## 模块子目录
| 模块 | 子目录 |
| --- | --- |
| modules/savant_replay | archive |
| modules/savant_security | archive, config, custom, poc_deps, savant_patches |

## Docker Compose 文件与 service 清单
| compose 文件 | services |
| --- | --- |
| infra/archive/docker-compose.phase1c.yml | phase1c-ffmpeg-source, redis, rtsp-server, savant-phase1c |
| infra/archive/docker-compose.phase1d.yml | phase1d-ffmpeg-source, redis, rtsp-server, savant-phase1d |
| infra/archive/docker-compose.phase1e.yml | phase1e-ffmpeg-source, redis, rtsp-server, savant-phase1e |
| infra/archive/docker-compose.phase1f.yml | phase1f-ffmpeg-source, redis, rtsp-server, savant-phase1f |
| infra/archive/docker-compose.phase2b.yml | phase2b-ffmpeg-source, redis, rtsp-server, savant-phase2b |
| infra/archive/docker-compose.phase2c.yml | phase2c-ffmpeg-source, rtsp-server, savant-phase2c |
| infra/archive/docker-compose.phase2d.yml | phase2d-ffmpeg-source, redis, rtsp-server, savant-phase2d |
| infra/archive/docker-compose.phase2e.yml | event-worker, postgres, redis |
| infra/archive/docker-compose.phase2f.yml | api, event-worker, postgres, redis |
| infra/archive/docker-compose.phase2g.yml | api, event-worker, postgres, redis |
| infra/archive/docker-compose.phase2h.yml | api, event-worker, postgres, redis |
| infra/archive/docker-compose.phase2i.yml | api, event-worker, ffmpeg-source, postgres, redis, rtsp-server, savant |
| infra/archive/docker-compose.phase3a.yml | api, clip-worker, event-worker, ffmpeg-source, media-worker, postgres, redis, replay-service, rtsp-server, savant, source-adapter, video-file-sink |
| infra/archive/docker-compose.phase3b.yml | api, clip-worker, event-worker, ffmpeg-source, media-worker, postgres, redis, replay-service, rtsp-server, savant, source-adapter, video-file-sink |
| infra/archive/docker-compose.phase3h-zmq.yml | api, event-worker, evidence-worker, metadata-sink, postgres, redis, savant-zmq, source-adapter, video-file-sink |
| infra/archive/docker-compose.savant-smoke.yml | savant-smoke |
| infra/archive/phase-only/20260602/docker-compose.c1-official-adapter.yml | api, event-worker, evidence-worker, face-worker, metadata-sink, postgres, redis, savant-security, video-file-sink |
| infra/archive/phase-only/20260602/docker-compose.d1-rtsp-15min-detection.yml | redis, replay-service, savant-security, source-adapter |
| infra/archive/phase-only/20260602/docker-compose.dev.yml | api, event-worker, postgres, redis |
| infra/archive/phase-only/20260602/docker-compose.p1-replay-clip-poc.yml | api, clip-worker, event-worker, media-worker, postgres, redis, replay-service, savant-security, source-adapter, video-file-sink |
| infra/archive/phase-only/20260602/docker-compose.p1a-replay-inline-poc.yml | redis, replay-service, savant-security, source-adapter |
| infra/archive/phase-only/20260602/docker-compose.p1b-replay-manual-sink-poc.yml | video-file-sink |
| infra/archive/phase-only/20260602/docker-compose.p1b-rtsp-replay-manual-sink.yml | redis, replay-service, savant-security, source-adapter, video-file-sink |
| infra/archive/phase-only/20260602/docker-compose.p1c-rtsp-replay-event-evidence.yml | clip-worker, event-worker, media-worker, postgres, redis, replay-service, savant-security, source-adapter, video-file-sink |
| infra/archive/phase-only/20260609/docker-compose.c1-official-replay-dev.yml | api, clip-worker, event-worker, evidence-viewer, face-worker, media-worker, postgres, redis, replay-service, savant-security, source-adapter, video-file-sink |
| infra/archive/phase-only/20260609/docker-compose.c2-post-savant-replay-poc.yml | evidence-viewer, redis, replay-service, savant-security, source-adapter, video-file-sink |
| infra/archive/phase-only/20260609/docker-compose.c2-replay-first-dev.yml | clip-worker, event-worker, evidence-viewer, face-worker, media-worker, postgres, redis, replay-service, savant-security, source-adapter, video-file-sink |
| infra/docker-compose.midterm.yml | analysis-forwarder, analysis-forwarder-a, analysis-forwarder-b, api, clip-worker, event-worker, evidence-viewer, face-worker, media-worker, postgres, qdrant, qdrant-sync-worker, redis, replay-a, replay-b, replay-c, replay-d, replay-e, replay-f, replay-g, replay-h, replay-service, rolling-cache-sink, rolling-cache-sink-a, rolling-cache-sink-b, savant-a, savant-b, savant-security, source-adapter, video-file-sink, video-file-sink-a, video-file-sink-b, video-file-sink-c, video-file-sink-d, video-file-sink-e, video-file-sink-f, video-file-sink-g, video-file-sink-h |

## infra/env/*.env
| env 文件 | 变量数 | 配置类别 | 代表性 key |
| --- | --- | --- | --- |
| infra/env/midterm.env | 210 | Savant/export/frame annotation; evidence admission/materialization; storage/maintenance; face/Qdrant/watchlist; Replay/RTSP/runtime module | FACE_OBSERVATION_EXPORT_ENABLED, FACE_REID_MIN_INTERVAL_MS, FACE_REID_MIN_CONFIDENCE, FACE_REID_MIN_FACE_SIZE, FACE_REID_NORM_TOLERANCE, FACE_CONFIDENCE_THRESHOLD, PERSON_OBSERVATION_EXPORT_ENABLED, PERSON_OBSERVATION_STREAM, PERSON_BBOX_OBSERVATION_MIN_INTERVAL_MS, PERSON_OBSERVATION_MIN_INTERVAL_MS, ... |

## db/migrations 清单
| migration | 首个有效 DDL/说明片段 |
| --- | --- |
| db/migrations/001_init.sql | CREATE EXTENSION IF NOT EXISTS vector;; CREATE EXTENSION IF NOT EXISTS pg_trgm; |
| db/migrations/002_events.sql | CREATE EXTENSION IF NOT EXISTS pgcrypto;; DROP TABLE IF EXISTS tracks; |
| db/migrations/003_audit_logs.sql | CREATE TABLE IF NOT EXISTS audit_logs (; id UUID PRIMARY KEY DEFAULT gen_random_uuid(), |
| db/migrations/004_camera_config.sql | DROP TABLE IF EXISTS camera_rules;; DROP TABLE IF EXISTS camera_zones; |
| db/migrations/005_face_observations.sql | CREATE EXTENSION IF NOT EXISTS vector;; CREATE EXTENSION IF NOT EXISTS pgcrypto; |
| db/migrations/006_gallery_schema.sql | CREATE EXTENSION IF NOT EXISTS vector;; CREATE EXTENSION IF NOT EXISTS pg_trgm; |
| db/migrations/007_match_results_gallery_semantics.sql | ALTER TABLE match_results; ADD COLUMN IF NOT EXISTS query_observation_id UUID |
| db/migrations/008_unified_event_evidence_algorithm_rules.sql | ALTER TABLE events; ADD COLUMN IF NOT EXISTS algorithm_type TEXT NOT NULL DEFAULT '', |
| db/migrations/009_evidence_lifecycle.sql | ALTER TABLE evidence_tasks; ADD COLUMN IF NOT EXISTS metadata_path TEXT, |
| db/migrations/010_algorithm_config_api.sql | ALTER TABLE cameras; ADD COLUMN IF NOT EXISTS input_type TEXT NOT NULL DEFAULT 'rtsp', |
| db/migrations/011_person_bbox_observations.sql | CREATE EXTENSION IF NOT EXISTS pgcrypto;; CREATE TABLE IF NOT EXISTS person_bbox_observations ( |
| db/migrations/012_operator_camera_schema_compat.sql | ALTER TABLE cameras; ADD COLUMN IF NOT EXISTS source_id TEXT, |
| db/migrations/013_storage_maintenance_audit.sql | CREATE EXTENSION IF NOT EXISTS pgcrypto;; CREATE TABLE IF NOT EXISTS maintenance_jobs ( |
| db/migrations/014_worker_media_indexes.sql | CREATE INDEX IF NOT EXISTS events_media_clip_status_with_clip_idx; ON events ((payload -> 'media' ->> 'clip_status')) |
| db/migrations/015_evidence_materialization_manifest_state.sql | ALTER TABLE evidence_tasks; ADD COLUMN IF NOT EXISTS materialization_status TEXT NOT NULL DEFAULT 'manifest_ready', |
| db/migrations/016_camera_rule_zone_id_text_compat.sql | ALTER TABLE camera_rules; DROP CONSTRAINT IF EXISTS camera_rules_zone_id_fkey; |
| db/migrations/017_evidence_database_artifacts.sql | CREATE TABLE IF NOT EXISTS evidence_bundles (; event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE, |
| db/migrations/018_events_table_performance_indexes.sql | CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_created_at_desc; ON events (created_at DESC); |
| db/migrations/019_media_worker_events_queue_indexes.sql | CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_media_generated_clip_queue; ON events (id) |
| db/migrations/020_evidence_queue_playable_indexes.sql | CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_evidence_bundles_playable_recent; ON evidence_bundles (event_created_at DESC) |
| db/migrations/021_qdrant_gallery_sync_outbox.sql | CREATE TABLE IF NOT EXISTS gallery_vector_sync_outbox (; id BIGSERIAL PRIMARY KEY, |
| db/migrations/022_replay_slot_lifecycle.sql | ALTER TABLE evidence_tasks; ADD COLUMN IF NOT EXISTS replay_job_id TEXT, |
| db/migrations/023_evidence_event_links.sql | CREATE TABLE IF NOT EXISTS evidence_event_links (; event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE, |
| db/migrations/024_evidence_task_runtime_epoch_barrier.sql | ALTER TABLE evidence_tasks; ADD COLUMN IF NOT EXISTS runtime_epoch_id TEXT; |
| db/migrations/025_evidence_task_materialization_ready_at.sql | ALTER TABLE evidence_tasks; ADD COLUMN IF NOT EXISTS materialization_ready_at TIMESTAMPTZ; |

## harness/tests 测试清单与初步归属
| 测试文件 | 看起来对应的服务/模块 |
| --- | --- |
| harness/tests/conftest.py | harness/test fixtures |
| harness/tests/test_alert_policy_scoped_cooldown.py | services/event-worker |
| harness/tests/test_algorithm_rule_response_schema.py | modules/savant_security |
| harness/tests/test_algorithm_support_matrix.py | services/api |
| harness/tests/test_analysis_forwarder.py | services/analysis-forwarder |
| harness/tests/test_api_cameras.py | services/api |
| harness/tests/test_api_event_evidence_camera_name.py | services/api |
| harness/tests/test_api_people_delete_maintenance.py | services/api |
| harness/tests/test_api_people_face_registration.py | modules/savant_security |
| harness/tests/test_api_runtime_config_export.py | services/api |
| harness/tests/test_api_storage_maintenance.py | services/api |
| harness/tests/test_behavior_rules_config_loader_integration.py | modules/savant_security |
| harness/tests/test_camera_api_runtime_sources_direct.py | services/api |
| harness/tests/test_camera_config_cli.py | services/api |
| harness/tests/test_camera_config_export.py | services/api |
| harness/tests/test_camera_config_loader.py | services/api |
| harness/tests/test_camera_preview_api.py | services/api |
| harness/tests/test_camera_repository_rows.py | services/api |
| harness/tests/test_camera_runtime_apply_service.py | services/api |
| harness/tests/test_camera_source_controller.py | services/api |
| harness/tests/test_chasing_rule.py | modules/savant_security |
| harness/tests/test_clip_worker_queue_safety.py | services/clip-worker / modules/savant_replay |
| harness/tests/test_completion_aware_replay_admission.py | cross-cutting or needs manual mapping |
| harness/tests/test_config_export_script.py | cross-cutting or needs manual mapping |
| harness/tests/test_crowd_gathering_rule.py | modules/savant_security |
| harness/tests/test_event_repository.py | services/event-worker |
| harness/tests/test_event_worker_recording_policy.py | services/event-worker |
| harness/tests/test_events_table_performance_indexes_static.py | db/migrations / query indexes |
| harness/tests/test_evidence_db_index.py | services/media-worker |
| harness/tests/test_evidence_event_coverage_merge.py | cross-cutting or needs manual mapping |
| harness/tests/test_evidence_materialization_phase0.py | services/media-worker |
| harness/tests/test_evidence_materialization_phase2plus.py | services/media-worker |
| harness/tests/test_evidence_viewer_alarm_machine_time.py | services/evidence-viewer / services/api |
| harness/tests/test_evidence_viewer_database_index.py | services/evidence-viewer / services/api |
| harness/tests/test_evidence_viewer_frame_identity_static.py | services/evidence-viewer / services/api |
| harness/tests/test_evidence_viewer_materialization_state.py | services/media-worker |
| harness/tests/test_face_config.py | modules/savant_security |
| harness/tests/test_face_match_evidence_policy.py | services/face-worker / face gallery |
| harness/tests/test_face_observation_event.py | modules/savant_security |
| harness/tests/test_face_observation_exporter.py | modules/savant_security |
| harness/tests/test_face_person_association.py | modules/savant_security |
| harness/tests/test_face_quality.py | modules/savant_security |
| harness/tests/test_face_reid_gate.py | modules/savant_security |
| harness/tests/test_face_roi_selector.py | modules/savant_security |
| harness/tests/test_face_vector_store.py | modules/savant_security |
| harness/tests/test_face_worker.py | services/face-worker / face gallery |
| harness/tests/test_face_worker_qdrant_backend_static.py | services/face-worker / face gallery |
| harness/tests/test_fall_rule.py | modules/savant_security |
| harness/tests/test_frame_annotation_event_window.py | modules/savant_security |
| harness/tests/test_frame_annotation_exporter_runtime.py | modules/savant_security |
| harness/tests/test_frame_behavior_rule_runtime.py | modules/savant_security |
| harness/tests/test_gallery_enrollment.py | services/face-worker / face gallery |
| harness/tests/test_gallery_match.py | services/face-worker / face gallery |
| harness/tests/test_intrusion_rule_entrypoint.py | modules/savant_security |
| harness/tests/test_loitering_rule.py | modules/savant_security |
| harness/tests/test_media_worker_perf_safety.py | services/media-worker |
| harness/tests/test_midterm_deployment_contract.py | scripts/runtime / infra |
| harness/tests/test_midterm_evidence_drain_check.py | scripts/runtime / infra |
| harness/tests/test_midterm_pressure60_script.py | scripts/runtime / infra |
| harness/tests/test_midterm_pressure_artifact_analyzer.py | scripts/runtime / infra |
| harness/tests/test_midterm_replay_cadence_payload.py | services/clip-worker / modules/savant_replay |
| harness/tests/test_midterm_replay_duration_tuning.py | cross-cutting or needs manual mapping |
| harness/tests/test_midterm_replay_epoch_isolation.py | services/clip-worker / modules/savant_replay |
| harness/tests/test_midterm_replay_evidence_duration_guard.py | services/clip-worker / modules/savant_replay |
| harness/tests/test_midterm_stream_session_isolation.py | cross-cutting or needs manual mapping |
| harness/tests/test_midterm_worker_indexes_static.py | db/migrations / query indexes |
| harness/tests/test_operator_face_registration_static.py | modules/savant_security |
| harness/tests/test_operator_roi_editor_static.py | services/evidence-viewer / services/api |
| harness/tests/test_operator_runtime_overview_static.py | services/evidence-viewer / services/api |
| harness/tests/test_operator_storage_maintenance_static.py | services/evidence-viewer / services/api |
| harness/tests/test_person_pose_adapter.py | modules/savant_security |
| harness/tests/test_phase2_operating_point_appendix.py | scripts/runtime / infra |
| harness/tests/test_phase2_single_t4_pressure.py | scripts/runtime / infra |
| harness/tests/test_phase2_single_t4_readiness.py | scripts/runtime / infra |
| harness/tests/test_pose_behavior_event_payload_contract.py | modules/savant_security |
| harness/tests/test_pose_behavior_rule_activation.py | modules/savant_security |
| harness/tests/test_pose_rule_config_contract.py | modules/savant_security |
| harness/tests/test_post_savant_evidence_bundle_crop.py | services/media-worker |
| harness/tests/test_qdrant_gallery_store.py | services/face-worker / face gallery |
| harness/tests/test_qdrant_gallery_sync_outbox.py | services/face-worker / face gallery |
| harness/tests/test_record_request_idempotency.py | services/event-worker |
| harness/tests/test_replay_shard_routing.py | services/clip-worker / modules/savant_replay |
| harness/tests/test_roi_polygon_validation.py | cross-cutting or needs manual mapping |
| harness/tests/test_roi_preview.py | cross-cutting or needs manual mapping |
| harness/tests/test_rolling_cache_materialization.py | services/media-worker |
| harness/tests/test_rule_registry.py | modules/savant_security |
| harness/tests/test_running_rule.py | modules/savant_security |
| harness/tests/test_runtime_config_export.py | services/api |
| harness/tests/test_runtime_control_service.py | services/api |
| harness/tests/test_runtime_overview_api.py | services/api |
| harness/tests/test_runtime_performance_service.py | services/api |
| harness/tests/test_runtime_topology_service.py | services/api |
| harness/tests/test_savant_perf_metrics_contract.py | modules/savant_security |
| harness/tests/test_savant_redis_stream_writer.py | modules/savant_security |
| harness/tests/test_savant_supervisor_service.py | modules/savant_security |
| harness/tests/test_storage_maintenance_evidence_preview.py | services/api |
| harness/tests/test_storage_maintenance_face_preview.py | modules/savant_security |
| harness/tests/test_storage_maintenance_path_safety.py | services/api |
| harness/tests/test_time_utils.py | cross-cutting or needs manual mapping |
| harness/tests/test_trajectory_query.py | services/face-worker / face gallery |
| harness/tests/test_video_file_sink_pressure_parser.py | services/media-worker |
| harness/tests/test_watchlist_evidence_identity_continuation.py | cross-cutting or needs manual mapping |

## docs/ 与 specs/ 文档清单
| 文档 | 标题 | 大致主题（H2 摘要或标题） |
| --- | --- | --- |
| docs/code_review/00_inventory.md | Phase 0 Inventory | 本次代码审查 Phase 0 全仓库事实清点 |
| docs/code_review/01_module_boundaries.md | Phase 1 Module Boundaries | 本次代码审查 Phase 1 模块边界确认 |
| docs/archive/phase-only/20260610/C2.15UUID-FIRST.md | (no H1 found) | (no H1 found) |
| docs/archive/phase-only/20260610/analy.md | (no H1 found) | (no H1 found) |
| docs/archive/phase-only/20260610/api_algorithm_rule_design.md | API Algorithm Rule Design | Registry; Rule API; Runtime Export |
| docs/archive/phase-only/20260610/artifact_output_policy.md | Artifact Output Policy (Phase H1) | 1. 核心原则; 2. 环境变量; 3. Run ID 格式 |
| docs/archive/phase-only/20260610/c1e_1_evidence_trust_hardening.md | C1E.1 Evidence Trust Hardening | Scope; Duration Verification; Keyframe Fallback Policy |
| docs/archive/phase-only/20260610/c1e_2_replay_clip_garbling_fix.md | C1E.2 Replay Clip Garbling Fix | Scope; Garbling Cause; Real-Time Pacing |
| docs/archive/phase-only/20260610/c1e_3_replay_upstream_continuity_diagnosis.md | C1E.3 Replay Upstream Compressed-Frame Continuity Diagnosis | Status; Clean Runtime Verification; Evidence Bundle Diagnosis |
| docs/archive/phase-only/20260610/c1e_4_rtsp_transport_and_replay_continuity_isolation.md | C1E.4 RTSP Transport and Replay Continuity Isolation | Status; Why C1E.3 Was Not Enough; RTSP Transport |
| docs/archive/phase-only/20260610/c1e_replay_evidence_integration.md | C1E Replay Evidence Integration | Final Phase Conclusions; Scope; Runtime Drift Audit |
| docs/archive/phase-only/20260610/c1f1_same_frame_pose_face_detection.md | C1F.1 Same-Frame Dual Primary Detection — Final Report | 1. Objective; 2. Phase Results; 3. C1F.1d Isolation Summary |
| docs/archive/phase-only/20260610/c1f1a_dual_primary_current_state_audit.md | C1F.1a Dual Primary Current State Audit | Scope; Audit Results; Pipeline Element Order |
| docs/archive/phase-only/20260610/c1f1c_same_frame_debug_summary_export.md | C1F.1c Same-Frame Pose + Face Debug Summary Export | Scope; Implementation; Output |
| docs/archive/phase-only/20260610/c1f2a_face_observation_pipeline_audit.md | C1F.2a Face Observation Pipeline Audit | 1. Objective; 2. Target Chain; 3. Audit Results |
| docs/archive/phase-only/20260610/c1f2b_face_pipeline_runtime_reality_correction.md | C1F.2b Face Pipeline Runtime Reality Correction | 1. Purpose; 2. Current Runtime Validated Capabilities; 3. NOT Runtime Validated |
| docs/archive/phase-only/20260610/c1f2c_adaface_runtime_enablement.md | C1F.2c AdaFace Runtime Enablement | 1. Objective; 2. Relationship to C1F.2b; 3. Why Only AdaFace |
| docs/archive/phase-only/20260610/c1f2d_face_observation_redis_smoke.md | C1F.2d — Face Observation Redis Export Smoke | 目标; 与 C1F.2c 的关系; 为什么复用已有 gate/exporter |
| docs/archive/phase-only/20260610/c1f3_restore_registration_and_gallery_match.md | C1F.3 — Restore Archived Face Registration + Gallery Match Smoke | Goal; Background; Archived Images |
| docs/archive/phase-only/20260610/c1f3b_face_worker_longrun_gallery_recognition.md | C1F.3b — Production-like Face Worker Long-run Gallery Recognition | Why C1F.3 Was Not Production-grade; Production-like Definition; Face-worker Service |
| docs/archive/phase-only/20260610/c1f3c_15min_face_recognition_replay_monitor.md | C1F.3c — 15-minute Face Recognition Replay Monitor | Goal; Why 15 Minutes; Production-like Face-worker Chain |
| docs/archive/phase-only/20260610/c1f4a_face_match_evidence_overlay_bundle.md | C1F.4a Face Match Evidence Overlay Bundle | Goal; Why C1F.3c Had No Visual Output; Frame Anchors |
| docs/archive/phase-only/20260610/c1f4b_evidence_overlay_preview.md | C1F.4b Evidence Overlay Preview | Goal; Input Bundle; Overlay Source |
| docs/archive/phase-only/20260610/c1f4c_unified_file_based_evidence_viewer.md | C1F.4c Unified File-Based Evidence Viewer | Goal; Non-Goals; Service Structure |
| docs/archive/phase-only/20260610/c1g1_algorithm_config_api_and_operator_page.md | C1G.1 Algorithm Configuration API + Simple Operator Frontend | Goal; Observation / Match / Alert Semantics; API |
| docs/archive/phase-only/20260610/c1g1b_operator_frontend_enhancement.md | C1G.1b — Operator Frontend Enhancement | Overview; Page Path; Supported Configuration |
| docs/archive/phase-only/20260610/c1g2_db_config_export_runtime_config.md | C1G.2 DB Config Export to Generated Runtime Config | Goal; Source Of Truth; CLI |
| docs/archive/phase-only/20260610/c1g2c_evidence_bundle_boundary_annotation_integrity.md | C1G.2c Evidence Bundle Boundary And Annotation Integrity | Scope; Root Causes; Fixes |
| docs/archive/phase-only/20260610/c2_demo_checklist_2026_06_07.md | C2 Demo Checklist | Baseline; Open Demo Artifacts; Confirm Watchlist Evidence Fields |
| docs/archive/phase-only/20260610/c2_post_savant_watchlist_evidence_rebaseline_2026_06_07.md | C2 Post-Savant Watchlist Evidence Rebaseline | Executive Summary; Current Final Status; Milestone Table |
| docs/archive/phase-only/20260610/c2_rebaseline_2026_06_07.md | C2 Rebaseline - Post-Savant Evidence Topology | Current Decision; Completed Milestones; Current Known Limitations |
| docs/archive/phase-only/20260610/current_runbook_single_camera_evidence.md | Current Runbook — Single-Camera Evidence Pipeline (E1 Dev Path) | 0. Mainline entrypoint at a glance; 1. Stop legacy / conflicting stacks; 2. Check media output lockdown |
| docs/archive/phase-only/20260610/d1_rtsp_15min_detection_report.md | D1 RTSP 15-Minute Detection Report | Runtime Contract; Docker Discipline; Outputs |
| docs/archive/phase-only/20260610/git_reconcile_c1_2_f0_2026_05_25.md | Git Reconciliation — C1.2 and F0 Commit Order | 1. Original Problem; 2. Reconciliation Goal; 3. Backup Branches |
| docs/archive/phase-only/20260610/legacy_and_prototype_inventory.md | Legacy and Prototype Inventory | Legacy / Prototype Areas; Keep; Archive or Prototype |
| docs/archive/phase-only/20260610/media_output_directory_policy.md | Media Output Directory Policy | 1. 目的; 2. 目录规范; 3. API URL 映射 |
| docs/archive/phase-only/20260610/model_assets_manifest.md | Model Assets Manifest | Face Intelligence Pipeline (first version, F1 / F2); Existing models (already prepared); Policy |
| docs/archive/phase-only/20260610/module_and_compose_consolidation_plan.md | Module & Compose Consolidation Plan | 1. Current Module Inventory; 2. Current Compose Inventory; 3. Consolidation Principles |
| docs/archive/phase-only/20260610/operator_add_algorithm.md | Operator: Add an Algorithm | Purpose; Principles; Current Algorithm Stack |
| docs/archive/phase-only/20260610/operator_add_algorithm_rule.md | Operator: Add Algorithm Rule | Algorithm List; Camera, ROI, And Rule; Enable Or Disable |
| docs/archive/phase-only/20260610/operator_add_camera.md | Operator: Add a Camera | Purpose; Runtime; Add the Camera |
| docs/archive/phase-only/20260610/operator_configure_roi_and_rules.md | Operator: Configure ROI and Rules | Purpose; Concepts; Example Shape |
| docs/archive/phase-only/20260610/operator_event_evidence_flow.md | Operator: Event Evidence Flow | Flow; Media Paths; Debug Sinks |
| docs/archive/phase-only/20260610/operator_register_face.md | Operator: Register a Face | Purpose; Image Requirements; Environment |
| docs/archive/phase-only/20260610/operator_run_local_video_face_test.md | Operator: Run Local Video Face Test | Purpose; Scope; F4.3A Observation Smoke |
| docs/archive/phase-only/20260610/operator_run_single_rtsp_camera_inference.md | Operator: Run Single RTSP Camera Inference | Purpose; Stream From Another Machine; Verify RTSP Reachability |
| docs/archive/phase-only/20260610/p1_single_stream_replay_clip_output.md | Phase P1 Single-Stream Replay Clip Output | Step 1 Inventory; Required P1 Topology; Blocker |
| docs/archive/phase-only/20260610/p1a_replay_inline_pass_through_topology.md | P1a Replay Inline Pass-through Topology | Streams; Runtime; Latest Local Verification |
| docs/archive/phase-only/20260610/p1b_replay_manual_job_to_video_sink.md | Phase P1b — Replay Manual Job to Video File Sink | Topology; Runtime Configuration; Files |
| docs/archive/phase-only/20260610/p1b_rtsp_replay_manual_job_to_video_sink.md | P1b-RTSP Replay Manual Job to Video File Sink | Input; Runtime Topology; Runtime |
| docs/archive/phase-only/20260610/p1c_1_controlled_event_recording_policy.md | P1c.1 Controlled Event Recording Policy | Problem; P1c.1 Policy; Replay Job |
| docs/archive/phase-only/20260610/p1c_2_runtime_discipline_and_evidence_contract_hardening.md | P1c.2 Runtime Discipline and Evidence Contract Hardening | Entry Audit; Hardening Scope; Runtime Contract |
| docs/archive/phase-only/20260610/p1c_rtsp_replay_event_evidence_bundle.md | P1c-RTSP Replay Event-triggered Evidence Bundle | Goal; Boundary; Runtime Test Policy |
| docs/archive/phase-only/20260610/performance_test_preflight.md | Performance Test Preflight | Purpose; Required Preflight Checks; Next Performance Matrix |
| docs/archive/phase-only/20260610/phase2_summary.md | Phase 2 Completion Report | Commit History; Phase Goals; Three Types of Acceptance Tests |
| docs/archive/phase-only/20260610/phase3a_summary.md | Phase 3A — Official Replay Media MVP Summary | Goal; Final Architecture; Component Responsibilities |
| docs/archive/phase-only/20260610/phase3b1_summary.md | Phase 3B.1 — Replay Timestamp Anchoring Fix Summary | Goal; Why Timestamp Anchoring Matters; Changes |
| docs/archive/phase-only/20260610/phase3b2_compose_smoke.md | Phase 3B.2 Docker Compose Integration Smoke | Commands Run; Manual Verification Summary; Services Status |
| docs/archive/phase-only/20260610/phase3b_summary.md | Phase 3B — Replay Media Reliability & Contract Hardening Summary | Goal; Changes by Area; Test Results |
| docs/archive/phase-only/20260610/phase3c_summary.md | Phase 3C — Event Snapshot MVP | Design Goal; Snapshot Extraction Policy; Modified Files |
| docs/archive/phase-only/20260610/phase3e_summary.md | Phase 3E — Annotated Snapshot MVP | Design Goal; Annotation Content; Bbox Trust Guard (Phase 3E.1) |
| docs/archive/phase-only/20260610/phase3f0_2_diagnosis_report.md | Phase 3F0.2 — Bbox/Snapshot Alignment Diagnosis Report | Selected Events; Image Dimensions; Clip Metadata |
| docs/archive/phase-only/20260610/phase3f0_3_topology_review.md | Phase 3F0.3 — Single Ingestion Topology / Replay-Savant Alignment Review | 1. Current Video Topology (infra/docker-compose.phase3b.yml); 2. Why Savant Is NOT Behind Replay; 3. Topology Options Comparison |
| docs/archive/phase-only/20260610/phase3f0_postgres_persistence.md | Phase 3F0.2a — Postgres Persistence Hotfix | Problem; Fix; Verification |
| docs/archive/phase-only/20260610/phase3f0_real_savant_bbox_summary.md | Phase 3F0 — Real Savant Bbox Media Evidence | Design Goal; Why Replay metadata.json Cannot Be the Trust Source; Bbox Trust Model (Phase 3F0.1) |
| docs/archive/phase-only/20260610/phase3h_1_metadata_sink_verification.md | Phase 3H.1 — Official Metadata Sink Verification | 1. Purpose; 2. Topology; 3. What Was Changed |
| docs/archive/phase-only/20260610/phase3h_2_savant_output_video_sink_poc.md | Phase 3H.2 — Savant Output Video File Sink POC | 1. Purpose; 2. Full Topology; 3. What Was Changed |
| docs/archive/phase-only/20260610/phase3h_official_adapter_ingestion_poc.md | Phase 3H — Official ZeroMQ Adapter Ingestion POC | 1. Purpose; 2. What Was Built; 3. Evidence |
| docs/archive/phase-only/20260610/phase_c1_1_camera_config_runtime_bridge.md | Phase C1.1 — Camera Config Runtime Bridge | 1. What C1 already delivered; 2. Why API → evidence is still NOT one-shot; 3. The bridge components landed by C1.1 |
| docs/archive/phase-only/20260610/phase_c1_2_official_adapter_runtime.md | Phase C1.2 — Official Adapter Camera Runtime Control | 1. Goal recap; 2. Pieces that landed; 3. camera_id ↔ source_id |
| docs/archive/phase-only/20260610/phase_c1_2_official_adapter_runtime_plan.md | Phase C1.2 — Official Adapter Runtime Plan | 1. Savant's official adapter ↔ module separation; 2. camera_id ↔ source_id mapping; 3. Current phase3h-zmq gaps vs. the official model |
| docs/archive/phase-only/20260610/phase_c1_3_operator_camera_config_cli.md | Phase C1.3 — Operator Camera Config CLI + ROI Polygon Validation MVP | 1. Why C1.3; 2. ROI polygon rule (final, identical in API and loader); 3. The CLI |
| docs/archive/phase-only/20260610/phase_c1_camera_roi_config_mvp.md | Phase C1 — Camera / RTSP / ROI Configuration MVP | 1. Background; 2. Goals; 3. API surface |
| docs/archive/phase-only/20260610/phase_c1m0_evidence_timeline_diff_audit.md | C1M Evidence Alignment Root Cause | Findings; Runtime Guard; Watchlist Visual Binding |
| docs/archive/phase-only/20260610/phase_c1m5_pose_coordinate_transform_repair.md | C1M Pose Keypoint And Coordinate Repair | Keypoint Export; Coordinate Restore |
| docs/archive/phase-only/20260610/phase_c2_10_runtime_api_viewer_recovery.md | Phase C2.10 - Runtime API / Evidence Viewer Recovery for Watchlist Evidence | Result; Inputs; Runtime Status |
| docs/archive/phase-only/20260610/phase_c2_11_demo_packaging.md | Phase C2.11 - Demo Packaging / Operator Walkthrough Bundle | Result; Inputs; Package Output |
| docs/archive/phase-only/20260610/phase_c2_12a_external_face_enrollment_rebuild.md | Phase C2.12A - External Face Enrollment Rebuild for Reese / Finch | Goal; Input Directory; Registration Method |
| docs/archive/phase-only/20260610/phase_c2_12b_external_person_watchlist_evidence.md | Phase C2.12B - External Person Watchlist Evidence Search | Goal; Inputs; Search Method |
| docs/archive/phase-only/20260610/phase_c2_12c_runtime_reese_finch_capture.md | Phase C2.12C-A - Runtime Reese / Finch Capture | Direction; Inputs; Runtime Pipeline Status |
| docs/archive/phase-only/20260610/phase_c2_12d_runtime_finch_visual_evidence_join.md | Phase C2.12D - Runtime Finch Visual Evidence Join | Goal; C2.12C Finch Match; Join Strategy |
| docs/archive/phase-only/20260610/phase_c2_13_rtsp_watchlist_intrusion_smoke.md | Phase C2.13 - Real RTSP Watchlist + Intrusion Dual Algorithm Smoke | Goal; RTSP Source; Watchlist Setup |
| docs/archive/phase-only/20260610/phase_c2_13p_rtsp_pose_intrusion_repair.md | Phase C2.13P - RTSP Pose / Person Observation Repair | Why C2.13R Intrusion Was Partial; Pose / Person Path; Repair |
| docs/archive/phase-only/20260610/phase_c2_13r_rtsp_runtime_alignment.md | Phase C2.13R - RTSP Runtime Source Alignment | Why C2.13 Was Partial; Alignment; Worker Runtime |
| docs/archive/phase-only/20260610/phase_c2_13v_rtsp_video_retention_audit.md | C2.13V RTSP Video Evidence and Retention Audit | Why This Phase Exists; Current RTSP Source; Services Inspected |
| docs/archive/phase-only/20260610/phase_c2_14_rtsp_segment_ring_event_clip_builder.md | C2.14 RTSP Segment Ring + Event Clip Builder MVP | Why This Phase Exists; RTSP Source; Why Replay Is Not The Main Path |
| docs/archive/phase-only/20260610/phase_c2_14b_runtime_rtsp_segment_writer_event_clip.md | C2.14B Runtime RTSP Segment Writer and Event Clip | Why C2.14B exists; Current RTSP source; Runtime sink config changes |
| docs/archive/phase-only/20260610/phase_c2_14c_rtsp_intrusion_evidence_acceptance.md | C2.14C RTSP Intrusion Evidence Acceptance | Scope; Bundle Relationship; Acceptance Checks |
| docs/archive/phase-only/20260610/phase_c2_14d_rtsp_watchlist_known_face_evidence.md | C2.14D RTSP Watchlist Known-Face Evidence | Scope; Evidence Path; Identity Binding |
| docs/archive/phase-only/20260610/phase_c2_15_replay_first_single_stream_evidence.md | C2.15 Replay-First Single-Stream Evidence Baseline | Current Active Runtime; Goal; Runtime Files |
| docs/archive/phase-only/20260610/phase_c2_3b_r2_stable_sink_workaround_rebaseline.md | C2.3B-R2H Stable Sink Workaround Rebaseline | Result; What This Is Not; What Passed |
| docs/archive/phase-only/20260610/phase_c2_3b_r2_time_domain_integrity_repair.md | C2.3B-R2 Time-Domain Integrity Repair | Scope; Prior Diagnosis; R2 Repair Policy |
| docs/archive/phase-only/20260610/phase_c2_3q_evidence_output_correctness_audit.md | C2.3Q Evidence Output Correctness Audit | Result; Inputs; Audit Summary |
| docs/archive/phase-only/20260610/phase_c2_4_identity_binding_evidence_patch.md | Phase C2.4 - Identity Binding / Known Face Evidence Patch MVP | Result; Input Evidence; Identity Binding Strategy |
| docs/archive/phase-only/20260610/phase_c2_5_watchlist_event_evidence_semantics.md | Phase C2.5 - Watchlist Event Evidence Semantics MVP | Result; Input Bundle; Watchlist Event Contract |
| docs/archive/phase-only/20260610/phase_c2_6_live_watchlist_from_face_worker.md | Phase C2.6 - Live Watchlist Event From Face-Worker MVP | Result; Inspection Summary; Input Observation |
| docs/archive/phase-only/20260610/phase_c2_6r_redis_consumer_watchlist_event.md | Phase C2.6R - Redis Consumer Loop Watchlist Event Proof | Result; Inspection Summary; Execution Mode |
| docs/archive/phase-only/20260610/phase_c2_7_event_worker_persistence_api_query.md | Phase C2.7 - Event-Worker Persistence / API Event Query MVP | Result; Input; Execution Mode |
| docs/archive/phase-only/20260610/phase_c2_8_api_evidence_detail_recovery.md | Phase C2.8 - API/UI Evidence Detail Recovery MVP | Result; Input; Query Path |
| docs/archive/phase-only/20260610/phase_e1_alert_evidence_mvp.md | Phase E1 — Alert Evidence MVP (Single-Ingestion Dev Path) | 1. Purpose; 2. Topology; 3. Why Not Phase 3B Replay Bypass |
| docs/archive/phase-only/20260610/phase_e1_run_report.md | Phase E1 Run Report — 2026-05-25 10:51 UTC | Run Summary; Generated Evidence; Database State |
| docs/archive/phase-only/20260610/phase_f0_face_detection_readiness.md | Phase F0 — Face Detection Readiness / Isolated Harness | 1. F0 Target; 2. Face Intelligence Pipeline Overview; 3. Redis Stream Contract (hard constraint) |
| docs/archive/phase-only/20260610/phase_f1_0_face_detector_strategy_review.md | Phase F1.0 — Face Detector Strategy Review | 0. User final decision (2026-05-26) — supersedes parts of this doc; 1. Current face-related assets in the repo; 2. Does Savant support SCRFD out of the box? |
| docs/archive/phase-only/20260610/phase_f1_1a_in_pipeline_face_architecture_lock.md | Phase F1.1a — In-Pipeline YOLOv8-Face + AdaFace Architecture Lock | 1. Final data flow; 2. The Redis boundary rule; 3. Why AdaFace stays in the Savant pipeline |
| docs/archive/phase-only/20260610/phase_f1_1b_yolov8_face_onnx_asset_verification.md | Phase F1.1b — YOLOv8-Face ONNX Asset Verification | 1. Inputs to this phase; 2. Inspection tool; 3. Inspection result (verbatim, trimmed to the salient fields) |
| docs/archive/phase-only/20260610/phase_f1_1c_savant_face_reid_official_reference.md | Phase F1.1c - Savant Face ReID Official Reference | 1. What the official sample does; 2. Official module wiring; 3. Official model asset |
| docs/archive/phase-only/20260610/phase_f1_2_yolov8_face_runtime.md | Phase F1.2 — YOLOv8-Face Detector Runtime Integration | 1. What Changed; 2. Why Official Converter; 3. Detector Batch Limitation |
| docs/archive/phase-only/20260610/phase_f1_3_face_person_association.md | Phase F1.3 — Face-Person Association | 1. What Changed; 2. Architecture; 3. Unit Tests |
| docs/archive/phase-only/20260610/phase_f2_0_adaface_onnx_asset_verification.md | Phase F2.0 — AdaFace ONNX Asset Verification | 1. Current HEAD; 2. Model Path; 3. ONNX Inspection Result |
| docs/archive/phase-only/20260610/phase_f2_1_adaface_runtime.md | Phase F2.1 — AdaFace Runtime | Purpose; Official Sample Mapping; What We Reuse |
| docs/archive/phase-only/20260610/phase_f2_2_face_reid_gate.md | Phase F2.2 — Face ReID Gate and Throttle | Purpose; Gate Rules (MVP); Throttle Policy |
| docs/archive/phase-only/20260610/phase_f2_3_face_observation_redis.md | Phase F2.3 — Redis Face Observations Producer | Purpose; Final Redis Payload Schema; Idempotency Key |
| docs/archive/phase-only/20260610/phase_f3_2_pgvector_similarity_harness.md | Phase F3.2 — pgvector Similarity Search Harness | Purpose; Scope; Out of scope |
| docs/archive/phase-only/20260610/phase_f3_3_gallery_retention_match_policy_design.md | Phase F3.3 — Gallery / Retention / Match Policy Design | 1. In Scope; 2. Out of Scope; 3. Final Decisions |
| docs/archive/phase-only/20260610/phase_f3_4_gallery_schema_enrollment.md | Phase F3.4 — Gallery Schema and Enrollment Harness | 1. Summary; 2. Deliverables; 3. Tests |
| docs/archive/phase-only/20260610/phase_f3_5_face_intelligence_foundation_summary.md | Face Intelligence Foundation — F1 至 F3.5 阶段总结 | 1. 已完成 Phases 和 Commits; 2. 当前完整链路; 3. 当前数据表 |
| docs/archive/phase-only/20260610/phase_f3_5_gallery_match_harness.md | Phase F3.5 — Gallery Match Harness | 目标; 范围; 语义 |
| docs/archive/phase-only/20260610/phase_f3_5b_one_face_recognition_e2e.md | Phase F3.5b — One Face Recognition End-to-End Smoke | Purpose; What This Proves; What This Does NOT Prove |
| docs/archive/phase-only/20260610/phase_f3_6_registered_person_trajectory_query.md | Phase F3.6 — Registered Person Trajectory Query Harness | Summary; Files; TrajectoryRepository |
| docs/archive/phase-only/20260610/phase_f4_2_external_face_registration_mvp.md | F4.2 External Submitted Image -> Gallery Registration MVP | Goal; F4.2 vs F4.2b vs F4.2c; Explicit Scope |
| docs/archive/phase-only/20260610/phase_r1_mainline_consolidation_plan.md | Phase R1 — Mainline Module / Compose Consolidation Plan | 1. Verified mainline capability (E1 closure); 2. POC / reference content (NOT mainline); 3. Future module target |
| docs/archive/phase-only/20260610/production_ingestion_topology_policy.md | Production Ingestion Topology Policy | 1. 文档目的; 2. 生产目标拓扑; 3. 临时允许拓扑 |
| docs/archive/phase-only/20260610/project_rebaseline_2026_05_25.md | Project Rebaseline — 2026-05-25 | 1. Purpose; 2. Completed Capabilities; 3. Incomplete Capabilities |
| docs/archive/phase-only/20260610/r2_file_inventory_and_cleanup_plan.md | R2 File Inventory and Cleanup Plan | Scope; F4.3 Script and Document Inventory; Unrelated Dirty or Pre-Existing Files |
| docs/archive/phase-only/20260610/r2_pipeline_runtime_verification.md | R2 Pipeline Runtime Verification | Result; Static Evidence; Architecture Boundary |
| docs/archive/phase-only/20260610/r3_1_code_inventory.md | R3.1 Code Inventory | Reviewed Areas; Architecture Questions; Reusable Code |
| docs/archive/phase-only/20260610/r3_1_evidence_output_architecture_plan.md | R3.1 Evidence Output Architecture Plan | Output Classes; Unified Flow; Output Path |
| docs/archive/phase-only/20260610/r3_1_implementation_plan.md | R3.1 Implementation Plan | R3.1A - Behavior Event Evidence MVP; R3.1B - Face Match Evidence MVP; R3.1C - Media Retention and Debug Sink Policy |
| docs/archive/phase-only/20260610/r3_1_performance_design.md | R3.1 Performance Design | Dual T4 / 60 Streams Constraints; Async Evidence Generation; Cooldown / Rate Limit |
| docs/archive/phase-only/20260610/r3_1a_behavior_event_evidence_mvp.md | R3.1A Behavior Event Evidence MVP | Scope; Lifecycle; Metadata JSON |
| docs/archive/phase-only/20260610/r3_1b_face_match_evidence_mvp.md | R3.1B Face Match Evidence MVP | Scope; Watchlist Hit MVP; Idempotency |
| docs/archive/phase-only/20260610/r3_1c_media_retention_and_debug_sink_policy.md | R3.1C Media Retention and Debug Sink Policy | Production Evidence Path; Debug Sink Boundary; Source Adapter Cache Boundary |
| docs/archive/phase-only/20260610/r3_2a_metadata_snapshot_evidence_mvp.md | R3.2A Metadata + Snapshot Evidence MVP | Scope; Output Path; Snapshot Capture Backend |
| docs/archive/phase-only/20260610/r3_2a_snapshot_capture_backend_policy.md | R3.2A Snapshot Capture Backend Policy | Corrected Snapshot Source Strategy; Metadata Requirements; Evidence Media Policy |
| docs/archive/phase-only/20260610/r3_2b_raw_clip_evidence_mvp.md | R3.2B Raw Clip Evidence MVP | Scope; Clip Source Strategy; Metadata Schema Additions |
| docs/archive/phase-only/20260610/r3_2d_debug_visual_rejection_report.md | R3.2D Debug Visual Rejection Report | Failure Symptoms; Root Cause; Why Rtsp Current And Unmapped Raw Fallback Cannot Be Used |
| docs/archive/phase-only/20260610/r3_2e_local_video_debug_evidence.md | R3.2E Local Video Debug Evidence Mode | Purpose; Required Inputs; Outputs |
| docs/archive/phase-only/20260610/r3_3_code_inventory.md | R3.3 Code Inventory | Current Available Code; Current Missing Fields; Timestamp Sources |
| docs/archive/phase-only/20260610/r3_3_implementation_plan.md | R3.3 Implementation Plan | R3.3A — Timestamp / Frame Metadata Inspection; R3.3B — Choose Mapping Strategy And Schema; R3.3C — Exact Snapshot MVP |
| docs/archive/phase-only/20260610/r3_3_options_replay_vs_segment_recording.md | R3.3 Options: Replay Vs Segment Recording | Option A — Savant Replay / Keyframe Mapping; Option B — Controlled Segment Recording; Option C — External NVR / MediaMTX Recording |
| docs/archive/phase-only/20260610/r3_3_timeline_mapping_evidence_plan.md | R3.3 Timeline Mapping Evidence Plan | Current R3.2 Alignment Failure; Why Local Video Debug Aligns; Target Production Chain |
| docs/archive/phase-only/20260610/r3_3a0_frame_uuid_runtime_probe.md | R3.3A0 Savant Frame UUID / Replay Anchor Runtime Probe | Purpose; Why Runtime Probe Comes First; Probe Scope |
| docs/archive/phase-only/20260610/r3_3a1_unified_frame_anchor_propagation.md | R3.3A1 Unified Frame Anchor Propagation | Architecture Boundary; Frame Anchor; Propagation Rules |
| docs/archive/phase-only/20260610/r3_3a2a_frame_uuid_source_frame_identity_report.md | R3.3A2a Frame UUID Source-Frame Identity Report | Test Input; Event Information; Trace Hit Result |
| docs/archive/phase-only/20260610/r3_3a2b_replay_keyframe_visual_clip_report.md | R3.3A2b Replay Keyframe Visual Clip Report | POC Harness; Event And Anchor Information; Replay Anchor Decision |
| docs/archive/phase-only/20260610/r3_3a2c_replay_uuid_domain_report.md | R3.3A2c Replay/Cache UUID Domain Report | Scope; Topology Inspection; UUID Generation Point |
| docs/archive/phase-only/20260610/r3_3a_replay_uuid_feasibility.md | R3.3A Replay UUID Feasibility Inspection | Executive Summary; Direct Answers; 1. Replay Service Status |
| docs/archive/phase-only/20260610/r3_unified_event_and_evidence_architecture.md | R3 Unified Event, Evidence, and Algorithm Plugin Architecture | Algorithm Registry; Event Types; SecurityEvent Contract |
| docs/archive/phase-only/20260610/runtime_rebaseline_20260608_canonical_restore.md | Runtime Rebaseline: Canonical Repo Restore | Why this restore was done; Dirty main archive; New canonical baseline |
| docs/archive/phase-only/20260610/runtime_test_policy.md | Runtime Test Policy | 1. Bind Mount Verification Before Runtime Smoke; 2. Restart-Not-Rebuild Default; 2.4 Docker Daemon Access |
| docs/archive/phase-only/20260610/v1_1_visual_annotation_correctness.md | V1.1 Visual Annotation Correctness | Inspection Summary; Correctness Rules; BBox Parser |
| docs/archive/phase-only/20260610/v1_3_frame_aligned_visual_rendering_fix.md | V1.3 Frame-Aligned Visual Rendering Fix | Diagnosis of V1.1 Output; Frame-Aligned Resolver; Diagnosis Fields |
| docs/archive/phase-only/20260610/v1_4_actual_frame_image_source_proof.md | V1.4 Actual Frame Image Source Proof | Current State; Acceptance Boundary; Source Extraction |
| docs/archive/phase-only/20260610/v1_visual_result_output_mvp.md | V1 Visual Result Output MVP | Scope; Inputs; Annotations |
| docs/compose_inventory.md | Compose Inventory | Current Deployment; Runtime Shape; Archived Stage Entrypoints |
| docs/current_mainline_status.md | Current Mainline Status | 2026-06-15 Midterm Project Version; Current Runtime Chain; Current Calibration |
| docs/midterm_8090_camera_source_control_evidence_safety_2026-06-26.md | Midterm 8090 摄像头源控制与 Evidence 安全边界报告 | 结论; 修复前问题; 当前控制边界 |
| docs/midterm_8090_port_integration.md | Midterm 8090 端口功能与程序对接现状 | 总结; 2026-06-24 操作台一致性修正; 2026-06-10 实测状态 |
| docs/midterm_8090_single_operator_surface_cleanup_2026-06-26.md | 8090 单一操作台入口清理记录 | 结论; 为什么清理; 本次代码改动 |
| docs/midterm_8090_topology_management_8fps_report_2026-06-29.md | Midterm 8090 推理拓扑管理与 60 路 8fps 压测报告 | 结论; 8090 新增能力; 本次修复点 |
| docs/midterm_analysis_forwarder_30_stream_offline_pressure_2026-06-26.md | Midterm Analysis-forwarder 30-stream Offline Pressure Record - 2026-06-26 | Scope; Command; Workload |
| docs/midterm_camera_runtime_source_identity_findings_2026-06-12.md | Midterm Camera Runtime and Source Identity Findings | Summary; Runtime Evidence; Component Boundaries |
| docs/midterm_clean_machine_migration_2026-06-25.md | Midterm 新机器干净迁移说明 | 1. 干净迁移只需要拷贝什么; 2. 为什么有 3 个 replay-midterm 目录; 3. downloads 是什么 |
| docs/midterm_clip_worker_proof_gate_rootcause_2026-06-17.md | Midterm Clip-Worker Proof/Gate Root Cause and Solution | 1. Symptoms (observed, from the prior 20-minute window); 2. Correction to the 2026-06-16 findings; 3. Root cause (code-level) |
| docs/midterm_current_program_technical_analysis_2026-06-28.md | Midterm 当前程序技术分析报告 - 2026-06-28 | 1. 报告范围; 2. 执行摘要; 3. 当前部署边界 |
| docs/midterm_data_directory_inventory.md | Midterm /data 目录核对与清理记录 | 核对依据; 当前必须保留的目录; 本次已删除或清空的历史内容 |
| docs/midterm_deployment.md | Midterm Deployment | Files; Start; Savant Source-Reset Hardening |
| docs/midterm_downstream_evidence_performance_2026-06-28.md | Midterm 下游证据链 60 路 3 FPS 修复与压测报告 - 2026-06-28 | 结论; 本轮修复; 压测结果 |
| docs/midterm_dual1gpu_batch4_4fps_report_2026-06-28.md | Midterm 单卡双分支 60 路 4 FPS Batch4 压测报告 - 2026-06-28 | 结论; 压测配置; 结果摘要 |
| docs/midterm_dual1gpu_batch4_8fps_report_2026-06-28.md | Midterm 单卡双分支 60 路 8 FPS Batch4 压测报告 - 2026-06-28 | 结论; 压测配置; 结果摘要 |
| docs/midterm_dual1gpu_evidence_chain_4fps_8fps_report_2026-06-29.md | Midterm 单卡双分支 60 路证据链 4 FPS / 8 FPS 压测报告 | 结论; Artifact; 4 FPS 结果 |
| docs/midterm_event_algorithm_implementation_research_2026-06-28.md | Midterm 事件算法实现方式调研 - 2026-06-28 | 外部依据; 总体实现原则; 算法结论总表 |
| docs/midterm_event_detection_completion_scope_2026-06-28.md | Midterm 事件检测补全阶段边界 - 2026-06-28 | 当前算法支持状态; 2026-06-28 当前进度快照; 补全阶段建议 |
| docs/midterm_evidence_cooldown_double_gate_fix_2026-06-24.md | Midterm Evidence Cooldown 双重门控根因与修复 | 问题现象; 根因分析; 修复方案 |
| docs/midterm_evidence_output_cleanup_2026-06-26.md | Midterm Evidence 输出清理记录（2026-06-26） | 结论; 本次移除; 保留的保护 |
| docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md | Midterm 证据生成延迟与分片流水线改造方案 | 1. 背景; 2. 当前证据链路; 3. 当前观测现象 |
| docs/midterm_frontend_inference_performance_2026-06-28.md | Midterm 60 路前端推理入口性能诊断 | 0. 既有 16fps 基线; 1. Analysis-forwarder 隔离结论; 2. 接回 Savant 的 batch 扫描 |
| docs/midterm_knowledge_base/00_Index.md | Midterm 知识库索引 | 快速入口; 当前一句话结论; 如何使用这个知识库 |
| docs/midterm_knowledge_base/01_System_Overview.md | 系统总览 | 系统定位; 主链路; Source of truth |
| docs/midterm_knowledge_base/02_Runtime_Data_Flow.md | 运行时数据流 | 摄像头配置流; 视频与推理流; Redis worker 流 |
| docs/midterm_knowledge_base/03_Module_Map.md | 模块地图 | Compose 与配置; 入口和控制; 视频与推理 |
| docs/midterm_knowledge_base/04_Control_Plane_8090.md | 8090 控制面 | 产品职责; 配置保存与 runtime apply 的区别; Runtime performance |
| docs/midterm_knowledge_base/05_Evidence_Chain.md | 证据链 | 设计目标; 状态流; Admission 与 backpressure |
| docs/midterm_knowledge_base/06_Performance_Optimization_History.md | 性能优化历史 | 当前已证明能力; 已完成优化; 重要误区 |
| docs/midterm_knowledge_base/07_Deployment_Migration.md | 部署与迁移 | 当前部署入口; 干净迁移原则; 离线包 |
| docs/midterm_knowledge_base/08_Open_Risks_And_Next_Actions.md | 剩余风险与下一步 | P0 真实 RTSP / 长时间 soak; P1 生产硬件 profile; P1 face-worker 同步链路 |
| docs/midterm_knowledge_base/09_AI_Agent_Onboarding.md | AI agent 快速上手 | 先读顺序; 不要踩的坑; 常用定位入口 |
| docs/midterm_knowledge_base/10_Glossary.md | 术语表 | 8090; Replay; analysis-forwarder |
| docs/midterm_knowledge_base/11_Data_Contracts_And_Storage.md | 数据契约与存储 | 总体原则; PostgreSQL 表组; 摄像头配置表 |
| docs/midterm_knowledge_base/12_Service_Deep_Dive.md | 服务深潜 | evidence-viewer / 8090; api; replay-service |
| docs/midterm_knowledge_base/13_Runtime_Control_Runbook.md | 运行控制手册 | 控制面分层; 保存配置; 启停摄像头 |
| docs/midterm_knowledge_base/14_Performance_And_Acceptance_Playbook.md | 性能与验收手册 | 当前已证明的能力; 不同测试的含义; 指标解释 |
| docs/midterm_knowledge_base/15_Design_Invariants_And_Decisions.md | 设计不变量与决策 | 不变量 1：8090 是管理入口; 不变量 2：PostgreSQL 是配置和业务事实源; 不变量 3：配置保存不等于 runtime apply |
| docs/midterm_knowledge_base/16_Troubleshooting_Playbook.md | 排障手册 | 总体排障顺序; 8090 保存后刷新丢失; ROI 点选或显示错乱 |
| docs/midterm_knowledge_base/17_Testing_And_Change_Guide.md | 测试与变更指南 | 通用原则; 只改文档; 改 8090 摄像头/ROI/规则 |
| docs/midterm_knowledge_base/18_Artifact_And_Directory_Map.md | 目录与 artifact 地图 | Repo 关键目录; `/data/video-analytics`; 压测 artifact |
| docs/midterm_knowledge_base/README.md | Midterm Project Knowledge Base | Midterm Project Knowledge Base |
| docs/midterm_lab_alarm_and_60_stream_readiness_findings_2026-06-16.md | Midterm 实验室告警与 60 路就绪性发现 | 1. 实验室摄像头告警行为; 2. `min_inside_ms` 语义; 3. 实验室检测率发现 |
| docs/midterm_media_finalizer_pacer_8fps_report_2026-06-29.md | Midterm media-worker finalizer 平滑调度 8 FPS 压测报告 | 结论; 压测配置; 关键结果 |
| docs/midterm_media_finalizer_pool_8fps_report_2026-06-29.md | Midterm media-worker finalizer pool 8 FPS 压测报告 | 结论; 实现范围; 验证配置 |
| docs/midterm_media_worker_snapshot_performance_findings_2026-06-12.md | Midterm Media-Worker Snapshot and Performance Findings | Summary; Current Policy Path; Runtime Evidence |
| docs/midterm_migration_performance_completeness_report_2026-06-26.md | Midterm 迁移、性能、程序完整度分析报告 | 1. 总体结论; 2. 程序完整度分析; 3. 性能现状 |
| docs/midterm_migration_runbook_2026-06-23.md | Midterm Migration Runbook | 1. Migration Principle; 2. What Must Move; 3. Target Host Prerequisites |
| docs/midterm_operator_algorithm_controls_runtime_status.md | Midterm 8090 算法控制与运行时应用现状 | 当前结论; 2026-06-28 事件规则与 8090 前端适配进度; 2026-06-25 规则区域兼容修复 |
| docs/midterm_operator_portal_runtime_design.md | Midterm Operator Portal Runtime Design | Runtime Entry; Portal Scope; Runtime Performance Controls |
| docs/midterm_performance_optimization_backlog_2026-06-27.md | Midterm 性能优化问题清单与分阶段处理计划 - 2026-06-27 | 1. 边界; 2. 当前结论; 3. 性能浪费与优化清单 |
| docs/midterm_post_inference_bottleneck_static_review_2026-06-28.md | Midterm 推理后链路静态瓶颈 Review - 2026-06-28 | 结论; 静态发现; 已缓解项 |
| docs/midterm_post_savant_evidence_proof_windows_2026-06-15.md | Midterm Post-Savant Evidence Proof Window Fix - 2026-06-15 | Summary; Problem Split; Root Cause |
| docs/midterm_pressure60_1080movie_single_savant_3fps_report_2026-06-27.md | Midterm 60 路 1080movie 单 Savant 3 FPS 压测报告 | 结论; 压测配置; 采样结果 |
| docs/midterm_pressure60_1080movie_single_savant_report_2026-06-27.md | Midterm 60 路 1080movie 单 Savant 压测报告 - 2026-06-27 | 1. 结论; 2. 压测配置; 3. 推理表现 |
| docs/midterm_pressure60_2fps_rerun_findings_2026-06-27.md | Midterm 60 路 2 FPS 复跑失败复盘 - 2026-06-27 | 结论; 已修复; 当前未通过项 |
| docs/midterm_progress_snapshot_2026-06-11.md | Midterm Progress Snapshot - 2026-06-11 | 快照范围; 运行状态结论; 数据库运行计数 |
| docs/midterm_qdrant_face_gallery_baseline_2026-06-29.md | midterm Qdrant 人脸图库替换 baseline | 结论; 当前规则目标; 当前 Redis 状态 |
| docs/midterm_qdrant_face_gallery_cutover_2026-06-29.md | midterm Qdrant 人脸图库切换与规模验收 | 结论; 已完成修复; 60 路 Qdrant Authoritative 压测 |
| docs/midterm_qdrant_pressure60_final_8fps_report_2026-06-29.md | midterm Qdrant final 60-route 8 FPS pressure report | Scope; Result; Evidence Acceptance |
| docs/midterm_quick_reference.md | Midterm Deployment Quick Reference | One-Click Scripts; Quick Management Commands; API Endpoints for Camera/People Management |
| docs/midterm_replay_intrusion_clip_duration_diagnosis.md | Midterm Replay Intrusion Clip Duration Diagnosis | Status; Observed Evidence; Why It Happens |
| docs/midterm_replay_routing_id_recovery.md | Midterm Replay Routing ID 故障记录 | 当前状态补充（2026-06-15）; 结论; 2026-06-11 追加故障：sink 网络别名丢失 |
| docs/midterm_replay_shard_change_record_2026-06-18.md | Replay 双分片录制最小闭环修改记录 | 背景; 主要修改; 已运行验证 |
| docs/midterm_runtime_performance_observability.md | Midterm Runtime Performance Observability | 当前结论; 常开指标与按需诊断; Savant 每路摄像头性能指标 |
| docs/midterm_sidecar_session_filter_rootcause_2026-06-21.md | Sidecar stream_session_id 过滤导致标注丢失根因分析 | 现象; 根因; 为什么是系统性的 |
| docs/midterm_uos_clean_machine_migration_steps_2026-06-29.md | Midterm 统信 UOS 新机器迁移步骤 | 0. 迁移边界; 1. UOS 新机器基础环境; 2. 源机器打包 |
| docs/midterm_web_operator_guide.md | Midterm Web 操作台使用指南 | 访问地址; 界面功能; 顶部概览面板 |
| docs/operator_camera_and_face_registration_plan.md | Operator Camera and Face Registration Plan | Implementation Snapshot; Goal; Non-Goals |
| docs/phase_c2_15_replay_sink_observability_and_admission.md | C2.15 Replay Sink Observability and Completion-Aware Admission | Scope; Current Diagnosis; C2.15A - Sink Observability |
| docs/program_healthcheck_r1.md | 程序体检 R1 | 1. 执行摘要; 2. 当前主线拓扑; 3. 服务清单 |
| docs/program_healthcheck_r1_plan.md | Program Healthcheck R1 Plan | Goal Command Objective; Non-Negotiable Guardrails; Starting Assumptions To Verify |
| docs/project_current_progress_summary.md | Project Current Progress Summary | 总体结论; 当前运行入口; 已具备能力 |
| docs/project_knowledge_network.md | Project Knowledge Network | Current Spine; Runtime Topology; Phase Network |
| docs/repair_goal/midterm_8090_runtime_control_center_goal_2026-06-13.md | Midterm 8090 运行控制中心实施计划 | 当前基线; 目标; 必须保持不回退 |
| docs/repair_goal/midterm_alarm_frequency_backpressure_findings_2026-06-13.md | Midterm 报警频率差异与 Source Adapter 背压排查记录 | 当前状态补充（2026-06-15）; 结论; 当前两路配置 |
| docs/repair_goal/midterm_multi_camera_source_convergence_savant_perf_2026-06-12.md | Midterm 多摄像头源收敛与 Savant 性能可观测性修复记录 | 修复范围; 运行时验证; 验证命令 |
| docs/repair_goal/midterm_phase05_forwarder_spike_2026-06-15.md | Midterm Phase 0.5 Forwarder Spike | Scope; S1 Result: Gate Is Before Decode; S2 Result: Savant Image Can Passthrough `VideoFrame` |
| docs/repair_goal/midterm_phase0_evidence_alignment_baseline_2026-06-15.md | Midterm Phase 0 Evidence Alignment Baseline - 2026-06-15 | Scope; Runtime Snapshot; Redis Record Request State |
| docs/repair_goal/midterm_phase0_observability_baseline_2026-06-14.md | Midterm Phase 0 Observability Baseline - 2026-06-14 | Phase 0 Observability; Baseline Snapshot; Gate Status |
| docs/repair_goal/midterm_phase0a_max_fps_control_experiment_2026-06-15.md | Midterm Phase 0A MAX_FPS_CONTROL Experiment - 2026-06-15 | Preconditions; Before Window; After Window |
| docs/repair_goal/midterm_phase0b_replay_short_retry_experiment_2026-06-15.md | Midterm Phase 0B Replay Short-Retry Experiment - 2026-06-15 | Preconditions; Before Window; Experiment |
| docs/repair_goal/midterm_phase0c_sync_output_experiment_2026-06-15.md | Midterm Phase 0C SYNC_OUTPUT Experiment | Scope; Baseline Before Change; Runtime Apply |
| docs/repair_goal/midterm_phase0d_source_adapter_tolerance_verification_2026-06-15.md | Midterm Phase 0D Source Adapter Tolerance Verification | Scope; Evidence; Decision |
| docs/repair_goal/midterm_phase1_analysis_forwarder_2026-06-15.md | Midterm Phase 1 Analysis Forwarder | Scope; Topology; Implementation |
| docs/repair_goal/midterm_phase2_operating_point_appendix_2026-06-15.md | Midterm Phase 2 Operating Point Appendix | Scope; Added Tool |
| docs/repair_goal/midterm_phase2_pressure_runner_2026-06-15.md | Midterm Phase 2 Pressure Runner | Scope; Added Runner |
| docs/repair_goal/midterm_phase2_readiness_gate_2026-06-15.md | Midterm Phase 2 Readiness Gate | Scope; Added Gate; Current Host Result |
| docs/repair_goal/midterm_recent_24h_findings_goal_2026-06-12.md | Midterm Recent 24h Findings Goal | Source Documents; Goal; Current Mainline |
| docs/repair_goal/midterm_recent_24h_goal_completion_2026-06-12.md | Midterm 最近 24 小时性能修复完成记录 | 总体结论; c2/post-savant-poc...origin/c2/post-savant-poc [领先 20]; 本次固化的修复点 |
| docs/repair_goal/midterm_replay_runtime_split_goal_order.md | Midterm Replay Runtime Split Goal Order | Status; Non-Negotiable Invariants; Shared Files And Locking |
| docs/replay_evidence_fix/midterm_replay_evidence_upstream_fix_plan.md | Midterm Replay Evidence Upstream Fix Plan | Status; Pre-Fix Verified Facts; Fix Plan |
| docs/runtime_stability_fix/midterm_multi_source_runtime_stability_plan.md | Midterm Multi-Source Runtime Stability Plan | Status; Pre-Fix Verified Facts; Runtime Apply Ordering |
| docs/runtime_stability_fix/midterm_worker_savant_batching_findings.md | Midterm Worker And Savant Batching Findings | Status; Runtime Chain Boundary; Official Savant Pattern |
| docs/storage_maintenance_delete_plan.md | Storage Maintenance and Delete Plan | 1. 目标; 2. 当前实现调查结论; 3. 总体架构选择 |
| specs/00_project_goal.md | 00_project_goal.md | 1. 项目名称; 2. 项目背景; 3. 业务目标 |
| specs/03_behavior_rules.md | 03_behavior_rules.md | 1. 行为规则设计目标; 2. 核心数据结构; 3. 通用规则机制 |
| specs/06_api_design.md | 06_api_design.md | 1. API 目标; 2. 通用约定; 3. 摄像头 API |
| specs/09_harness.md | 09_harness.md | 1. Harness 目标; 2. 目录结构; 3. 规则测试输入格式 |
| specs/13_pose_behavior_algorithm_fusion.md | 13_pose_behavior_algorithm_fusion.md | 1. 目标; 2. 输入代码来源; 3. 当前主线约束 |
| specs/14_replay_evidence_duration_guard_fix.md | 14_replay_evidence_duration_guard_fix.md | 1. Goal; 2. Current Failure; 3. Non-Goals |
| specs/15_savant_performance_observability.md | 15_savant_performance_observability.md | 1. Goal; 2. Official Savant Performance Statistics Model; 3. Current Midterm Gap |
| specs/16_dual_path_30x2_t4_production_optimization.md | 16_dual_path_30x2_t4_production_optimization.md | 1. Goal; 1.1 Implementation Status - 2026-06-15; 2. Confirmed Failure Chain and Open Boundary (live evidence, 2026-06-14 ~02:55Z) |
| specs/17_clip_worker_evidence_realtime_alignment_fix.md | 17_clip_worker_evidence_realtime_alignment_fix.md | 1. Goal; 1.1 Implementation Status - 2026-06-15; 2. Original Facts and Current Deltas |
| specs/18_database_backed_evidence_viewer.md | 18_database_backed_evidence_viewer.md | 1. Goal; 2. Problem; 3. Target Design |
| specs/19_clip_worker_proof_gate_scheduling_fix.md | 19_clip_worker_proof_gate_scheduling_fix.md | 1. Goal; 2. Background and verified root cause; 3. Non-goals |
| specs/20_dual_4090_as_t4_two_source_validation.md | 20_dual_4090_as_t4_two_source_validation.md | 1. Goal; 2. Relationship To Spec 16; 3. Current Baseline |
| specs/21_replay_evidence_io_optimization_60_stream_production.md | 21_replay_evidence_io_optimization_60_stream_production.md | 1. Goal; 2. Current Finding; 3. Hard Constraints |
| specs/22_midterm_60_stream_readiness_risk_closure_plan.md | 22_midterm_60_stream_readiness_risk_closure_plan.md | 1. Purpose; 2. Current Verdict; 3. Reviewed Risks |
| specs/23_midterm_8090_operator_algorithm_control_plane.md | 23_midterm_8090_operator_algorithm_control_plane.md | 1. Purpose; 2. Current Finding; 3. Target Contract |
| specs/24_midterm_evidence_metadata_database_storage_plan.md | 24_midterm_evidence_metadata_database_storage_plan.md | 1. 目标; 2. 当前问题; 3. 设计原则 |
| specs/25_midterm_events_table_performance_plan.md | 25_midterm_events_table_performance_plan.md | 0. Implementation Result - 2026-06-28; 1. Purpose; 2. Current Finding |
| specs/26_midterm_post_inference_bottleneck_closure_plan.md | 26_midterm_post_inference_bottleneck_closure_plan.md | 1. Purpose; 2. Current Finding And Checkout Status; 3. Coordination Gate |
| specs/27_midterm_face_worker_vector_matching_optimization_plan.md | 27_midterm_face_worker_vector_matching_optimization_plan.md | 1. Purpose; 2. Current Finding; 3. Decision: Do Not Rewrite To C++ First |
| specs/28_midterm_qdrant_face_gallery_migration_plan.md | 28_midterm_qdrant_face_gallery_migration_plan.md | 1. Purpose; 2. Decision; 3. Why Qdrant Fits This Codebase |
| specs/29_midterm_evidence_replay_latency_optimization_plan.md | 29_midterm_evidence_replay_latency_optimization_plan.md | 1. Goal; 2. Current Finding; 3. Non-Goals |
| specs/30_midterm_rolling_cache_evidence_plan.md | 30_midterm_rolling_cache_evidence_plan.md | 1. Goal; 2. Current Problem; 3. Non-Goals |
| specs/archive/phase-only/20260610/01_architecture.md | 01_architecture.md | 1. 总体架构; 2. 组件职责; 3. 逻辑数据流 |
| specs/archive/phase-only/20260610/02_savant_pipeline.md | 02_savant_pipeline.md | 1. Savant Module 目标; 2. Pipeline 元素说明; 3. module.yml 骨架 |
| specs/archive/phase-only/20260610/04_face_intelligence.md | 04_face_intelligence.md | 1. 目标; 2. 设计原则; 3. Face Intelligence Pipeline |
| specs/archive/phase-only/20260610/05_database_schema.md | 05_database_schema.md | 1. 数据库选择; 2. 扩展; 3. cameras |
| specs/archive/phase-only/20260610/07_deployment.md | 07_deployment.md | 1. 部署目标; 2. 服务列表; 3. GPU 分配 |
| specs/archive/phase-only/20260610/08_performance_policy.md | 08_performance_policy.md | 1. 性能目标; 2. 默认 FPS 策略; 3. Batch 策略 |
| specs/archive/phase-only/20260610/10_dev_machine_vs_deployment_machine.md | 1. 两台机器的定位不同 | 1. 两台机器的定位不同; 2.1 CPU 区别; 2.2 GPU 区别 |
| specs/archive/phase-only/20260610/11_docker_communication_and_clip_design.md | Docker Compose 容器通信、事件流与 Savant Replay 报警录像设计 v0.3 | 2.1 Image，镜像; 2.2 Container，容器; 2.3 一个 image 可以启动多个 container |
| specs/archive/phase-only/20260610/12_algorithm_activation.md | 12_algorithm_activation.md | 1. 目标; 2. 算法清单; 3. 三层模型 |

## Phase 2 进度状态
Phase 0/Phase 1 之后按下列模块推进深度审查；模块清单已确认，按顺序逐个产出报告。
| 模块 | 状态 |
| --- | --- |
| services/event-worker | 已完成 Phase 2: docs/code_review/module_services_event_worker.md |
| services/clip-worker | 已完成 Phase 2: docs/code_review/module_services_clip_worker.md |
| services/media-worker | 未开始 Phase 2 |
| services/api | 未开始 Phase 2 |
| services/analysis-forwarder | 未开始 Phase 2 |
| services/face-worker | 未开始 Phase 2 |
| modules/savant_security | 未开始 Phase 2 |
| modules/savant_replay | 未开始 Phase 2 |
| services/evidence-viewer | 未开始 Phase 2 |
| infra | 未开始 Phase 2 |
| db/migrations | 未开始 Phase 2 |
| scripts/runtime and scripts/ops | 未开始 Phase 2 |
| libs/evidence_metadata and libs/face_registration | 未开始 Phase 2 |
| harness/tests | 未开始 Phase 2 |
