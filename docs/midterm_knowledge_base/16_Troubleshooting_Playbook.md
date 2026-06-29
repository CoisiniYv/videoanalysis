---
type: troubleshooting
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - troubleshooting
  - operations
  - diagnostics
---

# 排障手册

本页按症状给出优先排查路径。排障时先定层级，不要直接改代码。

## 总体排障顺序

1. 先看用户看到的问题属于 8090、配置、推理、事件、取证、证据展示还是性能。
2. 查 `git status --short`，确认是否有未提交运行快照或用户改动。
3. 查 Docker 容器真实状态。
4. 查 Redis stream lag/pending。
5. 查 PostgreSQL 事实源和任务状态。
6. 查相关 worker 日志。
7. 查 pressure artifact 或 runtime metrics。
8. 最后才改代码。

## 8090 保存后刷新丢失

可能原因：

- 前端只改了本地 state，没调用 API；
- API 写错字段；
- camera/rule ID 混用了 UUID 和文本 zone/rule id；
- DB 写入成功但导出 runtime config 失败；
- 读取接口从 generated YAML 而不是 DB 回显。

检查：

- 浏览器 network；
- API response；
- PostgreSQL `camera_zones` / `camera_rules`；
- generated `cameras.midterm.yml`；
- 8090 刷新后的 GET response。

修复方向：

- 保存路径以 DB 为准；
- zone/rule 使用稳定文本 ID；
- config sync 只导出，不重启；
- UI 回显从 DB API 读。

## ROI 点选或显示错乱

可能原因：

- canvas 坐标和视频显示尺寸不一致；
- normalized coordinate 转换错误；
- 第一帧 screenshot aspect ratio 不稳定；
- overlay 层拦截 pointer event；
- 点编号来自旧 state 未清空；
- 多 ROI 列表展示不清，用户以为没保存。

检查：

- 前端 canvas bounding rect；
- polygon 坐标是否 0-1；
- 保存 payload；
- DB 中 zone geometry；
- 右侧列表是否显示当前 ROI；
- 刷新后是否仍存在。

## 修改算法配置后误重启

症状：

- 保存阈值/ROI 后 Savant/forwarder/source 重启；
- evidence 生成被中断；
- 8090 变卡。

原因：

- 前端保存按钮调用 runtime apply；
- API router 混用了 config sync 和 controlled apply；
- camera source controller 触发全量 reconcile。

验收修复：

- 保存规则后 `containers_restarted=[]`；
- `source_containers_touched=[]`；
- evidence guard 不触发；
- generated config 更新；
- 8090 刷新值不丢。

## 8090 显示状态漂移

先区分三种状态：

- DB `cameras.enabled`；
- generated source/Savant config；
- Docker container running/exited。

典型误判：

- `primary_rtsp` 在 compose 中 exited，但 DB disabled；
- dynamic `lab` source 正常运行；
- runtime overview 把 disabled compose source 当成错误。

处理：

- 以 PostgreSQL 为配置事实源；
- 以 Docker 为运行事实；
- disabled source 的 exited 应降级为 info；
- 8090 要展示“配置启用”和“运行容器”两个维度。

## 没有事件

按链路查：

```text
source adapter
  -> Replay
  -> analysis-forwarder
  -> Savant
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
```

检查项：

- source 是否 running；
- Replay 是否收到帧；
- forwarder seen 是否增长；
- forwarder forwarded 是否增长；
- Savant effective FPS；
- Redis `XLEN security.events`；
- event-worker log；
- DB `events` 是否新增。

常见原因：

- ROI 配置不包含目标；
- algorithm disabled；
- Savant camera config 未 sync；
- forwarder queue 被下游堵住；
- source PTS 异常。

## watchlist hit 不触发

按链路查：

```text
Savant face exporter
  -> security.face_observations
  -> face-worker
  -> face_observations
  -> pgvector/Qdrant gallery query
  -> watchlist_hit event
  -> event-worker
```

检查：

- 人员是否在 `persons`；
- 图库 embedding 是否在 `person_gallery_embeddings`；
- face observation 是否新增；
- embedding norm/quality 是否合格；
- watchlist threshold；
- target person filtering；
- face-worker Redis lag；
- 当前 `FACE_VECTOR_BACKEND`；
- Qdrant health / collection alias / vector count；
- Qdrant outbox pending/failed/lag；
- fallback-to-pgvector count；
- `match_results`；
- `security.events` 是否有 watchlist_hit。

注意：

- watchlist hit 不应被 intrusion cooldown 影响；
- source observation hit 对应人名应在 evidence 中延续展示；
- 后续高于阈值的帧应继续红色/命中态展示，不要突兀变 unknown。

## evidence 生成慢

先拆阶段：

1. event-worker 创建 task；
2. record request 发布；
3. clip-worker claim；
4. proof 等待；
5. Replay job；
6. video-file-sink 写 raw clip；
7. media-worker 扫描；
8. ffprobe/decode/integrity；
9. DB-backed bundle 写入；
10. 8090 查询。

看指标：

- Redis pending；
- `evidence_tasks.status`；
- `materialization_status`；
- media-worker queue wait；
- lifecycle p95/p99；
- deadline slack；
- media-worker CPU；
- sink 输出目录；
- `imageio_ffmpeg_fallback_count`。

判断：

- lifecycle p95 < 300s 且 retained playable 达标，慢但可接受；
- p95/p99 接近 300s，要调 pacer 或 finalizer；
- raw clip 已有但 DB bundle 没有，查 media-worker；
- task pending 但没有 Replay job，查 clip-worker/proof。

## evidence 不完整

先区分：

- raw clip 不可播放；
- raw clip 可播放但 annotation missing；
- 8090 list 查不到；
- 8090 detail 查到但 overlay 少；
- DB 有 bundle 但文件丢。

常见原因：

- Replay job 失败；
- sink metadata 缺失；
- frame annotation 没对齐；
- media-worker fallback；
- DB terminal update 失败；
- cleanup 太早。

当前语义：

- raw clip playable 是核心；
- annotation missing 可以 degraded；
- 不要把 annotation missing 等同于 evidence 完全失败。

## forwarder queue 背压

判断问题在 forwarder 还是 Savant：

- null sink 测试通过：forwarder 自身通常不是瓶颈；
- 接 Savant 后 queue 满：下游 Savant 消费不足或模型链慢；
- send failures 非 0：ZeroMQ/Savant 连接或消费异常；
- Savant effective FPS 低：看 batch、parallel streams、模型阶段。

处理方向：

- 降 FPS；
- 调 batch；
- 增 parallel streams；
- 同卡双分支；
- 补 Savant 阶段级 latency；
- 减少不必要 exporter/annotation 输出。

## media-worker CPU 高

先看是否真的失败：

- CPU peak 高但 lifecycle 在 deadline 内，可能只是瞬时峰值；
- 8 FPS pacer 后 CPU peak 已降到约 98%；
- retained playable 达标就不要急着大改。

检查：

- ffmpeg/ffprobe 是否存在；
- fallback count；
- ffmpeg threads；
- decode/integrity time；
- cleanup time；
- queue wait；
- deadline slack。

避免：

- 不要直接多开 media-worker 容器；
- 不要无 DB claim 机制并发 finalization；
- 不要牺牲 terminal idempotency。

## Redis pending 不为 0

检查：

```bash
docker exec video-analytics-midterm-redis redis-cli XPENDING security.record_requests clip-workers-midterm
```

判断：

- 短期 pending 可能正常；
- 长期 pending 且日志反复 reclaim，需要查对应 event/task；
- 如果 DB event/task 已不存在，clip-worker 应终态清理并 `XACK`。

## PostgreSQL 是否瓶颈

先查：

- `pg_stat_activity`；
- locks；
- deadlocks；
- active queries；
- table/index size；
- dead tuples；
- EXPLAIN。

当前经验：

- 最近调查没有证明 PG 写队列/锁等待是 evidence 不完整主因；
- PG 热路径风险主要在 events list/query、evidence queues、face vector search；
- 大图库前不要武断上 ANN；当前在线 gallery 检索优化方向是 Qdrant derived index + exact rerank。

## 压测失败如何记录

必须记录：

- run id；
- commit；
- dirty diff；
- runtime epoch；
- source count；
- FPS/batch/topology；
- 首个失败阶段；
- artifacts；
- Redis/PG/worker/Savant/forwarder 指标；
- 失败是否可复现；
- 是否影响当前生产目标。
