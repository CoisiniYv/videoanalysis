# 生产 T4 40 路 5+5 秒全视觉证据压测（2026-07-12）

## 结论

最终正式门禁 `pressure40_4p1_dual1gpu_5s5s_fullvisual_finalgate_20260712T074300Z`
通过。生产机为单张 Tesla T4、双 Savant 分支，40 路目标 4 FPS，YOLO Pose
batch=4、YOLO Face batch=4、ROI AdaFace batch=16。正式采样 600 秒，
rolling-cache prefill/postfill 各 25 秒，drain 120 秒，所有视频统一前后 5 秒。

最终 artifact：

`/data/video-analytics/artifacts/pressure40_4p1_dual1gpu_5s5s_fullvisual_finalgate_20260712T074300Z`

最终 `report.json` 状态为 `passed`，`failure_reasons=[]`。唯一 warning 是
`validate_seq_iq_expected_sampling_gap`，属于采样降帧的预期提示，不是入口丢失。

## 本轮固化的边界修复

1. `--clear-existing-evidence` 现在只删除 events 及其级联 evidence DB 数据、
   evidence 文件和 rolling cache；不再删除 `face_observations`、
   `person_bbox_observations`、人员/gallery 或机械盘轨迹图。
2. 清理前先停旧源并等待 evidence guard。root 容器创建的媒体若无法由宿主用户
   删除，会映射到 media-worker 的可写挂载中删除；删除后必须重新枚举为 0，
   不能再把 `ignore_errors` 的尝试数当成成功数。
3. 正式采样前始终清除 warmup events/evidence，但保留 warmup 产生的人脸轨迹和
   person bbox observations，避免启动边界任务混入正式报告。
4. 摄像头使用 1-based 可读名称 `压力摄像头 01` 至 `压力摄像头 40`；8090 API
   和轨迹结果优先展示该名称，而不是内部 source/camera UUID。
5. rolling-cache 段覆盖在 cutoff 后、停源前采集。EOS 后源目录会消失，旧时序会
   把真实存在的段误报为 0。
6. postfill 后只剪除“event time 超过 cutoff、且既没有直接 bundle、也没有关联
   bundle”的非物化任务事件；已生成 bundle、face/person observations 和轨迹图
   全部保留。

## 清空旧 evidence 的生产证明

第一次真实清理前有：

- 25,453 events、9,558 evidence tasks、4,230 bundles；
- 11,500 个旧 evidence 顶层路径，约 36 GB；
- 296,797 face observations、757,173 person bbox observations；
- 4,137 张机械盘轨迹 JPEG。

旧实现虽报告已删除路径，但 11,500 个 root-owned 目录实际仍存在。修复后再次清理
11,613 个目录，container fallback 返回 0，`remaining_evidence_paths=0`；DB 中
events/tasks/bundles/artifacts/timeline/overlay 全部为 0。face/person observations
及轨迹 JPEG 计数在清理前后不变。

生产私有存储未改：长期 evidence/轨迹仍在 2 TB 机械盘 `/data`，rolling cache 和
每人最多 100 张轨迹热缓存仍在 512 GB 系统盘 `/home`。

## 最终压力与推理门禁

- steady counter FPS：3.9992，要求不低于 3.96；
- 40/40 forwarder 与 Savant source 覆盖；
- 主 forwarder 最大队列深度 0，queue-full 0；
- Savant send failure delta 0；
- raw forwarder drop 0、send failure 0；
- rolling-cache：40/40 source、3,064 个有效段；
- rolling raw segment FPS：p50=24.263，超过 90% full-rate 门槛；
- AdaFace ROI observations：20,648，40/40 source 覆盖；
- cooldown 60 秒，无 cooldown violation。

为了验证严格的 queue=0 门禁，最终轮的 600 秒采样窗口内没有额外调用 8090、DB、
Docker stats 或 GPU 诊断接口；所有用户视角探针均在采样结束并恢复日常态后执行。

## Evidence 与 bbox 验收

- 1,177 events，其中 693 suppressed、484 unsuppressed；
- 484 evidence tasks、484 bundles、484 playable bundles；
- 360 个 intrusion 视频、124 个 watchlist 图片；
- 360/360 视频策略为唯一 `5:5`；
- 360/360 时长通过，duration mismatch=0；
- 484/484 8090 DB-backed detail 通过；
- 360/360 timeline 通过，fallback=0；
- 360/360 annotation 通过；
- bbox missing=0、person_context missing=0、annotation mismatch=0。

额外逐个请求 360 个视频的 `/media/evidence/{event_id}/raw_clip.mov`，360/360 均返回
HTTP 206 Range，证明浏览器可播放和拖动，不只是文件存在或 ffprobe 可读。

## 人脸轨迹、名称和加载时间

最终轮共核到 129 条可展示轨迹：

- 129/129 有 `trajectory_thumbnail_url`；
- 129/129 有 `face_crop_url`；
- 129/129 缩略图 HTTP 200；
- 129/129 使用可读的 `压力摄像头 xx` 名称；
- 视频 bundle 360/360 同样使用可读摄像头名称；
- SSD 热缓存 `person_1=100`、`person_2=100`，没有超过每人 100 张；
- 机械盘长期轨迹 JPEG 共 4,970 张，未被 LRU 或压测清理删除。

日常单分支恢复后连续请求 20 次：

- evidence list：20/20 HTTP 200 且非空，p50=1.7324 秒、p95=2.0637 秒、
  max=2.0818 秒；
- person 2 trajectory：20/20 HTTP 200 且非空，p50=0.1105 秒、
  p95=0.1116 秒、max=0.1135 秒。

因此本轮没有加载超时或空白。证据列表在累计保留多轮结果后约 2 秒，仍有进一步做
分页查询/索引优化的空间，但不阻塞当前最小 MVP；轨迹首屏已稳定在约 0.11 秒。

补充用户视角 artifact：

`/data/video-analytics/artifacts/pressure40_4p1_dual1gpu_5s5s_fullvisual_finalgate_20260712T074300Z/operator_visual_acceptance.json`

## 中间轮次为何没有冒充通过

- `...T064200Z`：发现 root-owned 旧 evidence 实际未删除，立即中断并恢复日常态；
- `...T065100Z`：正式 600 秒内证据全部完成，但 8 个 cutoff 后无 bundle 任务在
  drain 中 expired，且 rolling 段在 EOS 后采集而误报 0；
- `...boundaryfix...T072100Z`：全部视觉门禁通过，但一个 30 秒快照出现主队列深度
  3，严格 `queue=0` 门禁未通过；
- `...finalgate...T074300Z`：冷却后零干扰复验，所有正式门禁通过。

原始失败报告均保留，未回写或伪造；修复后的结果使用独立 run id 和 artifact。

## 回归与恢复

- 压力脚本及相关部署/轨迹/8090测试：189 passed；
- `python -m py_compile scripts/runtime/run_midterm_pressure60.py`：通过；
- `bash -n scripts/runtime/run_pressure60_dual1gpu_profile.sh`：通过；
- `node --check services/evidence-viewer/app/static/evidence.js`：通过；
- 最终 pressure source 数为 0；
- 双分支、ROI worker 和 CUDA MPS 临时运行态已停止；
- 日常单 Savant、workers、PostgreSQL、Redis 和 8090 已恢复。
