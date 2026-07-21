---
type: module-map
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - modules
  - services
---

# 模块地图

## 部署与配置

| 模块 | 关键文件 | 职责 |
| --- | --- | --- |
| Compose | `infra/docker-compose.midterm.yml` | 基础服务、profiles、端口和 env wiring |
| Env | `infra/env/midterm.env` | 单分支默认值，不等同于 operator preset 生效值 |
| Storage | `infra/midterm-storage.override.yml` | rolling/trajectory 快速盘挂载 |
| Operator override | `infra/operator-dual-runtime.override.yml` | 单 GPU 双分支预创建时限定 Savant B GPU0 |
| Start | `scripts/midterm_start.sh` | 构建、基础启动、目录准备、双分支预创建 |
| Precreate | `scripts/runtime/precreate_operator_dual_runtime.sh` | `--no-start` 创建 8090 管理容器 |

## 入口与控制面

| 服务/模块 | 关键文件 | 职责 |
| --- | --- | --- |
| `evidence-viewer` | `services/evidence-viewer/app/main.py` | 8090 UI、API/media proxy、旧 evidence 兼容接口 |
| FastAPI | `services/api/app/main.py` | 内部 API composition root |
| runtime router | `services/api/app/routers/runtime.py` | overview、latency、control、performance、topology |
| topology | `services/api/app/services/runtime_topology.py` | 预设、分片、容器编排、prefill/evidence gate |
| topology jobs | `services/api/app/services/runtime_topology_jobs.py` | 进程内后台 apply 与原子状态文件 |
| runtime latency | `services/api/app/services/runtime_latency.py` | annotation/DB/branch 延迟摘要 |

## 视频与推理

| 角色 | 实现 | 当前职责 |
| --- | --- | --- |
| Replay | Savant Replay image/config | 全率 ingest/RocksDB、raw fanout 上游、兼容 Replay job |
| raw fanout | `services/analysis-forwarder/` | 原始 PUB；完整模式还承担 sampled output |
| analysis-forwarder | 同上 | 单分支/推理-only 分析采样 |
| Savant | `modules/savant_security/module.yml` | Pose、tracker、rules、Face、ROI/annotation export |
| dynamic sources | `scripts/runtime/camera_source_controller.py` | DB/export 驱动 RTSP adapter |

## Redis workers

| 服务 | 输入 | 输出 |
| --- | --- | --- |
| `event-worker` | `security.events` | events、alerts、evidence tasks、单分支 record requests |
| `person-observation-worker` | `security.person_observations` | 批量 person bbox trajectories |
| `adaface-roi-worker` | `security.face_rois` | `security.face_observations` |
| `face-worker` | `security.face_observations` | face observations、matches、watchlist events |
| `clip-worker` | `security.record_requests` | 兼容 Replay job、fenced slot/state |

`person-observation-worker` 复用 event-worker 镜像，但有独立进程入口、consumer group 和
batch size；它不是 event-worker 主循环中的附属轮询。

## Evidence

| 模块 | 关键文件 | 职责 |
| --- | --- | --- |
| rolling sink | `services/rolling-cache-sink/` | GStreamer passthrough、fragment 原子发布、ready/metrics |
| scheduler | `services/media-worker/app/materialization_scheduler.py` | bounded dispatch、lane/source/permit reservation |
| lifecycle repository | `services/media-worker/app/materialization_repository.py` | claim/lease/retry/handoff/terminal CAS |
| segment index | `services/media-worker/app/segment_index.py` | epoch/source catalog、row cache、read pins |
| rolling materializer | `services/media-worker/app/rolling_cache.py` | time-domain mapping、coverage、remux/image |
| finalizer root | `services/media-worker/app/worker.py` | resource lifetime、finalizer、publish/index/cleanup |
| DB index | `services/media-worker/app/evidence_db_index.py` | bundle/artifact/timeline/overlay transaction |
| lifecycle contract | `libs/evidence_lifecycle/contract.py` | canonical status/phase/reason vocabulary |

## 数据与查询

| 模块 | 职责 |
| --- | --- |
| `db/migrations/` | schema、lifecycle、fencing、hot-path indexes |
| API event/evidence repositories | 分页、alias、bundle/detail 查询 |
| `services/face-worker/app/vector_store.py` | 默认 pgvector 查询 |
| Qdrant modules/sync | 可选图库 derived index |

## 验证与报告

- `scripts/runtime/run_midterm_pressure60.py`：统一 pressure runner；
- `scripts/runtime/run_pressure60_dual1gpu_profile.sh`：T4/4090 profile wrapper；
- `harness/tests/test_*`：静态、契约、故障注入和回归测试；
- `docs/code_review/`：带日期/revision 的 checkpoint 和运行证据。
