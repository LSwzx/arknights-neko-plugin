# 进度快照 · 2026-11-13 22:26

> 状态：**本地可运行的完整骨架已落盘，引擎侧契约测试全绿；业务层测试（S 组）尚未落地。**
> 未提交任何远程仓库（本目录**不是 git 仓库**，也没有 `git init`）。全部改动只在本机磁盘。

## 一、当前基线（实测，不是估计）

```
cd plugin/plugins/arknights_neko_plugin

python tests/test_contract.py          →  通过 92 / 92，失败 0，未实现 0，阻塞 0
python tests/test_contract.py --list   →  共 92 条（W26 / A15 / R19 / P28 / X4）
python tests/test_service.py           →  通过 0 / 13，未实现 13（S01–S13 仍是占位）
```

**供应链模块已冻结**（改动前请先看这条）：
`maa/provision.py` = **1564 行** / 172 条离线断言全绿 / `sha256 db5127ca44b83e21…`（前 16 位）。
三条曾红的用例已闭合：R09（`NO_SPACE.detail` 带真实 `shutil.disk_usage` 缺口，探测失败则退化为无数字）、
R14（`purge()` 连归档 zip 一起删）、R17（`safe_extract` 的 `release` 改为**必填位置参数**，漏传即
`TypeError`；全文件不再有 `AssertionError`）。
快照 `_snapshots/arknights_neko_plugin_20261113-222529.zip` 与磁盘逐字节一致（23 文件 / 360 KB）。

## 二、文件清单

| 文件 | 行数 | 状态 |
|---|---|---|
| `plugin.toml` | 69 | ✅ `entry = plugin.plugins.arknights_neko_plugin:ArknightsNekoCompanion`，`[plugin.store]` + `[plugin.database]` 已开 |
| `__init__.py` | 431 | ✅ 7 个 entry 纯路由 + 2 个 lifecycle 钩子（**未做接线验证**，见 §四） |
| `maa/errors.py` | 737 | ✅ 18 码 + 异常族 + `RESOURCE_CODES`；`__str__` 白名单渲染（K9） |
| `maa/wrapper.py` | 744 | ✅ 纯 ctypes，三条硬契约（父目录 / 原样 PNG / Connected 后不提前 return） |
| `maa/adapter.py` | 813 | ✅ `Queue`/`Future` 桥接、只读白名单 4 项（`Recruit` 排除）、`Resolution` 三态 |
| `maa/provision.py` | 1564 | ✅ `ReleaseSpec` + `KNOWN_RELEASES`(仅 windows-x64) + 原子下载/安全解压 |
| `maa/runtime.py` | 1221 | ✅ 七态状态机、`bootstrap()` 起后台任务立即返回（实测 2ms） |
| `companion/service.py` | 1576 | ✅ 文案表 / `sanitize_maa_text` / `submitted` 判据 / image part 直推 |
| `companion/logic.py` | 253 | ✅ `AutomationRequest` + `execute_automation()` 占位（**只读三类，绝不执行写任务**） |
| `companion/knowledge.py` | 446 | ✅ 静态知识，零 I/O |
| `tests/test_contract.py` | 4733 | ✅ 92 条全绿 + `_mut_a/_mut_r/_mut_p` 三个变异脚本 |
| `tests/test_service.py` | 112 | ⚠️ **S01–S13 仍是占位** |
| `docs/01-architecture.md` | 2403 | ✅ 设计定版（含 K1–K10 契约、裁决表 D1–D7/C1–C10） |
| `docs/02-test-plan.md` | 1065 | ✅ 用例计划与变异矩阵 |
| `docs/03-engine-provisioning.md` | 1380 | ✅ 供应链安全方案（威胁模型 + 附录 C） |

## 三、已验证的关键事实

1. `bootstrap()` 在"缺引擎需下载"的最坏分支上 **2ms 返回 `DOWNLOADING`**，不阻塞启动；`on_shutdown` 的 `cancel_download()` 会 `cancel` + `await` 收尾，`.part` 不残留。
2. 全链 import 通过：`maa.errors` / `wrapper` / `adapter` / `provision` / `runtime`。
3. `str(exc)` 路径隔离实测：10 个异常样本 `LEAK none`；`to_dict()` 仍保留全量路径供日志。
4. `service.py` 静态审计：**不 import** `maa.runtime`/`wrapper`/`provision`（X03）；`get_image_bgr` 0 次；`push` 全程同步调用。
5. 真实下载→校验→解压→装载链路已用**真 zip + 真哈希 + 本地回环 http** 走通（R13 用例）。

## 四、未完成 / 未验证（按优先级）

1. **S01–S13 业务层测试**（13 条）—— `tests/test_service.py` 仍是占位，`service.py` 的 1576 行文案层**目前没有任何自动化断言**。
2. **入口接线验证**——`plugin.sdk` 不在本机，`__init__.py` 从未被 import 过。已搭好 stub SDK 沙箱（`%TEMP%\neko_entry_harness\`），尚未跑通。
3. **U1：`MaaCore.dll` 精确相对路径** —— 仍 `TODO_VERIFY`，代码用"三候选 + 命中多处即判歧义失败"兜底；`ReleaseSpec.library_relpath` 故意留空串（空串 = 未核验，不是"在根目录"）。
4. **51 条文件级变异矩阵** —— 已验 17 条（A9 / R4 / P4），其余待跑。
5. **真实引擎烟测** —— 从未在真实设备 + 真实 MAA 包上跑过（本机拉不动 295 MB）。

## 四之二、业务层交付时明确登记的风险（来自编码侧，未消除）

- **宿主 API 一次都没真跑过**：`push_message` 返回形状、`Ok`/`Err` 构造、`store` 的 `Result` 包装、`config.dump()`、`data_path()` 五项全部按同仓 legacy 插件的用法照抄。判据是 `result["submitted"]`，真宿主若返回带属性的对象会**静默不推送**（进 `transport_error` 分支，不崩）。
- **OCR 链路没接**：`handle_analyze_stage` 在未给 `stage` 时返回 `ocr_ok=False` + `ocr_error_code="ocr.not_wired"`，**故意不假装识别成功**。
- **`report_status` 没接**：进度只走 `arknights_status` 读 `runtime.download_progress`。
- **`auto_start` 从 legacy 的 `false` 改成 `true`**：`service`/`runtime` 都在 `on_startup` 构造，`false` 会让每个 entry 恒回"引擎还没准备好"。这是对 legacy 的**行为改动**，尚未经真实宿主验证。
- **新增配置键 `max_inline_image_mb`**（文档未定义，非法值回落 2 MB）。
- **`knowledge.py` 数值一律留空**（`sanity=None` / `drops=()` / `bonus=""`，只给 2 条 `verified=True`），文件内标了 `# TODO_VERIFY` 位置——宁可少给也不给错。

## 五、恢复方式

```
cd plugin/plugins/arknights_neko_plugin
python tests/test_contract.py            # 应为 92/92
python tests/test_service.py             # 当前 0/13（占位）
```

设计口径以 `docs/01-architecture.md` 为权威；用例编号/总数以 `tests/test_contract.py --list` 为准（当前 **92**，文档里若出现 104/105 是旧数，已改）。
