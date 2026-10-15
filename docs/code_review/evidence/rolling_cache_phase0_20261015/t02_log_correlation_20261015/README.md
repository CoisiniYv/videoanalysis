# 历史日志 event_id 关联补充

本目录是用户追加授权下的独立证据，不修改旧 T0.2 证据或其 MANIFEST。仅读取 10-13 ART 中的历史 worker 日志与非成功任务明细，没有数据库、活服务、容器或正式 checkout 操作。

- `summary.json`：分类、输入 SHA256、计数、基线归一化方法、过期任务 ready_at 五分钟桶和限制。
- `correlated_events.json`：ART 中全部 2,877 个非成功事件的精确 event_id 关联。
- `reason_samples.json`：每类最多 10 个不同事件的原文样本，包含日志行号和 UTC/KST。
- `probe_log_reasons.*`：最初按归一化码探查，字面命中为零；原始日志存异常原文，不能把这个零误读成没有重试。
- `probe_log_events.*`：历史日志事件格式、计数与小样本，确认存在 `rolling_cache_coverage_deferred`。
- `correlate_log_reasons.py` / `*.capture.json`：SSH stdin 的远端只读脚本与回执；取 ART task_updated_at 之前最后一次 persisted=True 的重试日志。
- `normalization_contract_98d388c.py`：`git show 98d388c:libs/evidence_lifecycle/contract.py` 的逐字原件，用于规范性映射，不是历史镜像哈希证据。
- `summarize_correlation.py`：本地运行基线 normalize_reason_code，验证新旧输入 SHA256 相同并生成最终统计。
- `finalize_log_evidence.mjs` / `redactions.json`：本地 URI 凭据脱敏及本目录 MANIFEST 生成。未脱敏副本保留于仓库外受限目录；远端 ART 不变。

中间提取件 `correlated_events.raw.json` 不入库：其 2,877 个事件与全部原字段均被 `correlated_events.json` 完整保留，且与 `correlate_log_reasons.capture.json` 的 stdout 解析结果语义相同。复现脚本直接读取 capture，不依赖该中间副本。所有 capture 均保留原始 stdout 的结构，URI userinfo 可能因脱敏替换，不能用其本地字节哈希冒充源日志哈希。源日志与明细 SHA256 单独保存在 summary.inputs；仓库外受限原件未改动。

已完成的命令：

```sh
node /tmp/savant-readonly-capture.mjs uos157 \
  docs/code_review/evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/correlate_log_reasons.py \
  docs/code_review/evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/correlate_log_reasons.capture.json
/Users/khonsu/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12 -B \
  docs/code_review/evidence/rolling_cache_phase0_20261015/t02_log_correlation_20261015/summarize_correlation.py
```

日志缺失不是单独判定从未尝试的依据；attempt_count=0 来自 ART。日志最后观测原因不是数据库 lifecycle_v2_retry audit。成功任务不在该非成功 cohort 中，不能生成完整的成功/过期 ready_at 对照。
