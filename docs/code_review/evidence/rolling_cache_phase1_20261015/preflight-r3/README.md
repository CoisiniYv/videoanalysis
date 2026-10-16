# 当前源码真实辅助容器只读预检

时间：2026-10-17 00:23:26.545690–00:23:28.662632 KST；原始 UTC 保存在 JSON。

隔离目录 `/tmp/pressure60-phase1-preflight-20261016`，宿主 Python 3.7.9；当前工作树源码与 Compose 输入复制到该目录，运行只读 `docker inspect`、`docker compose config`、`ffprobe` 和 storage gate。测试输入是仓库 `testVideo/test.mp4` 的只读副本，没有创建容器、重建服务、写数据库或写正式目录。

| 测试 | 实际退出码 | report.status | 判盘结果 |
| --- | ---: | --- | --- |
| 正常 storage override | 0 | preflight_passed | 两个缓存路径均 `/home/user/video-analytics-fast/...`，dm-2 → nvme0n1p3 → nvme0n1，rotational=false |
| `--preflight-without-storage-override` | 2 | preflight_failed | `/data/video-analytics/media/rolling-cache` → sda1 → sda，rotational=true |

两次 stdout/stderr 均为空；两次均写出 `report.json`，含 `reason` 与 `exit_code`。`commands_and_exit_codes.json`、两份 report、原始输出和源码哈希保留本轮证据。

远端正式容器前后均为 **30 个且完全一致**（ID、镜像、稳定状态字段、RestartCount、挂载、cpuset、Env 摘要和 Compose 标签）；比较范围见 `snapshot_comparison.json`，完整脱敏快照见 `readonly_validation.sanitized.json`。

本轮只读验证没有启动压测、没有进入阶段 2。
