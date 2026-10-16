# 阶段 1 全量 Harness 复验（路线二收回 + 运行态镜像哈希约束）

运行时间：2026-10-17T04:41:40.353419+09:00；解释器为 `/tmp/pressure60-harness-env/bin/python`，依赖锁为 `/tmp/pressure60-harness-lock.txt`，并移除 `DATABASE_URL` 与 `VIDEO_ANALYTICS_DATABASE_URL`。

命令：

```sh
env -u DATABASE_URL -u VIDEO_ANALYTICS_DATABASE_URL \
  /tmp/pressure60-harness-env/bin/python -m pytest -q harness/tests \
  --continue-on-collection-errors --junitxml=/tmp/pressure60-current-rerun-latest.xml
```

| 指标 | 基线 `98d388c` | 本轮 `recheck-r5` |
|---|---:|---:|
| tests | 1928 | 2121 |
| passed | 1839 | 2035 |
| failed | 15 | 11 |
| skipped | 73 | 74 |
| collection error | 1 | 1 |

按完整 `nodeid + kind` 比较，新增失败/错误为 **0**；当前失败集合是基线集合的子集。

消失的失败/错误：

- `harness/tests/test_midterm_pressure60_script.py::test_restore_rolling_cache_sinks_recreates_stopped_services_from_daily_compose:failure`
- `harness/tests/test_midterm_pressure60_script.py::test_restore_rolling_cache_sinks_restarts_only_originally_running_services:failure`
- `harness/tests/test_midterm_pressure60_script.py::test_restore_rolling_cache_sinks_sanitizes_pressure_shell_env_and_verifies_daily_config:failure`
- `harness/tests/test_midterm_pressure60_script.py::test_start_rolling_cache_sinks_fails_when_dual_raw_fanout_missing:failure`

本轮新增的运行态代码哈希测试也通过；纯预检仍只校验渲染配置，不读取旧容器。完整 XML、摘要、集合差异和源码哈希见本目录其他文件。
