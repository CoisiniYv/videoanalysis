---
type: deployment-note
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - deployment
  - migration
  - uos
---

# 部署与迁移

## 当前部署入口

- 启动：`scripts/midterm_start.sh`
- Compose：`infra/docker-compose.midterm.yml`
- Env：`infra/env/midterm.env`
- 操作台：`http://127.0.0.1:8090/operator`
- 打包：`scripts/midterm_package_clean.sh`
- 部署：`scripts/midterm_deploy_clean.sh`

## 干净迁移原则

迁移：

- `/home/user/video-analytics` repo snapshot；
- `/data/video-analytics/models`；
- 可选 Docker images 离线包。

不迁移：

- old PostgreSQL；
- Redis；
- Replay RocksDB；
- evidence media；
- historical artifacts；
- downloads；
- `models-savant-b`；
- 旧人脸库。

原因：

- 新机器不需要旧运行态；
- Replay / Redis / evidence 是运行产生的数据；
- `models-savant-b` 可由同一模型目录或运行时生成策略处理；
- 人脸库有效数据在 PostgreSQL，干净迁移后需要重新注册。

## 离线包

打包：

```bash
bash scripts/midterm_package_clean.sh --include-images
```

输出包含：

- `repo.tgz`
- `models.tgz`
- `images.tar`
- `image_list.txt`
- `deploy_clean.sh`
- `manifest.txt`
- `SHA256SUMS`

部署脚本行为：

- 校验 SHA256；
- 解压 repo 和 models；
- 若存在 `images.tar` 且未指定 `--skip-image-load`，自动 `docker load`；
- 若加载了 images，启动时追加 `--no-build`。

## UOS 目标机要求

宿主机必须先具备：

- Docker Engine；
- Docker Compose v2 plugin；
- NVIDIA driver；
- NVIDIA Container Toolkit；
- tar / gzip / curl / ss；
- 可用 GPU；
- 8090、8098、6396、18080、18081 空闲；
- 如果使用 local PostgreSQL profile，5439 空闲。

应用包不负责安装这些宿主依赖。

## local PostgreSQL

默认 worker/API 指向宿主 PostgreSQL `host.docker.internal:5432`。干净新机器推荐可启用 compose
`local-postgres` profile，使用空库。

需要注意：

- 旧业务数据不会自动迁移；
- 人脸库需要重新注册；
- 如果要迁人员/人脸/历史 evidence，需要另写业务数据迁移和校验方案。

## 迁移后检查

至少检查：

```bash
docker compose version
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
bash scripts/midterm_health.sh
```

8090 侧检查：

- 摄像头可添加；
- RTSP 可启停；
- 人员/人脸可注册；
- 证据列表可查询；
- runtime overview 不出现核心服务异常。
