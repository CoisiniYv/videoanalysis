---
type: ai-agent-onboarding
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - ai-agent
  - onboarding
---

# AI agent 快速上手

## 先读顺序

1. `docs/current_architecture.md`；
2. `docs/current_mainline_status.md`；
3. `docs/documentation_sync_audit_2026-07-20.md`；
4. [[00_Index|知识库索引]]；
5. [[11_Data_Contracts_And_Storage|数据契约与存储]]；
6. [[12_Service_Deep_Dive|服务深潜]]；
7. [[15_Design_Invariants_And_Decisions|设计不变量与决策]]；
8. 任务对应 spec、tests 和最新带日期 artifact 报告。

## 开始前

```bash
git rev-parse --short HEAD
git status --short --branch
```

当前 repo 经常有大规模 dirty worktree。不要回滚用户变更；必须区分 HEAD、工作区和
已部署机器。

## 最容易误判的事实

- 完整双分支主 evidence 路径是 rolling-cache，不是逐事件 Replay job；
- 单分支仍保留 clip-worker/video-file-sink 兼容路径；
- `security.record_requests=0` 在 full preset 可以正常；
- 4090 preset 不用 MPS，但仍用 ROI AdaFace/rolling；
- 当前默认向量后端是 pgvector，不是 Qdrant；
- person observations 有独立 worker；
- original PTS 和 rolling mux PTS 不能混用；
- T4 40 路当前已验证，最新代码 4090 60 路尚需复跑；
- 8090 apply-status 恢复页面显示，但不恢复 API 重启后的执行线程；
- `midterm_health.sh` 的固定服务表不是当前完整 topology 权威。

## 代码入口

### 控制面

- `services/api/app/routers/runtime.py`
- `services/api/app/services/runtime_topology.py`
- `services/api/app/services/runtime_topology_jobs.py`
- `services/api/app/services/runtime_latency.py`
- `services/evidence-viewer/app/`

### 数据面

- `modules/savant_security/module.yml`
- `services/analysis-forwarder/`
- `services/rolling-cache-sink/`
- `services/adaface-roi-worker/`
- `services/event-worker/app/person_worker.py`
- `services/face-worker/`
- `services/media-worker/`
- `services/clip-worker/`
- `libs/evidence_lifecycle/contract.py`
- `db/migrations/029*` 到 `032*`

## 排障顺序

```text
user-visible symptom
  -> API/DB truth
  -> runtime epoch/source/session
  -> Redis delivery/lag/pending
  -> task status/phase/owner/lease/reason
  -> rolling/Replay media source
  -> final artifact + DB index
  -> 8090 query/render
```

不要把“无 event”“有 event 无 task”“task 无 coverage”“已物化但 UI 查不到”混成一个
evidence 问题。

## 验证重点

- targeted pytest 与 migration tests；
- Compose effective config（含 env/storage/operator override）；
- `git diff --check`；
- current source count/FPS/queue；
- event/person/face Redis groups；
- evidence active/terminal/residual ownership；
- raw 24 FPS 与 analysis 4/8 FPS 分别验证；
- DB-backed detail/timeline/annotation；
- artifact 记录 revision、dirty diff、input identity 和 cleanup audit。
