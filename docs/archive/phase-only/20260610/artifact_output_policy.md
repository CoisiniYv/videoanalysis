# Artifact Output Policy (Phase H1)

本文档定义所有 P1/D1/V1 及后续阶段运行产物的统一目录规范。

## 1. 核心原则

1. **repo 只允许保存代码、docs、tests、scripts、compose。**
2. **所有运行产物必须写到 `/data/video-analytics/artifacts/`。**
3. **禁止在 repo 根目录或任何 repo 子目录下生成运行产物。**

## 2. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VIDEO_ANALYTICS_DATA_ROOT` | `/data/video-analytics` | 数据根目录 |
| `VIDEO_ANALYTICS_ARTIFACT_ROOT` | `/data/video-analytics/artifacts` | 产物根目录 |

所有 smoke 脚本和 exporter 必须读取 `VIDEO_ANALYTICS_ARTIFACT_ROOT` 环境变量，
并在未设置时使用默认值。

## 3. Run ID 格式

每次 smoke 必须生成唯一 run_id：

```
<phase>_<timestamp>_<short_uuid>
```

示例：`d1-rtsp-15min_20260531T120000Z_a1b2c3`

- `phase`：阶段标识，如 `p1c-rtsp`、`d1-rtsp-15min`、`v1-visual`
- `timestamp`：ISO 8601 基本格式 `YYYYMMDDTHHmmssZ`
- `short_uuid`：6 位随机十六进制

## 4. 运行目录结构

每次运行目录：

```
/data/video-analytics/artifacts/runs/<phase>/<run_id>/
```

### D1 输出目录

```
/data/video-analytics/artifacts/runs/d1-rtsp-15min/<run_id>/
  manifest.json
  report.md
  summary.json
  people_tracks.csv
  people_tracks.json
  face_observations.csv
  face_observations.json
```

可选：`gallery_hits.csv`、`gallery_hits.json`。

### P1c 输出目录

```
/data/video-analytics/artifacts/runs/p1c-rtsp/<run_id>/
  manifest.json
  evidence/<event_id>/
    raw_clip.mov
    metadata.json
    sink_metadata.json
    event_annotation.json
```

同时允许业务兼容路径 `/data/video-analytics/media/evidence/<event_id>/`，
但 manifest.json 必须记录二者关系。

### P1b 输出目录

```
/data/video-analytics/artifacts/runs/p1b-rtsp/<run_id>/
  manifest.json
  sink_output/
    metadata.json
    video.mov
```

## 5. Latest 指针

每个 phase 的最新 run 通过 symlink 或 latest.json 指向：

```
/data/video-analytics/artifacts/latest/<phase> -> ../runs/<phase>/<run_id>
```

或：

```
/data/video-analytics/artifacts/latest/<phase>.json
{
  "run_id": "...",
  "run_dir": "/data/video-analytics/artifacts/runs/<phase>/<run_id>",
  "created_at": "..."
}
```

latest 指针**不复制大文件**，只允许软链接或 latest.json。

## 6. manifest.json Schema

每个 run 必须生成 `manifest.json`：

```json
{
  "schema_version": "1.0",
  "phase": "d1-rtsp-15min",
  "run_id": "d1-rtsp-15min_20260531T120000Z_a1b2c3",
  "created_at": "2026-05-31T12:00:00Z",
  "input": {
    "input_type": "rtsp",
    "input_uri": "rtsp://10.37.57.112:8554/live/1080movie",
    "source_id": "d1_rtsp_15min",
    "camera_id": "cam_d1_rtsp_15min"
  },
  "runtime": {
    "docker_access": "DOCKER_ACCESS_OK",
    "build_used": false,
    "source_adapter_stopped_after_run": true
  },
  "artifacts": {
    "report_md": "...",
    "summary_json": "...",
    "people_tracks_csv": "...",
    "face_observations_csv": "..."
  },
  "git": {
    "commit": "...",
    "status_clean": true
  }
}
```

P1c manifest 还要包含：

```json
{
  "evidence_dir": "...",
  "raw_clip": "...",
  "metadata_json": "...",
  "event_annotation_json": "...",
  "sink_metadata_json": "..."
}
```

### manifest.json Required Fields

以下字段在所有 phase 的 manifest 中必须存在：

- `schema_version`
- `phase`
- `run_id`
- `created_at`
- `input`
- `runtime`
- `artifacts`
- `git`

### manifest.json Field Types

- `schema_version`：string，值为 `"1.0"`
- `phase`：string
- `run_id`：string
- `created_at`：ISO 8601 string
- `input`：object
- `runtime`：object
- `artifacts`：object
- `git`：object

## 7. 禁止行为

以下行为**严格禁止**：

1. 在 repo 根目录生成 `manual-inspection/`、`tmp/visual_results/`、`oodd/` 目录。
2. 在 repo 下生成 `*.mov`、`*.mp4`、`*.webm`、`*.jpg`、`*.png`、`*.zip` 运行产物。
3. 在 repo 下生成 runtime `*.csv`、`*.json` 输出（docs 下的正常 json/md 测试文件除外）。
4. smoke 脚本默认输出到 repo 下的 `manual-inspection/`。
5. exporter 默认输出到 repo 下的 `manual-inspection/`。

## 8. 允许的 Repo 内产物

以下文件允许在 repo 中存在（被 .gitignore 保护）：

- `manual-inspection/`：本地调试产物，已被 .gitignore 忽略。
- `testVideo/`：测试视频，已被 .gitignore 忽略。
- `yolomodel/`：模型文件，已被 .gitignore 忽略。

这些目录**不允许被 git commit**。

## 9. Cleanup Policy

### scripts/maintenance/list_artifacts.sh

列出 `/data/video-analytics/artifacts/runs` 下所有 phase/run，显示大小、时间、文件数。

### scripts/maintenance/cleanup_artifacts.sh

支持参数：
- `--phase <phase>`：只清理指定 phase
- `--older-than-days <N>`：只清理 N 天前的 run
- `--dry-run`：只显示将被清理的 run，不实际删除
- `--keep-latest <N>`：每个 phase 保留最新 N 个 run

**默认必须 dry-run**，不允许误删。

## 10. 迁移策略

### 当前 manual-inspection 内容

现有 `manual-inspection/d1_15min_detection_latest/` 内容可保留用于本地查看，
但后续 D1 smoke 不再写入此目录。

### 当前 /data/video-analytics/media/ 内容

现有 `/data/video-analytics/media/` 下的 evidence、replay-sink-output 等目录
继续作为业务兼容路径存在，但新 run 的主产物目录为
`/data/video-analytics/artifacts/runs/<phase>/<run_id>/`。

## 11. 合规检查

`harness/tests/test_h1_artifact_output_policy.py` 提供以下静态检查：

1. D1 smoke 不得写 `repo/manual-inspection`。
2. P1c smoke 使用 `VIDEO_ANALYTICS_ARTIFACT_ROOT`。
3. manifest.json schema 合规。
4. latest 指针存在。
5. `.gitignore` 禁止 runtime media artifacts。
6. smoke report 必须输出 `artifact_root` / `run_id` / `manifest_path`。
7. repo root 不允许出现 `oodd/`、`manual-inspection/`、`tmp/visual_results/`。
8. cleanup script 默认 dry-run。
