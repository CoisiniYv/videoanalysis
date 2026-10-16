# 阶段 1 全量 Harness 复验（路线二收回后）

运行时间：2026-10-17 04:30 KST；解释器为 `/tmp/pressure60-harness-env/bin/python`，依赖锁为 `/tmp/pressure60-harness-lock.txt`，并移除 `DATABASE_URL` 与 `VIDEO_ANALYTICS_DATABASE_URL`。

命令：

```sh
env -u DATABASE_URL -u VIDEO_ANALYTICS_DATABASE_URL \
  /tmp/pressure60-harness-env/bin/python -m pytest -q harness/tests \
  --continue-on-collection-errors --junitxml=/tmp/pressure60-current-rerun.xml
```

| 指标 | 基线 `98d388c` | 本轮 `recheck-r4` |
|---|---:|---:|
| tests | 1928 | 2119 |
| passed | 1839 | 2033 |
| failed | 15 | 11 |
| skipped | 73 | 74 |
| collection error | 1 | 1 |

按完整 `nodeid + kind` 比较，新增失败/错误为 **0**，当前失败集合是基线集合的子集。消失的 4 个失败用例为：

- `test_restore_rolling_cache_sinks_recreates_stopped_services_from_daily_compose`
- `test_restore_rolling_cache_sinks_sanitizes_pressure_shell_env_and_verifies_daily_config`
- `test_restore_rolling_cache_sinks_restarts_only_originally_running_services`
- `test_start_rolling_cache_sinks_fails_when_dual_raw_fanout_missing`

前三项现在使用原始 sink Env 快照作为恢复期望，修复了测试夹具与正式恢复契约的错配；最后一项补齐 Docker Env 只读替身，使测试到达原本要验证的缺失双路 fanout 分支。其余 11 个失败和 1 个 collection error 均在基线中已有。完整 XML、摘要、集合差异和源码哈希见本目录其他文件。
