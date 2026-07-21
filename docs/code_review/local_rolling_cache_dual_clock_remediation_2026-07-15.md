# Rolling-cache 双时间域修复与本地压力验收（2026-07-15）

## 结论

本地 40 路、4fps、单卡双 Savant 分支、双 YOLO batch=4、AdaFace batch=16、Evidence 5+5 秒、600 秒采样和 120 秒 drain 已通过压力门禁。

最终 run id：`local_rolling_dualclock_dual40_4fps_600s_r15_20260715`

Artifact：`/data/video-analytics/artifacts/local_rolling_dualclock_dual40_4fps_600s_r15_20260715`

生产机 `192.168.1.100` 本次未连接、未同步、未部署；等待用户下一次明确授权。

## 根因

RTSP source adapter 使用 `use_wallclock_as_timestamps=1`。40 路并发调度时，完整的 24fps 编码帧可能在墙钟 PTS 上表现为数百毫秒停顿后集中到达。旧 rolling sink 把这个墙钟 PTS 同时当作：

1. 事件、轨迹和 bbox 的关联时间域；
2. qtmux/MOV 的媒体时间域；
3. rolling fragment 与 Evidence 5+5 秒裁剪时间域。

这会导致 parser PTS correction 累计漂移、fragment metadata 边界错位、`coverage_not_complete`、短视频以及最终 materialization expiry。把每帧强制推进到 `last_pts + duration` 同样不正确，因为 24000/1001 的舍入差异会被逐帧累加。

## 固化合同

- 原始 Savant `pts/frame_pts` 不修改，继续用于 event、frame UUID、人员轨迹、bbox 和 DB-backed annotation 关联。
- rolling sink 为每个编码帧新增 `rolling_cache_mux_pts`，按输入 frame duration 形成稳定的 24fps mux cadence。
- qtmux、splitmux fragment、rolling remux 和 5+5 秒裁剪只使用 mux 时间域。
- media-worker 通过 event frame UUID（找不到时才用最近原始 PTS）把墙钟事件窗口映射到 mux 时间域。
- finalizer 的视频完整性门禁使用 mux 时间域；annotation/timeline 仍保存并使用原始 Savant 时间域。
- materializer 在 ffmpeg 前检查端点和 fragment 缺失；墙钟 PTS 的亚秒到达抖动不再被误判为编码帧丢失。
- 成功 Evidence 继续 DB-backed；未恢复 sidecar 依赖，也未启用 Replay fallback。

## 验证结果

### 8 路双分支 canary

- Run id：`local_rolling_dualclock_dual8_4fps_90s_r13_20260715`
- 24/24 bundle 可播放。
- 16/16 视频通过 5+5、时长和最低 23.5fps 门禁。
- 每视频 239 条 DB timeline，overlay 39–42 段。
- 8/8 人员轨迹覆盖，materialization pending/failed/expired 均为 0。

### 40 路 180 秒并发验证

- Run id：`local_rolling_dualclock_dual40_4fps_180s_r14_20260715`
- 125/125 task materialized，125/125 bundle 可播放。
- 98 个视频全部通过窗口、时长和帧率门禁。
- 40/40 人员轨迹覆盖，11,679 条 observation，loss=0。
- materialization pending/failed/expired 均为 0。

### 40 路 600 秒正式门禁

- Run id：`local_rolling_dualclock_dual40_4fps_600s_r15_20260715`
- 状态：`passed`；仅保留预期的 `validate_seq_iq_expected_sampling_gap` warning。
- 396/396 task materialized，396/396 bundle 可播放。
- 312 个视频全部为 5+5，时长 mismatch=0，帧率 mismatch=0。
- rolling segment 帧率 p50=24.296fps，3,524 个 segment，40/40 source。
- 8090 detail 432/432 OK；视频 timeline 312/312 OK；annotation 312/312 OK。
- `annotation_bbox_missing_count=0`，`annotation_person_context_missing_count=0`，fallback=0。
- 人员轨迹 40/40 source，38,572 条 observation，loss=0，最终 lag/pending=0。
- AdaFace 15,106 条 observation，40/40 source，worker pending=0。
- cleanup 保留可视化结果：未删除 Evidence path、events、face observations 或 person bbox observations。

## 运行态恢复

压力源、双分支 Savant、双 rolling sink 和临时 AdaFace ROI worker 已清理；日常单 Savant、forwarder、worker、数据库和原摄像头源已恢复。生产部署必须在用户重新开机并明确授权后单独执行。
