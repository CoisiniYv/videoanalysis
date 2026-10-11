# media-worker 候选补位与老任务排序：离线修复（2026-10-11，KST）

## 状态与审查范围

- 分支：`CZray/evidence-remux-wake-review-20261011`。
- 本次增量基线：`53759651ed70bc11741ed49f0688e4e135914b09`。
- P0 的开关快照及共享许可释放唤醒已在上述基线中完成，不是仍待实现。
- 用户已关闭 uos157。本次全部在开发机修改、运行离线测试，没有访问服务器、部署或启动压测。
- 本次没有提高 CPU、WIP 或剪辑并发，没有调整分析帧率、人脸间隔、分析队列年龄、冷却时间和 180 秒验收线。

## 已复现的候选补位问题

原 runner 在单剪辑通道下通常只选一个来源、获取一个候选。准备函数直接返回空值、抛出异常，或来源名额在选择后被其他通道占用，都可能让本轮没有任何剪辑提交。没有运行中的任务完成，就不能依靠完成唤醒立即补位。

新回归分别复现了以上三种情况。修复同时扩大来源选择窗口和候选行窗口；只增大 SQL LIMIT、仍只选一个来源是不够的。

- 默认候选窗口：`max(本轮提交上限, remux_workers) + 4`；单通道时最多看 5 条。
- 首条不可提交时继续尝试本次有限窗口中的下一条。
- 实际提交数量仍受 `max_per_poll`、可用剪辑槽、WIP 和每源许可约束。
- 候选全部失败就结束这一轮，不做无界循环或同轮反复查库。
- 失败路径释放已经拿到的执行名额；仅成功提交的来源推进轮转游标。
- 这是有限补位，不保证任意数量不可执行任务下每轮都能找到可执行任务。

素材覆盖不足的情况需要区分执行路径：启用 segment index 时，素材发现和 pin 通常发生在剪辑 worker，失败的 Future 本来就会发出 P0 完成通知；准备阶段直接跳过和认领竞争才是本次测试直接覆盖的空等路径。不能把所有覆盖失败都说成没有完成通知。

## 排序修复保留摄像头公平性

没有采用“所有等待超过 60 秒的任务全局最老优先，绕过摄像头轮转”。现有测试专门覆盖忙摄像头挤占安静摄像头的回归；60 秒冷却限制输入频率，不等于已经保证了处理顺序的公平性。

这次采用更小范围的调整：

1. 高优先级任务仍优先。
2. 仍按每源 rank 交错，并保留持久化的摄像头轮转顺序。扩大候选窗口后，SQL 显式使用传入的来源顺序，防止旧的全局 due-time 排序让后面的来源抢走本轮首选来源的机会。
3. 在同一来源、同一优先级内，等待超过 60 秒的已就绪任务按原始 `materialization_ready_at` 优先。这样一个老任务即使刚到新的重试时间，也不会仅因 `next_attempt_at` 较新被较年轻的任务反复排到前面。
4. 尚未到重试时间的任务仍由 WHERE 条件排除；年龄不绕过重试时间、素材就绪条件、许可或租约。
5. image 候选排序不变。

这不是全局最早截止时间调度，也没有证明上一轮 20 条未生成任务由排序造成。它能避免特定的老任务排序问题，不增加硬件处理能力，不保证 180 秒达标。

## 配置与恢复

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `ROLLING_CACHE_MATERIALIZATION_CANDIDATE_LOOKAHEAD` | 4 | 额外候选数量；限定为 0–64，0 不增加额外候选行 |
| `ROLLING_CACHE_MATERIALIZATION_AGING_SECONDS` | 60 | 同源任务的年龄提升门槛；0 关闭年龄提升 |

两个参数均进入 Config、compose、midterm.env，以及压测脚本的 media-worker 环境快照，原值可随既有配置恢复流程保存和恢复。它们控制候选尝试与排序，不改变素材内容或数据库结构。

来源轮转顺序的显式保留是补位正确性的一部分；把两个参数设为 0 不等于逐字恢复整段旧排序代码。严格 P0/P1 对照应分别使用提交 `5375965` 和本次提交，而不是把开关关闭视为完全相同的旧实现。

## 离线验证

测试使用本机已缓存的 Python 3.12、uv、pytest、PyYAML、psycopg、Redis、Pillow 和 imageio-ffmpeg，没有安装或启动新的数据库服务。

- P1 修改前的现有公平性、完成唤醒及 scheduler_v2 基线：**65 passed / 8 skipped**。
- 新文件修复前：**12 failed / 2 passed / 4 skipped**；其中 3 个用例直接复现首候选被跳过后槽位闲置，其余失败覆盖新窗口边界、配置字段、快照及 SQL 排序契约。
- 修复后，上述三套加新补位套件：**79 passed / 12 skipped**。
- 媒体准备/性能安全、长驻资源及压测脚本兼容套件：**288 passed / 5 failed**。
- 两组修后测试合计：**367 passed / 5 failed / 12 skipped**。未运行全量 harness。

5 项失败的名称和错误消息与基线逐项一致：2 项调用本机不存在的 Docker；3 项原有恢复夹具的保留时间是 300 秒，而仓库配置要求 600 秒。未通过修改断言或产品默认值隐藏这些问题。

12 项跳过均因未配置 `EVIDENCE_FAIRNESS_TEST_DATABASE_URL`。其中新增的 4 项实际 PostgreSQL 用例验证：同源老重试任务优先、年龄提升关闭时保持 due-time 顺序、旧来源不抢新来源的轮转机会、优先级与重试资格不被绕过。

**SQL 参数和排序结构已离线检查，但上述真实数据库用例尚未执行。** 在部署前应指向可丢弃的测试数据库运行，不能使用生产数据库；测试会建立并删除自己的 fixture schema。

```bash
uv run --offline --python 3.12 \
  --with pytest --with pyyaml --with 'psycopg[binary]' --with redis \
  --with pillow --with imageio-ffmpeg \
  python -m pytest -q --tb=short -p no:cacheprovider \
  harness/tests/test_media_worker_remux_refill.py \
  harness/tests/test_evidence_source_fairness.py \
  harness/tests/test_media_worker_completion_wake.py \
  harness/tests/test_media_worker_scheduler_v2.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_media_worker_long_lived_resources.py \
  harness/tests/test_midterm_pressure60_script.py
```

## 开机后的验证边界

先由 Opus 审查本次增量，再执行真实数据库回归和部署验证。若需要分清收益，先测 P0 的 `5375965`，再保持输入、CPU、并发和业务规则不变测试 P1；把 P0/P1 一起部署只能说明组合效果。

随后按原隔离条件运行 60 路 × 4 FPS × 1 小时，检查候选数量、准备/提交数、空闲槽、可剪后等待、队列走势及完整视频解码；验收仍包含所有应取证视频和未生成任务。`0.84 秒` 是历史中位数，不能据此承诺单通道平均容量为 `1 / 0.84` 或宣称并发 2 必然达标。

本次有限候选窗口只约束单轮查询和尝试数量，不是整个调度循环的每秒频率限制。完成通知已合并，但高频图片完成时的调度/日志成本仍需观测。

新提交与推送记录使用 Asia/Seoul（+09:00）；提交时间与客户端推送开始/完成时间分别记录，具体 SHA 和完成时间见交接回复。
