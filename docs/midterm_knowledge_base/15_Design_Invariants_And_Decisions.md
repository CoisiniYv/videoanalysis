---
type: design-decisions
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - architecture
  - decisions
  - invariants
---

# 设计不变量与决策

## 1. 8090 是用户入口

- 浏览器不直接访问 API 8000；
- 新产品功能必须有 8090 路径或明确说明仅内部；
- 内部 ID 不应替代摄像头/人员可读名称；
- 高风险操作需要确认、状态、失败详情和审计。

## 2. PostgreSQL 是事实源

摄像头、规则、人员、图库、事件、任务和 evidence 索引以 PostgreSQL 为准。Redis、
YAML、topology JSON、页面缓存和 artifact 都不是最终事实源。

## 3. 保存配置不等于 runtime apply

ROI/规则保存只做 DB + config sync。source、RTSP、performance、topology 和显式重启才
走 evidence guard/runtime apply。

## 4. Full preset 是 single-ingestion + full-rate side tap

同一路摄像头只拉一次。Replay 后的 raw fanout 在 sampler 之前把全率编码帧交给
rolling sink，同时把采样帧交给 Savant。不得从 Savant/采样输出重建“原始”证据。

## 5. Rolling-cache 是完整预设主 evidence 路径

完整预设：

- suppress per-event record requests；
- rolling materialization enabled；
- Replay fallback disabled；
- source-scoped annotation required；
- prefill 完成后才创建任务。

Clip/Replay/video-file-sink 仍是单分支与兼容路径，但不能写成 full preset 的必经主链。

## 6. 时间域分离

- UUID：视觉身份；
- 原始 PTS：事件/轨迹/annotation；
- mux PTS：rolling 视频封装/裁剪；
- wall clock/DB created_at：业务与诊断。

不得把 wall clock 或 Redis id 当媒体锚点，也不得用 mux PTS 替换原始标注时间。

## 7. Evidence 状态由 PostgreSQL 和唯一 owner 控制

- materialization v2 是 canonical；
- deferred 是终态；
- retry 用 pending + next-attempt；
- claim/Replay create 都必须有 fence；
- rolling recovery 只归 media-worker；
- Replay recovery 只归 clip-worker；
- shadow path 不能产生第二份副作用；
- durable terminal commit 之后才清理。

## 8. DB-backed metadata，文件保存媒体

最终视频/图片保存在文件系统，bundle/timeline/overlay/state 在数据库。8090 以 DB API
为主，旧文件扫描只兼容。文件存在不等于用户可查，DB row 存在也不等于媒体可播放。

## 9. Cooldown/coverage/失败语义透明

- event 被 cooldown 抑制不是 materialization failure；
- covered child 必须指向 parent；
- skipped/failed/expired 不是成功；
- watchlist image 与 intrusion video 可以在不同页面展示；
- 验收必须报告 detection、unsuppressed task、physical bundle 和 playable coverage。

## 10. 高率人体轨迹与事件策略隔离

`person-observation-worker` 独立 consumer/process 是架构边界，防止 event/evidence 策略
工作导致轨迹 stream trimming loss。不要无验证地重新合并循环。

## 11. 向量后端显式化

PostgreSQL 永远是图库事实源。当前默认查询后端 pgvector；Qdrant 是可选 derived
index。文档和运行报告必须记录 effective backend、fallback 和 sync 状态。

## 12. 性能结论绑定 profile

任何容量结论必须带：revision/dirty diff、硬件、输入身份、路数、FPS、batch、拓扑、
MPS、evidence 窗口、cooldown、duration/drain、artifact 和 residual state。

- T4 40 路当前已验证，不外推到 60；
- 4090 60 路早期通过，不自动证明后续代码；
- pressure publisher 不等于混合真实 RTSP；
- 原始 24 FPS evidence 与 4/8 FPS 分析必须分别验收。

## 13. 当前资源决策

- T4 production preset：40 路、MPS、media 10 active / 5 remux / 5 finalizer process；
- 4090 preset：60 路、无 MPS、media 12 / 8 / 16；
- media-worker 使用单进程 scheduler + bounded lanes + process finalizer，而不是旧的
  单线程 pacer，也不是多个无围栏 media-worker 容器；
- 扩并发前先定位 queue wait、segment discovery、publish/DB 与磁盘瓶颈。

## 14. 干净迁移默认不带业务历史

代码、模型和可选镜像属于部署包；旧 Redis、Replay、PostgreSQL、evidence、artifacts 和
已注册图库默认不带。业务数据迁移必须另做 dump/media/identity 验收。
