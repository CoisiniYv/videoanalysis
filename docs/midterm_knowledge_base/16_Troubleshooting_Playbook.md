---
type: troubleshooting
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - troubleshooting
  - runtime
  - evidence
---

# 排障手册

## 总体顺序

```text
用户现象
  -> 当前 profile/apply-status
  -> DB 配置与 runtime epoch
  -> source/branch/queue/FPS
  -> Redis producer/group lag/pending
  -> task status/phase/owner/lease/reason
  -> rolling/Replay media source
  -> final artifact + DB index
  -> 8090 query/render
```

先确认是 full rolling preset 还是 single compatibility path，后续检查完全不同。

## 8090 打不开或部分页面失败

- `/health`；
- API proxy 是否返回 502/504；
- evidence-viewer/api container；
- PostgreSQL/Redis；
- 浏览器静态资源 query version/cache。

8090 `/docs` 不存在不是故障。

## 配置保存后丢失/误重启

- 查 DB row 是否成功；
- 查 config sync 结果；
- 查 generated snapshot；
- 确认没有误调用 runtime apply；
- 不要手改 YAML 覆盖 DB；
- ROI/规则保存不应触发 full restart。

## 完整启动失败

1. `GET runtime/topology-config/apply-status`；
2. phase/error/details；
3. source 选择是否精确 40/60；
4. A/B、ROI、rolling、MPS 预创建容器；
5. Savant B model cache；
6. evidence guard；
7. DB enabled cameras 和 branch assignment；
8. dynamic source/RTSP readability。

API 在启动中重启后，状态会失败；不要假设后台线程还活着。

## 页面说 single，但实际双分支存在

`runtime/control` 有兼容 mode 字段。联合检查：

- topology plan/effective mode；
- A/B Savant/raw-fanout/source 数；
- pipeline ready；
- current epoch；
- apply-status。

不要只用一个 mode 字段判断。

## 没有事件

- source 是否在正确分支可见；
- Pose/Face output 是否增长；
- camera rule/ROI 是否启用；
- behavior exporter/write errors；
- `security.events` 是否有新行；
- runtime epoch/source_id 是否一致。

没有 event 时不要先调 media-worker。

## 没有人体轨迹

- `security.person_observations` 是否增长；
- exporter queue/drop/error；
- `person-observation-workers-midterm` lag/pending；
- 独立 person worker 是否运行；
- batch DB insert/duplicates/errors；
- Redis maxlen 是否在 lag 期间 trim 未读消息。

event-worker 正常不代表 person worker 正常。

## watchlist 无命中

- ROI exporter/payload TTL；
- ROI worker pending/expired/batch/engine；
- face observations 与 embedding norm；
- gallery person/embedding 是否存在；
- effective vector backend；
- pgvector/Qdrant query、threshold、target filter；
- face-worker event publish/ACK；
- watchlist event 是否被 event-worker 接收。

不要默认 Qdrant 已启用；先看 env/runtime。

## Full preset evidence 慢或没有

正常链：

```text
event -> evidence_task -> rolling coverage -> media scheduler
  -> image/remux -> finalizer -> DB index
```

依次检查：

1. task 是否因 prefill activation/cooldown 创建；
2. source/epoch 对应 rolling segment 是否存在；
3. segment FPS、metadata commit marker；
4. task phase/retry reason/ready/deadline；
5. lease owner/token/generation/heartbeat；
6. read pin 和 retention；
7. lane/WIP/source cap；
8. finalizer handoff/process；
9. DB bundle/artifact/timeline/overlay；
10. cleanup_pending。

`security.record_requests=0` 是 full preset 的正常特征。

## Single path evidence 慢或没有

兼容链：

```text
record_request -> clip-worker proof/plan/admission -> Replay job
  -> video-file-sink -> media-worker
```

检查 Redis pending、proof、Replay slot fence/create state、shard/source map、job id、sink
identity/stability、media handoff。不要把这套步骤套到 full rolling preset。

## 视频短、卡顿或框错位

- 输入 fixture/camera raw FPS；
- rolling mux PTS cadence；
- fragment endpoint/internal gap；
- event frame UUID 到 mux window 映射；
- MOV duration/frame rate；
- DB timeline clip_frame_index；
- original PTS/epoch/session filter；
- bbox/person_context。

分析 4/8 FPS 的稀疏框不等于 24 FPS 视频卡顿。

## Media queue 高

分开看：

- event-to-DB late arrival；
- waiting_ready/coverage；
- segment index discovery/lock；
- WIP、remux depth、finalizer depth；
- finalizer pool wait 与实际 finalization；
- publish/DB index；
- rolling maintenance lock wait；
- deadline/oldest ready。

增加 remux/finalizer 数之前先定位哪一段饱和。

## Redis pending 不为 0

看 group、oldest idle、lag 是否持续增长，以及 DB durable state。Full preset 应重点看
events/person/face/ROI/source annotations；record request group 主要属于 single path。

## PostgreSQL 是否瓶颈

- connection pool in-use/timeout/reset；
- lock waits；
- cleanup/cooldown/ready queries 是否命中新索引；
- migration 032 是否应用；
- bundle list/detail query；
- timeline/overlay insert；
- autovacuum/dead tuples/WAL/checkpoint。

## 压测失败记录

必须保存 revision/dirty diff、input identity、profile/observed env、runtime samples、Redis/
PG、task details、logs、visual gates、cleanup audit。失败 artifact 不删除，不用后续成功
覆盖同名 run。
