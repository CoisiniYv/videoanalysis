# Video Analytics Platform

多路实时视频分析系统，基于 Savant + NVIDIA DeepStream + TensorRT。

## Phase 0 — 基础设施

Phase 0 启动最小本地开发环境：

- Redis 7.4.9
- PostgreSQL 16 + pgvector
- FastAPI 健康检查 API
- Event worker（心跳 + 连接检查）

未包含：Savant、GPU 支持、人脸识别、录像、前端、Prometheus、Grafana。

### 目录结构

```text
infra/docker-compose.dev.yml
services/api/Dockerfile
services/api/requirements.txt
services/api/app/main.py
services/event-worker/Dockerfile
services/event-worker/requirements.txt
services/event-worker/main.py
db/migrations/001_init.sql
.env.example
```

### 环境变量

默认值已在 `docker-compose.dev.yml` 中设置。如需覆盖，复制 `.env.example`：

```bash
cp .env.example .env
```

### 启动

从仓库根目录运行：

```bash
sudo docker compose -f infra/docker-compose.dev.yml up -d --build
```

### 停止

```bash
sudo docker compose -f infra/docker-compose.dev.yml down
```

删除数据卷（会丢失数据库和 Redis 数据）：

```bash
sudo docker compose -f infra/docker-compose.dev.yml down -v
```

### 查看日志

```bash
sudo docker compose -f infra/docker-compose.dev.yml logs -f
sudo docker compose -f infra/docker-compose.dev.yml logs -f api
sudo docker compose -f infra/docker-compose.dev.yml logs -f event-worker
```

---

## Phase 0 验收命令

```bash
# 1. 查看容器状态
sudo docker compose -f infra/docker-compose.dev.yml ps

# 2. 健康检查
curl http://localhost:8000/health

# 3. Readiness 检查（Redis + PostgreSQL）
curl http://localhost:8000/ready

# 4. Redis ping
sudo docker compose -f infra/docker-compose.dev.yml exec redis redis-cli ping

# 5. PostgreSQL 表检查
sudo docker compose -f infra/docker-compose.dev.yml exec postgres \
  psql -U video -d video_analytics -c "\dt"

# 6. 检查 events 表字段
sudo docker compose -f infra/docker-compose.dev.yml exec postgres \
  psql -U video -d video_analytics -c "\d events"

# 7. 查看 worker 心跳日志（每 10 秒输出）
sudo docker compose -f infra/docker-compose.dev.yml logs -f event-worker
```

---

## 常见问题

### PostgreSQL 报 role "video" does not exist

旧数据卷已经用旧用户（如 `phase0`）初始化过了。使用新用户 `video` 前需要删除旧数据卷：

```bash
sudo docker compose -f infra/docker-compose.dev.yml down -v
sudo docker compose -f infra/docker-compose.dev.yml up -d --build
```

> **注意**：`down -v` 会删除当前开发数据库所有数据，仅限 Phase 0 / 开发环境使用。

### Docker 普通用户权限问题

如果 `docker compose` 命令报 permission denied，说明当前用户不在 docker 组：

```bash
sudo usermod -aG docker $USER
```

然后**退出当前终端，重新登录**，或重启 shell。验证：

```bash
groups
docker ps
```

### 端口冲突

如果 `8000`、`6379`、`5432` 被占用，可在 `.env` 中设置：

```bash
API_PORT=8001
REDIS_PORT=6380
POSTGRES_PORT=5433
```

### API 返回 503

Redis 或 PostgreSQL 尚未就绪。等待几秒后重试，或查看日志：

```bash
sudo docker compose -f infra/docker-compose.dev.yml logs api
```
