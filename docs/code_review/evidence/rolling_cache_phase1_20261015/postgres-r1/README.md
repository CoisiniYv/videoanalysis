# B1 真实 PostgreSQL 验证

日期展示时区：Asia/Seoul（KST，UTC+09:00）。本证据使用阶段 1 当前分支源码，未连接正式数据库，未修改正式服务或正式 checkout。

## 执行环境

- 远端辅助主机：`user157`；Docker Server `27.3.1`。
- 临时镜像：`postgres:16-alpine`。
- 临时容器：`pressure60-rule-test-20261017`，ID `802ad047daccd210b9ee9311c769e415a6c0c1ff2f36ca8bb524cc0ca6a2aa3d`。
- 端口：远端 `127.0.0.1:15432`，通过本机 SSH 隧道映射到 `127.0.0.1:15432`。
- 数据库用户/库：`video` / `video_analytics`；仅用于 disposable schema，测试结束后删除 schema、容器和隧道。

## 原始命令与结果

```text
ssh user157 'docker run -d --rm --name pressure60-rule-test-20261017 -p 127.0.0.1:15432:5432 -e POSTGRES_USER=video -e POSTGRES_PASSWORD=video -e POSTGRES_DB=video_analytics postgres:16-alpine'
=> 802ad047daccd210b9ee9311c769e415a6c0c1ff2f36ca8bb524cc0ca6a2aa3d

ssh user157 'for i in $(seq 1 30); do docker exec pressure60-rule-test-20261017 pg_isready -U video -d video_analytics >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
=> exit 0

ssh -N -L 15432:127.0.0.1:15432 user157
=> SSH tunnel established; local test process exited it after the test.

PRESSURE60_RULE_TEST_DATABASE_URL='postgresql://video:video@127.0.0.1:15432/video_analytics' \
  /tmp/pressure60-harness-env/bin/python -m pytest -q \
  harness/tests/test_midterm_pressure60_postgres.py
=> .                                                                        [100%]
=> 1 passed in 0.45s
=> exit 0

ssh user157 'docker rm -f pressure60-rule-test-20261017'
=> pressure60-rule-test-20261017
=> exit 0
```

The test creates a disposable schema, applies migrations `002_events.sql`, `004_camera_config.sql`, `008_unified_event_evidence_algorithm_rules.sql`, `010_algorithm_config_api.sql`, `012_operator_camera_schema_compat.sql`, and `016_camera_rule_zone_id_text_compat.sql`, then uses non-UUID TEXT camera IDs. It asserts the `camera_rules` readback and the `ANY(%s::text[])` query both return the inserted IDs.

## 判定

已确认：B1 在真实 PostgreSQL 上通过；退出码为 `0`；临时容器已移除。该验证不改变阶段门：Opus 复审仍是阶段 1 到 A 轮的前置条件，正式镜像内 `docker cp` 哈希核验仍未执行。
