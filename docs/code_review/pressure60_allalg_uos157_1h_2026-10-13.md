# 10-13 历史压测只读复盘（阶段 0 / T0.2）

## 1. 结论

**初次核查结论：T0.3 决策门证据不完整。** 历史 JSON 没有保存任务的 `materialization_retry_reason` 或 `materialization_audit`，当时按 T0.3 第⑤类停止。用户随后授权只读历史日志关联，并明确该补充不阻塞另行授权的阶段 1。新增结果见第 8 节：已恢复 1,641 个过期事件在快照之前最后一次观测到的已持久化重试原文，均映射为窗口边缘缺口；它们不是恢复出的数据库 lifecycle audit。

本报告只复盘历史文件，不修服务、不复测、不连数据库、不创建容器。当前盘位、业务影响和方案 A 另见同目录阶段 0 报告；不能用当前容器日志或环境反证 10-13 的环境。

## 2. 做了什么与证据范围

- 分支：`CZray/pressure60-allalg-review-20261014`；任务书提交：`68400c8f86940c972296c2ae300ad140c3d2e746`。本文件为阶段 0 文档，提交 SHA 由阶段 0 提交记录补充。
- 远端：`uos157`，`192.168.1.100`。仅读取以下两个 `$ART`，没有读取正式 checkout、当前容器、数据库或 `/var/log/sysstat`。
- 10-13：`/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2`。
- 10-12：`/data/video-analytics/artifacts/pressure60_remux_refill_t4_4fps_1h_20261011_r2`。
- 只读采集完成时间范围：**2026-10-15 18:56:07.609–19:06:38.335 KST**；原始 UTC：`2026-10-15T09:56:07.609Z` 至 `2026-10-15T10:06:38.335Z`。
- 命令形式：`node /tmp/savant-readonly-capture.mjs uos157 <本地只读Python脚本> <本地capture.json>`，通过 SSH stdin 运行 Python；远端脚本只使用文件读取、JSON 解析和日志统计。脚本、命令回执及摘要都在 [T0.2 证据目录](evidence/rolling_cache_phase0_20261015/t02/README.md)。

原始 `report.json` 的 SHA256：10-13 `7287175334e90507666883efc4948a95169fc5b4ac1dfe771b4289b2d021c625`；10-12 `0506feb96e58dd6dd8aed44c899fcf50e39b3d55ceb4aeccad6ab5d156aecb1d`。以 [机器摘要](evidence/rolling_cache_phase0_20261015/t02/t02_findings.json) 中 `runs.<日期>.report_sha256` 为权威字段；原件留在远端，提取件中的 URI 凭据已脱敏。

初稿曾手写一个无机器证据来源的错误 10-13 哈希，已更正并保留于 [更正记录](evidence/rolling_cache_phase0_20261015/t02/doc_hash_correction.json)。两次已采集回执中的实际哈希一致，未发现 ART 在两次采集间变化的证据。

正式窗口来源为 `report.pressure_sampling_window`：

| 轮次 | KST | 原始 UTC |
|---|---|---|
| 10-13 | 2026-10-13 06:34:18.482326 → 07:34:49.666642 | `2026-10-12T21:34:18.482326+00:00` → `2026-10-12T22:34:49.666642+00:00` |
| 10-12 | 2026-10-11 23:46:20.762412 → 2026-10-12 00:46:32.035005 | `2026-10-11T14:46:20.762412+00:00` → `2026-10-11T15:46:32.035005+00:00` |

日志无时区后缀。本次按 UTC 解读，其时间与 epoch-ns 的 `last_pts` 相差约秒级；这是时钟域交叉核验，不是取得了历史 `TZ` 环境快照。30 秒桶同时保留 UTC 和 KST。

## 3. 关键数字

“上一轮”没有新增执行轮次，填未执行。所有本轮数字均为历史 10-13 复盘，不能误读为新的服务压测。

| 指标 | 上一轮 | 本次核实的 10-13 | 10-12 参考 | 来源与状态 |
|---|---|---:|---:|---|
| 任务总数 | 未执行 | 4,182 | 4,296 | `report.db_summary_before_cleanup.tasks`；已确认 |
| 过期任务 | 未执行 | 2,876 | 0 | `task_statuses[].materialization_status/count`；已确认 |
| 图片就绪 | 未执行 | 1,305 | 1,391 | `watchlist_image_ready_bundles`；已确认 |
| 视频可播放 | 未执行 | 0 | 2,905 | `behavior_video_playable_bundles`；已确认，口径为报告内清理前数据库快照 |
| 非成功明细中的从未尝试 | 未执行 | 1,235 | 0 | `non_materialized_task_details[].materialization_attempt_count==0`；已确认 |
| 缓存快照切片数 | 未执行 | 12,015 | 41,161 | `rolling_cache_segment_visibility.segments_measured`；已确认，不代表整小时总产出 |
| 缓存快照可见延迟 P50 / P95 | 未执行 | 1,187.410 / 4,195.728 秒 | 0.642 / 1.344 秒 | `metadata_visible_lag_s.{p50,p95}`；已确认 |
| 正式发布窗口队列驻留 P50（A / B） | 未执行 | 103.649 / 101.188 秒 | 0.024936 / 0.026972 秒 | sink 日志重算；已确认 |
| 剪辑 `remux_ms` P50 / 样本数 | 未执行 | 271 ms / 6 | 676 ms / 3,025 | `diagnostics.log_summary.media_worker.media_remux_ms`；已确认 |
| 分析队列瞬时峰值，两分片合计 | 未执行 | 16,384 | 6,980 | `diagnostics.sample_summary.max_forwarder_queue_depth_instantaneous`；已确认 |

因此任务书待核实数字中：2,876 / 1,305 / 0 / 12,015 / 1,187 秒均有对应字段；“约 102 秒”对应正式发布窗口内的**队列驻留中位数**，不是 `publish_total_ms`；“剪辑中位 322 ms”与指定 ART 字段不符，应改为 271 ms，且样本只有 6 条。

## 4. 缓存位置、保留期和 compose

### 缓存位置证据资格

10-13 `rolling_cache_segment_visibility.root=/data/video-analytics/media/rolling-cache`、`segments_measured=12015`，`run_config.pressure_rolling_cache_host_root=""`。10-12 对应为 `/home/user/video-analytics-fast/rolling-cache`、`41161`、空字符串。

但是两轮 `report.json` 均未记录历史宿主 `PRESSURE_ROLLING_CACHE_ROOT_HOST`，全文该 token 次数为 0。**未记录不等于当时未设置。** 任务书要求的第三项无法确认，因此这两个 `root` 不能单独取得三条件全部满足的证据资格。快照中的 `sampled_metadata_paths` 与 `root` 一致，支持盘位推断；物理盘型由当前 T0.1 只读盘链另证，不能倒推历史挂载没有变过。

### 三处保留期

| 来源字段 | 10-13 | 10-12 | 状态 |
|---|---:|---:|---|
| `run_config.pressure_rolling_cache_retention_s` | 0（使用默认计算） | 600 | 已确认 |
| `report.rolling_cache_sinks_pressure.rolling_cache_retention_seconds` | 4,320 | 600 | 已确认 |
| `observed_env.rolling-cache-sink-a.ROLLING_CACHE_RETENTION_SECONDS` | 4,320 | 600 | 已确认 |
| `observed_env.rolling-cache-sink-b.ROLLING_CACHE_RETENTION_SECONDS` | 4,320 | 600 | 已确认 |

10-13 的 `3600+600+120=4320` 与报告实际值一致，不能写成保留 600 秒。10-12 的 41,161 同时见于 `rolling_cache_segment_visibility.segments_measured` 和实际键 **`rolling_cache_full_rate_gate.segments_measured`**；指定报告没有 `harness_rolling_cache_full_rate_gate` 这个顶层键。

10-12 快照的可见时间桶从 **2026-10-12 00:06:00 至 00:46:30 KST**（原始 UTC `2026-10-11T15:06:00+00:00` 至 `15:46:30+00:00`），跨度至少 40 分钟，与严格的 600 秒留存窗口不一致。三处配置都确为 600，不能据切片数把配置改称 4,320。此次许可内证据不足以确定维护进程、读 pin 或保留行为的具体原因；该矛盾保留为未核实。

10-13 `compose_file=infra/docker-compose.midterm.yml`，10-12 为 `/home/user/video-analytics-pressure-remux-0523808/infra/docker-compose.pressure-main.yml`，后者明确是独立测试 compose。两轮 ART 顶层清单均无 `compose.rolling-cache-pressure-storage.override.yml`，支持没有使用 harness tmpfs 模式的推断。没有保存完整 compose 命令、历史宿主环境和当时基础 compose 文件内容，不能确认当时完整叠加链或手工卷改动。

## 5. 发布耗时与随时间变化

下表均为**全捕获日志**的 P50 / P95，单位 ms；来源 `report.diagnostics.log_summary.rolling_cache_sink_a|b.rolling_cache_<字段>`。它们包括预热和收尾，不能与上一表的正式窗口分位数混用。

| 字段 | 10-13 A | 10-13 B | 10-12 A | 10-12 B |
|---|---:|---:|---:|---:|
| `publication_queue_residence_ms` | 64,493.205 / 331,638.526 | 87,224.386 / 333,368.851 | 24.955 / 294.683 | 26.962 / 321.433 |
| `publication_capacity_wait_ms` | 209.784 / 2,600.083 | 262.142 / 2,868.094 | 0.010 / 0.015 | 0.010 / 0.015 |
| `publish_total_ms` | 1,241.710 / 6,350.543 | 2,157.268 / 7,387.365 | 17.683 / 38.674 | 17.659 / 38.903 |
| `publish_metadata_fsync_ms` | 303.499 / 1,367.434 | 483.106 / 1,462.823 | 3.109 / 11.425 | 3.090 / 11.455 |
| `publish_manifest_fsync_ms` | 334.206 / 1,443.221 | 510.187 / 1,595.597 | 2.862 / 7.778 | 2.875 / 7.754 |
| `publish_staging_dir_fsync_ms` | 167.066 / 979.461 | 319.880 / 1,093.869 | 0.131 / 0.689 | 0.129 / 0.679 |
| `publish_parent_dir_fsync_ms` | 287.801 / 1,392.214 | 481.440 / 1,468.080 | 1.597 / 6.245 | 1.603 / 6.131 |

原报告 A/B 指标样本数为 8,861 / 6,210。本次要求行首必须是完整时间戳后，A 的真实发布日志为 8,860 条；另 1 条只是异常文本含 `segment published`，不纳入时序。B 为 6,210 条。

按日志时间减 `last_pts/1e9` 计算可见延迟，按日志时间每 30 秒分桶，完整结果在 [10-13 日志统计](evidence/rolling_cache_phase0_20261015/t02/1013_log_analysis.json) 的 `logs.a|b.buckets_30s`。正式窗口 A/B 分别有 4,419 / 4,389 条发布记录。

| 30 秒桶 / 指标 | 10-13 A | 10-13 B | 来源 |
|---|---:|---:|---|
| 06:34:00 KST 桶可见延迟 P50 | 50.746 秒 | 50.489 秒 | `formal_first_bucket.metrics.log_minus_last_pts_s.p50` |
| 07:34:30 KST 桶可见延迟 P50 | 3,156.132 秒 | 3,158.499 秒 | `formal_last_bucket.metrics.log_minus_last_pts_s.p50` |
| 正式窗口桶 P50 线性拟合斜率 | 0.842624 秒/秒 | 0.843051 秒/秒 | `formal_lag_p50_regression.slope_seconds_per_second` |
| 线性拟合 R² | 0.998871 | 0.998857 | `formal_lag_p50_regression.r_squared` |

桶边界含正式窗口外的首尾少量记录，逐行正式窗口分位数另存 `formal_by_publication_time`。**已确认**是接近线性的持续增长，不是单一阶跃；**推断**发布供给持续低于输入需求。现有证据不能排除其他资源竞争，也不能代替 task-level `last_reason`。

`log_time-last_pts` 本身已经包含队列驻留。机器证据另外保留任务书字面相加量 `lag_plus_queue_residence_s`，但它会重复计算队列部分，不用它声称真实可见延迟。

10-13 的 metadata 快照只覆盖尚存文件，其可见时间桶为 **06:31:00–08:13:30 KST**（UTC `2026-10-12T21:31:00+00:00` 至 `23:13:30+00:00`）。这不是完整小时的连续观测窗口，更不是 12,015 个切片的产出率；它仅用于交叉校验。

## 6. 过期、剪辑与分析配置

### 过期任务

两轮 `run_config.discard_pressure_results=false`。10-13 `pressure_runtime_cleanup_after_restore` 记录 `deleted_events=0`、`deleted_cameras=0`、`removed_evidence_paths=0`、`visual_results_retained=true`；before/after 数据库摘要的任务总数均为 4,182。历史报告还记录 `removed_rolling_cache.source_dirs=127`，这是**当时的清理记录**，本次没有删目录。

`non_materialized_task_details.json` 全量 2,877 条，逐条键集合检查没有 `task_type`、`materialization_retry_reason`、`materialization_audit` 或任何 retry/audit 字段。能取得的是 `event_type`、终态、过期原因和尝试次数，不能把 `event_type` 冒充 `task_type`：

| `event_type` | 终态 | 从未尝试 | 数量 | 来源 |
|---|---|---|---:|---|
| intrusion | expired / business_deadline_expired | 否 | 1,641 | 明细全量分组 |
| intrusion | expired / business_deadline_expired | 是 | 1,225 | 明细全量分组 |
| watchlist_hit | expired / business_deadline_expired | 是 | 10 | 明细全量分组 |
| watchlist_hit | failed | 否 | 1 | 明细全量分组 |

全部过期类型中的从未尝试比例为 `1235/2876=42.94%`，该分母包含图片和视频，**不能用来判断 T0.3 第③类要求的视频任务子集是否多数从未尝试**。明细缺少 `task_type`，该视频子集仍无法取得。终态 `business_deadline_expired` 也不是最后一次重试原因，无法据此分进 T0.3 第①或②类。每类 retry detail 抽样 10 条、按 `task_type`/ready_at 的成功与过期 5 分钟分桶都**无法从已取得的允许范围产物完成**。只有非成功明细也不足以还原全部成功任务的 ready_at。T0.3 第⑤类因此停止；未拿当前数据库补齐当轮原因。

### 剪辑样本

按完整正则 `media_event_finalized .* remux_ms=[0-9]+` 取数，不统计 `remux_ms=None`。10-13 仅 6 条，全部发生在 **2026-10-13 07:56:09.405–07:56:16.590 KST**（UTC `2026-10-12T22:56:09.405000+00:00` 至 `22:56:16.590000+00:00`），均在正式采样窗口结束后。P50=271 ms、P95=850.5 ms、最大 1,035 ms；正式完成窗口样本数为 0。来源 `1013_log_analysis.json.remux`，包含全部 6 条事件 ID、行号和时间。

报告统计的视频可播放为 0；日志在正式采样窗口外有 6 条 `finalized`，不等价于视频成功或可播放。现有证据未确定这些日志与该数据库快照的采集先后，也缺少逐条任务关联，不能择一覆盖另一项或据此推算正式窗口的视频成功率。

### 分析、模型与算法

- **已确认：** 直连 A/B 读取对象是 `replay-raw-fanout-a/-b`，ART `runtime_000.json.forwarder.shards` 的 URL 是 18185/18186，不能拿停用的 analysis-forwarder 默认环境作证。
- **已确认：** 10-13 `compose.dual-shard-same-gpu.override.yml` 的 A/B `FORWARDER_QUEUE_MAX_SIZE='8192'`，但没有 `FORWARDER_MAX_QUEUE_AGE_MS` 或 `FORWARDER_STRICT_FPS_BUDGET`。10-12 同一文件显式写了 `'30000'` / `'true'` / `'8192'`。缺失 override 项不能等价解释为最终生效值为 0/false；基础 compose、宿主环境和镜像默认值未有历史完整快照。
- **已确认：** 10-13 合计队列瞬时峰值为 16,384；单分片容量各 8,192。在只有两个分片且两者都遵守容量上限的前提下，合计峰值支持二者同时满的推断。本次最初单分片扫描未正确读取 `shard_id`，其 `unknown:8192` 只作初步聚合，不当成独立的 A/B 各自峰值证明。
- **未核实：** 保存的 98 个 10-13 runtime JSON、105 个 10-12 runtime JSON 没有 stale/resync/head-age 字段；这可能是采集解析器遗漏，不能据字段缺失判镜像不支持。没有取得历史镜像代码哈希或原始 Prometheus 指标，禁止用当前镜像/`docker run` 替代历史证据。因此“队龄门未生效导致顶满”保持推断。
- **已确认：** Savant A/B 的 10-13 override 中 `FACE_INFER_INTERVAL=4`、`FACE_EMBEDDING_INFER_INTERVAL=4`；10-12 均为 3。两轮 `adaface_roi_redis=true`，override 开 `FACE_ROI_EXPORT_ENABLED` 并设置 `ADAFACE_INPUT_OBJECT=disabled.face`，embedding 间隔不适用于 AdaFace ROI worker 的独立处理路径。没有将 override 声称为历史容器 Env 回读。
- **已确认：** 10-13 `db_summary_before_cleanup.event_types` 只有 intrusion=10,338、watchlist_hit=1,685。`savant_ablation_manifest.selected_elements` 含 behavior_rules，仅说明元素存在；不能证明 fall/crowd/chasing 规则已开启。ART 未取得压测 `camera_rules` 或开启操作记录，因此“全算法”运行资格未核实，不能仅凭 run-id 的 allalg 命名确认。
- **已确认：** publication workers 两轮分别为 4 / 1（`rolling_cache_sinks_pressure.observed_env`）；CPU 隔离分别为 none / t4-16cpu-evidence，MPS false / true，batch timeout 40,000 / 10,000（各自 `run_config`）。两轮不是单变量对照。

## 7. 已确认、推断、未核实与下一步

**已确认：** 过期/图片/视频快照、三来源 retention、发布及 fsync 分布、正式发布窗口约 102 秒驻留、近线性可见延迟增长、6 条窗口外 remux 样本、历史 task retry/audit 字段缺失。每个数均在前述 JSON 路径或逐行日志统计中保存。

**推断：** 10-13 写入 `/data`、10-12 写入 NVMe 目录；发布处理供给不足；10-13 两分片同时顶满。历史宿主环境、完整 compose 和镜像代码未取到，盘位与队龄机制不能提升为完整已确认。

**未核实/无法取得：** 任务归一化最后原因与 detail、完整 task_type/ready_at 成功过期分桶、历史 compose 内容、`PRESSURE_ROLLING_CACHE_ROOT_HOST`、历史镜像哈希/原始 stale-resync 指标、10-12 超过 600 秒的快照留存原因、全算法启用记录。磁盘历史 **ART 中无**；本次授权仅限 `$ART`，未读 `/var/log/sysstat/sa13`，不能声称服务器没有历史 sar。

初次核查据此停在阶段 0。用户后续已授权第 8 节的历史日志补充，且明确不阻塞另行授权的阶段 1；本报告不把日志补充当作阶段 2 或方案 A 的执行批准。所有服务、镜像、数据和 main 保持本次只读约束。

## 8. 后续授权：按 event_id 关联历史重试日志

**结论：日志关联已完成，数据库审计缺失没有被冒称补齐。** 在用户后续授权下，仅重新读取 10-13 `$ART` 的 `media_worker_logs_since_start.txt`、`event_worker_logs_since_start.txt` 和已经获准读取的 `non_materialized_task_details.json`；没有查询历史库、活服务或其他目录。新增证据独立存入 [日志关联目录](evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/README.md)，旧证据及旧 `MANIFEST.sha256` 不变。

关联采集完成时间：**2026-10-15 20:24:50.994 KST**；原始 UTC `2026-10-15T11:24:50.994Z`。`media_worker` 日志与非成功明细的 SHA256 均与初次采集一致，见 `summary.inputs_same_sha256_as_first_t02_capture=true`，没有观察到这两份 ART 在两次采集间变化。

### 关联口径

以 ART 非成功明细中的 2,877 个 `event_id` 为 cohort，不用整个压测前缀上的其他历史行。逐条解析 `rolling_cache_coverage_deferred event_id=... reason=... retry_after_s=... persisted=... generation=...`，每个事件只选其 ART `task_updated_at` **之前最后一次 `persisted=True`** 的重试日志。时间继续按容器日志的 UTC 解释，并同时保存 KST。结果是“日志中最后一次观测到的已持久化重试”，不能等同于查询数据库 `lifecycle_v2_retry` 的值。

归一化直接运行仓库基线 `98d388c:libs/evidence_lifecycle/contract.py` 的 `normalize_reason_code`。该原件及其 SHA256 随证据保存；这说明本次如何映射异常原文，不用于证明历史镜像恰好装了该代码版本。

### 已确认的分类

| 指标 | 数量 | 新证据来源 |
|---|---:|---|
| 解析的延期日志 | 32,687 | `summary.log_counts.parsed_lines` |
| 其中 `persisted=True` | 32,687 | `summary.log_counts.persisted_true` |
| 属于 ART cohort / 不属于 cohort 的日志 | 32,486 / 201 | `cohort_lines` / `outside_cohort_lines` |
| cohort 重复 event_id / 无法解析的延期日志 | 0 / 0 | `duplicate_event_id_count` / `unparsed_deferred_line_count` |
| 可关联快照前最后已持久化重试的过期事件 | 1,641 | `summary.groups` 中 `last_observed_normalized_reason=coverage_not_complete` |
| 最后原文只有 `post_gap_ns=` | 1,638 | `subtype_counts.missing_post_window` |
| 最后原文同时有 `pre_gap_ns=`、`post_gap_ns=` | 3 | `subtype_counts.missing_pre_and_post_window` |
| ART 尝试数为 0 且没有关联重试日志 | 1,235 | `subtype_counts.no_retry_log_and_artifact_attempt_count_zero` |
| 只有快照之后的重试、未纳入最后原因 | 1 | `excluded_post_snapshot_event_rows`，该行是图片失败 |

1,641 条最后原文均为 `rolling_cache_requested_window_not_fully_covered:...`，由基线契约映射为 `coverage_not_complete`，细节均是窗口前后边缘缺口。**已确认**：已关联的最后原因中没有 `internal_gap_ns=`、`requested_window_internal_gap`、`rolling_cache_output_duration_short`、`rolling_cache_output_frame_rate_low` 或 `capacity_unavailable`。这只描述此次 cohort 的已关联最后原因，不能推成全运行期没有这些问题。

ART 中还明确保存了 `event_media.clip_required`。2,876 个过期事件中，有 2,866 个 `clip_required=true`，其余 10 个是图片要求；在这 2,866 个明确要求视频的事件里，1,641 个（**57.26%**）有上述窗口边缘缺口日志，1,225 个（**42.74%**）的 ART `attempt_count=0` 且无重试日志。来源为 `summary.clip_required_expired_*`。这是按明确的事件媒体要求分组，仍不是恢复出的数据库 `task_type`。不能再用第 6 节所有过期类型的 `1235/2876` 代替这个视频要求子集。

另 1 个失败事件是 `clip_required=false`、`snapshot_required=true`，ART `failure_reason=face_image_frame_not_found`；其唯一关联重试出现在 ART `task_updated_at` 之后，已排除。它不是 T0.3 第②类的视频专属故障证据。

### 样本、分桶与边界

[reason_samples.json](evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/reason_samples.json) 保存归一化 `coverage_not_complete` 的 10 个不同事件原文，以及细分 `post_gap` 的 10 个、`pre+post` 的全部 3 个样本；每条都有 event_id、task_id、源日志行号、UTC/KST、原文、persisted 和 generation。双边缺口总共只有 3 个，不虚构第 4–10 个样本。

[correlated_events.json](evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/correlated_events.json) 保存全部 2,877 个事件的关联和缺失状态；[summary.json](evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/summary.json) 的 `ready_at_5m_expired_only` 保存按 ART ready_at 的 13 个五分钟桶。ART cohort 只有非成功任务，**成功任务的 ready_at 分桶仍无法取得**；没有用日志完成时刻替代 ready_at。

**推断：** 在已关联的已尝试视频要求事件中，发布素材尚未覆盖要求窗口是共同的重试原因，与前述发布可见延迟持续增长相符。该结论不解释 1,225 个从未尝试的视频要求事件为何没有获得尝试，也不证明完整物理盘链或队龄机制。数据库原始最后 audit、真实 task_type、成功任务 ready_at 与历史宿主存储环境仍未取得。

本次用户已明确允许该历史补充与阶段 1 并行，因此本补充不增加阶段 1 的等待条件；方案 A、正式服务操作和阶段 2 的维护窗口仍分别需要用户批准。
