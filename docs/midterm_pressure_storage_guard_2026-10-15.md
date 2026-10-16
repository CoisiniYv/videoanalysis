# 压测缓存存储门控与恢复修复

日期：2026-10-15，展示时区 Asia/Seoul（KST，UTC+9）。

## 结论与授权

**阶段 1 修复与重验达到交审标准，当前修复待提交推送后交 Opus 审查。** 最新授权后的第 1 轮全量 harness 为 2029 passed / 11 failed / 74 skipped / 1 collection error，失败名单为基线 `98d388c` 的子集，无新增失败或收集错误；新增集合 187 项为 186 passed / 1 skipped / 0 failed / 0 error。当前源码在真实辅助容器上的只读预检正向返回 0、反向返回 2，30 个正式容器前后一致。前次 17 个失败的停止记录和原始证据保留，不以旧轮 11 个失败代替本次重验。未进入阶段 2。

阶段 1 基于阶段 0 最新提交 `417d45827224a5d427b702506cf1072d5d6ffdda`，分支为 `codex/rolling-cache-storage-guard-20261015`；提交和审查结果见下节记录。代码只修改 `scripts/runtime/` 和 `harness/`，另补本开发文档和规格状态。正式服务、正式 checkout、已有数据和镜像未修改。

用户已接受当前正式服务的机械盘挂载，并明确越过 T0.3⑤，见 [阶段 0 决策](code_review/rolling_cache_phase0_decision_2026-10-15.md)。阶段 2 每轮恢复的缓存源迁往 NVMe 已预批准；这不是维护窗口批准。60 个历史源、4020 秒保留期和 8090 的 40 路配置保留为已知漂移。

本工作实现 [任务书](midterm_rolling_cache_storage_taskbook_2026-10-14.md) T1.1–T1.5，以及用户追加 T1.6。相关规格：[滚动缓存取证计划](../specs/30_midterm_rolling_cache_evidence_plan.md)。阶段 1 代码已推送，Opus 审查尚未执行，不得进入阶段 2。

### 最新用户规则（仅阶段 1）

用户明确批准修复六项失败与参数非法时的最小报告，并替代此前阶段 1 的自修停止规则：

- 本分支开发中出现的 harness 失败、预检工具缺陷，自行修复，不必停下请示。
- 交审前硬性要求：失败集合必须是基线 `98d388c` 的子集（逐用例名单比较）；实机纯预检正向返回 0、反向返回 2；30 个正式容器前后一致。
- 仅在需要动正式服务、开压测维护窗口、改变任务书范围或做设计取舍、或者 3 轮自修后仍达不到交审标准时停止问用户。
- 阶段 2 每轮维护窗口仍须用户单独批准。上述授权不包含压力负载、服务重建、数据删除、镜像重建或 main 合并。

本次为最新授权后的自修第 1 轮；定向测试先验证新增失败报告用例为红，再实现后转绿。没有启动第 2 或第 3 轮交审重验。

## 现象、影响与根因

已确认：基线脚本的多个 Compose 重建和恢复调用遗漏相邻 storage override，默认落到 `/data`。恢复还可能重新叠加本轮 tmpfs，或者通过 CPU 恢复重建 worker 覆盖刚恢复的环境。现有正式服务的实际源位于机械盘，但用户根据零启用摄像头、最近 24h 零正式任务接受当前影响；这不能证明带负载取证健康。

T0.2 已从历史日志恢复部分事件的最后观测重试原文，完整结果在 [10-13 复盘](code_review/pressure60_allalg_uos157_1h_2026-10-13.md)。这些日志不是数据库 lifecycle audit，未恢复的数据仍标为无法取得。

## 实现

### T1.1 Compose 与正式恢复

`compose_invocation_args` 统一生成绝对 base/env/storage/dual/tmpfs 文件参数及 profile；`_compose_base_args` 委托该函数。预检 config 和实际 sink/MW up 共用该前缀，MW 仅另加 Env overlay。单路使用 `rolling-cache`，双路使用 `dual-4090-two-source` 与 `rolling-cache-dual`。相邻 `midterm-storage.override.yml` 存在即追加；所有 `up` 使用 `--no-build`。压力 tmpfs override 位于 storage override 之后。`run()` 先写入 shell 引号化命令，再执行，失败也保留首行。

正式恢复从 media-worker 的 Compose 标签解析正式 base，验证工作目录、env 和 storage 文件存在；不依赖历史 ART override。恢复配置清空压力根目录。sink 和 media-worker 保存并严格回读完整原始 Env；CPU 恢复保存 Env、镜像和运行状态，失败回滚同样使用正式恢复配置，保留已知漂移。完整 Env 中的 `$` 为 Compose 转义。若无法精确保留原环境，恢复校验失败，不把默认值当原值。

移除主流程结尾无条件预建整套 operator runtime 的调用，避免其在恢复之后再次覆盖快照环境。缓存服务先恢复，随后恢复运行时 API；最终 CPU 恢复后重读正式缓存挂载，通过才清理本轮临时缓存。核验失败保留临时目录并写入恢复错误。

本次 completion-wake 回归的真实原因是公共 Compose 参数重构后强制读取 `cfg.dual_shard_same_gpu`，破坏了 media-worker 原有最小配置调用路径；已修生产代码，缺失该可选字段时使用原有单路模式。`test_media_worker_completion_wake.py` 的原四项测试没有修改。

三项调度参数核查：`MEDIA_WORKER_COMPLETION_WAKE_ENABLED` 在基线及重构后始终按请求写入并严格回读；`ROLLING_CACHE_MATERIALIZATION_CANDIDATE_LOOKAHEAD`、`ROLLING_CACHE_MATERIALIZATION_AGING_SECONDS` 在 `98d388c` 及修复前都只进快照，缺少显式 pressure 覆盖/核验，属于既有缺口，不能称为本轮删丢。现按基础 Compose 现有默认值 `4` / `60` 显式写入并复用逐项回读；完整原 Env 恢复会恢复原值，不强行恢复为默认值。新增五项测试覆盖两种 wake 请求、三变量逐项未生效拒绝及不同原值恢复。污染检测两项仅适配 mock 的 `additional_profiles` 参数并断言 `("*",)`，不改变被测逻辑语义。

### T1.2 存储预检与回读

`--preflight-only` 在 DB 连接、摄像头禁用、Redis 修改和服务重建前返回。允许写独立 ART 报告及供 Compose 解析的压力 override；不创建缓存目录，不发服务变更命令。失败报告包含 `preflight_recovery=unchanged`，说明前置守卫没有需要撤销的变更。

独立模块 `rolling_cache_storage_preflight.py` 用实际 base/storage/dual/tmpfs/profile 参数渲染 Compose JSON，用输入 ffprobe 结果估算码率。预检和 main 均从 `scripts.runtime.pressure_storage_guard` canonical 路径导入同一异常类。Compose、ffprobe、路径和设备解析失败均写含原因的报告，返回码 2；成功写报告并返回码 0。`--preflight-without-storage-override` 只在纯预检允许使用。CLI 测试通过真实子进程运行脚本，PATH 假 Docker 按 profile 返回 JSON，覆盖 NVMe、机械盘、未知设备及异常报告。

`pressure_storage_guard.py` 以 `st_dev` 和 sysfs 递归追踪分区及 dm/LVM slaves；任一底层盘为 rotational 即拒绝，未知盘位也拒绝。tmpfs 明确记录并放行；`--allow-rotational-rolling-cache` 只放行已解析的机械盘，不能放行未知设备。

参数与配置构造阶段抽为 `_prepare_pressure_run`；CLI 对该阶段失败写最小 `report.json`，含 `status=preflight_failed`、`phase=argument_validation`、原因、`exit_code=2` 和未变更标记，不记录全量配置或凭据。数值类型错误、未知参数、非法业务值、无效 policy 和非法测试开关组合都有真实子进程测试，且断言未调用 Docker/ffprobe。`--help` 正常退出；后续压力运行异常不进入该参数错误处理器。预检成功/失败报告也显式写 `exit_code=0/2`。报告目录本身不可写时返回 2 并向 stderr 报告写入失败。

sink 仅校验其实际读写的 `/media/rolling-cache`，media-worker 校验该目录与 `/media/rolling-cache-materialized`。sink 没有物化目录覆盖时，不能把其未使用的 `/media` 父挂载当作物化输出盘而误拒绝。共享缓存路径必须一致；重建后、正式恢复后分别与各自渲染的期望路径严格比对。

缓存预算为实际保留秒数 × 路数 × 每路字节/秒 × 1.3；物化预算为最大 pre+post 窗口 × 码率 ×（active+finalizer queue）×1.3；证据预算按时长、冷却、算法数和最大窗口计算。同一文件系统合并预算，缓存文件系统扣除估算后须保留至少 20%；证据盘与 `/dev/shm` 单独记录。估算是保守投影，不是实测流量上界。

报告字段：`rolling_cache_storage`，含命令和文件、来源路径、设备链、文件系统、rotational、空间及预算，另含 `post_recreate`、`post_restore`。Compose 全量环境仅用于内存解析，不写入预检存储报告。

### T1.3 实际运行配置

直连滚动缓存读 `replay-raw-fanout-a/-b`，其他模式读 `analysis-forwarder-a/-b`。对队龄、strict FPS budget、实际队列容量逐项比对；在容器内对 main/queueing/sampler 三文件计算 SHA256，与当前 checkout 对应源码比对。Savant A/B 回读人脸和 embedding 间隔；ROI 模式明确记录 embedding 间隔不适用。任何缺失或不符抛出异常并进入恢复；不会因为环境值匹配而跳过镜像代码校验。

### T1.4 统计口径

队列满按分片自身容量判断，报告保留旧的合计 2048 阈值供一个版本比较。保留 stale/resync 丢帧计数与 queue head age。sink 日志带 Docker UTC 时间戳，按正式窗口、每路每分钟报告发布实数、期望数和实际保留期；日志未覆盖的窗口标未知，不填零。目录留存切片数仍不能充当整轮发布总数。

### T1.5 规则可复现

`--behavior-rules fall,crowd_gathering,chasing` 从算法注册表读取其他默认参数，冷却统一为 `--pressure-algorithm-cooldown-s`。仅对本轮槽位禁用残留规则，再启用本轮请求集合；事务内逐摄像头回读，不一致即中止。回读写入 `pressure_camera_rules.json` 和所有版本的 `run_config.json`。

### T1.6 基线污染

`baseline_pollution` 独立记录历史源、超正式保留期、启用的历史压测规则及 8090 预期路数与当前启用数的差异。通过 `site_id=pressure`、压力槽位 location 及历史前缀关联自定义源 ID；按单/双 sink 选择容器。无法读取的环境、规则、正式保留值或运行时路数进入 `unverified`。

污染始终 `blocking=false`。存在发现时 `severity=red`，同时生成红色 HTML 警示 Markdown 和终端红字；不修改正式源、保留期、规则或 8090 配置。

### Opus 阻断项修复记录

用户提供的审查提交 `516a31b` 指出 B1/B2 和 M1–M5。当前工作树的处理结果如下：

- **B1 已修复：** 规则回读的 `camera_id` 数组改为 `text[]`；新增真实 PostgreSQL 迁移测试，使用非 UUID 的 TEXT camera ID 执行 `insert_pressure_cameras` 并回读规则。无一次性数据库 URL 时该测试明确跳过，不伪造通过。
- **B2 已修复：** 双路压测重建前快照 `savant-a/b`、`replay-raw-fanout-a/b`，以及请求拓扑中的 `adaface-roi-worker`/decoupled 附加服务，记录完整 Env、Image、Mounts、Labels 和 Running 状态。恢复固定使用正式 base、`operator-dual-runtime.override.yml`、storage override 和快照 Env；原停止服务用 `--no-start`，恢复后对 Env、镜像、挂载、标签和运行状态做双向比对。不存在的可选服务只核验不存在，不清理其他 10-13 残留。
- **M1 已修复：** 每个恢复步骤独立捕获异常，写入 `restore_errors` 后继续；先停双路压测运行时，最终状态在任一恢复差异时为 `failed_restore_verification`，退出码为 1。新增三处注入失败的 `main()` 级测试和 blocked + CPU 隔离路径测试。
- **M4 已修复：** `--preflight-runtime-readback` 在纯预检阶段读取实际 forwarder/Savant 环境和代码哈希；规则注册表默认值在任何服务或数据库变更前解析；配置不一致写报告并返回 2。null-sink 不回读未重建的 Savant。
- **M2 残留边界：** 阶段 2 A 轮使用双路滚动缓存、`force_runtime_restart=true`，正常恢复顺序会先恢复双 sink 再恢复 CPU 隔离，两个缓存端点共同使用正式 storage override。blocked-active-evidence 的提前退出路径不在阶段 2 参数范围，仍保留“CPU 隔离恢复可能单独重建 media-worker”的残留风险，已由测试覆盖并记录，不在本轮扩大设计范围。
- **M3 残留边界：** 单路 rolling-cache-sink 不属于阶段 2；原本不存在的容器现在按不存在处理并删除意外残留后核验，单路路径仍不作为阶段 2 证据。
- **M5 已按阶段 2 范围修复：** 非 rolling-cache 模式只记录 Compose、不做缓存盘门控；ffprobe 无码率时可用 `--rolling-cache-bytes-per-second` 明确回退并标记来源；其余只读运行时回读按实际重建服务选择。阶段 2 A 轮仍使用固定文件输入，不依赖该回退。
- **次要问题：** 冷却统计已加入 `fall`、`crowd_gathering`、`chasing`；污染源判定统一复用共享前缀函数；MPS 停止摘要改为停止后读取。其余非阶段 2 的遗留项保留为风险，不改变正式服务状态。

## 验证与证据

新增测试先写失败用例，再实现对应路径；本次新增参数报告、B1/B2/M1–M5 和调度契约覆盖。最终新增集合以本轮两份 JUnit 差集确定，共 **187 项，186 passed / 1 skipped / 0 failed / 0 error**，单跑退出码 0；跳过项是未配置一次性 PostgreSQL URL 的真实数据库测试，来源为同目录 `new-tests-summary.json` / `new-tests-junit.xml`。同一个冻结解释器和依赖、均移除数据库环境变量，运行全量 harness：

```sh
env -u DATABASE_URL -u VIDEO_ANALYTICS_DATABASE_URL \
  /tmp/pressure60-harness-env/bin/python -m pytest -q harness/tests \
  --continue-on-collection-errors --junitxml=<独立输出>
```

| 口径 | 基线 98d388c | 阶段 1 | 10-12 运行参考 |
|---|---:|---:|---|
| passed | 1839 | 2029 | 不适用，非同套 harness |
| failed | 15 | 11 | 不适用 |
| skipped | 73 | 74 | 不适用 |
| collection error | 1 | 1 | 不适用 |
| 新失败/新收集错误 | — | 0 / 0 | 不适用 |

来源：[验证比较与源码摘要](code_review/evidence/rolling_cache_phase1_20261015/comparison.json)、同目录 JUnit 和依赖锁；[完整失败集合及差异](code_review/rolling_cache_phase1_failure_comparison_2026-10-15.md)。本次最新摘要保留原始 suite 时间并换算 KST。基线原有四个 sink/mock 失败仍消失；本轮新增集合全部无失败，当前失败和 collection error 名单逐项为基线子集。

历史停止轮：22:32:12–22:33:23 KST 得到 2001 passed / 17 failed / 73 skipped / 1 error，其中四项 completion-wake 为配置字段访问回归、两项污染检测为 mock 签名不兼容。最新授权后已按上述不同原因分别修复；原始堆栈、XML、日志和当时对照文档保留于 [修复前历史证据](code_review/evidence/rolling_cache_phase1_20261015/history/harness-before-authorized-repair/)。该历史轮不计为最新三轮授权的修复后重验。

最小验证：两条 Compose config 均通过；deployment contract 与 smoke 均为 25 passed / 4 failed，四项来自基线缺少 CLAUDE/generated sources 和运行脚本白名单落后；doctor 的四项失败为 MAX_FPS_CONTROL、pose 阈值、replay 配置白名单、generated sources，相关配置和验证脚本无本轮改动。不能声称最小验证集全绿。

两条 Compose 渲染在本机 wrapper 中经 SSH stdin 传仓库输入到 uos157 的匿名 memfd，只执行 `docker compose config`。doctor 仅 inspect/配置检查，摘要写本机 `/tmp`。没有服务创建、重建或部署。

### uos157 真实 CLI：历史失败与授权后的迭代 1

宿主 Python 3.7 可以运行 `--help`。新脚本及仓库原版 base/storage/env 放在独立 `/tmp/pressure60-phase1-preflight-20261015`，没有修改正式 checkout；测试输入为只读 RTSP 获取的独立短样本，保留在临时目录，不作为阶段 2 固定输入。

已有执行结果确认：三个纯预检调用均 `rc=1`、stdout 为空、没有 `report.json`，全部在 Compose 路径检查处失败，尚未到达码率、NVMe/HDD 类型或空间门控。因此不能声称真实 NVMe 放行或机械盘拒绝已经验收。来源：[原始输出与边界](code_review/evidence/rolling_cache_phase1_20261015/preflight/README.md)、同目录实际命令、快照与摘要。

源码与 traceback 支持以下原因推断，停止后没有继续运行异常类身份测试：

1. 预检 Compose config 没有启用实际双 sink 所属 profile，缺少 `rolling-cache-sink-a`，抛出 `StoragePreflightError`。
2. preflight 模块先按顶层模块名导入 guard，而 main 按 `scripts.runtime` 包名导入；同一源码可能加载为两个模块，异常类身份不同，使主流程捕获不到该异常，未按契约写失败报告。

三个调用由一个已启动的只读采集器连续执行，读取返回结果后发现缺陷，立即停止后续工作，没有继续修复、新增测试或重跑。前后 30 个正式容器按 Mounts 的 Destination/Source/Type 排序后快照完全一致，ID、Image、StartedAt、RestartCount、Env SHA 均无差异；原先仅有 10 个容器的 Mounts 数组顺序不同，不能把该数组顺序当服务变化。

远端实际 main 的 SHA256 为 `7703ffdfc004fe421fecb6737f68a3d853ce19116dcc5d5a69543ff70846058a`，未包含本机随后补充的恢复读回/退出码修改。不能把该 CLI 结果称为最终工作树完整验收；本机最终测试树精确 SHA256 另在 `comparison.json`。

上述为最新授权前的历史停止记录，原产物保留。用户之后明确放行两处修复与只读重试。

**迭代 1：通过。** `2026-10-15T22:33:23.523869+09:00` 至 `22:33:25.438974+09:00`，最新源码上传独立 `/tmp/pressure60-phase1-preflight-20261015-r1`，只复用既有短输入。正常 storage override 的 CLI 返回 0，两个缓存路径均识别为 dm-2 → nvme0n1p3 → nvme0n1，rotational=false；不带 storage override 返回 2，识别 `/data` → sda1 → sda，rotational=true 并拒绝。两次均写报告和原因，stdout/stderr 均为空，原样保留。

前后 30 个正式容器完整比较范围内相同；源代码 main SHA256 为 `7ee707b205b03cea42d4a411aff6f8fd8b4372c764032cb6d427fe56403d5dcf`。命令、退出码、原始输出、报告、源码摘要与前后快照见 [历史迭代 1 证据](code_review/evidence/rolling_cache_phase1_20261015/preflight-r1/README.md)。随后全量失败扩大曾依当时规则停止；用户最新已批准修复并调整阶段 1 自修规则。

**最新授权后第 1 轮交审重验：通过。** `2026-10-15T22:47:25.613763+09:00` 至 `22:47:27.545463+09:00`，最新完整源码在独立 `/tmp/pressure60-phase1-preflight-20261015-r2` 运行，复用既有输入。NVMe 返回 0，机械盘返回 2，两次报告均含原因和退出码。30 个正式容器前后一致。全部原始输出、报告、退出码及精确源码哈希见 [最新实机证据](code_review/evidence/rolling_cache_phase1_20261015/preflight-r2/README.md)。目录 r2 只延续历史命名，不代表本次授权已消耗两轮。

**当前工作树真实辅助容器复验：通过。** `2026-10-17T00:23:26.545690+09:00` 至 `00:23:28.662632+09:00`，当前源码在 `uos157` 的独立 `/tmp/pressure60-phase1-preflight-20261016` 运行。正常 storage override 返回 0 并识别两个 NVMe 缓存路径；省略 storage override 返回 2 并识别 `/data` 的 rotational=true。两次均写报告，stdout/stderr 为空；前后 30 个正式容器完全一致。完整命令、报告、原始输出、容器比较和当前源码哈希见 [当前实机证据](code_review/evidence/rolling_cache_phase1_20261015/preflight-r3/README.md)。

### 推送与审查状态

阶段 0 最新提交为 `417d45827224a5d427b702506cf1072d5d6ffdda`，author/committer 均为 `2026-10-15T22:29:49+09:00`。含用户要求的历史脚本差异、1,225 项视频调度假设及前次修复授权。阶段 1 初始实现提交为 `fc77792dba21c0a67fbe8e3c446bf7512dd5f21b`，author/committer 均为 `2026-10-15T22:54:15+09:00`；本轮 Opus 阻断项修复提交为 `a16f926ac7f164b3760004faa1d267124c858413`，author/committer 均为 `2026-10-17T00:40:24+09:00`。

本轮修复已按 Seoul 时间提交；推送完成后在此记录推送开始/完成和 `ls-remote` SHA。审查通过前不进入阶段 2。

最初三次推送失败：20:23:31 KST 开始的连接尝试失败；20:26:06 KST 的 HTTP/1.1 尝试超时；20:28:12 KST 经本机代理连接到 GitHub 后认证失败。前两次完成时刻没有精确采集。用户随后说明已在本机登录，核实 `/opt/homebrew/bin/gh` 的 CoisiniYv 认证恢复有效。

已成功推送阶段 0 的 `ba8f262` 至 [GitHub 分支](https://github.com/CoisiniYv/videoanalysis/tree/CZray/pressure60-allalg-review-20261014)：push 开始 `2026-10-15T21:12:30+09:00`（原始 `2026-10-15 12:12:30 UTC`），客户端成功完成的确认时刻 `2026-10-15T21:13:07+09:00`（原始 `2026-10-15 12:13:07 UTC`）。最新 `417d458` push 开始 `22:32:14+09:00`，成功完成确认于 `22:33:12+09:00`，随后 `ls-remote` 核验远端 SHA 一致。开始时间来自 KST date 输出（原格式 offset 显示异常，时分秒有效）；完成是客户端观察时刻，不是精确的服务端接收时间。阶段 1 因新的全量 harness 停止条件未提交/推送，尚未交 Opus 审查，未进入阶段 2。

## 非目标、回滚与风险

不改变 sink 持久化算法、线程数、镜像、取证调度和正式部署；不执行方案 A/B、压力负载或阶段 2。现有清理开关不是本次授权，阶段 2 执行前仍要按用户禁止删数据的约束生成精确执行单。

代码回滚点为阶段 0 的 `417d458`，在独立 worktree 选择旧提交即可；旧脚本有已确认存储漏洞，不能拿回滚当作批准运行。正式服务尚未改动，无本轮服务回滚操作。

已确认：最新实机只读正反预检符合预期、30 容器前后一致、当前全量失败集合为基线子集、三调度参数覆盖与核验、非法参数最小报告、代码已推送。推断：保守空间预算能减少容量不足风险，但受输入码率和事件密度影响。未核实：Opus 实际审查、带负载重建后的真实读回与所有退出路径恢复；后者必须在批准的维护窗口中验证。当前镜像若与源码不符，T1.3 将中止，不能自动重建镜像绕过。

推送、Opus 审查与阶段 2 维护窗口是独立门槛，缺一项不得进入下一阶段。所有新提交验证 author/committer 为 `+09:00`；推送开始和完成单独记录，不用提交时间冒充 GitHub 接收时间。
