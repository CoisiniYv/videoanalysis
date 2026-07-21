# Media Worker finalizer admission fenced retry 修复记录（2026-07-21）

## 1. 状态与范围

本文记录 2026-07-20 本地单卡双分支 `60 路 × 8 fps × 1 小时` 压测后，
对 Spec 33 Phase 4 正确性门的重新打开，以及 Phase 6 第一组容量候选。

本次修复范围严格限定为：

- 复用 `evidence_tasks.materialization_phase=finalizer_pending` 作为
  PostgreSQL 持久化 finalizer 队列；
- remux handoff 未进入 finalizer 内存队列时，立即按原始
  owner/token/generation 执行 fenced lease 归还；
- 保持 immutable handoff，不降级到 `waiting_ready`，不重新 remux；
- 补齐阶段时延、admission 拒绝、持久队列深度和残留状态门禁；
- 在正确性修复后，单独把共享 WIP/remux/finalizer 入口容量调整为首个
  A/B 候选。

明确不做：新建 admission 服务、放宽未过期 lease 抢占、使用无界内存
队列、仅靠增加 finalizer 数量、改变 `5+5` 证据窗口或 9 秒 ready grace。

## 2. 触发证据

基线 artifact：

```text
/data/video-analytics/artifacts/pressure60_8p1_1h_retain_4800_20260720T084830Z
```

正式窗口为北京时间 `2026-07-20 16:51:10–17:51:28`。60/60 路可见，
平均有效帧率 8.0246 fps，输入和推理链路没有 Savant send failure 或
forwarder queue-full，但 5,790 个正式证据任务中只有 5,057 个 materialized，
733 个因 business/materialization deadline 过期。

状态机与容量证据必须分开解释：

- remux 完成 handoff candidates=5,144，immediate admitted=5,072，差额
  72；`handoff_recovered=72`，一一对应 lease-expiry recovery；
- 733 个 expired 的 `materialization_attempt_count` 全为 0，表示它们从未
  进入 remux，不能用修复 72 个 handoff 来解释或掩盖；
- remux 满载采样约 41.1%，共享 WIP 满载约 23.4%，finalizer lane 满载
  约 5.4%；
- 真正 finalization p50=1.46s、p95=2.68s，主要延迟发生在 remux claim
  之前和 handoff admission 边界；
- 原配置为 WIP=12、remux=8、finalizer threads/processes/queue=4。

根因是原 Scheduler V2 在 finalizer lane/source/WIP 无容量时直接退出当前
admission 批次。remux 已经持久化 handoff 并仍持有 lease/WIP，但任务既没
进入 finalizer 内存队列，也没有即时清 lease。恢复查询正确地只选择
`materialization_lease_token IS NULL`，因此任务只能等约 120 秒 lease 到期。

## 3. 最终状态转换

```text
remux 完成并持久化 immutable handoff
        |
        v
尝试预留 finalizer lane / source slot / shared WIP
        |
        +-- 成功
        |     迁移 WorkPermit 到 finalizer
        |     内存排队期间持续 heartbeat 原 lease
        |     executor 启动后 fenced claim 为 finalizing
        |     bundle/index/terminal CAS
        |
        +-- 未提交（lane/source/WIP/shutdown/discovery/submit exception）
              使用 remux 传入的原始 MaterializationLease
              owner + token + generation 精确 CAS
              保持 materializing/finalizer_pending
              保持 immutable handoff
              清 lease，next_attempt_at=0.5–0.75s 抖动
              CAS 尝试完成后释放 shared WIP
              下个 scheduler tick 从 PostgreSQL 重新领取
```

安全不变量：

1. 未成功提交 finalizer 的 durable handoff 不能继续持有无人负责的 lease；
2. fenced CAS 尝试发生在 WIP release 之前；
3. lane full 后批次中尚未提交的所有 transfer 都必须收敛，不能只处理触发
   `break` 的当前任务；
4. recovered、无 lease 的 handoff 如果再次无容量，仍保持
   `finalizer_pending`，不能调用通用 retry 降回 `waiting_ready`；
5. recovery 查询保留 `lease_token IS NULL`，不抢占未过期 lease；
6. accepted handoff 从进入 finalizer 内存队列开始 heartbeat，而不是等线程
   真正执行后才开始；
7. capacity retry 不刷新原 handoff 的 queue-entry timestamp，确保
   `oldest_handoff_age_ms` 不会被反复 admission 重置。

## 4. 实现落点

### Repository

`services/media-worker/app/materialization_repository.py`：

- `retry_finalizer_handoff()` 保持 `finalizer_pending` 和 handoff，通过原始
  lease 精确 CAS 清 lease；capacity 原因使用 generation=1 的短退避；
- `retry_unclaimed_finalizer_handoff()` 处理 recovered handoff 的无 lease
  重试，保持阶段和 handoff；
- `recoverable_finalizer_handoffs()` 继续要求 lease token 为 NULL，并返回
  原 handoff persisted timestamp；
- `finalizer_pending_metrics()` 报告 total/unleased/leased/oldest age。

### Scheduler

`services/media-worker/app/worker.py`：

- `_FinalizerHandoffTransfer` 同时移动 `MaterializationLease`、`WorkPermit`
  和 queued heartbeat；
- `admit_metadata()` 的 public `finally` 对所有未消费 transfer 统一执行
  exact fenced retry；
- lane full、source cap、WIP、admission close、processed/invalid/already-ready、
  discovery exception 和 executor submit exception 都不能绕过 convergence；
- submit 成功前记录真实 `finalizer_submitted_*`，executor 入口记录
  `finalizer_started_*`，修正原 `finalizer_pool_wait_ms=0` 的埋点位置；
- recovered handoff 从 immutable handoff 恢复原 remux 阶段诊断。

### Pressure harness

`scripts/runtime/run_midterm_pressure60.py` 和
`scripts/runtime/run_pressure60_dual1gpu_profile.sh`：

- 独立配置 finalizer threads/process workers/queue 和 rolling max-per-poll；
- 记录 candidates/admitted/gap、handoff recovery、fenced retry、持久队列和
  drain 最后采样；
- DB summary 增加 attempt=0 expired、active lease 和 finalizer_pending
  residual；
- 正式 rolling-cache 门禁新增 lease-expiry recovery、unaccounted admission
  gap、fenced retry failure、attempt=0 expiry、lease/WIP/lane/pending residual。

## 5. Phase 6 首个容量候选

正确性门通过后使用：

| 参数 | 基线 | Candidate B |
| --- | ---: | ---: |
| shared WIP | 12 | 20 |
| remux workers | 8 | 12 |
| finalizer threads | 4 | 8 |
| finalizer queue | 4 | 8 |
| rolling max-per-poll | 原有效值 | 8 |
| finalizer process workers | 4 | 4 |

process workers 暂不升到 8。先用修正后的真实
`finalizer_pool_wait_ms`、bundle process 提交/完成/失败计数判断是否存在
process-pool wait。PostgreSQL 是持久队列，内存 queue 不做大容量蓄水。

## 6. 指标与验收

新增/修正指标：

- `ready_to_remux_claim_ms`、`remux_ms`；
- `handoff_to_finalizer_admission_ms`；
- `media_finalizer_admission_rejected reason=<reason>`；
- `finalizer_handoff_retry_total/failed`；
- `finalizer_pending_total/unleased/leased/oldest_age_ms`；
- 修正后的 `finalizer_pool_wait_ms`。

正确性关：

- 正常压力 `handoff_recovered=0`；
- `candidates - immediate_admitted <= handoff_retry_total`；
- `handoff_retry_failed=0`；
- duplicate materialization/bundle=0；
- drain 后 active lease、WIP、lane 和 finalizer_pending residual 全为 0。

吞吐关：

- `materialization_attempt_count=0` 的 expired task=0；
- oldest-ready 不再随时间单调增长；
- 60/60 路达到最低 7.92 fps；
- Savant send failure、forwarder queue-full/raw fanout loss=0；
- 全部 retained video 通过 ffprobe、8090 detail/timeline/annotation/bbox/
  person-context 检查。

执行顺序为 10 分钟同口径短测，成功后再做连续 1 小时正式验收；正式结论
以 artifact 和本文后续结果段为准。

## 7. 已完成的代码级验证

截至正式运行前：

```text
Scheduler V2 unit tests:                    26 passed
Pressure harness tests:                    191 passed
Disposable PostgreSQL lifecycle contracts: 6 passed
Broader targeted suite:                    346 passed, 6 skipped
```

真实 PostgreSQL 合约已在从当前 runtime schema 克隆的独立临时数据库中
执行并在退出时删除；定向回归没有失败项。

## 8. 回滚

如短测触发重复 bundle、fence loss、retry failure 或残留 lease/WIP：

1. 停止新 pressure source，等待/执行 bounded drain；
2. 保留 artifact、日志和所有 task/event/evidence 行；
3. 恢复 pressure harness 记录的原 media/event worker 环境；
4. recreate media-worker，不删除 durable `finalizer_pending`；
5. 由 PostgreSQL recovery 在 lease fence 下继续收敛；
6. 不通过删除 `lease_token IS NULL` 条件或扩大无界 queue 绕过故障。

正式运行完成后，本文追加短测/1 小时 artifact、门禁结果、配置恢复和提交
哈希。

## 9. Candidate B 实测结果

### 9.1 10 分钟短测

Artifact：

```text
/data/video-analytics/artifacts/pressure60_8p1_admissionfix_ab10m2_20260721T090841Z
```

正式采样 10 分钟，60/60 路可见，平均有效帧率 8.0447 fps。正式窗口
976/976 个任务均 materialized；含 warmup/postfill 共 1,017/1,017 个视频通过
窗口、帧率和 8090 detail/timeline/annotation/bbox/person-context 校验。短测
candidates=1,017、admitted=1,017、handoff recovery=0、duplicate=0，
oldest-ready p95=12.77s、max=20.50s。

短测唯一业务门禁失败为 `adaface_roi_watchlist_events_zero`，与 2026-07-20
基线相同，不属于 scheduler 变更。

### 9.2 首次 1 小时正式测

Artifact：

```text
/data/video-analytics/artifacts/pressure60_8p1_admissionfix_1h_20260721T092938Z
```

正式窗口为北京时间 `2026-07-21 17:32:48–18:32:48`，配置为 WIP=20、
remux=12、finalizer threads=8、queue=8、process workers=4、rolling
max-per-poll=8。输入门通过：60/60 路、平均有效帧率 8.0246 fps，Savant
send failure、forwarder queue-full、raw fanout drop/failure 均为 0。

正式窗口 5,778 个任务：

- 4,921 materialized，85.17%；
- 857 expired，14.83%，全部 `materialization_attempt_count=0`；
- drain 后 active task/lease/finalizer_pending 均为 0。

吞吐门失败的阶段证据：

- ready-to-remux claim p50=215.23s、p95=285.70s；
- scheduler tick gap p95=7.40s、max=17.37s；
- remux lane p95=12/12，WIP p95=20/20；
- finalizer process-pool wait p95=3.65s、max=9.15s；
- oldest-ready p95=299.06s，随后通过 deadline 淘汰维持有界。

含 warmup/postfill 共保留 5,002 个 bundle，5,002/5,002 通过视频窗口与
帧率检查，也全部通过 8090 detail/timeline/annotation/bbox/person-context；
轨迹持久化 578,373 条，60/60 source，loss=0，duplicate bundle=0。

因此 Candidate B 只能证明短窗口正确，不能作为 60 路持续容量配置。

## 10. 正式测发现的第二个 Phase 4 竞态

本次真实命中两条互相独立的路径：

1. `f488fced-a603-470f-9aa8-d38a3fd55f07` 在 finalizer lane full 时执行
   fenced retry，退避 0.523s，lease 被立即归还，随后正常 materialized；
2. `b16bf5ba-de5d-4e30-806d-3e84540de777` 的 handoff 于
   `09:50:37.680716Z` 持久化，`09:50:38` 已进入 admitted batch，但
   finalizer 新连接在 scheduler handoff 事务提交前使用 `SKIP LOCKED`
   重新 claim，得到 `claim_busy`。flight 完成后没有 fenced convergence，
   原 lease 于 `09:52:40.867895Z` 到期并被 recovery 回收。

根因是原始 `MaterializationLease` 虽已随 WorkPermit 到达 admission scheduler，
却没有继续传入 finalizer job 的 claim 边界。修复方式是：

- `_FinalizerAdmissionV2` / `_FinalizerJob` 携带原始 handoff lease；
- `claim_finalizer_task(..., expected_lease=...)` 先仅按
  owner/token/generation 执行阻塞式 `SELECT ... FOR UPDATE`；
- 获得行锁后再验证 `finalizer_pending`、immutable handoff 和 attempt token，
  并沿相同 token/generation 转移 owner/phase；
- 无 lease 的 recovered handoff 仍使用原有 `SKIP LOCKED` 普通领取，不允许
  抢占其他有效 lease。

真实双连接 PostgreSQL 回归测试覆盖“handoff 更新未提交时 claim 必须等待，
提交后 exact fence 成功转移”，避免用普通 mock 掩盖事务竞态。

## 11. Candidate C 容量依据

下一组 10–15 分钟 A/B 使用：

| 参数 | Candidate B | Candidate C |
| --- | ---: | ---: |
| shared WIP | 20 | 32 |
| remux workers | 12 | 16 |
| rolling max-per-poll | 8 | 16 |
| finalizer threads | 8 | 8 |
| finalizer queue | 8 | 8 |
| finalizer process workers | 4 | 8 |

queue 仍保持 8，PostgreSQL 继续是持久队列。max-per-poll=16 用于覆盖实测
7.40s 的 p95 tick gap；WIP=32 允许 16 个 remux 与最多 16 个 finalizer
lane owner 同时存在；process workers=8 则由已修正埋点的 3.65s p95 wait
直接驱动。Candidate C 短测只有在 attempt=0 expiry=0、oldest-ready 不累积、
handoff lease-expiry recovery=0 后才能进入新的 1 小时正式测。
