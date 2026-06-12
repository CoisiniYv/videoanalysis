# Midterm 多摄像头源收敛与 Savant 性能可观测性修复记录

日期：2026-06-12

## 修复范围

- 摄像头新增、更新、启用、禁用后改为触发 source-only runtime apply，不再走完整 Savant/Replay/worker 重启路径。
- 保留完整 `/api/v1/cameras/runtime/apply` 作为规则、区域、算法和模块配置变更后的重载路径。
- 加固 `source_id`：限制字符集和长度，拒绝空值、`.`、`..`，并在已有事件/证据/观测数据后阻止变更。
- Savant supervisor 增加 source convergence 分支：模块运行但 source 状态不健康时只收敛 source，避免误重启 Savant。
- Operator 页面摄像头保存/启停/手动按钮改为“应用摄像头源”，规则/区域/算法保存仍走完整 runtime apply。
- 新增 `SavantPerfMetricsPyFunc`，导出稳定 `va_savant_*` metrics，并按 `source_id` 标注多路 source。
- Event API 响应补齐 `camera_name`，便于多摄像头事件回显。

## 运行时验证

- 完整 runtime apply 成功，最终验证 epoch：
  `midterm-20260612T082406Z-a717774c`
- Savant ready：
  `savant_ready=true`，`savant_ready_wait_seconds=2.01`
- source 启动结果：
  `primary_rtsp` 通过 compose source 启动；
  `source_00000000-0000-4000-8000-781078565686` 通过 dynamic source 启动。
- 手工复刻 perf smoke 的 metrics 检查通过：
  `PASS_MANUAL_SAVANT_PERF_OBSERVABILITY_METRICS`
- 5 秒采样窗口内 `va_savant_frames_seen_total` 推进：
  `primary_rtsp` delta `68`；
  `source_00000000-0000-4000-8000-781078565686` delta `69`。
- Savant restart count 采样前后保持 `0`。
- Redis `security.frame_annotations` 中两路 source 均有当前 epoch 的 frame annotation。
- Savant 最近 500 行日志未发现：
  `Traceback`、`fatal`、`ZeroMQ`、`send timeout`、`backpressure`。
- runtime doctor 通过：
  `runtime_doctor_ok=True`
  summary: `/tmp/video-analytics-runtime-doctor-summary.json`

## 验证命令

```bash
pytest -q harness/tests/test_camera_api_runtime_sources_direct.py harness/tests/test_camera_runtime_apply_service.py harness/tests/test_savant_supervisor_service.py harness/tests/test_api_event_evidence_camera_name.py harness/tests/test_midterm_deployment_contract.py harness/tests/test_operator_face_registration_static.py harness/tests/test_savant_perf_metrics_contract.py
docker compose -f infra/docker-compose.midterm.yml config
git diff --check
env PYTHONPYCACHEPREFIX=/tmp/va-pycache python3 -m py_compile services/api/app/routers/cameras.py services/api/app/schemas/cameras.py services/api/app/repositories/cameras.py services/api/app/services/runtime_apply.py services/api/app/services/savant_supervisor.py services/api/app/schemas/events.py modules/savant_security/custom/pyfuncs/savant_perf_metrics.py
bash scripts/smoke/current/check_midterm_deployment.sh
env SUMMARY_JSON=/tmp/video-analytics-runtime-doctor-summary.json bash scripts/runtime/doctor_midterm.sh
```

结果：

- `63 passed in 0.64s`
- compose config 通过
- `git diff --check` 通过
- py_compile 通过
- `PASS_MIDTERM_DEPLOYMENT_CONTRACT`
- `runtime_doctor_ok=True`

## 备注

- Codex sandbox 中 `FastAPI TestClient` 会在 Starlette/anyio portal 阶段卡住，因此本次新增 `test_camera_api_runtime_sources_direct.py`，直接调用路由/服务函数覆盖摄像头 API 行为。
- Codex sandbox 里执行 `bash scripts/smoke/current/check_savant_perf_observability.sh` 时，脚本内 `curl > file` 会因 sandbox network disabled 访问 localhost 失败；实际顶层 `curl --noproxy '*' --output ...` 能访问 `18080/metrics`，因此本次用等价的手工采样复刻并通过了脚本的核心检查。
- Savant counter metric 注册名必须不带 `_total`，否则 Savant 导出时会生成 `*_total_total`。当前实现使用无后缀注册名，Prometheus 样本导出为 smoke 期望的 `*_total`。
