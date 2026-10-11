# media-worker 完成唤醒复核与审查交接（2026-10-11，KST）

## 结论与状态

Opus 的完成唤醒方向得到代码路径和已有日志支持。本次在其实现上补全两个可复现的遗漏，先推新分支交 Opus 审查，再进入实机部署复测。

- 待审分支：`CZray/evidence-remux-wake-review-20261011`。
- 审查基线：`claude/evidence-remux-wake-20261011` 的 `4e0f4626e24a70d769594c8df48ff4031d5def17`，包含实现 `6828fbc`。
- 上游的压测基线：`b3073d2adfad61c78b27765b96a1b01fc614bc5e`。
- 本次未部署，未启动新压测，未合入 main。uos157 仅只读提取已有诊断数据。
- “所有应保存视频在事件发生后 180 秒内保存完成”仍未验收通过。

可用 `git diff 4e0f462..CZray/evidence-remux-wake-review-20261011` 只审本次增量；用 `b3073d2` 作基线可审完整的完成唤醒改动。

## 两项复现与修复

### 1. [P1] 压测新开关写入后必然核验失败，恢复也漏掉它

路径：`scripts/runtime/run_midterm_pressure60.py` 的 `media_worker_rolling_cache_env_snapshot()`。

原实现的配置函数写入 `MEDIA_WORKER_COMPLETION_WAKE_ENABLED`，重建容器后调用快照函数回读，并逐项核验。但快照字段列表缺少这个变量。即使容器正确应用 on/off，回读字典也没有它，因此两种取值都会抛出：

```text
media-worker MEDIA_WORKER_COMPLETION_WAKE_ENABLED was not applied after compose recreate
```

同一快照也用于保存原配置，遗漏会导致恢复时丢失原开关。修复是在既有字段列表中纳入该变量，不放松核验。

新增测试保留真实的快照、YAML 配置生成、回读比较和恢复逻辑，仅替换 Docker 执行。覆盖 false→true→false、true→false→true，并确认容器未应用请求时仍报错。

### 2. [P2] 共享许可释放后，空闲 remux 通道仍可能等旧定时点

路径：`services/media-worker/app/materialization_scheduler.py` 的 `rolling_poll_after_wake()`。

原实现只有 remux 完成才推进 rolling 准入的截止时刻。image/finalizer 完成会唤醒主循环及其完成回收，但不会让下一轮 rolling 准入立即到期。

反例：WIP 许可已经用满，remux 通道空闲，下一段视频等待许可。image 或 finalizer 完成释放共享许可后，没有 remux 任务会再发出完成通知；若 rolling 定时点尚未来临，就继续空等。每源许可也可能产生类似情形。

修复让 remux/image/finalizer 三类完成都触发一次 rolling 准入检查。原有 WIP、每源限制、通道容量和优先级仍由准入代码执行，general/recovery 的时间表不变。禁用开关时保留原定时行为；空通知和未知通道不触发提前检查。

新增测试使用真实的 bounded lane、共享 WIP、Future 回调和完成信号，覆盖 image/finalizer × 开关启用/禁用。将待到期时间设为未来 60 秒以判定是否提前，不依赖毫秒级执行速度。4 个用例中，启用开关的 2 个在原实现失败，禁用路径保持通过。

## 旧产物支持什么结论

只读提取时间：`2026-10-11T09:09:28.230589+09:00`（Asia/Seoul）。

来源：uos157 的 `/data/video-analytics/artifacts/pressure60_isolated_guard_t4_4fps_1h_20261011/pressure_diagnostics.json`。提取值保存为 [机器可读指标](evidence_remux_wake_review_2026-10-11.metrics.json)。这些是 harness 抓取日志的聚合，包含预热与收尾，不是仅含正式一小时窗口的百分位。

| 指标 | 样本数 | P50 | P95 | 最大 |
|---|---:|---:|---:|---:|
| 可剪 → remux 认领 | 2996 | 88.192 秒 | 245.492 秒 | 285.463 秒 |
| remux 执行 | 2996 | 0.615 秒 | 0.856 秒 | 1.942 秒 |
| remux 总耗时 | 2996 | 0.838 秒 | 1.104 秒 | 2.379 秒 |
| finalizer 池内等待 | 2996 | 0 秒 | 0.001 秒 | 0.014 秒 |
| finalizer 通道服务 | 2996 | 1.431 秒 | 1.923 秒 | 6.596 秒 |
| 调度 tick 执行 | 6485 | 0.017 秒 | 0.099 秒 | 4.456 秒 |
| 调度 tick 间隔 | 6484 | 0.815 秒 | 1.001 秒 | 4.561 秒 |

等待远大于 remux 执行，且代码确有固定节拍的补位等待，支持优先修这一机制。`remux_total` 的 P95 实际略高于 1 秒；CPU、存储、finalizer 和分析延迟仍可能影响整体结果。这不是受控 A/B，不能据此承诺修后必过 180 秒。

同时修订原说明中的结论边界：含旧 reader 且解码抽检失败的轮次不是整体通过基线；1382 张人脸图片就绪不等于已核实全部及时；60 路/60 秒对应约 1 条/秒仅是单视频规则的长期平均上界；3.87 FPS 低于目标，不能把全部差额算作预算的预期效果。

## 本地验证

2026-10-11（Asia/Seoul），使用缓存的 Python 3.12 和 uv。没有连接生产数据库，也没有执行真实的容器重建。

| 范围 | 结果 |
|---|---|
| 新增 8 个用例，修复前 | 4 failed / 4 passed；失败分别是两个开关快照/恢复用例、两个共享许可释放后补位用例 |
| 完成唤醒 + 长驻资源，修复后 | 32 passed（完成唤醒 19，长驻资源 13） |
| scheduler_v2 + pressure60，修复后 | 251 passed / 5 failed |
| 同样的扩展套件，原提交 `4e0f462` | 251 passed / 5 failed；失败名及错误消息逐项一致 |

复现命令：

```bash
uv run --offline --python 3.12 \
  --with pytest --with pyyaml --with 'psycopg[binary]' --with redis \
  --with pillow --with imageio-ffmpeg \
  python -m pytest -q -p no:cacheprovider \
  harness/tests/test_media_worker_completion_wake.py \
  harness/tests/test_media_worker_long_lived_resources.py \
  harness/tests/test_media_worker_scheduler_v2.py \
  harness/tests/test_midterm_pressure60_script.py
```

上述两组测试分别执行，合计 283 passed / 5 个既有失败。5 项不是全部归为“环境问题”，其具体原因为：

- 本机没有 `docker` 可执行文件（2 项）：`test_rolling_cache_cleanup_removes_orphan_materialized_dirs_by_metadata`、`test_start_rolling_cache_sinks_fails_when_dual_raw_fanout_missing`。
- 原有恢复测试夹具与仓库配置不一致（3 项）：`test_restore_rolling_cache_sinks_recreates_stopped_services_from_daily_compose`、`test_restore_rolling_cache_sinks_sanitizes_pressure_shell_env_and_verifies_daily_config`、`test_restore_rolling_cache_sinks_restarts_only_originally_running_services`；夹具观察到保留时间 300 秒，配置期望 600 秒。

未重复全量 harness。原说明里的全量结果属于 Opus 复核前的运行，不能当作这次的全量结果。

## 审查后实测

待 Opus 复审后部署该分支，只重新创建挂载源码的 media-worker。首先核验开关回读、启动日志和 `wake_lanes`；其后沿用隔离轮次的 60 路 × 4 FPS × 1 h 条件：Pose/Face/AdaFace，业务规则 intrusion + watchlist，冷却 60 秒，人脸身份刷新 5 秒，WIP 4、remux 1、原 CPU profile。该组合不能表述成所有业务算法全开。

保存旧的服务状态与环境快照并验证恢复；保留现有取证文件。复测报告同时给出全部应取证事件、保存/超时/缺失数量、事件→保存全程最大值、积压走势、视频完整解码、事件覆盖以及源/推理进程重启。180 秒标准和内部 300 秒期限分别报告，不以提高内部期限代替达标。

若仍积压，再单独测试 remux 1→2 或扩大 CPU 绑定，避免一次改变多个因素。若要估计唤醒本身的收益，on/off 必须控制源内容及其他配置；跨历史轮次对比只能提供线索。

## 时间与发布记录

本次新提交使用 `TZ=Asia/Seoul`，推送前核验 author/committer 的 ISO 时间偏移为 `+09:00`。推送开始/完成另记，不以 commit 时间冒充 GitHub 接收时间。保留上游提交和既有事件时间，不重写历史。实际新提交 SHA 与推送完成时间在交接回复中提供。
