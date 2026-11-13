# 02 测试计划与用例清单（负责：云测 / neko-tdd）

> 状态：**计划 + 用例骨架**（未实现全部测试，按主理人要求等骨架确认后开闸）。
> 契约源已定稿：`docs/01-architecture.md`（18 条 `ErrorCode` / 7 态迁移表 / 18 条文案映射表 / 全部签名）。
> **签名以 §1.3 为准**（本轮把架构给的权威签名逐项替换进来，我原先猜的 `MaaWrapper`/`ReleaseSpec`/`EnginePaths`/6 档 `stage` 全部作废）。
> 规模：**105 条用例 / 51 条变异**（W26/W27/A15/S12/S13 五条按主理人改判补齐；X04 按主理人开闸指令新增——防"状态值漂移"，补完即**重新冻结**）。
> 本文档的异常名、`ai_behavior`、`parts` 形状、状态迁移均已与架构对齐；剩 5 项待实测见 §7.1，改判记录见 §7.4。
> 证据分级：`[FACT]` 有源码/实测支撑并附出处；`[INFERENCE]` 由 FACT 推导；`[GUESS]` 未验证推测。
> 目标：把 MAA C API 的契约钉死，让上一版踩过的坑每一条都有**一条会变红的用例**守着。

---

## 0. 结论先行

| 项 | 结论 |
|---|---|
| 用例总数 | **105 条**（W 26 / A 15 / **R 19** / S 13 / P 28 / X 4） |
| **当前进度** | `test_contract.py` **92/92 全绿**（W+A+R+P+X 全实现，exit 0）；`test_service.py` **9/13**（S04/S06/S07/S08 红，落盘后由 `neko-tdd-2` 负责）。验牙脚本：`_mut_a.py`(9/9) / `_mut_r.py`(4/4) / `_mut_p.py`(4/4) / `_mut_matrix.py`(**26 条，杀 22**，4 条为等价变异，见 §4.1) |
| 变异条目 | **51 条**，每条必须至少让 1 条指定用例变红（M46–M51 是新增用例的配对变异，见 §4 注） |
| 可完全脱离真机的层 | `wrapper.py`、`adapter.py`、`runtime.py`、`service.py` 的 **逻辑**全部可测；真机只剩"fake 规格是否与真实 MAA 一致"这一件事 |
| 测试执行方式 | `python tests/test_contract.py`（92 条）+ `python tests/test_service.py`（13 条），零依赖、Python 3.13 标准库即可；有 pytest 时 `python -m pytest tests` 作为第二意见，**两边计数必须相等** |
| 最大的验收风险 | 不是"测试写不完"，而是"**写了但没牙**"——所以第 4 节变异清单是唯一验收标准，第 5 节的收集器一致性校验是防止"静默跳过"的硬闸 |

---

## 1. MAA C API 契约基线（已逐条对照源码核实）

来源：`https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev/` 下
`include/AsstCaller.h`、`src/MaaCore/AsstCaller.cpp`、`src/MaaCore/Assistant.cpp`、
`src/MaaCore/Controller/Controller.cpp`、`src/MaaCore/Controller/AdbController.cpp`。
本轮由云测独立复核（2026-11-13），与主理人给的表格一致，另有 3 处**补充与修正**见 §8。

| # | 契约 | 源码事实 | 出处 | 级别 |
|---|---|---|---|---|
| F1 | `AsstLoadResource` 必须传**父目录** | `auto res_path = os_path / "resource"_p;` 然后 `ResourceLoader::load(res_path)` | `AsstCaller.cpp: AsstLoadResource` | [FACT] |
| F2 | `AsstGetImage` 返回 **PNG** 字节 | `cv::imencode(".png", img, buf); return buf;` | `Assistant.cpp: Assistant::get_image` | [FACT] |
| F3 | 另存在返回原始 BGR 的接口 | `AsstGetImageBgr` → `get_image_bgr()` 直接拷贝像素 | `AsstCaller.h` / `Assistant.cpp` | [FACT] |
| F4 | `NullSize = (AsstSize)-1 = 2**64-1` | `static constexpr AsstSize NullSize = static_cast<AsstSize>(-1);` | `AsstCaller.cpp` 顶部 | [FACT] |
| F5 | `buff_size < data_size` 也返回 `NullSize`；`data_size==0` 返回 **0** | `if (buff_size < data_size) { return NullSize; } ... return data_size;` | `AsstCaller.cpp: AsstGetImage` | [FACT] |
| F6 | 同一个 `NullSize` 同时代表"缓冲不足 / handle 为空 / 未 init"，**不区分** | `if (!inited() \|\| handle == nullptr \|\| buff == nullptr) { return NullSize; }` | `AsstCaller.cpp: AsstGetImage` | [FACT] |
| F7 | `AsstCreateEx` 在**资源未加载**时返回 `nullptr` | `if (!inited()) { return nullptr; }`，`inited()` = `ResourceLoader.loaded()` | `AsstCaller.cpp` | [FACT] |
| F8 | `AsstAsyncConnect` 失败返回 **`InvalidId = 0`** | `if (!inited() \|\| handle == nullptr) { return InvalidId; }` | `AsstCaller.cpp` | [FACT] |
| F9 | `AsstSetConnectionExtras` 返回 **void**，JSON 解析失败**静默 return** | `void AsstSetConnectionExtras(...)`；`if (!jopt) { LogError << ...; return; }` | `AsstCaller.cpp` | [FACT] |
| F10 | `AsstDestroy(nullptr)` 直接返回（安全幂等） | `if (handle == nullptr) { return; }` | `AsstCaller.cpp` | [FACT] |
| F11 | 连接回调 `what` 取值确定 | `ConnectFailed`(why 有 8 种)、`UuidGot`、`ResolutionGot`、**`ResolutionError`**、`Connected`、`Reconnecting`、`Reconnected`、`Disconnect`、`ScreencapFailed`、`FastestWayToScreencap`、`ScreencapCost` | `AdbController.cpp: connect/screencap`、`Controller.cpp: get_image` | [FACT] |
| F12 | 当前 dev 分支的 `AdbController::connect` 里 `Connected` **最后**才发 | 顺序：`UuidGot` → `ResolutionGot`/`ResolutionError` → `Connected` | `AdbController.cpp: connect` | [FACT] |
| F13 | `AsyncCallInfo` 载荷形状 | `{"uuid","what":"Connect","async_call_id":N,"details":{"ret":bool,"cost":N}}`；`ret` 为 **bool** | `Assistant.cpp: call_proc` | [FACT] |
| F14 | `AsyncCallInfo.ret=True` 的语义比"连上"更强 | `AsyncCallItem::Type::Connect` → `ret = ctrl_connect(...)`，而 `Controller::connect` 只有 **截图成功后** 才 return true | `Assistant.cpp` + `Controller.cpp: Controller::connect` | [FACT] |
| F15 | 所有回调载荷都被自动注入顶层 `uuid` | `if (!more_detail.contains("uuid")) { more_detail["uuid"] = m_uuid; }` | `Assistant.cpp: append_callback` | [FACT] |
| F16 | `AsstBool` 是 **`uint8_t`**，不是 C `bool` | `typedef uint8_t AsstBool;` | `AsstCaller.h` | [FACT] |
| F17 | `address` 不含 `:` 时 MAA 直接判失败，**不回落 5555** | `ConnectFailed / why="Address does not contain ':' and no devices found"` | `AdbController.cpp` | [FACT] |
| F18 | `AsstLoadResource` 会**顺带**在 UserDir 为空时把它设成同一个父目录 | `if (asst::UserDir.empty()) { asst::UserDir.set(os_path); }` | `AsstCaller.cpp` | [FACT] |
| F19 | 回调类型 | `typedef void(ASST_CALL* AsstApiCallback)(AsstMsgId msg, const char* details_json, void* custom_arg);`，`AsstMsgId = int32_t` | `AsstCaller.h` | [FACT] |
| F20 | **`ResolutionError` 是真实存在的分支**：分辨率获取失败时发 `ConnectionInfo{what:"ResolutionError", why:"Get resolution failed", details:{width:0, height:0}}` | 由 `ResolutionGot` 的对象改写而来：`info["what"] = "ResolutionError"; info["why"] = "Get resolution failed";`，`details.width/height` 此时为 **0** | `src/MaaCore/Controller/AdbController.cpp :: connect` | [FACT] |
| F21 | `AsyncCallInfo` 自带 `async_call_id`，无需靠谓词猜 | `cb_info` 构造时写入 `async_call_id: call_item.id` | `Assistant.cpp :: call_proc` | [FACT] |
| F22 | `AsstStop` 签名可核，但**不打断**正在执行的 `call_proc` 项 | `AsstBool ASSTAPI AsstStop(AsstHandle)` → `handle->stop()`；只 `m_tasks_list.clear()` + `clear_cache()` | `include/AsstCaller.h` / `Assistant.cpp` | [FACT] |

### 1.1 Release 资产命名与哈希来源（实测，2026-11-13）

`GET https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases/latest` [FACT 实测]

- `tag_name = v6.22.0`，`prerelease = false`，`published_at = 2026-11-09T14:51:41Z`
- **13 个资产，全部带 `digest` 字段且形如 `sha256:<64 hex>`** ——
  也就是说**权威哈希可直接从 GitHub API 取到，不需要我们自己维护哈希文件**。
- 平台矩阵（归一化后共 13 种，无第 14 种）：

| 归一化模式 | 用途 |
|---|---|
| `MAA-{ver}-win-x64.zip` | **Windows x64 主包（本插件目标）** |
| `MAA-{ver}-win-arm64.zip` | Windows ARM64 |
| `MAA-{ver}-linux-x86_64.{tar.gz,AppImage}` | Linux x64 |
| `MAA-{ver}-linux-aarch64.{tar.gz,AppImage}` | Linux ARM64 |
| `MAA-{ver}-macos-universal.dmg` / `MAA-{ver}-macos-runtime-universal.zip` | macOS |
| `MAAComponent-{ver}-android-{arm64,x64}.tar.gz` | Android 组件（非桌面引擎） |
| `MAAComponent-DebugSymbol-{ver}-{win-x64,win-arm64,macos-universal}.zip` | 调试符号 |

**三条会影响 runtime.py 设计的实测结论**：

1. **不存在"精简包"**。当前 release（含近几个 beta）的资产名里没有 `minimal` / `lite` / `light` / `narrow`
   任何一种。[FACT 实测，range: 最近 3 个 release；更早版本未穷尽，标 GUESS]
   → 任务书里"确认精简包平台矩阵"的前提在 v6.22.0 上不成立。
   → 设计后果：`MAA-v6.22.0-win-x64.zip` 是 **294,969,951 字节（约 281 MiB）**。
   要么让用户自己装 MAA 并配 `maa_root`（推荐默认），要么接受下载 281 MiB。**不该假装有轻量包。**
2. **`digest` 只存在于 GitHub API 响应里**，release 资产列表里**没有** `.sha256` / `checksums.txt` 之类文件
   （13 个资产全部核对过）。且未认证 API 限流 60 次/小时。
   → **runtime.py 不应在运行时调 API**。正确做法：把 `sha256` 与 `size` 固定写进仓库内的 manifest
   （pin 版本 + pin 哈希），运行时只做本地校验；维护者升级版本时才去 API 复核一次。
   → R01 因此断言的是"manifest 里的 sha256 与下载内容不符必须删文件并抛 `EngineHashMismatch`"，
     而不是"运行时去问 GitHub"。
3. **降级方案**（拿不到 digest 时，按强度递减）：
   ① 仓库内 pin 哈希（首选）；
   ② 校验 `size` 严格相等 + zip 中央目录 CRC；
   ③ 只做"结构完整性"校验：解压后必须存在 `MaaCore.dll` 与 `resource/config.json`，否则判坏包。
   方案 ③ 挡不住恶意替换，必须在文案里如实说明"本次未校验完整性"。
   → 建议 R04 增加一条子断言：降级校验时必须**显式标记** `integrity="size_only"` 之类的字段，不许静默降级。

> 待核实一项（`[GUESS]`，留给 L5 烟雾）：`MAA-{ver}-win-x64.zip` 解压后是否同时包含
> `MaaCore.dll` 与 `resource/`（本插件 `AsstLoadResource` 需要父目录下的 `resource/`）。
> 若无 `resource/`，`maa_root` 语义与下载后的目录布局都要重新设计。建议在 G4/G5 里加一条
> "解压后断言 `MaaCore.dll` 与 `resource/config.json` 同时存在"的冒烟。

---

### 1.2 本轮契约变更（安全 + 架构两侧，已并入用例）

| 变更 | 内容 | 影响 |
|---|---|---|
| 新增第三个文件 | `maa/provision.py`：`KNOWN_RELEASES` + `verify_sha256()` + `safe_extract()` + `atomic_download()`，**纯标准库、无 asyncio、无宿主依赖** | 新增 **P 组 28 条**（§3.7），不用构造 `MaaRuntime` |
| **`bootstrap` 契约（主理人否掉架构方案后定稿）** | `bootstrap(*, force: bool = False) -> EngineState`——**没有 `allow_download` 参数**。`on_startup` 调用时若磁盘缺引擎：**创建后台 `asyncio.Task` 发起下载，立即返回 `DOWNLOADING`**，绝不在钩子里 await 到下载完成；`force=True` 复用已有 `download_task`（幂等，不并发起第二次）；`on_shutdown` 必须 `cancel()` **并 await** 收尾；`report_status(progress)` 由**后台任务**上报 | R12/R16 期望整体反转（见下）；新增 R18（`DOWNLOADING` 时 capture 快速失败） |
| 异常新增 `kind` 稳定判别字段 | `DownloadFailure`：`NETWORK/THROTTLED/ASSET_GONE/SIZE_MISMATCH/NO_SPACE/REJECTED_SCHEME/WRITE_ERROR/**CANCELLED**`（**8 档**）；**`UnpackFailure`：`BAD_ARCHIVE/UNSAFE_MEMBER/LIMIT_EXCEEDED/SIZE_DRIFT/NO_SPACE/WRITE_ERROR`（6 档，后两档本轮新增，见 §1.2.2）**。签名变为 `EngineDownloadFailed(url, target, kind, status=None, detail="")`、`EngineUnpackFailed(archive, target_dir, kind, member=None, detail="")` | R02/R03/R09/R17 与 S 组的"构造参数 = 诊断契约"断言按新签名写；`疑似攻击`（`UNSAFE_MEMBER`/`LIMIT_EXCEEDED`）与 `包损坏`（`BAD_ARCHIVE`）与 `环境故障`（`NO_SPACE`/`WRITE_ERROR`）**三类必须走三段不同文案**，且 **`service` 必须按 `kind` 再分档，不能只按 `code` 分档** |
| **`wrapper.get_image` 失败一律抛 `MaaCallError`，不返回 `None`**（主理人裁决） | `MaaCallError(api, status, *, detail=None)`，`status ∈ {NULL_SIZE, EMPTY, INVALID_ID, REJECTED}`；`detail` 带 `buffer_size` / `reason`（`grow_limit_reached` / `no_budget_to_grow` / `buffer_budget_exceeded` / `returned_more_than_buffer`）。**旧稿写的"返回 None + 记 last_error"作废** | W05/W06/W08/W09/W10 改成断 `status is NULL_SIZE`；W10 断 `EMPTY`；W11 断 `REJECTED`；W20 断 `detail.reason == "returned_more_than_buffer"`。理由：wrapper 一旦"返回 None + 记 last_error"就变成**有策略**，直接破分层 |
| **`EngineNotReady` 纯关键字构造**（主理人裁决） | `EngineNotReady(state=..., required=(...), progress=None)`；**位置传参必须 `TypeError`**（漏传字段要炸出来） | R16/R18 一律关键字；R16 额外断"位置传参 → `TypeError`" |
| **`DownloadFailure` 8 档 / `UnpackFailure` 6 档**（本轮定版） | `DownloadFailure` 增 `CANCELLED`（**不是失败**：state 落 `ABSENT`、文案"已暂停喵"、`priority=2`、不计失败计数、走 `_render_paused()`）；`UnpackFailure` 增 `NO_SPACE` + `WRITE_ERROR` | P25 断 `kind == "cancelled"`；R09 两变体；S04 断"取消不许出现失败字样"；R16 断"`cancelled` 时 state 落 `ABSENT` 不落 `FAILED`" |
| **文案表必须按 `kind` 二次分档**（主理人本轮裁决） | `ErrorCopy` 增加 `variants: Mapping[str, ErrorCopy]`；`ENGINE_UNPACK_FAILED` 一个 code 下至少三份文案（`BAD_ARCHIVE` / `UNSAFE_MEMBER` / `WRITE_ERROR`），且 `NO_SPACE` 与 `WRITE_ERROR` **不许合档** | **S04 新增子断言**：同一 `code` 的不同 `kind` 产出的 `parts` **必须不相等**；配对变异 **M46** |
| 家族判定改码集合 | `PROVISION_CODES`（download/hash/unpack/layout/load）、`CAPTURE_CODES`（capture×4 + adb×3）；判定写 `exc.code in PROVISION_CODES`，**没有** `EngineProvisionError` 中间基类 | 我的用例**不写** `isinstance(exc, EngineProvisionError)`；改断 `exc.code in PROVISION_CODES` |
| **provision 直接复用 `errors.py` 的异常**（主理人裁决 B） | `maa/provision.py` 顶部 `from .errors import EngineDownloadFailed, EngineHashMismatch, EngineUnpackFailed, EngineLayoutInvalid` + `DownloadFailure` / `UnpackFailure`；**不养自己的异常族**，**没有** `ProvisionError` 中间基类 | R17 改写为"AST 扫 `provision.py` 的所有 `raise`，类型必须 ⊆ 上面四个类"；`runtime` 只做边界捕获 + 状态落地，**不做异常翻译** |
| `wrapper.load()` 签名 | `load(*, dll_directory: Path \| None = None)`；Windows 必须传核心库目录，句柄**保持在实例上** | 新增 **W23–W25**（D 组）：裸名 vs 绝对路径、句柄生命周期、`WinError 193/126` 可分 |
| 自动化范围收窄 | **v1 只支持 Windows x64 自动 Bootstrap**；Linux/mac 一律 `EngineNotReady` + 手动放置（不提供跑不通的路径） | `PlatformUnsupported` 用例按"非 Win/mac/Linux"写，不要写"mac 能自动下载" |
| 下载必须带进度与协作式取消 | `on_progress(downloaded, total)` + `cancel: threading.Event`；清理只有一个 `finally` 出口，覆盖 `BaseException` | P25/P26/P28；R13 的假服务器也要走这条路径 |

#### 1.2.1 异常归属：已由主理人裁决（选 B，provision 直接复用 `errors.py`）

原先三份文档给了三个不同落点（安全 §5 代码在 `provision.py` 内、安全 §8 建议表放 `errors.py` 且继承 `EngineNotReady`、架构说无中间基类）。**现按裁决 B 定稿**：

- `maa/provision.py` **直接** `from .errors import EngineDownloadFailed, EngineHashMismatch, EngineUnpackFailed, EngineLayoutInvalid`（+ `DownloadFailure` / `UnpackFailure` 枚举），**不自建异常族**。
- 采纳理由（主理人给的三条，我认同第二条最关键）：两套词汇表就必须写翻译层，翻译层漏一条 → `service` 拿到陌生异常 → 走兜底文案 → **复现旧实现"所有问题都变成 ADB 没连上"**。少一层翻译就少一整类故障。
- `maa/errors.py` 本身零依赖（只 `enum`/`pathlib`/`typing`），所以"provision 纯标准库可脱离宿主单测"这个目标**仍然成立**，不成环。
- **`R17` 随之改写**：不测"翻译保字段"，改成一条更便宜也更硬的静态不变量——
  `ast` 扫 `provision.py` 的全部 `Raise` 节点，异常类型必须 **⊆ 上述四个类**。永久防"图省事自定义一个新异常"。
- **删除**本文档任何"runtime 负责把异常翻译成 18 条之一"的表述：`runtime` 只做**边界捕获 + 状态落地**（`state=FAILED` + `last_error`），不改异常类型。

**由此解锁的两条**（原先被这个问题卡住）：
1. `test_contract.py` 顶部 import 清单已确定（见 §5.5）。
2. "service 只允许 import `maa/errors.py`"这条硬约束**可以写成断言** → 新增 **X03**（`ast` 扫 `companion/service.py` 的 import，禁止 `maa.wrapper` / `maa.provision` / `maa.runtime`；允许 `maa.errors` 与 `maa.adapter`）。

#### 1.2.2 缺口已闭合：`UnpackFailure` 加 **`NO_SPACE` + `WRITE_ERROR`** 两档（主理人裁决）

**原状**：`DownloadFailure` 有 7 档（含 `NO_SPACE`/`WRITE_ERROR`），但 `UnpackFailure` 只有 4 档——
`BAD_ARCHIVE` / `UNSAFE_MEMBER` / `LIMIT_EXCEEDED` / `SIZE_DRIFT`。
解压阶段磁盘写满（`OSError(ENOSPC)`）或目标目录无写权限（`PermissionError`），这四档**一个都不对**：
说成 `BAD_ARCHIVE` 是撒谎（包是好的），说成 `LIMIT_EXCEEDED` 是把环境故障当攻击。
后果是本项目反复要根除的那种"假事实"——**把用户推去重下一个 295 MB 的包，而真实原因是磁盘满**。

**裁决（主理人）**：采纳 (a)，且**比我提的更细**——不合成一档，而是拆两档，因为文案不同：

| 新档 | 触发 | 文案要点 | 用户该做什么 |
|---|---|---|---|
| `UnpackFailure.NO_SPACE` | `OSError` 且 `errno == ENOSPC` | "磁盘空间不够喵（还需要约 X MB）" —— **必须带具体缺口数字** | 清盘 |
| `UnpackFailure.WRITE_ERROR` | 其它写盘失败（`PermissionError` / `EACCES` / `EROFS`） | "写不进去喵（目录可能没有写权限）" | 改权限 / 换目录 |

**为什么必须拆两档**（主理人的原话，直接引用）：两者文案不同，合档的话只能说"写文件失败"，
用户还是不知道该清盘还是改权限。→ 这条同时上升为 `service` 的分档断言（§3.4 S04）。

**落地改动**：
- `UnpackFailure` 变 **6 档**；R17 的白名单集合不变（仍是那四个异常**类**，`kind` 不是类）。
- **R09 的 `kind` 断言不再留 `TODO`**：拆成两个变体（ENOSPC → `NO_SPACE`；`PermissionError` → `WRITE_ERROR`），
  都断"可被上层接住 + 无残留 + `detail` 非空 + `kind` 精确相等"。
- `NO_SPACE` 的"约需 X MB"必须来自**真实可用空间探测**（`shutil.disk_usage`）而非猜；
  探测本身失败时退化为不带数字的文案，**不许编一个数字**（S08 的同类要求）。

---


### 1.3 签名定稿（架构权威，本轮对齐后**测试按此构造 fake**）

> 这一节是对"我猜的签名"的**逐项替换**。凡与本节冲突的写法一律以本节为准；
> 我原先猜的 `MaaWrapper` / `ReleaseSpec` / `EnginePaths` / 6 档 `stage` 字符串 **全部作废**。

**wrapper 是 C 函数 1:1 镜像**——所以 `bootstrap()` 不在 wrapper（在 runtime），
`screencap()` / `connect()` 不在 wrapper（在 adapter）。这条决定了 W 组怎么切。

| 模块 | 权威签名（只列测试会用到的） |
|---|---|
| `maa/wrapper.py` | `class MaaCoreWrapper(*, library_path, max_image_buffer_bytes, max_buffer_grow, preferred_loader=None, dll_loader: Callable[[str], Any] \| None = None)`；`platform()` / `default_library_names()` / `candidate_library_paths(root)` 是 `@staticmethod`；`load(*, dll_directory=None)`、`close()`、`version`（property）、`set_callback(handler)`、`create_instance()`（返回 0 = 失败）、`destroy_instance(h)`、`set_user_dir(p)`、`load_resource(parent_dir)`、`async_connect(h, *, adb_path, address, config)`、`async_screencap(h)`、`get_image(h, *, buffer_size=None)`、`get_image_bgr(h, *, buffer_size=None)`、`is_connected(h)`、`null_size()`、`stop_tasks(h)`。**全部同步**；`AsstHandle/AsyncCallId/AsstSize` 都是 `int` 别名 |
| `maa/wrapper.py` 私有异常 | `MaaCallError(api, status, detail=None)`，`status ∈ {NULL_SIZE, EMPTY, INVALID_ID, REJECTED}`。**不许越过 adapter** → W 组可以断它，S 组不该见到它（§2.3 矩阵已写明 adapter 负责翻译） |
| `maa/adapter.py` | `ResolutionStatus = KNOWN / FAILED / UNKNOWN`；`Resolution(status, width=None, height=None, reason="")`，`__post_init__` 断言 **KNOWN ⇒ w,h 正整数；否则 w is h is None**（**禁止默认值**）；`DeviceProfile(connected, uuid, resolution, reason="", call_id=0)`；`async connect(*, address, adb_path, config="General") -> DeviceProfile`；`async wait_resolution(*, timeout) -> Resolution`（超时返回 `UNKNOWN`，**不抛错**）；`resolution() -> Resolution`（同步快照） |
| `maa/adapter.py` 常量 | `READONLY_TASK_TYPES = frozenset({"Screencap","OperBox","Depot","Debug"})`（**4 项，不含 `Recruit`**）、`ALLOWED_DEBUG_MODES = frozenset({"ocr"})`。**定义处是 adapter**（最贴近 `AsstAppendTask` 调用点，也不与 runtime 成环）；runtime 再 import 一次做第二层 |
| `maa/runtime.py` | `async bootstrap(*, force: bool = False) -> EngineState`（只起后台任务，立即返回 `DOWNLOADING`）；`download_task`（property）、`download_progress`（property，0–100，不在下载时 `None`）、`async cancel_download()`（cancel **并 await** 收尾）、`async ensure_ready()`、`_run_provision()`（私有后台本体） |
| `maa/runtime.py` 注入 | ctor 收 `engine_root / user_dir / data_dir / cache_dir` 四个 `Path`（由 `__init__.py` 展开 `data_path("maa")` 后传入，**不传插件实例**）→ R07 可直接断言"产物全在 data_path 之下"；`ProvisionPorts(fetcher=None, extractor=None)` **构造期注入**（不是方法参数，因为后台任务拿不到方法参数） |
| `maa/provision.py` | **`ReleaseSpec`**（**只有一个**版本数据类，见架构 §7.3.1 D5：早先我按主理人口径写的 `EngineRelease` 已作废、架构已从文档删除）：`version / url / sha256 / archive_name / size_bytes / platform / library_relpath / resource_parent_relpath`；`MaaRuntime.__init__(release: ReleaseSpec | None)` **直接吃 `KNOWN_RELEASES[platform]` 的值，没有转换函数**；`KNOWN_RELEASES` 与本类型**同一文件**（常量表与类型不许分家）；`safe_extract(...) -> ExtractResult(dest, core_lib, selected, skipped, excluded, skipped_sample)`——**`core_lib` 由 provision 定位后回传，测试不再自己拼路径**；放行规则 = `resource/**` + 归档根目录下文件 + `bin/**`/`lib/**`，顺序固定 **路径校验 → 硬排除 → 放行**（§3.7 P 组按 v1.2 口径） |
| `maa/runtime.py` | **`RuntimeLayout`**（原猜名 `EnginePaths` 已废弃）：`engine_root / library_path / resource_parent / resource_dir / user_dir / data_dir / cache_dir` + `missing_pieces()` |
| `maa/runtime.py` 状态机 | 迁移表**新增两行**：`DOWNLOADING → DOWNLOADING`（进度自环）、`DOWNLOADING → ABSENT`（取消/失败清半包）。**没有 `DOWNLOADING → FAILED`**——失败也回 `ABSENT`，原因记 `last_error` |
| `companion/service.py` | `ErrorCopy(text, hint, visibility=("chat",), ai_behavior="read", priority=3, variants: Mapping[str, ErrorCopy] = {})`；`_COPY: Final[Mapping[ErrorCode, ErrorCopy]]`；**导入期不变量** `assert set(_COPY) == set(ErrorCode)`。**不建 `EngineFault`**（理由：`ErrorCode` 已是稳定契约，再造一个枚举就要写 18 行映射函数，把一条不变量变成两处） |
| `companion/knowledge.py` | `KnowledgeBase.analyze_text(text: str, *, stage: str = "", concern: str = "") -> StageAdvice`；`StageAdvice(stage, ocr_ok: bool, highlights: tuple[str,...], concern_reply: str)`。**`ocr_error` 不在返回值里**——它是 `StageOutcome.ocr_error_code: str \| None`（异常码，不是文本）；knowledge 保持"纯静态、零 I/O、零异常感知" |
| `push_message`（宿主 SDK） | **同步调用、不要 `await`**；返回 **dict**；权威判据 `result["submitted"]`，被拒时另有 `result["ok"] is False` 与稳定 `reason`（`backpressure`/`transport_error`/`transport_unavailable`）；图片 part = `{"type":"image","data":<bytes>,"mime":"image/png"}`；`ctx.images.upload()` **只在 entry/timer/message/custom event 里可用，生命周期钩子里不可用** |
| `MaaRuntime.last_error` | **派生只读快照**（由状态机 + 最近一次异常算出），**没有版本号/`_error_seq`** → A11 改断"迟到 id 不在 `_pending` 里 ⇒ `stats.events_dropped_late += 1` 且 `last_error` 不变" |

**异常名对齐（我的猜测 → 权威）**：`EngineLibraryError`→`EngineLoadFailed`；
`EngineResourceLoadError`→**拆两个** `ResourceMissing` / `ResourceLoadRejected`（"路径没东西"vs"路径对但引擎说不"，**必须分开测**）；
`EngineInstanceError`→`InstanceCreateFailed`；`EngineImageUnavailable`→**拆三个** `CaptureEmpty` / `CaptureFailed` / `CaptureFormatUnexpected`；
`EngineArchiveError`→`EngineUnpackFailed` + `EngineLayoutInvalid`；
**`EngineVersionMismatch` 与 `EngineIOError` 这两个类不存在**——版本不符表现为 `EngineLoadFailed(symbol=...)` 或 `ResourceLoadRejected`，
IO 故障用 `EngineDownloadFailed(kind=WRITE_ERROR)`。18 条全集以 `01-architecture.md` §2.2 表为唯一权威（含 `PlatformUnsupported`）。

**旧 6 档 `stage` → 新码 + 状态**（文案分派时用；`stage` 字符串已从设计中移除）：

| 旧 stage | 新 `code` | 新 `EngineState` |
|---|---|---|
| `library` | `engine.load_failed` | `FAILED` |
| `resource` | `resource.missing` / `resource.load_rejected` | `FAILED` |
| `instance` | `instance.create_failed` | `FAILED` |
| `connect` | `adb.disconnected` / `adb.connect_timeout` | `LOADED`（**不是 FAILED**） |
| `no_address` | `adb.address_missing` | `LOADED` |
| `ready` | （无异常） | `READY` |

---

## 2. 测试分层策略

### 2.1 五层，以及"不依赖真机"的边界

| 层 | 名字 | 覆盖模块 | 依赖 | 是否门禁 |
|---|---|---|---|---|
| L0 | 纯函数/静态断言 | `wrapper.py` 的类型声明、`knowledge.py` 规则 | 无 | 是 |
| L1 | fake lib 契约 | `wrapper.py` 全部逻辑 | `FakeMaaCore`（纯 Python + 真 ctypes 缓冲） | 是 |
| L1.5 | **纯函数安全契约** | `provision.py`（`verify_sha256`/`safe_extract`/`atomic_download`/`KNOWN_RELEASES`） | 纯标准库 + 伪造 opener / `file://` / 本地 `http.server`；**零真网络** | 是（P 组 28 条） |
| L2 | 假回调线程 + 假网络 | `adapter.py`、`runtime.py` | `FakeMaaCore.emit()` 起真 `threading.Thread`；注入式 fetcher | 是 |
| L3 | 假宿主 SDK | `service.py`、`logic.py` | stub `push_message` 记录调用形状 | 是 |
| L4 | 沙盒真跑（官方 `user_plugin_server.py`） | 插件整体能否在宿主进程里起来 | 已核实可用（§6.4） | 是（仅 runtime 层三条路径） |
| L5 | 真机（真模拟器 + 真 MaaCore.dll） | **只验 fake 的规格与现实是否一致** | 真 ADB + 真资源 | **否**（非门禁） |

**边界定义（一句话）**：L1 之前完全不需要真机、不需要网络、不需要 MaaCore.dll；
L5 唯一的职责是校准 L1 的 fake，而不是验 `wrapper.py` 的逻辑。理由见 2.3。

### 2.2 fake 必须实现的符号表（逐个给签名）

C 原型逐字来自 `include/AsstCaller.h` [FACT]；"fake 语义"列是**照抄** `AsstCaller.cpp` 的行为，
fake 的目标是当"规格的可执行副本"，不是当"能跑就行"的桩。

| 符号 | C 原型（引用自 `AsstCaller.h`） | fake 的 Python 签名 | fake 必须复刻的行为 |
|---|---|---|---|
| `AsstSetUserDir` | `AsstBool AsstSetUserDir(const char* path)` | `set_user_dir(path: bytes) -> int` | 记录调用顺序与 path；可被设为返回 0（模拟不可写） |
| `AsstLoadResource` | `AsstBool AsstLoadResource(const char* path)` | `load_resource(path: bytes) -> int` | **断言型 fake**：路径下必须存在 `path/resource/` 才返回 1，否则 0（复刻 F1）。同时记录 `user_dir_was_empty` 以复刻 F18 |
| `AsstCreate` | `AsstHandle AsstCreate(void)` | `create() -> int` | `resource_loaded == False` 时返回 0（复刻 F7） |
| `AsstCreateEx` | `AsstHandle AsstCreateEx(AsstApiCallback cb, void* custom_arg)` | `create_ex(cb: int, custom_arg: int) -> int` | 同上；保存 `cb` 供 `emit()` 使用；`resource_loaded == False` 返回 0 |
| `AsstDestroy` | `void AsstDestroy(AsstHandle h)` | `destroy(h: int) -> None` | `h in (0, None)` 直接 return（复刻 F10）；否则把句柄标记为已死，之后任何用它的调用按 `nullptr` 处理 |
| `AsstAsyncConnect` | `AsstAsyncCallId AsstAsyncConnect(AsstHandle h, const char* adb_path, const char* address, const char* config, AsstBool block)` | `async_connect(h: int, adb: bytes, address: bytes, config: bytes, block: int) -> int` | 句柄空/未 init → 返回 0（复刻 F8）；否则返回自增 id ≥ 1，并按脚本化事件序列在**独立线程**里 `emit()` |
| `AsstSetConnectionExtras` | `void AsstSetConnectionExtras(const char* name, const char* extras)` | `set_connection_extras(name: bytes, extras: bytes) -> None` | 必须**永不抛异常**；`extras` 非法 JSON 时静默（复刻 F9），并记录被丢弃 |
| `AsstAsyncScreencap` | `AsstAsyncCallId AsstAsyncScreencap(AsstHandle h, AsstBool block)` | `async_screencap(h: int, block: int) -> int` | 句柄空 → 0；可配置为"阻塞 N 秒后返回"或"永不返回"（用于超时/取消用例） |
| `AsstGetImage` | `AsstSize AsstGetImage(AsstHandle h, void* buff, AsstSize buff_size)` | `get_image(h: int, buff, buff_size: int) -> int` | 逐行复刻 F5/F6：`h` 假 → `NULL_SIZE`；`buff is None` → `NULL_SIZE`；`buff_size < len(payload)` → `NULL_SIZE`；否则 `ctypes.memmove(buff, payload, len(payload))` 后返回 `len(payload)`；`payload == b""` → 返回 0 |
| `AsstGetNullSize` | `AsstSize AsstGetNullSize(void)` | `get_null_size() -> int` | 默认返回 `2**64-1`；可配置成别的值（用于 W05"哨兵落在成功区间"的反例）；可整体删掉该符号（用于 W13） |
| `AsstGetImageBgr` | `AsstSize AsstGetImageBgr(AsstHandle h, void* buff, AsstSize buff_size)` | `get_image_bgr(h: int, buff, buff_size: int) -> int` | 返回**裸 BGR 像素**（长度 = w*h*3，**无宽高信息**）。fake 必须让它与 `get_image` 的 PNG 字节**明显不同**，否则"只有 PNG 过魔数校验"这条对照测试测不出东西。`[FACT]` 它保留的唯一用途就是这条对照 + 本地调试 |
| `AsstGetVersion` | `const char* ASST_CALL AsstGetVersion(void)` | `get_version() -> bytes` | 返回 `b"v6.0.0"` |
| `AsstAppendTask` | `AsstTaskId AsstAppendTask(AsstHandle h, const char* type, const char* params)` | `append_task(h: int, type: bytes, params: bytes) -> int` | 句柄空 → 0；记录 `type`/`params` 供只读白名单断言 |
| `AsstStart` | `AsstBool AsstStart(AsstHandle h)` | `start(h: int) -> int` | 句柄空 → 0；正常 → 1 |
| `AsstConnected` | `AsstBool ASSTAPI AsstConnected(AsstHandle h)` | `connected(h: int) -> int` | 句柄空 → 0；否则返回脚本化值（可设 `0x80` 测 W22）。这是 `wrapper.is_connected()` 的唯一来源（架构用它做截图熔断的权威探针） |
| `AsstStop` | `AsstBool ASSTAPI AsstStop(AsstHandle h)` | `stop(h: int) -> int` | 句柄空 → 0；**可配置为"什么都不做"**——因为 `[FACT]` F22 它不打断 `call_proc` 里正在跑的项，fake 必须复刻这个"假解除"行为，否则会写出假通过的用例 |
| `AsstSetStaticOption` | `AsstBool ASSTAPI AsstSetStaticOption(AsstStaticOptionKey key, const char* value)` | `set_static_option(key: int, value: bytes) -> int` | 未知 key → 0（复刻源码 `default: return false`）；用于 CPU/GPU OCR 选项，测试只需断言"未知 key 不被当成功" |

fake 的**载体**（关键实现细节，决定"能不能真的测出 `c_byte` 那个 bug"）：

```python
class _FakeFunc:
    """模拟 CDLL 里的函数对象：可写 argtypes/restype，调用透传到 Python 实现。

    wrapper 会执行 lib.AsstGetImage.restype = ctypes.c_uint64，所以 fake 的函数
    必须允许属性写入（ctypes 的 CFUNCTYPE 实例不允许），同时把写入的类型记下来
    供 W14 的"声明对照"用例断言。
    """
    def __init__(self, impl):
        self._impl = impl
        self.argtypes = None
        self.restype = None
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args)
```

`buff` 参数**不能**被 fake 吞掉：`wrapper` 传进来的必须是真的
`(ctypes.c_ubyte * n)()` 数组，fake 用 `ctypes.memmove(buff, payload, len(payload))`
把字节写进去。这样 `bytes(buf[:got])` 这一步就是**真实**的读回路径 ——
若 `wrapper` 写成 `c_byte`，`bytes()` 会在 ≥0x80 的字节上抛 `ValueError`，
W04 就会红。[FACT] 这不需要 C 编译器，也不需要真 `MaaCore.dll`。

### 2.3 为什么 `wrapper.py` 可以完全用 fake lib 测

1. **职责单一**：`wrapper.py` 只做两件事——(a) 把 Python 值按 `AsstCaller.h` 的类型编解码，
   (b) 把 C 侧的哨兵值翻译成明确的 Python 语义（成功/缓冲不足/未初始化）。
   两件事都**不涉及**设备、网络、时序，故不含必须真机的成分。
2. **规格可复刻**：§1 的 F1–F19 都是逐行的源码事实，fake 可以做成"规格的可执行副本"。
   假库里 `buff_size < len(payload) → NULL_SIZE` 这一行，和 C 源里那一行是同一个意思。
3. **真机测不到最危险的分支**：缓冲不足、`NULL_SIZE` 被当成成功、句柄失效、库越界返回——
   这些在真机上要么极难触发，要么触发即静默出错。**只有 fake 能强制走到这些分支。**
   "真机跑通"对这几条路径的证明力约等于 0。
4. **契约漂移有独立守卫**：单靠 fake 有"两边一起改错就都绿"的风险，所以另设 W14
   （argtypes/restype 声明对照）与 W12（`AsstGetNullSize` 与本地常量一致）两条用例，
   再加 L5 真机烟雾做最终校准。

**L5 的边界（明确不做什么）**：真机只跑三条——
① 真实 `AsstLoadResource(父目录)` 返回 1；② 真实 `AsstGetImage` 返回的首 8 字节等于 PNG 签名；
③ 真实 `AsstGetNullSize() == 2**64-1`。三条都通过，即认为 fake 的规格可信，之后不再为逻辑回归跑真机。

---

## 3. 用例清单

约定：函数名 = `test_<编号小写>_<slug>`，编号与函数名的对应关系**由收集器自动校验**（§5.3），
文档表格与代码登记表也由 X01 交叉校验，因此三者不可能漂移。

### 3.1 W 组：`tests/test_contract.py` — wrapper 的 C API 契约（fake lib，无真机）

| 编号 | 所属模块 | 前置桩 | 断言要点 | 对应的真实故障 |
|---|---|---|---|---|
| W01 | wrapper | `FakeMaaCore`，`resource/` 在 `root/resource` | `load_resource()` 收到的 bytes 解码后 `== str(root)`，且 `root/"resource"` 是目录；返回值不抛 | 上一版传 `resource` 本体，MAA 拼出 `resource/resource`，加载必失败（F1） |
| W02 | wrapper | `resource_dir = root/"resource"` | **wrapper 是 1:1 镜像，不许静默改写入参**：传进去什么、C 就收到什么，**不得自己拼/剥 `resource`**（`resource/resource` 的根因是调用方传错，不是 wrapper 该替它修）。"调用方必须给父目录"由 `RuntimeLayout.resource_parent` 保证 → 归 R15 | 上一版传 `resource` 本体，MAA 再拼一次 → 加载必失败（F1）。**分层修正**：原写法让 wrapper 抛 `ResourceMissing`，与 §2.3 矩阵（wrapper 不感知）冲突，已按矩阵改 |
| W03 | wrapper | fake `payload = bytes(range(256))*4` | 返回值 `== payload`（逐字节相等，含长度） | 曾把 PNG 当 RGB 二次编码，图全坏（F2） |
| W04 | wrapper | fake `payload` 为真 PNG 头 `b"\x89PNG\r\n\x1a\n" + IHDR...` | 不抛 `ValueError`；返回字节含 `0x89`、`0x50...`；`bytes` 里不存在负值 | 曾用 `c_byte`，`bytes([-119,...])` 直接 `ValueError` |
| W05 | wrapper | `null_size = 12345`，`buff_size` 默认 1 MiB，fake 返回 12345 | 抛 `MaaCallError(status=NULL_SIZE)`，`detail` 含 `buffer_size`；**绝不**返回 12345 个零字节 | 哨兵落在 `(0, buffer_size]` 时被当成功，交付一坨零字节（F4/F6） |
| W06 | wrapper | fake `payload` 长 200，显式 `buffer_size=100`，`max_buffer_grow=0`（不许再增长） | 抛 `MaaCallError(status=NULL_SIZE)`，且 `detail.reason ∈ {grow_limit_reached, no_budget_to_grow, buffer_budget_exceeded}`——**必须能分出「缓冲不足可重试」而不是「实例/句柄坏了」** | 曾把 `buff_size < data_size` 的 `NULL_SIZE` 当成功（F5）；也曾在文案里说成「实例失效」 |
| W07 | wrapper | 依次：第 1 次不足（payload 长 > buff）、第 2 次够 | 最终拿到完整 payload；fake `get_image` 调用 2 次，第 2 次 `buff_size > 第 1 次` | MAA 不返回"需要多大"，只能倍增；曾完全不重试 |
| W08 | wrapper | fake 永远返回 `NULL_SIZE` | 抛 `MaaCallError(NULL_SIZE)`；`AsstGetImage` 调用次数 **== `1 + max_buffer_grow`**（默认 grow=2 → 3 次，不多不少） | 无上限重试 → 内存与时间双爆。**名变更**：旧稿的 `IMAGE_RETRY_MAX` 常量已不存在，现在是构造参数 `max_buffer_grow` |
| W09 | wrapper | 每次返回 `NULL_SIZE`，payload 比 `max_image_buffer_bytes` 还大 | 抛 `MaaCallError(NULL_SIZE)` 且 `detail.reason` 可分类；记录到的**最大** `buff_size <= max_image_buffer_bytes`，不再继续翻倍 | 一个谎报分辨率的端点能把进程 OOM |
| W10 | wrapper | fake `payload = b""` → 返回 0 | 抛 `MaaCallError(status=EMPTY)`（`detail` 含 `buffer_size`）；**不得**当「成功但 0 字节」 | `data_size==0 返回 0` 被当成「成功但 0 字节」（F5） |
| W11 | wrapper | `create_instance()` 后 fake `AsstCreateEx` 返回 0，随后调 `get_image` | 抛 `MaaCallError(status=REJECTED)`（**wrapper 私有异常，只在 W 组可见**，adapter 负责翻译、S 组不该见到）；**文案里不出现「缓冲区」**；fake `get_image` **调用计数为 0**（不发 C 调用） | `NULL_SIZE` 三义混用，把「句柄空」报成「缓冲不足」，用户去改分辨率（F6） |
| W12 | wrapper | fake `get_null_size()` 返回 `2**64-1` | `wrapper` 用的哨兵常量 `== 2**64-1` 且 `== fake.get_null_size()`；`NULL_SIZE != -1` 也要断言 | 曾拿 `-1` 去比较 `c_uint64` 的返回值，永不相等 |
| W13 | wrapper | fake **不提供** `AsstGetNullSize` 属性 | 不抛 `AttributeError`；回退到本地常量 `2**64-1`，W05 语义仍成立 | 老版本 MaaCore 无此符号会让整个引擎不可用 |
| W14 | wrapper | fake 记录 `argtypes/restype` 写入 | `AsstGetImage.restype is ctypes.c_uint64`；`argtypes == (c_void_p, c_void_p, c_uint64)`；`AsstAsyncConnect` 的 block 参数用 `c_ubyte`（`AsstBool=uint8_t`，F16）；`AsstBool` 系列 restype 不是 `c_int` | 类型写错 → 64 位返回值被截断成 32 位；哨兵判定全错 |
| W15 | wrapper | `load()` + `create_instance()` 路径下 fake `AsstCreateEx` 返回 0 | wrapper **不隐藏**失败：返回裸 `0`/`None`（或抛 `MaaCallError`/`InstanceCreateFailed`），且**不误调 `destroy_instance`**。"文案指向 resource 未加载"那一半归 runtime（R 组） | 空句柄真实根因是资源没加载，旧文案把人推去重装宿主（F7）。**分层修正**：§2.3 明确 wrapper 返回裸句柄、runtime 抛 `InstanceCreateFailed` |
| W16 | wrapper | `async_connect` 返回 0 | 视为"调用被拒"；不进入等待；文案含 adb 路径与 address | `InvalidId=0` 被当成合法 call id（F8） |
| W17 | wrapper | `set_connection_extras(name, b"{not json")` | 不抛异常；不被当作成功信号；记录一条 debug 日志（或可观测字段） | C 侧 void + 静默 return，装成功后用户以为配置生效（F9） |
| W18 | wrapper | `destroy(0)`、`destroy(None)` 各调一次 | 都不抛；内部状态一致 | 退出路径上抛错会让 `on_shutdown` 卡死 |
| W19 | wrapper | fake `payload` 长度**正好等于** `buff_size` | 判定为**成功**并返回全部字节 | 写成 `got >= buff_size` 即 off-by-one，塞满时误报失败 |
| W20 | wrapper | fake 返回 `buff_size + 1`（库违规越界） | 抛 `MaaCallError(NULL_SIZE)` 且 `detail.reason == "returned_more_than_buffer"`；**不得**把整个 buf 读出来当图像 | 自定义/未来版本库越界 → 读到未初始化内存并当图像交付 |
| W22 | wrapper | fake 的 `AsstStart`/`AsstConnected` 返回 `0x80`、`0xFF`（`AsstBool` 是 `uint8_t`，F16） | 不得抛 `ValueError`；按"非 0 即真"映射（或按声明拒绝，但必须抛**自己的** `MaaCallError` 而非 `ValueError`）；`argtypes` 用 `c_ubyte` 而非 `c_bool` | `c_bool` 读 `uint8_t` 的 0x80 会 `ValueError`；`c_byte` 会读成负数 → 布尔判断反转 |
| W23 | wrapper | 用一个**系统自带**的 `kernel32.dll` 代替 MaaCore 做机制级回归（不绑 MAA 符号） | 裸名 `CDLL("MaaCore.dll")` 必须**失败**、绝对路径必须**成功**；固化为回归测试 | CPython 3.8+ 的 `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR` 行为（安全 §3.7 D4）：裸名加载不搜 DLL 所在目录，靠"凑巧 PATH 里有"是定时炸弹 |
| W24 | wrapper | 给一个**可 dlopen 但不含 MAA 符号**的库 | 成功路径：`close()` 之前 `_dll_dir_handle` 非空、之后为空；失败路径（缺符号）**句柄也必须被释放**（不泄漏）；`close()` 幂等。缺符号的错误类型**两条路都接受**：wrapper 抛裸 `AttributeError`（附符号名）或 runtime 包成 `EngineLoadFailed(symbol=...)` | 把 `add_dll_directory` 写成 `with` 块 → 句柄提前释放，后续延迟加载失败（安全 D5） |
| W25 | wrapper | ①库里真的没有该文件；②（需样本）32 位 DLL 放 64 位进程；③（需样本）把同目录 opencv/onnx 改名 | ① **必须有可诊断的错误**：裸 `OSError`（含 `winerror`/路径）或 runtime 包装的 `EngineLoadFailed(oserror≠None, symbol=None)` 两者皆可，但**不许吞**；② → `WinError 193`；③ → `WinError 126`，且文案能区分"缺同目录依赖"与"缺系统运行库" | ②③ 需要真实样本，标 **L5 非门禁**；① 必须离线可测 |
| W26 | adapter（第一层白名单） | 表驱动写任务类型：`Fight`、`Infrast`、**`Recruit`**、`Mall`、`Award`、`Roguelike`、`Copilot`；每次 `await adapter.append_readonly_task(type)` | 全部抛 `TaskNotAllowed(task_type=..., allowlist=READONLY_TASK_TYPES)`；**且 fake lib 的调用计数器为 0**（`AsstAppendTask`/`AsstStart` 都必须是 0）——**"在调用任何 ctypes 之前就被拒"比"抛了异常"重要得多**（防"先点进去再拒绝"）；`READONLY_TASK_TYPES` == **`{Screencap, OperBox, Depot, Debug}`（4 项）** | **这是全项目唯一一处"错了会越过安全边界"的地方**。⚠️ **`Recruit` 归属有冲突**：架构 §3.3 的常量含它，adapter 与主理人指令按"旧实现注释逐字写着严禁 Recruit、参数可触发游戏内确认动作"把它排除 → 我按**拒绝**断（拒绝是唯一安全默认），冲突见 §7.4 |
| W27 | adapter（第一层白名单） | `Debug` 任务的两个变体：①`mode="ocr"`；②`mode="screenshot"` / 缺省 / `mode=""` | ① 放行（进到 fake lib，计数器 ≥1）；② 抛 `TaskNotAllowed`；`ALLOWED_DEBUG_MODES == frozenset({"ocr"})` | `Debug` 是唯一一个"名字听着只读、参数能改行为"的任务类型；放行任意 mode 等于开了个后门 |

### 3.2 A 组：`tests/test_contract.py` — adapter 的回调 → asyncio 契约

| 编号 | 所属模块 | 前置桩 | 断言要点 | 对应的真实故障 |
|---|---|---|---|---|
| A01 | adapter | fake 在**独立 `threading.Thread`** 里调回调，主线程跑 `asyncio.run` | 事件被主协程收到；`len(events) == 1`；无 `RuntimeError: no running event loop` | 回调在 C 线程，任何"绑定某个 loop"的写法都会失效 |
| A02 | adapter | 协程已在等待，事件 150ms 后到达 | 从事件到达到协程恢复 < 200ms；期间不依赖定长 `sleep` 轮询（断言 fake 的 `get_nowait` 未被高频调用，或用 `loop.call_soon_threadsafe` 计数 ≥1） | 用 `sleep(0.05)` 轮询 → 延迟与 CPU 双输 |
| A03 | adapter | 事件序：`Connected` 先到，`ResolutionGot(1920x1080)` + `UuidGot` 200ms 后到 | 终态由 `AsyncCallInfo(Connect).ret=True` 决定（**不是**由 `Connected` 决定）；`profile.resolution.status is ResolutionStatus.KNOWN` 且 `(width, height) == (1920, 1080)`；**绝不等于默认 1280x720**；`uuid` 非空 | 曾收到 `Connected` 就 return，分辨率永远停在默认 1280x720 |
| A04 | adapter | 事件序完全颠倒：`ResolutionGot` → `UuidGot` → `Connected` → `AsyncCallInfo` | 与 A03 结论**逐字段相等**（含 `resolution.status`）；实现不依赖任何到达顺序 | 顺序假设写死在条件里，换 MAA 版本静默错 |
| A05 | adapter | 只发 `ResolutionError`（width=0, height=0），无 `ResolutionGot` | `profile.resolution.status is ResolutionStatus.FAILED`；**`width is None` 且 `height is None`（不是 0）**；`reason` 非空；**`Resolution(KNOWN, None, None)` 构造必须 `AssertionError`**（`__post_init__` 不变量：禁默认值）；后续缓冲尺寸回退默认且 > 0；状态页不得显示"已获取" | F20：`ResolutionError` 是真实分支，旧代码只判 `ResolutionGot`，会带着 1280x720 一路跑并谎报已获取 |
| A06 | adapter | 只发 `AsyncCallInfo{what:"Connect", async_call_id:N, details:{ret:true, cost:12}}`（**两个分辨率事件都不发**） | `connected is True` 且**立即**结束等待（耗时 ≪ timeout）；`resolution.status is ResolutionStatus.UNKNOWN` 且 `width/height is None`（`ret=True` **不能**推出分辨率已回填，`get_scale_size()` 无 C API 暴露）；`uuid` 可能为空 | 把 `ret=True` 当"全都齐了" → 用默认尺寸建缓冲；或反之完全不认它而空等到超时 |
| A07 | adapter | `AsyncCallInfo{what:"Connect", details:{ret:false}}` + `ConnectFailed{why:"..."}` | 立即失败；`AdbDisconnected.why` 为 MAA 原文；文案含真实 address | 把失败当"还没收到"继续等 |
| A08 | adapter | `ConnectionInfo{what:"ConnectFailed", why:"Address does not contain ':' and no devices found"}` | 文案含**why 原文**与真实 address；不含"5555" | 用户看到的必须是 MAA 的原话（F17） |
| A09 | adapter | `address=""` | 抛 `AdbAddressMissing`（**在任何 ctypes 调用之前**）；文案指向 `adb_address`；不含任何端口号；fake 的 `async_connect` **未被调用** | 曾虚构 5555 报给用户（F17） |
| A10 | adapter | fake `async_screencap` 阻塞在 `threading.Event` 上，用例 cancel 协程 | cancel 后：无 pending task；释放 Event 后工作线程 2s 内结束；`threading.active_count()` 回落基线；`_CallSlot` 已标记 ABANDONED | 超时后 `to_thread` 线程仍在跑 → 每超时一次永久吃一个 worker |
| A11 | adapter | 调用 A 超时（`CaptureTimeout` 已记入 `last_error`），随后到达 `async_call_id == A` 的迟到失败，再发起调用 B | 迟到 id 不在 `_pending` 里 → **`stats.events_dropped_late += 1`**；`last_error` 仍是 `CaptureTimeout`（派生只读快照，无版本号、无 `_error_seq`）；B 只收到自己 id 的事件 | 共享字段 + 版本号比对；迟到的孤儿线程把用户看到的原因改写成"缓冲不足" |
| A12 | adapter | `connect→cancel→connect→cancel→connect` 三轮 | 每轮 `_CallSlot` 独立；回调对象是同一实例（模块级 keepalive 名单，**永不注销**）；无 `never retrieved` 告警 | 每轮泄漏一个回调强引用；或 CFUNCTYPE 被 GC → UAF |
| A13 | adapter | 回调载荷为 `b"\xff\xfe"`、`b"{not json"`、`None` | 均不抛穿到 C 线程；事件被丢弃并计数 | C 线程里抛异常 = 未定义行为/进程崩 |
| A14 | adapter | 调用 A 已结束，随后到达 `async_call_id == A` 的迟到事件，再发起调用 B | 迟到事件被**按 id 丢弃**（F21）；B 只收到自己 id 的事件；B 的结果不来自 A | 盲扫队列 + 谓词猜 → "第二次同步"拿到第一轮结果并写进缓存 |
| A15 | adapter | fake `AsstGetImage` 返回非 PNG 字节（`b"\x00" * 64`，且与 BGR 裸像素**等长**以排除"长度凑巧"） | 抛 `CaptureFormatUnexpected(magic=<首 8 字节>, length=64)`；**原始字节确实落盘**（文件存在、字节逐一相等、长度 64）；落盘路径出现在文案里；**不得尝试二次编码兜底**（断言没有第二次 `imencode`/`PIL`/`cv2` 调用，且上行的是原始字节的落盘路径而不是"修好的 PNG"） | 旧 `maa_adapter.py` 的 docstring 就记着"PNG/RGB 二次编码"这个坑。二次编码会把"库版本不对"变成一个能正常显示的错图，**问题被吃掉且再无复现证据** |

### 3.3 R 组：`tests/test_contract.py` — runtime 的引擎供给契约

| 编号 | 所属模块 | 前置桩 | 断言要点 | 对应的真实故障 |
|---|---|---|---|---|
| R01 | runtime | 注入 fetcher 返回内容与 manifest 里 sha256 不符 | 抛 `EngineHashMismatch`；**磁盘上该文件已被删除**；未调用 `AsstLoadResource` | 坏包被当可用 → 之后每次加载都失败，且看不出原因 |
| R02 | runtime | fetcher 抛“连接中断”且已写入一半 | 抛 `EngineDownloadFailed(url, target, kind=DownloadFailure.NETWORK)`；半包文件不留在最终路径；**失败后 state 回 `ABSENT`（不是 `FAILED`）**，原因记 `last_error`；未调用 `AsstLoadResource` | 半包被当完整包，加载失败却在报“资源缺失” |
| R03 | runtime | 构造含 `../../evil.dll` 与绝对路径成员的 zip | 抛 `EngineUnpackFailed(archive, target_dir, kind=UnpackFailure.UNSAFE_MEMBER, member=...)`；断言目标路径 resolve 后仍在解压根内；`evil.dll` 在解压根外**不存在** | zip slip：攻击者构造的引擎包能写到插件目录外 |
| R04 | runtime | manifest 要求 `maa>=6.0`，实际 `v5.2` | 抛 `EngineLoadFailed(symbol=<缺失符号名>)` 或 `ResourceLoadRejected`；`symbol` 字段非空（供文案分档）；不进入 `LOADED` | 版本不匹配时符号缺失，报错变成"加载 DLL 失败" |
| R05 | runtime | 两个协程 `asyncio.gather(bootstrap(), bootstrap())` | fetcher 只被调用 **1 次**；两个结果相同（同一对象或等值）；无第二次解压。**幂等键是 `self._download_task` 单句柄 + `task.done()` 判断，不是 marker 文件**（`.maa-provision.json` 是"下次启动跳过下载"的安装产物标记，不是并发锁——这条也要断） | 并发 bootstrap 各下一份，互相覆盖半成品 |
| R06 | runtime | 第一次 bootstrap 失败；第二次成功 | 第二次不因上一次失败而直接返回失败（失败态不粘住） | 一次网络抖动之后永久不可用，用户只能重装插件 |
| R07 | runtime | 插件实例的 `data_path` 指向临时目录 | 所有产物（下载、解压、缓存）都在 `data_path("maa")` 之下；`plugin_dir` 下**没有新增文件** | 写进只读安装目录 → 权限错误或污染包 |
| R08 | runtime | 5MB 假流，fetcher 记录每次 `read(n)` | 每次读取 `n <= 1MB`（流式校验/写盘），峰值内存不随文件线性增长 | 全量 `read()` 进内存 → 大引擎包直接把宿主内存拉满 |
| R09 | runtime | **两个变体**：①解压时注入 `OSError(ENOSPC)`；②注入 `PermissionError`（目标目录无写权限） | ① 抛 `EngineUnpackFailed(...kind=UnpackFailure.NO_SPACE)`，且 `detail` 含**真实探测到的缺口 MB 数**（`shutil.disk_usage`；探测失败时退化为无数字，**不许编**）；② 抛 `EngineUnpackFailed(...kind=UnpackFailure.WRITE_ERROR)`。两者都：可被上层 `except EngineUnpackFailed` 接住、解压根内不留半成品目录、`detail` 非空。**`kind` 断言不再留 `TODO`**（§1.2.2 已闭合） | 失败后留下"看起来完整"的目录下次被当已就绪；或把"磁盘满/无权限"报成"包坏了"，把用户推去重下 295 MB |
| R10 | runtime | 解压中途 `interrupt` | 先解到临时目录再原子 rename；中断后最终目录**不存在**（或仍为旧版本） | 非原子解压 → 半个引擎被当可用 |
| R11 | runtime | 假 `ReleaseSpec` 的 `sha256` 为空串（老 release 可能如此；字段与类型同住 `maa/provision.py`） | 抛 `EngineHashMismatch(expected="<unavailable>")` 或直接拒绝该 release；**绝不跳过校验继续下载/解压**（安全红线） | 缺哈希就静默不校验 → 被替换过的引擎包可长驱直入 |
| R12 | runtime | 磁盘上无引擎；`bootstrap()`（无 `allow_download` 参数）；fetcher 记录调用次数，且**永不返回**（模拟慢网） | ① 返回 `EngineState.DOWNLOADING`（**不是** `ABSENT`）；② 调用耗时 **< 1s**（证明没有等待下载）；③ fetcher **恰好被调用 1 次**（不是 0 次）；④ 再调一次 `bootstrap()` → fetcher 调用次数**仍是 1**（复用 `download_task`，不并发起第二次） | 曾把默认值定成"不下载"，砍掉了用户明确要求的"首次启动自动下载"；反过来若在钩子里 await 295MB，慢网必然撞破宿主 300s 上限 |
| R13 | runtime | 本地 `http.server` 起一个几 KB 的合法假 zip（hash 由测试真算） | 走完 `download → verify_archive → unpack` 全链，state 到 `UNPACKED`；用真哈希而非 mock 掉校验；**不访问真实网络** | 下载链路只有真机验证 → 一旦坏就只能在用户机器上发现 |
| R14 | runtime | `unpack()` 成功后调 `purge()` | `engine_root` 下**不留任何引擎文件**（zip + 解压产物都删）；state 回到 `ABSENT`；再次 `bootstrap()` 不认为已就绪 | 解压后占盘约 2 倍；`arknights_engine_repair --clean` 名不副实 |
| R15 | runtime | tmp 里造三种布局：`MaaCore.dll` 在 `root/`、`root/bin/`、`root/lib/`（各自带 `resource/config.json`） | 三种布局 `RuntimeLayout.resolve()` 都能找到 `library_path` 与 `resource_parent`；`resource_parent` 必须是**含 `resource/` 的那一层**（不是 `resource` 本身）；找不到时 `resource_parent is None` 且 `missing_pieces()` 非空 | `[GUESS]` 该 zip 内层布局未实测，靠“名字猜 resource”正是旧实现的病根 |
| R16 | runtime | ①遍历迁移表（含 `DOWNLOADING→DOWNLOADING` 自环、`DOWNLOADING→ABSENT`、`ABSENT→UNPACKED` 手动放置）；②`on_startup` 里调 `bootstrap()`，fetcher 永不返回 | ①禁止迁移（`ABSENT→READY`/`ABSENT→LOADED`/`VERIFIED→READY`/`READY→DOWNLOADING`/**`DOWNLOADING→UNPACKED`**/`DOWNLOADING→LOADED`）抛 `EngineNotReady(state=..., required=...)`（**必须关键字传参**，位置传参应 `TypeError`）；合法链逐级成立；**`DOWNLOADING→DOWNLOADING`（进度自环）、`DOWNLOADING→ABSENT`（取消/失败清半包）、`ABSENT→UNPACKED`（手动放置）三条必须合法**；**`DOWNLOADING→FAILED` 不存在**——失败也回 `ABSENT`，原因记 `last_error`；`kind=cancelled` 时 state 落 **`ABSENT` 不落 `FAILED`**；②`on_startup` 返回后状态是 **`DOWNLOADING`**，且**未 await 下载完成**（耗时 **< 1s**）；`on_shutdown` → `cancel_download()` 必须 **cancel 并 await** 收尾，之后 `.part` 不存在 | 这条用例的唯一价值是锁"启动不被 295MB 拖死"——写反了就等于把这个坑又挖回来。**迁移表本轮改判**：`ABSENT→UNPACKED` 从"禁止"移到"允许"（手动放置路径，D6） |
| R17 | provision | 静态扫描 `maa/provision.py` 的 AST | 所有 `raise` 节点的异常类型 **⊆ `{EngineDownloadFailed, EngineHashMismatch, EngineUnpackFailed, EngineLayoutInvalid}`**（即 `PROVISION_CODES` 对应的四个类）；**不得**出现自定义新异常（含 `ProvisionError` 之类的中间基类）；`runtime` 只做边界捕获 + 状态落地，**不做异常类型翻译** | 两套异常族 → 必须写翻译层 → 漏映射一条，service 拿到陌生异常走兜底 → 复现旧实现"所有问题都变成 ADB 没连上" |
| R18 | runtime | state 被置于 `DOWNLOADING`（后台下载中） | 调 `capture()` **快速失败**：抛 `EngineNotReady`，且 `exc.state == "downloading"`（`state` 字段是 `EngineState.value`）；耗时 **< 100ms**；**不得阻塞等待下载**；fake 的 `AsstGetImage` 未被调用。`progress` 字段口径见 §7.4（两处文档不一致，已提请架构收口） | 下载期间一次截图请求就阻塞 → 该 entry 必被宿主超时打穿（295MB 慢网下窗口很长） |
| R19 | runtime | fake lib 的调用记录器；序列 = `set_user_dir` → `load_resource` → `create_instance` | 调用序号严格递增：`AsstSetUserDir` **先于** `AsstLoadResource` **先于** `AsstCreateEx`；且 `load()`/`bootstrap()` **不得隐式**调 `set_user_dir`（主理人：隐蔽副作用比少一次便利危险） | F18：`AsstLoadResource` 在 UserDir 为空时会把它绑到资源父目录 → MAA 的日志/调试目录被写进资源目录。**编号变更**：原 W21，主理人裁决移到 R 组（属 runtime 编排契约，不是 wrapper 契约） |


### 3.4 S 组：`tests/test_service.py` — 异常状态机与猫娘文案

| 编号 | 所属模块 | 前置桩 | 断言要点 | 对应的真实故障 |
|---|---|---|---|---|
| S01 | service | 遍历 `ErrorCode` 全部 **18 个成员** + 每个成员的全部 `variants` | `set(_COPY) == set(ErrorCode)`（少一个码即红，**这条同时是生产代码的导入期不变量**：`assert set(_COPY) == set(ErrorCode)`）；每个码的文案非空；**`(text, hint)` 组合在"码 × 变体"层面唯一**（两两不同要**排除 variants 分组**：`download_failed` 的七个变体本来就是不同文案，`network` 与 `throttled` 允许相似）；`EngineLoadFailed` 的 `symbol` 两档文案**必须不同** | 文案串台：缺库报成"ADB 没连上"，用户去重装模拟器 |
| S02 | service | 每个码 | 文案含**可执行动作**（与 §3.5.1「排查方向」列对应）：`adb connect` / `adb_address` / `resource/` / `searched` 路径 / 重试入口名；不含只有"失败/错误"的空话。**`CaptureEmpty` 额外硬约束（G3 未闭环）**：文案**不得含具体归因词**——断言 `"ADB" not in text and "掉线" not in text`，只允许"这次没拿到画面（引擎说手里是空的）"这种**不指定原因**的形态。**锁"不许猜"，而不是锁某种猜法** | 只说"失败了"，用户无法自救；猜错原因比不说更贵 |
| S03 | service | 抛 `RuntimeError("boom at C:\Users\x\secret")`（非 `EngineError`） | 兜底文案；不含 traceback；不含绝对路径；含一个通用下一步 | 兜底文案把内部路径/栈泄给用户 |
| S04 | service | stub `push_message` 记录 kwargs，遍历 18 个码 **+ 全部 `variants`** | **逐码按 §3.5.1 表断言**：`visibility`（`AdbDisconnected` 必须含 `"hud"`，其余 `["chat"]`）、`ai_behavior`（`AdbAddressMissing`/`AdbConnectTimeout`/`AdbDisconnected`/`TaskNotAllowed` 为 `respond`，**其余 14 个为 `read`**）、`priority` 相等；`parts` 非空 list。**分档硬断言（本轮新增）**：同一 `code` 的不同 `kind`，产出的 `parts` **必须不相等**——具体至少 `ENGINE_UNPACK_FAILED` 的 `BAD_ARCHIVE` / `UNSAFE_MEMBER` / `WRITE_ERROR` 三条互不相等，且 `NO_SPACE` 与 `WRITE_ERROR` 也不相等（一个说清盘、一个说权限）；`engine.download_failed` 的 `CANCELLED` 必须走 `_render_paused()`（文案"已暂停喵"、`priority=2`、**不含"失败"字样**、不计失败计数）。**另加一条守门断言（架构 §3.5 的生产代码不变量）**：`_KIND_DOMAINS` 里 `set(_COPY[code].variants) == {k.value for k in kind_enum}`——从 `_COPY` 里删掉一个 variant 必须**导入期 `AssertionError`**（否则漏档的表现不是报错，而是"磁盘满被说成包坏了，请重下 295MB"）。配对变异 **M46** | 全部写死 `respond` → 14 个码会抢答；只按 `code` 分档 → 磁盘满和包损坏说同一句，用户不知道清盘还是改权限 |
| S05 | service | 触发需要配图的场景（stub `ctx.images.upload` 返回一个带 `{"type":"image",...}` 的 part） | **直接断言**（不再写"形态无关断言"）：`parts[n]["type"] == "image"`、`isinstance(parts[n]["data"], bytes)`、`parts[n]["mime"] == "image/png"`；`upload` 的返回值被**原样**放进 `parts`（不是自己拼一个）；文本 part 里不出现裸文件路径；`push_message` **是同步调用**（对返回 dict 用 `await` 会 TypeError——所以断言结果是 dict，不是 awaitable） | 把路径当文本发出去，模型读不到图 |
| S06 | service | stub `push_message` 返回 `{"submitted": False, ...}` | 只调用 **1 次**（不重试）；**文案与 S04 断言的那份完全相同**（不改文案）；`Outcome.push_failure_reason` 非空（值为 `backpressure` / `transport_error` / `transport_unavailable` 之一）；`submitted` 是权威判据（`ok` 不参与判定） | 无视 `submitted` 反复重推刷屏；或把 `ok=True` 当成功而丢失降级信号 |
| S07 | service | 同一码连续触发 3 次（同一消息窗内） | 有效 `push_message` 调用 ≤ 1；或第 2/3 次合并进一条 | 截图连续失败 → 用户被同一句话刷屏 |
| S08 | service | 异常携带原生值 `18446744073709551615` / `0x7ff...` / 绝对路径 | 文案里不出现裸哨兵数字/指针/绝对路径；出现的是人话；原始值只在 `context`（`to_dict()['context']`） | 把 `2**64-1` 直接甩给用户 |
| S09 | service | stub `push_message` 抛 `SdkError` | 原始业务异常不被吞掉（Outcome 仍带原始 `code`）；`push` 的异常不二次抛出 | 推送失败掩盖真实故障，排障方向全丢 |
| S10 | service | 每个码 | 文案含猫娘口吻标记（称呼/语气词）**且**同时含 S02 的动作信息（同一条断言两个条件） | 只卖萌没信息，或只有冷冰冰的报错 |
| S11 | service | 模拟 `on_startup` 路径（G5 缓解：启动期只存 pending_notice，不 push） | `on_startup` 期间 `push_message` **零调用**；`ctx.images.upload()` **零调用**（该 API 明确禁止在生命周期钩子里调）；补发发生在第一次业务 entry 内 | 钩子里发消息/传图 → 宿主侧不可预期失败，或启动被判 startup-failed |
| S12 | service | 触发 `TaskNotAllowed`（如用户说"帮我打这一关"） | 文案**不得含"失败/错误/异常"字样**（它是安全边界，不是故障，说"失败"会让用户以为是 bug 而反复重试）；必须含"不代打"这类边界说明；`ai_behavior == "respond"`（要回话解释，不能只 `read`）；`visibility == ("chat",)`；**不写审计日志为 error 级** | 把安全拒绝报成故障 → 用户反复重试，模型还会道歉说自己坏了 |
| S13 | service | 调 `service.execute_automation(...)`（`logic.py` 的自动化入口） | **必抛 `NotImplementedError`**；**零副作用**：`push_message` 零调用、`upload` 零调用、adapter 的 `run_task` 零调用、`last_error` 不被改写；异常消息含"初版不支持"类说明 | 这是未来自动化的入口。将来有人把它实现成"真能执行"，这条会**立刻红**——比在 PR 里靠人眼盯可靠 |

### 3.5 X 组：元用例（防"文档与代码漂移"）

| 编号 | 所属模块 | 前置桩 | 断言要点 | 对应的真实故障 |
|---|---|---|---|---|
| X01 | infra | 读取本文件 `docs/02-test-plan.md` | 表格里出现的编号集合 `==` 收集器登记的编号集合；无多余、无缺失 | 文档说有 65 条，实际跑了 59 条，没人发现 |
| X02 | infra | 导入 `test_contract`/`test_service` 模块 | 模块内所有 `test_*` 可调用对象都已被登记（反之亦然）；编号无重复 | **上一版栽的正是这个**：`pytest` 能跑但脚本入口静默跳过 |
| X03 | infra | `ast` 解析 `companion/service.py` | ①其 `import` / `from ... import` 节点**不得**出现 `maa.wrapper` / `maa.provision` / `maa.runtime`；允许 `maa.errors` 与 `maa.adapter`（service 持有 adapter 的只读句柄）；②**全文不得出现 `get_image_bgr` 标识符**（`Name`/`Attribute` 节点都扫）——`AsstGetImageBgr` 保留但"禁上行"，用**静态不可达**达成，而不是靠删方法（留着它才能写"PNG 与裸像素字节不同、只有 PNG 过魔数"的对照测试） | 有人为图方便在业务层直接 `from ..maa.provision import KNOWN_RELEASES`，把供应链逻辑漏进业务层；或把裸 BGR 当 PNG 上行（图全坏且无魔数可查） |
| X04 | infra | `ast` 解析 `maa/adapter.py` | 所有 `EngineNotReady(state=...)` 的取值 ⊆ `{s.value for s in EngineState}`：字符串字面量、模块级常量（`STATE_ABSENT: Final[str] = "absent"`）、`EngineState.X` 三种写法都要**解析出来再比**；解析不出来（变量/`getattr`/f-string 拼的）判失败 | 已经出现过 `state="unloaded"`——异常类型完全正确，不会让任何用例红，只会让状态页显示一个不存在的阶段 |

---

### 3.7 P 组：`tests/test_contract.py` — `maa/provision.py` 的攻击面（离线）

> 来源：`docs/03-engine-provisioning.md` §6 的 A/B/C 三组（云安给出 60+ 条，他本人已用伪造 opener 跑通 29 条断言）。
> 编号按"文档追加顺序"排在 §3.7，**不重排既有 §3.1–§3.5**，以免引用漂移。
> **硬约束：全部离线**——`io.BytesIO` 伪造 opener / `file://` / 本地 `http.server`；
> 任何用例触碰真实 GitHub 即视为违规（`KNOWN_RELEASES` 的哈希是发版时人工固化的常量，运行时调 API 是**错误行为**，不能写成合法路径）。
> 被测对象是 `provision` 的**模块级公开函数**，不需要构造 `MaaRuntime`：
> `verify_sha256(path, expected) -> bool`、`safe_extract(archive, dest, *, policy=None) -> ExtractResult`、
> `atomic_download(url, dest, expected_sha256, *, expected_size=None, allow_reuse=True, use_env_proxy=False, timeout=30, on_progress=None, cancel=None) -> Path`。

**一条用例覆盖 N 个变体**：A1 的 14 个恶意条目名用**表驱动**在一条用例里跑完（每条断言独立、失败时报出具体变体名）。
这样注册表仍是"一条用例 = 一个函数"，但覆盖不缩水。

| 编号 | 所属模块 | 前置桩 | 断言要点 | 对应的真实故障 |
|---|---|---|---|---|
| P01 | provision | 14 个恶意条目名表：`../evil.txt`、`../../evil.txt`、`a/../../evil.txt`、`..\..\evil.txt`（反斜杠）、`/etc/cron.d/x`、`\\server\share\x`（UNC）、`C:\Windows\evil.dll`、`C:evil.txt`、`file.txt:evil`（NTFS ADS）、`CON`、`NUL.txt`、`aux `（尾空格）、`a.`（尾点）、`a<b>.txt`；**另加两条"排除前缀也不吃穿越"**：`resource/../../evil.dll`、`externals/../../evil.dll` | 每个变体①被拒绝；②**`dest` 之外不存在任何新文件**（在 `dest` 外预置哨兵文件，断言 `dest` 父目录的文件集合**完全不变**）；③`externals/../../evil.dll` **必须在路径校验阶段就失败**，不能被排除逻辑静默跳过（否则这个攻击样本永远看不见）；④**清空对照**：显式传 `ExtractPolicy(exclude_prefixes=(), exclude_globs=())`（等于拆掉黑名单）后，`resource/../../evil.dll` **仍然必须被拒** | 只看"抛没抛异常"会漏掉"先写后校验"的实现；写过一次就已经落地了。④ 是唯一能证明"黑名单不是安全边界"的断言——没有它，将来没人拦得住有人把白名单换回黑名单 |
| P02 | provision | 手写 `ZipInfo`，`external_attr = (0o120755 << 16)`，内容 `../../etc/passwd` | 拒绝；`dest` 外无新文件；**不得**在 POSIX 上真的创建符号链接 | 符号链接条目是 zip slip 的变体：解压后路径合法但指向外面 |
| P03 | provision | 加密条目（把 `flag_bits` 的加密位置 1，或用 `pyzipper` 造样本） | 拒绝（不能"跳过但继续"）；`skipped` 里不得混入加密条目当正常跳过 | 加密条目读不出内容，静默跳过会得到残缺引擎 |
| P04 | provision | 1 MiB 压缩 → 100 MiB 零字节 | 压缩比/解压总量上限触发；拒绝；磁盘未被打满 | zip bomb 在写盘阶段才爆炸 |
| P05 | provision | 20 万个空条目 | 条目数上限触发；拒绝且**不创建 20 万个文件** | 条目数攻击（inode 耗尽） |
| P06 | provision | 篡改中央目录 `info.file_size` 使其小于实际流 | 写出阶段二次计数触发 `SIZE_DRIFT`；拒绝；无残留 | 只信中央目录声明的大小 = 磁盘可被打爆 |
| P07 | provision | ①正常包但删掉 `MaaCore.dll`；②`MaaCore.dll` 出现两份 | 均拒绝；且抛出的异常**必须能被 `runtime` 的 `except` 接住**并译成布局类错误（具体类名待 §1.2.1 收口，`TODO`） | 解出"看起来完整"的目录，加载阶段才炸 |
| P08 | provision | 正常包（核心库 + 依赖 + `resource/` 子树） | 解压后逐条目 `hashlib` 比对与包内一致；POSIX 上可执行位保留（Windows 跳过该断言） | 内容被截断/权限丢失 → 加载或读资源失败 |
| P09 | provision | `dest` 已存在且**非空** | 拒绝（不静默合并）；抛出的异常必须能被上层 `except` 接住（类名待 §1.2.1 收口，`TODO`） | **云安自测踩到**：`mkdir(exist_ok=False)` 抛的是 `FileExistsError`，上层接不住 → 冒成 500 |
| P10 | provision | **v1.2 真实包结构口径**（10 条目，无 `MAA/` 包裹层）：`MaaCore.dll`、`opencv_world4.dll`、`onnxruntime.dll`、`MAA.exe`、`MAA.pdb`、`README.md`、`resource/tasks.json`、`resource/template/x.png`、`externals/PresentationCore.dll`、`externals/System.Runtime.dll` | `selected == 6`（3 个 DLL + 2 个 resource + `README.md`）；`excluded == 4`（`externals/**` ×2 + `MAA.exe` + `MAA.pdb`）；`skipped == 0`；`core_lib == dest/MaaCore.dll`；`resource/template/x.png` 存在（子目录递归放行）；**`README.md` 必须存在**——这是"根目录放行"策略的**已知后果**，写断言是为了**防止未来有人把白名单换成黑名单**（顺带一变体：`externals/` 放 500 个条目 → `excluded == 500` 且一个都不在 `selected` 里，防计数口径写错导致磁盘悄悄变大） | 两个方向的错：漏解依赖 DLL（引擎起不来）/ 多解 GUI 运行时（死重量 + 攻击面）；计数口径错则"排除了"只是嘴上说 |
| P11 | provision | `MaaCore.dll` + `resource/../../evil.dll` + `externals/../../evil.dll` | 全部拒绝（`kind=UNSAFE_MEMBER`）；`dest` 外无新文件；**清空 `exclude_prefixes`/`exclude_globs` 后仍拒绝** | **证明白名单/黑名单都不能替代路径校验**——这是最容易写错的一条 |
| P12 | provision | ①0 个核心库；②**歧义**：`MaaCore.dll` 与 `bin/MaaCore.dll` 同时存在；③缺 `resource/`；④`bin/` 兜底：`MAA.exe` + `bin/MaaCore.dll` + `bin/opencv_world4.dll` + `resource/a.json` | ①②③ 拒绝（分别 `EngineLayoutInvalid` / `EngineLayoutInvalid` / `ResourceMissing`）；④ `core_lib` 指向 `bin/MaaCore.dll`、`selected == 3`、`excluded == 1`、`resource/a.json` 存在 | 多核心库 = 不确定加载哪个（版本混淆攻击面）；0 个 = 必然加载失败却报成"资源缺失"。**注**：架构 A11 曾把"核心库在根目录"记作已知退化行为，安全 v1.2 实核真实包**就是根目录并列**，故根目录是正常布局（见 §7.4 改判） |
| P13 | provision | 真值表 7 条：正确哈希 / 带 `sha256:` 前缀 / 大写 hex / 改一字节 / 截断 / 空 `expected` / 文件不存在 | 前三者 `True`；后四者 `False`；**全程不抛异常**（这是契约：失败用返回值表达） | 抛异常会让调用方把"校验失败"当"程序错误"，走上不同降级路径 |
| P14 | provision | 5 MB 文件，spy 记录 `read(n)` 调用 | 读取分块（`read` 次数 > 1，单次 `n <= 1 MiB`）；不一次性读入内存 | 295 MB 全量读入 → 宿主内存峰值翻倍 |
| P15 | provision | 伪造 opener 返回完整内容 + 正确哈希 | `dest` 存在且哈希正确；**`.part` 不存在、`.lock` 不存在**（清理只有一个出口） | 残留 `.part` 会在下次被误认为"半成品可续" |
| P16 | provision | 伪造 opener 返回内容与 `expected_sha256` 不符 | 抛 `HashMismatch`；**`dest` 与 `.part` 都不存在** | 坏包留在盘上 = 下次直接复用（最关键的一条） |
| P17 | provision | ①半包 EOF；②给 `expected_size + 1` 字节；③`Content-Length` 不符；④200 但返回 HTML 错误页 | 均失败；均无残留；②③ 断言**早停**（写出字节数 ≤ `expected_size`） | 服务器返回 200 + 错误页是最常见的"看起来成功" |
| P18 | provision | ①302 跳到 `http://`；②302 跳到 `evil.example.com` | 均拒绝（`REJECTED_SCHEME` / 非白名单 host）；**不跟随** | 允许 https→http 降级 = 传输通道可被降级攻击 |
| P19 | provision | ①写盘时 `OSError(ENOSPC)`；②写一半 `KeyboardInterrupt` | 均无残留（`dest`/`.part` 都不存在） | **必须 `except BaseException`**：只写 `except Exception` 会漏掉 `KeyboardInterrupt` |
| P20 | provision | 两个线程（或进程）同时 `atomic_download` 同一 `dest` | 只有一次真正下载（另一线程等锁或失败）；最终 `dest` 哈希正确；**内容不交错** | 双写同一 `.part` → 交错内容，哈希当然对不上，但用户看到的是"下载随机失败" |
| P21 | provision | 预置一个 mtime 为 1 小时前的 `.lock` | 能自愈（判定陈旧 → 接管）；最终成功 | 一次崩溃留下永久锁 → 插件永远下载不了 |
| P22 | provision | ①`dest` 已存在且哈希正确；②已存在但哈希错 | ① **opener 一次都没被调用**（spy 计数 == 0）；② 删除并重新下载（不是直接复用） | 只判"文件存在"就复用 = 把被替换的引擎当合法 |
| P23 | provision | ①`http://` URL；②非白名单 host；③`expected_sha256` 长度/字符非法 | 立即抛；**零 HTTP 请求**（spy 计数 == 0） | 参数校验放在发请求之后 = 先泄露一次请求 |
| P24 | provision | 设 `HTTPS_PROXY=http://127.0.0.1:1`（死代理），`use_env_proxy=False`（默认） | 仍成功（证明默认不走环境代理） | 用户机器上的代理会把请求导向第三方 |
| P25 | provision | ①`cancel` 已置位；②未置位 | ① 失败且**无 `dest`/`.part`/`.lock` 残留**，且上层拿到的 `kind == "cancelled"`（**不是 `network`**——主理人定了"取消在文案上叫'已暂停'"，kind 落成 `network` 用户会看到"网络错误"）；② 正常完成（不误取消） | 取消路径漏清理 → 残留半包；kind 归类错 → 用户对着"网络错误"反复重试一个自己取消掉的操作 |
| P26 | provision | 3 MB payload，`on_progress` 回调 | 至少调用 1 次；最后一次 `downloaded == total`；**回调自身抛异常时 `.part` 仍被删除** | 进度回调是 UI 注入的，它出错不能污染清理路径 |
| P27 | provision | 检查 `KNOWN_RELEASES` 每一项 | 每项 `sha256` 是 64 位小写 hex、`size_bytes > 0`、`url` 是**tag 固定**形式（含 `releases/download/v{version}/`）、**不含 `releases/latest`**；平台键在支持矩阵内 | `releases/latest` 的资产集合会变；哈希与 URL 必须是发版时固化的常量 |
| P28 | provision | 异步包装：`await` 中被 `task.cancel()` | `cancel` event 已置位；工作线程收尾后 `.part` 不存在 | 协程返回了但线程还在写盘 → 下次启动读到半个文件 |


## 4. 变异清单（验收"测试有牙"的唯一标准）

做法：对 `maa/`、`companion/` 打一个临时补丁 → 跑 `python tests/_runner.py` → 必须**非零退出**且
指定编号的用例变红 → 还原。51 条全部满足才算过（§6.3 给零依赖实现）。

> **变异只随新用例增加，不随心情增加**：主理人先定"99 条 / 42 变异已经够了，不要再扩"；
> 后来他自己改判"前两个缺口必须补"（只读白名单 + 魔数落盘），用例 99 → 104，
> 于是**配套变异也必须 46 → 51**（M47–M51）。除此之外一条不加：
> **没有配对变异能杀掉它，就不算真断言**——这是本计划对"加用例"的唯一门槛。

| # | 变异 | 期望变红的用例编号 |
|---|---|---|
| M01 | `_resource_root()` 返回 `resource` 本体而非父目录 | W01, W02, R19 |
| M02 | 缓冲数组 `c_ubyte` → `c_byte` | W04, W03 |
| M03 | `bytes(buf[:got])` → `bytes(buf)`（忽略 `got`） | W03, W19 |
| M04 | 哨兵判定挪到 `0 < got <= buff_size` **之后** | **W07, W08**（原写 W05/W10，实测选错，见 §4.1） |
| M05 | 删除 `got > buffer_size` 的防御分支 | W20 |
| M06 | 去掉 `MAX_IMAGE_BUFFER_BYTES` 夹逼 | W09 —— **单点等价变异**，必须两处一起拆（M06b），见 §4.1 |
| M07 | 重试次数 `IMAGE_RETRY_MAX + 1` | W08 |
| M08 | 删除 `got == 0` 分支（并入成功路径） | W10 |
| M09 | `AsstGetImage.restype` 写成 `c_size_t` 或 `c_int` | W14 —— `c_size_t` 在 64 位平台**等价**（M09 存活），真截断要用 `c_uint32`（M09b），见 §4.1 |
| M10 | 空句柄不预检，直接调 `AsstGetImage` | W11 |
| M11 | `AsstSetUserDir` 与 `AsstLoadResource` 调用顺序互换 | R19 |
| M12 | adapter 事件入队改用 `loop.call_soon`（非 threadsafe） | A01, A02 |
| M13 | `Connected` 到达即刻 return | A03, A04 |
| M14 | 删除 `ResolutionError` 分支 | A05 |
| M15 | 忽略 `AsyncCallInfo.details.ret is True` | A06 |
| M16 | 写 `last_error` 前不比对 `seq` | A11 |
| M17 | `start()` 不 `drain` 旧事件 | A14, A12 |
| M18 | 取消/超时后不标记 stuck、不解除 orphan 记账 | A10 |
| M19 | 哈希不匹配时不删坏文件 | R01 |
| M20 | 不解校验和就 rename 到最终目录 | R02, R10 |
| M21 | 解压不校验成员路径 | R03 |
| M22 | 去掉 bootstrap 的 in-flight 复用 | R05 |
| M23 | 产物落到 `plugin_dir` 而不是 `data_path("maa")` | R07 |
| M24 | 文案表里任一枚举映射到兜底文案 | S01 |
| M25 | `parts` 里用文本路径代替 image part | S05 |
| M26 | 完整性降级为"只校验 size"时不写标记字段（静默降级，见 §1.1 结论 3） | R01, R04 |
| M27 | `digest` 缺失时跳过校验（改成“无哈希就不校验”） | R11 |
| M28 | `bootstrap()` 缺引擎时**不**起后台任务、直接返回 `ABSENT`（"默认不下载"的老毛病） | R12 |
| M29 | `purge()` 只删归档不删解压产物 | R14 |
| M30 | `RuntimeLayout.resolve()` 只搜 `engine_root/` 一层 | R15 |
| M31 | 禁止迁移不抛 `EngineNotReady`（静默 return 当前 state） | R16 |
| M32 | 文案表里把 `ai_behavior` 统一写成 `respond` | S04 |
| M33 | `on_startup` 里直接 `push_message`（不当成钩子限制） | S11 |
| M34 | `safe_extract` 只做白名单、去掉成员路径校验 | P01, P02, P11 |
| M35 | 校验失败不删 `dest` / `.part` | P16 —— 实现里有两处删除（下载前预清 + 哈希不符兜底），**单点拆任一处都是等价变异**，要双拆（M35c），见 §4.1 |
| M36 | 复用只判 `dest.exists()`、不复核哈希 | P22 |
| M37 | 允许 http 或跟随跨 host 重定向 | P18, P23 |
| M38 | `atomic_download` 去掉 `.lock` 互斥 | P20 |
| M39 | 清理写成 `except Exception`（漏掉 `KeyboardInterrupt`） | P19 |
| M40 | `ExtractResult` 不记 `skipped` / `skipped_sample` | P10 |
| M41 | `on_progress` 回调异常打断 `finally` 清理 | P26 |
| M42 | `provision.py` 遇错直接 `raise RuntimeError(...)`（或用裸 `BaseException` 子类糊过去），不包装成那四个类之一 | R17, P16, P19 |
| M43 | `bootstrap()` 在 `on_startup` 里 `await` 到下载完成 | R12, R16 |
| M44 | `provision.py` 内自定义一个新异常类（如 `class _Bomb(Exception)`） | R17 |
| M45 | `service.py` 直接 `from ..maa.provision import KNOWN_RELEASES` | X03 |
| M46 | `_COPY[ENGINE_UNPACK_FAILED].variants` 的三个 `kind` 指向**同一份** `ErrorCopy`（分档只写在文档里，代码没真分） | S04 |
| M47 | adapter 的第一层白名单删掉（只剩 runtime 那一层），或改成"先 `AsstAppendTask` 再检查" | W26 |
| M48 | `READONLY_TASK_TYPES` 里补进 `Infrast`（或 `Debug` 放行任意 `mode`） | W26, W27 |
| M49 | `TaskNotAllowed` 的文案改成"任务执行失败喵，再试一次吧" | S12 |
| M50 | `CaptureFormatUnexpected` 分支里加"二次编码兜底"（`cv2.imdecode` → `imencode`），或丢弃原始字节只报长度 | A15 |
| M51 | `execute_automation` 改成真的 `await self._adapter.run_task(...)` | S13 |

### 4.1 验牙实况（`tests/_mut_matrix.py`，进程内改源码、不动仓库里的 `maa/`）

跑法：`python tests/_mut_matrix.py`（`--list` 看登记表，`M02 M31` 只跑指定几条）。
每条变异都会重建整条 `errors → wrapper → provision → adapter → runtime` 链（只换一个模块
会让另一层继续引用旧对象，出现"假存活"）。

| 轮次 | 结果 |
|---|---|
| 第一轮（A 组，`_mut_a.py`） | 9 条 / 杀 9 |
| 第一轮（R 组，`_mut_r.py`） | 4 条 / 杀 4 |
| 第一轮（P 组，`_mut_p.py`） | 4 条 / 杀 4 |
| 矩阵（`_mut_matrix.py`） | **26 条 / 杀 22 / 存活 4 / 坏变异 0** |
| 合计已验 | **43 条**，其中 4 条被判定为**等价变异**（不是"用例没牙"） |

**4 条存活全是"隐藏的等价变异"**——实现里有**两道独立防线**，拆一道另一道仍然拦得住：

| 存活 | 为什么等价 | 配对的双拆变异（已杀） |
|---|---|---|
| M06 去掉字节上限夹逼 | 倍增循环里还有 `size * 2 > max` 的预算判定，会提前 `no_budget_to_grow` | **M06b**（两处一起拆）→ W09 红 |
| M09 `restype` 改 `c_size_t` | 64 位平台上 `c_size_t == c_uint64`，同宽同符号 | **M09b**（`c_uint32`）→ W14 红 |
| M35 哈希不符时不删目标 | 下载前的"预清旧归档"已经删过一遍 | **M35c**（两处一起拆）→ P16 红 |
| M35b 下载前不预清 | 哈希不符分支里还会再删一次 | 同上 |

**M04 的受害用例在文档里写错了**：W05 的哨兵刻意落在 `(0, buffer]` 内（12345），
"哨兵先判"和"哨兵后判"两种顺序走**同一个分支**，所以 W05 不可能变红；W10 的 `got == 0`
根本到不了哨兵判定。这个变异真正改掉的行为是"不再倍增重试"，所以受害用例改为
**W07/W08**（实测两条都红）。

### 4.2 这一轮揪出的 4 处"假绿"（用例已修，实现未动）

| 位置 | 假绿形态 | 修法 |
|---|---|---|
| `_tmpdir()` | `with` 出口就删目录 → `assert not dest.exists()` **恒真**（P15/P16/P17/P19/P20/P21/P22/P25/P26 的"失败路径无残留"全部是空断言） | 改成**进程退出才删**（`atexit`），`with` 之后 `dest.exists()` 才有对象可断言 |
| P16 变体 | 同上（旧包是否被删从来没被检查过） | 依赖上面那条修好后自然有牙 |
| W20 | 没传 `buffer_size` → 初始 64MB 缓冲先撞"预算超限"，`got > size` 分支**一次没跑到**（M05 因此存活） | 传 `buffer_size=cap`，并断言 `detail.reason == "returned_more_than_buffer"` |
| P10 | 样本里没有"正常但不在白名单"的条目 → `skipped == 0` **恒真**（M40 因此存活） | 加一条 `lang/zh-CN.txt`，断 `skipped == 1` 且 `skipped_sample == ("lang/zh-CN.txt",)` |

**P04 的加严**（对应"安全用例必须先证明样本具备攻击特征"这条通用规矩）：
原来只断 `kind is LIMIT_EXCEEDED`，而**四条上限分支共用这一个 kind** —— 压缩比判据被拆掉
也照样绿。现在：① 先算样本自身压缩比 > `MAX_COMPRESSION_RATIO`；② 再断言声明的量没越
单文件/总量两条线；③ 断 `detail` 里出现"压缩比"；④ 加一份**同声明体积、不可压缩**的对照组
必须解压成功。变异 `M-P4`（拆掉比值判据）实测红。

---

## 5. 测试基础设施（零依赖）

### 5.1 取舍：不用 pytest，也不用 conftest.py

本机 `python -c "import pytest"` → `ModuleNotFoundError`，且无外网、`pip` 不可用。[FACT 本机实测]
因此**主入口是自建收集器**，同时**保留 pytest 兼容形态**（`test_*.py` + 模块级 `test_*` 函数 +
无 fixture），这样有 pytest 的机器上 `python -m pytest tests` 也能收集同一批函数，作为第二意见。

不引入 `conftest.py`：它只在 pytest 下生效，脚本入口读不到，**反而制造"两套配置各说各话"**。
需要共享的东西一律放 `tests/_fakes.py`（普通模块，两边都能 import）。

### 5.2 目录布局

```
plugin/plugins/arknights_neko_plugin/
├── docs/02-test-plan.md        ← 本文件
├── maa/{wrapper,adapter,runtime,provision,errors}.py   ← provision 与 errors 是**纯标准库**，可脱宿主单测
├── companion/{service,logic,knowledge}.py
└── tests/
    ├── _runner.py              收集器 + 一致性校验 + main() -> int
    ├── _fakes.py               FakeMaaCore / _FakeFunc / FakeFetcher / FakeHost
    ├── test_contract.py        W* / A* / R* / P* / X*
    └── test_service.py         S*（X02 也会在此文件里被复核一次）
```

> **已知债务（主理人裁决：不拆，维持两个文件）**：用户给的分层只列 `test_contract.py` 与 `test_service.py`，
> 这是他的**结构规范**，不是主理人的偏好。**R 组 18 条 + P 组 28 条 + W/A/X 全部压在 `test_contract.py` 里，
> 预计 1800+ 行**。裁决原文：99 条里绝大多数是表驱动，1800 行不会真的难读；等真跑起来觉得疼再拆，
> 那时是**重构**不是偏离设计。→ 本条记成债务，不写"偏离申请"；真拆分时收集器与 X01/X02 一字不用改。

### 5.3 收集器与一致性校验（核心代码骨架）

上一版的实际事故：`main()` 里手写 `tests = [("A1", fn), ...]` 硬编码清单，新增 `test_*` 函数但
忘了登记 → pytest 能跑、脚本入口静默跳过，报告仍然 100% 通过。
**修复思路：注册用装饰器（不可能忘），一致性用断言（不可能静默）。**

```python
# tests/_runner.py
"""零依赖测试收集器。退出码：0=全绿，1=有用例失败，2=收集期错误（绝不当成通过）。"""
from __future__ import annotations

import asyncio
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CASE_PREFIX = "test_"
EXIT_OK, EXIT_FAIL, EXIT_COLLECT = 0, 1, 2


@dataclass
class Case:
    id: str                    # "W01"
    name: str                  # "test_w01_load_resource_receives_parent_dir"
    fn: Callable[[], None]
    is_async: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: list[Case] = []


def case(cid: str, *tags: str) -> Callable:
    """登记一条用例。用例名必须以 test_<编号小写>_ 开头，便于机器校验一一对应。"""
    def deco(fn):
        REGISTRY.append(
            Case(id=cid, name=fn.__name__, fn=fn,
                 is_async=asyncio.iscoroutinefunction(fn), tags=tags)
        )
        return fn          # 原样返回，pytest 仍能收集
    return deco
```

```python
def check_registry(module) -> list[str]:
    """收集期自检：登记表与模块命名空间必须严格 1:1。

    返回问题列表；非空即 EXIT_COLLECT。这四条覆盖了"静默跳过"的全部成因。
    """
    problems: list[str] = []
    funcs = {
        n: v for n, v in vars(module).items()
        if n.startswith(CASE_PREFIX) and callable(v)
    }
    registered = {c.name: c.id for c in REGISTRY}

    # 1) 有 test_* 但没登记 —— 上一版事故的直接成因
    for name in sorted(set(funcs) - set(registered)):
        problems.append(f"未登记（会被静默跳过）: {name}")

    # 2) 登记了但函数不存在（改名/删除后残留）
    for name in sorted(set(registered) - set(funcs)):
        problems.append(f"登记了不存在的函数: {name}")

    # 3) 编号重复 → 报表会互相覆盖
    ids = [c.id for c in REGISTRY]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        problems.append(f"编号重复: {sorted(dup)}")

    # 4) 编号与函数名不对应 → 文档表格与代码会悄悄错位
    for c in REGISTRY:
        want = f"{CASE_PREFIX}{c.id.lower()}_"
        if not c.name.startswith(want):
            problems.append(f"函数名与编号不符: {c.id} -> {c.name}（应形如 {want}...）")

    return problems
```

```python
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--list" in argv:                      # 与本文档表格交叉核对用
        for c in REGISTRY:
            print(f"{c.id}\t{c.name}")
        return EXIT_OK

    only = _parse_ids(argv)                   # --id W05 单跑，变异验证用
    targets = [c for c in REGISTRY if not only or c.id in only]

    # 收集期闸门：任何不一致都直接 2，绝不继续跑成"看似通过"
    problems = check_registry(sys.modules["test_contract"]) + \
               check_registry(sys.modules["test_service"])
    if problems:
        print("\n".join(f"[COLLECT] {p}" for p in problems))
        return EXIT_COLLECT

    # 宿主强制 WindowsSelectorEventLoopPolicy，测试必须同构，否则"测试过 / 宿主挂"
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    fails: list[tuple[Case, str]] = []
    for c in targets:
        try:
            if c.is_async:
                asyncio.run(asyncio.wait_for(c.fn(), timeout=30))
            else:
                c.fn()
        except AssertionError as exc:
            fails.append((c, f"AssertionError: {exc}"))
        except Exception:
            fails.append((c, traceback.format_exc(limit=4)))

    print(f"\n通过 {len(targets) - len(fails)} / {len(targets)}")
    for c, why in fails:
        print(f"  FAIL  {c.id} {c.name}\n        {why.splitlines()[0]}")
    return EXIT_FAIL if fails else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
```

配套的**文档一致性用例**（X01，普通用例即可，无需额外工具）：

```python
@case("X01", "infra")
def test_x01_case_ids_match_plan_doc() -> None:
    """三处编号必须完全一致：用例清单表、附录函数名清单、收集器登记表。

    文档里有两张表（§3 用例清单、§9 函数名清单），都按 `| W01 |` 形状出现，
    所以这里解析成**集合**再比较，顺带保证两张表之间也没有漂移。
    防止"文档说 65 条、实际跑 59 条"这种没人会发现的静默缩水。
    """
    doc = (Path(__file__).resolve().parent.parent / "docs" / "02-test-plan.md").read_text("utf-8")
    in_doc = set(re.findall(r"^\| ([WARSX]\d{2}) \|", doc, re.MULTILINE))
    in_roster = set(re.findall(r"^\| ([WARSX]\d{2}) \| `test_", doc, re.MULTILINE))
    in_code = {c.id for c in REGISTRY}
    # TODO: 断言正文
    # assert in_doc == in_roster == in_code, (
    #     f"清单表多出 {in_doc - in_roster}；附录多出 {in_roster - in_doc}；"
    #     f"代码多出 {in_code - in_doc}；代码缺失 {in_doc - in_code}"
    # )
```

> 注：X01 统计的是**文档里的引号集合**，不是"文档里出现次数"——两张表重复出现同一编号是
> 设计使然（一份给人读、一份给机器核）。

**这套机制我已经先跑过一遍**（不写插件代码、只用假模块，证明"闸门真的会关"）：

```
A 正常模块 -> 0 问题（绿）
B 有未登记函数 -> ['未登记（会被静默跳过）: test_w03_image_bytes_pass_through_exact']
B 实际会被执行的编号: ['W01', 'W02'] | 模块里 test_* 个数: 3      ← 静默跳过被复现
C 函数不存在 -> ['登记了不存在的函数: test_w01_old_name']
D 编号重复 -> ["编号重复: ['W01']"]
E 命名错位 -> ['函数名与编号不符: A01 -> test_a02_wrong_slug（应形如 test_a01_...）']
```

B 那行就是上一版事故的最小复现：模块里有 **3** 个 `test_*`，收集器只会跑 **2** 个，
而旧版报告依然 100%。现在它会在**收集期**直接 `exit 2`。
（这条只证明"机制成立"，**不**证明"用例成立"——用例绿不绿要等开闸后逐组跑。）

### 5.4 五条落地约定（避免"写出来才发现测不了"）

1. **可注入时钟/休眠**：`adapter.py` 不得直接 `asyncio.sleep`，必须走注入的 `clock`
   （默认 `time.monotonic` + `asyncio.sleep`），否则 A02/A03/A10 只能靠真睡眠，慢且 flaky。
2. **可注入 fetcher/解压器（**构造期**，不是方法参数）**：`ProvisionPorts(fetcher=None, extractor=None)`
   由 `MaaRuntime.__init__` 注入，默认走 `provision.atomic_download` / `provision.safe_extract`。
   理由：**后台任务拿不到方法参数、又必须能替换**——所以 `bootstrap(*, force=False)` 上没有
   `fetcher=` 这个参数（我原先那样写是错的）。测试一律注入；测试**永不发真实网络请求**。
3. **不支持"固定睡眠"**：所有等待都等可观测条件（`asyncio.Event`、`threading.Event`、队列长度），
   `_fakes.py` 里提供 `wait_until(pred, timeout)` 辅助函数。超时值只是上界，不是等待时长。
4. **每个用例拥有自己的临时目录**：`tempfile.TemporaryDirectory()` 写在用例内，避免 R 组互相污染，
   也让并发跑（`--id` 单跑）天然幂等。
5. **零真网络是红线，不是偏好**：`KNOWN_RELEASES` 的哈希是发版时人工固化的常量，运行时调 GitHub API 属**错误行为**。
   所有下载类用例必须走伪造 opener / `file://` / 本地 `http.server`。
   **守卫必须是运行期断言，不是 grep 源码**——`patch` 掉 `urllib.request.urlopen` 与 opener 入口，任何命中直接 fail 并打印调用栈。
   （grep 挡不住运行时拼出来的 URL；这是主理人明确要求的口径。）

---

### 5.5 `test_contract.py` 的 import 清单（已定稿，照抄即可）

```python
# tests/test_contract.py 顶部（plugins.<id>.<...> 视角，故用 ..）
from ..maa.errors import (
    ErrorCode, EngineError,
    EngineDownloadFailed, EngineHashMismatch, EngineUnpackFailed, EngineLayoutInvalid,
    PlatformUnsupported, EngineLoadFailed,
    ResourceMissing, ResourceLoadRejected, InstanceCreateFailed,
    AdbAddressMissing, AdbConnectTimeout, AdbDisconnected,
    CaptureTimeout, CaptureFailed, CaptureEmpty, CaptureFormatUnexpected,
    TaskNotAllowed, EngineNotReady,
)                                                   # ← 18 个异常类，一个不少
from ..maa.errors import DownloadFailure, UnpackFailure
from ..maa.provision import (
    KNOWN_RELEASES, ReleaseSpec,                    # 类型与常量表同住一个文件（D5：没有第二个类）
    ExtractResult, ExtractPolicy,                   # P01 的"清空黑名单"对照要构造 ExtractPolicy
    verify_sha256, safe_extract, atomic_download,
)
from ..maa.wrapper import MaaCoreWrapper, MaaCallError          # MaaCallError 只在 W 组断
from ..maa.adapter import (
    ResolutionStatus, Resolution, DeviceProfile,     # 三态分辨率 + 连接结果
    READONLY_TASK_TYPES, ALLOWED_DEBUG_MODES,        # 只读白名单的**定义处**是 adapter
)
from ..maa.runtime import MaaRuntime, EngineState, RuntimeLayout, ProvisionPorts
```

**注意**：`maa.provision` 里**没有**自有异常（裁决 B），所以上面所有异常全部来自 `maa.errors`。
若哪天发现要从 `maa.provision` import 异常，说明有人违反了 R17 的静态不变量——那不是"import 少写了"，是设计被改坏了。

`test_service.py` 的 import 清单**故意更窄**（X03 会扫它）：只允许
`from ..maa.errors import ...` + `from ..maa.adapter import ...`，**不许**碰 `maa.wrapper` / `maa.provision` / `maa.runtime`。

`errors.py` 的 import 路径（三处，别混）：
- 包内（`maa/*.py`）：`from .errors import ...`
- `__init__.py`：`from .maa.errors import ...`
- `companion/*.py`：`from ..maa.errors import ...`


## 6. 验收门槛（过 / 不过判据）

| 编号 | 门槛 | 通过判据 | 不过时的处置 |
|---|---|---|---|
| G0 | 可导入 | `python -c "import maa.wrapper, maa.adapter, maa.runtime, companion.service"` 退出 0 | 阻塞：模块或签名未就绪，回到第 7 节 |
| G1 | 收集一致 | `python tests/test_contract.py --list` 行数 == **92**（契约组）+ `python tests/test_service.py --list` 行数 == 13（S 组）= 105；`check_registry` 零问题；`X01`/`X02` 绿 | 记作**收集期失败（exit 2）**，不允许跳过 |
| G2 | 全绿 | `python tests/test_contract.py` 退出 0 且输出 `通过 92 / 92`；`python tests/test_service.py` 退出 0 且输出 `通过 13 / 13`。**两个文件分开跑**：`tests/_runner.py` 只是收集器模块，直接执行它只会打印提示并以 exit 2 退出 | 修代码，不修测试；任何 `# TODO` 残留视为不过 |
| G3 | 变异全杀 | 51 条变异逐条施加后，`--id <期望编号>` 至少一条红（非零退出）；51/51 | 存活变异 → 补用例，不许删变异 |
| G4 | 真跑一次 | 沙盒里 `GET /plugin/status` 显示插件 `loaded`；`run-entry` 三条路径（正常/边界/错误）各有一份输出证据 | 未跑 = 不过；需先 `prepare`+`install`（§6.4） |
| G5 | 真机校准（**非门禁**） | §2.3 的三条 L5 烟雾全过 | 不过 → 修 fake 的规格，不是修 wrapper 逻辑 |
| G6 | 无静默跳过 | 报告里"跳过/未收集"数 == 0 | 与 G1 同罪 |

**G2 的一条硬规矩**：`# TODO: 断言正文` 未填的用例必须**显式失败**
（骨架期用 `def test_...(): raise AssertionError("未实现")`），
不允许写成"空函数体自动通过"——那会让"用例数"变成一种装饰。

### 6.4 沙盒命令（L4，已核实环境可用）

沙盒入口：`.../neko-plugin-ops/1.6.0/skills/neko-plugin-sandbox/scripts/sandbox.py`，
子命令 `doctor / prepare / sync / install / plugin-add / plugin-rm / serve`（已读源码确认）。
本机 `doctor` 实测：python 3.13.14 OK、git OK、uv OK、磁盘 284 GiB、端口 43117 空闲。[FACT 本机实测]

```bash
python <sandbox>/scripts/sandbox.py doctor  --root .sandbox
python <sandbox>/scripts/sandbox.py prepare --root .sandbox          # 克隆/挂载官方仓库
python <sandbox>/scripts/sandbox.py install --root .sandbox          # 建 venv 装依赖（较重）
python <sandbox>/scripts/sandbox.py plugin-add --root .sandbox \
       --src D:/Copilot_work/neko-plugin/plugin/plugins/arknights_neko_plugin
python <sandbox>/scripts/sandbox.py serve   --root .sandbox          # 常驻，另开终端查路由
curl -s localhost:<port>/health
curl -s localhost:<port>/plugin/status
```

> `doctor` 的 `--root` 目录是本机探测用的临时路径，`prepare` 会克隆官方仓库（需要网络与 ~8 GiB 依赖）。
> 任务书里提到的 `python -m sandbox.cli run-entry ...` 入口**在本机不存在**（未找到该模块），
> 以 `sandbox.py` 的子命令为准。[FACT 本机实测]

---

## 7. 契约状态：主干已闭环，剩 1 项微差待收口 + 5 项待实测

架构文档 `docs/01-architecture.md` 已定稿并成为**唯一契约源**。原先的 8 个阻塞点全部闭环；
本轮新增的"provision 异常归属""`UnpackFailure` 缺档""版本数据类命名""G3 未闭环"四项也已裁决（见 §1.2 与 §7.4）。

| 原阻塞点 | 现在以什么为准 | 状态 |
|---|---|---|
| ① wrapper 的类名/签名/异常族 | `MaaCoreWrapper`（§3.2）；异常族是 `maa/errors.py` 的 **18 条 `ErrorCode`** + 同名异常类（§2.2，每个类有关键字构造参数） | 已闭环 |
| ② 连接结果类型 | `DeviceProfile(connected, uuid, resolution, reason, call_id)`；`Resolution` 是**三态**（`KNOWN`/`FAILED`/`UNKNOWN`），`__post_init__` 禁默认值；`AsyncCallInfo(Connect).ret` 作终态 | 已闭环（A03/A04/A05/A06 按三态重写） |
| ③ runtime 的 bootstrap 契约 | **`bootstrap(*, force: bool = False) -> EngineState`——没有 `allow_download` 参数**（主理人否掉架构首版）；缺引擎时起后台任务即返回 `DOWNLOADING`；版本数据类是 **`provision.ReleaseSpec`**（**不是** `EngineRelease`，D5 已合并）、`RuntimeLayout` 在 runtime；四个 `Path` 由 ctor 注入；`ProvisionPorts` 构造期注入 | 已闭环（R12/R16/R18 按此写） |
| ④ 异常枚举权威清单 | 18 条 `ErrorCode`，`value` 是稳定点分串（§2.1/2.2） | 已闭环 |
| ⑤ `push_message` 形状 | `push_message(source=..., visibility=[...], ai_behavior=..., parts=[...], priority=...)`，返回 dict，**`submitted` 是权威判据**；image part = `{"type":"image","data":<bytes>,"mime":"image/png"}`；拒绝原因 `backpressure`/`transport_error`/`transport_unavailable` | 已闭环（主理人核到官方 SDK 原文） |
| ⑥ knowledge/logic 签名 | `StageAdvice` / `OperatorFact` / `StageFact`（§3.6），纯同步零 I/O | 已闭环 |
| ⑦ `last_error` 形态 | 改为**派生只读快照**（`MaaRuntime.last_error -> EngineError \| None`），不是可变共享字段；每次调用独立 `_CallSlot` | 已闭环（A11 按此断言） |
| ⑧ 只读白名单归属 | `TaskNotAllowed(task_type, allowlist)`，runtime + adapter **双层**校验 | 已闭环（建议再补 2 条：`Fight` 必拒、`Debug` 非 `ocr` mode 必拒） |

**导入路径变更（已按主理人裁定）**：异常模块是 **`maa/errors.py`**（不是根级 `errors.py`）。
包内用 `from .errors import ErrorCode, EngineError`；跨包用 `from ..maa.errors import ...`。

### 7.0 已收口：provision 异常归属（裁决 B）

详见 **§1.2.1**。原先三份文档给了三个落点（安全 §5 在 `provision.py` 内自建族、安全 §8 说放 `errors.py` 且继承
`EngineNotReady`、架构说无中间基类）。**主理人裁决选 B：`provision.py` 直接 `from .errors import` 那四个类，
不养自己的异常族、不要 `ProvisionError` 基类、不要翻译层。**（理由见 §1.2.1，核心是少一层翻译就少一整类
"漏映射 → 掉进兜底文案 → 复现旧实现'一切都是 ADB 没连上'"的故障。）

由此闭环的两件事：① `test_contract.py` 顶部 import 清单已定稿（§5.5）；② "service 只允许 import `maa/errors.py`"
已写成断言 X03（并扩展为同时禁 `get_image_bgr`）。

### 7.1 我这边的 5 项待实测（架构 §7 的 GUESS 清单分派）

按"若不核实，最坏后果"排序。

**铁律：这四项只在"能真跑宿主/真机"时才做；未实测一律在用例里标 `待验`，绝不用猜测值填充断言。**
宁可让用例显式红在"待验证"，也不要用一个编出来的默认值让它看起来是绿的（这正是本项目要根除的"假事实"）。

| # | 待核实 | 怎么验 | 影响哪条用例 | 未核实时的临时口径 |
|---|---|---|---|---|
| G2 | `[plugin.database]` / `[plugin.store]` 表内字段 | 最小插件只加该段，起一次沙盒宿主，看 `self.db` 是否可用（段名已由主理人核为 FACT，字段仍是 GUESS） | 无（`plugin.toml` 归架构） | 不写 `[plugin.database]`；`store` 先按 `[plugin.store] enabled=true` 起草 |
| G3 | `AsstGetImage` 返回 `0` ⇒ "未连接/未初始化" | 两个用例：①拔 ADB 后截图应得 `CaptureEmpty`；②连上但黑屏应得**合法 PNG** | W10、A 组截图链路 | **文案不指定原因**（S02 已按此写：不含 `"ADB"`/`"掉线"`）；实测不符则改文案不改码 |
| G4 | `ConnectionInfo` 的 `what` 全集与顺序 | 真机接一次，把 `(msg, what)` 序列落盘成 fixture，之后所有顺序假设以 fixture 为准 | A03/A04/A05/A08 | 已按"不依赖顺序"设计，故不阻塞；fixture 只用于**加**用例 |
| G6 | `max_image_buffer_bytes` / `capture_timeout` / `resolution_grace` 默认值 | 真机量 P95 截图耗时 | W09 | 全部走构造参数注入，测试不依赖具体数值 |
| G8 | 不声明 `timeout` 时宿主给 entry 的默认值 | 写一个故意 sleep 12s 的 entry 看是否被掐断 | 无（入口超时预算不在本计划断言范围） | 不假设；`arknights_capture` 显式声明 30s |

> G5（钩子里能否 `push_message`）架构已用"启动期只存 pending_notice、第一次业务 entry 补发"规避，
> 因此**不写成"钩子里能 push"的用例**，而是写成 **S11：钩子期间零 push、零 upload**。
> G7（`AsstStop` 打断卡死）已由 F22 否掉，**不写这类用例**（必然假失败）。

### 7.2 开闸前我需要的一句话确认

主理人已明确"现在不要写可执行用例，等用户确认骨架后开闸"。
骨架即本文档 §3（105 条用例）+ §9（105 个函数名）+ §4（51 条变异）。
**确认后**我按 **先跑 M 矩阵 → 再写新用例** 的顺序推进（顺序反过来会得到一堆"看起来有牙、其实没咬过"的测试）。
每组跑完贴 `_runner.py` 的 `通过 N/N` 原文输出。


### 7.3 缺口已补（主理人裁定后，用例数 99 → 104）

主理人的理由不是"覆盖率高一点"，而是——**只读白名单是这个插件的存在理由**：用户需求第一句就是
"初版绝不能执行 Fight/Infrast 等写操作，只开放感知"，而当时 99 条里**一条都没压它**；
这是全项目唯一一处"错了会越过安全边界"的地方，其余都是"错了会难用"。

| 已补 | 编号 | 关键断言（比"抛了异常"更重要的那一半） |
|---|---|---|
| 写任务类型一律拒 | **W26** | 7 种写任务全 `TaskNotAllowed`，**且 fake lib 调用计数为 0**（含 `AsstAppendTask`/`AsstStart`）——防"先点进去再拒绝" |
| `Debug` 只放行 `ocr` | **W27** | `mode="ocr"` 放行（计数器 ≥1）；其它 mode 拒 |
| 安全拒绝不是故障 | **S12** | 文案**不含"失败/错误/异常"**；`ai_behavior="respond"`；`visibility=("chat",)`；不记 error 级审计 |
| 魔数不符 + 原始字节落盘 | **A15** | 抛 `CaptureFormatUnexpected`、原始字节**逐字节落盘**、路径进文案、**不得二次编码兜底** |
| 自动化入口不可用 | **S13** | `execute_automation` 必抛 `NotImplementedError` 且**零副作用**（push/upload/run_task 全 0，`last_error` 不动） |

**`knowledge.py` / `logic.py` 仍然不写用例，理由如下（不是偷懒）**：`logic.py` 初版只有一个数据类 +
一个 `NotImplementedError` 占位，`knowledge.py` 是静态表——给它们写用例只能测"常量是不是常量"，收益接近零。
**但那个 `NotImplementedError` 必须有用例（即 S13）**，因为它是未来自动化的入口：将来有人把它实现成真能执行，
S13 会立刻红。这条比给静态表写 20 条断言有用。

### 7.4 本轮三处改判（已收口 / 一处待架构）

1. **`EngineNotReady.progress` —— 主理人已定，不用标 `待验`**：`EngineNotReady(state: str, required: Sequence[str], progress: int | None = None)`。
   R18 直接断 `exc.state == "downloading"` 且 `exc.progress in (None, 0..100)`；
   **不断 `progress is not None`**——刚起任务时确实可能还没有进度值，断非空会造出偶发红。
2. **"核心库在根目录"不是退化行为**：架构 A11 曾把它记作已知退化；安全 v1.2 实核真实包结构是
   **10307 条目、顶层没有 `MAA/` 包裹层、`resource/`/`externals/` 与根目录散文件并列**，
   所以**根目录就是正常布局**。P10 的主用例（`core_lib == dest/MaaCore.dll`）与 P12 的 `bin/` 兜底变体按 v1.2 写。
   `MaaCore.dll` 的精确位置在真实包里**仍未定**（安全标 `TODO_VERIFY`）→ P10/P12 走 `bin/` 与根目录两种布局，
   **不假设某一种**。
3. **待架构一句（不阻塞）**：安全 v1.2 强化 C17 要求"取消后 `kind == "cancelled"`"——**已解决**，架构加了 `DownloadFailure.CANCELLED`（8 档），P25 的 `kind == "cancelled"` 不再退回 `待验`。同时确认：取消时 `state` 落 **`ABSENT`**、文案"已暂停喵"、`priority=2`、不计失败计数。

4. **`Recruit` 不进只读白名单——已定稿：排除，不再变**：
   - 定稿 `READONLY_TASK_TYPES = {Screencap, OperBox, Depot, Debug}`（4 项，定义在 `maa/adapter.py`）；`Recruit` 进"必拒"表（W26 的 `_WRITE_TASKS`）。
   - 实证理由：公开招募会**消耗招聘许可、占用招募位**，与 `Fight` 同类，不是"可能只读"。
   - 架构 §3.3 常量里那个 `Recruit` 是笔误（主理人已让架构改文档，**代码不动**）。判据不留活口，后来者不必再问一遍。


### 7.5 R 组三条红的闭环记录（**曾红，已修，用例未改**）

> 现状：**三条已全部转绿，用例一字未改**（云雨按下面的最小修法改了 `maa/`）。留档的意义是
> 记住"红的时候我们没改测试"。复核命令：`python tests/test_contract.py --only R` → 19/19。

| 用例 | 断言原文（节选） | 根因 | 最小修法 |
|---|---|---|---|
| R09 | `NO_SPACE 的 detail 必须带真实探测到的容量缺口`，实际 `detail='[Errno 28] No space left on device'` | `provision.py` 的 `except OSError` 分支用 `detail=str(exc)` 直传，**没有**探测容量；`errors.py:144` 的枚举注释写着「文案要带真实缺口」 | `errno == ENOSPC` 时用 `shutil.disk_usage(target_dir.parent)` 算出缺口 MB 拼进 `detail`；**探测失败就退化成不带数字的句子，不许编** |
| R14 | `purge 之后归档还在` | `MaaRuntime.purge()` 只删 `engine_root` + `.part`/`.lock`；**已校验通过的归档留在 `cache_dir`** | `_purge()` 里补一句 `archive.unlink(missing_ok=True)`（占盘约 2 倍的另一半就在这里） |
| R16 | `DOWNLOADING→FAILED 出现在迁移表里` | `_TRANSITIONS[DOWNLOADING]` 含 `FAILED`；而 §3.3 R16 定稿是"`DOWNLOADING→FAILED` 不存在——失败也回 `ABSENT`" | 从 `frozenset` 里删掉 `FAILED` 一个词；`_set_state(FAILED)` 的唯一调用点在布局歧义与 `_load_engine`（源状态 `ABSENT`/`UNPACKED`），**删掉不影响现有路径** |

> 当时坚持不改绿的理由（留档）：R09 改绿等于允许"磁盘满"没有缺口数字；R14 改绿等于允许 `--clean` 名不副实；R16 改绿等于允许下载失败把用户永久钉死在 FAILED。

## 8. 事实修正的落地结果（主理人裁定后）

| # | 原表述 | 裁定 | 落地位置 |
|---|---|---|---|
| 1 | "Connected 后 `ResolutionGot`/`UuidGot` 可能延迟到达" | **采纳并加严**：`ResolutionError` 是真实分支且旧代码完全没处理 | §1 **F20**（附源码行）+ A05（断言 `resolution.status is FAILED`、`width is None`（不是 0）、`Resolution(KNOWN,None,None)` 构造必炸、状态页不得谎报"已获取"） |
| 2 | "Connected 先到 → 设计不能提前 return" | **限缩**：设计**不建在顺序上**。终态用 `AsyncCallInfo(Connect).ret`；三个事件各自到达各自记录，缺谁记 `UNKNOWN`，绝不用默认值填 | A03（`Connected` 先到）、A04（完全颠倒）两条断言**结果逐字段相等**；A06 保留"`ret=True` 不能推出宽高已回填" |
| 3 | "空句柄" 旧文案归因为"MaaCore 内部初始化失败" | **采纳**：`AsstCreateEx` 返回 null 的唯一条件是 `!inited()`，即资源未加载 | §1 F7 + **W15**（断言 `InstanceCreateFailed(resource_loaded=False)`、文案指向 resource、`destroy_instance` 未被调用） |
| 4 | 我建议"wrapper 不要暴露 `AsstGetImageBgr`" | **改判**：暴露但给出路——wrapper 保留该方法，docstring 标"仅本地调试，禁止上行"；**service 层不得 import 它** | §1 F3 保留；**X03 新增静态断言**：扫 `companion/service.py` 的 `Name`/`Attribute` 节点，全文不得出现 `get_image_bgr` |
| 5 | `AsstBool` 是 `uint8_t` | **采纳并加用例** | §1 F16 + **W22**（fake 返回 `0x80`/`0xFF` 不得抛 `ValueError`，`argtypes` 用 `ctypes.c_ubyte`） |
| 6 | 我给的沙盒入口 `python -m sandbox.cli run-entry` | **我核错了，已作废**；以 `scripts/sandbox.py` + `doctor/prepare/install/plugin-add/serve` 为准 | §6.4 已更正 |
| 7 | 我猜的签名（`MaaWrapper`/`ReleaseSpec`/`EnginePaths`/6 档 `stage`/`EngineVersionMismatch`/`EngineIOError`） | **全部作废**，以架构 §3 与本文档 §1.3 为准 | §1.3 整节；§3.1–§3.4 用例按权威签名改写 |
| 8 | `UnpackFailure` 缺"写盘失败"档 | **采纳 (a) 并加细**：拆 `NO_SPACE` + `WRITE_ERROR` 两档 | §1.2.2；R09 两变体；S04 分档断言；M46 |
| 9 | `bootstrap(allow_download=False)` | **主理人否掉**："首次启动自动下载"是用户明确要求 | R12/R16 期望整体反转；M28 改成"不起后台任务" |
| 10 | "runtime 负责把 provision 异常翻译成 18 条之一" | **删除该表述**（裁决 B：不养两套词汇表） | §1.2.1；R17 改 AST 不变量；M42 改成"provision 抛非四类异常" |
| 11 | 我申请"把 R/P 组拆出 `test_runtime.py` / `test_provision.py`" | **不拆**：用户给的分层是两个文件，那是**结构规范**不是偏好；用例绝大多数是表驱动，1800 行不难读；真疼了再拆，那时是**重构** | §5.2 改记成"已知债务"（不写"偏离申请"） |
| 12 | 我把 `EngineNotReady` 的 `progress` 标成 `待验` | **主理人定版**：`EngineNotReady(state, required, progress: int \| None = None)`，`progress` 是**可选**字段 | R18 直接断 `exc.state == "downloading"` 且 `exc.progress in (None, 0..100)`；**不断 `progress is not None`** |
| 13 | 变异 M42 原名写"抛裸 `OSError`" | **改名**：只写"抛裸 `RuntimeError`"。`OSError` 有合法来源（磁盘满时 `open()` 自己抛，是被捕获后包装的源头），断"不许出现 OSError"会**误伤** | §4 的 M42 已改 |

补充两条由架构核实、会改变实现口径的事实（已进 §1）：

- **F21** `AsyncCallInfo` 自带 `async_call_id` → adapter 按 id 精确派发，**不再**需要"扫队列 + 谓词猜本次调用"。
  这条直接简化了 A14 的写法：不需要构造模糊的陈旧事件，只需回一个**旧 id** 的事件，断言它被丢弃。
- **F22** `AsstStop` **不打断** `call_proc` 中正在执行的项 → **不写**"`AsstStop` 能解除截图卡死"的用例（必然假失败）。
  截图卡死的兜底是 `destroy()` 的超时放弃策略（架构 §7.1 已接受该有界泄漏）。

另外三处**顺带纠正我自己的写法**（对齐架构后改的，不影响结论）：

1. `ai_behavior` **不是**统一 `respond`。§3.5.1 里只有 4 个码是 `respond`
   （`AdbAddressMissing`/`AdbConnectTimeout`/`AdbDisconnected`/`TaskNotAllowed`），其余 14 个是 `read`；
   `AdbDisconnected` 的 `visibility` 还额外含 `"hud"`。→ S04 改成**逐码按表断言**。
2. `submitted=False` 时**不改文案**（我原先写成"降级文案"），只带 `push_failure_reason`。→ S06 已改。
3. 异常名全部对齐 `maa/errors.py` 的 18 条（原先自拟的 `EngineResourceLoadError`/`EngineInstanceError`/
   `EngineArchiveError`/`EngineVersionMismatch`/`EngineIOError` 都不存在）：分别落到
   `ResourceMissing` / `ResourceLoadRejected` / `InstanceCreateFailed` / `EngineUnpackFailed` /
   `EngineLoadFailed(symbol=...)` / `EngineDownloadFailed`。


## 9. 附录：105 条用例的完整函数名清单

命名规则：`test_<编号小写>_<slug>`。左侧编号 ↔ 右侧函数名是**机器可校验的一一对应**
（§5.3 的 `check_registry` 第 4 条规则 + X01 的文档交叉校验）。
落地时可直接照这份清单生成 99 个带 docstring 的函数骨架，每个函数体先写
`raise AssertionError("未实现")`（见 §6 的 G2 硬规矩）。

| 编号 | 函数名 | 文件 |
|---|---|---|
| W01 | `test_w01_load_resource_receives_parent_dir` | test_contract.py |
| W02 | `test_w02_load_resource_rejects_resource_dir_itself` | test_contract.py |
| W03 | `test_w03_image_bytes_pass_through_exact` | test_contract.py |
| W04 | `test_w04_high_bit_bytes_survive_roundtrip` | test_contract.py |
| W05 | `test_w05_sentinel_inside_buffer_range_is_not_success` | test_contract.py |
| W06 | `test_w06_short_buffer_is_retryable_not_fatal` | test_contract.py |
| W07 | `test_w07_buffer_doubles_and_succeeds` | test_contract.py |
| W08 | `test_w08_retry_is_bounded` | test_contract.py |
| W09 | `test_w09_buffer_never_exceeds_hard_cap` | test_contract.py |
| W10 | `test_w10_zero_length_is_not_empty_success` | test_contract.py |
| W11 | `test_w11_null_handle_is_not_reported_as_buffer_problem` | test_contract.py |
| W12 | `test_w12_null_size_matches_library_symbol` | test_contract.py |
| W13 | `test_w13_missing_null_size_symbol_falls_back` | test_contract.py |
| W14 | `test_w14_argtypes_and_restypes_match_header` | test_contract.py |
| W15 | `test_w15_null_handle_create_points_at_resource` | test_contract.py |
| W16 | `test_w16_async_connect_invalid_id_is_rejected` | test_contract.py |
| W17 | `test_w17_connection_extras_bad_json_does_not_raise` | test_contract.py |
| W18 | `test_w18_destroy_null_handle_is_idempotent` | test_contract.py |
| W19 | `test_w19_exact_fit_buffer_is_success` | test_contract.py |
| W20 | `test_w20_return_longer_than_buffer_is_rejected` | test_contract.py |
| W22 | `test_w22_asst_bool_high_byte_does_not_raise` | test_contract.py |
| W23 | `test_w23_bare_name_load_fails_absolute_path_succeeds` | test_contract.py |
| W24 | `test_w24_dll_directory_handle_lifetime` | test_contract.py |
| W25 | `test_w25_load_failure_reports_oserror_vs_symbol` | test_contract.py |
| W26 | `test_w26_write_task_types_rejected_before_any_ctypes_call` | test_contract.py |
| W27 | `test_w27_debug_task_allowed_only_in_ocr_mode` | test_contract.py |
| A01 | `test_a01_callback_from_c_thread_enqueues` | test_contract.py |
| A02 | `test_a02_wakeup_is_event_driven` | test_contract.py |
| A03 | `test_a03_connected_does_not_return_early` | test_contract.py |
| A04 | `test_a04_reordered_events_still_backfill` | test_contract.py |
| A05 | `test_a05_resolution_error_does_not_poison_size` | test_contract.py |
| A06 | `test_a06_async_call_ret_true_counts_as_connected` | test_contract.py |
| A07 | `test_a07_async_call_ret_false_fails_fast` | test_contract.py |
| A08 | `test_a08_connect_failed_carries_why_and_address` | test_contract.py |
| A09 | `test_a09_empty_address_never_invents_port` | test_contract.py |
| A10 | `test_a10_cancel_leaves_no_orphan_worker` | test_contract.py |
| A11 | `test_a11_late_error_does_not_overwrite_timeout` | test_contract.py |
| A12 | `test_a12_restart_does_not_leak_callbacks` | test_contract.py |
| A13 | `test_a13_malformed_payload_is_swallowed` | test_contract.py |
| A14 | `test_a14_stale_events_do_not_satisfy_next_task` | test_contract.py |
| A15 | `test_a15_non_png_magic_keeps_raw_bytes_and_never_reencodes` | test_contract.py |
| R01 | `test_r01_hash_mismatch_deletes_file` | test_contract.py |
| R02 | `test_r02_partial_download_is_not_usable` | test_contract.py |
| R03 | `test_r03_zip_slip_is_rejected` | test_contract.py |
| R04 | `test_r04_version_mismatch_blocks_load` | test_contract.py |
| R05 | `test_r05_concurrent_bootstrap_downloads_once` | test_contract.py |
| R06 | `test_r06_failed_bootstrap_can_retry` | test_contract.py |
| R07 | `test_r07_artifacts_land_under_data_path_maa` | test_contract.py |
| R08 | `test_r08_checksum_is_streamed_in_chunks` | test_contract.py |
| R09 | `test_r09_io_error_keeps_root_clean` | test_contract.py |
| R10 | `test_r10_extraction_is_atomic` | test_contract.py |
| R11 | `test_r11_missing_digest_is_rejected` | test_contract.py |
| R12 | `test_r12_bootstrap_returns_downloading_and_dedupes` | test_contract.py |
| R13 | `test_r13_download_verify_unpack_chain_against_local_server` | test_contract.py |
| R14 | `test_r14_purge_removes_unpacked_tree` | test_contract.py |
| R15 | `test_r15_layout_resolves_three_install_shapes` | test_contract.py |
| R16 | `test_r16_forbidden_state_transitions_raise` | test_contract.py |
| R17 | `test_r17_provision_raises_only_known_errors` | test_contract.py |
| R18 | `test_r18_capture_fails_fast_while_downloading` | test_contract.py |
| R19 | `test_r19_user_dir_before_load_resource` | test_contract.py |
| S01 | `test_s01_each_exception_has_unique_copy` | test_service.py |
| S02 | `test_s02_copy_gives_actionable_next_step` | test_service.py |
| S03 | `test_s03_unknown_exception_falls_back_cleanly` | test_service.py |
| S04 | `test_s04_push_message_shape` | test_service.py |
| S05 | `test_s05_image_is_image_part_not_path` | test_service.py |
| S06 | `test_s06_submitted_false_degrades_once` | test_service.py |
| S07 | `test_s07_repeated_failure_is_throttled` | test_service.py |
| S08 | `test_s08_copy_hides_raw_native_values` | test_service.py |
| S09 | `test_s09_push_failure_does_not_mask_error` | test_service.py |
| S10 | `test_s10_copy_keeps_persona_and_action` | test_service.py |
| S11 | `test_s11_lifecycle_never_pushes` | test_service.py |
| S12 | `test_s12_task_not_allowed_copy_has_no_failure_wording` | test_service.py |
| S13 | `test_s13_execute_automation_raises_not_implemented_without_side_effects` | test_service.py |
| X01 | `test_x01_case_ids_match_plan_doc` | test_contract.py |
| X02 | `test_x02_no_test_function_left_unregistered` | test_contract.py |
| X03 | `test_x03_service_import_direction` | test_contract.py |
| X04 | `test_x04_adapter_state_literals_are_real_engine_states` | test_contract.py |
| P01 | `test_p01_zip_slip_variants_leave_no_files_outside_dest` | test_contract.py |
| P02 | `test_p02_symlink_member_is_rejected` | test_contract.py |
| P03 | `test_p03_encrypted_member_is_rejected` | test_contract.py |
| P04 | `test_p04_zip_bomb_ratio_is_capped` | test_contract.py |
| P05 | `test_p05_entry_count_limit` | test_contract.py |
| P06 | `test_p06_declared_size_drift_is_caught` | test_contract.py |
| P07 | `test_p07_layout_rejects_missing_or_duplicate_core` | test_contract.py |
| P08 | `test_p08_normal_archive_contents_match` | test_contract.py |
| P09 | `test_p09_existing_dest_is_rejected_not_merged` | test_contract.py |
| P10 | `test_p10_whitelist_selected_and_skipped_counts` | test_contract.py |
| P11 | `test_p11_whitelist_does_not_replace_path_check` | test_contract.py |
| P12 | `test_p12_core_library_location_rules` | test_contract.py |
| P13 | `test_p13_verify_sha256_truth_table` | test_contract.py |
| P14 | `test_p14_verify_sha256_streams_in_chunks` | test_contract.py |
| P15 | `test_p15_download_success_leaves_no_part_or_lock` | test_contract.py |
| P16 | `test_p16_hash_mismatch_deletes_dest_and_part` | test_contract.py |
| P17 | `test_p17_short_long_and_html_errors_clean_up` | test_contract.py |
| P18 | `test_p18_downgrade_redirect_is_rejected` | test_contract.py |
| P19 | `test_p19_disk_full_and_keyboard_interrupt_clean_up` | test_contract.py |
| P20 | `test_p20_concurrent_download_is_serialized` | test_contract.py |
| P21 | `test_p21_stale_lock_self_heals` | test_contract.py |
| P22 | `test_p22_reuse_requires_matching_hash` | test_contract.py |
| P23 | `test_p23_invalid_url_or_hash_fails_before_any_request` | test_contract.py |
| P24 | `test_p24_env_proxy_is_ignored_by_default` | test_contract.py |
| P25 | `test_p25_cancel_leaves_no_residue` | test_contract.py |
| P26 | `test_p26_progress_callback_cannot_break_cleanup` | test_contract.py |
| P27 | `test_p27_known_releases_constants_are_pinned` | test_contract.py |
| P28 | `test_p28_async_cancel_stops_worker_before_return` | test_contract.py |