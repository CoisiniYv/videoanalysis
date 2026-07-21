# Midterm Deployment Quick Reference

更新时间：2026-07-20

## 整栈命令

```bash
bash scripts/midterm_start.sh
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh
bash scripts/midterm_stop.sh
```

`midterm_start.sh` 还会准备快速盘、Savant B 模型 cache，并预创建 8090 管理的双分支
容器。基础启动完成后，在 8090 选择 40/60 路并启动完整链路。

`midterm_health.sh` 的固定服务表仍偏向旧单分支：未列出
`person-observation-worker`，并期待 legacy `source-adapter`。请结合 8090 overview/latency。

## 入口

- 操作台：`http://127.0.0.1:8090/operator`
- 8090 health：`http://127.0.0.1:8090/health`
- Runtime overview：`http://127.0.0.1:8090/api/v1/runtime/overview`
- Runtime latency：`http://127.0.0.1:8090/api/v1/runtime/latency`
- Evidence DB health：`http://127.0.0.1:8090/api/v1/evidence/health`

8090 不代理 FastAPI `/docs`。API 清单见
`docs/frontend_interface/02_api_inventory.md`。

## Compose 查看命令

为了与启动脚本的存储挂载一致：

```bash
docker compose \
  --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml \
  -f infra/midterm-storage.override.yml ps
```

查看单个基础服务日志：

```bash
docker compose \
  --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml \
  -f infra/midterm-storage.override.yml logs -f api
```

双分支容器由 8090/Docker Engine 管理时，也可以直接按容器名查看：

```bash
docker logs --tail 200 video-analytics-midterm-savant-a
docker logs --tail 200 video-analytics-midterm-rolling-cache-sink-a
docker logs --tail 200 video-analytics-midterm-person-observation-worker
```

## 常用 API

### 摄像头

```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/cameras | jq
```

新增摄像头建议默认不加入当前运行：

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8090/api/v1/cameras \
  -H 'Content-Type: application/json' \
  -d '{"name":"Camera 1","rtsp_url":"rtsp://host/stream","enabled":false}' | jq
```

### 人员/人脸

```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/people | jq

curl --noproxy '*' -X POST \
  http://127.0.0.1:8090/api/v1/people/register-face \
  -F 'image=@/path/to/photo.jpg' \
  -F 'name=John Doe' \
  -F 'external_person_id=employee_001' | jq
```

批量注册使用 `/api/v1/people/register-faces`，重复传 `images` 字段。

### Evidence

```bash
curl --noproxy '*' 'http://127.0.0.1:8090/api/v1/evidence/bundles?limit=10' | jq
curl --noproxy '*' http://127.0.0.1:8090/api/v1/evidence/bundles/<event_id> | jq
```

### Runtime

```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/runtime/topology-config | jq
curl --noproxy '*' http://127.0.0.1:8090/api/v1/runtime/topology-config/apply-status | jq
curl --noproxy '*' http://127.0.0.1:8090/api/v1/runtime/latency | jq
```

普通操作员应使用 8090 快速启动器，不直接拼 apply body。

### Savant supervisor

```bash
curl --noproxy '*' http://127.0.0.1:8090/api/v1/cameras/runtime/supervisor | jq
curl --noproxy '*' -X POST \
  http://127.0.0.1:8090/api/v1/cameras/runtime/supervisor/recover | jq
```

## 端口

| 端口 | 用途 |
| ---: | --- |
| 8090 | 操作台、API/media proxy |
| 6396 | Redis |
| 8098 | 基础 Replay API |
| 18080 | 基础 Savant metrics |
| 18081 | 基础 analysis-forwarder metrics |
| 18184 | 基础 raw fanout metrics |
| 18180/18181 | A/B Savant metrics |
| 18185/18186 | A/B raw fanout metrics |
| 18187 | ROI AdaFace metrics |

## 快速排障顺序

### 完整启动失败

1. `apply-status` phase/error/details；
2. selected source 数是否精确为 40/60；
3. A/B、ROI、rolling、MPS 预创建容器是否存在；
4. Savant B 模型 cache 是否完整；
5. active evidence guard；
6. dynamic source/RTSP 是否收敛。

### 有推理、无 evidence

完整预设 `security.record_requests=0` 可以正常。依次检查 event、task gate、rolling
segment coverage、media task phase/lease、finalizer handoff、DB index 和媒体文件。

### 无人员轨迹

检查 `security.person_observations` 的
`person-observation-workers-midterm` group lag/pending，以及独立 person worker 日志。

### 无人脸命中

检查 ROI worker pending/lag、face observations、face-worker、图库注册和当前 effective
vector backend。默认是 pgvector；不要假设 Qdrant 已启用。

## 关键文档

- `docs/current_architecture.md`
- `docs/current_mainline_status.md`
- `docs/midterm_deployment.md`
- `docs/midterm_web_operator_guide.md`
- `docs/frontend_interface/02_api_inventory.md`
