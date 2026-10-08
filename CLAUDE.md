# CLAUDE.md

本文件是本仓库当前工程工作的入口说明。历史阶段文档仍保留在 `docs/`
和 archive 目录中，但当前项目机器部署不再使用历史代号入口。

## 当前部署版本

当前部署入口是中期项目版本：

```text
infra/docker-compose.midterm.yml
infra/env/midterm.env
modules/savant_replay/config.midterm.json
modules/savant_security/config/cameras.midterm.yml
modules/savant_security/module.yml
```

运行命令：

```bash
bash scripts/midterm_start.sh
```

迁移到新机器后的整体启动入口也是 `scripts/midterm_start.sh`。启动完成后，摄像头、
人员/人脸、证据、存储维护和受控运行时重启都从 8090 operator portal 管理；
不要把内部 API 的 `8000` 端口作为客户入口发布。

默认 compose project 和容器名前缀是 `video-analytics-midterm`，默认
`SOURCE_ID` 是 `primary_rtsp`。

## 当前链路

```text
RTSP source
  -> Replay storage
  -> analysis-forwarder sampled analysis path
  -> Savant inference
  -> Redis annotations/events
  -> event-worker / face-worker
  -> clip-worker Replay job
  -> video-file-sink raw clip
  -> media-worker evidence bundle
  -> 8090 operator portal (`/#evidence`)
```

证据包以 Replay/video-file-sink 的 `raw_clip.mov` 为原始视频，以
`annotations.frame_cache.identity.jsonl` 为 8090 operator 证据页叠框 sidecar。当前部署不
生成 annotated video clip。`analysis-forwarder` 只限流分析分支；full-rate evidence
仍来自 Replay。

## 部署面规则

1. 当前部署只使用 `midterm` 文件。
2. 不要把 archive 中的历史代号 compose/env/config 当作当前入口。
3. 不要在根 `infra/` 增加新的阶段代号 compose。
4. 新的项目机器配置应扩展 `infra/docker-compose.midterm.yml` 和
   `infra/env/midterm.env`，或者在明确需求下创建中性命名的部署入口。
5. evidence 元数据使用 `EVIDENCE_VERSION=midterm` 和
   `EVIDENCE_SCHEMA_VERSION=2.0-midterm`。
6. 当前部署关闭 legacy metadata 字段：
   `EVIDENCE_INCLUDE_LEGACY_METADATA_FIELDS=false`。
7. 客户入口只暴露 8090 operator portal；内部 API 使用 compose 网络端口
   `8000`，不要发布宿主 8000 端口。
8. 中期内部 API 使用 `services/api/Dockerfile.face-runtime`，复用
   `video-analytics-midterm-face-worker:latest` 中已有的 ORT/OpenCV/Numpy
   人脸注册运行层。

历史入口保存在：

```text
infra/archive/phase-only/
modules/savant_replay/archive/phase-only/
modules/savant_security/config/archive/phase-only/
```

## 架构原则

Savant 是实时视频主干，负责 GPU 解码、YOLO26-pose、nvtracker、YOLOv8-Face、
AdaFace、轻量规则判断和 Redis 输出。Savant module 内不要做阻塞数据库写入、
慢 HTTP 请求、重型业务查询、截图/剪辑生成或大规模向量搜索。

实时链路处理最新帧，不追求处理每一帧。发生压力过载时优先降低二级模型频率、
降低 pose FPS、丢弃过期帧，并保留主行为检测链路。
当前 midterm 拓扑已把 Replay `out_stream` 接到 `analysis-forwarder`，再由
forwarder 写入 Savant。不要把 forwarder 采样分支当作 evidence 原始视频来源。

重点人员布控、轨迹追踪、一键找人复用同一条 Face Intelligence Pipeline：

```text
YOLO26-pose + nvtracker
  -> YOLOv8-Face full-frame primary
  -> face-person association
  -> face quality / throttle
  -> AdaFace embedding
  -> Redis security.face_observations
  -> face-worker
  -> PostgreSQL + pgvector
```

## Replay 和证据对齐原则

1. 生产默认是 single-ingestion：同一路摄像头只接入一次。
2. Replay 存储、analysis-forwarder 和 Savant 推理必须使用同一帧谱系。
3. 告警帧 `frame_uuid` 是视觉绑定身份锚点。
4. Replay job 使用同一帧谱系中的 `keyframe_uuid` 或
   `previous_keyframe_uuid` 作为 `anchor_keyframe`。
5. `frame_pts` 用于窗口计算和裁剪，不替代 UUID 身份锚点。
6. `event_ts_ms`、Redis stream id、PostgreSQL `created_at` 只作业务/诊断时间，
   不作媒体锚点。

## 开发规则

1. 修改规则算法前，先添加或更新 harness 测试。
2. 不允许在 Savant `process_frame` 中直接做同步数据库或 HTTP 操作。
3. 不允许把摄像头 ROI、阈值、RTSP 地址硬编码在算法逻辑里。
4. 所有事件必须符合统一 `SecurityEvent` 结构。
5. 所有 worker 应可重复消费或具备幂等处理策略。
6. 保持部署入口中性命名；历史阶段名只能留在 archive/history 语境中。
7. 功能代码变更后的运行验证，默认只对受影响服务执行
   `docker compose -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate --no-deps <service>`。
   不要为了挂载最新代码新增本地 compose override；只有 Dockerfile、requirements、
   base image、系统依赖变化或镜像缺失时才 rebuild，并在结果中说明原因。

## 开发期文档规则

涉及运行链路、证据生成、性能、部署、数据模型或 8090 行为的修改，不能只
留下代码 diff。开发期间必须同步维护文档：

1. 修复前确认或创建目标文档，优先使用 `specs/` 中的当前目标；临时排查记录
   放在 `docs/`，文件名包含日期和主题。
2. 文档必须写清现象、影响范围、根因判断、非目标、配置开关、数据/状态语义、
   回滚点和剩余风险。
3. 运行态问题必须记录可复现或可验证的证据：DB/Redis 查询、关键日志、容器
   restart/status、生成的 evidence 路径、8090/API 状态、测试命令和结果。
4. 如果一个问题拆成多类，例如“没有 upstream event”和“event 已产生但
   evidence 失败”，文档必须明确边界，不能把不同根因混在一个修复结论里。
5. 每次验证通过并 commit 后，目标 spec 或 docs 需要补充实现状态、commit hash、
   验证时间窗口和仍未完成的后续项。
6. 不要把聊天里的结论当作唯一记录；重要发现必须固化到仓库文档后再作为后续
   goal 的依据。

## 验证命令

当前部署入口的最小静态验证：

```bash
docker compose -f infra/docker-compose.midterm.yml config
python -m pytest -q harness/tests/test_midterm_deployment_contract.py
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/runtime/doctor_midterm.sh
git diff --check
```

运行时验证前先确认端口冲突。旧历史容器如果仍在运行，可能占用 `6396`、
`8090`、`8098`、`18080` 或 `18081`；不要在未确认的情况下直接 `down -v` 或删除数据卷。

长期产能验证必须按 `specs/16_dual_path_30x2_t4_production_optimization.md`
执行。当前开发机如果不是真实 T4 且不足 30 路输入，只能跑 Phase 2 readiness
脚本并记录失败原因，不能宣称 `PASS_PHASE2_SINGLE_T4_30`。
