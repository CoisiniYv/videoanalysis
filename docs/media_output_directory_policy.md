# Media Output Directory Policy

Date: 2026-05-25
Status: **ACTIVE** — 所有涉及媒体文件输出的实现必须遵守本文档。

---

## 1. 目的

定义项目中所有媒体输出（截图、标注截图、视频片段）的唯一合法目录、命名规范和 API URL 映射规则。

**本文件是强制性策略文件。**

---

## 2. 目录规范

### 2.1 唯一 MEDIA_ROOT

```
MEDIA_ROOT=/data/video-analytics/media
```

所有服务通过统一的 Docker 挂载访问媒体文件：

```yaml
volumes:
  - /data/video-analytics/media:/media
```

### 2.2 事件证据输出目录（唯一合法路径）

```
/data/video-analytics/media/evidence/events/{event_id}/
```

每个事件一个目录，目录内固定文件名：

| 文件 | 路径 | 说明 |
|---|---|---|
| snapshot.jpg | `.../events/{event_id}/snapshot.jpg` | 原始截图（无标注） |
| annotated_snapshot.jpg | `.../events/{event_id}/annotated_snapshot.jpg` | 标注截图（bbox + label） |
| clip_raw.mp4 | `.../events/{event_id}/clip_raw.mp4` | 原始视频片段 |
| evidence_metadata.json | `.../events/{event_id}/evidence_metadata.json` | 证据元数据 |
| clip_annotated.mp4 | `.../events/{event_id}/clip_annotated.mp4` | 标注视频片段（未来） |

### 2.3 Phase 3H 输入缓存目录（临时 POC）

```
/data/video-analytics/media/phase3h-savant-output/
```

仅用于 Phase 3H/E1 的 `video.mov` + `metadata.json` 输入缓存。不作为最终事件证据目录。生产环境不使用此路径。

### 2.4 Phase 3H 元数据输出（临时 POC）

```
/data/video-analytics/media/phase3h-metadata/
```

仅用于 Phase 3H.1 NDJSON 输出缓存。生产环境不使用此路径。

---

## 3. API URL 映射

容器内路径到 API URL 的映射规则：

| 容器内路径 | API URL |
|---|---|
| `/media/evidence/events/{event_id}/snapshot.jpg` | `/media/evidence/events/{event_id}/snapshot.jpg` |
| `/media/evidence/events/{event_id}/annotated_snapshot.jpg` | `/media/evidence/events/{event_id}/annotated_snapshot.jpg` |
| `/media/evidence/events/{event_id}/clip_raw.mp4` | `/media/evidence/events/{event_id}/clip_raw.mp4` |
| `/media/evidence/events/{event_id}/clip_annotated.mp4` | `/media/evidence/events/{event_id}/clip_annotated.mp4` |

API 通过 FastAPI StaticFiles 挂载 `/media` → `MEDIA_ROOT` 提供服务。

---

## 4. 数据库写入规范

| 字段 | 位置 | 值 |
|---|---|---|
| `snapshot_path` | `events.snapshot_path` (column) | `/data/video-analytics/media/evidence/events/{event_id}/snapshot.jpg` |
| `clip_path` | `events.clip_path` (column) | `/data/video-analytics/media/evidence/events/{event_id}/clip_raw.mp4` |
| `annotated_snapshot_path` | `payload.media.annotated_snapshot_path` | 同上，`annotated_snapshot.jpg` |
| `annotated_clip_path` | `payload.media.annotated_clip_path` | 同上，`clip_annotated.mp4`（未来） |
| `output_root` | `payload.media.output_root` | `/data/video-analytics/media/evidence/events/{event_id}` |

---

## 5. 环境变量规范

| 变量 | 值 | 说明 |
|---|---|---|
| `MEDIA_ROOT` | `/data/video-analytics/media` | 媒体根目录（API 使用） |
| `EVIDENCE_ROOT` | `/data/video-analytics/media/evidence` | 证据根目录 |
| `EVIDENCE_EVENTS_DIR` | `/data/video-analytics/media/evidence/events` | 事件证据目录（evidence-worker 使用） |
| `VIDEO_INPUT_DIR` | `/data/video-analytics/media/phase3h-savant-output` | Phase 3H 输入缓存（evidence-worker 使用） |

**禁止使用的旧变量名**（已废弃）：
- `SNAPSHOT_OUTPUT_DIR`
- `ANNOTATED_OUTPUT_DIR`
- `CLIP_OUTPUT_DIR`

---

## 6. 禁止行为

以下行为**严格禁止**：

1. 将事件证据写入 repo 相对路径（如 `../media/evidence/snapshots/`）。
2. 使用平铺目录结构（如 `snapshots/*.jpg`、`clips/*.mp4`）作为事件证据输出。
3. 在 compose 中使用 repo 相对路径挂载证据输出目录。
4. 将 Phase 3H 临时缓存目录作为最终证据目录。
5. 创建新的 `docker-compose.phaseXX.yml` 使用老旧媒体目录结构。
6. 持续运行 Phase 3B Replay bypass continuous sink（`phase3b-video-file-sink`、`phase3b-media-worker` 等）。

---

## 7. 允许的临时行为

以下行为在开发/MVP 阶段**明确允许**：

1. Phase 3H `video-file-sink` 和 `metadata-sink` 写入 `/media/phase3h-savant-output/` 和 `/media/phase3h-metadata/` 作为临时 POC 缓存。
2. `/data/video-analytics/media/replay-sink-output/` 和 `/data/video-analytics/media/snapshots/` 中的历史文件保留（不删除）。
3. repo 下 `media/evidence/` 中的旧 E1 平铺目录保留（不删除）。

---

## 8. 清理策略

1. **不删除历史文件**。旧 Phase 3B、旧 E1 媒体文件保留在磁盘上。
2. **旧容器必须停止**。Phase 3B 的 continuous sink 容器不得运行。
3. **只有 Phase E1.1a 以后的新代码才写入规范目录**。
4. 历史目录可在后续阶段通过专门的清理脚本处理。

---

## 9. 违规处理

如果任何实现、PR 或 Claude Code 会话中出现违反本策略的行为：

1. **立即停止实施。**
2. **不要合并。**
3. **修正为符合本策略的路径和目录结构。**

---

## 10. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/phase_e1_alert_evidence_mvp.md` | Phase E1 证据生成文档 |
| `docs/phase3h_2_savant_output_video_sink_poc.md` | Phase 3H.2 视频输出 POC |
| `docs/production_ingestion_topology_policy.md` | 生产拓扑策略 |

---

*Written 2026-05-25. Phase E1.1a.*
