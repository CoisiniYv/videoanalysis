---
type: ai-agent-onboarding
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - ai-agent
  - onboarding
---

# AI agent 快速上手

## 先读顺序

1. [[00_Index|知识库索引]]
2. [[01_System_Overview|系统总览]]
3. [[02_Runtime_Data_Flow|运行时数据流]]
4. [[03_Module_Map|模块地图]]
5. [[05_Evidence_Chain|证据链]]
6. [[06_Performance_Optimization_History|性能优化历史]]
7. [[08_Open_Risks_And_Next_Actions|剩余风险与下一步]]
8. [[11_Data_Contracts_And_Storage|数据契约与存储]]
9. [[12_Service_Deep_Dive|服务深潜]]
10. [[14_Performance_And_Acceptance_Playbook|性能与验收手册]]

然后再读正式报告：

- `docs/midterm_current_program_technical_analysis_2026-06-28.md`
- `specs/26_midterm_post_inference_bottleneck_closure_plan.md`
- `docs/midterm_media_finalizer_pacer_8fps_report_2026-06-29.md`

如果是排障或压测失败，优先加读：

- [[16_Troubleshooting_Playbook|排障手册]]
- [[18_Artifact_And_Directory_Map|目录与 artifact 地图]]

如果是准备改代码，优先加读：

- [[15_Design_Invariants_And_Decisions|设计不变量与决策]]
- [[17_Testing_And_Change_Guide|测试与变更指南]]

## 不要踩的坑

- 不要只看 `cameras.midterm.yml` 判断配置真相。DB 是 source of truth。
- 不要把 8090 页面状态等同于容器运行状态；要看 runtime overview、Docker、Redis、PostgreSQL。
- 不要把 `16/1` 高入口压力误解成 16 FPS 推理通过。
- 不要把 pressure source 通过误解成真实 RTSP 长时间生产通过。
- 不要在算法/ROI 保存时触发 full runtime apply。
- 不要无故 rebuild 镜像。Dockerfile / requirements / base image 变化才需要 rebuild。
- 不要回滚用户未提交改动。

## 常用定位入口

### 8090 / API

- `services/api/app/main.py`
- `services/api/app/routers/cameras.py`
- `services/api/app/routers/runtime.py`
- `services/api/app/services/runtime_apply.py`
- `services/api/app/services/runtime_topology.py`
- `services/evidence-viewer/app/main.py`

### 推理

- `modules/savant_security/module.yml`
- `services/analysis-forwarder/app/main.py`

### Redis workers

- `services/event-worker/app/worker.py`
- `services/event-worker/app/record_request.py`
- `services/face-worker/app/worker.py`
- `services/face-worker/app/vector_store.py`
- `services/clip-worker/app/worker.py`
- `services/clip-worker/app/replay_shards.py`
- `services/media-worker/app/worker.py`

### 压测

- `scripts/runtime/run_midterm_pressure60.py`

## 修改后常见验证

按改动范围选择：

```bash
pytest -q harness/tests/test_midterm_pressure60_script.py
pytest -q harness/tests/test_evidence_materialization_phase0.py
pytest -q harness/tests/test_post_savant_evidence_bundle_crop.py
pytest -q harness/tests/test_midterm_deployment_contract.py
python -m compileall <changed-python-files>
git diff --check
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config >/tmp/midterm-compose-config.check.yml
```

运行态检查：

```bash
docker ps -a --format '{{.Names}}\t{{.Status}}'
docker exec video-analytics-midterm-redis redis-cli XPENDING security.record_requests clip-workers-midterm
```

## 压测 artifact 重点看什么

路径示例：

```text
/data/video-analytics/artifacts/<run_id>
```

重点文件：

- `report.json`
- `sample_summary.json`
- `downstream_observability_summary.json`
- `db_summary_before_cleanup.json`
- `drain_snapshots.json`
- `kept_50_evidence.csv`
- `media_worker_logs_since_start.txt`
- `clip_worker_logs_since_start.txt`

判断 60 路 evidence profile 是否健康：

- source exited/restart/negative PTS 为 0；
- forwarder queue 不长期满；
- send failures 为 0；
- semantic outputs 持续增长；
- Redis pending/lag 不持续增长；
- retained evidence 达标；
- 8090 proof OK；
- media lifecycle p95/p99 在 deadline 内。

## 当前最佳下一步

不要继续扩大配置面。优先：

1. 真实 RTSP 8 FPS 长时间 soak；
2. 生产硬件 profile；
3. face-worker 大图库 EXPLAIN / p95；
4. Savant 阶段级 latency；
5. 根据真实压力结果决定是否继续改 media finalizer。
