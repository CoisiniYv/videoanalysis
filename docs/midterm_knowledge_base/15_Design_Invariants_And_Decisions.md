---
type: design-decisions
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - architecture
  - decisions
  - invariants
---

# 设计不变量与决策

本页记录已经形成共识的架构约束。后续 agent 改代码前，应先确认没有破坏这些不变量。

## 不变量 1：8090 是管理入口

用户应主要使用 8090：

- 摄像头；
- ROI；
- 算法规则；
- 人员/人脸；
- evidence；
- runtime status；
- performance；
- topology；
- storage maintenance。

API 8000 是内部服务，不作为用户直接入口。

影响：

- 新功能优先接入 8090；
- 8090 文案要中文友好；
- UI 应展示 camera name / zone name，不要只展示内部 ID；
- 高风险操作必须有确认、审计和状态提示。

## 不变量 2：PostgreSQL 是配置和业务事实源

事实源：

- cameras；
- camera_zones；
- camera_rules；
- persons；
- person_gallery_embeddings；
- events；
- evidence metadata。

非事实源：

- `cameras.midterm.yml`；
- `sources.generated.yml`；
- `/data/video-analytics/artifacts` 压测快照；
- 8090 页面缓存状态。

影响：

- 不要手改 generated YAML 当作最终配置；
- runtime apply 必须从 DB 导出；
- camera drift 判断要查 DB、generated config 和 runtime container 三者。

## 不变量 3：配置保存不等于 runtime apply

普通保存：

- ROI；
- line；
- threshold；
- cooldown；
- rule target；
- algorithm enable/disable。

这些不应重启推理链路。

runtime apply：

- source 启停；
- RTSP URL 改变；
- topology 改变；
- performance apply；
- 手动受控重启。

影响：

- API 和 8090 必须区分两个按钮/动作；
- 保存配置后只做 config sync；
- 回归测试必须防止 ROI 保存触发容器重启。

## 不变量 4：证据链保留 raw clip，metadata DB-backed

当前证据策略：

- 文件系统保留 `raw_clip.mov`；
- PostgreSQL 保存 bundle/artifact/timeline/overlay/annotation metadata；
- 8090 从 DB-backed API 展示；
- raw clip playable 是硬门槛；
- annotation complete 是增强项，可 degraded。

不采用：

- 所有 evidence 都烧录标注视频作为唯一输出；
- 只靠 sidecar 文件扫描作为主索引；
- 把 jsonl 小文件作为长期主索引。

影响：

- 不要改变证据存储方式，除非明确设计迁移；
- media-worker 改动要保持 DB-backed evidence index；
- 8090 evidence API 不应退回全目录扫描。

## 不变量 5：Replay 是取证时间窗权威

analysis-forwarder 会降采样；Savant 只处理分析帧。

证据要从 Replay 取：

- 事件前窗口；
- 事件后窗口；
- raw clip；
- constant-cadence replay job。

影响：

- 推理 FPS 低不等于证据视频 FPS 低；
- evidence 前后 5s/自定义时长由 Replay job 控制；
- source -> Replay 稳定性是证据链基础。

## 不变量 6：事件风暴下允许 admission 跳过低价值证据

当前系统不是“每个事件都完整物化”。

设计目标：

- 保留预算内高价值证据；
- watchlist/live-search 优先；
- intrusion 事件风暴可被跳过；
- retained evidence 可播放且可审查。

影响：

- `materialization_skipped` 大量存在时，不要直接判定失败；
- 压测要看 retained evidence 的目标和质量；
- 如果生产要求全事件物化，需要重新设计 finalizer 扩展模型。

## 不变量 7：受控重启必须保护 evidence

运行时重启可能影响：

- Replay job；
- video-file-sink 输出；
- clip-worker pending；
- media-worker materializing；
- evidence task status。

影响：

- runtime apply 前要做 evidence guard；
- stale active task 要终态收敛；
- 不相关服务不应被牵连重启；
- 操作后要验收 retained evidence 没受影响。

## 不变量 8：性能结论必须绑定 profile

任何性能结论都必须带：

- git commit；
- dirty diff；
- source count；
- source type；
- FPS；
- batch；
- topology；
- GPU；
- evidence target；
- runtime epoch；
- artifact path。

不能说：

- “60 路已经通过”但不说 3/4/8 FPS；
- “16 FPS 通过”但其实只是 16/1 入口配置；
- “4090 通过所以 T4 也可以”；
- “pressure source 通过所以真实 RTSP 通过”。

## 决策：同卡双分支优先于单分支硬顶 8 FPS

原因：

- 单分支 60 路 8 FPS 出现 queue full；
- 同卡双分支 30+30 能降低单 branch 消费压力；
- pressure source 8 FPS retained evidence 已通过；
- 生产仍需真实 RTSP soak。

影响：

- 4090 优化档优先 dual_same_gpu；
- topology 和 replay shard 必须保持一致；
- clip-worker 必须按 shard 路由 Replay job。

## 决策：media-worker 先用 deadline-aware pacer

不直接上多容器/worker pool 的原因：

- 300 秒 evidence deadline 提供缓冲；
- 单进程 pacer 已将 CPU peak 降到约 98%；
- worker pool 会引入 DB claim、重复终态、cleanup 竞态；
- 当前 retained evidence 目标已达成。

升级条件：

- 真实 RTSP soak 下 lifecycle p95/p99 超 300s；
- materialization_expired 增长；
- production 要求全事件 evidence；
- backlog 长期不下降。

## 决策：face-worker 注册图库查询使用 Qdrant derived index

原因：

- 小图库 exact search 不是当前瓶颈；
- PostgreSQL ANN 可能改变阈值语义，且仍会把在线检索压力留在主库；
- Qdrant 更适合作为可重建的图库向量 serving layer；
- watchlist 命中需要可解释和可回归；
- 生产图库可能扩展到数千人员、每人多张图片。

当前状态：

1. PostgreSQL 仍是 `person_gallery_embeddings` 事实源；
2. Qdrant 是 derived index，可从 PostgreSQL bootstrap/reconcile；
3. 通过 PostgreSQL transactional outbox 同步 upsert/delete；
4. 当前 authoritative runtime 为 `FACE_VECTOR_BACKEND=qdrant`；
5. Qdrant 结果默认 exact rerank，阈值语义不变；
6. pgvector path 保留为 rollback / exact baseline；
7. 60 路 8 FPS 压测 fallback=0，Qdrant p95/p99 为 3ms/4ms；
8. 20,000 向量 benchmark all-search p95/p99 为 4.037ms/6.427ms。

后续路线：

- 如果 `face-worker` 仍有 pending/ACK 压力，优先拆 persistence/matching 队列；
- 如果图库扩展到 50k/100k active embeddings，再追加 Qdrant 规模 benchmark；
- 历史 `face_observations` 相似搜索仍是单独产品/索引设计，不混入注册图库 cutover。

## 决策：干净迁移不携带旧业务数据

迁移目标：

- 新机器可启动程序；
- 带代码、模型、可选镜像；
- 不携带旧 Redis/Replay/PostgreSQL/evidence/person 状态。

原因：

- 用户明确不需要旧数据；
- live runtime state 容易造成漂移；
- 人脸库在 DB 中，新机器可重新注册；
- evidence 历史不属于干净部署必需物。

## 修改设计前要问的问题

- 会不会破坏 8090 作为统一入口；
- 会不会让 generated YAML 变成事实源；
- 会不会让 ROI 保存触发 runtime restart；
- 会不会改变 evidence 存储方式；
- 会不会把 analysis-forwarder 采样误当成 Replay 存储；
- 会不会让 admission 语义不透明；
- 会不会让 performance 结论失去 profile 绑定；
- 会不会让迁移重新携带旧状态。
