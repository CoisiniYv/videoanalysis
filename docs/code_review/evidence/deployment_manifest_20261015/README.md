# 正式部署文件摘要

- 根目录：`/home/user/video-analytics`，SSH `uos157`，只读采集。
- 采集窗口：`2026-10-15 20:20:38.501672–20:20:38.765443 KST`；原始 UTC 保留在 manifest.json。
- 来源 `manifest.json.file_count`：1294 个普通文件；`errors=[]`，`unstable=[]`，29 个排除项，0 个符号链接。
- `SHA256SUMS` 是相对根目录的排序 SHA256 清单；不包含文件内容。
- 排除策略、路径、元数据和采集时间以 manifest.json 为准；采集脚本一并保存，未在远端生成文件。
- 该清单替代本次不可取得的 Git 版本记录，不代表正式目录具有可验证的 Git 提交或干净状态。
