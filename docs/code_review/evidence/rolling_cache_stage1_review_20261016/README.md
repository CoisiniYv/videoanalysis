# 阶段 1 审查证据（Opus）

- `harness_summary_*.txt` / `harness_failures_*.txt`：`env -u DATABASE_URL -u VIDEO_ANALYTICS_DATABASE_URL python -m pytest -q harness/tests -p no:cacheprovider --continue-on-collection-errors` 在 98d388c 与 ba76375 上的结果与失败名单。
- `probe_main.py`：审查用的桩化 main() 探针。用法 `python probe_main.py <checkout> <art_dir> <注入失败的函数名>`，打印 main() 实际调用的恢复步骤序列。可作为 main() 级恢复路径测试的参考，不是正式测试。
- `probe_blocked_cpu_restore.py`：blocked_active_evidence + CPU 隔离路径的同类探针（M2）。
