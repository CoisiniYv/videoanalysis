---
type: deployment-note
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - deployment
  - migration
  - uos
---

# 部署与迁移

## 当前部署入口

- 唯一支持的启动入口：`scripts/midterm_start.sh`
- Compose：`infra/docker-compose.midterm.yml`
- Env：`infra/env/midterm.env`
- 快速盘/轨迹挂载：`infra/midterm-storage.override.yml`
- 8090 双分支预创建：`infra/operator-dual-runtime.override.yml`
- 操作台：`http://127.0.0.1:8090/operator`
- 打包：`scripts/midterm_package_clean.sh`
- 部署：`scripts/midterm_deploy_clean.sh`

不要把裸 `docker compose ... up` 当成等价启动入口：它不会完整表达 storage override、
Savant B 模型缓存准备及 8090 管理的双分支容器预创建。

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
- `models-savant-b` 运行缓存；
- 旧人脸库。

原因：

- 新机器不需要旧运行态；
- Replay / Redis / evidence 是运行产生的数据；
- `models-savant-b` 是由部署脚本从模型目录准备的独立可写 engine cache；
- PostgreSQL 是人员、图库、事件和 evidence 索引的事实源；本页的“干净迁移”明确不迁
  旧业务库，因此目标机需要重新注册人员/人脸；
- Qdrant 是可选 derived serving，不是当前默认后端，也不应替代 PostgreSQL 备份。

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

当前启动脚本还会构建完整预设需要的 ROI AdaFace 与 rolling-cache sink，并以
`--no-start` 预创建 A/B、MPS、ROI、rolling 等容器；预创建不会主动启用摄像头。

## UOS 目标机要求

宿主机必须先具备：

- Docker Engine；
- Docker Compose v2 plugin；
- NVIDIA driver；
- NVIDIA Container Toolkit；
- tar / gzip / curl / ss；
- 可用 GPU；
- 8090、8098、6396、18080、18081、18180–18187 中实际启用的端口空闲；
- 如果使用 local PostgreSQL profile，5439 空闲。

应用包不负责安装这些宿主依赖。

## local PostgreSQL

默认 worker/API 指向宿主 PostgreSQL `host.docker.internal:5432`。干净新机器推荐可启用 compose
`local-postgres` profile，使用空库。

需要注意：

- 旧业务数据不会自动迁移；
- 人脸库需要重新注册；
- 如果要迁人员/人脸/历史 evidence，需要另写业务数据迁移和校验方案。

## 数据库迁移

部署 revision 对应的 `db/migrations/*.sql` 必须按顺序应用：

- 029/030：materialization lifecycle v2 与队列索引；
- 031：Replay slot/create fencing；
- 032：cleanup recovery 与 algorithm cooldown 热路径索引。

032 使用 `CREATE INDEX CONCURRENTLY`，必须以 autocommit、在非压力窗口执行，不能包在
事务中。数据库 schema、worker 镜像/挂载代码和文档必须来自同一 revision。

## 8090 完整双分支

当前操作台提供两个命名预设：

| 预设 | 路数/分支 | 分析 FPS | ROI AdaFace | MPS | rolling prefill/retention |
| --- | --- | ---: | ---: | --- | --- |
| `production_t4_40` | 40，20/20 | 4 | batch 16 | 45/45/10 | 25s/600s |
| `local_4090_60` | 60，30/30 | 8 | batch 16 | 关闭 | 25s/600s |

启动顺序是：保存预设和分片策略、选择精确 source、调用后台 apply、轮询 apply-status。
完整预设的主证据链是 full-rate raw fanout -> rolling-cache -> media-worker；
`clip-worker -> Replay job -> video-file-sink` 是单分支/兼容链。

apply-status 写入持久状态文件，浏览器刷新后可恢复；执行本身仍是 API 进程内线程，API
重启会中断任务并把遗留 running 状态标为 failed。4090 60 路在双时间域改造前有通过
记录，当前 revision 仍需重跑；T4 40 路、4 FPS 是当前 revision 的已验证基线。

## 迁移后检查

至少检查：

```bash
docker compose version
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
bash scripts/midterm_health.sh
```

还要检查：

- `/api/v1/runtime/overview`、`/api/v1/runtime/latency` 和 topology apply-status；
- A/B source 数、effective FPS、queue/send failure；
- ROI AdaFace、person、face consumer group lag/pending；
- rolling ready、segment coverage、retention 和 5+5 输出；
- evidence active/failed/expired/fallback 以及 DB-backed list/detail/timeline/annotation。

`midterm_health.sh` 的固定清单仍包含 legacy `source-adapter`，且未列出默认的
`person-observation-worker`；在脚本修正前，不能只凭该脚本判断完整双分支成功或失败。

8090 侧检查：

- 摄像头可添加；
- RTSP 可启停；
- 人员/人脸可注册；
- 证据列表可查询；
- runtime overview 不出现核心服务异常。

8090“停止完整链路”会停止 source 和 A/B 推理链，并保留 event/media/rolling 做有限
收尾；整栈停止使用 `scripts/midterm_stop.sh`。停止不默认删除持久数据。
