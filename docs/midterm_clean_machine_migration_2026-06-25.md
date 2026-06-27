# Midterm 新机器干净迁移说明

日期：2026-06-25

本文记录当前 midterm 系统迁移到新机器时的“干净启动”方案。这里的干净启动指：
只迁移代码和模型，不迁移旧数据库、旧证据、旧 Replay 缓存、Redis 队列或历史诊断
产物。新机器启动后，通过 8090 页面重新添加摄像头、注册人员/人脸、管理运行时。

## 1. 干净迁移只需要拷贝什么

必须拷贝：

```text
/home/user/video-analytics        # 当前代码快照
/data/video-analytics/models      # 模型 ONNX 和必要模型配置
```

其中模型目录至少需要：

```text
/data/video-analytics/models/yolo26_pose/yolo26_pose.onnx
/data/video-analytics/models/yolov8_face/yolov8n-face.onnx
/data/video-analytics/models/yolov8_face.onnx
/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx
```

`yolov8_face.onnx` 可以是指向 `yolov8_face/yolov8n-face.onnx` 的软链接。启动脚本
会在目标机器自动补这个软链接。

不迁移：

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
/data/video-analytics/replay-midterm/*
/data/video-analytics/replay-midterm-a/*
/data/video-analytics/replay-midterm-b/*
Redis 数据
PostgreSQL 旧数据
```

这些路径在新机器只需要创建空目录。`scripts/midterm_start.sh` 和
`scripts/midterm_deploy_clean.sh` 都会创建这些空目录。

## 2. 为什么有 3 个 replay-midterm 目录

当前 compose 同时支持默认单 Replay 和双 shard profile：

```text
/data/video-analytics/replay-midterm    # 默认 replay-service 使用
/data/video-analytics/replay-midterm-a  # dual-replay-shards / dual-4090 A shard
/data/video-analytics/replay-midterm-b  # dual-replay-shards / dual-4090 B shard
```

这些目录是 Replay service 的 RocksDB 存储目录，保存实时视频帧缓存和索引。干净迁移
不需要旧缓存，所以不要拷贝里面的 `.sst`、`.log`、`MANIFEST` 等文件。

默认启动只使用 `replay-midterm`。`replay-midterm-a` 和 `replay-midterm-b` 是为双
Replay shard / 双 4090 方案预留的空目录。

## 3. downloads 是什么

`/data/video-analytics/downloads` 是 Savant 的 `DOWNLOAD_PATH=/downloads` 挂载点，
用于框架或运行过程中的下载/缓存。当前机器这个目录为空，因此干净迁移不需要拷贝
它的内容。

目标机器只需要存在这个空目录。

## 4. models-savant-b 是什么

`/data/video-analytics/models-savant-b` 只在启用 `dual-4090-two-source` profile 时给
第二个 Savant 实例使用。它的目的不是保存不同模型，而是避免两个 Savant 实例同时
读写同一个 `/models` 工作目录和 TensorRT engine/cache 文件。

干净迁移默认不拷贝 `models-savant-b`。

如果目标机器后续要跑双 4090 profile，在目标机器上执行：

```bash
bash scripts/runtime/prepare_dual_4090_savant_b_model_cache.sh
```

这个脚本会从 `/data/video-analytics/models` 复制模型到
`/data/video-analytics/models-savant-b`，并排除旧 `.engine` 文件，让目标机器按自己的
GPU/driver/TensorRT 环境重新生成 engine。

## 5. artifacts 是什么

`/data/video-analytics/artifacts` 是诊断和验证产物目录，不是业务运行所需数据。当前
常见内容包括：

```text
runtime doctor summary
性能压测报告
smoke/test 输出
storage maintenance 审计记录
debug dump
```

干净迁移不拷贝历史 artifacts。目标机器创建空目录即可。

## 6. media 是什么

`/data/video-analytics/media` 是运行时媒体根目录。里面包含证据、截图、人脸上传文件、
注册 crop、Replay 输出等。

干净迁移不拷贝历史 media 内容。新机器启动后，系统会重新写入：

```text
media/evidence                         # 新证据包
media/replay-sink-output/midterm        # 新 Replay job 输出
media/midterm-snapshots                 # 新截图/中间图
media/face_uploads                      # 8090 新上传的人脸原图
media/face_registration                 # 8090 新注册的人脸保留 crop
media/debug                             # 运行时 debug 输出
```

## 7. 人脸注册实际存在哪里

8090 页面注册人脸时，数据分三层：

1. 上传原图：

```text
/data/video-analytics/media/face_uploads
```

2. 注册 crop/保留图：

```text
/data/video-analytics/media/face_registration
```

3. 真正用于识别的人员记录和 AdaFace 512 维向量：

```text
PostgreSQL persons
PostgreSQL person_gallery_embeddings
```

也就是说，仅拷贝图片文件不能恢复人脸库；真正可匹配的人脸库依赖
`person_gallery_embeddings.embedding` 里的向量。干净迁移不迁 PostgreSQL，因此目标
机器需要在 8090 页面重新注册人员和人脸。

## 8. 当前 Reese / Finch 在哪里

当前机器上 8090 API 查到的两个 active 人员是：

```text
Reese
person_id: 5
external_person_id: demo:f4_3:reese
gallery_id: 4
source_image_path: /data/video-analytics/media/face-registration/reese.jpg

Finch
person_id: 6
external_person_id: demo:f4_3:finch
gallery_id: 5
source_image_path: /data/video-analytics/media/face-registration/finch.jpg
```

注意这里是历史目录 `media/face-registration`，带横线。当前 8090 新注册默认使用
`media/face_registration`，带下划线。

如果目标机器不迁旧数据，不需要带 Reese/Finch。到新机器后从 8090 页面重新注册即可。

## 9. 打包和部署脚本

源机器打包：

```bash
bash scripts/midterm_package_clean.sh
```

输出类似：

```text
/data/video-analytics/artifacts/migrations/midterm-clean-YYYYMMDDTHHMMSSZ.tgz
/data/video-analytics/artifacts/migrations/midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh
```

拷贝这两个文件到目标机器，例如：

```bash
scp /data/video-analytics/artifacts/migrations/midterm-clean-*.tgz user@TARGET:/tmp/
scp /data/video-analytics/artifacts/migrations/midterm-clean-*_deploy_clean.sh user@TARGET:/tmp/
```

目标机器部署：

```bash
cd /tmp
bash midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh midterm-clean-YYYYMMDDTHHMMSSZ.tgz
```

如果目标机器使用 compose 内置 PostgreSQL：

```bash
bash midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh \
  midterm-clean-YYYYMMDDTHHMMSSZ.tgz \
  --local-postgres
```

如果只想解包和校验，不启动服务：

```bash
bash midterm-clean-YYYYMMDDTHHMMSSZ_deploy_clean.sh \
  midterm-clean-YYYYMMDDTHHMMSSZ.tgz \
  --no-start
```

部署成功后，浏览器访问：

```text
http://127.0.0.1:8090/operator
```

后续摄像头、人脸、证据、存储维护、受控运行时重启和推理 FPS/interval
性能档位都在 8090 页面管理。

## 10. 批处理参数说明

当前模型推理 batch 不是全部“只改 env 就生效”。

8090 当前可以保存并应用 Forwarder/Savant FPS、模型 infer interval 和
`BATCHED_PUSH_TIMEOUT`；应用时会按差异重建 `analysis-forwarder` 和/或
`savant-security`，并复用 evidence restart guard。模型 batch 和
`MAX_PARALLEL_STREAMS` 仍建议通过 env 加压测验证，不应在迁移时顺手调大。

可以直接通过 env 调整的是：

```text
MAX_PARALLEL_STREAMS
BATCH_SIZE
POSE_BATCH_SIZE
FACE_DETECTOR_BATCH_SIZE
FACE_EMBEDDING_BATCH_SIZE
BATCHED_PUSH_TIMEOUT
ANALYSIS_FPS
MAX_FPS
POSE_INFER_INTERVAL
FACE_INFER_INTERVAL
FACE_EMBEDDING_INFER_INTERVAL
SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS
SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS
SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE
MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE
MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S
MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG
```

当前 compose 中 batch 已不是写死值，默认值在 `infra/env/midterm.env`：

```text
BATCH_SIZE=1
POSE_BATCH_SIZE=1
FACE_DETECTOR_BATCH_SIZE=1
FACE_EMBEDDING_BATCH_SIZE=16
MAX_PARALLEL_STREAMS=4
BATCHED_PUSH_TIMEOUT=40000
SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS=50
SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS=50
SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE=1024
MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=2
MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S=180
MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG=200
```

其中 YOLOv8-Face detector 当前按静态 batch=1 使用。模型 batch 调大涉及 TensorRT
engine、显存、延迟和模型约束，应该作为单独性能任务验证，不应在迁移时顺手修改。
Redis exporter 已使用有界异步 writer，以上 Redis 参数只控制写入线程和队列的保护边界。
