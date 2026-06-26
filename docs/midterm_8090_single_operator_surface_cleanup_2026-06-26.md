# 8090 单一操作台入口清理记录

记录日期：2026-06-26

## 结论

当前 8090 端口只使用 `services/evidence-viewer` 这一套页面。

实际入口：

- 对外服务：`evidence-viewer`
- 编排文件：`infra/docker-compose.midterm.yml`
- 端口映射：`8090:8090`
- 页面目录：`services/evidence-viewer/app/static`
- 页面路由：`/` 和 `/operator`
- 内部业务接口：由 `evidence-viewer` 将 `/api/v1/*` 代理到内部 `api:8000`

被清理的旧入口：

- 旧目录：`services/api/app/static/operator`
- 旧静态路由：`/operator/static`
- 旧页面路由：内部 API 服务自己的 `/operator`

清理后，`services/api` 只保留业务 API 和 `/media` 静态媒体挂载，不再作为第二个操作台页面宿主。

## 为什么清理

审计发现仓库里同时存在两套操作台静态文件：

- `services/evidence-viewer/app/static/*`
- `services/api/app/static/operator/*`

但中期主线实际对外只暴露 8090，也就是 `evidence-viewer`。内部 `api` 服务只通过 Compose 网络暴露 `8000`，不直接暴露宿主机端口。继续保留 API 侧旧页面会造成两个问题：

- 后续改页面时容易改错目录。
- 旧 `/operator/static` 页面可能和 8090 当前页面能力漂移。

因此本次将 API 侧旧页面完全移除，让 8090 操作台只有一个权威来源。

## 本次代码改动

删除：

- `services/api/app/static/operator/app.js`
- `services/api/app/static/operator/evidence.js`
- `services/api/app/static/operator/index.html`
- `services/api/app/static/operator/style.css`
- 空目录 `services/api/app/static/operator`

修改：

- `services/api/app/main.py`
  - 移除 `OPERATOR_STATIC_DIR`
  - 移除 `/operator/static` 静态挂载
  - 移除 API 服务自己的 `/operator` 页面路由
- `harness/tests/test_operator_face_registration_static.py`
  - 断言 8090 页面仍由 `evidence-viewer` 提供
  - 断言 API 侧旧静态目录不存在
  - 断言 API 侧不再声明 `/operator/static` 或自己的 `/operator`
- 相关主线文档和规格
  - 将旧 `services/api/app/static/operator/*` 引用改为 `services/evidence-viewer/app/static/*`
  - 明确 API 侧旧页面已经移除

## 运行时重启

本次没有 rebuild 镜像。因为主线 Compose 对 `api` 和 `evidence-viewer` 都使用源码 volume 挂载，重启进程即可加载代码变化。

执行命令：

```bash
docker compose -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate --no-deps api evidence-viewer
```

重启结果：

- `video-analytics-midterm-api` 已重新创建并启动。
- `video-analytics-midterm-evidence-viewer` 已重新创建并启动。
- 8090 仍映射为 `0.0.0.0:8090->8090/tcp`。

## 验证结果

服务状态：

```text
video-analytics-midterm-api               Up
video-analytics-midterm-evidence-viewer   Up, 0.0.0.0:8090->8090/tcp
```

8090 健康检查：

```text
GET http://127.0.0.1:8090/health
{"status":"ok","evidence_root":"/evidence","read_only":true}
```

8090 页面检查：

```text
/          -> 200，包含“视频分析操作台”，不包含 /operator/static
/operator  -> 200，包含“视频分析操作台”，不包含 /operator/static
```

内部 API 路由检查：

```text
api_has_operator_route = False
api_operator_routes = []
```

静态和单元验证：

```bash
pytest -q \
  harness/tests/test_operator_face_registration_static.py \
  harness/tests/test_operator_storage_maintenance_static.py \
  harness/tests/test_operator_runtime_overview_static.py
```

结果：

```text
28 passed
```

语法检查：

```bash
python -m py_compile services/api/app/main.py services/evidence-viewer/app/main.py
node --check services/evidence-viewer/app/static/operator.js
node --check services/evidence-viewer/app/static/evidence.js
node --check services/evidence-viewer/app/static/maintenance.js
git diff --check
```

结果：全部通过。

## 当前权威开发位置

后续 8090 页面相关开发只应修改：

- `services/evidence-viewer/app/static/index.html`
- `services/evidence-viewer/app/static/operator.js`
- `services/evidence-viewer/app/static/evidence.js`
- `services/evidence-viewer/app/static/maintenance.js`
- `services/evidence-viewer/app/static/style.css`

不要再新增或恢复 `services/api/app/static/operator/*`。
