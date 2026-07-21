# Compose Inventory

更新时间：2026-07-20

本页回答“哪些服务属于基础栈、哪些由 profile/8090 管理”。正式启动使用
`scripts/midterm_start.sh`，底层定义为 `infra/docker-compose.midterm.yml`；
不要把单条裸 Compose 命令当成完整部署流程。

## 基础服务（无额外 profile）

| Service | 角色 |
| --- | --- |
| `redis` | Redis Streams 与运行缓存 |
| `api` | 内部 FastAPI 控制面/查询面 |
| `replay-service` | 基础单分支全率 Replay |
| `replay-raw-fanout` | Replay 后原始分发 |
| `analysis-forwarder` | 单分支分析采样 |
| `savant-security` | 单分支 Savant 推理 |
| `event-worker` | 事件、cooldown、evidence task、alerts |
| `person-observation-worker` | 独立人体轨迹批量持久化 |
| `face-worker` | 人脸 observation、图库匹配和 watchlist event |
| `clip-worker` | 单分支/兼容 Replay job 协调 |
| `video-file-sink` | 单分支/兼容 Replay job 输出 |
| `media-worker` | evidence 调度、固化、索引和清理 |
| `evidence-viewer` | 8090 UI 与代理 |

RTSP source 通常由 API 按数据库/生成快照动态创建为
`video-analytics-source-*`，不属于固定 service 表。

## 8090 完整双分支 profile

`operator-dual-runtime` 预创建：

- `cuda-mps-operator`；
- `savant-a`, `savant-b`；
- `replay-raw-fanout-a`, `replay-raw-fanout-b`；
- `analysis-forwarder-a`, `analysis-forwarder-b`（兼容/推理-only 角色）；
- `replay-a`, `replay-b`；
- `video-file-sink-a`, `video-file-sink-b`（兼容/回退角色）；
- `rolling-cache-sink-a`, `rolling-cache-sink-b`；
- `adaface-roi-worker`。

完整 `full_evidence` apply 中，`replay-raw-fanout-a/b` 自身负责采样输出到 Savant，
同时把全率帧发布给 rolling sink；单独的 `analysis-forwarder-a/b` 不应被误写成该模式
必经 hop。

T4 预设启动 MPS；4090 预设会显式停止 MPS，但仍启动 ROI 与 rolling sink。

## 其他 profiles

| Profile | 服务/用途 | 当前定位 |
| --- | --- | --- |
| `local-postgres` | `postgres` | 仅显式需要本地 DB 时 |
| `qdrant` | `qdrant`, `qdrant-sync-worker` | 可选图库向量 serving |
| `legacy-primary-rtsp` | `source-adapter` | 历史静态主源兼容 |
| `rolling-cache` | `rolling-cache-sink` | 单分支 rolling 调试 |
| `rolling-cache-dual` | rolling sink A/B | 手动双 rolling 调试 |
| `roi-adaface` | `adaface-roi-worker` | 手动 ROI worker |
| `dual-replay-shards` | replay/video-sink A–H | Replay shard 压测/兼容 |
| `dual-4090-two-source` | 多个 A/B 与 shard 服务 | 历史压测组合 |

profile 存在不代表当前 operator preset 会启动其中全部服务。

## 网络与端口

- 浏览器只使用 8090；
- API 仅 `expose: 8000`；
- 基础 Replay 8098；A/B Replay 默认 8198/8298；
- 基础 Savant 18080；A/B 18180/18181；
- 基础 analysis-forwarder 18081；基础 raw fanout 18184；
- A/B raw fanout 18185/18186；
- ROI AdaFace 18187；
- Redis 6396；local PostgreSQL 5439。

## 存储挂载

主 Compose 负责 `/data/video-analytics`，storage override 将：

- rolling segments 映射到
  `${ROLLING_CACHE_HOST_ROOT:-/home/user/video-analytics-fast/rolling-cache}`；
- rolling materialized 映射到
  `${ROLLING_CACHE_MATERIALIZED_HOST_ROOT:-/home/user/video-analytics-fast/rolling-cache-materialized}`；
- trajectory cache 映射到
  `${FACE_TRAJECTORY_CACHE_HOST_ROOT:-/home/user/video-analytics-fast/face_trajectory_cache}`。

API 还只读挂载 `/data/video-analytics/models-savant-b`，用于双分支启动前模型预检。

## 启动顺序

```text
midterm_start.sh
  -> base compose up
  -> prepare Savant-B cache
  -> operator-dual-runtime up --no-start per service
  -> 8090 owns later start/stop
```

运行中的双分支容器不会被普通预创建检查强制重建。

## 已知清单偏差

`scripts/midterm_health.sh` 仍把 profile 下的 `source-adapter` 当固定必需服务，且未列出
默认的 `person-observation-worker`。在该脚本修正前，Compose inventory 和 8090 runtime
overview 才是当前角色判断依据。
