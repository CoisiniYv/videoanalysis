# Midterm Evidence 输出清理记录（2026-06-26）

## 结论

当前 evidence 主线只保留 post-Savant Replay 证据包格式：

- `raw_clip.mov`
- `sink_metadata.json`
- `metadata.json`
- `summary.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.frame_cache.identity.json`
- 必要的生成日志，例如 `video_crop_ffmpeg.log`

8090 证据页面和文件 API 只把 `annotations.frame_cache.identity.jsonl` 当作生产标注来源。

## 本次移除

- 删除未被 8090 页面加载的旧静态脚本：`services/evidence-viewer/app/static/app.js`。
- 移除 8090 evidence API 的 `legacy` / `sidecar_preview` 标注源入口。
- 移除数据库 evidence 列表中把旧 `annotations.jsonl` 当作可用标注的判断。
- 移除 media-worker 旧 `MIDTERM_RAW_CLIP_FINALIZER_ENABLED` 分支，不再生成 `event_annotation.json` 或 `annotations.jsonl`。
- 删除无引用旧模块：`services/media-worker/app/continuous_annotation.py`、`services/media-worker/app/clip_sanitizer.py`。
- 新写入的 summary / metadata / event payload 不再包含 legacy fallback 或 `event_annotation_path` 空字段。

## 保留的保护

`frame_cache_sidecar_writer.py` 仍保留对旧文件名 `annotations.jsonl` 的负向保护，防止配置误把生产 sidecar 写到旧文件名。

## 运行期观察

清理前检查 `/data/video-analytics/media/evidence`，当前新 evidence bundle 的实际文件集合已经是 post-Savant sidecar 格式，没有发现当前输出中的 `event_annotation.json`、`annotation.json` 或 `annotations.jsonl`。

## 验证结果

- 本次运行态复查不做 rebuild；普通源码变更通过已挂载代码和服务 recreate/restart 生效。
- 8090 `/health` 返回 `ok`。
- 8090 `/api/v1/evidence/health` 返回 `index_source=database`。
- 最新 evidence bundle 的 `source=auto` 和 `source=sidecar` 都返回 `annotations.frame_cache.identity.jsonl`。
- `source=legacy` 和 `source=sidecar_preview` 返回 422，不再是可用入口。
- API 侧遇到旧 `annotations.jsonl` 路径时，`annotations_available=false`。
- `infra/docker-compose.midterm.yml` 不再设置 `MIDTERM_RAW_CLIP_FINALIZER_ENABLED`；如果旧镜像层仍显示该 ENV，当前代码也不再读取它。
