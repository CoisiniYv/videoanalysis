# Current Mainline Status

更新时间：2026-07-22

## 当前基线

- 分支：`feat/roi-adaface-redis-20260711`；
- 产品 checkpoint：`fd39fdb`；exact-lease 修复：`2a57f20`；
- Candidate C 验证文档基线：`cb0595e`；本文是其后的 docs-only 结论增补；
- 当前容量修复工作分支：`codex/segment-index-concurrency-fix-20260721`；最新结构提交
  `f075b46`，pressure restore 修复 `da35730`，dispatcher observability
  `647dd2f`/`3c80722`，production grouping disable `555afef`，finalizer attribution
  `478bff5`；尚未合入，且 exact r300 仍未通过全部 Spec 33 容量门，不能声明为 60 路默认容量；
- 部署入口：`scripts/midterm_start.sh`；
- Compose：`infra/docker-compose.midterm.yml`；
- 用户入口：`http://<host>:8090/operator`；
- 当前架构权威说明：`docs/current_architecture.md`。

user157 在 `cb0595e` 完成后工作区干净。下表的“已实现”表示代码/配置存在；“已验证”
只在有对应 revision、参数和 artifact 时成立。

## 实现状态

| 领域 | 当前实现 | 验证状态 |
| --- | --- | --- |
| 8090 管理入口 | UI、API/media 代理、摄像头/人员/evidence/维护/运行页 | 已实现 |
| 完整启动 | 摄像头批量选择、T4/4090 预设、异步 apply/status、五段进度 | 已实现；后台线程不跨 API 重启 |
| 双分支推理 | 单 GPU A/B，Replay/raw-fanout/Savant，自动或手动分片 | T4 40 路已验证；4090 60 路有早于最新双时间域改造的通过记录 |
| ROI AdaFace | Savant 导出 ROI，独立 TensorRT worker 批量 embedding | T4 40、历史 4090 60 均有验证 |
| 人体轨迹 | 独立 `person-observation-worker` 批量写 PostgreSQL；丢失 Redis group 后从 retained rows 自愈 | 40/60 压测报告均有覆盖；group 自愈与日志轮转已做代码/运行 smoke，仍缺 restart soak |
| rolling-cache | 自有 GStreamer sink、原子 fragment/manifest、单 worker/128 outstanding FIFO durable publication、stage/commit 与 queue-residence/service attribution、生产 preparation group limit=1、双时间域、分 catalog COW segment index、有界 I/O admission；工作分支另含 bounded pin、publication journal、selected-identity/metadata reuse、DB bulk-row rebase bypass、default-off single-inode v2 与 alias-free metadata-only v3 publication | Rounds 27-32 的 grouping/arbitration/并发/fdatasync/single-inode 容量诊断均被拒绝；metadata-only v3 静态和真实双镜像正确性通过，但压力容量尚未测，exact r300 与后续门仍未解锁 |
| evidence 固化 | Scheduler V2、image/remux/finalizer lanes、进程 finalizer、DB pool | exact-lease 正确性通过；`a88472c` 后 finalizer publish 已不再是 p95 主约束，60 路严格容量门仍未闭合 |
| 生命周期 | materialization v2、lease/fence/handoff、Replay create fencing | migrations 029–031；`2a57f20` exact-transfer 通过一小时正确性门 |
| 热路径索引 | cleanup recovery 与 algorithm cooldown concurrent indexes | migration 032 已提交；目标 DB 是否应用仍需单独核对 |
| Evidence UI | DB-backed list/detail/timeline/overlay；视频 HTTP Range | T4 审计通过；文件接口仅兼容 |
| 向量匹配 | pgvector 默认；Qdrant 可选且可由 PostgreSQL 重建 | Qdrant 有历史 benchmark，不是当前默认 profile |

## 当前完整双分支主链

```text
RTSP -> Replay A/B -> replay-raw-fanout A/B
  |-> sampled Savant A/B -> events/person/face ROI/source annotations
  `-> full-rate rolling-cache-sink A/B

events -> event-worker -> evidence_tasks
person stream -> person-observation-worker -> trajectories
face ROI -> adaface-roi-worker -> face-worker -> watchlist events
tasks + rolling segments -> media-worker -> DB-backed evidence -> 8090
```

完整预设 suppress `security.record_requests` 且关闭 Replay fallback。因此
`clip-worker -> Replay job -> video-file-sink` 是普通单分支和兼容链，不是完整预设主链。

## 当前运行预设

| 预设 | 当前代码参数 | 状态 |
| --- | --- | --- |
| `production_t4_40` | 40 路、20/20、4 FPS、batch 4、ROI 16、MPS 45/45/10、media 10/5/5 | 当前生产基线 |
| `local_4090_60` | 60 路、30/30、8 FPS、batch 4、ROI 16、无 MPS、media 12/8/16 | 输入/正确性已复验；Candidate B/C override 的一小时容量均失败，默认容量未获准 |

## 最近有效证据

1. `production_t4_pressure40_currentcode_2026-07-14.md`：T4 40 路、4 FPS、600s +
   120s drain 正式通过；
2. `production_runtime_evidence_audit_2026-07-15.md`：生产双分支约 4 小时稳定审计，
   evidence 无失败/过期/fallback，但同步波峰 queue wait p95 约 47 秒；
3. `local4090_pressure60_worker_regression_remediation_2026-07-13.md`：4090 60 路、
   8 FPS 在 7 月 14 日当时 revision 和正确 fixture 下通过；
4. `local_rolling_cache_dual_clock_remediation_2026-07-15.md`：后续双时间域实现通过
   40 路、4 FPS 正式门禁；
5. `media_worker_finalizer_admission_fenced_retry_2026-07-21.md`：`2a57f20`
   exact-lease 正确性通过；Candidate C 60 路、8 FPS 一小时只有
   4,742/5,781 materialized（82.03%），不能作为默认容量配置。

严格结论：当前代码可把 T4 40 路作为已验证基线；4090 60 路的输入稳定性、bundle
完整性与 exact-lease 正确性已在最新 revision 复验，但持续容量没有通过。B/C 两轮均在
约 300 秒 deadline 前形成 attempt=0 ready backlog，Phase 6 保持打开。

首个 two-slot I/O admission r300 诊断把 poll-gap p95 降到 1.736s，但仍形成
70.94s ready-to-remux p95；它同时暴露 person consumer `NOGROUP` 循环和约
168GB/85GB person/face Docker 日志。当前实现已增加 retained-row group 自愈和
50MB×3 日志轮转，受控删除 group 的 smoke 与 103 项相关测试通过。

防护生效后的 two-slot 受控复跑保留在
`pressure60_8p1_ioadm2_b10m_r300c_20260721T1740Z`；ready-to-remux/media queue/lifecycle
p95 仍为 105.45s/126.82s/127.21s。随后通过 artifact-audited override 只把 admission
改为 three-slot，结果保留在
`pressure60_8p1_ioadm3_b10m_r300_20260721T1809Z`。该轮 60/60、8.0527 FPS、969/969
正式任务 materialized、1,007/1,007 retained video 全部通过视频与 annotation 门，person
persistence 104,679/104,679，且 send/queue/raw loss、attempt=0 expiry、recovery、
claim-busy、duplicate、finalizer failure 与 residual 均为 0。

three-slot 把 ready/media/lifecycle p95 明显改善到 37.21s/57.80s/58.30s，poll-gap p95
1.360s，但仍未达到 5s/10s/30s，oldest-ready p95=44.21s，metadata visibility p95=9.43s。
实际 remux、DB claim、finalizer pool wait p95 仅 0.708s/0.383s/0.792s；主要等待仍在
index admission/refresh/pin。

最终 four-slot 简单候选保留在
`pressure60_8p1_ioadm4_b10m_r300_20260721T1837Z`。输入、969/969 正式任务、1,008
retained video、annotation、person persistence 和全部 fence/residual 继续通过，但 ready/media/
lifecycle p95 退化到 55.77s/76.73s/77.21s，oldest-ready p95=62.93s，metadata visibility
p95=10.46s。slot-wait 略降而 refresh/pin/lock-hold 上升，确认增加并发放大 I/O convoy。
不再测试 width 5；下一轮以 width 3 为对照做 refresh/pin publication 结构修复。

第一项结构修复已在工作分支 `b7068b0` 落地：完整 modern source bounds 只发布请求窗口 overlap
及两侧 guard 的 read pin，legacy/无效/无 overlap 保留全 catalog。真实 bind-mounted
artifact `segment_index_window_pin_smoke_20260721T192612Z` 证明 10 个 catalog leaf pin 5 个、
实际选择 3 个，source→mux 5+5 映射正确；同一 marker 下 retention 删除 5 个无关 leaf、
跳过 5 个 pinned leaf，same-size/same-mtime inode replacement 仍被 fence，legacy 仍 pin 10/10，
并输出 `segment_index_pinned_segments=5`。

随后相同 width-three r300 结果保留在
`pressure60_8p1_pinwin_ioadm3_b10m_r300_20260721T192945Z`。该轮 60/60、8.0469 FPS、
973/973 正式任务和 1,010/1,010 retained video 全部 materialized；视频、8090、timeline、
annotation/bbox/person-context、person persistence、exact-lease 和末态 residual 全通过。
pin-publication p95 从 1.934s 降到 0.073s，ready/media/lifecycle p95 也改善到
28.57s/48.69s/48.96s，但仍未达到 5s/10s/30s；oldest-ready p95=37.02s，metadata
visibility p95=10.27s，也超过 2s。refresh p95=3.009s，成为剩余主要 index 子阶段；
因此这是方向性改善而不是 r300 pass，r3840 继续禁止。harness 的唯一 declared failure
仍是 `adaface_roi_watchlist_events_zero`，不能用它掩盖独立的严格容量门失败。

`0bc6c82` 已修复 finalizer metric flattening 丢失 pinned-segment 等 count 字段；`1ec97fc`
则让 changed parent 复用已 catalog 的 immutable leaf membership，只 probe 新/pending manifest。
真实容器 `segment_index_refresh_pin_smoke_20260721T200925Z` 已证明 32 known + 1 new 只探测
新 manifest，finalizer flatten/log 均保留 pinned=5，并再次通过 modern/legacy pin、retention、
identity fence 和零 marker residual。

相同 width-three r300 已保留在
`pressure60_8p1_leafreuse_ioadm3_b10m_r300_20260721T2011Z`：60/60、8.0499 FPS、972/972
正式任务与 1,011/1,011 retained video 最终 materialized，视频/8090/annotation/person
persistence、exact-lease 与末态 residual 全通过。但 ready/media/lifecycle p95 退化到
36.60s/57.03s/57.53s，oldest-ready p95=40.68s、metadata visibility p95=7.50s，正式窗口
末仍有 79 active/39 ready，依靠 drain 才清零；r300 严格门失败，r3840 继续禁止。

该轮 artifact 的 5,847 是 300 秒 retention 后的保留快照，12,122 次 `new_or_changed` 是
累计计数，二者不能相除或作为 refresh 放大率。独立并发红测 `25d5fec` 已稳定复现同一
catalog 的重复 refresh，`b69575b` 在全局 width-three 之前增加 per-(source,epoch)
singleflight，并把 refresh watermark 记录在完成时刻；WIP/remux/finalizer/deadline 均
保持不变。真实容器
`segment_index_singleflight_smoke_20260721T204645Z` 的 8/8 用例证明同 catalog 只 refresh/parse
一次、跨 source/snapshot 不阻塞，且 bounded/legacy pin、retention、identity fence、count log
和零 marker residual 均保留。

相同 width-three r300 已保留在
`pressure60_8p1_singleflight_ioadm3_b10m_r300_20260721T2049Z`：60/60、8.038 FPS、
971/971 正式任务与 1,009/1,009 retained video 最终 materialized，视频/8090/annotation/
person persistence、exact-lease 与末态 residual 全通过。但 ready/media/lifecycle p95 为
53.42s/73.83s/74.37s，oldest-ready p95=58.93s、metadata visibility p95=15.24s，正式窗口
末仍有 101 active/65 ready，依赖 drain 才清零。slot wait/refresh p95 为 4.455s/2.575s，
而 per-catalog lock wait p95 仅 0.045ms、累计约 21ms；singleflight 修复了确定性竞态，但
不是压力瓶颈。r300 严格门仍失败，r3840 继续禁止。

`0f7d775` 随后加入 16MiB 有界、checksum、atomic rotation 的 segment publication journal：
sink 先原子 rename 完整 segment，再 best-effort append compact manifest 与 immutable identities；
append failure 不撤销 segment，filesystem reconciliation 继续负责 crash-window/deletion recovery。
真实容器 `segment_index_publication_journal_smoke_20260721T213820Z` 证明普通 refresh 只消费新
record、零 retained-directory scan、pin/retention 与零 residual。clean r300
`pressure60_8p1_pubjournal_ioadm3_b10m_r300_20260721T214242Z_r2` 的输入、969/969 正式任务、
1,005 retained video、annotation/person persistence 和全部 fence/residual 通过，但 ready/media/
lifecycle p95 仍为 35.24s/56.59s/56.89s；因此不是 capacity pass。更早的
`pressure60_8p1_pubjournal_ioadm3_b10m_r300_20260721T2139Z` 同时含宿主 DB 端口失败和主动中断
的 run-id collision，只是保留的无效启动 artifact。

`c06cf28`/`307a9c4` 再把 periodic reconciliation 改为 journal-tail-first：journal-covered leaf
先成为 catalog membership，随后目录审计只解析未 journal 的 rename crash leaf并 prune deleted
leaf。`segment_index_journal_first_reconcile_smoke_20260721T221437Z` 通过这组合同、两段 pin/
retention 和零 marker residual；`...T221307Z` 是测试 clock 超过 60s pin TTL 的保留 harness-only
失败。相同 width-three r300
`pressure60_8p1_journalfirst_ioadm3_b10m_r300_20260721T221602Z` 仍未过门：60/60、8.058 FPS、
973/973 正式与 1,011 retained 全 materialized，视频/8090/annotation/person/fence/residual 全通过，
manifest parse 总时长从 28.059s 降到 1.756s；但 ready/media/lifecycle p95 为
51.36s/70.52s/70.88s，正式尾部仍有 101 active/64 ready，依赖 drain。r3840 继续禁止。

当前最窄下一步是补齐 remux phase 盲区，而不是组合扩容：`remux_total` p50/p95 为
3.475s/7.438s，扣除现有 index/remux/pin 分段后仍约有 1.146s/4.771s 未归属；代码在 selected
frame metadata 的 pretty JSON serialize/write 前停止 `materialization_ms`，随后立即 parse 同一
文件。先以红测加入 publish time/bytes、reload、handoff-build 和 unattributed 指标，再只优化
实测主导子阶段。

`7e238a8`/`70d4e75` 已把 metadata publish time/bytes、immediate reload、handoff build 与
unattributed residual 贯通 handoff/recovery/log/artifact，未改变调度或序列化。exact diagnostic
`pressure60_8p1_metapubmetrics_ioadm3_b10m_r300_20260721T225212Z` 的 60/60、8.0518 FPS、
970/970 正式与 1,009 retained、视频/8090/annotation/person/fence/residual 全通过；capacity 仍以
ready/media/lifecycle p95 28.29s/48.47s/48.66s 失败。约 239KB metadata 的 pretty publish/reload
p95 是 1.101s/0.885s，但 residual p95 3.334s 与 `segment_index_lock_hold_ms` p95 3.067s 的逐
job 相关系数为 0.972。下一变量收窄为 `_catalog_segment_identities()` 对 selected 5-6 leaf 的
direct lookup，避免在 catalog lock 内 scan/resolve 全 retention catalog；metadata 格式和容量
本轮不同时改变。

`e88a4ed`/`72413a2` 已先红后绿实现该 direct lookup。真实容器
`segment_index_selected_identity_smoke_20260721T232129Z` 在 128 leaf catalog 中取 6 个 identity
时保持零全表 value visit、6 次 resolve，且 bounded pin、123+5 retention 删除、marker/full-row
residual 全通过。exact r300
`pressure60_8p1_selidentity_ioadm3_b10m_r300_20260721T232319Z` 保持 width 3、Candidate B
`20/12/8` 与 `8/4/8`、600s/120s、300s disk-backed retention 和固定 fixture/hash。输入 60/60、
8.0482 FPS，971/971 正式任务与 1,011 retained 全 materialized；视频、8090、timeline、annotation/
bbox/person-context、person persistence、exact lease 和末态 residual 全通过。该实现把 lock-hold p95
从 3.067s 降到 63ms、unattributed p95 从 3.334s 降到 1.926s，证明结构变量真实生效；但
ready/media/lifecycle/DB lifecycle p95 仍为 54.82s/74.66s/74.90s/77.23s，oldest-ready p95
62.77s、metadata visibility p95 13.83s，正式尾部 97 active/60 ready，依赖 drain，故 r300
严格门仍失败且 r3840 禁止。harness declared failure 仍含 `adaface_roi_watchlist_events_zero`，
不能代替独立容量判定。

当前剩余可直接归因的 normal-path 同步 round trip 是约 238KB metadata 的 pretty publish/reload，
p95 1.201s/0.988s，handoff-build 仅 58ms。下一单变量先以红测要求 normal success path 直接复用
内存中的 exact selected metadata 构造 immutable handoff，禁止立即重读刚发布的文件；crash recovery
继续读 durable file，on-disk pretty JSON、WIP/remux/finalizer、width、retention 和 deadline 不变。

`d73759d`/`fcbe2bd` 已实现这条 normal-path reuse；
`rolling_metadata_handoff_smoke_20260721T235829Z` 证明 normal loader 0 次、reload metric 0、durable
pretty JSON payload/bytes 不变，且 recovery 单独 load 一次。exact r300
`pressure60_8p1_metareuse_ioadm3_b10m_r300_20260721T235925Z` 输入 60/60、8.0385 FPS、零
send/queue/raw loss，969/969 正式与 1,008 retained 全 materialized，视频/timeline/person
persistence/fence/residual 均通过。reload p95 归零、remux-total p95 6.897s→5.876s，但
ready/media/lifecycle/DB lifecycle p95 仍为 50.87s/70.17s/70.69s/72.86s，oldest-ready
57.65s、metadata visibility 21.64s，正式尾部 82 active/41 ready；严格容量门失败，r3840 禁止。

该轮另暴露 8 个 annotation correctness failure：DB person-context fallback 已日志确认每 event
恢复并写出 16-27 annotation frames，但返回值缺 `annotations`，使 expanded-row DB index 显式收到
`overlay_rows=[]`、写入 0 行，随后又 prune 唯一 JSONL。结果 1,000/1,008 annotation 通过，8 个
bbox/person-context 均缺。下一步必须先以红测固化并修复这个 in-memory overlay propagation；之后
才允许用独立红测测试 metadata compact serialization，且不改变任何 capacity knob。

`7736ec5`/`7e01432` 已完成这项 fallback 修复。disposable DB smoke
`fallback_overlay_index_smoke_20260722T002952Z` 证明返回、JSONL、durable overlay、timeline 均为
2 行且 API 可读 person-context。`faee207`/`f11f561` 随后以独立红测把 selected-frame durable
metadata 改为 compact JSON；`compact_metadata_handoff_smoke_20260722T003409Z` 把 240-frame
payload 从 122,972 缩到 70,886 bytes（42.36%），decoded payload、zero normal reload、一次
recovery load 与零 pin residual 均通过。

exact r300
`pressure60_8p1_metacompact_ioadm3_b10m_r300_20260722T003536Z` 保持 width 3、Candidate B
`20/12/8` 与 `8/4/8`、600s/120s、300s disk retention 和固定 fixture/hash。输入通过 60/60、
8.0525 FPS 与零 send/queue/raw loss；972 formal 和 1,011 retained 最终全 materialized，
1,011/1,011 video/detail/timeline/annotation/bbox/person-context 全通过，DB 有 241,629 timeline、
63,718 overlay rows、144,238 bbox objects、66,403 person-context objects。person persistence
101,918/101,917、loss 0，expiry/recovery/retry-failure/claim-busy/duplicate/finalizer-failure 与末态
task/lease/lane/finalizer/pin residual 均为 0。

compact publication 确实改善 hot path：metadata median 237,431→156,786 bytes，publish p95
1.248s→0.962s，reload 保持 0，remux-total p95 5.876s→3.996s；ready/media/lifecycle/DB
lifecycle p95 也改善为 26.23s/48.30s/48.61s/50.53s。然而 formal 尾部仍有 72 active、31
ready、13 finalizer-pending，依赖后续 quiescence/drain，严格容量门仍失败。finalizer 已成为新暴露的
主约束：WIP/remux/finalizer depth p95=20/8/12，pool wait/finalization/handoff-admission p95=
4.658s/4.908s/6.340s，129 handoff 需要 durable retry。per-event log 显示 fenced canonical
publish p50/p95=1.890s/3.789s，terminal 后到 lane return 另占 0.230s/3.092s；下一单变量先拆分
publish heartbeat/rename/rebase 与完整 lane service，不先加 process workers 或组合扩容。

该轮 harness declared failure 为 `adaface_roi_watchlist_events_zero`。ROI transport 本身健康：发布
34,111 embeddings、DB 覆盖 60 source/30,696 observations、pending 0；face-worker gallery query=0
是因为数据库没有 active Reese/Finch targets，并持续记录 `watchlist rule has no active targets`。
下一 exact gate 必须先恢复图库前置条件；不能降低 watchlist gate，也不能用它替代独立容量失败。

`f65120c`/`478bff5` 已补齐 finalizer publish 与 complete lane-service attribution。遗漏 container
DSN 的 `pressure60_8p1_finalizerattrib_ioadm3_b2m_r300_20260722T012220Z` 在 sampling 前中断，禁止
作为证据；修正 DSN 的 120s canary
`pressure60_8p1_finalizerattrib_ioadm3_b2m_r300_20260722T013251Z` 显示 245 个 video 的 publish
total p50/p95=0.903s/2.253s，其中 canonical rebase 单独占 0.852s/2.129s，heartbeat/prepare/rename
p95 仅 100/82/26ms，complete lane service 为 1.625s/3.827s。因此下一单变量明确落在 rebase，
而不是继续加 worker。

Reese/Finch 已通过真实 ONNX operational registration path 注册为 active、primary、512 维、
unit-normalized 且 `real_embedding_used=true` 的图库向量。`a88472c` 的红测先证明旧 publish 会递归
遍历 DB-backed timeline/overlay 大列表；实现只跳过 `_db_timeline_rows`/`_db_overlay_rows`，其余
top-level 与 metadata/summary path 仍 canonical rebase。post-fix 120s smoke
`pressure60_8p1_rebasefast_ioadm3_b2m_smoke120_20260722T014950Z` 通过 303 formal、375 retained
与 100 watchlist image；rebase p50/p95 降至 24/40ms，lane service 降至 0.333s/0.780s。

exact r300
`pressure60_8p1_rebasefast_ioadm3_b10m_r300_20260722T020149Z` 保持 width 3、Candidate B
`20/12/8`、finalizer `8/4/8`、600s/25s postfill/120s drain、300s retention 和固定 fixture/hash。
输入通过 60/60、8.0538 FPS、零 send/queue/raw loss。图库修复后正式负载更强：1,500 formal=
970 video+530 watchlist image；1,552 retained=1,007 video+545 watchlist image，全部 materialized。
1,552/1,552 detail、1,007/1,007 video/timeline/annotation 全通过，bbox/person-context 缺失 0；
DB 有 240,574 timeline、63,414 overlay、144,080 bbox object、66,306 person-context object。
person persistence 100,893/100,891、measured loss 0；35,033 gallery query 与 692 emitted hit 均
零失败，末态 task/lease/Replay-slot/finalizer/pin/Redis pending 全为 0。

finalizer 结构修复动态有效：rebase p50/p95=24/49ms、publish total=27/109ms、lane service=
0.354s/1.249s、pool wait/finalization/handoff-admission p95=1ms/0.815s/60ms；1,007/1,007
handoff immediate admission，零 reject/retry/recovery。ready-to-claim p95 12.006s 已通过 Spec 33
release `<=15s`，lifecycle 26.140s、DB claim 93ms 与 CPU 296.42% 也通过各自 release 门；但
media queue p95 25.803s 仍高于 `<=15s`，rolling metadata visibility p95 19.008s 仍高于
`<=2s`，且 ready 尚未达到 final closure `<=5s`。formal cutoff 后仍有 25 个 video task，虽在
25s postfill 内全部完成（最晚 +15.82s），也不能覆盖上述失败。harness `status=passed` 只代表其
已编码 correctness gate；严格 r300 仍失败，r3840 与一小时验收继续禁止。

exact 结束后的 live audit 已恢复 daily single branch、零 enabled camera、零 pressure process、
worker DSN `postgres:5432`、max-active/remux `4/4`、finalizer `32/0/4`、rolling disabled、300s
retention、256-row cache、index width 2、Redis save 与 PostgreSQL checkpoint 默认值；相关 worker
restart count 均为 0，Reese/Finch 图库注册仍保留。

Round 22/23 后续诊断把剩余 tail 动态收窄到 host-storage fsync stall：在
`pressure60_8p1_remuxattrib_ioadm3_b6m_r300_20260722T034818Z` 中，sink A/B 的
4.060s/4.478s durable publish 与 8.331s media runner poll 同时出现，后者主要是 prepare/claim
5.195s 和 handoff persist 3.120s，candidate query/finalizer admission 仅毫秒级。`c86430e` 因此只把
未改动的 durable publish 序列移到每 sink 一个单 worker、128 outstanding FIFO dispatcher；
`647dd2f` 保留 terminal bound/backpressure/drain 指标。真实容器
`rolling_publication_dispatcher_smoke_20260722T042517Z` 已通过两 source exact order、hard-bound
backpressure、error continuation/source failure 和 shutdown drain，两个服务均报告
`drained=True` 与 final queue/outstanding/active/failure/timeout=0。该结论不是 60-route capacity
证明；exact r300、r3840 和一小时验收仍未解锁。

对应 360s causal diagnostic
`pressure60_8p1_pubdispatch_ioadm3_b6m_r300_20260722T043320Z` 已完成：60/60、8.0751 FPS、零
send/queue/raw loss，918/918 formal 与 989 retained 全 materialized，636 video 与 353 watchlist
image 的 8090/timeline/annotation/bbox/person-context、person persistence、exact fence 和 residual
全通过。dispatcher A/B outstanding peak 92/97、final queue/outstanding/active/failure/timeout 全 0。
但 media queue p95=19.316s、metadata visibility p95=7.901s，严格门失败；对应 10.298s scheduler
tick 仍与两 sink 一串 1.6-3.15s fsync-heavy publication 同时发生。slowest segments 自身 publish
仅 8-100ms，却在 shared FIFO 前等待约 11s。因此本轮 falsify“只移出 GLib 即可通过”的假设；
该轮之后先补 submit→worker-start queue residence 与 submit→complete attribution，未运行 exact r300。

`3fb27f0`/`3c80722` 已完成上述 instrumentation-only attribution。真实容器
`rolling_publication_dispatcher_timing_smoke_20260722T051218Z` 证明 per-segment capacity wait、
FIFO residence、durable service、dispatch total、peak identity、error continuation 与 shutdown
terminal totals。随后不变的 360s causal diagnostic
`pressure60_8p1_pubtiming_ioadm3_b6m_r300_20260722T051559Z` 通过 60/60 输入、930/930 formal、
1,000/1,000 retained、639 video 与 361 watchlist image 的媒体/8090/annotation/person/fence/
residual 门，但严格 ready/media/visibility p95 为 6.668s/23.426s/3.256s，仍失败。

直接归因显示 A/B durable-service p95 仅 28.8/33.2ms，worker 平均 busy 约 16.4%；FIFO
residence p95 却为 5.172/4.916s、p99 13.768/13.724s，两个队列都达到 outstanding 128。
dispatch total 与 residence 相关系数 0.998，而与自身 service 仅约 0.18；最慢 visible segment
自身 service 5-33ms，却等待 13.5-14.1s。

`0479f07`/`f4bf094` 随后把 bounded FIFO group preparation 先红后绿：同一 worker 最多 stage 32
个 natural cohort item，再按 exact FIFO 逐项执行所有原 fsync/rename/journal fence。真实容器
`rolling_publication_group_smoke_20260722T060238Z` 通过 `1/3/1` group、顺序、middle-stage-error
continuation、hard backpressure 与 clean drain。

不变的 Round 27 360s causal diagnostic
`pressure60_8p1_pubgroup_ioadm3_b6m_r300_20260722T060611Z` 保持 60 路、Candidate B、width 3、
r300 和固定 fixture/hash。60/60 输入、915/915 formal、986/986 retained、636 video/350 image、
8090/annotation/person/fence/residual 全通过，但 ready/media/lifecycle/visibility p95 退化到
19.265s/33.298s/33.987s/22.799s。dispatcher residence p95/p99 从 5.041s/13.761s 退化到
14.004s/31.765s，dispatch-total p95 从 5.216s 退化到 18.385s，capacity-wait events 从
A/B `28/27` 增到 `339/341`。size-32 prewrite 在第一次 retained per-item fsync 前制造更多 dirty
metadata，并放大跨 sink flush convoy，因此该假设已拒绝。`db20d24`/`555afef` 已把生产/default
preparation limit 固化为 1，同时保留显式 multi-item 诊断和全部 stage/commit metrics。

`d874756`/`2888927` 随后以红绿测试加入共享 epoch-root commit `flock`，并保留 staging 在锁外、
全部 fsync/rename/journal fence、每 sink 单 worker/128 outstanding 与 exact per-sink FIFO；lock
wait/hold 同时进入 per-segment 日志、sink 聚合和 pressure artifact。真实跨容器
`rolling_publication_commit_arbiter_smoke_20260722T064452Z` 证明两个容器使用相同 lock inode，
一个在另一个约 254ms hold 后等待 253.952ms，且注入 rename failure 后 peer 等待 247.203ms
仍成功 commit。

不变的 Round 28 360s diagnostic
`pressure60_8p1_pubarb_ioadm3_b6m_r300_20260722T064800Z` 的 correctness 完整通过：60/60、
8.0591 FPS、零 send/queue/raw loss、925/925 formal、993/993 retained，635 video 的 window/FPS/
timeline/annotation/bbox/person-context 与 358 image、8090/fence/residual 全通过；唯一 warning 是预期
`validate_seq_iq` sampling gap。但 capacity 相对 Round 26 明显退化：ready/media/lifecycle/visibility
p95 从 6.668s/23.426s/19.923s/3.256s 变为 10.714s/26.607s/27.131s/16.531s，dispatcher
residence/total p95 从 5.041s/5.216s 变为 13.879s/14.025s，capacity-wait event 从 55 增到 373。

arbiter 正确消除了跨 sink 慢 lock-hold overlap，但产生 74.592s peer lock wait；两个 FIFO queue
堆在同一全局慢 holder 后，扩大 residence、dispatch、visibility 与 callback backpressure。该假设已
拒绝。`a0b7f63`/`e590f9b` 将生产/default arbitration 固化为 `False`：普通 publisher 不打开
epoch commit-lock 且 wait/hold 精确为 0，显式 opt-in 与全部诊断仅保留历史复现；完整 sink suite
42 passed、pressure harness 201 passed、rendered deployment smoke 29 passed。该时点 exact r300、
r3840 与一小时验收继续禁止；后续因果轮次与当前准入边界如下。

Round 29-31 后续分别拒绝了 two-worker sharding、two-slot commit lanes 和 regular-file
`fdatasync`。Round 32 的 default-off single-inode v2 又通过 60/60 输入、922/922 formal、
992/992 retained 与全部媒体/8090/annotation/person/fence/residual 门，并把 regular-file sync
从 80.352s 降到 74.568s；但 directory sync 从 47.179s 升到 98.547s，residence/dispatch p95
约翻倍，capacity-wait 55→200，visibility p95 3.256s→14.251s。因此该变量也被拒绝。下一单变量
只删除 v3 metadata-only 表示中的 hard-link alias，保留一个 file fsync、两道 directory fsync、
atomic rename、journal/reconcile 和 daily split default。`da35730` 同时确保 pressure cleanup 真正
从 daily Compose 重建 sink 配置，不再只停掉带诊断环境的容器。

`22aa940`/`f075b46` 已把上述 v3 合同先红后绿：`metadata.json` 第一行是有界
`rolling-segment-manifest-v3`，其后为 exact native rows，且不创建
`segment_manifest.json`。journal 中 v3 的 manifest/metadata identity 相等，但 index catalog key
为 `metadata.json`；initial rebuild、periodic reconcile、fallback、read-pin fence 与 v1/v2
兼容均保留。静态门为 191 + 243 + 47 passed（另 1 expected skip），focused selection 329
passed。真实双镜像 artifact
`rolling_publication_metadata_only_smoke_20260722T104522Z` 的 sink/index marker 均通过；它只关闭
实现正确性，不代表 ext4/FIFO/visibility 容量改善。下一步仍是唯一变量的 360 秒 Candidate B
width-three r300 因果诊断。

## 已知开放项

### P0/P1

- 保持 production preparation limit=1、commit arbitration=False；显式 group/arbiter 只用于历史复现，
  不在生产 sink 启用；
- metadata-only v3 的红测、静态与真实双镜像 crash/recovery/index 合同已通过；下一步只运行
  不变 360s r300 唯一变量诊断，不组合调整 worker、queue、retention、index width、finalizer
  或 deadline；
- 全部既有 fsync/rename/journal fence、每 sink 单 worker/128 outstanding 与 Candidate B/width 3/
  retention/deadline 保持不变；
- r3840 和一小时验收继续禁止；只有后续 exact r300 的 input、Spec 33 capacity/visibility、
  watchlist、correctness、annotation 与 residual 全通过，才允许进入 3,840s retention 短门；
- 不同时增加 process workers、WIP、remux、finalizer queue 或 index width，也不放宽 deadline；
  当前 finalizer pool/finalization/handoff-admission p95 已仅 1ms/0.815s/60ms；
- 把 Reese/Finch active operational registration 纳入压测前置审计，保持真实 embedding 与
  watchlist gate，不把图库内容再次当成隐式机器状态；
- 完成真实混合 RTSP 的断流、重连和长 soak；
- 完成 event/person/face/media worker restart/recovery soak；
- 继续观察生产 T4 84–85°C、70W power cap 和 evidence 波峰排队。

### P2

- topology apply 改为跨 API 进程可恢复的持久作业；
- 修正 `midterm_health.sh` 对 legacy source 和新 person worker 的固定服务清单；
- 增加鉴权、RBAC 和更完整操作审计；
- 实现 8090 WebSocket upgrade 代理；
- 如启用 Qdrant，必须显式记录 profile/env、sync/outbox 和 fallback 状态。

## 文档规则

当前架构和状态只由本文件、`docs/current_architecture.md`、部署说明与当前代码共同决定。
带日期报告是证据，不是永久 current status；`archive/phase-only/` 不作为部署或设计入口。
