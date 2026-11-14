# 进度快照 · 2026-11-14 13:40

> 状态：**第二轮收尾完成 —— 两套测试全绿（126 条）、CI 的 ruff 告警清零（38→0）、已推送远程。**
> 仓库：`D:\Copilot_work\neko-plugin`，分支 `main`，远程 `LSwzx/arknights-neko-plugin`。

## 一、当前基线（实测，不是估计）

```
cd <仓库根>

python tests/test_contract.py   →  通过 110 / 110，失败 0，未实现 0，阻塞 0   exit 0
python tests/test_service.py    →  通过 16 / 16，失败 0，未实现 0，阻塞 0   exit 0
                                ────────────────────────────────────────
                                              合计 126 条
```

**供应链模块已冻结**（改动前请先看这条）：
`maa/provision.py` = **1564 行** / 172 条离线断言全绿 / `sha256 db5127ca44b83e21…`（前 16 位）。

## 二、文件清单

| 文件 | 行数 | 状态 |
|---|---|---|
| `plugin.toml` | 69 | ✅ `entry = plugin.plugins.arknights_neko_plugin:ArknightsNekoCompanion`，`[plugin.store]` + `[plugin.database]` 已开 |
| `__init__.py` | 431 | ✅ 7 个 entry 纯路由 + 2 个 lifecycle + 3 个 `@llm_tool` 双注册（stub-SDK 沙箱 **20/20**） |
| `maa/errors.py` | 737 | ✅ 18 码 + 异常族 + `RESOURCE_CODES`；`__str__` 白名单渲染（K9） |
| `maa/wrapper.py` | 744 | ✅ 纯 ctypes，三条硬契约（父目录 / 原样 PNG / Connected 后不提前 return） |
| `maa/adapter.py` | 813 | ✅ `Queue`/`Future` 桥接、只读白名单 4 项（`Recruit` 排除）、`Resolution` 三态 |
| `maa/provision.py` | 1564 | ✅ `ReleaseSpec` + `KNOWN_RELEASES`(仅 windows-x64) + 原子下载/安全解压 |
| `maa/runtime.py` | 1856 | ✅ 七态状态机、`bootstrap()` 起后台任务立即返回、连接幂等/强制重连、销毁走 daemon 线程（Stop→Destroy→Detach） |
| `companion/service.py` | 2196 | ✅ 文案表 / `sanitize_maa_text`（路径折叠已补齐）/ `submitted` 判据 / image part 直推 / 连接配置与 extras 透传 |
| `companion/logic.py` | 253 | ✅ `AutomationRequest` + `execute_automation()` 占位（**只读三类，绝不执行写任务**） |
| `companion/knowledge.py` | 446 | ✅ 静态知识，零 I/O，数值一律留空（`# TODO_VERIFY`） |
| `tests/test_contract.py` | 6535 | ✅ 110 条全绿（W26 / W28–W36 / A15–A18 / R19–R25 / P28 / X4） |
| `tests/test_service.py` | 1947 | ✅ 16 条全绿（S01–S16） |
| `tests/_bootstrap.py` | 52 | ✅ `sys.path` 引导收进 import 期副作用（CI 的 `E402` 无法用 noqa 抑制，见 §七） |
| `tests/_mut_{a,r,p}.py` | — | ✅ 9/9、4/4、4/4 |
| `tests/_mut_matrix.py` | — | ✅ 26 条 / 杀 22（4 条等价变异，已配对双拆证明） |
| `tests/_mut_s.py` | — | ✅ 9 条 / 杀 8（M-S4 等价变异） |
| `docs/01-architecture.md` | 2422 | ✅ 设计定版（K1–K10、裁决表 D1–D7/C1–C10） |
| `docs/02-test-plan.md` | 1237 | ✅ 用例计划 + §4.1 验牙实况 / §4.2 假绿 / **§4.3 死代码假绿** |
| `docs/03-engine-provisioning.md` | 1380 | ✅ 供应链安全方案（威胁模型 + 附录 C） |
| `config.example.toml` | 34 | ✅ 只放 `[arknights]` 业务段，**绝不写 `[plugin]` / `[plugin_runtime]`** |

## 三、本轮闭合的四条缺陷（都有实测证据）

1. **S04 priority 分档**：下载类失败（资产丢失 / 空间不足）回到 §3.5.1 的 4/5 档，不再与"取消"同档。
2. **S06 未知拒绝码归一**：宿主返回非 `backpressure`/`transport_error`/`transport_unavailable` 的
   `reason`，一律归一到 `transport_error`；判据仍只认 `result["submitted"]`。
3. **S07 `dedupe_key`**：连续失败去重；**只在 `submitted=True` 时更新 `_last_push_key`**（否则"没推成功"会被当成"推过了"）。
4. **S08 路径折叠**（云安介入）：`_PATH_HINT_RE` 修掉五个真实缺陷 ——
   * 盘符相对补左边界：`localhost:5555` / `timeout:30` 不再被当成 `C:foo` 折掉；
   * POSIX 允许壳前缀：`path=/sdcard/a.png`、`(/sdcard/a.png)`、`--arg=/data/...` 不再泄漏；
   * 上下文续段：`C:/Program Files/My Company/secret.dll` 整段折掉（且不误伤 `6/22`、`and/or`、`10061`）；
   * 凭据参数去掉 `[?&]` 前缀要求：`token=SECRET` 这种**行首裸查询串**不再漏（原来那条规则因注释尾随反斜杠是**死代码**）；
   * 截断以 `<路径>` 为原子：不再产出 `<路径…` 这种破损标记。

## 四、未完成 / 未验证（按优先级）

1. **U1：`MaaCore.dll` 精确相对路径** —— 仍 `TODO_VERIFY`，代码用"三候选 + 命中多处即判歧义失败"兜底；
   `ReleaseSpec.library_relpath` 故意留空串（空串 = 未核验，**不是**"在根目录"）。
2. **真实引擎烟测** —— 从未在真实设备 + 真实 MAA 包上跑过（本机拉不动 295 MB）。
3. **`ruff` 未执行** —— 本机 `python -m ruff` → `No module named ruff`，`ruff.toml` 只是配置先落盘。
4. **进程内入口接线** 只做了 stub-SDK 沙箱（20/20），**真实 host 一次都没跑过**。
5. **S07 的已知盲点**：单槽 `_last_push_key` 只看"上一次"，`A-B-A` 这种交替序列不会去重（文档已登记，不修）。

## 五、业务层明确登记的风险（未消除）

- **宿主 API 一次都没真跑过**：`push_message` 返回形状、`Ok`/`Err` 构造、`store` 的 `Result` 包装、
  `config.dump()`、`data_path()` 五项全部按同仓 legacy 插件的用法照抄。判据是 `result["submitted"]`，
  真宿主若返回带属性的对象会**静默不推送**（进 `transport_error` 分支，不崩）。
- **OCR 链路没接**：`handle_analyze_stage` 在未给 `stage` 时返回 `ocr_ok=False` +
  `ocr_error_code="ocr.not_wired"`，**故意不假装识别成功**。
- **`report_status` 没接**：进度只走 `arknights_status` 读 `runtime.download_progress`。
- **`auto_start` 从 legacy 的 `false` 改成 `true`**：`service`/`runtime` 都在 `on_startup` 构造，
  `false` 会让每个 entry 恒回"引擎还没准备好"。这是对 legacy 的**行为改动**，尚未经真实宿主验证。
- **新增配置键 `max_inline_image_mb`**（文档未定义，非法值回落 2 MB）。

## 六、恢复方式

```
cd <仓库根>
python tests/test_contract.py            # 110/110
python tests/test_service.py             # 16/16
python tests/_mut_s.py M-S7 /tmp/mutS/plg   # 验牙：副本树上跑，M-S7 必须杀 S08
```

设计口径以 `docs/01-architecture.md` 为权威；用例编号/总数以 `tests/test_contract.py --list` 为准（当前 **110**）。
**总数口径 = 110 + 16 = 126**（文档里若出现 104/105/106 是历史中间值，已改）。

## 七、CI 同参复验（ruff）

CI 的 verify 步骤带 `--ignore-noqa --isolated`（原文见 `ruff.toml` 顶部注释），
所以**本地 `ruff check .` 与 CI 不等价**：它尊重 `# noqa`、且会读 `ruff.toml`，
会漏掉 CI 仍会报的错（2026-11-14 那 38 条里，9 条只在 CI 出现）。

等价复验必须带同一套参数：

```
python -m ruff check --ignore-noqa --isolated --no-respect-gitignore \
    --target-version py311 --line-length 120 \
    --select E4,E7,E9,F,I --exclude vendor .
```

版本也要对齐：CI 锁定 `ruff==0.12.4`，不同版本个别规则会漂。
本机 PyPI 源不通时，可直接取官方二进制（先校验 sha256 再解压，二进制约 27 MB）：

```
https://github.com/astral-sh/ruff/releases/download/0.12.4/ruff-x86_64-pc-windows-msvc.zip
```

判定标准只有一条：**上面这条命令输出 `All checks passed!`**，而不是"本地 `ruff check .` 绿了"。

⚠️ 还有一个只在 CI 暴露的坑：`--isolated` 下 ruff 的 **first-party 判定跟着 cwd 走**。
cwd 在仓库根时 `maa/` 被认成一等公民（与第三方 import 之间要空行），cwd 在仓库外
（CI 的 cwd 是外层 N.E.K.O 仓库、被扫的是 `plugin-repo`）时 `maa` 算第三方（要同块排序）。
两种要求互斥，所以**同一份文件不可能同时满足** —— 2026-11-14 就在这里翻车过一次
（本地 0 条、CI 3 条 `I001`，全在 `tests/_mut_*.py`）。

因此复验时要跑**两个 cwd**，都过才算过：

```
python -m ruff check ... .                      # cwd = 仓库根
cd .. && python -m ruff check ... <仓库绝对路径>  # cwd = 仓库外，等价于 CI
```

结论性做法：`tests/` 下的脚本**不要在顶层 import `maa.*`**（用
`importlib.import_module("maa.x")` 取值），这样 import 块的排序与 cwd 无关。
