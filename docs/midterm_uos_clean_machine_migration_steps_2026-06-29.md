# Midterm 统信 UOS 新机器迁移步骤

日期：2026-06-29

本文给出把当前 midterm 版本迁移到统信 UOS 新机器的明确步骤。业务服务、8090、
worker、Replay、Savant、Redis 和推荐的新机器 PostgreSQL 都通过 Docker Compose
部署；UOS 宿主机只负责 Docker daemon、NVIDIA 驱动/runtime、持久化目录和启动脚本。
迁移方式采用“干净迁移”：只迁移当前代码快照、模型目录和可选 Docker 镜像包，不迁移
旧 PostgreSQL、Redis、Replay RocksDB、证据媒体、人脸库或历史诊断产物。

## 0. 迁移边界

必须迁移：

```text
/home/user/video-analytics
/data/video-analytics/models
```

不要迁移：

```text
/data/video-analytics/downloads
/data/video-analytics/models-savant-b
/data/video-analytics/artifacts/*
/data/video-analytics/media/evidence/*
/data/video-analytics/media/replay-sink-output/*
/data/video-analytics/media/midterm-snapshots/*
/data/video-analytics/media/face_uploads/*
/data/video-analytics/media/face_registration/*
/data/video-analytics/media/face-registration/*
/data/video-analytics/replay-midterm*
PostgreSQL 旧数据；新机器推荐使用 compose local-postgres 空库
Redis 旧数据
```

人脸库的有效识别数据在 PostgreSQL 的 `persons` 和
`person_gallery_embeddings` 表中。干净迁移不迁旧库，所以新机器启动后需要在
8090 页面重新添加人员和注册人脸。

## 1. UOS 新机器基础环境

先确认机器是 x86_64、NVIDIA GPU 可见、磁盘空间足够：

```bash
uname -m
df -h / /data
lspci | grep -i nvidia || true
```

UOS 上宿主机不需要安装项目 Python 依赖，不直接运行 API、worker、Savant 或
PostgreSQL 业务进程。业务全部通过 Docker 镜像运行。宿主机必须具备：

```text
Docker Engine
Docker Compose v2 plugin
NVIDIA driver
NVIDIA Container Toolkit
tar / gzip / curl / ss(iproute2)
```

安装命令以现场 UOS 软件源为准。完成后必须通过这些检查：

```bash
docker --version
docker compose version
docker info
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

注意：

- 使用 `docker compose` v2，不使用旧的 `docker-compose` v1。
- 如果普通用户要直接执行 Docker，需加入 `docker` 组并重新登录。
- 如果 UOS 机器不能直接访问 Docker Hub 或 `ghcr.io`，先在可联网机器预拉取镜像，
  再用 `docker save` / `docker load` 离线导入。
- 8090、8098、6396、18080、18081 需要在本机空闲。启用本地 PostgreSQL 时还会使用
  5439。

### 1.1 UOS 在线安装参考命令

UOS 版本、内核和软件源差异会影响安装细节。这里按 Debian 系发行版给出参考命令；
真正生产交付前，必须在同版本 UOS 测试机上固定并验证一套版本组合。

Docker Engine 和 Compose v2：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg lsb-release

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "UOS VERSION_CODENAME=${VERSION_CODENAME:-}"

# 如果 UOS 的 VERSION_CODENAME 不是 Docker 支持的 Debian 代号，
# 需要按实际底座替换为 bookworm / bullseye 等已验证代号。
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt-get update
sudo apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

sudo systemctl enable --now docker
docker --version
docker compose version
sudo docker run --rm hello-world
```

NVIDIA 驱动和 NVIDIA Container Toolkit：

```bash
# 先按 UOS / 现场 GPU 型号安装 NVIDIA 驱动，并确认宿主机 GPU 可见。
nvidia-smi

sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

如果普通部署用户需要直接执行 Docker：

```bash
sudo usermod -aG docker "$USER"
# 重新登录后验证：
docker info
```

### 1.2 UOS 离线前置环境包

生产现场不建议每台机器联网安装 Docker / NVIDIA runtime。更稳定的方式是在一台同版本
UOS 测试机上验证并固化前置环境包：

```text
host-prereqs-uos-<uos版本>-<gpu驱动版本>-<docker版本>.tgz
  install_host_prereqs.sh
  docker-ce*.deb
  docker-ce-cli*.deb
  containerd.io*.deb
  docker-buildx-plugin*.deb
  docker-compose-plugin*.deb
  nvidia-container-toolkit*.deb
  nvidia-container-toolkit-base*.deb
  libnvidia-container-tools*.deb
  libnvidia-container1*.deb
  NVIDIA driver 安装包或现场 UOS 驱动安装说明
```

目标机先安装前置包，再部署应用包：

```bash
sudo bash install_host_prereqs.sh
docker --version
docker compose version
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

“完全兼容 UOS”在生产上应理解为固定并验证这些变量：

```text
UOS 版本和 CPU 架构
Linux kernel 版本
NVIDIA GPU 型号
NVIDIA driver 版本
Docker Engine / Compose plugin / containerd 版本
NVIDIA Container Toolkit 版本
本项目 midterm-clean 离线应用包版本
```

不要把未验证的新 UOS 小版本、驱动大版本或 Docker 大版本直接带到生产机上。

## 2. 源机器打包

迁移包是一次性快照。代码、Dockerfile、requirements、模型或 compose/env 改动后，旧包
仍然可以部署，但部署的是旧版本；要让生产机拿到新版本，必须重新打包：

```bash
cd /home/user/video-analytics
bash scripts/midterm_package_clean.sh --include-images
```

判断是否需要重新打包：

```text
改 Python 代码：需要重新打包，因为业务镜像要重新 build
改 Dockerfile / requirements：需要重新打包，并重新验证镜像构建
改 compose / env / 部署脚本：需要重新打包
改 /data/video-analytics/models：需要重新打包 models.tgz
只在 8090 新增摄像头/人员/规则：不属于代码包；干净新机器需要重新配置
```

推荐生成离线包。它会打包代码、模型，并把当前 compose 所需镜像 build/pull 后保存到
`images.tar`：

```bash
cd /home/user/video-analytics
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/midterm_package_clean.sh --include-images
```

输出通常是：

```text
/data/video-analytics/artifacts/migrations/midterm-clean-YYYYMMDDTHHMMSSZ.tgz
/data/video-analytics/artifacts/migrations/midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh
```

`--include-images` 会包含这些运行镜像：

```text
redis:7-alpine
pgvector/pgvector:pg16
ghcr.io/insight-platform/savant-replay-x86:v0.6.0
ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0
video-analytics-midterm-*.latest 本地业务镜像
```

默认不打包 TensorRT `.engine` 文件。新机器 GPU、驱动、TensorRT 环境不同，通常应让
目标机器重新生成 engine。只有确认目标机器环境完全一致时，才考虑：

```bash
bash scripts/midterm_package_clean.sh --include-engines
```

## 3. 拷贝到 UOS 目标机

把两个文件拷到 UOS 目标机：

```bash
scp /data/video-analytics/artifacts/migrations/midterm-clean-YYYYMMDDTHHMMSSZ.tgz user@TARGET:/tmp/
scp /data/video-analytics/artifacts/migrations/midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh user@TARGET:/tmp/
```

如果打包时使用了 `--include-images`，目标机不需要单独 `docker pull` 或
`docker build`。部署脚本会自动校验并 `docker load` 包内的 `images.tar`，随后用
`--no-build` 启动。

## 4. 目标机部署

目标目录必须为空。新机器推荐直接使用本项目的本地 PostgreSQL profile，让数据库也
在 Docker 中运行：

```bash
cd /tmp
bash midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh \
  midterm-clean-YYYYMMDDTHHMMSSZ.tgz \
  --local-postgres
```

这会恢复代码到 `/home/user/video-analytics`，恢复模型到
`/data/video-analytics/models`，创建空运行时目录，校验 compose，并启动完整服务。

如果目标机要连接外部 PostgreSQL，而不是 compose 内置 PostgreSQL，先准备一个空库并
确保迁移 SQL 会在首次部署前应用，然后使用：

```bash
export VIDEO_ANALYTICS_DATABASE_URL='postgresql://video:video@<postgres-host>:5432/video_analytics'
cd /tmp
bash midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh midterm-clean-YYYYMMDDTHHMMSSZ.tgz
```

只解包和校验、不启动：

```bash
cd /tmp
bash midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh \
  midterm-clean-YYYYMMDDTHHMMSSZ.tgz \
  --local-postgres \
  --no-start
```

之后启动：

```bash
cd /home/user/video-analytics
bash scripts/midterm_start.sh --local-postgres --no-build
```

## 5. 启动后检查

目标机执行：

```bash
cd /home/user/video-analytics
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml ps
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh
```

浏览器访问：

```text
http://目标机IP:8090/operator
```

首次进入后按顺序做：

1. 在 8090 添加摄像头。
2. 给需要告警的摄像头添加区域和启用规则。
3. 在 8090 应用运行时配置。
4. 重新注册人员和人脸。
5. 触发一条测试事件，确认 8090 证据页能看到新证据包。

## 6. UOS 常见问题

如果 `docker compose` 不存在：

```text
安装 Docker Compose v2 plugin；不要改用旧 docker-compose v1。
```

如果 `docker run --gpus all ... nvidia-smi` 失败：

```text
先修 NVIDIA driver 和 NVIDIA Container Toolkit。项目容器启动成功不代表 GPU 推理可用。
```

如果 `ghcr.io` 拉取失败：

```text
在可联网机器提前 docker pull/build/save，目标 UOS 机器 docker load 后再启动。
```

如果 8090 起不来：

```bash
ss -ltnp | grep -E ':8090|:8098|:6396|:18080|:18081|:5439'
docker logs --tail 200 video-analytics-midterm-api
docker logs --tail 200 video-analytics-midterm-evidence-viewer
```

如果 Savant 容器反复重启或无推理输出：

```bash
nvidia-smi
docker logs --tail 200 video-analytics-midterm-savant
docker logs --tail 200 video-analytics-midterm-analysis-forwarder
```

如果模型缺失，确认这些文件存在：

```text
/data/video-analytics/models/yolo26_pose/yolo26_pose.onnx
/data/video-analytics/models/yolov8_face/yolov8n-face.onnx
/data/video-analytics/models/yolov8_face.onnx
/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx
```

## 7. 双 4090 后续准备

普通迁移不需要 `models-savant-b`。只有后续启用 `dual-4090-two-source` profile 时，在
目标机执行：

```bash
cd /home/user/video-analytics
bash scripts/runtime/prepare_dual_4090_savant_b_model_cache.sh
MIDTERM_COMPOSE_PROFILES=dual-4090-two-source bash scripts/midterm_start.sh --local-postgres
```

不要从源机器拷贝旧的 `models-savant-b` 或旧 `.engine` 缓存到 UOS 目标机。

## 8. 官方参考

宿主机前置环境以这些官方文档为准，UOS 现场脚本应先在同版本测试机验证后再固化：

```text
Docker Debian install:
https://docs.docker.com/engine/install/debian/

Docker Compose plugin install:
https://docs.docker.com/compose/install/linux/

NVIDIA Container Toolkit install:
https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
```
