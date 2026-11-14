# 明日方舟陪玩插件（内置 MAA 引擎 · 不代打）

为 [N.E.K.O](https://project-neko.online)（猫娘计划）开发的**只读**明日方舟陪玩插件。
插件进程内通过 `ctypes` 加载 [MaaAssistantArknights (MAA)](https://github.com/MaaAssistantArknights/MaaAssistantArknights)
的 `MaaCore` 动态库，提供当前画面截图、引擎状态、关卡分析、肉鸽建议与陪玩记忆。

> ⚠️ **安全边界：本插件不代打。**
> 不执行战斗、不改客户端、不注入、不修改内存，**也不调用** `LinkStart` / `Fight` /
> `Infrast` / `Recruit` / `Mall` / `Award` / `Copilot` 等任何"会改变账号状态"的任务。
> 代码里的只读白名单只有四项：`Screencap` / `OperBox` / `Depot` / `Debug`。
> `Recruit` 被**显式排除**——它的参数可以触发游戏内确认动作，属于"能改行为"的一类。
>
> **关于"点击"的准确说明**：`OperBox`（干员列表）与 `Depot`（仓库）的任务链由 MAA 在
> 游戏内**点击或滑动**，用来打开对应页面并翻页识别。它们只做页面浏览与识别，不进入
> 战斗、不消耗理智、不改动账号数据。如果你要求"零触摸"，只用截图 / 肉鸽建议即可
> （本轮 OCR 尚未接线，见「已知限制」）。

## 插件身份

| 项 | 值 |
|---|---|
| plugin_id | `arknights_neko_plugin` |
| 名称 | 明日方舟陪玩插件 |
| version | `0.2.0` |
| SDK recommended | `>=0.1.0,<0.2.0` |
| SDK supported | `>=0.1.0,<0.3.0` |
| 入口 | `plugin.plugins.arknights_neko_plugin:ArknightsNekoCompanion` |

三个身份串必须完全一致，任一处不一致宿主就会拒载：

```
目录名                            = arknights_neko_plugin
[plugin].id                       = arknights_neko_plugin
[plugin].entry 的模块路径          = plugin.plugins.arknights_neko_plugin
```

`id` 必须匹配 `^[a-z][a-z0-9_]*$`——不能用连字符。

## 从旧版升级：五处会踩到的行为变化

0.2.0 是**重写**，不是增量更新。装过旧版的用户请先看这张表：

| # | 变化 | 旧版 | 新版 | 你要做什么 |
|---|---|---|---|---|
| 1 | **引擎改为自动下载** | 引擎库放 `native/`、资源放 `resource/`，随仓库分发 | 两者都已删除；首次启动从官方 Release 下载并按 SHA256 校验 | 不用再手动准备 `native/`、`resource/`；网络受限请按 Q2 手动放置 |
| 2 | **配置键变了** | `adb_path` / `adb_address` / `resource_dir` / `maa_log_dir` | `adb_address` / `adb_path` / `verbose_diagnostics` / `max_inline_image_mb` | 删掉旧配置里的 `resource_dir`、`maa_log_dir`（现在不生效）；引擎路径不再可配置 |
| 3 | **入口 ID 全换，且没有 `sync_*` 了** | `arknights_engine_status` / `arknights_sync_operators` / `arknights_sync_depot` / … | `arknights_status` / `arknights_memory` / … 共 7 个（见下文「7 个入口」） | 引用旧入口 ID 的提示词、快捷指令、Agent 配置都要改；**干员/仓库同步这一轮没有实现**，别指望它们还能调 |
| 4 | **`auto_start` 必须为 `true`** | 旧清单是 `false`，需手动启动 | 新清单是 `true`，且这是**功能正确性要求** | 不要改回 `false`：service/runtime 都在 `on_startup` 构造，改回 `false` 会让每个入口恒回"引擎还没准备好" |
| 5 | **目录结构回到扁平** | 插件目录 = 仓库根 | 插件**就在仓库根**（`plugin.toml`、`__init__.py`、`maa/`、`companion/` 全部在根，`docs/`、`tests/` 同层） | 「加载源码目录」直接选**仓库根目录**即可；仓库里没有 `plugin/plugins/arknights_neko_plugin/` 这一层了 |

一处**没变**：`store` 的三个键名仍然是 `operators_cache` / `depot_cache` / `last_stage`，
所以升级后老用户的干员/仓库记忆不会被清空。

## 仓库结构

**仓库根就是插件目录**（目录名 = `plugin_id` = `arknights_neko_plugin`）。没有中间层：

```
neko-plugin/                                  # ← 仓库根 = 插件目录（目录名 = plugin_id）
├── plugin.toml                               # 插件清单
├── config.example.toml                       # 配置模板（[arknights] 四键，随插件一起分发）
├── __init__.py                               # 宿主接线层：7 个入口纯路由 + 2 个 lifecycle 钩子
├── maa/                                      # 引擎侧（纯标准库，可脱离宿主单测）
│   ├── errors.py                             # 稳定错误码 + 异常族 + 脱敏渲染
│   ├── wrapper.py                            # 纯 ctypes 绑定（不含业务判断）
│   ├── adapter.py                            # 只读任务白名单 + 队列/回调桥接
│   ├── provision.py                          # 下载 → 校验 → 解压 → 定位核心库
│   └── runtime.py                            # 七态状态机 + 引导/装载/连接
├── companion/                                # 业务层
│   ├── service.py                            # 唯一把错误变成人话、唯一组织推送的地方
│   ├── logic.py                              # 自动化请求占位（只读三类，绝不执行写任务）
│   └── knowledge.py                          # 静态决策知识（关卡 / 肉鸽规则，零 I/O）
├── tests/                                    # 零依赖测试（不需要 pytest）
├── docs/                                     # 设计定版、测试计划、供应链方案、进度快照
├── README.md
├── ruff.toml                                 # 仓库级 lint 配置（沿用 Market 模板生成）
├── audit-reports/                            # 本轮审计留档
└── .github/workflows/                        # 必须在仓库根，GitHub 才识别
    ├── verify.yml                            # 复用 N.E.K.O 官方 Verify 工作流
    └── release.yml                           # 打 tag 时发布 Release
```

安装方式三选一：

1. N.E.K.O「插件开发」里**加载源码目录**，选择**仓库根目录**；
2. 「打包」成 `.neko-plugin` 后导入；
3. `git clone` 后把仓库根直接指向宿主（克隆出来的目录名如果是 `neko-plugin`
   之类，重命名成 `arknights_neko_plugin` —— 宿主按目录名判包名）。

> 宿主会把 `plugin.toml` 的 `entry` 归一化成 `plugins.<插件目录名>` 再加载，
> 所以 `entry = "plugin.plugins.arknights_neko_plugin:ArknightsNekoCompanion"`
> **不用改**；变的只是"仓库根就是那个目录"，而不是"仓库里再套一层"。

## 首次启动：引擎不随插件分发，自动下载

从 0.2.0 起，**MaaCore 引擎不进版本库、不随插件分发，也没有 `resource/` 目录**。
插件里只有代码；引擎由首次启动时自动获取。

| 项 | 值 |
|---|---|
| 上游版本 | MAA `v6.22.0`（在常量表里 pin 死，不用 `latest`） |
| 资产 | `MAA-v6.22.0-win-x64.zip` |
| 体积 | `294,969,951` 字节（≈ 295 MB / 281 MiB） |
| 校验 | SHA256 `197e3b4c35f56ca75e3fc7cf0a6dfd5467f8aace613bd44626448cf6f8b4756c`（硬编码在 `maa/provision.py` 的 `KNOWN_RELEASES`） |
| 落点 | `data_path("maa")`——**唯一的权威位置**，没有第二处可配置 |
| 允许的主机 | `github.com` / `objects.githubusercontent.com` / `release-assets.githubusercontent.com`（https，仅此三家） |
| 峰值磁盘预算 | ≈ 1.5 GiB（`PROVISION_PEAK_BYTES = 1536 MiB`；下载包 + 解压树 + 旧版本共存 + 临时层叠加） |

**启动不会被下载阻塞。** `on_startup` 只构造运行时并起一个后台任务，开发侧的实测记录是
约 2 ms 就以 `downloading` 状态返回（见 `docs/STATUS.md` §3.1）；进度通过
`arknights_status` 的 `data.download_progress` 查看。

**耗时预期**：真实 295 MB 下载的耗时**未实测**（开发机未跑通完整链路）。按带宽粗算，
100 Mbps 约 25 秒、20 Mbps 约 2 分钟、5 Mbps 约 8 分钟；解压另需数十秒。
这是算术推算，不是实测值——慢网请以 `arknights_status` 的实际百分比为准。

**下载过程的安全约束**（写在实现里，不是写在文档里）：

- 先落 `.part`，算完 SHA256 再 `os.replace` 成正式文件名——不存在"只写了一半"的中间态；
- 哈希不符**必删**（`.part` 与可疑的旧文件都删），不会把可疑样本留在你机器上缓存；
- 解压是"路径校验 + 白名单"两件事：`externals/`（.NET/WPF 运行时，只有 GUI 需要）、
  `MAA.exe`、`*.pdb` 被硬排除，所以落盘比整包小；
- 核心库只允许在 `engine_root/`、`engine_root/bin/`、`engine_root/lib/` 三处命中**一份**，
  命中两份即判歧义失败——不会"挑第一个"。

### 下载失败了怎么自救

先记住三条，其余都是细节：

1. **失败不是永久失败。** 下载 / 校验 / 解压任一步失败，状态都只回 `absent`（可重试），
   不会把插件困在 `failed`。下次启动会自动重试；也可以立刻点 `arknights_engine_repair`。
   `failed` 只留给"装载失败 / 核心库命中多份"这类必须有人动手的情况。
2. **要手动放置，路径只从 `arknights_status` 的 `data.engine_root` 读。** 不要照抄任何
   文档里写死的路径——那个位置取决于宿主的用户数据目录，`data.engine_root` 是**真实绝对路径**。
3. **别放两份同名 `MaaCore.dll`。** 这是新版真正的坑（旧版是"只放单个文件会失败"，
   这条在新版已经不成立了）：`engine_root/`、`engine_root/bin/`、`engine_root/lib/`
   三处只允许命中**一份**，命中两份直接判 `engine.layout_invalid`——
   不会"挑第一个"。

`arknights_status` 返回的 `data.error.code` 是稳定错误码（快照是逐键白名单重建的，
不含绝对路径），另有 `data.missing_pieces` 说明缺哪几件（`library` / `resource` /
`engine.json`）。逐码处置：

| 错误码 | 含义 | 你要做什么 |
|---|---|---|
| `engine.download_failed` | 网络/超时/HTTP 错误 | 检查网络后点一次 `arknights_engine_repair` |
| `engine.download_failed` (`no_space`) | 磁盘放不下 | 清出 ≈1.5 GB，再点修复 |
| `engine.hash_mismatch` | 下到的内容与 pin 住的哈希不符 | 点 `arknights_engine_repair`（带上 `clean=true` 会先清盘再重下） |
| `engine.unpack_failed` | 解压失败（压缩包异常） | 同上，清盘重下 |
| `engine.layout_invalid` | 同一个核心库在三个候选目录里命中多份 | 删掉多余的那一份，只留一份，再点修复 |

`arknights_engine_repair` 只 kick 一次并回报快照，**不接受 URL、不接受路径**——
换源必须改常量表并发版，这是刻意的安全约束。

## ADB 配置

配置只有四个键（`plugin.toml` 的 `[arknights]` 段，或插件设置页）：

```toml
[arknights]
adb_address = ""              # 模拟器地址，如 "127.0.0.1:5555"
adb_path = ""                 # adb 可执行文件；留空用 MAA 内置的 adb
verbose_diagnostics = false    # true 时入口 data 里带更多结构化字段
max_inline_image_mb = 2       # 内联图片上限（MB），超过则先上传、失败降级落盘
```

两个必须知道的语义：

- **`adb_address` 留空就是不连。** 不会回落到 `5555`——各家模拟器、同一家不同版本的端口
  都不一样，写死一个数字只会让用户"照抄了还是连不上"。此时会明确告诉你"还没告诉我地址，
  所以我根本没去连"（错误码 `adb.address_missing`）。
- **引擎路径不可配置。** 没有 `resource_dir` / `maa_root` 这类键（旧版有，已删除）。
  引擎只有 `data_path("maa")` 一个位置；这个真实路径会出现在 `arknights_status` 与
  `arknights_engine_repair` 返回的 `data.engine_root` 里，手动放置时照着它放。

## 7 个入口

| 入口 ID | 功能 | 超时预算 | 说明 |
|---|---|---|---|
| `arknights_capture` | 截图 | 30 s | 小图内联进聊天窗，大图先上传；`save=true` 另存一份到缓存目录并返回路径 |
| `arknights_status` | 引擎状态 | SDK 默认 | 七态 / 下载进度 / 设备连接 / 最近故障码。**恒成功**——查状态本身不会失败 |
| `arknights_analyze_stage` | 关卡分析 | 60 s | 给了 `stage`（如 `1-7`）就是纯文本分析，不依赖设备；不给则尝试看画面（本轮未接线，会如实说明） |
| `arknights_roguelike_advice` | 肉鸽建议 | 30 s | 集成战略的启发式建议，最多三条。**纯静态知识，不联网、不读设备** |
| `arknights_memory` | 陪玩记忆 | SDK 默认 | 汇总已缓存的干员 / 仓库 / 最近关卡；读失败一律回落 `null`，不报错 |
| `arknights_reconnect` | 重新连接 | 45 s | 按 `adb_address` 重连；地址为空会明确说"没去连"，绝不回落 5555 |
| `arknights_engine_repair` | 修复引擎 | 280 s | 重试 / 强制重下 / 卡住时恢复；`clean=true` 先 `purge()` 清盘再下 |

宿主对单个入口的上限是 300 s，所以最贵的修复入口取 280 s 留余量。
入口既可以在插件详情「入口点」手动触发，也可以由宿主 Agent 按需调用
（`arknights_capture` / `arknights_analyze_stage` / `arknights_roguelike_advice`
同时注册了 `llm_tool`，可由模型直接调用）。

## 引擎七态

`arknights_status` 返回的 `state` 是下面七个值之一：

| 状态 | 含义 | 下一步 |
|---|---|---|
| `absent` | 本地没有可用引擎（首次运行 / 用户清空 / **下载或校验或解压失败后回退到这里**） | 下次启动会自动重试；也可直接点 `arknights_engine_repair` |
| `downloading` | 正在后台下载归档 | 等；`data.download_progress` 是百分比 |
| `verified` | 归档已落盘且 SHA256 匹配 | 自动继续解压 |
| `unpacked` | 解压完成，核心库 + `resource/` 齐备 | 自动继续装载 |
| `loaded` | 动态库加载 + 符号绑定 + `AsstLoadResource` + 实例创建全部成功 | 还没连设备 |
| `ready` | 以上全部 + ADB 已连接 | 可正常使用 |
| `failed` | 需要人工干预的终态（`last_error` 非空）：装载失败，或核心库在多处命中 | 按错误码处置；手动放置后点修复即可恢复 |

关键区别：**下载类失败回 `absent`（可重试、不会把插件困在 `failed`）**，
`failed` 只留给"装载失败 / 布局歧义"这类必须有人动手的情况。

## 常见问题

**Q1：首次启动后一直显示 `downloading`，进度不动。**
检查到 `github.com` 的网络。**插件默认忽略 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量**
（`use_env_proxy` 默认 `False`，构造 opener 时挂空 `ProxyHandler`），只走直连。
**这个开关目前没有暴露成配置键**——也就是说，必须经过代理的网络环境下下载会失败或卡住，
现阶段只能按 Q2 手动放置引擎。

**Q2：我想手动放置引擎（或下载一直失败）。**

1. 调一次 `arknights_status`，看 `data.engine_root` 的真实路径；
2. 从 MAA 官方 Release 下载 `MAA-v6.22.0-win-x64.zip` 并**解压到那个目录**，
   也就是让目录里直接出现 `MaaCore.dll` 和 `resource/`（`MaaCore.dll` 放在
   `engine_root/`、`engine_root/bin/` 或 `engine_root/lib/` 都可以，但**只能有一份**）；
3. 调一次 `arknights_engine_repair`，运行时发现"核心库 + `resource/` 齐备"就会补写标记进 `unpacked`，
   不需要任何"导入路径"入口。

注意资源与引擎**必须同版本**：旧版 `resource/` 配新版核心库会以 `resource.load_rejected`
或 `engine.load_failed` 报错。

**Q3：ADB 连不上。**

1. 先确认 `adb_address` 不是空的——空值表示"没去连"，不是"连不上"；
2. 自己跑一次 `adb connect <host:port>` 看是否通；模拟器重启后端口常常会变；
3. 按错误码区分：`adb.connect_timeout`（连接超时，运行时默认 10 秒）/ `adb.disconnected`
   （曾经连上，掉了）/ `adb.address_missing`（没配地址）；
4. 改用 `adb_path` 指定你自己的 adb（留空则用 MAA 内置的那一份）。

**Q4：`MaaCore.dll` 手动放进去了还是 `failed`。**
大概率是"命中多份"：同一个核心库同时出现在 `engine_root/` 和 `bin/` 里，运行时会判歧义
（`engine.layout_invalid`）而不是随便挑一个。删到只剩一份再点修复。

**Q5：连拍三张截图都失败，只收到一条提示？**
这是刻意的节流：截图失败路径带 `dedupe_key`（`capture:fail:<错误码>`），同一个故障码
在同一个消息窗里只推一次，避免同一句话刷三屏。成功截图**不做**去重——连拍就是要连拍。
另外只有推送真正成功（`submitted=true`）才会记住 key，所以一次失败的推送不会把后续同类
消息永久节流掉。

## 数据说明

- 引擎本体、MAA 日志与调试图片都落在 `data_path("maa")`（MAA 的 `user_dir` 也指向它），
  不写系统临时目录、不写源码目录。
- `arknights_capture(save=true)` 的另存副本写到 `cache_path("maa")`，可随时整体清理。
- 记忆只有三个键值：`operators_cache` / `depot_cache` / `last_stage`，由宿主 `store` 持久化
  （`plugin.toml` 里 `[plugin.store] enabled = true` 已开启）。读写失败一律回落 `None`，
  不影响入口成败。
- 仓库里**不含**任何引擎二进制或 `resource/`（见 `.gitignore`）；`data/` / `cache/` /
  `resource/` / `*.zip` / `*.dll` 全部不入库。

## 已知限制

如实列出，不含"计划中"的乐观表述：

- **OCR 链路未接线。** `arknights_analyze_stage` 在不给 `stage` 时会返回
  `ocr_ok=false` + `ocr_error_code="ocr.not_wired"`，**不会假装识别成功**。
  想分析关卡请显式传 `stage`——这条路径是纯静态知识，不依赖设备。
- **只有 `windows-x64` 走自动安装。** `KNOWN_RELEASES` 里目前只 pin 了这一个平台；
  Linux / macOS 会落到 `engine.platform_unsupported`，只能按 Q2 手动放置
  （语义是"没有自动安装通道"，不是"这机器绝对跑不了"）。
- **宿主 API 尚未在真实宿主里跑过。** `push_message` 返回形状、`Ok`/`Err` 构造、
  `store` 的 `Result` 包装、`config.dump()`、`data_path()` 五项按同仓既有插件用法实现，
  尚未经真机验证；判据是返回里的 `submitted`。
- **进度不主动上报。** 没有接 `report_status`，进度只能通过 `arknights_status` 查询。
- **真实引擎烟测未做。** 下载→校验→解压→装载这条链路用真 zip + 真哈希 + 本地回环 http
  跑通过，但没有在真机 + 真设备上完整跑过一次。
- **静态知识里的数值一律留空**（`sanity` / `drops` / `bonus`），宁可少给也不给错；
  文件里用 `TODO_VERIFY` 标了待核实位置。

## 开发与测试

测试**零第三方依赖**，不需要 pytest，在**仓库根**直接跑：

```bash

python tests/test_contract.py        # 契约测试
python tests/test_contract.py --list # 列出全部用例
python tests/test_service.py         # 业务层文案 / 推送语义测试
```

契约测试共 **92** 条（W26 / A15 / R19 / P28 / X4），覆盖供应链模块的
下载 / 校验 / 解压 / 并发锁 / 取消 / 脱敏等不变量；业务层 **13** 条（S01–S13），
覆盖文案分档、推送判据与去重语义。

> **发布条件**：上面两条命令的退出码都必须为 0。未实现的用例会被记成"未实现"
> 而不是"通过"——收集器刻意不让新增用例静默跳过，所以只要"未实现"非 0 就不算通过。
>
> **静态检查（`ruff`）本轮未执行。** 构建环境没有可用包索引，`pip install ruff` 取不到包；
> 这一项是按"未验证"记账，不是"通过"。发布前需在可联网环境跑一次
> `python -m ruff check .`（在仓库根执行）。

lint：

```bash
python -m ruff check .
```

设计文档以 `docs/01-architecture.md` 为准；
供应链安全方案见 `docs/03-engine-provisioning.md`；用例计划见 `docs/02-test-plan.md`。

## 许可证与致谢

- 本仓库目前**没有** `LICENSE` 文件，即未声明开源许可；在补齐之前请按"保留所有权利"处理。
- 引擎来自 [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)，
  版权归 MAA 项目及其贡献者所有，具体条款以其仓库内的 `LICENSE` 为准。
- 本仓库**不分发** `MaaCore` 二进制或 `resource/`（见 `.gitignore`）：运行时从官方 Release
  下载并按 pin 住的 SHA256 校验，所以你能复核的永远是官方发布的那一份。
- 感谢 N.E.K.O 猫娘计划提供的插件运行时与 Marketplace 工作流。
