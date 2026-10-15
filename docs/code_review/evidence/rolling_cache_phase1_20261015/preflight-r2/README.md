# 最新授权后的只读交审验证

时间：2026-10-15 22:47:25.613763–22:47:27.545463 KST。原始 UTC 保存在 JSON。
这是最新“允许修复六项并重验”授权后的第 1 轮交审重验；目录 r2 延续历史预检目录编号。

独立目录 `/tmp/pressure60-phase1-preflight-20261015-r2`，宿主 Python 3.7.9。
复用既有 `/tmp/pressure60-phase1-preflight-20261015/preflight-input.mp4`，没有创建容器或重新录流。
采集器为上级目录的 `validate_readonly_remote.py`，实际源码 SHA256 见 `source_sha256.json`。

| 测试 | 实际退出码 | report.status | 判盘结果 |
| --- | ---: | --- | --- |
| 正常 storage override | 0 | preflight_passed | 两个缓存路径均 dm-2 → nvme0n1p3 → nvme0n1，rotational=false |
| --preflight-without-storage-override | 2 | preflight_failed | /data/video-analytics/media/rolling-cache → sda1 → sda，rotational=true |

来源：`commands_and_exit_codes.json`、`nvme.report.json`、`rotational.report.json`。
两次 stdout/stderr 均为空，零字节原文件保留；成功与失败均有 `reason` 和 `exit_code`，失败另有 `rolling_cache_storage.errors`。
两次配置解析都启用 `dual-4090-two-source` 和 `rolling-cache-dual`，仅反向测试省略 storage override。

`snapshot_comparison.json` 确认前后 **30 个正式容器一致**：ID、Image、稳定 State 字段、RestartCount、挂载、cpuset、Env SHA256、Compose 标签。
Mounts 按 Destination/Source/Type 排序；不比较自然滚动的 health-log 样本。
完整前后快照在 `readonly_validation.sanitized.json`。

仅脱敏凭据字段和 URI userinfo，stdout/stderr 原样保留；原 JSON 留在服务器独立目录及本机 `/tmp/pressure60-readonly-validation-r2.json`。
`provenance.json` 保存原文件 SHA256。没有修改正式 checkout、正式服务、已有缓存或镜像。
本轮未启动压测、未进入阶段 2。
