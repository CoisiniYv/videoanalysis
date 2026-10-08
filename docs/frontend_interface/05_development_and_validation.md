# 前端开发与验证交接

## 1. 开始开发

```bash
cd /home/user/video-analytics
git status --short
git rev-parse --short HEAD
```

如果另一个 Codex/同事正在做 Worker 优化：

- 前端开发默认只修改 `services/evidence-viewer/**`、必要的 `services/api/**`、对应
  `harness/tests/**` 和本目录；
- 不修改 `services/clip-worker/**`、`services/media-worker/**`；
- 不使用 `git add -A`；
- 提交时按精确路径 stage；
- 如果确需修改 `db/migrations`、`runtime_apply.py`、evidence status 或 compose，先与
  Worker 优化负责人确认，因为这些是潜在交叉面。

## 2. 无构建前端开发模式

当前前端没有 npm build。代码改动直接来自 bind mount：

```text
../services/evidence-viewer:/app:rw
```

普通 HTML/CSS/JS/Python 代码修改验证时 recreate 服务，不 rebuild：

```bash
docker compose -f infra/docker-compose.midterm.yml up -d \
  --no-build --force-recreate --no-deps evidence-viewer
```

修改 API Python 时：

```bash
docker compose -f infra/docker-compose.midterm.yml up -d \
  --no-build --force-recreate --no-deps api evidence-viewer
```

只有 Dockerfile、requirements 或 base image 改变时才 rebuild。

## 3. 浏览器缓存

`index.html` 手工给静态资源附加版本查询参数：

```html
/static/style.css?v=...
/static/operator.js?v=...
/static/trajectory.js?v=...
/static/evidence.js?v=...
/static/maintenance.js?v=...
```

修改资源后同步修改对应 `?v=`。否则现场浏览器可能继续使用旧 JS/CSS，造成“后端已改、
页面没变”的假故障。

验证时至少使用一次：

- hard reload；或
- 新的无痕/干净浏览器 profile。

不要把“清浏览器缓存”作为长期修复；版本字符串必须进入代码提交和静态测试。

## 4. 最小静态验证

### 4.1 JavaScript syntax

```bash
node --check services/evidence-viewer/app/static/operator.js
node --check services/evidence-viewer/app/static/trajectory.js
node --check services/evidence-viewer/app/static/evidence.js
node --check services/evidence-viewer/app/static/maintenance.js
```

### 4.2 Python syntax

```bash
python -m compileall -q services/evidence-viewer/app services/api/app
```

### 4.3 Whitespace/config

```bash
git diff --check
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
```

## 5. 测试矩阵

### 5.1 Operator shell、人员、轨迹

```bash
pytest -q \
  harness/tests/test_operator_face_registration_static.py \
  harness/tests/test_operator_roi_editor_static.py \
  harness/tests/test_operator_runtime_overview_static.py \
  harness/tests/test_operator_storage_maintenance_static.py \
  harness/tests/test_api_people_face_registration.py
```

### 5.2 Evidence list/detail/overlay

```bash
pytest -q \
  harness/tests/test_evidence_viewer_alarm_machine_time.py \
  harness/tests/test_evidence_viewer_database_index.py \
  harness/tests/test_evidence_viewer_frame_identity_static.py \
  harness/tests/test_evidence_viewer_materialization_state.py
```

### 5.3 Camera、ROI、runtime apply

```bash
pytest -q \
  harness/tests/test_api_cameras.py \
  harness/tests/test_camera_runtime_apply_service.py \
  harness/tests/test_runtime_overview_api.py
```

### 5.4 Maintenance

```bash
pytest -q \
  harness/tests/test_api_people_delete_maintenance.py \
  harness/tests/test_api_storage_maintenance.py \
  harness/tests/test_storage_maintenance_evidence_preview.py \
  harness/tests/test_storage_maintenance_face_preview.py \
  harness/tests/test_storage_maintenance_path_safety.py
```

### 5.5 Trajectory backend and thumbnail cache

下列测试分别操作 `face-worker/app` 和 `api/app` import roots。为避免与同一 pytest
进程中其他 `app` package 缓存冲突，当前应各自单独运行：

```bash
pytest -q harness/tests/test_trajectory_query.py
pytest -q harness/tests/test_trajectory_thumbnail_cache.py
```

如果以后统一测试 package/import layout，应删除这一运行顺序限制并确保全目录 collect
无错误。

### 5.6 Deployment contract

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
```

## 6. Runtime Smoke

### 6.1 Service health

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8090/health
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/evidence/health
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/cameras
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/people?limit=1
```

证据 health 应显示 `index_source=database`。

### 6.2 Proxy failure semantics

临时 API 不可用时，8090 `/api/v1/*` 应返回 502/504 envelope，而不是 HTML traceback。
恢复 API 后页面刷新应重新成功，不要求重启浏览器。

### 6.3 页面人工验收

#### Cameras

- 可读摄像头名作为主标签；
- 新增/编辑/启停分别显示 DB 保存和 source apply 结果；
- JPEG preview 能加载；
- canvas 点击生成的 ROI 保存后位置一致；
- 保存规则后 generated runtime config 更新；
- support blocked 的算法不能在普通模式误保存。

#### People

- 人员列表和 gallery 显示真实图片；
- 新人员与追加人脸模式不会混淆 hidden `person_id`；
- 多选图片只提交给当前人员；`PARTIAL` 时保留成功图片并显示逐张失败原因；
- 上传失败展示 backend error code/message；
- 删除入口只打开维护 preview。

#### Trajectory

- system ID 和 external ID 都能解析；
- camera/time filters 正确进入 query；
- 每页 50，`has_more=false` 时 next disabled；
- 快速重复查询不会被旧响应覆盖；
- 图片 inline 展示，失败后只做一次 fallback；
- 从人员详情和 identity evidence 都能跳入正确人员。

#### Evidence

- list health 为 database；
- 可读摄像头名搜索有效；
- all 不混入 identity，identity 分类可单独查看；
- image evidence 不显示空 video 黑区；
- video 可播放且 canvas 随 current frame 更新；
- person/face/behavior bbox 不出现在错误帧；
- 生成中/失败 bundle 不伪装可播放；
- 删除入口进入 preview，执行后 list 刷新。

#### Runtime

- overview、control、performance、topology 均加载；
- save 与 apply 文案区分；
- active evidence guard 的错误详情可读；
- source 行展示 camera name，并保留必要的内部 ID 次级信息；
- 高影响操作保留 confirm。

#### Maintenance

- summary 容量数据正确；
- execution control 变更要求 reason；
- preview 展示候选、跳过、预计 bytes 和 expiry；
- 无 preview/reason/token 不能 execute；
- stale preview 会要求重新预览；
- 删除后提示不会自动重新生成证据。

## 7. 接口变更流程

修改 backend interface 时按顺序：

1. 修改 schema/router/repository；
2. 增加 API contract test；
3. 修改前端 consumer；
4. 修改 static DOM/JS contract test；
5. 更新本目录的 endpoint 和 data contract；
6. recreate API/evidence-viewer；
7. 运行 runtime smoke。

可用以下只读命令重新提取 OpenAPI 路径：

```bash
PYTHONPATH=services/api python - <<'PY'
from app.main import app
for path, item in sorted(app.openapi()["paths"].items()):
    methods = ",".join(method.upper() for method in item if method != "parameters")
    print(f"{methods:20} {path}")
PY
```

OpenAPI 不包含当前 WebSocket endpoint，需另外检查：

```bash
rg -n '@router.websocket' services/api/app/routers
```

## 8. 代码审查清单

### 8.1 API

- 是否保持 `{data,error,request_id}`；
- 是否为非 JSON 响应明确 content type；
- Query bounds 是否与 UI 输入一致；
- 分页是否返回 total/limit/offset 或 has_more；
- media URL 是否为 `/media/...`，而不是本机绝对路径；
- 摄像头是否返回可读 `camera_name`；
- 新状态是否更新 playable、label、API 和 tests；
- destructive action 是否仍为 preview/execute；
- error details 是否会被公共 request 保留。

### 8.2 JavaScript

- 是否存在未转义的用户/backend 字符串进入 `innerHTML`；
- async 请求是否可能由旧响应覆盖新 selection；
- 是否正确 disable/restore busy button；
- module 是否只通过小型 public bridge 跨文件；
- 是否新增 global name collision；
- media/binary 是否误用 JSON `request()`；
- 页面离开后 animation/timer 是否停止；
- localStorage 解析是否有 try/catch；
- cache-buster 是否更新。

### 8.3 CSS/DOM

- class 是否带 view 前缀；
- 是否误影响 `.pane`、`button`、`img` 等全局 selector；
- DOM ID、JS lookup 和 test 是否同步；
- 1260/860/540 三个断点是否可用；
- hidden internal fields 是否仍隐藏；
- image evidence 是否避免空黑区；
- keyboard button/form semantics 是否保留。

## 9. 与 Worker 优化的接口边界

另一个任务正在优化 `clip-worker` 和 `media-worker`。前端同事可以独立修改 UI，但以下
字段属于双方合同，任何一方变化都必须同步：

```text
evidence_state / evidence_reason
materialization_status / materialization_reason
raw_clip_available / raw_clip_url
playback_kind / image URLs
annotations records
sink timeline records
frame_uuid / frame_pts / stream_session_id / runtime_epoch_id
evidence bundle list pagination
runtime overview evidence state counts
```

Worker 可以改变内部模块、线程、lease 和 queue，但不能让前端根据临时内部 phase 拼
媒体 URL，或删除已有兼容状态而不更新 API contract。

如果 Worker 分支新增 migration/status 字段，前端分支合并后必须重新运行 evidence、runtime
和 deployment tests，再做一次真实 evidence smoke。

## 10. 已知技术风险

1. `operator.js` 与 `evidence.js` 体积大，仍有共享全局依赖；
2. 没有模块 bundler，script order 是隐式 build graph；
3. CSS 全局作用域，局部改动可能跨 view 回归；
4. evidence overlay 是复杂视觉对齐代码，不适合在纯 UI 重构中顺带修改；
5. 当前 8090 HTTP proxy 不支持 WebSocket upgrade；
6. 当前 API/Operator 没有统一 authentication/RBAC；
7. dependency 版本主要是最低版本约束，重新 build 可能产生漂移；
8. `test_trajectory_thumbnail_cache.py` 目前需要独立 pytest 进程。

## 11. 交付完成条件

交给下一位前端同事前，应具备：

- 本目录与当前 HEAD 对齐；
- 工作树干净或所有改动都有明确 owner；
- JavaScript syntax、targeted pytest、compose config、diff check 通过；
- 8090 六个页面人工 smoke 记录；
- API 变更包含 schema/router/repository/test/docs；
- cache-buster 已更新；
- 未把 Worker 或无关改动混入前端提交；
- remaining risks 在交接说明中明确，而不是口头传递。
