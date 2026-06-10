下面是一份可以直接放进项目文档里的总结，# 实验性开发机器与最终部署机器差异说明

## 1. 两台机器的定位不同

当前我们有两类运行环境：

```text
实验性开发机器：
13700K + 2 × RTX 4090

最终部署机器：
海光 3350 CPU + 2 × NVIDIA T4
```

它们都属于：

```text
x86_64 Linux + NVIDIA 独立显卡 dGPU
```

所以 **Docker、NVIDIA Container Toolkit、Savant DeepStream 镜像类型是一致的**，都应选择：

```text
x86_64 + dGPU 版本
```

不应选择：

```text
Jetson / L4T / ARM / Orin / Xavier 版本
```

但是，两台机器的 GPU 架构、性能、显存、推理吞吐和 TensorRT engine 都不同，因此不能把实验机上的性能结果直接当成最终部署结论。

---

# 2. 核心区别

## 2.1 CPU 区别

| 项目 | 实验机器 | 最终部署机器 |
|---|---|---|
| CPU | Intel i7-13700K | 海光 3350 |
| 定位 | 开发、调试、实验 | 生产部署 |
| 单核性能 | 较强 | 以服务器稳定性和多线程为主 |
| 用途 | 编译、调试、模型转换、联调 | 长时间稳定运行 |

CPU 差异对我们项目有影响，但不是最大风险。

主要影响：

```text
1. worker 处理速度可能不同
2. PostgreSQL 写入性能可能不同
3. FastAPI 响应性能可能不同
4. 视频流解码调度和线程调度可能不同
```

但真正需要重点关注的是 GPU 和 TensorRT engine 差异。

---

## 2.2 GPU 区别

| 项目 | 实验机器 | 最终部署机器 |
|---|---|---|
| GPU | 2 × RTX 4090 | 2 × NVIDIA T4 |
| GPU 架构 | Ada | Turing |
| 定位 | 高性能消费级/工作站卡 | 数据中心推理卡 |
| 性能 | 明显强于 T4 | 最终真实性能基准 |
| 显存 | 通常 24GB/张 | 通常 16GB/张 |
| 用途 | 开发和高性能实验 | 真实部署和验收 |

这意味着：

```text
4090 能跑通，不代表 T4 一定能跑满 60 路。
4090 上不卡，不代表 T4 上不会出现 Redis lag、Savant 队列积压或 GPU 满载。
```

---

## 2.3 TensorRT engine 不可直接复用

这是最重要的区别。

```text
ONNX 模型可以从 4090 迁移到 T4。
代码可以从 4090 迁移到 T4。
Savant module.yml 可以迁移到 T4。
Docker Compose 可以迁移到 T4。

但 TensorRT engine 不应直接从 4090 复制到 T4 使用。
```

原因是：

```text
RTX 4090 和 T4 属于不同 GPU 架构。
TensorRT engine 通常针对具体 GPU 架构、TensorRT 版本、CUDA 版本和模型 batch 配置生成。
```

因此模型目录建议这样管理：

```text
/data/video-analytics/models/
  yolo26_pose.onnx
  scrfd_2.5g.onnx
  arcface.onnx

/data/video-analytics/engines/
  rtx4090/
    yolo26_pose_b8_fp16.engine
    scrfd_2.5g_b16_fp16.engine
    arcface_b16_fp16.engine

  t4/
    yolo26_pose_b8_fp16.engine
    scrfd_2.5g_b16_fp16.engine
    arcface_b16_fp16.engine
```

结论：

```text
开发阶段可以在 4090 上生成 engine。
最终部署阶段必须在 T4 上重新生成 engine。
```

---

# 3. 当前实验机器应该承担什么工作？

当前 13700K + 2×4090 应该承担以下工作：

```text
1. 项目目录搭建
2. Docker Compose 架构验证
3. Savant 镜像验证
4. 单路视频 pipeline 跑通
5. YOLO26-pose 接入
6. YOLO26-pose converter 开发
7. nvtracker 接入
8. PersonPoseObservation metadata 输出
9. 行为规则纯 Python 模块开发
10. Redis Streams 事件输出
11. event-worker 入库
12. PostgreSQL + pgvector schema 验证
13. FastAPI 接口开发
14. SCRFD_2.5G 接入
15. ArcFace 接入
16. 人脸质量过滤
17. 重点人员布控逻辑
18. 一键找人逻辑
19. Harness 单元测试和 smoke test
```

也就是说，**绝大多数开发工作都可以先在实验机器上完成**。

---

# 4. 当前实验机器不应该承担什么结论？

实验机器不应该直接承担这些最终结论：

```text
1. 最终 60 路是否一定稳定
2. 双 T4 每张卡能否稳定 30 路
3. 最终 batch size 应该是多少
4. 最终每路 FPS 应该是多少
5. 最终人脸检测频率应该是多少
6. 最终 watchlist_hit 延迟是多少
7. 最终 GPU 显存是否足够
8. 最终 PostgreSQL 写入延迟是否满足要求
```

这些必须等最终海光 3350 + 双 T4 到位后重新压测。

---

# 5. 当前开发策略

## 5.1 第一阶段：环境打通

在实验机器上先完成：

```bash
nvidia-smi

docker run --rm --gpus all \
  nvidia/cuda:12.4.1-base-ubuntu22.04 \
  nvidia-smi

docker run --rm --gpus all \
  ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1 \
  nvidia-smi
```

目标是确认：

```text
1. 宿主机能看到两张 4090
2. Docker 容器能看到两张 4090
3. Savant DeepStream 容器能看到两张 4090
```

---

## 5.2 第二阶段：先跑单路 Savant

不要一开始就跑 30 路、60 路。

先跑：

```text
1 路测试视频
  -> Savant module 启动
  -> YOLO26-pose 加载
  -> 输出 person bbox
  -> 输出 keypoints
  -> nvtracker 输出 track_id
  -> Redis 收到结构化事件或 metadata
```

这个阶段只验证主链路是否成立。

---

## 5.3 第三阶段：按最终架构写 Docker Compose

虽然是实验机器，但 Compose 结构应该尽量接近最终部署：

```text
savant-gpu0 -> GPU 0
savant-gpu1 -> GPU 1
redis
postgres
api
event-worker
clip-worker
face-worker
prometheus
grafana
web
```

这样做的好处是：

```text
1. 开发环境和生产环境差异小
2. 后续迁移到 T4 时不用大改目录结构
3. 双 GPU 分流逻辑可以提前验证
4. Redis / worker / PostgreSQL 异步链路可以提前稳定
```

---

## 5.4 第四阶段：人为限制 4090，不要让它“跑得太舒服”

4090 性能远高于 T4，所以开发时要保守设置参数。

建议实验阶段使用：

```env
MAX_FPS=5/1 或 8/1
POSE_BATCH_SIZE=8
FACE_BATCH_SIZE=8 或 16
ARCFACE_BATCH_SIZE=8 或 16
MAX_PARALLEL_STREAMS=8 或 16
FACE_ATTEMPT_INTERVAL_MS=1000 或 2000
```

不要因为 4090 能跑很高 FPS，就把参数设得很激进。

我们的项目目标是最终在 T4 上稳定运行，所以开发阶段应该贴近最终策略：

```text
主检测：每路 5 到 8 FPS
人脸检测：每个 track 每秒最多尝试一次
ArcFace：只对质量合格的人脸执行
过载时优先降低人脸链路频率
```

---

# 6. 推荐当前开发顺序

## Phase 0：基础环境

```text
Docker
NVIDIA Container Toolkit
Savant DeepStream 镜像
Redis
PostgreSQL + pgvector
FastAPI 空服务
```

验收：

```text
docker compose up 能启动基础服务。
容器内 nvidia-smi 能看到两张 4090。
```

---

## Phase 1：主视频链路

```text
YOLO26-pose
nvtracker
person bbox
keypoints
track_id
metadata 输出
```

验收：

```text
单路视频可以检测人。
可以拿到 bbox、keypoints、track_id。
```

---

## Phase 2：行为规则

优先做：

```text
1. 周界入侵
2. 徘徊
3. 人群聚集
4. 摔倒初版
```

要求：

```text
规则必须是纯 Python 模块。
不能把核心判断逻辑写死在 Savant PyFunc 里。
必须可以脱离 Savant 做单元测试。
```

验收命令示例：

```bash
pytest harness/tests/test_intrusion.py
pytest harness/tests/test_loitering.py
pytest harness/tests/test_crowd_gathering.py
pytest harness/tests/test_fall.py
```

---

## Phase 3：事件链路

```text
Savant PyFunc
  -> SecurityEvent
  -> Redis Streams
  -> event-worker
  -> PostgreSQL events
  -> FastAPI 查询
  -> WebSocket / 大屏
```

验收：

```text
行为事件可以从 Savant 输出。
event-worker 可以消费并入库。
FastAPI 可以查询最近事件。
```

---

## Phase 4：人脸链路

```text
person/head ROI
  -> SCRFD_2.5G
  -> face quality filter
  -> ArcFace
  -> embedding
  -> PostgreSQL + pgvector
```

验收：

```text
清晰人脸可以入库。
同一个人 embedding 可以被 pgvector 检索命中。
```

---

## Phase 5：重点人员布控和一键找人

```text
persons
person_gallery_embeddings
watchlist_rules
live_search_jobs
watchlist_hit
live_search_hit
```

验收：

```text
注册一个人脸。
视频中出现该人。
系统产生 watchlist_hit 或 live_search_hit。
```

---

# 7. 从实验机器迁移到最终机器时需要做什么？

最终机器到位后，执行迁移验证：

```text
1. 安装 NVIDIA Driver
2. 安装 Docker
3. 安装 NVIDIA Container Toolkit
4. 拉同一个 Savant DeepStream 镜像 tag
5. 拷贝项目代码
6. 拷贝 ONNX 模型
7. 不拷贝 4090 engine
8. 在 T4 上重新生成 TensorRT engine
9. 单路 smoke test
10. 4 路 / 8 路 / 16 路 / 30 路 / 60 路压测
11. 调整 batch size、FPS、人脸检测频率
12. 固化最终 docker-compose 和 env 参数
```

---

# 8. 当前最推荐的行动方案

现在不要等待最终机器。

当前应该立即做：

```text
1. 在实验机器上安装 Docker + NVIDIA Container Toolkit
2. 固定 Savant DeepStream 镜像版本
3. 建立 video-analytics 项目目录
4. 写 docker-compose.dev.yml
5. 启动 redis / postgres / api / worker
6. 启动 savant-gpu0 / savant-gpu1
7. 先跑单路测试视频
8. 接 YOLO26-pose ONNX
9. 开发 converter
10. 输出 PersonPoseObservation
11. 再接 nvtracker
12. 再接行为规则
```

---

# 9. 一句话总结

```text
13700K + 双 4090 是开发机，用来把系统做出来、跑通、联调、写测试；
海光 3350 + 双 T4 是生产机，用来重新生成 TensorRT engine、做真实性能压测、确定最终 FPS / batch / 摄像头分配。
```

最关键的边界是：

```text
可以迁移：
代码、ONNX、配置、数据库 schema、API、规则、测试。

不能直接迁移：
4090 上生成的 TensorRT engine、4090 上得到的最终性能结论。
```