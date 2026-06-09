• 我查了当前代码和 Savant Replay 官方文档；这轮没有改文件。

  当前判断
  整体方向是合理的：现在代码已经从“按事件时间/post-window PTS 找 anchor”转成了“UUID-first / PTS-verified”。这和官方 Replay 语义一致：Replay job 的 anchor_keyframe 应该是 keyframe UUID，offset 才是从该 keyframe
  往前回拨的时间/块数；ts_sync=true 只是按 PTS/DTS 节奏重放，不是用 wall-clock/event_ts 对齐。官方依据见 Savant Replay 文档：Re-Streaming Jobs
  (https://insight-platform.github.io/savant-rs/services/replay/3_jobs.html)、REST API (https://insight-platform.github.io/savant-rs/services/replay/4_api.html)。

  分层计划

  1. Event-worker 对齐检查
     核对 security.events -> record_request 是否保留同一帧域字段：event_frame_uuid、event_frame_pts、previous_keyframe_uuid、keyframe_uuid、anchor_keyframe_uuid、anchor_keyframe_pts、requested_start_pts/end_pts。
     当前代码在 services/event-worker/app/record_request.py:35 和 services/event-worker/app/record_request.py:83 的方向是对的：报警帧 UUID 是事件帧，Replay anchor 归一到报警帧的 keyframe UUID 家族。

  2. Clip-worker 对齐检查
     核对 record_request -> Replay job 是否满足：Replay payload.anchor_keyframe == record_request.anchor_keyframe_uuid；offset.seconds == anchor_keyframe_pts - requested_start_pts；duration 覆盖到
     requested_end_pts。当前实现重点在 services/clip-worker/app/worker.py:721、services/clip-worker/app/worker.py:839、services/clip-worker/app/replay_client.py:374。这里最需要 runtime 证明，因为 offset 语义是最高
     风险点。

  3. Media-worker 对齐检查
     核对 Replay sink metadata -> evidence bundle 是否只用 Replay labels 和 sink frame PTS 做裁剪，不重新选择 anchor。当前代码在 services/media-worker/app/worker.py:1397、services/media-worker/app/worker.py:1552、
     services/media-worker/app/post_savant_evidence_bundle.py:242 走的是这个逻辑。media-worker 只能验证和裁剪，不能修复前面用错 anchor 的问题。

  必须验证的运行态链路
  当前 docker ps 只看到 savant、replay-service、redis、evidence-viewer、face-worker，没看到 event-worker、clip-worker、media-worker。所以现在只能说代码方向合理，还不能说端到端已对齐。

  验证命令建议：

  docker compose -f infra/docker-compose.c2-replay-first-dev.yml config
  PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider harness/tests/test_c2_15*

  运行态要抽同一个事件做五段 lineage：

  security.events.frame_uuid/keyframe_uuid/previous_keyframe_uuid/frame_pts
  -> record_request.event_frame_uuid/anchor_keyframe_uuid/requested_start_pts/requested_end_pts
  -> clip-worker replay_job_request.anchor_keyframe/offset.seconds/labels
  -> Replay sink metadata pts/frame uuid coverage
  -> media-worker summary evidence_anchor_strategy/time_domain_crop/event position

  通过条件是：event_frame_uuid 始终是报警帧；anchor_keyframe_uuid 始终是报警帧对应的 keyframe UUID；post_window_frame_uuid 和 start_window_frame_uuid 只作为证明/裁剪辅助；最终 evidence 里事件应落在约 pre_seconds
  位置。

  主要风险
  Replay TTL、Redis stream 长度、Savant frame annotation exporter 的写入错误、FPS gate 造成的 sparse metadata、以及 worker 当前未运行，都会让链路看起来“代码对了但运行时对不上”。另外旧的
  test_r3_3a_replay_uuid_feasibility_contract.py 已经和当前 C2.15 UUID-first 目标冲突，不应该再当作当前 blocker。
