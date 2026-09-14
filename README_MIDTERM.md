# Midterm 部署与操作指南

本文面向部署人员和日常操作员，介绍 Video Analytics Platform 的启动、摄像头配置、运行预设、状态检查与停止流程。项目总体介绍与架构请先阅读根目录 [`README.md`](README.md)。

## 启动系统

```bash
bash scripts/midterm_start.sh
```

启动脚本会完成运行环境检查、数据目录准备、基础镜像/服务启动，并准备由 8090 操作台管理的运行时组件。

启动后访问：

```text
http://127.0.0.1:8090/operator
```

8090 是浏览器侧统一入口；内部 API、Redis、推理服务和 metrics 端口主要用于服务间通信或诊断。

## 推荐操作流程

### 1. 登记摄像头

进入 **配置 → 摄像头**，为每路视频填写：

- 摄像头名称与 RTSP 地址；
- ROI；
- 需要启用的算法；
- 事件规则及相关参数。

批量启动完整双分支时，可以先完成配置，再统一选择需要运行的摄像头。

### 2. 选择运行预设

在 **启动与运行** 页面选择摄像头和硬件预设：

| 预设 | 参考规模 | 分析帧率 | 分支分配 |
| --- | ---: | ---: | --- |
| `production_t4_40` | 40 路 | 4 FPS | A/B 20/20 |
| `local_4090_60` | 60 路 | 8 FPS | A/B 30/30 |

页面支持自动均分，也支持手动指定 A/B source。实际可承载规模会受到视频分辨率、码率、编码参数、事件密度、GPU 型号和存储性能影响。

### 3. 启动完整链路

点击完整链路启动后，系统会依次完成运行时准备、source 收敛、rolling cache 建立和 evidence 链路开放。运行状态应以 8090 页面返回的实际 source、FPS、队列、延迟和任务状态为准。

### 4. 查看分析结果

操作台主要包含以下功能：

- **摄像头**：RTSP、ROI、算法和规则管理；
- **人员与人脸**：人员资料、人脸注册与图库管理；
- **证据**：事件视频、快照、检测框/姿态和时间线；
- **人员轨迹**：人员命中记录与轨迹图片；
- **启动与运行**：运行预设、source、吞吐与延迟；
- **高级维护**：存储、拓扑和运行时诊断。

## 运行架构

```text
RTSP Cameras
    |
    v
Replay A/B -> Raw Fan-out A/B
    |                 |
    |                 +--> full-rate stream -> Rolling Cache
    |
    +--> sampled stream -> Savant / GPU inference
                              |
                              +--> events / person observations
                              +--> face ROI -> AdaFace -> Face Worker

Events + Rolling Cache
          |
          v
     Media Worker
          |
          v
 Evidence + PostgreSQL index -> 8090 Viewer
```

完整预设使用 rolling cache 保存全帧率编码流，同时按配置帧率进行 AI 分析，从而将推理吞吐和证据视频质量解耦。

## 运行检查

常用命令：

```bash
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh
```

建议在 8090 同时确认：

- 目标摄像头均已进入预期分支；
- 每路 source 持续产生新帧；
- 分析 FPS 与预设相符且无持续下降；
- queue / retry / failure 指标没有持续增长；
- evidence task 能从排队状态进入完成或明确的终态；
- 事件证据、快照和人员轨迹可正常查询。

如需按摄像头核对证据生命周期，可使用 `scripts/runtime/report_evidence_camera_ledger.py`。

## 停止系统

只停止当前完整采集/分析运行时，可在 8090 使用 **停止完整链路**。这会停止对应采集和推理组件，并允许必要的后台任务完成收尾。

停止整套服务：

```bash
bash scripts/midterm_stop.sh
```

## 主要配置文件

| 文件 | 用途 |
| --- | --- |
| `infra/docker-compose.midterm.yml` | 主服务编排 |
| `infra/env/midterm.env` | 默认环境变量 |
| `infra/midterm-storage.override.yml` | 数据与媒体存储挂载 |
| `infra/operator-dual-runtime.override.yml` | A/B 双分支运行时 |
| `modules/savant_replay/config.midterm*.json` | Replay 配置 |
| `modules/savant_security/config/cameras.midterm.yml` | 摄像头运行快照 |
| `modules/savant_security/module.yml` | Savant 推理模块 |

运行预设的精确参数由 `services/api/app/services/runtime_topology.py` 中的 `RUNTIME_PROFILE_PRESETS` 维护。

## 进一步阅读

- [`docs/README.md`](docs/README.md)：文档总入口
- [`docs/current_architecture.md`](docs/current_architecture.md)：完整系统架构
- [`docs/midterm_deployment.md`](docs/midterm_deployment.md)：部署和存储说明
- [`docs/midterm_web_operator_guide.md`](docs/midterm_web_operator_guide.md)：8090 页面操作
- [`docs/midterm_quick_reference.md`](docs/midterm_quick_reference.md)：常用命令与排障
- [`docs/frontend_interface/README.md`](docs/frontend_interface/README.md)：前端与 API 集成

内部 FastAPI 文档位于 API 服务自身的 `/docs`。8090 主要提供操作界面和业务 API/media 代理，不应把 `http://127.0.0.1:8090/docs` 作为接口文档入口。
