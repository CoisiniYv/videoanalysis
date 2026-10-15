# 修复后只读预检：迭代 1

时间：2026-10-15 22:33:23.523869–22:33:25.438974 KST。原始 UTC 同存于 JSON。

独立目录 `/tmp/pressure60-phase1-preflight-20261015-r1`，宿主 Python 3.7.9。
复用上一轮输入 `/tmp/pressure60-phase1-preflight-20261015/preflight-input.mp4`，没有重新录流或创建容器。
采集器为上级目录的 `validate_readonly_remote.py`。

| 测试 | 退出码 | report.status | 判盘结果 |
| --- | ---: | --- | --- |
| 正常 storage override | 0 | preflight_passed | 两个缓存路径均 dm-2 → nvme0n1p3 → nvme0n1，rotational=false |
| --preflight-without-storage-override | 2 | preflight_failed | /data/video-analytics/media/rolling-cache → sda1 → sda，rotational=true |

来源：`commands_and_exit_codes.json`、`nvme.report.json`、`rotational.report.json`。
两次 stdout/stderr 均为空，保留对应零字节文件；失败原因写于机械盘报告的 `reason` 和 `rolling_cache_storage.errors`。
两次 Compose config 命令均启用 `dual-4090-two-source` 和 `rolling-cache-dual`。

`snapshot_comparison.json` 确认前后 30 个正式容器相同：ID、Image、稳定 State 字段、RestartCount、挂载、cpuset、Env SHA256、Compose 标签。
Mounts 按 Destination/Source/Type 排序；未比较会自然滚动的 health-log 样本。
完整前后快照与命令在 `readonly_validation.sanitized.json`。

报告只脱敏凭据字段和 URI userinfo，stdout/stderr 保持原样；原文件留在服务器独立目录及本机 `/tmp/pressure60-readonly-validation-r1.json`。
`provenance.json` 记录原文件 SHA256；`source_sha256.json` 标识真实执行源码。

这是最新授权后的第一次修复迭代，正反两项均符合预期，未使用第二次迭代额度。
只执行 Compose config、ffprobe、sysfs/文件系统读取及 docker ps/inspect，未修改正式 checkout、正式服务或数据。
随后全量 harness 发现 6 个新增失败而停止；本次预检通过不表示阶段 1 已验收通过。
