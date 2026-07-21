# Midterm Web 操作台使用指南

更新时间：2026-07-20

## 1. 访问与导航

```text
http://127.0.0.1:8090/operator
```

一级入口：

- **配置**：摄像头、人员与人脸库；
- **证据**：告警证据、人员轨迹；
- **运维控制**：启动与状态、高级维护。

页面顶部统计摄像头、当前启用、人员和证据数量。内部 API 不需要操作员直接访问。

## 2. 摄像头与算法

### 添加摄像头

1. 进入“配置 -> 摄像头”；
2. 点击“新增”；
3. 填写摄像头名称、RTSP 视频地址、站点和位置；
4. 配置需要的 ROI、检测线和告警算法；
5. 保存。

“保存后立即加入当前运行”建议保持未勾选。40/60 路完整运行应在配置完成后，通过
“选择摄像头并启动”一次性选择并启用，避免逐路接入当前单分支。

### 修改配置

- 选择左侧摄像头，修改基础信息后保存；
- ROI/检测线在画面编辑器中维护；
- 告警算法卡用于常用开关，复杂条件使用高级规则；
- 保存 ROI/规则只同步配置，不应重启整个运行时；
- RTSP、摄像头加入运行、性能和拓扑变化属于运行态操作。

如果只是准备双分支，不要连续点击每路“启用”。

## 3. 人员与人脸库

### 注册新人员

1. 进入“配置 -> 人员与人脸库”；
2. 选择“新人员”；
3. 选择一张或多张 JPG/JPEG/PNG/BMP/WEBP；
4. 填写唯一人员编号、姓名和可选描述；
5. 提交并查看逐张注册回执。

照片应清晰且每张只有一张主脸。批量注册可能返回部分成功，必须查看每张图片的结果。

### 追加照片

1. 选择现有人员；
2. 点击“追加到当前人员”；
3. 选择一张或多张新照片；
4. 提交并检查 gallery 是否更新。

### 查看/删除

- 人员详情显示身份和已登记图片；
- “查看轨迹”跳到人员轨迹；
- 删除人员或 gallery 图片必须进入维护 preview/confirm 流程，不能直接绕过预览。

当前默认在线向量后端是 pgvector；是否启用 Qdrant 是管理员配置，不影响操作员的注册
表单，但会影响后端诊断。

## 4. 选择并启动完整链路

首次部署或代码更新后，管理员先执行：

```bash
bash scripts/midterm_start.sh
```

该脚本会预创建但不启动 A/B、MPS、ROI 和 rolling 容器。之后由 8090 管理。

### 推荐入口

可以从以下位置进入同一快速启动器：

- 首屏“系统启动入口 -> 选择摄像头并启动”；
- 摄像头页“选择并启动完整链路”；
- “运维控制 -> 启动与状态 -> 完整链路快速启动”。

### 选择预设

| 预设 | 选择路数 | 分支 | 分析 FPS | 说明 |
| --- | ---: | ---: | ---: | --- |
| 生产 T4 | 40 | 20/20 | 4 | CUDA MPS、ROI AdaFace、rolling-cache |
| 本机 4090 | 60 | 30/30 | 8 | 不使用 MPS，仍使用 ROI AdaFace 和 rolling-cache |

操作步骤：

1. 选择运行方案；
2. 点击“按方案选择所需路数”或手动勾选；
3. 选择“系统自动均分”或手动 A/B；
4. 确认数量准确；
5. 点击“启动全部分析/启动完整双分支”；
6. 不重复点击，等待后台进度。

### 进度含义

页面依次显示：

1. 检查配置；
2. 启动识别服务/TensorRT；
3. 等待摄像头；
4. 准备 rolling 录像缓存；
5. 启用 evidence。

rolling prefill 默认 25 秒并显示倒计时。浏览器刷新或切换页面后可恢复进度；API
容器重启会中断后台任务并显示失败，此时先核对实际状态再重试。

“双分支正在运行”不等于“完整 evidence 链已就绪”。必须等最后阶段成功。

专项说明：

[8090 单卡双分支启动流程说明](midterm_8090_single_gpu_dual_branch_operator_runbook_2026-07-14.md)

## 5. 运行状态与实时延迟

进入“运维控制 -> 启动与状态”。

### 实时延迟

关注：

- 画面/annotation media lag；
- event/bundle DB lag；
- pending/materializing 和最老任务年龄；
- A/B queue；
- source 数和最近帧年龄。

页面在当前 view 中周期刷新，也可以手动刷新。短时 warning 可以继续观察，持续
critical、source age 增长或 queue 不归零需要停止扩容并排查。

### 运行总览

完整链路应满足：

- A/B 路数为 20/20 或 30/30；
- 最近帧持续更新；
- effective FPS 接近 4 或 8；
- queue/send failure 不增长；
- ROI AdaFace、person、face consumer pending/lag 收敛；
- evidence pending/materializing 能清空；
- failed/expired/fallback 不持续增加。

### 高级运维

推理性能和处理能力设置是高级入口：

- “保存”只保存草稿；
- “确认并应用”才改变运行时；
- 命名预设会锁定已验证参数，普通操作员无需手填 batch/FPS/MPS；
- active evidence 会阻止危险 apply；
- 不要把 force 当重试按钮。

## 6. 告警证据

进入“证据 -> 告警证据”。

可以按分类、摄像头、人员和分页查看。当前主页面以 intrusion 等视频 evidence 为主；
watchlist 图片可能主要在人员轨迹中展示，因此视频列表数量不等于所有 bundle 数。

### 视频复核

选择一条记录后检查：

- 原始录像可以播放；
- 典型窗口为前 5 秒 + 后 5 秒；
- 原始视频约 24 FPS；
- 人员框、人脸框、关键点和标签按开关显示；
- timeline/annotation 来自 DB-backed API；
- “待复核/未生成/生成失败/生成超时”不能当作可用证据。

分析 bbox 可能只有 4/8 FPS，因此在 24 FPS 视频上呈稀疏帧间隔，这是预期，不代表
视频本身只有 4/8 FPS。

删除当前证据必须先预览，再确认范围、数量和原因。

## 7. 人员轨迹

进入“证据 -> 人员轨迹”，按人员编号、时间和摄像头查询。结果按时间倒序、每页 50
条；选择记录后在右侧查看图片。

轨迹页使用已经持久化的匹配结果，不会在每次翻页时扫描全部 observation。若没有图片，
先分清“没有 watchlist 命中”与“媒体 URL/文件缺失”。

## 8. 停止与维护

### 只停止完整采集/推理

在快速启动卡点击“停止采集与推理”。系统会停止动态源和双分支，并禁用摄像头；
event/media/rolling 等会继续有限收尾。等待 evidence 收敛后再做整栈停止。

### 停止整栈

```bash
bash scripts/midterm_stop.sh
```

### 高级维护

存储删除必须：summary -> preview -> 核对候选/跳过/空间 -> 填写原因 -> confirm。
不要绕过 preview，也不要删除 active task、当前 epoch 或有 read pin 的 rolling 文件。

## 9. 常见问题

### 打不开 8090

```bash
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh
```

健康脚本的固定服务清单仍偏向旧单分支；双分支还要结合 8090 overview 和实际容器。

### 添加摄像头后没有立即运行

如果“保存后立即加入当前运行”未勾选，这是预期。完成配置后去快速启动器选择它。

### 启动按钮不可用

- 所选路数不是 40/60；
- 任务已经 running；
- 摄像头缺少 source_id/RTSP；
- 预设/容器/模型预检失败。

### 双分支运行但无 evidence

确认最终状态是“完整 evidence 链已就绪”，并检查 rolling sink、prefill、event task
gate 和 media-worker，而不是先看 `security.record_requests`；完整预设该 stream 为 0
可以是正常状态。

### 证据视频没有框

检查 DB annotation/timeline、当前显示开关和该事件是否真的有 object。不要用旧 sidecar
文件存在与否作为唯一判断。

### API 文档

8090 当前不代理 `/docs`。接口清单见
`docs/frontend_interface/02_api_inventory.md`；FastAPI 交互式文档仅在内部 `api:8000`。

## 10. 支持信息

遇到问题请保存：

- 页面错误与 apply phase；
- 运行预设和所选 source 数；
- runtime overview/latency；
- event/person/face Redis lag/pending；
- evidence task phase/reason；
- 相关容器日志与 runtime epoch；
- 可复现步骤。
