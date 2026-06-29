---
type: artifact-directory-map
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - artifacts
  - directories
  - deployment
---

# 目录与 artifact 地图

本页说明 repo、运行数据目录、压测 artifact 和迁移包各自是什么，避免把“需要部署的东西”和“运行产生的东西”混在一起。

## Repo 关键目录

| 路径 | 内容 |
| --- | --- |
| `infra/docker-compose.midterm.yml` | midterm compose 主入口 |
| `infra/env/midterm.env` | 默认运行 env |
| `infra/generated/` | source manifest 等运行生成快照 |
| `modules/savant_security/module.yml` | Savant/DeepStream 模型链 |
| `modules/savant_security/config/cameras.midterm.yml` | DB 导出的 Savant camera config 快照 |
| `services/api/` | FastAPI API 和 runtime control |
| `services/evidence-viewer/` | 8090 UI/代理 |
| `services/analysis-forwarder/` | Replay -> Savant 分析分支 |
| `services/event-worker/` | event 入库、admission、record request |
| `services/face-worker/` | face observation、gallery/watchlist |
| `services/clip-worker/` | Replay job 调度 |
| `services/media-worker/` | evidence finalization |
| `db/migrations/` | PostgreSQL schema |
| `harness/tests/` | targeted tests |
| `scripts/runtime/` | pressure/runtime 脚本 |
| `scripts/midterm_package_clean.sh` | 干净迁移打包 |
| `scripts/midterm_deploy_clean.sh` | 干净迁移部署 |
| `docs/midterm_knowledge_base/` | 本知识库 |

## `/data/video-analytics`

| 子目录 | 作用 | 说明 |
| --- | --- | --- |
| `models` | 模型文件 | 新机器需要 |
| `media` | evidence/video-file-sink/runtime media | 运行产生 |
| `artifacts` | 压测和诊断输出 | 可留作报告证据 |
| `downloads` | 临时下载/导入 | 当前不作为迁移必需 |
| Replay data | RocksDB 视频存储 | 运行产生 |
| PostgreSQL data | 可选 local profile 数据 | 干净迁移默认不带 |

用户曾问 `/data/video-analytics/downloads` 为什么要拷贝。当前结论：

- 干净迁移不需要它；
- 它不是模型目录；
- 它不是人脸库事实源；
- 如果为空，不应作为迁移必需项。

## 压测 artifact

路径形态：

```text
/data/video-analytics/artifacts/<run_id>
```

常见文件：

- `report.json`
- `sample_summary.json`
- `downstream_observability_summary.json`
- `db_summary_before_cleanup.json`
- `drain_snapshots.json`
- `kept_50_evidence.csv`
- `media_worker_logs_since_start.txt`
- `clip_worker_logs_since_start.txt`
- `runtime_overview*.json`
- `docker_stats*.json`
- `forwarder_metrics*.txt`
- `savant_metrics*.txt`

artifact 用途：

- 证明本次压测配置；
- 保留失败证据；
- 对比 worker/DB/Redis 指标；
- 让报告可审计。

不要做：

- 不要把 artifact 当成生产配置；
- 不要把旧 artifact 直接迁移到新机器作为运行依赖；
- 不要因为 artifact 多就混进 repo。

## Evidence 文件

当前证据核心文件：

- `raw_clip.mov`：必须可播放；
- snapshot/preview：辅助展示；
- manifest/metadata：可能存在文件或 DB-backed 记录；
- overlay/timeline：以 DB-backed evidence tables 为主。

事实判断：

- 8090 list/detail 以 DB-backed evidence API 为准；
- 文件存在但 DB 没有 bundle，用户可能查不到；
- DB 有 bundle 但 raw clip 丢失，playable 会失败；
- annotation missing 可以 degraded。

## 迁移包

干净迁移包包括：

- `repo.tgz`；
- `models.tgz`；
- 可选 `images.tar`；
- `image_list.txt`；
- 部署脚本和说明。

默认不包括：

- PostgreSQL 旧数据；
- Redis；
- Replay RocksDB；
- old evidence media；
- old artifacts；
- downloads；
- 已注册人员/人脸库。

如果生产要迁业务数据，需要单独设计：

- DB dump/restore；
- media evidence 路径重写；
- Replay 数据兼容；
- 人脸 embedding 校验；
- 8090 查询一致性验收。

## Generated 快照

常见 generated 文件：

- `modules/savant_security/config/cameras.midterm.yml`
- `infra/generated/sources.generated.yml`
- `/data/video-analytics/media/.runtime/replay_shards.topology.json`

语义：

- 它们是运行快照；
- 可以帮助排障；
- 不是人工配置源；
- 可以和 DB/runtime 对比判断漂移。

## 日志

优先日志：

- event-worker；
- face-worker；
- clip-worker；
- media-worker；
- analysis-forwarder；
- savant-security；
- replay-service；
- API；
- evidence-viewer。

压测时必须保存至少：

- worker logs since start；
- media/clip logs since start；
- source adapter exits；
- runtime overview；
- Redis/PG summaries。

## 清理原则

可以清理：

- 旧 pressure artifact；
- 临时 generated pressure sources；
- 过期 evidence preview；
- 未引用的 media outputs；
- 空 downloads。

不要随便清理：

- 当前运行中的 video-file-sink 输出；
- active evidence task 对应 raw clip；
- PostgreSQL data；
- models；
- 当前 replay shard runtime 文件；
- 用户明确保留的 50 个 evidence 样本。

## 新 agent 接手时先看

```bash
git status --short
find docs/midterm_knowledge_base -maxdepth 1 -type f -printf '%f\n' | sort
docker ps -a --format '{{.Names}}\t{{.Status}}'
```

然后读：

- [[09_AI_Agent_Onboarding|AI agent 快速上手]]
- [[11_Data_Contracts_And_Storage|数据契约与存储]]
- [[14_Performance_And_Acceptance_Playbook|性能与验收手册]]
- [[16_Troubleshooting_Playbook|排障手册]]
