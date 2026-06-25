# Midterm 视频分析系统 - 使用入口

> **快速开始：** 看 `QUICKSTART.txt` 一页纸指南

## 一键启动

```bash
# 迁移机/项目机统一启动入口
bash scripts/midterm_start.sh

# 打开浏览器访问
http://127.0.0.1:8090/operator
```

启动脚本负责机器层准备：检查 Docker/GPU、创建运行目录、验证模型资产、构建镜像并
启动所有容器。启动完成后，摄像头、人员/人脸、告警证据、存储维护和运行时重启都
从 8090 页面管理。

## Web 操作台

启动后通过浏览器操作，无需命令行：

- **摄像头管理**：添加/编辑 RTSP 视频源
- **人员与人脸**：上传人脸照片建立人员库
- **告警证据**：查看告警录像和识别结果

详细使用指南：[docs/midterm_web_operator_guide.md](docs/midterm_web_operator_guide.md)

## 一键管理脚本

| 脚本 | 功能 |
|------|------|
| `scripts/midterm_start.sh` | 迁移机/项目机整体启动入口 |
| `scripts/midterm_stop.sh` | 停止服务，默认保留 `/data/video-analytics` 数据 |
| `scripts/midterm_health.sh` | 全面健康检查 |

## 健康检查

```bash
bash scripts/midterm_health.sh
```

会检查：
- ✓ 容器状态（12个默认服务）
- ✓ API 端点可用性
- ✓ 摄像头配置状态
- ✓ 人脸库注册状态
- ✓ GPU 可用性
- ✓ 磁盘空间

## 文档导航

| 文档 | 用途 |
|------|------|
| `QUICKSTART.txt` | 一页纸快速入门（推荐第一次看这个） |
| `docs/midterm_web_operator_guide.md` | Web 操作台详细使用指南 |
| `docs/midterm_quick_reference.md` | API/命令/故障排查参考 |
| `docs/midterm_clean_machine_migration_2026-06-25.md` | 新机器干净迁移说明和打包/部署脚本 |
| `docs/midterm_migration_runbook_2026-06-23.md` | 系统迁移打包流程 |
| `CLAUDE.md` | 开发规则和部署入口说明 |

## 系统架构

```
RTSP 源
  → Replay 存储
  → analysis-forwarder 采样分析
  → Savant 推理（YOLO26-pose + YOLOv8-Face + AdaFace）
  → Redis 事件流
  → event-worker / face-worker
  → clip-worker Replay 作业
  → video-file-sink 原始录像
  → media-worker 证据包生成
  → 8090 Web 操作台复核
```

## 当前部署

- **启动入口**: `scripts/midterm_start.sh`
- **Compose 文件**: `infra/docker-compose.midterm.yml`
- **环境配置**: `infra/env/midterm.env`
- **compose 项目名**: `video-analytics-midterm`
- **默认 SOURCE_ID**: `primary_rtsp`

## 典型工作流

```bash
# 1. 启动系统
bash scripts/midterm_start.sh

# 2. 检查健康状态
bash scripts/midterm_health.sh

# 3. 打开浏览器配置和管理运行时
open http://127.0.0.1:8090/operator

# 4. 在 Web 界面添加摄像头和人员

# 5. 查看告警证据

# 6. 停止系统（保留数据）
bash scripts/midterm_stop.sh
```

## 故障排查

### 端口冲突

```bash
# 检查占用
ss -ltn | grep -E ':(6396|8090|8098|18080|18081)'

# 停止旧部署
bash scripts/midterm_stop.sh
```

### 服务日志

```bash
# 所有服务
docker compose -f infra/docker-compose.midterm.yml logs -f

# 特定服务
docker compose -f infra/docker-compose.midterm.yml logs -f api
docker compose -f infra/docker-compose.midterm.yml logs -f savant-security
```

### 重启单个服务

```bash
docker compose -f infra/docker-compose.midterm.yml restart <service>
```

## 开发规则

详见 `CLAUDE.md`，关键原则：

1. 当前部署只使用 `midterm` 文件
2. 修改前先添加/更新测试
3. 功能代码变更后只重启受影响服务
4. 运行态修改必须同步更新文档

## 性能验证

长期产能验证按 `specs/16_dual_path_30x2_t4_production_optimization.md` 执行。

## 迁移打包

准备迁移到其他机器：

1. 运行健康检查确认当前状态正常
2. 提交所有代码修改
3. 按 `docs/midterm_migration_runbook_2026-06-23.md` 打包

## 联系

- 问题反馈：查看日志 + 健康检查输出
- API 文档：http://127.0.0.1:8090/docs
