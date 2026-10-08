# Video Analytics Platform

多路实时视频分析系统，基于 Savant + NVIDIA DeepStream + TensorRT。

## 当前部署版本

项目机器部署使用中期项目版本，不使用历史代号入口。

```bash
bash scripts/midterm_start.sh
```

`scripts/midterm_start.sh` 是迁移到新机器后的整体启动入口。它会检查
Docker/GPU 基础环境，创建必要的 `/data/video-analytics` 运行目录，验证关键模型
资产，先构建 `face-worker` 基础镜像再构建 API 镜像，并统一使用
`infra/env/midterm.env` 渲染 compose。

启动完成后，日常管理通过 `http://127.0.0.1:8090/operator` 完成。内部 API 仍只在
compose 网络内监听 `8000`，不要作为单独客户入口发布。

当前部署入口：

| Purpose | File |
|---|---|
| Compose | `infra/docker-compose.midterm.yml` |
| Env | `infra/env/midterm.env` |
| Replay config | `modules/savant_replay/config.midterm.json` |
| Camera config | `modules/savant_security/config/cameras.midterm.yml` |
| Savant module | `modules/savant_security/module.yml` |

默认项目名和容器名前缀是 `video-analytics-midterm`，默认 source id 是
`primary_rtsp`。

## 当前链路

```text
RTSP source
  -> replay-service storage
  -> analysis-forwarder sampled analysis path
  -> savant-security inference
  -> Redis / PostgreSQL workers
  -> clip-worker Replay job
  -> video-file-sink raw clip
  -> media-worker JSONL sidecar evidence
  -> 8090 operator portal (`/#evidence`)
```

`replay-service` remains the full-rate evidence authority. The
`analysis-forwarder` only samples the analysis branch before Savant and exposes
metrics on host port `18081`.

证据包默认包含：

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.frame_cache.identity.json`
- `metadata.json`

## 默认端口

- Redis: `6396`
- Replay API: `8098`
- Savant metrics: `18080`
- Analysis-forwarder metrics: `18081`
- Operator portal / evidence viewer: `8090`
- Internal API service: compose-network port `8000` only, reached through 8090
- Optional local PostgreSQL profile: `5439`

The internal API image for the midterm compose uses
`services/api/Dockerfile.face-runtime`, which inherits from the local
`video-analytics-midterm-face-worker:latest` image to reuse the existing
ONNX Runtime/OpenCV/Numpy layer for face registration.

Workers 默认使用宿主 PostgreSQL：

```text
postgresql://video:video@host.docker.internal:5432/video_analytics
```

## 当前运行参数

`infra/env/midterm.env` 和 `infra/docker-compose.midterm.yml` 当前固定了中期
部署的主要阈值：

- `MAX_FPS_CONTROL=false`
- `INGRESS_FPS_GATE_ENABLED=true`
- `ANALYSIS_FPS=8/1`（analysis-forwarder 默认）
- `MAX_FPS=8/1`
- `MIN_FPS=2/1`
- `POSE_INFER_INTERVAL=1`
- `POSE_CONFIDENCE_THRESHOLD=0.50`
- `POSE_KEYPOINT_THRESHOLD=0.35`
- `POSE_SELECTOR_CONFIDENCE_THRESHOLD=0.50`
- `POSE_SELECTOR_NMS_IOU_THRESHOLD=0.50`
- `POSE_MIN_WIDTH=60`
- `POSE_MIN_HEIGHT=100`
- `FACE_CONFIDENCE_THRESHOLD=0.50`
- `WATCHLIST_THRESHOLD=0.60`

## 文档

- 文档知识网络：`docs/project_knowledge_network.md`
- 当前部署说明：`docs/midterm_deployment.md`
- Compose 清单：`docs/compose_inventory.md`
- 当前状态：`docs/current_mainline_status.md`
- 当前进度总览：`docs/project_current_progress_summary.md`
- 开发入口和文档规则：`CLAUDE.md`
- Dual-path / T4 产能计划：`specs/16_dual_path_30x2_t4_production_optimization.md`
- Evidence 实时对齐修复计划：`specs/17_clip_worker_evidence_realtime_alignment_fix.md`
- Evidence proof window 修复记录：
  `docs/midterm_post_savant_evidence_proof_windows_2026-06-15.md`
- 历史阶段/实验文档：各目录下的 `archive/phase-only/`

历史代号入口已归档到：

- `infra/archive/phase-only/20260609/`
- `modules/savant_replay/archive/phase-only/20260609/`
- `modules/savant_security/config/archive/phase-only/20260609/`
- `docs/archive/phase-only/20260610/`
- `harness/tests/archive/phase-only/20260610/`
- `scripts/*/archive/phase-only/20260610/`
- `services/archive/phase-only/20260610/`

这些归档文件只用于追溯和历史回归，不作为项目机器部署入口。
