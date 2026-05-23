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

## Phase 1A — Savant GPU 可用性验收

验证宿主 GPU、CUDA Docker 容器和 Savant DeepStream 镜像的 GPU 访问能力。

### 文件

```text
harness/smoke/savant_gpu_smoke.sh
infra/docker-compose.savant-smoke.yml
```

### 烟雾测试脚本

```bash
bash harness/smoke/savant_gpu_smoke.sh
```

脚本依次执行三项检查：

1. **host nvidia-smi** — 宿主 GPU 驱动可用
2. **CUDA container** — `nvidia/cuda:12.4.1-base-ubuntu22.04` 容器内可调用 GPU
3. **Savant DeepStream** — `ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1` 容器内可调用 GPU

每项检查输出 `[OK]`、`[FAIL]` 或 `[SKIP]`。全部通过时退出码为 0。

> **注意**：Savant DeepStream 镜像有自己的默认 entrypoint（`savant/entrypoint/run.py`），不能把 `nvidia-smi` 当作普通命令参数传入。必须使用 `--entrypoint nvidia-smi` 覆盖 entrypoint。

### Docker Compose 方式

```bash
sudo docker compose -f infra/docker-compose.savant-smoke.yml up
```

### 成功标准

所有三项检查均 `[OK]`，且脚本 exit code 为 0。

### 常见错误

```
python -m savant.entrypoint: error: argument config: can't open 'nvidia-smi'
```

**原因**：把 `nvidia-smi` 当作普通命令参数传给了 Savant 镜像，但镜像默认 entrypoint 是 Savant 启动器，它把 `nvidia-smi` 当作 pipeline 配置文件路径来解析。

**修复**：使用 `--entrypoint nvidia-smi` 覆盖默认 entrypoint。

### 前置依赖

- NVIDIA 驱动已安装（`nvidia-smi` 可执行）
- Docker 已配置 `nvidia-container-toolkit`
- （可选）Savant DeepStream 镜像已拉取：
  ```bash
  docker pull ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
  ```

---

## Phase 1B — Savant minimal module / compose smoke test

验证 Savant 容器在 Docker Compose 下稳定运行，GPU 直通、目录挂载、模块骨架全部正常。

### 文件

```text
infra/docker-compose.savant-smoke.yml
modules/savant_smoke/module.yml
modules/savant_smoke/config/cameras.yml
scripts/smoke/check_savant_smoke.sh
harness/tests/test_savant_smoke.sh
```

### 设计说明

- compose 用 `entrypoint: ["sleep"] + command: ["infinity"]` 保持容器存活，不启动实际 pipeline
- 挂载 `modules/savant_smoke` 到 `/opt/savant/src/module`
- 挂载 `scripts` 目录，方便在容器内执行自检
- GPU 通过 `deploy.resources.reservations.devices` 传递（单卡，`count: 1`）
- healthcheck 执行 `nvidia-smi`，每 15 秒检测 GPU 可用性

### 验收命令

```bash
# 1. 确保宿主数据目录存在
sudo mkdir -p /data/video-analytics/{models,downloads,media}

# 2. 验证 compose 配置
docker compose -f infra/docker-compose.savant-smoke.yml config

# 3. 启动
docker compose -f infra/docker-compose.savant-smoke.yml up -d

# 4. 确认状态
docker compose -f infra/docker-compose.savant-smoke.yml ps

# 5. 容器内 GPU 验证
docker compose -f infra/docker-compose.savant-smoke.yml exec savant-smoke nvidia-smi

# 6. 验证挂载目录
docker compose -f infra/docker-compose.savant-smoke.yml exec savant-smoke ls -la /opt/savant/src/module/module.yml
docker compose -f infra/docker-compose.savant-smoke.yml exec savant-smoke ls -la /models /downloads /media

# 7. 运行自检脚本
docker compose -f infra/docker-compose.savant-smoke.yml exec savant-smoke \
  bash /opt/savant/src/scripts/smoke/check_savant_smoke.sh

# 8. 运行完整验收
bash harness/tests/test_savant_smoke.sh

# 9. 清理
docker compose -f infra/docker-compose.savant-smoke.yml down
```

### 成功标准

- `docker compose ps` 显示 `healthy`
- 容器内 `nvidia-smi` 正常输出 GPU 信息
- `/opt/savant/src/module/module.yml` 可见
- `/models`、`/downloads`、`/media` 可读写
- 自检脚本全部 `[OK]`

---

## Phase 1C — 单路视频 / Minimal Pipeline

验证从视频文件 ffmpeg 推流 → RTSP server → Savant pipeline → frame metadata 输出的完整链路。

### 文件

```text
infra/docker-compose.phase1c.yml
modules/savant_security/module.yml
modules/savant_security/config/cameras.yml
modules/savant_security/custom/pyfuncs/minimal_frame_probe.py
harness/tests/test_pipeline_smoke.py
testVideo/test.mp4
```

### 架构

```text
testVideo/test.mp4
  -> ffmpeg（循环推流）
  -> mediamtx RTSP server
  -> Savant DeepStream（uridecodebin source + MinimalFrameProbe PyFunc）
  -> stdout log（每 30 帧输出 Frame #N）
```

### 设计说明

- **RTSP server**: `bluenviron/mediamtx:1.11.3`，仅用于本阶段本地测试
- **推流方案**: ffmpeg 以 `-stream_loop -1` 循环推送 `testVideo/test.mp4`，用 `libx264` 软件转码确保兼容性
- **Savant pipeline**: `module.yml` 定义 `uridecodebin` 源读取 RTSP 流，经过 `MinimalFrameProbe` PyFunc
- **FrameProbe**: 每 30 帧输出日志到 stdout（继承 `NvDsPyFuncPlugin`）
- **输出**: stdout log 为唯一必验 output sink。不写 Redis stream / `security.events` / PostgreSQL
- **Redis 容器**: 仅为后续阶段预留，Phase 1C 不依赖、不写入、不验收 Redis

### 验收命令

```bash
# 1. 确保宿主数据目录存在
sudo mkdir -p /data/video-analytics/{models,downloads,media}

# 2. 验证 compose 配置
docker compose -f infra/docker-compose.phase1c.yml config

# 3. 启动所有服务
sudo docker compose -f infra/docker-compose.phase1c.yml up -d

# 4. 查看状态
sudo docker compose -f infra/docker-compose.phase1c.yml ps

# 5. 查看 ffmpeg 推流日志
sudo docker compose -f infra/docker-compose.phase1c.yml logs --tail=50 phase1c-ffmpeg-source

# 6. 查看 Savant 日志（确认 Frame # 输出）
sudo docker compose -f infra/docker-compose.phase1c.yml logs --tail=100 savant-phase1c

# 7. 运行结构检查（无需 GPU）
pytest harness/tests/test_pipeline_smoke.py -q

# 8. 运行完整 GPU smoke test
pytest harness/tests/test_pipeline_smoke.py -q --gpu

# 9. 清理
sudo docker compose -f infra/docker-compose.phase1c.yml down -v
```

### 成功标准

- `testVideo/test.mp4` 被使用，未被替换
- ffmpeg 持续循环推流到 `rtsp://rtsp-server:8554/phase1c`
- Savant 读取 RTSP 流并连续处理多帧
- Savant 日志出现 `Phase1C Frame #N` 且帧数随时间持续增长（N >= 30）
- 没有启动 YOLO26-pose、nvtracker、SCRFD/ArcFace、event-worker
- 没有写入 `security.events` 或 PostgreSQL
- 不影响 Phase 0、1A、1B

### 常见问题

**Savant 容器反复重启**：
检查 ffmpeg 推流是否成功：`sudo docker compose logs phase1c-ffmpeg-source`。如果 ffmpeg 无法连接 RTSP server，mediamtx 可能尚未就绪。

**Savant 日志没有 Frame # 输出**：
Savant 启动后需要 5-15 秒初始化 GStreamer pipeline 和 GPU 资源。等待后重新查看日志。

**mediamtx 端口冲突**：
如果宿主机 8554 端口已被占用，修改 `docker-compose.phase1c.yml` 中 `rtsp-server` 的端口映射。

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
