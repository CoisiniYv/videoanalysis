# 滚动缓存修复前业务影响只读核查（2026-10-15）

## 1. 结论

**当前窗口未发现正在产生的业务取证负载；这不能证明 `/data` 挂载正确或取证能力正常。** 数据库当前所有摄像头均禁用，最近 24 小时没有 evidence_tasks；两个 sink 最近 30 分钟查询均未找到 `segment published` 样本，所以发布延迟 P50/P95 和增长趋势无法取得。正式服务是否修复、何时恢复运行仍由用户决定。

本报告只评估当前业务影响。T0.1 的盘位与方案 A 另见阶段 0 主报告；T0.2/T0.3 只以 `$ART` 当轮资料作主判断，不以本报告的现存历史库行替代。

## 2. 做了什么

- 基线：`68400c8f86940c972296c2ae300ad140c3d2e746`；独立阶段 0 worktree。此文档尚未提交，最终文档 commit 由阶段 0 汇总提交记录。
- 目标仅为 SSH 别名 `uos157`，主机回读 `localhost.localdomain`；没有连接 `user157`。
- 从现存 `video-analytics-midterm-postgres` 读取表结构、摄像头启用状态及任务汇总。每条 SQL 都在 `BEGIN READ ONLY` 中执行，设置事务内 `statement_timeout='30s'`、`timezone='UTC'`，最后 `ROLLBACK`；执行结果 `returncode=0`。未执行测试、写 SQL、服务管理或数据清理命令。
- PostgreSQL 用户名/库名由容器已有配置在内存中取得；命令证据将这两个参数替换为来源占位符，没有保存或打印密码。
- 读取两个 sink 的 `docker logs --timestamps --since <UTC起点> --until <UTC终点>`，匹配完整 `segment published` 日志行；分位数采用线性插值，空样本结果为 `null`，不按 0 毫秒处理。
- 读取 `$ART/run_config.json`、`$ART/report.json` 的清理和任务汇总字段，保存原始只读返回与派生结构化证据。

### 时间口径

| 项目 | KST（UTC+09:00） | 原始 UTC |
|---|---|---|
| 本次采集开始 | 2026-10-15 19:00:19.928700 | `2026-10-15T10:00:19.928700+00:00` |
| 本次采集结束 | 2026-10-15 19:00:22.339382 | `2026-10-15T10:00:22.339382+00:00` |
| 数据库事务时钟 | 2026-10-15 19:00:20.017240 | `2026-10-15T10:00:20.01724+00:00` |
| 最近 24 小时查询窗 | 10-14 19:00:19.928700 至 10-15 19:00:19.928700 | `[2026-10-14T10:00:19.928700+00:00, 2026-10-15T10:00:19.928700+00:00)` |
| sink 最近 30 分钟查询窗 | 10-15 18:30:19.928700 至 19:00:19.928700 | `2026-10-15T09:30:19.928700+00:00` 至 `2026-10-15T10:00:19.928700+00:00` |

来源：`current-business.json` 顶层时间字段及 `queries.db_clock.rows[0]`。多个查询为分别的只读事务，采集时差如上，不声称为同一数据库快照。

## 3. 关键数字

| 指标 | 上一轮 | 本轮 | 10-12 | 本轮来源 |
|---|---|---|---|---|
| 摄像头总数 / 已启用数 | 未采 | **61 / 0** | 无同口径当前业务快照 | `queries.camera_counts.rows[0].all_cameras/all_enabled` |
| 非压测摄像头总数 / 已启用数 | 未采 | **1 / 0** | 无同口径当前业务快照 | `queries.camera_counts.rows[0].nonpressure_cameras/enabled_nonpressure_cameras` |
| 最近 24h 全表 evidence_tasks | 未采 | **0** | 无同口径当前业务快照 | `queries.current_evidence_population.rows[0].all_tasks_24h` |
| 最近 24h 非压测 evidence_tasks | 未采 | **0** | 无同口径当前业务快照 | `queries.current_evidence_population.rows[0].joined_nonpressure_tasks` |
| 最近 24h camera 关联不到的任务 | 未采 | **0** | 无同口径当前业务快照 | `queries.current_evidence_population.rows[0].orphan_camera_tasks` |
| sink-a / sink-b 发布样本 | 未采 | **0 / 0** | 不作当前业务对比 | `logs.*.summary.count` |

表中本轮来源均在 [current-business.json](evidence/rolling_cache_phase0_20261015/business/current-business.json)。

### 摄像头排除规则与任务关联

以实际字段 `cameras.enabled` 判断启用；非压测筛选为：

```sql
COALESCE(c.source_id,'') !~* '^pressure'
AND COALESCE(c.name,'') !~ '(压力|压测)'
```

这会排除 source_id 以 `pressure` 开头（忽略大小写）或名称含“压力 / 压测”的摄像头。唯一余下的摄像头为 `id=cam_1080movie`、`source_id=source_cam_1080movie`、`name=1080movie测试`，`enabled=false`。它是非压测命名的测试源，不能据此宣称存在真实业务摄像头。即使进一步排除这个测试源，已启用业务源数仍为 0。完整非敏感字段清单见 `queries.cameras.rows`，未采集 RTSP 地址。

任务通过 `evidence_tasks.camera_id = cameras.id` 关联当前摄像头，使用 `evidence_tasks.created_at` 落入最近 24 小时窗口的口径。另计孤立 camera_id，避免把关联不到的行直接忽略。本窗全表任务本来即为 0，故没有实际发生因筛选规则造成的任务遗漏。

### 最近 24h 任务状态与最近过期时间

`queries.current_evidence_counts.rows=[]`：按 `task_type, materialization_status` 分组没有任何行；不是已有状态各自人为补 0。全表计数查询成功且为 0，可确认本窗口没有新增 evidence_tasks。

`queries.current_latest_expired.rows=[]`：本窗不存在可报告的最近一次非压测任务过期时间。另对当前保留的非压测摄像头查询所有现存过期任务（`queries.alltime_latest_nonpressure_expired`）亦无行。**没有过期时间可写，不能拿查询时间、deadline 或 updated_at 充当真实过期时间。** 此结论不覆盖已删除摄像头或已清理历史行。

### sink 发布耗时与日志覆盖

| 容器 | `segment published` | 队列驻留 P50/P95（ms） | publish_total P50/P95（ms） | 30s / 60s 趋势 |
|---|---|---|---|---|
| sink-a | 0 | 无法取得（n=0） | 无法取得（n=0） | 无发布样本，无法判断持续增长 |
| sink-b | 0 | 无法取得（n=0） | 无法取得（n=0） | 无发布样本，无法判断持续增长 |

来源：`current-business.json: logs.rolling-cache-sink-{a,b}.summary`、`buckets.30`、`buckets.60`。两个桶列表均为空；没有以健康检查或 maintenance 日志代替发布耗时。

| 容器 | 查询窗内最早可用日志（KST / UTC） | 查询窗内最晚可用日志（KST / UTC） |
|---|---|---|
| sink-a | 10-15 18:44:54.902067815 / `2026-10-15T09:44:54.902067815Z` | 10-15 18:59:57.493830209 / `2026-10-15T09:59:57.493830209Z` |
| sink-b | 10-15 18:44:54.245129863 / `2026-10-15T09:44:54.245129863Z` | 10-15 18:44:54.463736723 / `2026-10-15T09:44:54.463736723Z` |

来源：`logs.*.first_log_timestamp_utc/last_log_timestamp_utc`。查询命令均成功；可用日志含本次容器启动消息，但不能把请求的 30 分钟窗口等同于已覆盖完整 30 分钟的运行历史。是否由重建、启动或日志保留造成，应与阶段 0 容器快照核对。

原始日志：[sink-a](evidence/rolling_cache_phase0_20261015/business/rolling-cache-sink-a-last30m.log)、[sink-b](evidence/rolling_cache_phase0_20261015/business/rolling-cache-sink-b-last30m.log)。

## 4. 已确认 / 推断 / 未核实

### 已确认

- 所有摄像头均禁用，当前非压测命名摄像头亦禁用；上述 24 小时窗口没有新任务。
- 上述 30 分钟查询没有发布样本，无法计算发布延迟和增长趋势。
- `$ART/run_config.json.discard_pressure_results=false`，`report.json.db_summary_before_cleanup.tasks` 与 `db_summary_after_cleanup.tasks` 均为 4182；`cleanup.deleted_events=0`、`visual_results_retained=true`。来源为 `probe.json` 的 `stdout` JSON 中 `run_config.json/report.json` 对应字段。

### 推断

- 在采集截点，正式缓存挂错盘的风险尚未表现为正在新增的非压测取证任务损失；原因是当前无启用摄像头和近期任务样本。这不能外推到重新启用摄像头后的情况。
- 当前没有负载，为修复安排低峰提供了候选条件；实际维护窗口和重新应用运行时配置仍须用户批准。

### 未核实

- 实际取证链路在新事件下是否成功；本次未触发事件、启用摄像头或运行测试。
- 负载恢复后的 sink 发布延迟、积压增长及 NVMe 挂载切换收益。
- 禁用摄像头是否符合用户当前业务意图；本次只报告状态，不调整运行时。

## 5. 下一步建议及停止边界

在用户批准方案 A 前继续保持只读。修复后应在获批运行时下等待新的事件任务，验证对应证据实际生成；本次没有样本，不能预先写“通过”。不应为了取得 P50/P95 而擅自恢复摄像头、重建 sink 或启动压测。

T0.3 只能根据 `$ART` 可还原的当轮 cohort 和归一化 `last_reason` 作决策；若 ART 无法提供该原因，则按任务书第 ⑤ 类停止汇报。当前业务“0 负载”不是阶段 1 或阶段 2 的放行条件。

## 附：现存历史库行的补充核对（非 T0.3 主证据）

本次采集曾额外只读查询历史前缀现存库行，超出了用户对 T0.2“只读 `$ART`”的限定；发现后已停止历史库查询，未发生写入。原始输出保留于 [historical-database-supplement.json](evidence/rolling_cache_phase0_20261015/business/historical-database-supplement.json)，用于说明为何不能把当前库前缀总数当作当轮报告。此文件的 `evidence_status/warning` 明确限定用途。

`historical_presence` 返回任务书前缀 5252 条、60 源，created_at 从 `2026-10-12T21:34:33.246101+00:00` 到 `2026-10-12T22:52:14.196211+00:00`；与 ART 当轮 4182 条不一致。另一恢复快照前缀 `pressure60_allalg_rr157_uos157_1h_fixed_20261013_` 则为 1010 条。**这些前缀可能包含正式窗口之外的后续任务，不能将其原因比例、状态或成功视频数写成当轮结论。** 补充文件保存原因分组、每原因最多 10 条 detail 原文、按 ready_at 的 5 分钟桶及失败详情，仅供日后在额外授权和明确 cohort 后核对。

完整采集封装（含只读脚本、命令、返回码和原始 stdout）：[probe.json](evidence/rolling_cache_phase0_20261015/business/probe.json)、[live-and-historical.json](evidence/rolling_cache_phase0_20261015/business/live-and-historical.json)。
