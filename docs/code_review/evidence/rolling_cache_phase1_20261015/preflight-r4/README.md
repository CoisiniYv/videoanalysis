# 当前源码真实辅助容器只读预检（路线二收回后）

时间：2026-10-17 04:26:57–04:27:00 KST（UTC+09）；原始 UTC 时间保存在 `commands_and_exit_codes.json`。

源码复制到 uos157 的独立目录 `/tmp/pressure60-phase1-preflight-20261017`，只执行 `docker inspect`、`docker compose config`、`ffprobe` 和 `--preflight-only`。没有启动、停止、重建容器，没有连接数据库或 Redis，也没有写正式 checkout、缓存目录或镜像。输入使用既有只读样本，SHA256 保留在远端采集记录中。

| 测试 | 退出码 | `report.status` | 判定 |
|---|---:|---|---|
| 正常 storage override | 0 | `preflight_passed` | 两个缓存路径为 `/home/user/video-analytics-fast/...`，底层 NVMe，`rotational=false` |
| `--preflight-without-storage-override` | 2 | `preflight_failed` | `/data/video-analytics/media/rolling-cache*` 落到机械盘，`rotational=true` |

两次 stdout/stderr 均为空，均写出 `report.json`、原因和退出码。双路正式恢复配置也在正向预检中以 `operator-dual-runtime` profile 渲染，供配置回读验证。正式容器前后均为 30 个，比较范围包括 ID、镜像、稳定状态、重启次数、挂载、cpuset、Env 摘要和 Compose 标签，结果为完全一致。

完整产物：`commands_and_exit_codes.json`、`nvme.report.json`、`rotational.report.json`、`readonly_validation.sanitized.json`、`snapshot_comparison.json`、`source_sha256.json` 及原始空输出文件。
