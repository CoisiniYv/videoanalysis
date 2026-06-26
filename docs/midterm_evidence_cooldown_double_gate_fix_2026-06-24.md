# Midterm Evidence Cooldown 双重门控根因与修复

**日期**: 2026-06-24
**影响范围**: primary_rtsp 部分证据未生成，出现 `materialization_expired` 和 `cooldown` 失败
**状态**: ✅ 已修复并验证通过

---

## 问题现象

在 midterm 部署中，`primary_rtsp` 存在大量未生成的证据：
- 123 个 `materialization_expired` 状态证据（根因是 media-worker 物化功能被禁用，见另一文档）
- 部分 intrusion 事件在满足 30 秒 cooldown 条件后仍被 skip，未发出 `record_request`
- 部分已发出 `record_request` 的任务在 clip-worker 阶段失败，原因是 `cooldown`

## 根因分析

### 双重 Cooldown 门控

系统存在**两层独立的 cooldown 检查**，导致证据生成被意外拦截：

#### 1. event-worker 层：按 source_id 全局 cooldown

**原始逻辑**：
```python
# services/event-worker/app/worker.py (修复前)
cooldown_key = source_id  # 所有事件类型共享一个 cooldown key
```

**问题**：
- `primary_rtsp` 的 `watchlist_hit` 和 `intrusion` 共用同一个 cooldown key
- 当 `watchlist_hit` 事件发生时，会重置 cooldown 计时器
- 即使两条 `intrusion` 事件之间间隔 > 30 秒，如果中间有 `watchlist_hit`，仍会被误判为 cooldown 未满足

**实际案例**：
```
15:33:00  intrusion     距上条 intrusion: 30.022s  ✓
                        距上条 watchlist: 16.756s   ✗ 被 skip
          原因：watchlist 重置了 source_id 级别的 cooldown
```

#### 2. clip-worker 层：按 camera_id 再次 cooldown

**配置**：
```bash
CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=30
```

**问题**：
- event-worker 已经做过 cooldown 检查并发出 `record_request`
- clip-worker 再次用 `camera_id` 级别 cooldown 拦截
- 导致已经通过第一层门控的任务在第二层失败
- 最终状态：`status=failed`, `materialization_failure_reason=cooldown`

### Cooldown Grace 缺失

**原始判断逻辑**：
```python
elapsed_sec >= cooldown_seconds
```

**问题**：
- 严格的 `>=` 判断，但浮点运算可能导致 29.999 秒被拒绝
- 对于恰好 30 秒或稍大于 30 秒的情况，缺少容差
- 实际需求：只要接近 30 秒（如 >= 29 秒），就应该允许

---

## 修复方案

### 1. event-worker cooldown key 改为 `source_id + event_type`

**修改位置**: `services/event-worker/app/worker.py`

```python
# 修复后
cooldown_key = f"{source_id}:{event_type}"
```

**效果**：
- `primary_rtsp:watchlist_hit` 和 `primary_rtsp:intrusion` 各自独立计时
- 不同事件类型之间不再互相干扰 cooldown 判断

### 2. 引入 RECORDING_COOLDOWN_GRACE_MS

**新增配置**: `infra/env/midterm.env`

```bash
RECORDING_COOLDOWN_GRACE_MS=1000
```

**语义**：
- 允许容差：`elapsed_sec >= (cooldown_seconds - grace_seconds)`
- 例如：cooldown=30s, grace=1s → 只要 >= 29s 就允许
- **不是**限制 "29-31 秒闭区间"，而是放宽下限；上限无限制

**实现位置**: `services/event-worker/app/worker.py`, `services/event-worker/app/config.py`

### 3. 关闭 clip-worker 的 camera cooldown

**修改位置**: `infra/env/midterm.env`

```bash
CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=0
```

**理由**：
- event-worker 已经在上游做了精确的 cooldown 控制
- clip-worker 的二次 cooldown 是冗余且有害的
- 避免双重门控导致合法请求被误杀

---

## 验证结果

### 1. 运行时验证

**操作**：
- 修改配置后，执行 `docker compose -f infra/docker-compose.midterm.yml up -d --no-deps --force-recreate event-worker clip-worker`（无需 rebuild）

**观察**：
- ✅ 重启后没有新的 `record_request_skipped:cooldown` 日志
- ✅ 15:40:14 `intrusion` 和 15:40:15 `watchlist_hit` 在 1 秒内都成功发布 `record_request`，证明跨类型不再互相卡
- ✅ 15:44:12 后新 `primary_rtsp` 记录：2 条 `ready`，1 条 `materializing`；无 cooldown failure
- ✅ 出现的 `max_concurrent_per_source_reached` 是并发排队限制，不是 cooldown skip，符合预期

### 2. 测试验证

```bash
python -m pytest harness/tests/test_event_worker_recording_policy.py -v
# ✅ 6 passed

python -m pytest harness/tests/test_midterm_deployment_contract.py -v
# ✅ 21 passed

python -m pytest harness/tests/test_evidence_materialization_phase2plus.py -v
# ✅ 6 passed

python -m compileall -q services/event-worker services/clip-worker
# ✅ 通过
```

---

## 修改文件清单

### 核心逻辑
- `services/event-worker/app/worker.py`
  - cooldown key 改为 `source_id:event_type`
  - 引入 grace 容差判断

- `services/event-worker/app/config.py`
  - 新增 `RECORDING_COOLDOWN_GRACE_MS` 配置项

### 配置文件
- `infra/env/midterm.env`
  - 新增 `RECORDING_COOLDOWN_GRACE_MS=1000`
  - 修改 `CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=0`

- `infra/docker-compose.midterm.yml`
  - event-worker 和 clip-worker 环境变量透传

### 测试
- `harness/tests/test_event_worker_recording_policy.py`
  - 新增跨事件类型 cooldown 独立性测试
  - 新增 grace 容差边界测试

---

## 部署注意事项

### 升级步骤
1. 拉取最新代码（包含上述修改）
2. 无需 rebuild 镜像，直接 recreate 受影响服务：
   ```bash
   docker compose -f infra/docker-compose.midterm.yml up -d --no-deps --force-recreate event-worker clip-worker
   ```
3. 观察日志，确认无 `record_request_skipped:cooldown` 错误

### 回滚方案
如需回滚，恢复配置：
```bash
# infra/env/midterm.env
RECORDING_COOLDOWN_GRACE_MS=0  # 或删除该行
CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=30
```

并恢复 `services/event-worker/app/worker.py` 中 cooldown key 为 `source_id`。

---

## 相关文档

- **media-worker 物化功能禁用问题**：另见专门文档，解释为何 123 条 `materialization_expired` 的 `materialization_attempt_count=0`
- **证据生成链路**：`CLAUDE.md` 当前链路部分
- **Replay 和证据对齐原则**：`CLAUDE.md` 相关章节

---

## 遗留问题

### 已解决
- ✅ 跨事件类型 cooldown 互相干扰
- ✅ 双重 cooldown 门控误杀
- ✅ 浮点边界容差

### 待观察
- 长期运行时 `max_concurrent_per_source_reached` 排队行为是否符合预期
- `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1` 是否需要调整为更高值

---

**文档版本**: 1.0
**最后更新**: 2026-06-24
**修复提交**: `f58ff78`
