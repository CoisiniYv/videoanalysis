---
type: runtime-runbook
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - runtime
  - 8090
  - operations
---

# 运行控制手册

## 1. 启动层次

```text
scripts/midterm_start.sh
  -> start base services
  -> pre-create stopped operator dual containers
  -> 8090 selects cameras/profile and starts full runtime
```

基础启动成功不代表 40/60 路完整链已运行。必须在 8090 完成 source 选择和后台 apply。

## 2. 普通配置保存

适用：ROI、检测线、算法开关、阈值、cooldown、watchlist target。

期望：

```text
DB write -> config sync -> generated snapshot -> no full runtime restart
```

验收：保存后刷新仍存在，且没有不相关容器重启。

## 3. 完整链路启动

1. 登记摄像头和算法；
2. 打开“选择摄像头并启动”；
3. 选择 T4 40 或 4090 60；
4. 选择精确路数；
5. balanced 或 manual A/B；
6. 确认启动；
7. 观察 apply-status 直到 succeeded。

启动前预检至少包括：摄像头数、RTSP、A/B 容器、模型/B 分支 cache、GPU、并行流
容量和 active evidence guard。

启动阶段：

```text
preflight
  -> runtime epoch + branch containers
  -> dynamic sources converge
  -> rolling sinks ready
  -> 25s prefill
  -> event task creation enabled with activation timestamp
```

不要在任务 running 时重复点击。刷新页面可以继续查看；API 重启后任务会失败，需要先
核对实际容器/摄像头/epoch 再重试。

## 4. 运行中验收

### 推理

- A/B source 20/20 或 30/30；
- recent frame age 持续更新；
- effective FPS 达到 4 或 8；
- forwarder queue 不持续增长；
- send failure/drop 无增量。

### 人脸/轨迹

- ROI stream pending/lag 收敛；
- face observation 持续；
- person-observation worker lag/pending 收敛；
- 已登记人员轨迹图片可访问。

### Evidence

- rolling sink ready；
- segment source/epoch 覆盖正确；
- task pending/materializing 最终收敛；
- failed/expired/fallback 不增长；
- MOV 5+5、约 24 FPS、HTTP Range 206；
- DB timeline/annotation/bbox/person_context 完整。

### 在线延迟

读取 `/api/v1/runtime/latency`，区分 annotation media lag、DB event/bundle lag、
oldest active task 和 A/B source receive age。该面板不能替代正式 artifact。

## 5. 高级性能/拓扑

保存草稿不应用；apply 会触发 evidence guard 和容器变更。命名 preset 会覆盖分支
参数，自定义模式才应手填。

`force=true` 会绕过正常等待并终态化旧 epoch 任务，只能在明确接受丢失/中断风险时
使用，不是普通重试。

## 6. 停止完整链路

8090 stop 会：

- 停止 dynamic sources；
- 停止 A/B capture/inference、ROI 和 MPS；
- 禁用 DB cameras；
- 保留 event/media/rolling 收尾；
- 把 apply-status 标为 stopped。

等待收尾后如需停整栈，再执行：

```bash
bash scripts/midterm_stop.sh
```

## 7. 失败恢复

### apply 失败

先检查：

- apply-status 的 phase/error/details；
- 实际 container state；
- DB enabled cameras；
- runtime epoch 和 generated topology；
- source containers；
- evidence active/lease/slot。

不要只根据页面最后一句错误盲目重试。

### source 未收敛

- 验证 RTSP 可读；
- 看 dynamic adapter 状态/日志；
- 核对 source_id 与 branch assignment；
- 看 Replay/raw-fanout/Savant visible source；
- 防止用 legacy compose source 状态误判动态源。

### evidence 不收敛

分开判断：

1. event 是否产生；
2. task 是否因 cooldown/gate 创建；
3. rolling segment coverage 是否存在；
4. task phase、retry reason、lease；
5. finalizer handoff/DB index/cleanup；
6. 8090 DB query 与媒体文件。

full preset 的 `security.record_requests=0` 是预期，不代表 evidence worker 停止。

## 8. 已知工具边界

`midterm_health.sh` 的固定服务清单尚未同步 person worker/动态 source 角色。使用它时
必须结合 8090 overview、latency、Redis group 和容器实际状态。
