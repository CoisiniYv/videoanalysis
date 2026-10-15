# T0.2 历史 ART 只读证据

只读采集来自 uos157（192.168.1.100）上的任务书两个 ART 根目录；没有当前容器、数据库或 sysstat 读取。人工结论见上级目录 `pressure60_allalg_uos157_1h_2026-10-13.md`。

- `inventory_art.json`：远端两个 ART 的顶层文件名、大小、mtime。
- `t02_findings.json`：用于报告的精简字段、原始 report SHA256、证据资格和停止门。
- `1013_log_analysis.json`、`1012_log_analysis.json`：源日志 SHA256、样本范围、完整 30 秒桶、正式发布窗口分布、回归、剪辑样本以及非成功明细字段检查。
- `1013_run_config.json`、`1012_run_config.json` 和带日期的 `*.override.yml`：历史配置提取件；URI 凭据已脱敏。
- `*_report_schema.json`、`*_selected_report.json`、`*_log_probe.json`、`1013_more_art.json`：有来源键路径的中间提取件。
- `*.capture.json`：SSH 只读采集回执，内含远端脚本、stdout、stderr、状态和 UTC 采集时间。stdout 中 URI 凭据同样脱敏。
- `inventory_art.py`、`extract_reports.py`、`probe_log_formats.py`、`probe_more_art.py`、`analyze_art.py`：远端只读采集脚本。不独立安装依赖；兼容宿主 Python 3.7。
- `finalize_evidence.mjs`：本地摘要和脱敏脚本；无网络或远端服务操作。
- `redactions.json`：脱敏文件列表、脱敏前后哈希。需保留的本地未脱敏副本在仓库外 `/tmp/savant-t02-raw-20261015`，文件权限 0600，目录 0700；原始远端 ART 未改动。

采集命令示例（均已完成，不应当作重跑服务命令）：

```sh
node /tmp/savant-readonly-capture.mjs uos157 \
  docs/code_review/evidence/rolling_cache_phase0_20261015/t02/analyze_art.py \
  docs/code_review/evidence/rolling_cache_phase0_20261015/t02/analyze_art.capture.json
node docs/code_review/evidence/rolling_cache_phase0_20261015/t02/finalize_evidence.mjs
```

`analyze_art.py` 初次遇到异常文本中包含 `segment published` 的无时间戳行，随后增加行首时间戳校验后成功；最终回执 status=0。10-13 A 比旧报告少 1 个有效发布样本的原因已在主报告注明。

分位数采用 `(n-1)*p` 线性插值。`log_minus_last_pts_s` 不是独立的排队时间；`lag_plus_queue_residence_s` 只为保留任务书字面要求，不用于物理延迟结论。`shard_queue_max_initial_parse.unknown` 是初步扫描误把 `shard_id` 读成其他键的聚合字段，不作为逐分片证据。
