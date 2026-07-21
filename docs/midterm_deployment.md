# Midterm Deployment

更新时间：2026-07-20

## 1. 支持的入口

只使用：

```bash
bash scripts/midterm_start.sh
```

不要把裸 `docker compose -f infra/docker-compose.midterm.yml up` 当成等价入口：它不会
完整表达存储 override、B 分支模型缓存准备和 8090 双分支容器预创建流程。

部署文件：

| 用途 | 文件 |
| --- | --- |
| 主 Compose | `infra/docker-compose.midterm.yml` |
| 环境默认值 | `infra/env/midterm.env` |
| 快速盘/轨迹挂载 | `infra/midterm-storage.override.yml` |
| 8090 单卡双分支预创建 | `infra/operator-dual-runtime.override.yml` |
| Replay | `modules/savant_replay/config.midterm*.json` |
| Savant | `modules/savant_security/module.yml` |
| Camera 导出快照 | `modules/savant_security/config/cameras.midterm.yml` |

历史 `archive/phase-only/` 不是部署入口。

## 2. 启动脚本做什么

`midterm_start.sh` 按顺序：

1. 检查 Docker、Compose、GPU 和关键文件；
2. 创建 `/data/video-analytics`、`/tmp/video-analytics-mps` 和快速盘目录；
3. 验证 YOLO/AdaFace 模型；
4. 构建 face-worker，再构建复用其运行层的 API 和其他服务；
5. 构建完整预设使用的 ROI AdaFace 与 rolling-cache sink 镜像；
6. 启动无 profile 的基础服务；
7. 为 Savant B 准备独立可写 engine cache
   `/data/video-analytics/models-savant-b`；
8. 使用 `operator-dual-runtime` profile 将 A/B、MPS、ROI、rolling 等容器
   `--no-start` 预创建；
9. 等待 8090 和 API proxy。

预创建过程不会主动切换到双分支；8090 是后续 start/stop owner。若容器已经运行，
预创建脚本不会为了检查而中断它。

## 3. 数据目录

默认慢盘/持久数据根：

```text
/data/video-analytics
  models/
  models-savant-b/
  media/evidence/
  media/.runtime/
  replay-midterm*/
  artifacts/
```

默认快速盘根：

```text
/home/user/video-analytics-fast
  rolling-cache/
  rolling-cache-materialized/
  face_trajectory_cache/
```

可通过 `VIDEO_ANALYTICS_DATA_ROOT`、`VIDEO_ANALYTICS_FAST_ROOT` 及 storage override
中的 host-root 环境变量调整。最终 evidence 仍写入持久 `/data`；rolling 在线窗口和
中间物化使用快速盘。

## 4. 数据库与迁移

默认连接宿主 PostgreSQL：

```text
postgresql://video:video@host.docker.internal:5432/video_analytics
```

`local-postgres` profile 才会启用 Compose PostgreSQL 和宿主 5439。

部署前按顺序应用 `db/migrations/*.sql`。注意：

- 029/030 建立 evidence materialization lifecycle 和索引；
- 031 建立 Replay create fencing；
- 032 使用 `CREATE INDEX CONCURRENTLY`，不能放在事务中，必须以 autocommit 在非
  压力窗口执行；
- migration 完成不等于 worker 已经部署，schema 与镜像/挂载代码必须同版本。

## 5. 启动完整双分支

打开：

```text
http://<host>:8090/operator
```

普通操作员：

1. 登记摄像头、ROI 和算法，不必逐路加入当前运行；
2. 点击“选择摄像头并启动”；
3. 选择 `production_t4_40` 或 `local_4090_60`；
4. 选择精确的 40 或 60 路；
5. 自动均分或手动 A/B；
6. 启动并等待后台任务完成。

后台阶段：预检、A/B TensorRT/容器、摄像头收敛、rolling-cache 预热、evidence
开放。状态写入
`/data/video-analytics/media/.runtime/topology_apply_status.json`。浏览器刷新不影响
任务，但 API 容器重启会使任务失败。

### 当前预设

| 项目 | T4 40 | 4090 60 |
| --- | ---: | ---: |
| A/B | 20/20 | 30/30 |
| FPS | 4 | 8 |
| Pose/Face batch | 4/4 | 4/4 |
| ROI AdaFace | 16 | 16 |
| MPS | 45/45/10 | 关闭 |
| rolling prefill/retention | 25s/600s | 25s/600s |

4090 预设只关闭 MPS，不关闭 ROI AdaFace 或 rolling-cache。

## 6. 启动后验收

必须同时检查：

- 8090 `/health`；
- `/api/v1/runtime/overview`；
- `/api/v1/runtime/latency`；
- `/api/v1/runtime/topology-config` 与 apply-status；
- A/B source 数、effective FPS、queue 和 send failures；
- ROI AdaFace、person、face Redis group lag/pending；
- rolling sink ready、segment source 覆盖与 retention；
- evidence active/failed/expired/fallback；
- DB-backed list/detail/timeline/annotation；
- 新 MOV 的 5+5、约 24 FPS、HTTP Range 和 bbox/person context。

`scripts/midterm_health.sh` 仍有旧固定清单：它没有检查
`person-observation-worker`，并仍期待 legacy `source-adapter`。在代码修复前，不能只凭
这个脚本判定完整双分支失败或成功。

## 7. 停止语义

8090“停止完整链路”：

- 停止 dynamic source 和双分支推理/Replay/raw fanout/ROI/MPS；
- 把当前摄像头标为 disabled；
- 保留 event-worker、media-worker 和 rolling sink 做有限收尾；
- 更新 apply-status 为 stopped。

整栈停止：

```bash
bash scripts/midterm_stop.sh
```

该脚本包含 local-postgres、dual、rolling、ROI 和 operator profiles。默认保留
`/data/video-analytics` 数据。

## 8. 可选 profile

- `local-postgres`：本地 PostgreSQL；
- `qdrant`：Qdrant 与 gallery sync worker；
- `legacy-primary-rtsp`：旧静态 source adapter；
- `rolling-cache` / `rolling-cache-dual`：手动 rolling 调试；
- `roi-adaface`：手动 ROI worker；
- `dual-replay-shards`、`dual-4090-two-source`：历史/压测组合；
- `operator-dual-runtime`：8090 预创建与管理。

当前 env 默认 `FACE_VECTOR_BACKEND=pgvector`。启用 qdrant profile 还不等于 face-worker
已切换；必须同时设置向量后端、检查 outbox/bootstrap/reconcile 和 fallback 指标。

## 9. 端口

| 端口 | 用途 |
| ---: | --- |
| 8090 | 用户入口、API/media 代理 |
| 6396 | Redis |
| 8098 | 基础 Replay API |
| 18080 | 基础 Savant metrics |
| 18081 | 基础 analysis-forwarder metrics |
| 18184 | 基础 raw fanout metrics |
| 18180/18181 | A/B Savant metrics |
| 18185/18186 | A/B raw fanout metrics |
| 18187 | ROI AdaFace metrics |
| 5439 | 可选 local PostgreSQL |

8090 不代理 FastAPI `/docs`。接口清单见
`docs/frontend_interface/02_api_inventory.md`。

## 10. 相关文档

- 当前架构：`docs/current_architecture.md`
- 当前状态：`docs/current_mainline_status.md`
- Compose 服务表：`docs/compose_inventory.md`
- 操作指南：`docs/midterm_web_operator_guide.md`
- 双分支专项流程：
  `docs/midterm_8090_single_gpu_dual_branch_operator_runbook_2026-07-14.md`
