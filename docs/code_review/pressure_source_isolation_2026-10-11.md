# 压测输入隔离与共享 RTSP 工具

日期：2026-10-11，Asia/Seoul（KST，UTC+09:00）。

解码防护一小时复测中，模型侧有 60 路，但宿主机同时还运行着 40 个旧拉流容器。
MediaMTX 到 uos157 的连接数为 100，并出现 `reader is too slow, discarding ... frames`。
仅按本轮 run ID 数到 60 个容器，不能证明整个输入环境只有 60 个 reader。

## runner 修正

`prepare_evidence_guard()` 原先遇到 `force_runtime_restart` 就提前返回，跳过
`quiesce_existing_sources()`。现在强制重启仅允许跳过等待活动证据结束，仍按默认
策略停用旧源；已有的显式 `no_quiesce_before_guard` 配置保持可识别。

在目标源全部可见后，新增 `assert_pressure_source_isolation()` 查询 Docker 中
全部正在运行的动态源和 compose source-adapter，保存 `pressure_source_isolation.json`。
只有运行集合恰好等于本轮目标源集合才能开始正式采样；多余、缺失或 Docker 查询失败
都会中止，避免把带背景拉流的工况误报成纯 60 路验收。即使显式跳过 quiesce，也必须
通过这个正式采样前的检查。

新增回归验证了强制重启的旧源停用、额外动态源、额外 compose 源、缺失源和 Docker
不可用。旧实现会在强制重启用例中失败，修复后相关 6 项通过。

这项隔离修正没有部署进已经开始的一小时 b865999 测试。该轮的真实背景负载、结果、
原始时间和失败状态保留，详见同目录 decoder-guard 一小时报告。

另一个修正是 GPU 遥测：这次 CPU-only API helper 内没有 `nvidia-smi`，107 次
温度/时钟/功耗采样均失败。现在仅当本地二进制缺失时，采集器会通过已有的 Savant
容器执行查询，每次调用限时 5 秒；不创建额外 GPU 容器，也不将驱动错误隐藏为成功。
4 项回归与实际 CPU-only helper 中的查询均通过。正式窗口仍必须先确认能取得数值；
已经结束的一小时遥测不会用后来的兼容检查或 GPU 进程核验回填。

## 一个上游会话共享给本机读者

`scripts/tools/run_shared_rtsp_relay.py` 用 GstRtspServer 的 shared media factory
只读取一个上游 H264 RTSP 会话，再向本机多个 TCP 客户端转发。它不解码、不重编码、
不抽帧，不创建事件或改写证据。每个后端 source adapter 仍负责自己的 source ID。
源 URI 通过属性赋值，不插入 GStreamer pipeline 字符串；日志使用首尔时间。

使用现有固定 Savant 镜像中的 GstRtspServer，不需要下载安装 MediaMTX：

```bash
docker run --rm --network host \
  --name pressure60-shared-input-relay \
  -e GST_PLUGIN_PATH= \
  -v "$PWD":/work:ro \
  --entrypoint python \
  ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1 \
  /work/scripts/tools/run_shared_rtsp_relay.py \
  --source rtsp://192.168.1.105:8554/live/1080movie \
  --address 192.168.1.100 --port 18554
```

随后本机 adapter 使用 `rtsp://192.168.1.100:18554/live/1080movie`。容器不能用
自己的 `127.0.0.1` 访问宿主机 relay。此工具是压测输入工具；启动测试前先保存原
运行配置，停用背景源，结束后停止 relay 并恢复原状态。实际业务摄像头各有独立画面，
共享同一电影只能模拟多源处理负载与同步告警峰值。

自包含原生检查：

```bash
docker run --rm --network none -e GST_PLUGIN_PATH= \
  -v "$PWD":/work:ro --entrypoint python \
  ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1 \
  /work/scripts/tools/check_shared_rtsp_relay.py
```

该检查在容器内生成一个 H264 测试源，通过两个真实 RTSP 客户端解码，断言上游
只有一个 media/session，两客户端均收到可解码帧。真实 1080 电影、多客户端短测及
其限制写入本轮复测报告；短测不能替代一小时模型推理和完整证据业务验收。
