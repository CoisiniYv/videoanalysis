---
type: testing-guide
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - testing
  - validation
  - change-management
---

# 测试与变更指南

本页把常见改动和验证项对应起来，避免每次都用过宽或过窄的测试。

## 通用原则

- 保留用户未提交改动；
- 修改前看 `git status --short`；
- 只提交本次相关文件；
- Dockerfile/requirements/base image 未变时，不无故 rebuild；
- 代码改动后至少跑 targeted tests；
- 文档/markdown 改动后跑 `git diff --check`；
- runtime 语义变化要有 smoke 或 artifact proof。

## 只改文档

检查：

```bash
git diff --check
```

如果是 Obsidian 知识库，还要检查 wiki link：

```bash
for target in $(perl -nE 'while(/\[\[([^]|#]+)(?:#[^]|]+)?(?:\|[^\]]+)?\]\]/g){say $1}' docs/midterm_knowledge_base/*.md | sort -u); do
  test -f "docs/midterm_knowledge_base/${target}.md" || echo "missing wiki target: $target"
done
```

## 改 8090 摄像头/ROI/规则

建议测试：

```bash
pytest -q harness/tests/test_api_cameras.py
pytest -q harness/tests/test_operator_roi_editor_static.py
pytest -q harness/tests/test_roi_polygon_validation.py
pytest -q harness/tests/test_roi_preview.py
pytest -q harness/tests/test_camera_api_runtime_sources_direct.py
pytest -q harness/tests/test_camera_runtime_apply_service.py
```

必须验证：

- 保存后刷新不丢；
- DB 中 zone/rule 正确；
- config sync 更新 generated config；
- 保存 ROI/规则不触发 full runtime apply；
- rule target/watchlist target 不被混成全局默认。

## 改 runtime apply/performance/topology

建议测试：

```bash
pytest -q harness/tests/test_runtime_control_service.py
pytest -q harness/tests/test_runtime_performance_service.py
pytest -q harness/tests/test_runtime_topology_service.py
pytest -q harness/tests/test_operator_full_runtime_orchestration.py
pytest -q harness/tests/test_runtime_stop_and_latency.py
pytest -q harness/tests/test_runtime_overview_api.py
pytest -q harness/tests/test_replay_shard_routing.py
pytest -q harness/tests/test_savant_supervisor_service.py
```

compose 静态检查：

```bash
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config >/tmp/midterm-compose-config.check.yml
```

必须验证：

- evidence guard；
- runtime epoch；
- containers touched；
- replay shard JSON/path；
- 8090 状态提示；
- disabled source 不误报；
- `apply-async` 的并发 guard、阶段/百分比和终态；
- apply-status 刷新后可恢复，但 API 进程重启会把仍在运行的线程任务标为失败；
- `/runtime/latency` 的 10s/60s 阈值、A/B source/queue 和 DB/annotation 缺失语义。

## 改 event-worker

建议测试：

```bash
pytest -q harness/tests/test_event_repository.py
pytest -q harness/tests/test_event_worker_recording_policy.py
pytest -q harness/tests/test_record_request_idempotency.py
pytest -q harness/tests/test_alert_policy_scoped_cooldown.py
pytest -q harness/tests/test_pose_behavior_event_payload_contract.py
```

必须验证：

- 不重新引入 `XRANGE security.record_requests - +` 全流扫描；
- duplicate/retry/reclaim 幂等；
- watchlist hit cooldown 不受 intrusion 影响；
- admission skip 状态可解释；
- evidence task 不留假 active。

## 改 person-observation-worker / 人体轨迹

建议测试：

```bash
pytest -q harness/tests/test_event_worker_person_batch.py
pytest -q harness/tests/test_event_worker_priority_and_image_atomicity.py
pytest -q harness/tests/test_midterm_pressure60_script.py
```

必须验证：

- `person-observation-worker` 使用独立入口和 consumer group；
- event-worker 主循环不重新轮询高率 person stream；
- batch insert、duplicate、ACK、retry 和 pending reclaim 语义；
- person stream lag/pending、batch/duplicate/error 指标可观测；
- 压力脚本和 Compose 同时包含该 worker，且不会把它误计为 event-worker 的附属线程。

## 改 face-worker

建议测试：

```bash
pytest -q harness/tests/test_face_worker.py
pytest -q harness/tests/test_face_vector_store.py
pytest -q harness/tests/test_gallery_match.py
pytest -q harness/tests/test_face_match_evidence_policy.py
pytest -q harness/tests/test_watchlist_evidence_identity_continuation.py
# 使用 Qdrant 可选 profile 时补充：
pytest -q harness/tests/test_qdrant_gallery_store.py
pytest -q harness/tests/test_qdrant_gallery_sync_outbox.py
pytest -q harness/tests/test_face_worker_qdrant_backend_static.py
```

必须验证：

- embedding 维度和 norm；
- threshold correctness；
- target-person filtering；
- match_results 语义；
- watchlist_hit event payload；
- source_observation_id 绑定；
- 默认 pgvector 路径保持可用；Qdrant 不是当前默认 authoritative backend；
- 使用 Qdrant 时检查 score/threshold mapping、fallback count、outbox lag；
- 20k/50k/100k 规模 benchmark 结果，如果生产图库规模变化；
- observation insert、rule resolution、exact rerank、event publish、ACK p95/p99；
- 小目标名单 hybrid routing 不误扩成 all-active。

## 改 clip-worker / Clip Coordinator V2（单分支或兼容链）

建议测试：

```bash
pytest -q harness/tests/test_clip_worker_queue_safety.py
pytest -q harness/tests/test_clip_coordinator_v2_contract.py
pytest -q harness/tests/test_clip_request_processor_v2.py
pytest -q harness/tests/test_clip_worker_replay_planner.py
pytest -q harness/tests/test_clip_replay_executor.py
pytest -q harness/tests/test_clip_replay_slot_fencing.py
pytest -q harness/tests/test_midterm_replay_cadence_payload.py
pytest -q harness/tests/test_midterm_replay_duration_tuning.py
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
pytest -q harness/tests/test_midterm_replay_evidence_duration_guard.py
pytest -q harness/tests/test_replay_shard_routing.py
```

必须验证：

- stale/missing DB event/task 会 `XACK`；
- Replay job payload 仍 constant-cadence；
- 前后证据窗口正确；
- per-source/per-shard 并发；
- replay shard 路由；
- Redis pending 最终收敛；
- plan hash、owner/token/generation 和 create slot fencing；
- 完整双分支 rolling 预设仍 suppress record request，不能把本节误当成其主证据链。

## 改 rolling-cache-sink / rolling 物化

建议测试：

```bash
pytest -q services/rolling-cache-sink/tests/test_sink_contract.py
pytest -q harness/tests/test_rolling_cache_materialization.py
pytest -q harness/tests/test_midterm_stream_session_isolation.py
pytest -q harness/tests/test_analysis_forwarder.py
pytest -q harness/tests/test_savant_pts_fps_gate.py
```

必须验证：

- raw fanout 的 full-rate 分支与 analysis sampling 分支语义分离；
- fragment 以 epoch/source/session 隔离并原子发布；
- 双时间域 metadata、segment catalog/index 和跨片段选择正确；
- retention/byte cap 不删除仍被 pin 的片段；
- 5+5 窗口、约 24 FPS、关键帧边界和 remux 输出可播放；
- rolling 不可用时只能按当前配置进入明确 fallback/failed 状态，不能静默伪成功。

## 改 media-worker

建议测试：

```bash
pytest -q harness/tests/test_evidence_materialization_phase0.py
pytest -q harness/tests/test_evidence_materialization_phase2plus.py
pytest -q harness/tests/test_materialization_repository_contract.py
pytest -q harness/tests/test_evidence_lifecycle_contract.py
pytest -q harness/tests/test_media_worker_scheduler_v2.py
pytest -q harness/tests/test_media_worker_segment_index.py
pytest -q harness/tests/test_media_worker_finalizer_boundary.py
pytest -q harness/tests/test_media_worker_finalizer_integration.py
pytest -q harness/tests/test_media_worker_long_lived_resources.py
pytest -q harness/tests/test_media_worker_perf_safety.py
pytest -q harness/tests/test_post_savant_evidence_bundle_crop.py
pytest -q harness/tests/test_evidence_viewer_database_index.py
pytest -q harness/tests/test_evidence_viewer_materialization_state.py
```

必须验证：

- raw clip playable；
- bundle/artifacts/timeline/overlay DB 写入；
- degraded annotation 语义；
- lifecycle/queue wait/deadline slack；
- Scheduler V2 lane/source/permit reservation 与 bounded dispatch；
- image/remux/finalizer lane 隔离，进程 finalizer 不被请求线程生命周期截断；
- segment index 命中、失效与 filesystem fallback 可解释；
- lease/fencing/handoff/terminal CAS 和 stale-owner recovery；
- ffmpeg/ffprobe 不 fallback；
- terminal idempotency；
- cleanup 不删错文件。

## 改 analysis-forwarder

建议测试：

```bash
pytest -q harness/tests/test_analysis_forwarder.py
pytest -q harness/tests/test_midterm_pressure60_script.py
pytest -q harness/tests/test_savant_perf_metrics_contract.py
```

必须验证：

- PTS/FPS 采样；
- queue drop；
- send failure metrics；
- null sink profile；
- Savant connected profile；
- 不把采样缺口误解释成 Replay 丢帧。

## 改 Savant module/pyfunc

建议测试：

```bash
pytest -q harness/tests/test_behavior_rules_config_loader_integration.py
pytest -q harness/tests/test_face_person_association.py
pytest -q harness/tests/test_face_reid_gate.py
pytest -q harness/tests/test_frame_annotation_exporter_runtime.py
pytest -q harness/tests/test_savant_redis_stream_writer.py
```

必须验证：

- module config 渲染；
- Redis payload schema；
- face/person association；
- frame annotation；
- batch/interval；
- effective FPS；
- 不让 exporter 阻塞 `process_frame`。

## 改 DB migrations/index

建议测试：

```bash
pytest -q harness/tests/test_events_table_performance_indexes_static.py
pytest -q harness/tests/test_midterm_worker_indexes_static.py
pytest -q harness/tests/test_worker_hotpath_indexes_static.py
pytest -q harness/tests/test_algorithm_rule_response_schema.py
```

必须验证：

- migration 顺序；
- index concurrently；
- partial index predicate；
- migration 032 的 cleanup-recovery 与 algorithm cooldown 热路径索引；
- 032 以 autocommit、非压力窗口应用，不能包在 transaction 中；
- 旧数据兼容；
- API response schema；
- EXPLAIN 计划。

## 改迁移/打包

建议测试：

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
```

必须验证：

- `repo.tgz`；
- `models.tgz`；
- 可选 `images.tar`；
- `--no-build`；
- 不打包旧 Redis/PostgreSQL/Replay/evidence；
- UOS 目标机前置条件写清楚。

## 提交前检查

最小：

```bash
git status --short
git diff --check
```

Python 改动：

```bash
python -m compileall <changed-python-files-or-dirs>
```

最后：

- 只 `git add` 本次相关文件；
- commit message 描述真实改动；
- 提交后再 `git status --short`；
- 如果仍有用户改动，明确说明未触碰。
