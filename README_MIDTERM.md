# Midterm 视频分析系统：使用入口

更新时间：2026-07-20

## 第一次启动

```bash
bash scripts/midterm_start.sh
```

启动脚本负责机器层准备、镜像构建、基础服务启动，以及 8090 双分支容器的预创建。
它不会自动把 40/60 路摄像头全部加入运行。

打开：

```text
http://127.0.0.1:8090/operator
```

## 推荐操作流程

1. 在“配置 -> 摄像头”登记 RTSP、ROI 和算法规则；
2. 登记阶段可以先不选择“加入当前运行”；
3. 点击首屏“选择摄像头并启动”；
4. 选择“生产 T4 40 路完整链路”或“本机 4090 60 路完整链路”；
5. 选择精确路数，使用自动均分或手动 A/B；
6. 点击“启动完整双分支”，等待五段后台进度完成；
7. 在运行页确认 source/FPS/queue/latency，在证据和人员轨迹页确认结果。

T4 生产机当前容量基线是 40 路、4 FPS。不要在散热和正式门禁没有重新通过时改成
60 路。

## 页面能力

- 摄像头、ROI、规则和算法配置；
- 人员与单图/批量人脸注册；
- 人员轨迹和轨迹图片；
- 告警视频与 watchlist 图片；
- 运行总览、端到端延迟、完整链路启动/停止；
- 高级性能和拓扑配置；
- 存储统计、预览和受控删除。

## 运行与停止

```bash
bash scripts/midterm_health.sh
bash scripts/midterm_stop.sh
```

`midterm_health.sh` 的固定容器清单仍偏向旧单分支形态；请同时以 8090 的运行总览、
延迟和完整链路状态判断双分支与 `person-observation-worker`。

8090 的“停止完整链路”会停止摄像头采集和双分支推理并禁用摄像头，同时保留部分
worker 完成收尾；它与 `scripts/midterm_stop.sh` 的整栈停止不同。

## 架构摘要

```text
RTSP -> Replay A/B -> raw fanout A/B
  |-> sampled Savant A/B -> events / trajectories / face ROI / annotations
  `-> full-rate rolling-cache A/B

events + rolling segments -> media-worker -> DB-backed evidence -> 8090
```

完整预设使用外置 ROI AdaFace。4090 预设只是不启用 CUDA MPS，并不会关闭 ROI
AdaFace 或 rolling-cache。

## 关键文档

| 文档 | 用途 |
| --- | --- |
| `QUICKSTART.txt` | 一页快速入门 |
| `docs/current_architecture.md` | 当前架构和两种运行形态 |
| `docs/midterm_web_operator_guide.md` | 8090 详细操作 |
| `docs/midterm_deployment.md` | 部署、目录、profile 和验证 |
| `docs/midterm_quick_reference.md` | 命令与排障参考 |
| `docs/midterm_knowledge_base/00_Index.md` | 专题知识库 |
| `docs/documentation_sync_audit_2026-07-20.md` | 代码/文档差异审计 |

FastAPI `/docs` 只存在于内部 `api:8000`，8090 当前不代理 `/docs`。操作员请使用
8090 页面，接口开发请查 `docs/frontend_interface/02_api_inventory.md`。
