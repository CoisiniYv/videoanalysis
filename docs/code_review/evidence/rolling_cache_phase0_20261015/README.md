# 阶段 0 只读证据索引

本目录只保存本地生成的核查证据，不是维修脚本入口。正式主机未落采集脚本或输出文件。时间以各文件原始 UTC 为准；报告展示 KST。

| 目录 | 内容 |
|---|---|
| `storage/` | 三个路径的 realpath/findmnt/df、lsblk 物理链、遗留目录占用及逐命令返回码 |
| `snapshot/` | 全部 midterm 容器完整 inspect、配置文件存在性/摘要、Git 信息取得失败原文、media-worker 恢复快照逐键对比 |
| `business/` | 当前摄像头、24h 任务只读 SQL 和 sink 30min 日志；另保留超出 ART 限定的历史库读取审计，不能用于 T0.3 主判断 |
| `plan/` | 正式 Compose 只读渲染、卷/环境/镜像/CPU 对比、8090 GET、方案 A 阻断证据 |
| `t02/` | 10-13/10-12 ART 只读复盘、字段摘取与日志统计；独立于当前业务读数 |

## 完整快照与凭据保管

完整未脱敏 Docker inspect 位于 `snapshot/pre_repair_snapshot.json.aes256gcm`。同名 `.encryption.json` 保存 AES-256-GCM 的 IV、认证标签和明/密文 SHA256；`snapshot/pre_repair_snapshot.json` 是保留全部结构及非敏感值的脱敏审查副本。完整 `Config.Env` 在密文中，没有因脱敏丢弃。plan 原始采集同样有三组密文/元数据/脱敏副本。

密钥在 `/Users/khonsu/.codex/private-evidence/rolling_cache_phase0_20261015/snapshot-aes256gcm.key`，不在 Git 中；原始明文同时保留在该私有目录。已验证所有密文解密后逐字节与原件一致。不要向远端传密钥，不要直接提交原始环境。哈希标记只能用于相等性审查，不作为已知原值。

本机只读核验示例（只打印是否完整，既不打印环境也不写回任何服务器）：

```javascript
const fs = require('node:fs');
const crypto = require('node:crypto');
const source = 'docs/code_review/evidence/rolling_cache_phase0_20261015/snapshot/pre_repair_snapshot.json';
const meta = JSON.parse(fs.readFileSync(source + '.encryption.json', 'utf8'));
const decipher = crypto.createDecipheriv('aes-256-gcm', fs.readFileSync(meta.private_key_path), Buffer.from(meta.iv_hex, 'hex'));
decipher.setAuthTag(Buffer.from(meta.auth_tag_hex, 'hex'));
const bytes = Buffer.concat([decipher.update(fs.readFileSync(source + '.aes256gcm')), decipher.final()]);
console.log(crypto.createHash('sha256').update(bytes).digest('hex') === meta.plaintext_sha256);
```

## 可复核边界

快照保存的是采集时容器状态，不能替代未来维护前重采。正式目录无 Git 元数据且 `git` 不可用的缺口原样保留；配置文件 SHA256 不能冒充部署 commit。当前没有发布样本或任务时，分位数、增长趋势和业务验收均为未核实，不填 0。读过的历史库补充和 ART 当轮 cohort 明确分开。
