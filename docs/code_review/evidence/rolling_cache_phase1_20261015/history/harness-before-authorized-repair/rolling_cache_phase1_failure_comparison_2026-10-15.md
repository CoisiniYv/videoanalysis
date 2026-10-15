# 阶段 1 Harness 失败用例对照（2026-10-15）

## 结论与范围

**停止：最终全量 Harness 为基线 15 个失败、当前 17 个失败；当前新增 6 项、消失 4 项，失败集合并非基线的子集，命中任务书第 6 节阶段 1 停止条件。** 两边另各有 1 个相同的 collection error。来源为 [baseline-summary.json](evidence/rolling_cache_phase1_20261015/baseline-summary.json)、[current-summary.json](evidence/rolling_cache_phase1_20261015/current-summary.json) 的 `counts` / `failures_and_errors`，以及 [comparison.json](evidence/rolling_cache_phase1_20261015/comparison.json) 的集合差。先前保存的 11 个失败属于较早代码，不能用于本轮验收。本轮停止后未修复、未重跑，不进入推送审查或阶段 2。

## 环境、时间与计数

两轮使用 `/tmp/pressure60-harness-env/bin/python` 和同一依赖锁 `/tmp/pressure60-harness-lock.txt`，均清除 `DATABASE_URL` / `VIDEO_ANALYTICS_DATABASE_URL` 后运行 `pytest -q harness/tests --continue-on-collection-errors`。完整命令、cwd、解释器及锁路径保存在各自 summary；[dependencies.txt](evidence/rolling_cache_phase1_20261015/dependencies.txt) 保存依赖记录。基线代码为 `98d388c`，当前为提交前工作树（summary 标签 `working-tree-before-stage1-commit`）；当前确切被测文件哈希见 `comparison.source_sha256`，不能以稍后的工作树状态替代这份运行证据。

| 指标 | 基线 `98d388c` | 当前保存的一轮 | 来源 |
|---|---:|---:|---|
| JUnit tests（含 collection error） | 1,928 | 2,092 | 两份 summary 的 `counts.tests` |
| passed | 1,839 | 2,001 | `counts.passed` |
| failures | 15 | 17 | `counts.failures` |
| collection errors | 1 | 1 | `counts.errors` 与 JUnit error 内容 |
| skipped | 73 | 73 | `counts.skipped` |
| suite 开始时间（KST） | 2026-10-15 20:47:41.943159 | 2026-10-15 22:32:12.348678 | `raw_suite_timestamps` 从 UTC+08 转为 UTC+09 |
| summary 生成时间（KST） | 2026-10-15 20:49:07.860380 | 2026-10-15 22:33:23.466413 | `generated_at_kst` |
| pytest 退出码 | 1 | 1 | 命令执行记录；本轮另存 `comparison.current_harness_returncode` |

原始 suite 时间保留为 `2026-10-15T19:47:41.943159+08:00` / `2026-10-15T21:32:12.348678+08:00`。计数差为新增 164 个测试、passed 增加 162：新增测试中 162 通过、2 失败，原有 4 个失败消失、另有 4 个原有通过用例失败。来源为两份 JUnit 的 `(classname, name)` 集合差及后面的失败集合对照。

在停止决定送达前，已将这 164 个新增 nodeid 单独运行，结果为 **162 passed / 2 failed，退出码 1，6.64 秒**，并非全部通过。原始输出见 [new-tests-pytest.log](evidence/rolling_cache_phase1_20261015/new-tests-pytest.log)，逐项结果见 [new-tests-junit.xml](evidence/rolling_cache_phase1_20261015/new-tests-junit.xml)。这是本次唯一新增测试运行，不在停止后重跑。

## 完整失败名单

以下表格的“失败”均指 `kind=failure`，不混入 collection error。完整 nodeid 和异常原文来自两份 summary；更长堆栈见 [baseline-junit.xml](evidence/rolling_cache_phase1_20261015/baseline-junit.xml) 和 [current-junit.xml](evidence/rolling_cache_phase1_20261015/current-junit.xml)。所有“当前通过”均已在当前 JUnit 对应 testcase 中确认。

| # | 完整 nodeid | 基线 / 当前 | 本轮观测到的错误或变化 |
|---:|---|---|---|
| 1 | `harness/tests/test_camera_repository_rows.py::test_get_zone_names_raises_keyerror_only_when_column_missing` | 失败 / 失败 | `Failed: DID NOT RAISE KeyError`。 |
| 2 | `harness/tests/test_clip_media_legacy_behavior_baseline.py::test_required_characterization_scenarios_are_named_and_executable` | 失败 / 失败 | `late_annotation` 场景引用 `harness/tests/test_media_worker_perf_safety.py::test_late_annotation_retry_uses_unbounded_stream_id_lookback`，场景可执行性断言失败。 |
| 3 | `harness/tests/test_compare_adaface_roi_transport.py::test_load_gallery_normalizes_vectors_and_preserves_identity` | 失败 / 失败 | `ModuleNotFoundError: No module named 'app.offline_face_embedder'`。 |
| 4 | `harness/tests/test_midterm_deployment_contract.py::test_midterm_deployment_files_exist` | 失败 / 失败 | 文件存在断言 `AssertionError: assert False`。 |
| 5 | `harness/tests/test_midterm_deployment_contract.py::test_runtime_doctor_is_midterm_named` | 失败 / 失败 | runtime doctor 脚本名单不等于期望名单。 |
| 6 | `harness/tests/test_midterm_deployment_contract.py::test_midterm_source_id_and_camera_config_are_neutral` | 失败 / 失败 | 缺少 `infra/generated/sources.generated.yml`。 |
| 7 | `harness/tests/test_midterm_deployment_contract.py::test_docs_point_to_midterm_deployment_entrypoint` | 失败 / 失败 | 缺少 checkout 根目录的 `CLAUDE.md`。 |
| 8 | `harness/tests/test_midterm_pressure60_script.py::test_rolling_cache_cleanup_removes_orphan_materialized_dirs_by_metadata` | 失败 / 失败 | `FileNotFoundError: 'docker'`。 |
| 9 | `harness/tests/test_phase2_operating_point_appendix.py::test_update_appendix_text_replaces_existing_block` | 失败 / 失败 | 替换附录后仍含 `old`，`assert 'old' not in ...` 失败。 |
| 10 | `harness/tests/test_runtime_control_service.py::test_stop_dual_runtime_only_stops_dual_extension_containers` | 失败 / 失败 | `psycopg.OperationalError`，测试环境无法解析主机 `postgres`。 |
| 11 | `harness/tests/test_runtime_control_service.py::test_restart_single_runtime_blocks_when_evidence_active` | 失败 / 失败 | 实际 HTTP 状态 `503`，期望 `409`。 |
| 12 | `harness/tests/test_midterm_pressure60_script.py::test_restore_rolling_cache_sinks_recreates_stopped_services_from_daily_compose` | 失败 / 通过 | 基线两个 sink 的 retention 均为 expected `600` / observed `300`；当前改用原始环境快照，详见下文。 |
| 13 | `harness/tests/test_midterm_pressure60_script.py::test_restore_rolling_cache_sinks_sanitizes_pressure_shell_env_and_verifies_daily_config` | 失败 / 通过 | 同一 retention `600` / `300` 不匹配；当前测试补齐原始环境快照并验证 `expected_env`。 |
| 14 | `harness/tests/test_midterm_pressure60_script.py::test_restore_rolling_cache_sinks_restarts_only_originally_running_services` | 失败 / 通过 | 同一 retention `600` / `300` 不匹配；当前测试补齐原始环境快照。 |
| 15 | `harness/tests/test_midterm_pressure60_script.py::test_start_rolling_cache_sinks_fails_when_dual_raw_fanout_missing` | 失败 / 通过 | 基线在预期 missing-fanout 断言前因本地缺 `docker` 失败；当前 fixture 补 mock 后到达预期路径。 |
| 16 | `harness/tests/test_media_worker_completion_wake.py::test_pressure_completion_wake_is_verified_and_restored[false-true]` | 通过 / 失败（新增失败） | `AttributeError: 'types.SimpleNamespace' object has no attribute 'dual_shard_same_gpu'`。 |
| 17 | `harness/tests/test_media_worker_completion_wake.py::test_pressure_completion_wake_is_verified_and_restored[true-false]` | 通过 / 失败（新增失败） | 同上。 |
| 18 | `harness/tests/test_media_worker_completion_wake.py::test_pressure_completion_wake_rejects_an_unapplied_override[true]` | 通过 / 失败（新增失败） | 同上。 |
| 19 | `harness/tests/test_media_worker_completion_wake.py::test_pressure_completion_wake_rejects_an_unapplied_override[false]` | 通过 / 失败（新增失败） | 同上。 |
| 20 | `harness/tests/test_midterm_pressure60_baseline_pollution.py::test_collector_reads_mode_sinks_and_all_four_pollution_checks[False]` | 不存在 / 失败（新增失败） | 期望的 finding `retention_above_formal` 缺失。 |
| 21 | `harness/tests/test_midterm_pressure60_baseline_pollution.py::test_collector_reads_mode_sinks_and_all_four_pollution_checks[True]` | 不存在 / 失败（新增失败） | 同上。 |

## 新增的 6 项为何失败

**已确认：** 四项 completion-wake 的基线 testcase 均通过。本轮堆栈经过 `configure_media_worker_rolling_cache` → `_compose_base_args` → `compose_invocation_args` → `compose_config_paths`，最后直接读取 `cfg.dual_shard_same_gpu`；旧 fixture 的 `SimpleNamespace` 只有 `artifact_dir`、`env_file`、`compose_file`，因此产生 `AttributeError`。这是公共 Compose 参数改动引入的兼容性回归，不能记为原有环境失败。完整堆栈见 current JUnit。

**已确认：** 两项新增污染检测用例均缺少 `retention_above_formal`。collector 现以 `additional_profiles=("*",)` 调用 `_compose_base_args`，fixture 却将它 mock 为只接收 `cfg` 的 lambda。异常被 collector 捕获，正式 retention 上限未取得。两份测试产物分别记录 `formal_retention:TypeError`，并把对应服务的 `.formal_retention` 列为未核实；见 [单 sink 产物](evidence/rolling_cache_phase1_20261015/new-test-pollution-single.json) 与 [双 sink 产物](evidence/rolling_cache_phase1_20261015/new-test-pollution-dual.json)。单独运行新增用例也出现同样失败，因此不支持将其归因于全量测试顺序。

**未核实：** 尚未修复或验证上述问题，不能宣称只改 fixture 即可通过。本次只读预检自修复授权不覆盖全量 Harness 失败集合扩大，已停止等待用户决定。

## 消失的 4 项为何变化

### 三项 sink 恢复用例

**已确认：** 基线 `98d388c` 的 `restore_rolling_cache_sinks_after_pressure` 从 `daily_rolling_cache_sink_env(cfg)` 生成期望环境，再把容器回读限制到 `ROLLING_CACHE_SINK_COMPOSE_ENV_KEYS` 后比较（原码 5013–5037 行）。上述三个 fixture 把回读环境设为 `ROLLING_CACHE_SINK_COMPOSE_ENV_DEFAULTS`，其中 retention 为 `300`；本轮基线计算出的期望为 `600`，因此两个 sink 都报告 expected `600` / observed `300`，不是服务运行状态不匹配，错误中的 `state={}` 也保存于 baseline summary。

当前实现要求 `original_states[service].environment` 存在，生成恢复 override，并按该完整环境快照回读验证；这三个 fixture 同步把已使用的 `daily_env` 填入各 sink 的原始 `environment`。因此期望与 mock 观测均为 `300`，不再从另一套 daily 配置推导恢复目标。第二项断言也由 `daily_expected_env` 改为每个 sink 的 `expected_env`。证据为 `git diff 98d388c -- scripts/runtime/run_midterm_pressure60.py harness/tests/test_midterm_pressure60_script.py` 中该函数与这三个测试的差异，以及本轮 JUnit 对应 testcase。**边界：** 这是恢复契约和 fixture 同时变化后的通过，不足以单凭这三个用例证明正式服务已正确恢复；生产环境未在此测试中操作。

### 一项 missing-fanout 用例

**已确认：** 基线测试已 mock `run`、输入 FPS 探测及 `docker_container_state`，却未 mock `docker_container_env`。保存的 baseline JUnit 堆栈显示 `start_rolling_cache_sinks_for_pressure` → `docker_container_env` → `subprocess.run(['docker', 'inspect', ...])`，随后本地 `FileNotFoundError: 'docker'`；它在校验缺失的 `replay-raw-fanout-a` 前失败。当前仅在该 fixture 补 `monkeypatch.setattr(module, "docker_container_env", lambda _name: {})`，使其到达原本要测试的缺失依赖 `RuntimeError`，再断言异常含 `replay-raw-fanout-a`。这是测试隔离修正，不能当成新增的生产缺失依赖修复。

## Collection Error 单列

两边均在收集 `harness/tests/test_trajectory_thumbnail_cache.py` 时出错：第 14 行 `from app.main import MediaStaticFiles` 触发 `ModuleNotFoundError: No module named 'app.main'`。来源为两份 JUnit 的 `error message="collection failure"`，并由 summary 中 `kind=error` 对应。它不是上述 15 / 17 个失败用例之一，也未因阶段 1 修改而消失。

## 已确认、推断与未核实

- **已确认：** 本轮为 17 个失败，含新增 6 项；上述 4 项消失的代码/fixture 变化与保存的堆栈相符；collection error 没有新增；已命中停止条件。
- **推断：** 持续存在的导入、文件缺失、Docker/数据库环境类问题可能受本机测试环境影响；此表不把全部持续失败都归为环境问题，也不判断其生产影响。
- **未核实：** 修复后的测试结果、生产服务行为、Opus 审查结果及阶段 2 运行结果。本轮没有进行后续修复或重跑。
