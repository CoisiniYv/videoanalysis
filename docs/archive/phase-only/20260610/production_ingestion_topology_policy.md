# Production Ingestion Topology Policy

Date: 2026-05-25
Status: **ACTIVE** — 所有 Claude Code 会话必须在涉及视频接入、Replay、bbox/snapshot 对齐的实现前阅读本文档。

---

## 1. 文档目的

本文档定义项目在生产环境中视频接入拓扑的硬性策略。它是 `CLAUDE.md` Section 9 的详细展开，是 `specs/01_architecture.md` 和 `specs/11_docker_communication_and_clip_design.md` 的补充。

**本文件是强制性策略文件，不是建议文件。**

---

## 2. 生产目标拓扑

```
RTSP camera
  -> Savant Source Adapter  (官方 adapter 容器)
    -> Replay Service        (RocksDB 缓存 + REST API)
      -> Savant Module       (GPU 推理)
        -> Redis Streams
          -> event-worker → PostgreSQL
          -> clip-worker → Replay REST API → video-file-sink
          -> media-worker → snapshot / annotated_snapshot / clip_path
```

核心原则：**Single-Ingestion — 每路摄像头只接入一次。**

Savant 和 Replay 消费**同一条帧流**，bbox 和 snapshot 天然来自同一帧。

## 3. 临时允许拓扑

以下拓扑在开发和 MVP 验证阶段**明确允许**，但附带严格限制：

### 3.1 同 RTSP 双消费者（Phase 3F0.3a 方案 B）

```
test.mp4 → ffmpeg-source → RTSP server
       → Savant (uridecodebin)
       → source-adapter (rtsp.sh) → Replay Service
```

**允许条件：**
- 仅用于 MVP 功能验证和开发测试。
- 不得在文档中标记为"生产就绪"。
- 不得声称 bbox/snapshot 帧级对齐。
- bbox overlay 只能用于调试，不能用于面向用户的报警界面。

**已知限制：**
- 两个 RTSP 消费者独立连接，存在亚秒级连接时间差异。
- 即使消费同一个 RTSP 流，也不能保证 Savant 和 source-adapter 收到的帧完全一致。
- Replay keyframe lookup 仍然 unbounded（`from_ns=None, to_ns=None`）。

### 3.2 禁用真实 bbox overlay（方案 D）

**允许条件：**
- 任何时候都可以启用此保守模式。
- 保留 label overlay（event_id、event_type、camera_id、confidence、timestamp）。
- 跳过 bbox 矩形绘制。
- 通过环境变量 `SAVANT_BBOX_OVERLAY_ENABLED=false` 控制。

## 4. 禁止拓扑

以下拓扑**严格禁止**在任何阶段引入：

### 4.1 双路独立拉流

```
禁止：
  RTSP camera → Savant (独立 RTSP 拉流)
  RTSP camera → Replay (独立 RTSP 拉流)
```

### 4.2 双文件独立循环

```
禁止：
  test.mp4 → ffmpeg-source → RTSP → Savant
  test.mp4 → source-adapter → ZMQ → Replay
```

这是当前 Phase 3B compose 的实际拓扑，仅在最早的 MVP 验证阶段可接受。Phase 3F0.2 诊断已证明此拓扑导致 bbox/snapshot 不对齐。

### 4.3 持续全量录制

```
禁止：
  60 路摄像头持续切片落盘
  无 cooldown / severity policy 的全量 media generation
  无限制事件全量 snapshot/clip 生成
```

## 5. Bbox Overlay 启用/禁用策略

### 5.1 什么时候必须关闭 bbox overlay

| 条件 | 原因 |
|---|---|
| 未建立 single-ingestion 拓扑 | 帧内容可能不对齐 |
| Timestamp-domain mapping 未完成 | Replay keyframe 定位不精确 |
| `frame_uuid` / `keyframe_uuid` / `frame_num` 未填充 | 无法验证帧级对齐 |
| 面向用户的报警界面 | bbox 可能误导操作员 |

### 5.2 什么时候可以开启 bbox overlay

| 条件 | 用途 |
|---|---|
| Single-ingestion 拓扑已建立 | 开发/调试 |
| Timestamp-domain mapping 已完成 | 开发/调试 |
| 仅限开发环境，非用户界面 | 开发/调试 |
| 环境变量 `SAVANT_BBOX_OVERLAY_ENABLED=true` | 开发/调试 |

### 5.3 什么时候可以标记"生产就绪"

**全部以下条件满足时：**
1. Single-ingestion 拓扑已部署。
2. Timestamp-domain mapping 已实施并验证。
3. `frame_uuid` / `keyframe_uuid` 在 event payload 中正确填充。
4. Replay keyframe lookup 使用 `from_ns`/`to_ns` 锚定到 event_ts_ms。
5. 诊断脚本在多个事件上验证 bbox 与 snapshot 帧级对齐。
6. 回归测试全部通过。

## 6. 未来实现路线

```
Phase 3F0.3a-3F0.4 (已完成诊断，未实施):
  - 同 RTSP 源临时改良
  - Timestamp-Domain Mapping
  - 不声称帧级对齐

Phase 3H / 3H.1 / 3H.2 (已完成 POC):
  - Official ZeroMQ Source Ingestion
  - Official Metadata Sink Output
  - Official Video File Sink Output
  - 单路 single-ingestion video + metadata 同帧流已验证
  - Continuous sink 已冻结，media POC 不再扩展

R0 (当前):
  - 项目现状盘点，路线校准，仓库卫生

R1 (下一阶段):
  - 主模块 / compose 收敛
  - savant_security 成为唯一主模块

B2 (行为规则补完):
  - loitering
  - crowd_gathering
  - fall (初版)

C1 (配置系统):
  - camera_zones / camera_rules API
  - ROI 配置
  - config 同步

F1-F3 (人脸链路 — 重新排回近期主线):
  - SCRFD_2.5G 人脸检测
  - ArcFace embedding + pgvector
  - 重点人员布控 / 一键找人

Phase 4+: 生产加固
  - 60 路性能
  - GPU 显存稳定
  - Prometheus / Grafana
```

注：Phase 3H ZMQ frame+metadata 流已作为未来生产拓扑技术依据保留，不再继续无限扩展 media POC。

## 7. 相关文档

| 文档 | 内容 |
|---|---|
| `CLAUDE.md` Section 9 | 硬约束规则（所有 Claude Code 会话必读） |
| `specs/01_architecture.md` Section 8 | Replay 报警录像目标架构 |
| `specs/11_docker_communication_and_clip_design.md` Section 23 | 生产 Single-Ingestion 约束 |
| `docs/phase3f0_2_diagnosis_report.md` | bbox/snapshot 对齐诊断完整报告 |
| `docs/phase3f0_3_topology_review.md` | 四种拓扑方案详细对比 |

## 8. 违规处理

如果任何实现、PR、或 Claude Code 会话中出现了违反本策略的行为：

1. **立即停止实施。**
2. **不要合并。**
3. **在文档中标注已知偏离和原因。**
4. **记录回归计划（何时/如何恢复到合规拓扑）。**

---

*本文件由 Phase 3F0.2/3F0.3 诊断产生，基于 2026-05-25 的代码和架构状态。*
