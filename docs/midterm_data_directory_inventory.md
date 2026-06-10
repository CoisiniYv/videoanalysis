# Midterm /data 目录核对与清理记录

Date: 2026-06-10

本记录按当前代码和配置核对 `/data/video-analytics`。当前可部署主线是
midterm，不再以历史 C1/C2/P1 compose 作为运行入口。

## 核对依据

- 当前主线：`docs/current_mainline_status.md`
- midterm 部署入口：`docs/midterm_deployment.md`
- 8090 对接状态：`docs/midterm_8090_port_integration.md`
- 存储维护设计：`docs/storage_maintenance_delete_plan.md`
- 当前 compose：`infra/docker-compose.midterm.yml`
- 当前 env：`infra/env/midterm.env`
- 当前 Replay 配置：`modules/savant_replay/config.midterm.json`
- 当前摄像头配置：`modules/savant_security/config/cameras.midterm.yml`

## 当前必须保留的目录

| 宿主机目录 | 用途 | 依据 |
|---|---|---|
| `/data/video-analytics/media` | midterm 媒体总根；API、Savant、face-worker、media-worker、video-file-sink 都挂载这里 | `infra/docker-compose.midterm.yml` |
| `/data/video-analytics/media/evidence` | 8090 evidence-viewer 只读证据根，flat bundle 布局：`evidence/{event_id}` | `EVIDENCE_ROOT=/evidence`，`EVIDENCE_OUTPUT_DIR=/media/evidence` |
| `/data/video-analytics/media/replay-sink-output/midterm` | 当前 video-file-sink 原始 Replay clip 输出 | `DIR_LOCATION=/media/replay-sink-output/midterm/...` |
| `/data/video-analytics/media/midterm-snapshots` | 当前 media-worker 快照和 annotated 输出根 | `SNAPSHOT_OUTPUT_DIR=/media/midterm-snapshots` |
| `/data/video-analytics/media/face_uploads` | 8090 人脸上传暂存 | `FACE_UPLOAD_ROOT` |
| `/data/video-analytics/media/face_registration` | 8090 人脸注册图根 | `FACE_REGISTRATION_ROOT` |
| `/data/video-analytics/media/face-registration` | 历史兼容目录；现有数据可能仍有 URL/DB 引用 | 运行数据兼容 |
| `/data/video-analytics/media/.trash` | 存储维护软删除回收站根；当前可为空 | `storage_maintenance.py` |
| `/data/video-analytics/media/debug` | 当前调试 sink 根；可清空内容但保留目录 | `REPLAY_SAVANT_FRAME_DUMP_ROOT` / maintenance summary |
| `/data/video-analytics/models` | Savant/API/face-worker 模型根 | `MODEL_PATH=/models`，`YOLOV8_FACE_ONNX`，`ADAFACE_ONNX` |
| `/data/video-analytics/downloads` | Savant download cache 根 | `DOWNLOAD_PATH=/downloads` |
| `/data/video-analytics/replay-midterm` | 当前 Replay RocksDB | replay-service `DB_PATH=/opt/rocksdb` |
| `/data/video-analytics/artifacts` | 当前运行诊断/维护输出总根 | Savant 和 API 挂载 |
| `/data/video-analytics/artifacts/maintenance` | 存储维护审计/作业输出 | `STORAGE_MAINTENANCE_ARTIFACTS_ROOT` |
| `/data/video-analytics/artifacts/midterm` | midterm 专属诊断输出 | 当前 artifacts 命名 |
| `/data/video-analytics/artifacts/same_frame` | 当前 Savant same-frame 调试输出 | `modules/savant_security/module.yml` |

条件保留：

- `/data/video-analytics/postgres-midterm` 只在使用
  `docker compose -f infra/docker-compose.midterm.yml --profile local-postgres`
  启动本地 midterm PostgreSQL 时需要。当前运行栈使用的是 `phase0-postgres`
  Docker 容器/卷，不依赖这个宿主机目录。

## 本次已删除或清空的历史内容

根目录下已删除：

- `/data/video-analytics/archive`
- `/data/video-analytics/logs`
- `/data/video-analytics/postgres`
- `/data/video-analytics/postgres-p1c-rtsp-replay`
- `/data/video-analytics/postgres-phase3b`
- `/data/video-analytics/redis`
- `/data/video-analytics/replay`
- `/data/video-analytics/replay-c1-official-replay-dev`
- `/data/video-analytics/replay-d1-rtsp-15min`
- `/data/video-analytics/replay-p1a-inline`
- `/data/video-analytics/replay-p1b-rtsp`
- `/data/video-analytics/replay-p1c-rtsp`

`media` 下已删除：

- `_archive`
- `c1-official-metadata`
- `c1-official-savant-output`
- `c1e-snapshots`
- `c2-post-savant-replay-fps-probe`
- `c2-post-savant-replay-poc`
- `c2-replay-first-snapshots`
- `events`
- `evidence_audit`
- `p1b-replay-manual-sink`
- `p1b-rtsp-replay-manual-sink`
- `rtsp-ring`
- `replay-sink-output/c1e`
- `replay-sink-output/c2-replay-first`
- `replay-sink-output/p1c-rtsp`

已清空但保留目录：

- `/data/video-analytics/media/.trash`
- `/data/video-analytics/media/debug`

`artifacts` 下已删除所有历史 C1/C2 阶段子目录和 `repo_hygiene`，
仅保留：

- `maintenance`
- `midterm`
- `same_frame`

## 当前仍残留但不属于 midterm 的目录

以下目录不是当前 midterm 运行依赖，但文件由 root 或其他系统用户创建，当前
shell 没有无密码 sudo 权限，无法删除：

| 目录 | 当前核对结果 |
|---|---|
| `/data/video-analytics/postgres-c1-official-replay-dev` | 历史 PostgreSQL 目录，当前用户无权限读取/删除 |
| `/data/video-analytics/replay-c2-post-savant-fps-only-probe` | 历史 C2 Replay RocksDB，约 1.1G |
| `/data/video-analytics/replay-c2-post-savant-fps-probe` | 历史 C2 Replay RocksDB，约 112K |
| `/data/video-analytics/replay-c2-post-savant-replay-poc` | 历史 C2 Replay RocksDB，约 97M |
| `/data/video-analytics/replay-c2-replay-first-dev` | 历史 C2 Replay RocksDB，约 539M |

如需彻底清理，需要在宿主机上用有权限的账号执行删除；删除前仍应确认没有
运行历史 C2/C1 compose。

## 清理后的实际状态

清理前 `/data/video-analytics` 约 68G。清理后约 22G。

当前一级目录：

```text
artifacts
downloads
media
models
postgres-c1-official-replay-dev
replay-c2-post-savant-fps-only-probe
replay-c2-post-savant-fps-probe
replay-c2-post-savant-replay-poc
replay-c2-replay-first-dev
replay-midterm
```

当前 `media` 一级目录：

```text
.trash
debug
evidence
face-registration
face_registration
face_uploads
midterm-snapshots
replay-sink-output
```

当前 `artifacts` 一级目录：

```text
maintenance
midterm
same_frame
```

## 清理后验证

8090 和运行栈清理后仍正常：

```bash
curl --noproxy '*' http://0.0.0.0:8090/health
# {"status":"ok","evidence_root":"/evidence","read_only":true}

curl --noproxy '*' 'http://0.0.0.0:8090/api/bundles?limit=1'
# 可返回 bundle 列表；当前 total=2564

curl --noproxy '*' 'http://0.0.0.0:8090/api/v1/maintenance/storage/summary'
# 可通过 8090 proxy 返回 API 统计；
# evidence.bundle_count=2564，trash.bytes=0
```

容器状态：

- `video-analytics-midterm-savant`：`running healthy`
- `video-analytics-midterm-evidence-viewer`：`running`
- `video-analytics-midterm-api`：`running`
- `video-analytics-midterm-media-worker`：`running`
- `video-analytics-midterm-video-file-sink`：`running`
- `video-analytics-midterm-replay-service`：`running`
