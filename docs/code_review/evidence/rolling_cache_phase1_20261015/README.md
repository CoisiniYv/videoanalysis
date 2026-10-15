# Stage 1 validation evidence

All displayed audit times use Asia/Seoul; original timestamps remain in JSON/XML.

- `comparison.json`, current JUnit/summary/log: latest authorized repair round 1, 2016 passed / 11 failed / 73 skipped / 1 existing collection error; no new failure/error versus baseline 98d388c.
- `new-tests-*`: exact JUnit set difference, 173 newly added tests, all passed.
- `preflight-r2/`: latest tested source, real readonly NVMe rc0 / HDD rc2, 30 production containers unchanged.
- `baseline-*`, `dependencies.txt`: fixed baseline and shared isolated dependency environment.
- `history/harness-before-authorized-repair/`: earlier 17 failures / 6 new failures, preserved before the latest authorized repair. Root `new-test-pollution-{single,dual}.json` are also historical failure examples, not the latest passing collector results.
- `preflight/`, `preflight-r1/`: earlier real CLI failure and subsequent profile/import repair check, retained as history.
- `minimum-results.json`, doctor/deployment/smoke outputs: existing minimum validation run; related configuration inputs and check scripts have not changed. Known baseline failures are documented, not presented as green.

The root manifest covers top-level packaged files; subdirectories have their own manifests. Raw logs are explicitly tracked despite repository log ignores. The local attributes disable whitespace checks only for raw XML/CSV/log evidence, preserving original traceback and line-ending bytes. Source and hand-written documentation remain checked.
