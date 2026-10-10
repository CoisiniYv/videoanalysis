# 8090 新页面本地兼容修复与上线准备（2026-10-10）

本次范围由用户确定为“先完成新版 8090 页面及兼容修复”。服务器未开机，
因此本次完成本地集成、回归测试、离线浏览器验证和静态更新包准备。
后台吞吐、排队、冷却间隔和事件居中校验不在本次修改范围内。

## 代码来源

- GitHub remote：`github`，仓库 `CoisiniYv/videoanalysis`。
- GitHub main：`d5d52c350eefbcfca20e7b96d679da4be2764994`。
- 新页面分支：`claude/8090-ui-refresh-20261010`。
- 新页面集成点：`dda79149cf8619efab6c9eea00a951ac7dc1a949`。
- 新页面基于 main，仅新增前端布局及说明；main 在拉取时没有更新。
- 本地修复分支：`CZray/8090-ui-compat-20261010`，包含上述新页面及本次兼容修复。
- 工作目录：`/Users/khonsu/.codex/worktrees/pressure60-main/SavantProject`。
- 原始 `/Users/khonsu/SavantProject` checkout 和已有压测报告保留。

## 已修复的问题

1. 路线节点合并同一摄像头记录后，显示的时间属于最早记录，点击却打开最新记录。
   现在节点显示并打开该组最新记录；再次返回同一摄像头仍保留为另一站。
2. 轨迹旧页也标“起点/最近”，容易被理解为完整轨迹。
   现在路线和统计明确标为本页，节点标“本页最早/本页最新”。
3. 长人员名称、长摄像头名称和查询摘要会撑出页面宽度。
   现在概况和路线限制宽度，标题省略并保留完整 title，查询摘要换行。
   390px 和 1280px 视口均验证无水平页面溢出。
4. 轨迹图片 `hidden` 被 CSS 的 `display: block` 覆盖。
   增加明确的 hidden 样式，无图、失败和重置时不显示破图占位。
5. 列表的待复核录像在详情中显示“已验证”；生成中也会显示“已验证”。
   现在详情按 clip/materialization/visual 状态显示，待复核有明确提示；
   缺少图片地址显示“图片暂不可用”，不会声称图片已就绪。
6. 旧人脸命中缺少 `person_id` 时仍提示“点击查看此人轨迹”。
   现在保留证据详情入口；有人员 ID 时仍沿用 `operatorTrajectory.openForPerson`。
7. 卡片中的待复核标签重复，生成异常露出 `generated_corrupt`，以及未就绪记录显示验证标签。
   现在去除重复提示、使用中文状态并按媒体可用性展示验证标签。

这些修改只涉及显示和前端导航，不改 UUID/PTS 对齐、推理频率、事件生成或证据落盘逻辑。
`operator.js`、`maintenance.js`、服务端 API 和数据库均未修改。
CSS 与两个页面脚本的资源版本追加 `compat-20261010`，部署后页面会请求新资源。

## 本地验证

修改前新增的行为测试成功复现路线时间错误、旧页标签误导、缺 ID 跳转和错误验证状态。
修改后执行：

```sh
node --test harness/tests/js/evidence_viewer_ui.test.cjs
node --check services/evidence-viewer/app/static/trajectory.js
node --check services/evidence-viewer/app/static/evidence.js
uv run --python 3.12 --with pytest --with pyyaml --with fastapi --with httpx --with 'psycopg[binary]' python -m pytest -q \
  harness/tests/test_evidence_viewer_alarm_machine_time.py \
  harness/tests/test_evidence_viewer_database_index.py \
  harness/tests/test_evidence_viewer_frame_identity_static.py \
  harness/tests/test_evidence_viewer_materialization_state.py \
  harness/tests/test_operator_face_registration_static.py \
  harness/tests/test_operator_roi_editor_static.py \
  harness/tests/test_operator_runtime_overview_static.py \
  harness/tests/test_operator_storage_maintenance_static.py \
  harness/tests/test_evidence_viewer_ui_behavior.py
git diff --check
```

- Node：11 个行为场景通过，无跳过。pytest 中的一个用例调用同一 Node 测试，不能重复计数。
- pytest：74 项通过，其中 73 项是原有相关兼容检查，1 项运行新增行为测试。
- 两个脚本语法和 diff 空白检查通过。
- Python 离线预览工具与 pytest 包装器通过编译检查。

离线浏览器使用实际 `index.html` 和所有页面脚本/CSS，模拟已有 API 格式。
`scripts/tools/preview_8090_ui.py` 仅监听 `127.0.0.1`，写操作返回 405，
不访问真实 API、数据库或远端。画面为 SVG 模拟图和 2 秒合成测试视频。

已在浏览器实际验证：

- 证据列表每页 50 条，第二页 51–54 条，分类切换。
- 待复核详情/顶部提示，旧人脸记录图片详情，video/canvas 隐藏。
- 有人员 ID 的人脸卡片打开人员轨迹，路线节点打开对应最新记录。
- 轨迹每页 50 条，第二页 51–58 条，分页按钮边界与重置。
- 摄像头筛选、无结果提示、主图失败回退缩略图、无图提示。
- ↑↓ 切换记录，输入框箭头不切换；桌面、390px 手机宽度及深色模式。
- 两类长名称和查询摘要不撑出页面；浏览器未捕获 JavaScript error 日志。

预览截图位于 `/Users/khonsu/.codex/artifacts/8090-ui-20261010/`：
`evidence-desktop.jpg`、`trajectory-desktop.jpg`、`trajectory-mobile.jpg`。
这些结果只证明前端兼容，不代表真实证据画面、生产 API 或一小时负载复测通过。

## 更新包与服务器开机后的操作

本地静态更新包目录：`/Users/khonsu/.codex/artifacts/8090-ui-20261010/`。
`8090-static-update.tar.gz` 仅含四个部署文件及 `manifest.json`：

```text
static/index.html
static/style.css
static/trajectory.js
static/evidence.js
manifest.json
```

manifest 记录新文件 SHA-256、大小、main 的旧文件 SHA-256、新页面来源和本地修复提交，
并记录未修改的 `operator.js`、`maintenance.js` 校验值，方便确认接口脚本配套。
mock fixtures、报告、测试和视频不进入部署包。

依据关机前已保存的静态同步记录，服务器页面目录是
`/home/user/video-analytics/services/evidence-viewer/app/static`，
此前文件为 main `d5d52c3`。开机后应再次核验，不能只依据旧记录覆盖：

1. 确认 SSH 与 8090 可用，读取当前 static bind mount、文件哈希和服务状态。
2. 如果当前文件与 manifest 的已知基线不同，先保留并比较服务器修改。
3. 备份完整静态目录，传入更新包并逐项校验 SHA-256。
4. 在同一目录分别写入临时文件，再原子替换四个文件，最后替换 index.html。
   静态目录已绑定挂载时无需重建 GPU 链路或重建 Compose。
5. 通过 8090 HTTP 再校验实际返回的静态文件哈希；检查缓存版本、真实列表、
   轨迹分页、图片/视频加载，以及配置/运维页面入口。
6. 任一真实页面检查失败，恢复本次备份四文件并重新核验 HTTP。

本地已准备完成；尚未向 GitHub 推送修复，也未部署到服务器。
用户开机后再执行推送/同步和真实接口验证。
