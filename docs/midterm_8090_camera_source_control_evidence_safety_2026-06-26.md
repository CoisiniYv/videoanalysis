# Midterm 8090 摄像头源控制与 Evidence 安全边界报告

日期：2026-06-26

## 结论

本次问题已完成闭环。

8090 端口上的摄像头新增、保存、启用、停用，已经收敛为 source-only 控制路径。该路径只同步摄像头运行配置并收敛对应 source adapter，不会重启 evidence 生成链路中的 worker，也不会重启 8090 管理端。

因此，动态新增摄像头或启停摄像头不应再因为牵连其他服务重启而中断不相关摄像头的 evidence 生成。

## 修复前问题

之前存在两个容易混淆的问题。

第一，前端曾经重复触发 source apply：

- 后端 `POST /api/v1/cameras/{camera_id}/enable|disable` 已经执行一次 source convergence；
- 8090 前端随后又额外调用一次 `POST /api/v1/cameras/runtime/sources/apply`；
- 结果是一次启停操作可能执行两轮 source 收敛，体感变慢。

该问题已通过提交 `2bbf0e7 fix: avoid duplicate camera source apply` 修复。前端现在直接使用摄像头接口返回的 `runtime_source_apply` 结果，不再重复 apply。

第二，`modules/savant_security/config/cameras.midterm.yml` 曾经滞后：

- 8090 数据库中 `Primary RTSP Camera` 已停用；
- `infra/generated/sources.generated.yml` 中 `primary_rtsp` 已停用；
- 实际容器 `video-analytics-midterm-source-adapter` 已退出；
- 但 `modules/savant_security/config/cameras.midterm.yml` 仍显示 `primary_rtsp enabled=true`。

根因是启停摄像头当时只写 `sources.generated.yml` 并收敛 source adapter，没有同步模块侧 `cameras.midterm.yml` 快照。

该问题已通过提交 `3c6be01 fix: sync module config during source convergence` 修复。8090 的摄像头新增、保存、启用、停用现在统一走 `sync_camera_runtime_config_and_sources()`：

- 从 8090 数据库导出当前摄像头配置；
- 收敛 source adapter；
- 同步写入 `modules/savant_security/config/cameras.midterm.yml`；
- 同步写入 `infra/generated/sources.generated.yml`；
- 保留当前 `runtime_epoch_id`；
- 不创建新 runtime epoch；
- 不重启 Savant；
- 不重启 evidence worker。

## 当前控制边界

### 安全的 source-only 操作

以下操作只影响摄像头源输入层：

- 新增摄像头；
- 保存摄像头基础配置；
- 启用摄像头；
- 停用摄像头；
- 手动应用摄像头源。

这些操作可能启动、停止或重建对应 source adapter，但不会重启以下服务：

- `video-analytics-midterm-event-worker`
- `video-analytics-midterm-face-worker`
- `video-analytics-midterm-clip-worker`
- `video-analytics-midterm-media-worker`
- `video-analytics-midterm-replay-service`
- `video-analytics-midterm-video-file-sink`
- `video-analytics-midterm-savant`
- `video-analytics-midterm-api`
- `video-analytics-midterm-evidence-viewer`

### 有中断风险的 full runtime 操作

以下操作仍然属于重操作，可能中断进行中的 evidence 任务：

- 算法配置的保存并应用；
- `POST /api/v1/cameras/runtime/apply`；
- `POST /api/v1/cameras/runtime/restart`；
- 8090 页面上的受控重启运行时；
- 单路链路重启。

这些操作会影响推理和 evidence 链路，可能停止或重启 `event-worker`、`clip-worker`、`media-worker`、Replay、Video File Sink、Savant 等容器。执行前应确认没有进行中的 evidence 任务。

## 2026-06-26 验证结果

8090 当前数据库状态：

```json
{"name":"Primary RTSP Camera","source_id":"primary_rtsp","enabled":false}
{"name":"lab","source_id":"source_00000000-0000-4000-8000-781078565686","enabled":true}
```

8090 source-only apply 返回：

```json
{
  "runtime_action": "source_converge",
  "module_config_synced": true,
  "runtime_epoch_id_preserved": "midterm-20260626T062143Z-0b1fb842",
  "lifecycle": [
    {
      "source_id": "primary_rtsp",
      "enabled": false,
      "action": "stopped",
      "actual_state": "exited"
    },
    {
      "source_id": "source_00000000-0000-4000-8000-781078565686",
      "enabled": true,
      "action": "kept",
      "actual_state": "running"
    }
  ]
}
```

配置文件状态已经对齐：

- `modules/savant_security/config/cameras.midterm.yml` 中 `primary_rtsp enabled=false`；
- `infra/generated/sources.generated.yml` 中 `primary_rtsp enabled=false`；
- `modules/savant_security/config/cameras.midterm.yml` 中 `lab enabled=true`；
- `infra/generated/sources.generated.yml` 中 `lab enabled=true`。

容器状态已经对齐：

- `video-analytics-midterm-source-adapter` 退出，对应停用的 `primary_rtsp`；
- `video-analytics-source-source_00000000-0000-4000-8000-781078565686` 运行，对应启用的 `lab`；
- `video-analytics-midterm-savant` 运行且健康；
- evidence worker 容器保持运行。

## 验证命令

```bash
curl --noproxy '*' -fsS 'http://127.0.0.1:8090/api/v1/cameras' \
  | jq -c '.data.cameras[] | {name,source_id,enabled}'

curl --noproxy '*' -fsS -X POST \
  'http://127.0.0.1:8090/api/v1/cameras/runtime/sources/apply' \
  | jq -c '.data | {runtime_action,module_config_synced,runtime_epoch_id_preserved,lifecycle:[.source_lifecycle[] | {source_id,enabled,action,actual_state}]}'

rg -n 'primary_rtsp|source_00000000-0000-4000-8000-781078565686|enabled:' \
  modules/savant_security/config/cameras.midterm.yml \
  infra/generated/sources.generated.yml

docker ps -a --format '{{.Names}}\t{{.Status}}' \
  | rg 'video-analytics-midterm-source-adapter|video-analytics-source-source_00000000-0000-4000-8000-781078565686|video-analytics-midterm-savant'
```

测试覆盖：

```bash
python -m pytest -q \
  harness/tests/test_camera_api_runtime_sources_direct.py \
  harness/tests/test_camera_runtime_apply_service.py

python -m compileall -q \
  services/api/app/routers/cameras.py \
  services/api/app/services/runtime_apply.py

git diff --check
```

## 受控重启证据保护

8090 的 full runtime apply/restart 已增加 evidence restart guard：

- 在 full runtime apply/restart 和单路链路 restart 前，检查是否存在 `pending`、`waiting_proof`、`queued`、`replay_job_created`、`replaying`、`materializing`、`finalizing` 等进行中的 evidence 任务；
- 若存在进行中任务，默认返回 409 并阻止重启，8090 页面展示阻断原因和前几条任务摘要；
- 已超过 `CAMERA_RUNTIME_EVIDENCE_GUARD_STALE_AFTER_S` 且 materialization/replay/annotation deadline 均已过期的历史卡死任务不阻断重启，但会以 `stale_tasks` 返回；
- 只有显式传入 `force=true` 时才允许带活跃 evidence 强制重启，返回结果会记录 `evidence_restart_guard.forced=true`；
- source-only 摄像头新增、保存、启用、停用不重启 evidence 链路，不受该 guard 阻断。

该保护是运行时操作安全增强，目的是避免受控重启打断 Replay、clip-worker、media-worker 正在生成的证据。
