---
type: testing-guide
project: video-analytics-midterm
updated: 2026-06-29
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
- disabled source 不误报。

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

## 改 face-worker

建议测试：

```bash
pytest -q harness/tests/test_face_worker.py
pytest -q harness/tests/test_face_vector_store.py
pytest -q harness/tests/test_gallery_match.py
pytest -q harness/tests/test_face_match_evidence_policy.py
pytest -q harness/tests/test_watchlist_evidence_identity_continuation.py
# Qdrant 接入后补充：
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
- Qdrant score/threshold mapping、fallback count、outbox lag；
- 20k/50k/100k 规模 benchmark 结果，如果生产图库规模变化；
- observation insert、rule resolution、exact rerank、event publish、ACK p95/p99；
- 小目标名单 hybrid routing 不误扩成 all-active。

## 改 clip-worker

建议测试：

```bash
pytest -q harness/tests/test_clip_worker_queue_safety.py
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
- Redis pending 最终收敛。

## 改 media-worker

建议测试：

```bash
pytest -q harness/tests/test_evidence_materialization_phase0.py
pytest -q harness/tests/test_evidence_materialization_phase2plus.py
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
pytest -q harness/tests/test_algorithm_rule_response_schema.py
```

必须验证：

- migration 顺序；
- index concurrently；
- partial index predicate；
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
