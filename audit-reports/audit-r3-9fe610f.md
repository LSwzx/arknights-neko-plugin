# 第三轮只读审计报告 · arknights_neko_plugin

- 审计基线：`9fe610f`（`fix: make capture actually inline, keep timeouts inside host budget, survive stuck ADB`）
- 工作区状态：干净（`git status --short` 空）
- 审计模式：**只读**。本轮不改任何代码、不产生 commit。
- 参与专家：`sec-r3`（安全）、`arch-r3`（架构）、`review-r3`（代码评审）、`simp-r3`（简化）、`tdd-r3`（测试）、`sbx-r3`（沙箱运行时）

## 0. 基线与取证口径

本轮所有结论都以 **md5** 定位文件，不认行号（上一轮出现过同一文件一夜之间 10+ 个版本的漂移）。

| 文件 | 行数 | md5 |
| --- | --- | --- |
| `__init__.py` | 407 | `94ae032d50022aea83bd681cd375924f` |
| `maa_engine.py` | 685 | `cb2b930b600866025187cb483475f824` |
| `maa_adapter.py` | 232 | `967721d142ce6123f17b1d33c8828c5d` |
| `knowledge.py` | 163 | `e7b64368f277899366487138ff706682` |
| `tests/test_contract.py` | 972 | `9fecf3f6e618493f8780b51c409e4048` |

基线复跑（本轮现场执行，非引用上一轮结论）：

```
python -B tests/contract_check.py   →  32 PASS / 0 FAIL
grep -c "^def test_" tests/test_contract.py  →  32
```

即：**基线是绿的**。后文所有"会静默错"的判断，含义都是"现有 32 条用例抓不到"，而不是"现在就是坏的"。

**本轮的取证纪律**（下一轮沿用）：

1. 结论用 md5 定位，不用行号——上一轮同一个文件一夜之间出现 10+ 个版本，行号引用有 4 条在发出时已过期；
2. 每个结论标明它是 FACT（我或某位专家亲手跑出来的）还是 INFERENCE（读码推断）还是 needs_validation（需要真库/真机）；
3. **结论必须能被反例检验**：本轮就有一句"reload 也不清 `last_error`"被反例推翻——reload 是换进程（pid 实测），新进程 `__init__` 会重置。写"永久"这种绝对词之前，先问"什么动作能重置它"；
4. 同一现象有多个专家报时，只保留机理最准的那一版（本轮"7 个入口全 `is_ok`"被三人分别报成"静默 Ok""降级即 Ok 契约""错得最像对"，最后合并成 R1 一条）。

---

## 1. lead 亲自复核的事实（不依赖任何专家，可直接引用的硬结论）

这一节是我在派单之外自己动手 grep/读代码/跑测试确认的，用来给专家结论做地板。凡专家结论与本节冲突，以本节为准。

### L1【中】写侧与读侧的 store key 是两份独立字面量

```
__init__.py:36   STORE_OPERATORS = "operators_cache"
__init__.py:37   STORE_DEPOT     = "depot_cache"
__init__.py:397  operators = await self._store_get(STORE_OPERATORS, None)   # 读
__init__.py:398  depot     = await self._store_get(STORE_DEPOT, None)       # 读
maa_adapter.py:147  await self._persist("operators_cache", data)            # 写
maa_adapter.py:170  await self._persist("depot_cache", data)                # 写
```

读侧用常量、写侧用字面量。改任一侧（重命名 key、加前缀做命名空间）都不会报错，只会让 `arknights_memory` 静默读空。32 条用例里没有一条跨 adapter/入口两侧对 key 做断言，因此**抓不到**。
修法：常量下沉到 `maa_adapter`，`__init__` 反向 import；补 1 条断言（写一次 → 用 `STORE_OPERATORS` 读回）。

### L2【中】`ocr()` 仍有一条"静默返回空"的分支，且它错误得最像对

```
maa_adapter.py:185  # 不能返回 []：那等于告诉上层"屏幕上没有文字"，于是 analyze_stage 会基于
maa_adapter.py:187  raise MaaEngineError(self.engine.last_error or "截图失败，无法进行 OCR")
maa_adapter.py:207  except Exception as exc:            # ← Debug/ocr 任务本身失败
maa_adapter.py:208      logger.warning(...)
maa_adapter.py:209      return []                        # ← 仍然吞掉
```

上一轮只修了「截图失败」这一条路径（改成抛错），但「截图成功、Debug/ocr 任务失败」这条路径依旧 `return []`。此时 `analyze_stage` 拿到空字符串，`analyze_stage_text("")` 会返回"暂未识别到明确关卡编号"——用户看到的是一个**格式正确的错误答案**，而不是错误。
影响面：OCR 依赖 MAA v6.x 的 `Debug/ocr` 模式，老版本库上会永久静默失效且完全不可观测。

### L3【中】`stop()` 清标记的动作，把"孤儿仍在飞"这一事实抹掉了

三个字段的真实语义（`maa_engine.py:147-154` 注释已写明）：

| 字段 | 语义 |
| --- | --- |
| `_screencap_stuck` | 存在一个"是否已返回尚不确定"的原生调用 |
| `_screencap_inflight` | 当前有调用在跑（只挡并发，`finally` 里就清） |
| `_screencap_orphan` | 那个孤儿 Task，唯一可信的恢复判据 |

`stop()` 的结尾（`maa_engine.py:396-398`）无条件把三个字段全清零；`start()` 开头（`334-335`）又清一次。而 `_get_image` 是在**线程内部**才读 `self._handle`（`maa_engine.py:610`）：

```python
got = self._lib.AsstGetImage(self._handle, buf, buffer_size)
```

于是存在这样一条时序：截图超时 → `stuck=True`、`orphan=task1` → 用户 reload → `stop()` 跳过 `AsstDestroy`（对的）但把 `stuck/orphan` 清成 False/None → `start()` 再清一次 → `AsstCreateEx` 建新实例 → 孤儿线程此刻才真正进入 C，读到的是 `self._handle`（新实例）与按**旧分辨率**算出的 `buffer_size`。

结论要说准确，这里有**两件事**，而且更容易触发的是第一件：

1. **`stop()` 只判 `_screencap_stuck`，不判"是否正有截图在飞"。** 一次**完全正常、没有超时**的截图（`inflight=True`、`stuck=False`）撞上 shutdown/reload，`AsstDestroy` 会照常执行 —— 实例被 free 的瞬间，`_get_image` 那个线程正在里面读图。这条不需要任何超时就能触发，是主路径。
2. **`stop()`/`start()` 把 `orphan` 清得比事实早。** 超时留下孤儿后，`stop()` 清了 `stuck/orphan`、`start()` 又清一次，于是"孤儿的线程还活着"这个事实在插件里没有任何记录；随后 `_get_image` 读到的是 `self._handle`（新实例或 None）。

sbx-r3 用 fake lib 把第 1 条跑成了**可复现时间线**（见 §6.4）：`AsstDestroy(inflight=True)` 之后 0.3s 才 `AsstGetImage::enter handle=None`（向 C 传 NULL），并且 destroy 比读图返回早 0.7s（实例已 free 仍在读）。

修法要点：`stop()` 的条件要从"没卡死"扩到"没有在飞的原生调用且孤儿已返回"；`_get_image` 改成调用点快照 handle 并用 `functools.partial` 传参，杜绝线程内重读 `self._handle`；`start()` 在孤儿未 done 时拒绝重建实例。

### L4【低，但零成本】文档里的用例数已全线过期

```
README.md:41            # 21 条用例本体
tests/contract_check.py:4   用例本体在 tests/test_contract.py（21 条）
tests/contract_check.py:16  pytest 直接收集 test_contract.py 里的 21 个 test_*
tests/test_smoke.py:7   行为契约（…等 21 条）
```

实测 `grep -c "^def test_"` = **32**。四处（不是三处）需要同步。

---

## 2. 专家原始结论 · simp-r3（简化轴）

**净可删仅 7 行**，全部是"删了不会错、留着也不痛"的量级：

| 位置 | 可删 | 行数 |
| --- | --- | --- |
| `ocr` 内联 PNG 编码改复用 `save_png` | 重复编码逻辑 | −2 |
| `_READONLY_TASKS` 别名 | 冗余别名 | −2 |
| `ocr` 形参 `raw` | 已无调用方传值 | −2 |
| `screencap` 形参 `buffer_size` | 恒为 None | −1 |

另有 2 处"0 行收敛"：`arknights_capture` 里恒真的 `if self._engine`；`capture`/`save_png` 的 `dest` 参数。

**唯一"会静默错"的项**就是上面 L1（缓存键双份定义），我独立复核成立。

**双轨 `main()` 的判断：不要删脚本轨。** 本机默认 `python` 没有 pytest（本轮现场验证：`No module named pytest`），`tests/contract_check.py` 是唯一的本地全量入口，pytest 轨只在 CI/uv 环境存在。建议：把 32 条注册表提为模块级 `TESTS` 列表，pytest 侧加 1 条断言覆盖「注册表条数 == 收集到的 test_ 数量」，把"漏登记即假绿"堵死。

**明确判定"不要删"**（防止下一轮有人当成冗余）：只读双层校验、`READONLY_DEBUG_MODES`、每个失败分支写 `last_error`、`_degraded` + `_status_label` 单源、store 三层防御、`AsstMsg` 里 14 个零引用成员（它们是协议常量，不是死代码）。

**复杂度热点 TOP3**：`screencap`（74 行 / 12 分支）、`_connect`（47 行 / 17 分支，**无任何直接单测**）、`on_startup`（48 行 / 11 分支）。

---

## 3. 专家原始结论 · arch-r3（架构轴）

### 3.1 状态机：8 个字段、3 个判定函数、0 个单一真值源

`_ready()` / `_status_label()` / `arknights_engine_status` / `engine.status()` 各自取字段的不同子集，互相不覆盖对方字段。已实测到的自相矛盾组合：

| 组合 | 可达路径 | 表现 | 谁处理 |
| --- | --- | --- | --- |
| `connected=T` + `screencap_stuck=T` | ADB 掉线后首张截图超时 | `/status` 报 `connected`、`degraded=false`，但截图必失败 | 无人 |
| `loaded=T` + `_degraded=T` | 同实例 stop→start | `_ready()`=T 而标签说 degraded | 无人：两个判定各漏一半字段 |
| `engine=None` | — | `_status_label()`→`"degraded"`，`arknights_engine_status`→`"unavailable"` | 同条件两个值 |

单向边：`_bound` F→T 永不回、`_degraded` F→T 永不回、`connected` 只在 init/start/stop 赋值（运行时无失效路径，赋值点已穷举）。

### 3.2 接缝：MAA 语义散在三层，换后端 ≥14 触点

任务名+白名单策略（策略在 engine、调用在 adapter，4 处）、回调 `what`（adapter 3 + engine 6）、`details` 结构（adapter 5 + engine 3）、截图字节→分辨率（adapter 猜 16:9 回退 engine 字段）、编码+内联上限（编码在 adapter、上限与会话信封在入口）。

入口层直接读 engine 内部字段共 **6 处**（`.loaded` / `.connected` / `.last_error` / `encode_png` / `save_png` / `status()` 扁平键）。arch-r3 的判断：UI→adapter 的边没问题，UI→engine 内部字段这条边该消失。

### 3.3 降级恢复：7 类形态里 4 类只能重载，插件自身无恢复入口

`_degraded`（startup 抛错）、`manual`（空地址/连接失败）、`screencap_stuck`（除 orphan 真返回外）、`_bound` 缺符号（同进程内不可逆）——四条都没有恢复入口。唯一可逆的是任务 45s 超时和 store 写失败。

arch-r3 明确建议：**先做 `_health()`，再谈 reconnect**；直接加 reconnect 会放大状态机复杂度。

### 3.4 上报的发现（已并入第 1 节复核的部分不再重复）

| 严重度 | 问题 | 与我的复核关系 |
| --- | --- | --- |
| 高 | `analyze_stage` 的"设备必需性"判在入口、实现无条件 `await self.ocr()`，两层互相矛盾 | 与 L2 同源，L2 是它的必然后果 |
| 高 | `AsstMsg.TaskChainError / SubTaskError / AllTasksCompleted / InternalError` 全仓零消费方 | 新增，采信。MAA 侧报错一律退化成"烧满 45s 后超时" |
| 高 | `_ready()` / `_status_label()` / `arknights_engine_status` 三处各拼状态 | 新增，采信（与 3.1 同一条） |
| 中高 | `status()` 的 `connected` 无运行时失效 | 新增，采信 |
| 中 | `start()` 复位 3 项、`stop()` 复位 5 项的手工清单 | 与 L3 同源，L3 给出了更准确的机理 |
| 中 | `_neko_adb_missing` 是唯一 `Ok(ok=False)`，其余全 `Err(SdkError)` | 新增，采信 |
| 中 | 默认 `adb_address=""` 时首次安装必然 manual，且文案不区分"未配置/连不上" | 新增，采信 |
| 低 | `_load_and_bind` 文案要求"重启宿主"，实际 reload 插件子进程即可 | 采信 |

### 3.5 阈值（arch-r3 给的是数字，不是形容词）

现状：8 个状态字段 / 3 个判定函数 / 0 个单一真值源 / 0 个健康计数器 / `_ready()` 调用点 4 个。
**触发线：`_ready()` 调用点 > 6 或状态字段 > 10 时，一次改动同时碰到两处组合判断的概率过半**——按当前结构只剩 1~2 个新入口的余量。

今天就能观测、不需要新指标的失效信号：
- a) `/status` 同时出现 `connected=true` 与 `screencap_stuck=true`（此刻就可能出现）；
- b) `last_error` 非空但 `status="connected"`；
- c) `analyze_stage` 返回 Ok 但 `stage=""` 且 `settlement_likely=false`（必来自 L2 的静默 `[]`）；
- d) `_events` 队列 `qsize()` 单调增长（当前无上限、无告警）；
- e) reload 后线程数只增不减（每次 stuck +1）——**当前无计数器，属不可观测泄漏**，需先加才能观测。

### 3.6 待裁决：两份报告对 `analyze_stage` 降级行为的描述不一致

arch-r3 实测降级态返回 `Err("关卡识别失败：截图失败：引擎尚未建立实例")`（即 `ocr()` 在截图阶段抛错，`185-187` 生效）；而"返回 Ok 但 stage 为空"只在 **Debug/ocr 任务本身失败**（`207-209`）时出现。

两者不矛盾，是两条不同路径：**截图失败 = 可见的 Err；OCR 任务失败 = 静默的假成功**。合并结论以 L2 为准，arch-r3 的失效信号 c) 需要限定在后者。

---

## 4. 专家原始结论 · review-r3（评审轴）

**总评：不通过 —— 3 阻塞 / 5 建议 / 6 小改进。** 它的方法论值得单独记一笔：本轮三个"失败 vs 空结果"折叠点**全部实测复现**，不是读代码猜的。

### 4.1 三条阻塞项

**B1【阻塞】`sync_depot` / `sync_operators` 用空缓存覆盖正确缓存，并且返回成功**

```python
# sync_depot
try:
    depot = json.loads(data_json) if isinstance(data_json, str) else dict(data_json or {})
except Exception:
    depot = {}                       # ← 解析失败当空仓
data = {"depot": depot, "count": len(depot)}
await self._persist("depot_cache", data)   # ← 照写
```

`sync_operators` 侧同构：`details.get("own_opers", [])` 缺键就给空列表。

这和上一轮刚修好的"超时不得写空缓存"是**同一条根因的另一个入口**：上一轮堵了 `result is None`，没堵"result 有、但 payload 形状不对"。review-r3 实测把 `data="{broken"` 喂进去，返回 `{'depot': {}, 'count': 0}` 且 store 里确实写进了空缓存；`sync_operators` 缺 `own_opers` 时同样写入 `{'operators': [], 'count': 0}`。

危害排序里这是第一：它**销毁用户已有的正确数据**，而且回报 Ok，用户完全没有察觉的入口。

**B2【阻塞】`_encode_png` 会输出宿主打不开的 PNG，入口仍返回 Ok —— 我已独立复现**

`_infer_dimensions` 在推断失败时回退到 `engine.width/height`，而 `_encode_png` 按这个宽高逐行切 `rgb_bytes`，**不校验 `len(rgb_bytes) == width*height*3`**：

```python
raw = b"".join(b"\x00" + rgb_bytes[y*stride:(y+1)*stride] for y in range(height))
```

我的复现（1280×720 少 1 字节，即分辨率中途变化 / 缓冲被截断的最常见形态）：

```
pixels 921599 -> infer 1280 720   need 2764800  have 2764799  short 1
png len 2759   （照样产出了 PNG）
```

最后一条 scanline 缺 1 字节。review-r3 用 Pillow 12.3.0 解码得到 `OSError: image file is truncated`；本机无 Pillow，我只验证了"长度不足仍照样编码"这一半，机制成立。宿主侧会把解码失败的图**静默丢弃**，于是用户看到"截图成功但没图"。

触发面要说实话：**窄但真实**——需要 `len(raw) % 3 != 0`，或设备真实分辨率与 `engine.width/height` 不一致（拔插分辨率变化、ADB 重连后回填失败）。修法是一行长度校验，或按可用字节回退并补 padding，成本极低，所以值得当阻塞处理。

**B3【阻塞】OCR 失败与"屏幕上没字"不可区分** —— 与 L2 同一条，review-r3 独立复现：`ocr()` 抛错与 `ocr()` 超时两种情形下 `analyze_stage` 输出**同一句**"暂未识别到明确关卡编号…"。

### 4.2 五条建议项

| 级别 | 问题 | 关键点 |
| --- | --- | --- |
| 🟠 | `_StoreBridge.set/get` + `_store_set`/`_persist` 的 `except Exception` 是死防御 | SDK `PluginStore.set` **不抛异常**，失败返回 `Err(...)`（`_run_local_result` 内部已 catch+log），`unwrap_or(...)` 把 Err 吞成 default。所以那两处 `except` 永远打不到，缓存写失败 100% 静默、读失败被显示成"没有缓存" |
| 🟠 | `append_readonly_task` 的 `_task_lock` 无等待上限 | 第二个并发 `sync_*` 要先等前一个 45s + 自己 45s = 90s > 入口 60s 预算，用户看到的是不可读的 `DEADLINE_EXCEEDED`。建议 `wait_for(lock.acquire(), 3)` 快速失败并给可读文案 |
| 🟠 | `_neko_adb_missing` 一律甩锅 ADB | 缺库 / 资源加载失败 / ADB 未配 三种都渲染成"ADB 好像没连上"，而 `last_error` 里已经有可执行原因（"请放置到 native/ 目录"、"资源目录是否有效"）却没被引用。**首启最常见失败恰恰是没放 `MaaCore`/`resource`** |
| 🟠 | `AsstLoadResource` 没有内部超时 | 唯一上限是 `[plugin_runtime].timeout=30`；超时由宿主 `wait_for_startup` → `PluginLifecycleError("startup timed out")` 并 **abort 进程**，"所有失败收敛为 degraded"的承诺在这条路上不成立（`CancelledError` 也不会被 `except Exception` 抓到） |
| 🟠 | `knowledge.roguelike_advice` 对脏输入抛 TypeError | docstring 声明"对任意脏输入都不抛"，实测 `current_operators=[{"a":1}]` / `[["x"]]` → `TypeError: unhashable type`，入口无 try → 直接冒成 `PLUGIN_EXECUTION_FAILED`。LLM 按自然语义传对象数组就能打中 |

### 4.3 六条小改进（都便宜，建议顺手做）

1. **"手动录入"这套降级模式并不存在**：README 快速开始 §4、`__init__` 降级注释、`_neko_adb_missing`、`resource/README.md` 四处反复承诺"降级为手动录入 / 把干员仓库情况告诉我"，但 7 个入口里**没有任何手动写入口**（`memory` 只读缓存，`roguelike_advice.current_operators` 不落盘）。要么改文案，要么真加 `arknights_manual_record`。
2. `_connect` 的 `if event is None: return False` 不写 `last_error` → 用户看到上一次的陈旧原因。
3. `_connect` 的 `int(d.get("width", self.width))` 对非法值无防御，`int("1920.0")` 抛 `ValueError` 会让整个 `start()` 失败并降级。
4. 文档"21 条"过期（与 L4 同一条，review-r3 也独立抓到；README 目录树还漏了 `tests/test_smoke.py`）。
5. **内联图上限口径与宿主不一致**：宿主交付上限是 **500KiB base64**，超过会被重编码成 JPEG 并追加 `_image_warnings`（所以模型看到的常常不是 PNG）；宿主像素上限 `3840×2160`，而插件 clamp 到 `4096×4096`。2MiB 内联本身不会撞传输上限（控制面上行 ≈4.56MiB，这点是对的），但注释该注明"超过 500KiB 会被宿主重编码"。
6. `save_png` / `ocr` 用固定文件名 `capture.png` / `ocr.png`，连续两次会覆盖，前一次返回的 `path` 在用户打开前就失效。

### 4.4 对上一轮两条结论的独立确认

- `python -B tests/contract_check.py` → **32/32 PASS**；真 SDK 挂载副本下 `pytest` → **34 passed / 7.13s**。与我的复跑一致。
- 宿主 `_MAX_TOOL_IMAGE_B64_BYTES=2MiB` 用 `>` 判限，插件侧边界**是对的**（与 B8 用例 `test_inline_threshold_boundary_is_exact` 吻合）。

---

## 5. 专家原始结论 · tdd-r3（测试保真度轴）—— 本轮最强的一份

**一句话结论：现在的 32 条不是"覆盖了插件"，而是"覆盖了 stub 与 3 个算法函数"。**

### 5.1 变异实测：24 个变异，20 个全绿

tdd-r3 把仓库复制到 `%TEMP%` 再改（不碰工作区），打了 24 个只读变异：

```
24 mutants → 20 GREEN / 4 RED
被察觉的只有：M3 _ready 只判引擎非空、M16 内联阈值放宽 2 倍、
              M17 判限对象换成 raw、M24 启动失败不标降级
```

零区分力清单（括号内是"改坏了仍然 32/32 绿"的变异）：

| 用例 | 它实际约束了什么 | 变异证据 |
| --- | --- | --- |
| `test_roguelike_caps_three_advices` / `..._all_themes_nonempty` | 什么都没约束 | 删掉 `knowledge` 与 `analyze_stage_text` 两处 `[:3]` 仍全绿（M1/M2）——当前分支结构天然 ≤3，"≤3 条"这个契约不可证伪 |
| `test_all_seven_entries_callable` | 只断言 `is_ok` | 把 `sync_operators` / `sync_depot` / `analyze_stage` **三个函数体整体桩化**（M4/M5/M6）仍 32/32 —— 三个主力入口实现等于零约束 |
| `test_shutdown_idempotent` | 只断言"不抛错" | `on_shutdown` 完全不调 `engine.stop()`（M11）仍绿 |
| 降级即 Ok 一组 | 把失败渲染成成功 | `_store_set` no-op（M13）、`_store_get` 恒返回默认（M22）、`arknights_memory` 直接 `Ok({})`（M23）、`screencap` 失败改 Ok 降级（M10）、`save=True` 改 `Ok({})`（M9）全部绿 |
| `test_degraded_flag_has_single_source_of_truth` | 只证明"同源"，不证明"正确" | `_status_label` 恒返回 `"degraded"`（M21）仍绿 |
| `arknights_engine_status` 的 `engine is None` 分支 | 该分支一行未执行 | M14 改成 `is None and False` 绿 |
| `_get_image` / `AsstAsyncScreencap` | 上界与返回值 | 去掉 `got <= buffer_size` 上界（M15）、返回 0 当成功（M18）均绿 |

反过来，**质量最高的两条用例**也点出来了：`test_capture_inlines_small_png` + `test_inline_threshold_boundary_is_exact`（M17 判限换 raw 时这两条红）。`test_capture_oversize_falls_back_to_file` 单看没有区分力（拿 raw 判限也会判超大），它必须和那两条一起才有意义。

**本轮最硬的一条零区分力证据（注册面）：删掉 `@lifecycle` 装饰器，32/32 仍全绿。**

```
M25 删除 on_startup 的 @lifecycle 装饰器      → 通过 32/32
M26 删除 on_shutdown 的 @lifecycle 装饰器     → 通过 32/32
M27 startup 钩子 id 改错（startup → start）   → 通过 32/32
```

真机反证：`plugin/core/host.py:1322` 是 `startup_fn = lifecycle_events.get("startup")` —— **id 写错等于 startup 永不被调用，插件起不来**，而离线套件一声不响。根因两条，都很干净：

1. B1 用例是 `inst.on_startup()` **直接调方法**，绕过装饰器，所以装饰器在不在它都无感；
2. stub 侧 `collect_lifecycles()` 的产出**无人断言**，`id` 更没人核对。

这条比"逻辑分支删掉仍绿"更严重：它测的是**宿主认不认你这个钩子**（注册面），逻辑面再密也覆盖不到。修法成本极低：加一条用例断言 `collect_lifecycles()` 的 id 集合 == `{"startup", "shutdown"}`，改 id 即红。

**一条写法指导（M21 判别力对照）**：

```
正常实现   capture=Ok | status=connected | degraded=False | C17(派生断言)=True | 独立断言 status=='connected'=True  → 绿
M21 变异   capture=Ok | status=degraded  | degraded=True  | C17(派生断言)=True | 独立断言 status=='connected'=False → 红
```

结论：**C17 这种"由实现自己维持"的派生断言零判别力**（`degraded == (status != "connected")` 两边都是实现自己算的，实现错了它也跟着错）。新用例必须写独立期望值，例如「快照 `connected=True` 且本次截图成功 → `status == "connected"`」「快照 `connected=True` 但本次截图失败 → `status != "connected"`」——只有这两条会让 M21 变红。

**绑实现细节（重构即红、缺陷仍在）**：
- `test_callback_bridge_is_loop_agnostic` 断言 `not hasattr(eng, "_loop")` —— 负向私有属性名断言，改名即红，而"正确缓存 loop"的另一种写法照样抓不到；
- `test_debug_mode_whitelist` 用 `"拒绝" not in msg and "引擎未就绪" in msg` 判"通过了 mode 校验"——文案改通顺即红，且**没有断言"未发生原生调用"**（缺 negative 守卫）；
- `test_entry_ids_unique_and_method_named` 的 `len(ids) == 7` 是类扫描结果；真 SDK `collect_entries()` 实际返回 **12 个 key**（7 entry + startup + shutdown + 3 个 `__llm_tool__*`），注册表规模无人验证 —— 与 sbx-r3 实测的 12 项吻合；
- `tests/test_smoke.py::test_plugin_manifest_exists` 仍是文本字符串断言：把 `id = "x"` 写成等价的 `id="x"` → 该条红、同文件 tomllib 用例仍绿（历史教训残留）；
- `test_knowledge_facts` 直接断言私有结构 `_ROGUELIKE_THEMES["BlackFlow"]["name"]`，改成 dataclass 即红。

### 5.2 零覆盖路径（按失败后果排序，全部 0 断言）

1. **`MaaEngine._connect` 整个函数**：`ResolutionGot` / `UuidGot` / `Connected` / `ConnectFailed` / `AsyncCallInfo ret=False` / 超时，六条解码分支零覆盖（M7 整体 `return False` 仍绿）。真机"连上没连上"全靠它 —— 与 simp-r3 独立标出的复杂度热点（47 行 / 17 分支）是同一处。
2. **`arknights_capture` 失败分支**：`res is None → Err`（超时/熔断原因透传）、`except → Err`、`save=True` 返回 path。用户看到的失败文案与落盘全程未验证。
3. **`sync_operators` / `sync_depot` 的 `result is None → raise`**：即"不得把空结果写进缓存覆盖上一次"这条规则本身，**0 覆盖**，改成 `return {}` 无人察觉（与 review-r3 的 B1 是同一处代码的两面）。
4. **`ReadonlyAdapter.ocr` 全函数 + `analyze_stage` 全函数**：截图失败必须 raise、Debug OCR 不可用 → `return []`、`mode == "ocr"` 谓词 —— "错得最像对"的路径正落在无覆盖区（与 L2 同一处）。
5. **缓存整体**：`arknights_memory` 命中路径、`_store_set` 写入、store=None 时的 set 分支，全 0 覆盖。
6. `MaaEngine.start` 的 reload 分支（`if self._handle: await self.stop()`）、`AsstCreateEx` 返回空句柄 → raise、`AsstSetUserDir`/`AsstLoadResource` 失败 → raise。
7. **`stop()` 的销毁成功路径**（`destroyed=True` → 释放 `_callback`）：C16 只覆盖"跳过销毁"和"本无实例"，**正路没人走**。
8. `append_readonly_task`：`task_id == 0`、`AsstStart` 失败、`wait_predicate is None`、`_task_lock` 互斥、追加前排空 `_drain_events`（防"第二次同步拿到第一轮结果"），全 0 覆盖（M8/M19 绿）。
9. `_load_and_bind` 的幂等早返回、`_on_callback` 的 `int(msg)` 失败 / `details=None`、`_parse_details` 非法 JSON、`_find_adb` 回退、`_platform_info` 不支持平台。
10. `on_startup` 的 `config.dump()` 抛错降级分支（真 SDK 模式下 100% 命中却无人断言）；`_status_label` 的 `"connected"` 分支（测试里永远只走 degraded/manual）。

### 5.3 ★ 最重要的一条：stub 绿、真机静默失效的是 store

| 维度 | stub | 真 SDK | 判定 |
| --- | --- | --- | --- |
| **store 启用源** | 从 `plugin.toml` 的 `[plugin.store].enabled` 读 → `true`，内存 dict | 从 `ctx._effective_config["plugin"]["store"]["enabled"]` 取，**缺省 False**；disabled 时 `_get_conn` 抛 `RuntimeError` → `Err` → 被生产侧 `unwrap_or` 吞 | **实测**：真 SDK 模式 34 passed 的同时，`store.enabled=False`、写后读 = `None`；注入 `_effective_config` 后才读到 `{'a': 1}` |
| `collect_entries` 规模 | 只收 `kind=="entry"`，返回裸函数，7 项 | 收任何带 `EVENT_META_ATTR` 且 id 非空的方法 + 动态注册 `__llm_tool__*`，返回 `EventHandler`，**12 项** | 掩盖 entry id 与 lifecycle id / llm_tool 名撞名（现已有 3 对同名） |
| 装饰期校验 | kw 原样塞 dict，不校验不推断 | llm_tool 名正则强校验、`plugin_entry` 推断 `input_schema`，违规 **import 期 raise** | 线下绿、真机 import 炸 |
| config 通道 | `config.dump()` 直读 plugin.toml | 走 `ctx.get_own_config` IPC，FakeCtx 无此方法 → 抛 → 生产侧 `cfg={}` 降级 | 降级分支无人断言 |
| 超时字段名 | `dict["timeout"]` | `EventMeta.timeout` / `LlmToolMeta.timeout_seconds` | 测试 `_meta_get` 双读兜住（唯一处理正确处） |
| **`Err` 的接口** | 只有 `.error` 属性，**没有 `.err()`** | `result.py:106` 有 `def err(self) -> E` | 写断言必须用 `getattr(out, "err", lambda: None)()`，直接 `out.err()` 会让脚本轨 AttributeError |
| Ok/Err、`unwrap_or`、`SdkError` 签名、`store.keys` | 与真 SDK 一致（逐项比对过） | 一致 | 无风险，列出以示已核对 |

**并且："真 SDK 模式"在脱离 sandbox 树后根本不会发生。** 仓库内跑 pytest 也只加载 `tests/stub`（探针证实），因为 `tests/test_contract.py` 开头是：

```python
sys.path.insert(0, str(STUB_DIR))   # tests/stub
sys.path.insert(0, str(ROOT))
```

一旦 `plugin` 包没被先行导入，`import plugin.sdk.plugin` 就落到 stub 上。所以"两种形态都通过"只在**被挂载进 N.E.K.O 树**时成立。

更正一处措辞：上一轮的 sandbox 树内实跑（34 passed）本身**是成立的**——tdd-r3 在同一路径上复现，探针确实是真 SDK。问题在于它是**偶然成立**：没有任何断言检查 `plugin.sdk.plugin.__file__` 落在哪，脱离该树就会静默退回 stub，而输出仍然是 34 passed、看不出区别。所以"两种形态都跑过了"这句话的可靠性，取决于跑在哪个目录，而不取决于命令本身。

### 5.4 tdd-r3 建议补的三条 + 一道门禁

- **T1（最高）store 往返 + 禁用必须显式**：FakeCtx 注入 `_effective_config={"plugin":{"store":{"enabled":True}}}`，`_store_set` 之后 `arknights_memory` 必须读回同一份；再在 `enabled=False` 下断言输出里明确标"未缓存 / degraded"，**而不是静默 Ok**。意图：写进去必须读得回，写不进去必须说出来。
- **T2 识别超时不得覆盖缓存**：让 `_run_readonly` 返回 `None`，断言 `sync_*` 返回 Err 且 store 里上一次的值仍在。意图：`None` 不是空结果。（直接对应 review-r3 的 B1）
- **T3 截图失败语义与 save 分支**：`screencap` 返回 `None` 时入口必须 Err 且消息含 `engine.last_error`；`save=True` 返回的 path 必须真实存在且宽高一致。（对应 L2 / B2）
- **T4（有余量再做）**：喂 `_connect` 三段假回调 `ResolutionGot → UuidGot → Connected` 与 `ConnectFailed`，断言返回值与 `last_error`。真机连接的唯一入口，0 覆盖。
- **门禁（不写测试，改 CI）**：① 在真 SDK 树上做一次不插 stub 的 import 冒烟；② 跑套件前断言 `plugin.sdk.plugin.__file__` 落在 SDK 源码树而非 `tests/stub`，否则 fail。

### 5.5 tdd-r3 的实跑数字

- 仓库根 stub 模式：`python -B tests/test_contract.py` → 32/32，exit 0。
- 挂载树真 SDK：`pytest tests -p no:cacheprovider -q` → **34 passed in 6.82s**，探针 `plugin.sdk.plugin = <sandbox>\repo\plugin\sdk\plugin\__init__.py`。
- 仓库副本（脱离 sandbox）跑同一条 pytest → 34 passed，但探针显示加载的是 `<临时目录>\tests\stub\...` → **真 SDK 模式不可复现**。
- 污染检查：`git status --porcelain` 空；未新增 `__pycache__` / `.pytest_cache`；变异脚本全在 `%TEMP%`。

---

## 6. 专家原始结论 · sbx-r3（沙箱运行时轴）

**核心价值：把 arch-r3 的推断升级成了运行时事实。** 它在独立 N.E.K.O 运行时里真起 pluginsrv、真 import 插件、真调 7 入口与 startup/shutdown。

### 6.1 实跑结果

| 项 | 结果 |
| --- | --- |
| 发现与加载 | 13 个插件被扫到（0.64s），本插件**成功载入**（用时 0.00s），entry `ArknightsNekoCompanion` 成功解析并实例化 |
| 入口规模 | `collect_entries()` 注册 **12 项** = 7 entry + 1 startup + 1 shutdown + **3 个 `__llm_tool__*`** —— 与 tdd-r3 独立给出的数字一致；测试只断言 7 |
| 包 ID / 类型 | `arknights_neko_plugin`、entry_type=plugin，均正确；自定义日志 handler 正确挂到 `pluginsrv.plugins.arknights_neko_plugin` |
| 未启动态调入口 | **7/7 全 Err(err_code=1, PLUGIN_EXECUTION_FAILED)**，堆栈为 `'NoneType' object has no attribute 'analyze_stage'` —— 即入口没有"未就绪"守卫，直接拿 `None` 调适配器 |
| startup | 走 degraded：`no core library found under ...\native` → `{status: "degraded", engine: {...}}` |
| memory | `{operators: None, depot: None, last_stage: None, engine: {lib_loaded: False, handle: False, message: "no core library found", native_dir, resource_dir, adb_address}}` —— **没有 `status`、没有 `degraded` 字段** |
| shutdown | 幂等，handle_cleared=True、callback_released=True；**但没有 `stop()` 的返回值/日志证据** |
| 插件自身日志 | **0 条 ERROR、0 条 WARNING** —— 降级/失败没有一条落在插件自己的 logger 上 |

### 6.2 sbx-r3 上报的发现

| 级别 | 位置 | 问题 | 修法 | 证据 |
| --- | --- | --- | --- | --- |
| 中 | `arknights_analyze_stage` + `ReadonlyAdapter.analyze_stage` | 与 arch-r3 同一条，sbx-r3 用运行时实证：带 `stage="CE-6"` 仍返回 `Err("关卡识别失败：截图失败：引擎尚未建立实例")`；未启动态直接 AttributeError | 设备必需性做成 adapter 的显式契约 | FACT（运行时） |
| 中 | `_neko_adb_missing` + `ReadonlyAdapter.ocr` | 该兜底把 `last_error` 丢掉了，用户**只看到 adb-missing 这一种解释**，而 `native/` 与 `resource/` 是首启最常见缺口 —— 与 review-r3 的按钮文案项同一处，但 sbx 是从运行日志确认"插件侧 0 条 WARNING" | 按 `last_error` 分类输出 | FACT |
| 中 | `ArknightsNekoCompanion.on_startup` | `self._engine = MaaEngine(...)` 之前**不检查、也不停掉已有 `_engine`**；旧引擎若已连接（持有 ADB 连接、线程、原生实例），重载时直接丢引用 → 泄漏 + 僵尸 ADB 会话 | `await self._engine.stop()` 或 `if self._engine: await self.stop_engine()` | FACT（**我复核过，成立**） |
| 低 | `arknights_memory` | `memory["engine"]` 是裸 `engine.status()`，**无 status / degraded**；与 startup 和详情页三处三种形状（arch-r3 §2 第③点） | 走同一 `_health()` 视图 | FACT |
| 低 | 宿主错误信封 | `PluginExecutionError.actual` **把插件 Err 消息前缀又加了一遍**：用户看到 `[PluginExecutionError] Plugin execution failed: Plugin ... raised SdkError: 关卡识别失败：截图失败…`，而插件侧已经写过一次"关卡识别失败" | 插件侧别再重复包一层前缀，只给最短原因；详细日志走 logger | FACT |
| 信息 | 沙箱工具链 | `scripts/neko_sandbox.py entry` 里 `SandboxPaths` 用了 `slots=True`，但 `Paths` 也定义了 `__slots__` → startup 直接 `TypeError: 'SandboxPaths' object has no attribute 'paths'`。属**工具项目自身 bug，与插件无关** | 工具侧修 | FACT |

### 6.3 sbx-r3 明确"没能验证"的

- 真 native 库：仓库 `native/` 只有 README，无 `MaaCore.dll`/`.so` → **所有涉及原生安装/截图/识别的路径无法在沙箱内进入**，只验证了"缺库 → degraded"这条。
- ADB：沙箱无 adb、无设备 → `_connect()` 的真实分支仍未跑过（与 tdd-r3 §5.2 第 1 条同一个洞）。
- 真 MAA 资源包：`resource/` 只有 README → `AsstLoadResource` 成功路径未跑。
- 多次 reload 的泄漏累积：只做了单实例重启，没有循环 reload 观察线程数（因为无计数器可观测，见 arch-r3 §3.5 信号 e）。

### 6.4 补充实测（fake lib + 真实插件代码，未加载 MaaCore）—— 三处表述修订

**修订 1：缺陷 A 要合并成一条"结果语义不一致"，不能只说"返 Err"。**
同一条代码路径，返回什么取决于引擎处在哪个状态：

| 引擎状态 | `arknights_analyze_stage(stage="CE-6")` 实际返回 |
| --- | --- |
| 未就绪（无 MaaCore，宿主实测） | `Err(关卡识别失败：截图失败：引擎尚未建立实例…)` |
| 就绪、但 OCR 失败（进程内实测） | `Ok`，内容是"暂未识别到明确关卡编号"的安抚建议，**零失败信号** |

前者吵、后者哑，用户拿不到一致的行为。根因不变（adapter 无条件先 `ocr()`），修法仍是 `if not stage: await self.ocr()`。这一条同时解释了 arch-r3 报 Err 与 sec/review 报 Ok 的表面矛盾——**两边都对，只是引擎状态不同**。

**修订 2：`stop()` 与在飞截图的竞争从推断升为 FACT（时间线可复现）。**
fake lib 记录到：

```
AsstDestroy(inflight=True)              ← 销毁时截图仍在飞
+0.3s  AsstGetImage::enter handle=None  ← 向 C 传了 NULL handle
destroy 比读图返回早 0.7s                ← 实例已 free，线程仍在读
```

Python 侧时序可复现；真库上"是否必然崩"仍属推断，但"向 C 传 NULL / 已释放实例继续被读"这两个前置条件已经成立。**这条比"重复 startup 泄漏引擎"更该进发布前必修清单。**

**修订 3：`sync_depot` / `sync_operators` 的空缓存覆盖，sbx 侧独立实测确认。**
`sync_depot` 遇到不可解析的 `details.data` 会把旧缓存覆盖成 `count: 0`；`sync_operators` 缺 `own_opers` 字段时覆盖成空列表。现有守卫只挡"超时返回 `None`"，挡不住"有返回但内容不可用"。与 review-r3 的 B1 是同一处代码，两家独立复现。

证据脚本：`D:\neko-sandbox\_verify\r3_sec_claims.py`（可复跑）。

---

## 7. 专家原始结论 · sec-r3（安全轴）

sec-r3 在 sbx-r3 的 fake lib 实测回来后更新了定级，方向未变、证据等级上调。

### 7.1 定级变更

| 原 | 现 | 条目 | 理由 |
| --- | --- | --- | --- |
| 高 | **高**（Python 侧 FACT / 崩溃后果 INFERENCE） | `stop()` 与在飞原生截图竞争 | 时序可复现：`AsstGetImage::enter handle=None`；`AsstDestroy` 早于读图返回 0.7s |
| — | **新增 中（FACT）** | **关停期间截图假成功** | destroy 完成后 `screencap` 仍返回 12 bytes、`last_error=''` → 入口回 Ok + 一张图 |
| 中 | 中（已实测） | `ocr()` 吞异常 → 空结果 | 就绪态静默 Ok（有安抚建议），未就绪态 Err —— 同一缺陷两种表现 |
| 中 | 中（已实测） | `sync_depot` / `sync_operators` 写空覆盖旧缓存 | 坏 JSON（`'{"龙舌兰":5,'` 截断）与缺 `own_opers` 都把 `count:1` 覆盖成 `count:0` |

其余条目等级不变：

| 级别 | 条目 | 要点 |
| --- | --- | --- |
| 高 | 资源包 / 原生库完整性 + "不代打"披露口径 | 白名单只比对**任务名字符串**，动作序列全在 MAA pipeline；`resource_dir` 来自可写配置；`native/`、`resource/` 只有 README，无二进制、无 checksum/manifest。接口层成立（无 `LinkStart`/`Fight`/`Infrast`/`Recruit` 调用路径），**行为层不成立** |
| 中 | `append_readonly_task` 无 `AsstStop` | 超时只停"等待"不停任务，MAA 侧继续点设备；`_drain_events()` 只挡已在队列里的事件，僵尸任务仍产新事件 |
| 中 | `.github/workflows/release.yml` | reusable workflow `@main` 未 pin SHA + `neko-ref: main`，且 job 持 `contents: write` |
| 中低 | `_locate_library` 的 `find_library("MaaCore")` 回退 | native_dir 找不到就按裸库名走系统搜索（Win 含 CWD/PATH），加载期间还把 native_dir 前置进 PATH → DLL 预置劫持 |
| 低 | `AsstCreateEx` 的 `custom_arg` 恒 NULL | 回调事件没有实例身份，僵尸与新实例事件混队 → 谓词误命中 |
| 低 | `AsstApiCallback`(CFUNCTYPE) 对 `_WIN_DLL`(WinDLL) | x64 无事；32 位解释器上回调返回栈失衡崩溃（INFERENCE） |
| 低 | `stage` 参数无形状校验即进建议文本并持久化 | `_store_set(STORE_LAST_STAGE, result.get("stage"))` 是跨会话回灌通道 |
| 低 | `arknights_capture` 回传绝对缓存路径 | 泄露本机用户名/目录；结合 `sync_*` 会真点设备，构成"图片→模型→点击"弱链路 |

### 7.2 sec-r3 自己划的证据边界（很重要，别过度声明）

- **已实测**：Python 侧**确实**会向 C 传 NULL handle、**确实**在 destroy 后继续读图；`ocr()` / `sync_*` **确实**会回 Ok 或写空覆盖。
- **不能证的**：缓存覆盖证据来自注入的 store shim（键名与插件实际用的 `depot_cache`/`operators_cache` 一致），只能证"逻辑上会写空"，**不能**证"用户已经在游戏里丢了缓存"。
- **仍是推断**：真 MaaCore 对 NULL / 已释放 handle 的具体表现（返回 0 / 抛 / 崩进程）未验证。所以报告里不写"会崩"，只写"向 C 传了 NULL / 已释放实例继续被读"。

---

## 8. 合并结论（lead 裁决）

六份报告合计上报 **38 条**，去重后收敛为 **7 条根因**。下面每条都标了定级分歧是怎么裁的。

### R1 · 失败被折叠成成功（最高优先，5 个入口，4 位专家独立命中）

这不是 4 个 bug，是**一条验收原则没有被写下来**：

| 位置 | 现在的行为 | 用户看到 |
| --- | --- | --- |
| `ocr()` `except → []`（就绪态） | 空文本 → `analyze_stage_text("")` | Ok + "暂未识别到明确关卡编号"的安抚建议 |
| `sync_depot` 解析失败 → `{}` | 写入 `count: 0`，**覆盖旧缓存** | Ok + "仓库 0 件" |
| `sync_operators` 缺 `own_opers` → `[]` | 写入空列表，**覆盖旧缓存** | Ok + "0 个干员" |
| `stop()` 后在飞截图返回 12 bytes | `last_error=''` | Ok + 一张无效图 |
| `_encode_png` 长度不足仍编码 | 坏 PNG，入口 Ok，宿主静默丢图 | "截图成功"但没图 |
| `_store_set/_store_get` 的 `except` 是死防御 | SDK 返回 `Err` 被 `unwrap_or` 吞 | 缓存写失败 100% 静默 |

**验收原则（建议写进 README 和测试名）**：*返回 Ok ⇒ 结果真实有效；拿不到结果一律 Err 并带 `last_error`。*

上一轮修的是这条原则的一个入口（超时不得写空），本轮发现它还有四个入口。**这是本轮唯一"必须修"的类别。**

#### R1 的前置裁决：「降级」与「失败」必须分成两件事

这条原则落地前有个硬约束——现有两条用例已经把"降级态必须 Ok"锁死了：

```
tests/test_contract.py:253  test_capture_degrades_not_raises
    assert out.is_ok(), "ADB 未连接时截图应返回 Ok(降级说明)，而不是 Err（否则 Agent 侧表现为入口报错）"
tests/test_contract.py:238-241  test_all_seven_entries_callable
    for entry_id in ...: assert out.is_ok(), "降级路径不应返回 Err"
```

所以不能一刀切"失败就 Err"，否则这两条必然红，而它们表达的是**产品决策**（没配 ADB 不该让 Agent 侧看到入口报错），不是坏测试。

这条裁决不是凭产品口味定的，有上游代码依据（`plugin/core/entry_executor.py`）：

- 入口返回 `Err` → `raise PluginExecutionError(str(result.error))` → 回包带**协议级** `error` 字段；
- 入口返回 `Ok` → value **原样作为载荷**返回，宿主**没有任何机制**去解读载荷里的 `ok` / `degraded` 键。

也就是说：`Err` 是唯一被宿主强制传播的失败通道，`ok:false` / `degraded` 只是君子协定——消费方不读就看不见。
（避免误引：`llm_tool_registry.py` 里 `body.get("ok") is False → raise` 是**工具注册**响应，不是调用结果，不能当成"`ok:false` 会被自动当失败"的证据。）

据此定四档：

**已核实：分档与现有两条用例零冲突。** 它们的调用链只经过 `_ready()` 为假的分支（无 MaaCore → `_neko_adb_missing()` → `Ok` + `degraded=True`），从未进入"引擎就绪但截图/OCR 失败"的路径。所以把第三档改成 `Err` 不会让任何现有用例变红——`test_capture_degrades_not_raises` 断言的是"未就绪不许报错"，不是"失败也不许报错"。

| 档 | 情形 | 判据 | 返回 |
| --- | --- | --- | --- |
| ① | **不需要设备** | `arknights_memory` / `arknights_engine_status` / `arknights_roguelike_advice` / `arknights_analyze_stage(stage≠"")` | **永远 `Ok`**（除参数非法），不触碰引擎 |
| ② | 需要设备、**未就绪/未配置** | `_ready()` 为假（引擎没起来、ADB 没配） | `Ok` + `degraded=True`（**真未配置时是约定；快照过期时是缺陷**，见下） |
| ③ | 需要设备、**就绪但失败** | 引擎在，但截图/OCR/识别内容/编码校验没过 | `Err(SdkError(原因))` |
| ④ | 需要设备、就绪且成功 | — | `Ok`，且结果必须真实有效 |

第 ① 档是最容易被忽略的一档，也是今天已经破掉的一档：`arknights_analyze_stage(stage="CE-6")` 在未就绪时返回 `Err`（§6.4 实测），而它本应完全不碰设备。这不是"失败没报错"，是"**不需要设备的路径被设备状态绑架**"——修法在 adapter：`if not stage: results = await self.ocr()`。

第 ② 档要顺手补一条断言：现在 `test_capture_degrades_not_raises` 只断言 `degraded=True`，**没断言 `ok is False`**。约定不写成断言就会漂，把 `ok is False` 钉住，第 ② 档才算真的锁死。

第 ② 档还有个**对称后果**必须一起说：它挡住"没连上时一片红"的噪音，代价是"连上了但链路已死"时用户仍然会看到一片 `Err`（因为 `connected` 是快照，见 R3）。两边表现相反，**在同一份配置下会互相打脸**。所以 reconnect 不是锦上添花，是第 ② 档成立的前提；而 reconnect 又必须先有 `_health()`——顺序是 `_health()` → 护栏用例 → reconnect，见 R3 末尾。

**而且第 ② 档只在"真未配置"时才是约定。** sbx 的实测把另一半变成了缺陷：链路完全正常（`AsstAsyncScreencap` 返回 1、`AsstGetImage` 返回 12 字节）、唯一变量是 `connected` 快照为 `False`（启动没连上、设备后上线）——

```
三次 capture → Ok degraded=True，原生调用累计 = 0      ← 零尝试，不是"尝试后降级"
engine_status → status=manual, connected=False, loaded=True
```

两个要害：**① 一次原生调用都没发生**，所以不是"连不上"而是"从没试过"；**② 降级文案在谎报原因**——它说"ADB 好像没连上…已降级为手动录入"，而链路是通的，而且 `loaded=True` 与 `status=manual` 同框（状态页自认库已加载，却把人判成未连接）。

定性：快照过期时，②档是**缺陷**，性质与 R9 的"谎报原因"相同，只是触发点不同；③档方向相反（死链路被判活、报原生错误、状态页称正常）。合起来就是 R3 那句 —— `connected` 被**同时**用作门禁（`_ready()`）和话术（`_status_label()` / 降级文案），所以快照一过期就双向失真。

**因此 reconnect 从"建议"升级为"②档承诺成立的必要条件"。** 在补齐之前，报告不为这句降级文案背书。

同时提醒一句"别删"：B2/B4 这两条并不是空转——tdd-r3 的变异 M3 把 `_ready()` 放宽成 `self._engine is not None`，**30/32 变红**。它们锁的是 `_ready()` 的三个条件，重构时不要当成"降级路径的冗余断言"删掉。

R1 的 5 个折叠点全部落在第 ③ 档：**它们都不是"未配置"，而是"配了但没做到"**。

顺带记一条测试设计陷阱，做 T2/T3 时别踩：`_persist()` 开头有 `if self.store is None: return`。如果测试没注入 store，`sync_*` 的空写**什么都不会发生**，断言"旧缓存还在"会假绿。必须注入可观测的 store。

定级裁决：review-r3 把 `_encode_png` 判阻塞，其余未报。我复核了机制（逐行切分不校验长度），触发面窄但真实，**按"高"并入 R1**，不单列为阻塞——因为它和 R1 家族共用同一条修法思路（校验后 Err，不要给假结果）。

### R2 · `stop()` 与在飞原生调用竞争（唯一能造成 native 内存破坏的项）

四位专家的定级分别是 高 / 中 / 高 / 高，**统一为最高**。理由：它同时是两个用户可见后果的共同根因——内存破坏（NULL handle / use-after-destroy）和关停后假成功（Ok + 12 字节图）。

三个具体缺陷叠在同一个函数里：
1. `stop()` 只判 `_screencap_stuck`，**不判是否有截图在飞**（正常截图 + 重载就能触发）；
2. `_get_image` 在**线程内部**才读 `self._handle`；
3. `stop()`/`start()` 把 `orphan` 清得早于事实。

修法（约 3 行改动）：destroy 条件覆盖 `_screencap_inflight` 与 orphan 未 done；`_get_image` 在调用点快照 handle 并用 `functools.partial` 传参；`start()` 在 orphan 未 done 时拒绝重建实例。

**别误判"恢复边已经有覆盖"**：C19 确实覆盖了 `orphan.done()` → `stuck=False` 的恢复，该路径测下来是真实的。**缺的不是恢复判据，而是 stop 的清理顺序**——C19 证明"能恢复"，不能证明"destroy 时不会踩到在飞的调用"。修 R2 时不要看到 C19 就以为这条已经有护栏。

**两支用户可见后果，均已在 fake lib 上实测**（把 fake lib 改成"对非法句柄返回 0"以模拟真库拒绝 NULL）：

```
t=+0.312s  AsstDestroy(inflight=True, handle=0x1a2b3c4d)   ← 截图在飞时 destroy
t=+0.609s  AsstGetImage(handle=NULL) -> 0                  ← 已销毁后传 NULL 给 C
entry 返回：Err
```

- 真库对 NULL 返回 `>0` → `screencap` 返回 bytes 且 `last_error=''`，用户看到"截图成功"，图是无效的；
- 真库对 NULL 返回 `0` → 入口 `Err`，但文案是 `截图失败：截图失败：AsstGetImage 返回 0（缓冲区 2764800 字节）`。

真库到底走哪一支（返回 0 / 崩进程）未验证，但**两支的现状都不合格，且修法相同**：

| 真库行为 | 现状 | 修完 R2 后 |
| --- | --- | --- |
| NULL 返回 `>0` | `Ok` + 一张无效图，`last_error=''`（最坏的一支：假成功） | 不会再有无效 handle，`Err` 带真实原因 |
| NULL 返回 `0` | `Err`，但归因错 + 双重前缀 | `Err`，单一前缀，归因指向"实例已销毁" |
| 崩进程（未验证） | 带走宿主 agent_server | 不再发生 |

**「错误归因错」建议单列成一条独立缺陷**（中低 / FACT）：`_get_image` 现在把所有失败都写成"缓冲区 N 字节不够"，而真实原因可能是句柄已销毁、ADB 已断、实例未创建；修法是失败时先判 `self._handle` 是否为 None 再决定文案，并且**只在一处加前缀**（入口不再拼接已有的 `截图失败：`）。

**它不依赖并发——已用无竞争对照组证实。** sbx-r3 做了这样一组：有效句柄、单线程、不调 `stop()`、lib 直接返回 0：

```
entry: Err
用户可见文案: 截图失败：截图失败：AsstGetImage 返回 0（缓冲区 2764800 字节）
```

与竞态路径的文案**逐字相同**，双重前缀也在纯单线程下复现。所以这条可以脱离 R2 单独修、单独验收，也是整份报告里最容易被接受的一条改动。对应的用例可以参数化成三支：lib 返回 `>0` / 返回 `=0` / 抛异常——零时序依赖，不需要任何 fake 竞态。

**这条最值得写进报告的理由不是"存在竞争"，而是"错误归因错"**：真实原因是句柄已销毁，文案却指向"缓冲区 2764800 字节不够"——用户按它调分辨率永远修不好。另外 `截图失败：` 前缀被入口（`f"截图失败{('：' + reason) ...}"`）与 `_get_image` 的 `last_error`（`f"截图失败：AsstGetImage 返回 …"`）各拼一次，出现肉眼可见的 `截图失败：截图失败：`。这两点不依赖真库、维护者无法反驳，比竞态本身更好定级。

唯一仍需授权才能定的：真库到底走哪一支（返回 0 / 崩）。在此之前按"Python 侧不设防"陈述。

### R3 · 状态有三个真值源（arch-r3 的"高"，tdd-r3 证明测试只验了"同源"不验"正确"）

`_ready()` / `_status_label()` / `engine.status()` / `arknights_engine_status` / `arknights_memory` 五处各取字段子集。已实测的自相矛盾：`connected=true` 同时 `screencap_stuck=true`（报"已连接"但截图必失败）、`engine=None` 时两值分裂、`_degraded` 粘滞无复位点、`memory.engine` 缺 `status/degraded`。

配套事实（措辞已按 arch-r3 的更正收紧）：**7 类降级形态里 4 类只能靠 reload 或重启宿主恢复；插件自身不提供任何恢复入口**——7 个 entry 里没有 reconnect / reset 类动作。要说准的差别是：用户不是"无路可走"，而是"**唯一的路在插件外面**"（宿主侧 reload 或重启宿主）。

这个差别影响 R8 的定级：缓冲不自适应时，reload 会重新走 `_connect`，收到 `ResolutionGot` 就恢复、没收到就复现**同样的失败**。所以那不是"重启就好了"，而是"重新掷一次骰子"——这也是为什么 reconnect 不能当体验优化来排期。

定级分歧：arch-r3 判高，review-r3 只在 §3 顺带提。**按"高但属重构"处理**——它不产生错误结果，产生的是"状态报告不可信"，所以排在 R1/R2 之后，但必须先做 `_health()` 再做 reconnect，否则会放大复杂度（arch-r3 的原话）。

**一个被实测证实的后果：降级契约取决于一张陈旧快照。** sbx-r3 拍到同一个 ADB 死链路，因 `connected` 快照不同而给出两种相反的对外行为：

- `connected` 快照为 `True`（链路后来死了）→ 入口走真实路径 → `Err("截图失败：…")`；
- `connected` 快照为 `False` → 入口走 `_neko_adb_missing()` → `Ok` + 友好降级文案。

也就是说"用户看到报错还是看到友好降级"，取决于启动瞬间的一次连接结果，而不是当前链路状态。这既是 R3（`connected` 无失效路径）的症状，也是 R1 四档契约里 ②/③ 档难以稳定判定的根源——**分档的判据本身不可信**。

**顺序修正（arch-r3 的最后一条，我采纳）：不要直接做 `_health()`。** 先补两条**护栏用例**，把今天已经不可信的语义先钉住，再重构：
1. `connected` happy path —— 覆盖 `ResolutionGot → UuidGot → Connected` 三段；
2. "卡死时不得同时声称 connected" —— 直击 `connected=true` + `screencap_stuck=true` 那个组合。

没有这两条护栏，重构 `_health()` 时无法判断"状态视图改对了没有"。

**不变量（可直接当验收，不需要外部状态就能自证）：**

> 若 `screencap_stuck is True`，则 `status != "connected"` 且 `degraded is True`。

今天必红。这两个字段本来就在同一份 `engine.status()` payload 里，所以判据是自证的。

**为什么"余量"比上一轮估计的还要小 —— happy path 零覆盖：**

| 路径 | 覆盖 |
| --- | --- |
| `start()` 成功分支（`ok_user/ok_resource → loaded=True → AsstCreateEx → _connect`） | **0**，所有用例都在 `_load_and_bind` 抛错处终止 |
| `_connect()` 整个函数 | **0**（变异成 `return False` 仍 32/32 全绿） |
| `_status_label()` 的 `"connected"` 分支 | **0 断言**（B5/B7/B8 只 monkeypatch 置位就绪，从不调 `arknights_engine_status`；唯一的 status 断言来自 degraded 实例 C17） |
| 7 个入口的"已就绪"路径 | 只有 `arknights_capture` 有；`sync_operators`/`sync_depot`/`analyze_stage`（不带 stage）**0** |

也就是说**最常走的迁移恰恰最没护栏**。`_health()` 的前置因此再加一条：必须先有一条能让 `start()` 真正走通的就绪路径用例，否则收敛动作本身就在裸奔。

**但"就绪态可构造"和"`start()` 已覆盖"是两件事，别混。**

- 构造"已就绪"状态**不需要 monkeypatch**——给引擎两个属性赋值即可（`eng.loaded = True; eng.connected = True`），再加一个 fake lib。写成"需 monkeypatch"会让实现者觉得测 happy path 很 hack，于是继续只测降级路径；
- 然而这种**属性注入**并没有跑 `start()`。指纹很清楚：属性注入出来的引擎 `uuid == ''`（连接回调从未发生），而 `start()` 成功分支会走 `ok_user → ok_resource → loaded=True → AsstCreateEx → _connect`。

写这条护栏用例时，**主断言只能是 `connected is True`（+ `loaded is True`）**：`start()` 里是 `self.connected = await self._connect()`，而 `_connect` 只在 `what == "Connected"` 分支 `return True`，这是"回调真的回来过"的唯一必然证据。另外两个字段都不能当主断言：

- **`uuid != ""` 只能当加强项**：`UuidGot`（回填 uuid）与 `Connected`（return True）是**两个独立 elif 分支**，只发 `Connected` 的最小 fake 会得到 `connected=True, uuid=""`——此时实现是对的，拿 uuid 当唯一断言就是假红；
- **`version != "unknown"` 必须删掉**：`self.version` 全部 3 处赋值都在这条链上（`__init__` 的 `"unknown"` + `_load_and_bind` 成功路径 + 兜底路径，后两处都是 `_call_version()` → `AsstGetVersion()`），`_connect` 整个函数体**零次出现** `self.version`——所以它是**加载指纹不是连接指纹**。更糟的是 `_bind_signatures` 强制 fake lib 必须定义 `AsstGetVersion`，只要 fake 返回任意字节这个断言就由 **fixture 自己**保证；把 `_call_version` 改成返回常量，它照样全绿。这和刚被否掉的 C17 是同一个毛病：**真值来自构造方式，而不是被测行为**。

所以：入口与状态判定的 happy path 可以低成本补上（`_ready()` 只看 `loaded`/`connected`/`adapter`，属性注入与真启动对它等价）；**但 `start()` 成功分支在零覆盖清单里**，那一条不因补齐入口用例而消失。

**一条连带警告（改一处必须同时改两处）**：若把 `degraded` 的语义改成"最近一次调用失败即降级"，现有 C17 的 `detail["degraded"] == (status != "connected")` 会与新语义互斥。**改语义必须同步改 C17**，否则新用例和旧断言互相打脸——这正是"改一处碰两处"的阈值信号已经到了。

### R4 · 生命周期复位靠手写清单

`on_startup` 里 `self._engine = MaaEngine(...)` 之前不停旧引擎（sbx 报低-中、我复核成立）→ 接真库后每次同进程重启泄漏 1 个 ADB 会话 + 1 个原生实例；`start()` 复位 3 项、`stop()` 复位 5 项的手工清单。定级**中**（当前无库不显形，接上真库就必现，修法一行）。

### R5 · 测试套件保真度（元问题，影响前四条的可信度）

- 24 个变异 → **20 个全绿**；三个主力入口实现整体桩化仍 32/32。
- 真 SDK **测试形态**下 store 是 `enabled=False`（FakeCtx 没有 `_effective_config`）→ 所有缓存写变 `Err` 被吞。准确结论不是"真机 store 坏了"，而是**"绿而不可判定"**：32 条里没有任何一条做过写后读往返（把 `sync_operators`/`sync_depot` 整个桩化仍 32/32 全绿，就是这条的证明）。
  两条约束：① 新增 store 断言**必须给 FakeCtx 注入 `_effective_config={"plugin":{"store":{"enabled":True}}}`**，否则 store 被禁用、"旧缓存保留"这类断言必然空转成立；② 真宿主 `data\store.db` 里有 `kv_store` 表、读路径 `Ok`，只证明"建表 + 读"，**写路径从未验证过**（写入只在识别成功时发生，而没人成功识别过）。
  所以正确表述是「**缓存断言不得依赖双入口一致**」，不是「真 SDK 下 store 被禁用」。
- "真 SDK 模式"是否生效取决于跑在哪个目录，无任何断言检查；脱离 sandbox 树静默退回 stub，输出仍是 34 passed。
- 双轨 `main()` 手写注册表（simp-r3：不要删脚本轨，本机无 pytest）。
- `collect_entries()` 实际 12 项，测试只断 7。

**写新用例前必须先知道的三条工程约束**（否则改完仍然假绿）：

1. **不能用 `@pytest.mark.parametrize`。** 脚本轨的收集器是 `check(name, fn)` → `fn()`，零参调用：

   ```python
   def check(name: str, fn) -> None:
       try:
           fn()
   ```

   参数化函数在脚本轨会 `TypeError: missing required argument`。要用"一个函数内部 for 循环"或"拆成多个显式函数"。
2. **新用例必须手工登记进 `main()` 的 `tests = [...]`。** 那是手写注册表（simp-r3 指出的"漏登记即假绿"风险点），不登记就只有 pytest 轨跑到，脚本轨静默跳过——而本机只有脚本轨能跑。建议顺手把这 32 条提成模块级 `TESTS`，再加一条"注册表条数 == 收集到的 `test_` 函数数"的校验用例，把双轨漂移堵死。
3. **`_get_image` 的上界分支至今零区分力**：M15 去掉 `got <= buffer_size` 仍然全绿。原因是没有任何用例让 lib 返回 `buffer_size + 1`。补这一支之后 M15 立刻变成有区分力的变异——而且它背后是 R8 那条活缺陷，不是单纯的测试空白。
4. **断言不要用 `hasattr` 探测未来 API**：stub 的 `Err` 只有 `.error`，真 SDK 才有 `.err()`；`last_ok()` 两边都没有。要用 `getattr(out, "err", lambda: None)()` 这种双兼容写法，并靠断言确认返回的是哪一支。
5. **用 `MaaEngine.__new__` 裸构造的实例没有 `width`/`height`**（`__init__` 没跑）。现有 C11 那 5 行是靠后续赋值绕开的；照抄写新用例会直接 `AttributeError`。正确做法是在用例里显式补 `engine.width, engine.height = 1280, 720`，**不要为了测试在生产侧写 `getattr`**——别把叶子原语弄脏。
6. **"双前缀"和"哨兵值泄漏"要写成两条独立断言**：`18446744073709551615` 出现在文案里、和 `截图失败：` 出现两次，是两个可以独立修好的缺陷。写成一条会让红绿对照互相掩盖——修好一个另一个仍然错，但用例已经绿了。

### R6 · 文档与对外承诺失真

"手动录入"这套降级模式**不存在**（4 处文案承诺，7 个入口里无一手动写入口）；"不代打"只在接口层成立；"21 条"过期 4 处；clamp 4096×4096 vs 宿主 3840×2160；2MB 内联会被宿主按 500KiB 重编码成 JPEG。

### R7 · 供应链三道缺失（sec-r3 报「高」，但修复不在功能路径上）

| 层 | 现状 | 缺什么 | 说明 |
| --- | --- | --- | --- |
| 资源包完整性 | 装进去是什么就信什么，不比对版本或哈希 | 本仓库不含资源包本体，反向也证明不了外部 `resource_dir` 指向的内容 | 能校验的只有哈希，而哈希清单由本项目发布，loop 闭不上 |
| 依赖不可变 | `release.yml` 的 reusable workflow 写 `@main`，`neko-ref: main` | 没有 pin SHA，也没有锁文件 | 上线到 Market 后用户装到的是"当时的 main"，升级不可复现 |
| 原生库来源 | `_locate_library` 有 `find_library("MaaCore")` 回退 | 加载前不校验 resolved 路径落在 `native_dir` 内 | 系统预置同名库即可劫持（中低） |

裁决：sec-r3 判"高"的理由是**对外承诺失真**（说"不代打"，而行为层由未校验的资源包定义）。代码侧的三条都不昂贵，可以下一轮统一做；**但披露口径的修正不需要等代码**，见 R6。

### R8 · 【高，待真机确认】截图取回的是 PNG，却被当原始 RGB 处理（缓冲不自适应是它的下游症状）

**先看这一条，它可能让整个截图链路在真库上从未正确工作过。**

上游 `AsstCaller.cpp` 的 `AsstGetImage` 实际是：

```cpp
auto png = cv::imencode(".png", handle->get_image(), buff);   // ← 编码成 PNG
if (buff_size < png.size()) return NullSize;
memcpy(buff, png.data(), png.size());
return png.size();
```

即它返回的是**压缩后的 PNG 字节**，不是 `w*h*3` 的原始像素。而插件把返回值当原始 RGB 处理：

```
maa_adapter.capture()   pixels = len(raw) // 3        ← 用 PNG 长度当像素数
_infer_dimensions(...)                               ← 算不出 → 回退 engine.width/height
_encode_png(raw, w, h)  raw[y*stride:(y+1)*stride]   ← 按 RGB 行宽切 PNG 字节
```

我在本机用插件自己的编码器复现了后果：造一张 1280×720 的帧、编码成 PNG（**2,759 字节**），再喂回这条链路——

```
MAA 返回的 PNG = 2759 字节   插件假设应是 w*h*3 = 2,764,800
pixels = 2759//3 = 919        _infer_dimensions → (1280, 720)（回退值）
二次编码后 PNG = 168 字节      ← 按 3840 字节/行切 2759 字节的数据
```

**168 字节不可能是 1280×720 的 PNG**，解码必然失败或被当成极小图。也就是说：只要真库按上述契约返回 PNG，`arknights_capture` 的内联图、`save=True` 落盘的图、以及 OCR 用的临时图**全都是坏的**——而这正是本插件最高频、最核心的路径。

**一分钟定性的真机验证**：真机截图一张，用任何看图软件打开 `data/maa/capture.png`。打不开或被识别成几十×几十像素 → 上游契约成立，R8 升级为最高优先；能正常打开 → 说明目标构建的 `get_image()` 返回原始像素，本条作废（但 3 字节/像素的假设仍需量）。

**对应用例的判据也应从"size"改成"可解码"**：断言产物 PNG 的 IDAT 解压后长度 `== height * (1 + width*3)`，或直接解码校验。这条今天必红、且不依赖任何文案与实现细节，比"第二次的 size 更大"稳得多。

分档理由：这条的证据来自**上游源码阅读 + 本地编码复现**，没在真实 MaaCore 上跑过，所以标"待真机确认"而不是直接给最高。但它是本轮唯一一条"如果成立就让功能整体不可用"的候选，必须放在 R8 最前面。

---

**缓存/缓冲那部分（原 R8 主体，若 PNG 契约成立则降为次要）**

⚠️ 先标清依赖关系：**下面整节的前提是"`raw` 是原始 RGB"**。若上游确实返回 PNG，则 PNG 通常远小于 `1280*720*3 = 2764800`，`AsstGetImage` 根本不会走到"缓冲不足"分支，本节降级为次要问题（仍成立的只有：`buffer_size` 恒按 1280×720 算、单次 capture 只调 1 次、无重试、宽高可冻结）。所以 **R9 那条"1080p → 缓冲必不足 → 会话必失败"的推论已随前提撤回**。

一条把这个"必然性"钉死的机制：`_encode_png` 生成的 IDAT 长度恒为 `height + len(raw)`。只要 `raw` 是 PNG，这个等式就永远不等于 `width*height*3`（对 1920×1080：10,053 vs 应有 2,765,520），**不是概率问题，是恒不成立**。对照实验也证明编码器本身没问题——同样一张帧若 `raw` 真是原始 RGB，输出 9,284 字节、1920×1080、合法 PNG。

这条顺带解释了 `ocr()` 那个"静默返回空"的真正根因：作者写的"不能返回 `[]`，要 raise"只在 `raw is None` 时触发，而坏 PNG 让 OCR 走的是另一条路——**修好的那半边被整条绕过**。

这条是从 tdd-r3 的变异点 M15（`0 < got <= buffer_size` 上界零覆盖）反推出来的**活缺陷**，不是测试缺口：

```
maa_engine.py:163-164   self.width  = 1280      ← 默认值
                        self.height = 720
maa_engine.py:454-455   self.width  = int(d.get("width", self.width))   ← 只在 ResolutionGot 时更新
maa_engine.py:548-549   buffer_size = min(max(width,1280),4096) * min(max(height,720),4096) * 3
maa_engine.py:607-614   got = AsstGetImage(handle, buf, buffer_size)
                        if 0 < got <= buffer_size: return bytes(...)     ← 超出就只报错
```

推演：`ResolutionGot` 没到（连接时序 / 配置差异）→ `width/height` 停在 1280×720 → `buffer_size` 恒为 2,764,800；而设备实际是 1920×1080（需 6,220,800）。于是**本次会话里每一次截图都失败，且没有自愈路径**。要写准这一点（sbx-r3 的代码事实）：
- `_connect` 全文只有一个调用点（`start()`），`connected` 也只有三处赋值（`__init__`/`start`/`stop`），**7 个入口没有任何一个会触发重新连接**——所以"用户重连一下"这条路不存在，状态页上的 `connected` 本身就是启动快照而非实时链路状态；
- **重启不等于恢复，只是重新掷一次骰子**：重启会重新走 `_connect`，收到 `ResolutionGot` 就按真实分辨率重算、恢复正常，没收到就同样永久失败。不要写成"重启也没用"——那需要真库 + 真 ADB 才能证。

文案还只报"缓冲区 2764800 字节"，不提示任何可恢复手段。

真库在缓冲不足时是返回"所需大小"还是返回 0，未验证；但**两种情况下"不自适应"都成立**，所以定级不受影响。

**这条必须分两层写，否则会被真机反驳**（和 store 那条踩的是同一个坑）：

- **FACT（条件句成立）**：`_get_image` 函数体内**零次回填** `width/height`，而 `width/height` 的赋值点只有 `__init__`（1280/720）与 `ResolutionGot` 分支；`_connect` 又只在 `start()` 内被调用一次（无 reconnect）。**若** `ResolutionGot` 未到达 → 缓冲恒为 `1280*720*3 = 2764800` → 取图超界即丢弃并返回 None → **单次 capture 内没有重算机会** → 会话级失效。
- **INFERENCE（不能写成已确认）**：真机 MAA 的**事件入队顺序**是不是 `Connected → ResolutionGot`。注意争议点只在"顺序"，不在"发没发"——结构上后到的 `ResolutionGot` 必被丢弃这件事已经由代码定死（见下）。上一版写成"真机是否发 `ResolutionGot`"是不准确的，现在收窄。

**零成本的取证前置步骤（修复前先做这个，比争代码省事）**：`_get_image` 的失败文案自带全部参数——`截图失败：AsstGetImage 返回 {got}（缓冲区 {buffer_size} 字节）`。真机只要看一眼 `engine_status.last_error`：

- `got = 0` → ADB / 渲染层截不到图，与分辨率无关，R8 这条不命中；
- `got > 2764800` → **证实**缓冲不足路径，上面的 INFERENCE 直接升级为事实。

修复方向和三支用例的口径都依赖这一步的结果，所以把它排在最前面。

**但还有一个比"事件缺失"更硬的机理：顺序依赖。** `_connect` 的消费循环里，`Connected` 分支是立刻返回的：

```python
if what == "ResolutionGot":
    self.width = int(d.get("width", self.width)); self.height = ...
elif what == "UuidGot": ...
elif what == "Connected":
    return True                     # ← 循环在这里结束
```

所以**只要队列顺序是 `Connected → ResolutionGot`，后到的那条 `ResolutionGot` 永远不会被用于回填**（后续 `_wait_for` 只会把它从队列里取走丢掉）。这不是"MAA 没发"，是"发了也没人接"——`width/height` 于是在本次会话内冻结在 1280×720，且没有第二次 `_connect` 来补救。

**另外两条容易被误读的地方：**

- `screencap(buffer_size=...)` 的**生产调用点为零**（`maa_adapter` 两处都是无参 `await self.engine.screencap()`，测试也是），所以不存在"上层传个更大的缓冲"这个现成逃生口——修法必须落在 engine 侧（同一次调用内重算 + 重试 + 回填），不是改一行调用方。
- 钳位 `min(max(w,1280),4096)` **不是无损保护**：宽 > 4096 的设备会被截到 4096，缓冲反而小于实际需要（4320×2160 时缓冲 26,542,080 < 需要 27,993,600），仍然回到同一条失败路径。算术成立、真机罕见（4K 手机多为 3840），只作脚注，不升级定级。

**别把三种截图失败混为一谈**（文案不同、根因不同）：

| 文案 | 根因 |
| --- | --- |
| `AsstGetImage 返回 {got}（缓冲区 {buffer_size} 字节）` | 取图失败：`got=0` 是截不到图，`got > buffer_size` 是缓冲不足 |
| `AsstAsyncScreencap 返回 0（调用被拒绝）` | 请求被拒，根本没开始取图 |
| `上一次截图尚未返回` | 并发互斥，压根没发第二次调用 |

sbx-r3 用 fake lib 把它跑成了可复现现象：让 `AsstGetImage` 返回 `buffer_size + 1`（模拟真库说"差 1 字节"）、`ResolutionGot` 未到、连调三次：

```
三次均为 Err，buffer_size 恒为 2764800（1280*720*3），msg 一字不差
engine.width/height 仍 1280/720   ← 从未回填
```

它同时确认了没有隐性反馈环：`width/height` 全文件只有两处赋值（`163-164` 默认值、`454-455` 的 `ResolutionGot` 回调），`_infer_dimensions` 只在**成功拿到字节之后**才反推，不参与 `buffer_size` 计算。所以这是个纯单向、无自愈的循环。

**修法不能靠 `got` 反推——上游契约已经查实，我先前那条建议是错的。** 上游 `src/MaaCore/AsstCaller.cpp`（`AsstSize` 为 `uint64_t`）：

```cpp
static constexpr AsstSize NullSize = static_cast<AsstSize>(-1);   // 2^64-1
AsstSize AsstGetImage(AsstHandle handle, void* buff, AsstSize buff_size) {
    if (!inited() || handle == nullptr || buff == nullptr) return NullSize;
    auto img_data = handle->get_image();
    size_t data_size = img_data.size();
    if (buff_size < data_size) return NullSize;   // ← 缓冲不足也是 NullSize
    memcpy(...); return data_size;
}
```

即：**缓冲不足、handle 为空、buff 为空、未初始化，四种情况共用返回值 `18446744073709551615`**；图像为空才返回 `0`。所以 `got` 里根本没有"需要多大"的信息，反推这条路不存在。

可行修法只剩三条，按成本排序：

1. **先绑 `AsstGetNullSize()`**（全仓零命中、从未绑定）拿到 `null_size`，用它把"缓冲不足"与"句柄失效"拆开——文案不再吐天文数字，也消掉下面的不可区分缺陷。**这条最便宜，建议进第一批**；
2. 靠 `ResolutionGot` 回填分辨率（前提是修掉 §R8 的顺序依赖）；
3. 盲目倍增重试一次，或按已知分辨率直接算足。

**还有一条会决定"是否永远不足"的未知数**：插件按 3 字节/像素算缓冲（`w*h*3`），而上游另有 `AsstGetImageBgr` 暗示两者数据格式不同。**若实际是 4 字节/像素，缓冲永远不足**。真机成功时 `got` 是 `w*h*3` 还是 `w*h*4` 即可定性——这是修复方首先要量的数。

fake lib 里"返回 `buffer_size + 1`"只是假设。真机取证判据要从"`got > buffer_size`"改成 **`got == 18446744073709551615`**；一次抓取同时定死三件事（`got` 是否为 NullSize、`width/height` 是否冻结在 1280×720、会话内是否可复现）。

**这带来「错误归因」的第四面，比前三面都硬：两种不同根因产生逐字相同的日志。**

| 场景 | `last_error` |
| --- | --- |
| 缓冲不足 | `截图失败：AsstGetImage 返回 18446744073709551615（缓冲区 2764800 字节）` |
| 在飞句柄失效（`stop()` 在两次原生调用之间置空 handle） | **逐字相同** |
| 图像为空（返回 0） | `… 返回 0 …`（可与上两者区分） |

所以不只是"归因不准"，而是**不可区分**，且把 `2^64-1` 这种天文数字直接摆给用户。低成本修法：绑定 `AsstGetNullSize()`，`got == null_size` 时用 `self._handle is None` 二分，文案改成"缓冲不足"/"引擎句柄已失效"。

**不要为此绑定 `AsstGetNullSize()`。** 它按定义就是 `(AsstSize)-1 = 0xFFFFFFFFFFFFFFFF`，而 `AsstGetImage` 的 `restype` 已是 `c_uint64`，所以直接在 Python 侧比常量即可。绑定访问器反而有兼容风险：插件声称支持 v5.x~v6.x，旧版若无该符号，`_bind_signatures` 会抛 `AttributeError` 并判"缺少必需符号"，**整个引擎不可用**——比它要修的缺陷严重得多。

三支互斥文案，成本 = 一个 `if`：

```python
if got == 0xFFFFFFFFFFFFFFFF:
    if self._handle is None: "引擎句柄已失效（实例已销毁）"
    else:                    "缓冲不足（本次 {buffer_size} 字节，需按设备真实分辨率重算）"
elif got == 0:               "未取到图像（图像为空）"
```

不含天文数字、可分辨根因、与 R9 的自适应回填完全解耦——即使 R9 排期靠后，用户也不用再看 `18446744073709551615`。

**顺带更正两个数字**：

- R9 基线里 `got` 不是 `6220801`，是 `18446744073709551615`；`buffer_size = 2764800`、单次调用 1 次、宽高冻结不变。
- `last_error` 的 16 个写入点里，有 **4 个在 `_connect` 内部且被 `start()` 的通用文案覆盖**（`ADB 连接失败（详见日志）…`），所以**用户可见的归因写入点实际只有 8 个**。这也解释了 `_connect` 为什么零覆盖——不是没人想测，是**通过任何入口都观测不到它的归因**。
- 另有一条静默分支：`_connect` 的 `if event is None: return False` 不写 `last_error`，投递坏 JSON 时会"超时 + 无原因"，是排查成本最高的一种失败。

**这条已经用「唯一变量 = 事件顺序」的对照实验跑出来了**（真实 `_on_callback` → queue → `_connect` 消费，不加载 MaaCore）：

| 观测 | 正序 `ResolutionGot→UuidGot→Connected` | 逆序 `Connected→UuidGot→ResolutionGot` |
| --- | --- | --- |
| `_connect` 返回 | True | **True** |
| `width × height` | 1920 × 1080 | **1280 × 720（未回填）** |
| `uuid` | `abcdef0123` | **`''`** |
| 队列残留 | 0 | **2 条被 `_wait_for` 当垃圾吃掉** |
| 取图缓冲 | 6,220,800 | **2,764,800** |
| `screencap()` | 6,220,800 bytes | **None** |
| `last_error` | `''` | `截图失败：AsstGetImage 返回 6220800（缓冲区 2764800 字节）` |

关键点：**两种顺序下 `connected` 都是 `True`**。所以这不是"状态是快照、后来过期了"，而是**同一时刻 `connected` 为真、该会话的截图能力已经废了**——`connected` 作为"连接可用"的断言被实测证伪。

由此得出一条验收铁律：只看 `connected is True` 不够，**必须同时满足 `(width, height) != (1280, 720)` 或 `uuid != ""`**，否则"连上了但截图全废"的会话会被判成健康。

**验收要用行为断言，但不能写成"第二次的 size 严格大于第一次"**——如果修法是"同一次 capture 内重算 + 重试"（最小修法），跨调用比较时 size 可能完全相同，正确实现会被判红。可用的上位不变量是**下面三支择一**：

> **③（必选）** 成功一次之后 `engine.width/height` 不再是 `1280/720`（被回填）；
> **① / ②（补充证据，可选）** 跨调用 `buffer_size` 取值集合不止一个；单次 capture 内 `AsstGetImage` 被调用 ≥2 次。

为什么 ③ 必须必选：只做"重试"不做"回填"是一种半修——用户的截图症状消失了，但 `engine_status` 会永久报 1280×720（状态页继续给错信息），而且每个会话的首个截图固定多一次失败的原生调用。若把 ①/② 也设为必选就会误伤"单次调用内重算 + 重试"这种正确实现，所以它们只作"未退化"检查。

③ 有个前提必须写清：`ResolutionGot` 也是独立分支，而 `width/height` 的**默认值就是 1280/720**。如果 fake 发的恰好是默认分辨率，"回填过了"和"回调从未来过"完全无法区分。**必须让 fake 发非默认分辨率（如 1920×1080）再断言回填**，否则这条永远假绿。

sbx 实测今天**三支全红**（单次只调 1 次、size 恒 2764800、未回填），所以放宽后依然是必红用例，只是不再绑死一种实现。纯文案修改在这三支下都假绿——这正是要的区分力。

这条与「错误归因」叠在同一个 `Err` 上，但**修法完全不同**（改文案 vs 改缓冲策略），所以分成 R8 与 R9 两条，不合并。

### R9 · 失败路径的统一不变量（把 R1/R2/R8 收成可断言的句子）

| 不变量 | 现在 | 意义 |
| --- | --- | --- |
| `screencap() is None` ⇒ 入口 `Err` 且 `last_error` 非空 | 基本成立 | 失败必须可解释 |
| `destroy 已发生` ⇒ `screencap()` 必须返回 `None` | **不成立**（关停后仍返回 12 字节） | 销毁后不得再产出任何字节 |
| `Ok` ⇒ 载荷是本轮真实结果 | 5 处不成立 | R1 的验收原则 |
| 失败原因的**前缀只由一层负责** | **不成立**，见下 | 文案可信度 |

关于最后一条，`last_error` 的赋值点穷举（`maa_engine.py` 共 16 处写入，含 `__init__` 的清空）暴露出的是**分层重复**，不是单点笔误：

```
带前缀：  532  "截图失败：引擎尚未建立实例（AsstCreateEx 未成功）"
          569  "截图失败：AsstAsyncScreencap 返回 0（调用被拒绝）"
          615  "截图失败：AsstGetImage 返回 {got}（缓冲区 {buffer_size} 字节）"
不带前缀：538  "上一次截图调用仍未返回，稍后再试会自动恢复…"
          543  "上一次截图尚未返回，请稍后重试"
          565  "截图超时（>{SCREENCAP_TIMEOUT}s），ADB 链路可能已断开"
入口：    __init__  f"截图失败{('：' + reason) if reason else '（ADB 可能已断开）。'}"
```

于是 6 条失败路径里，**3 条正常、3 条变成 `截图失败：截图失败：…`**。同理 `532` 那条在"引擎尚未建立实例"时也会双前缀，所以这个缺陷在最常见的首启失败里就能看到。

另外两处相关但独立：
- `_connect` 有一条超时返回不写 `last_error`（review-r3 报），用户看到的是**上一次**的原因；
- `last_error` 的写入点定稿 **16 处 = 3 处带 `截图失败：` 前缀 + 1 处 `""` 重置（仅 `__init__`）+ 12 处纯原因**（`截图超时（>` 在两个分支各写一次，是唯一重复项）。**同一进程内它一旦非空就永不自动清**：`stop()` 不清、重跑 `start()` 也不清、成功也不清；只有 reload/重启换进程才会因 `__init__` 重新执行而归零（sbx 实测 reload 是 pid 58440→46724 换进程）。所以"陈旧原因"不是偶发，是长跑宿主里的默认状态——进程不重启是常态，"几小时前的错误一直挂着"成立。这 3 处前缀改动**不会误红现有用例**：C12/C15/C18 只匹配"尚未返回/仍未返回/取消"，不碰前缀文本。sbx 实测的连续两次失败（同一引擎、无并发、无真库）：

```
第 1 次 (AsstGetImage 返回 0)  → Err  last_error = '截图失败：AsstGetImage 返回 0（缓冲区 2764800 字节）'
第 2 次 (lib 抛异常)           → Err  文案 = '截图失败：lib boom'
第 2 次之后 last_error 仍是第 1 次的旧原因
arknights_engine_status        → 照样报这串旧原因
```

  用户点「引擎状态」看到的是与本次失败无关的原因，报 bug 时会把我们引向错误方向。这是「错误归因」的第二个面，同样不需要并发或真库前提。

- `stop()` 里的异常只走 `logger`，既不进 `last_error` 也不进 `status()`——这是 arch-r3 的"状态不可信"和 sbx 的"插件日志 0 条 WARNING"合流的地方：**失败既不进 status、也常常不进日志，只在某一次入口返回值里一闪而过**。
- **第③面：成功路径也从不清理 `last_error`。** `screencap` 只在失败分支写这个字段（`532/538/543/565/569/576/584/615`），`return got`（`604`）之前没有任何清零动作。所以先失败一次、再成功若干次之后，`arknights_engine_status` 仍然报着那次旧失败，而截图其实一直正常。

**裁决：不窄化断言，改实现。** 异常通道也必须写 `last_error`（`logger.exception` + 一句面向用户的原因）。理由与四档契约同源——`Err` 是唯一被宿主强制传播的通道，而 `arknights_engine_status` 是用户唯一的自查看口；把断言收窄到"只在不成立的那一半"，等于把缺陷写成契约。

配套把 `last_error` 定成三条约定（这是"错误归因"那批的根治办法）：

1. **失败必写**：所有失败路径（含异常通道、`_connect` 超时、`stop()` 内异常）都刷新它；
2. **成功必清**：`screencap` 成功返回前清空，否则 status 会长期显示旧故障；
3. **前缀只在入口加一次**：`last_error` 存**纯原因**（"AsstGetImage 返回 0"），`截图失败：` 由 `arknights_capture` 加。这样 6 条失败路径的文案一次性统一，也消掉 `截图失败：截图失败：`。

改完之后最直观的验收是 `status()` 的自相矛盾消失——失败后成功一次再查状态，现在长这样（sbx 实测）：

```
{"status": "connected", "degraded": false,
 "last_error": "截图失败：AsstGetImage 返回 0（缓冲区 2764800 字节）"}
```

**测试设计陷阱：这两条必须成对断言，不能分开。** 如果只断"成功时 `last_error` 为空"、或只断"失败时 `last_error` 非空"，那么"**永远清空 `last_error`**"这个错误实现能让两条各自都绿（清空版：失败时错误文案丢了，成功时恒空）。必须用同一个引擎做「失败 → 断言非空 → 成功 → 断言为空」的序列。

用例也要分开写，不要塞进同一条：`test_shutdown_idempotent` 现在断的是"重复停在已停状态不出错"，而 R2 需要的是"首次 stop 真的触发释放"，语义不同——合成一条会让两个缺陷互相掩护。

---

## 9. 建议的动手顺序

| 顺序 | 做什么 | 为什么排这里 | 量级 |
| --- | --- | --- | --- |
| 1 | R2 的 3 处（destroy 条件 / handle 快照 / start 拒绝重建） | 唯一能碰 native 内存 + 关停后假成功，且改动小 | ~3 行 + 1 条用例 |
| 2 | R9 的 `last_error` 三条约定（失败必写 / 成功必清 / 前缀单层） | 零时序依赖，顺手消掉双重前缀与陈旧原因；是后面所有失败语义的地基 | ~10 行 + 2 条用例 |
| 3 | R8 缓冲区自适应（按 `got` 反推并重试一次） | 分辨率对不上时截图**永久失败**，用户只能重连；同时把 M15 补成有区分力 | ~6 行 + 1 条用例 |
| 4 | R1 的验收原则 + 5 个折叠点 + 四档契约 | 会让用户"照着错答案行动"或"悄悄丢掉已有数据" | ~10 行 + 3 条用例（T2/T3） |
| 5 | R4 的 `on_startup` 补 `stop()` | 接真库前不修，第一次 reload 就泄漏 | 1 行 |
| 6 | 测试门禁（断言 `plugin.sdk.plugin.__file__` 落点）+ T1 store 注入 | 不修这条，前面改完仍然是"stub 说好" | CI 2 步 + 1 条用例 |
| 7 | R3 的 `_health()` 单一真值 | 是 R6 文案和 reconnect 的前置 | 中等重构，可拆 |
| 8 | R6 文案（含披露三层写法） | 不修代码也能做，且是对外承诺 | 纯文档 |
| 9 | R7（pin SHA 最便宜，资源包校验最贵） | 分发安全，不在功能路径上 | 视取舍 |

第 1~3 步都是"改完立刻能写断言验收、且不依赖真库"的项，适合当成同一批做掉再跑两轨。

## 10. 本报告没有证明的事

- 真 MaaCore 加载、`AsstCreateEx`、ADB 连接、截图、OCR、Depot/OperBox 识别：`native/` 与 `resource/` 只有 README，**全部未在真库上跑过**。
- `_connect()` 的六条解码分支：0 覆盖、0 实测。
- 真库上 NULL / 已释放 handle 的具体后果（返回 0 / 抛 / 崩进程）。
- 缓存覆盖的证据来自注入的 store shim，只证"逻辑上会写空"，不证"用户已经丢了数据"。
- 线程数/泄漏累积：无计数器，属不可观测。

## 11. 证据索引（想复核时从这里进）

所有证据都在仓库外，`9fe610f` 工作区自始至终 `git status --porcelain` 为空。

| 证据 | 路径 | 证明什么 |
| --- | --- | --- |
| fake lib 双分支（销毁后 NULL / use-after-destroy / 关停后假成功） | `D:\neko-sandbox\_verify\r3_sec_claims.py` | R2 的时间线；sec 的定级变更 |
| fake lib 返回 0 的竞态复现 | `D:\neko-sandbox\_verify\r3_sec_fake0.py` | "错误归因 / 双重前缀"不依赖并发 |
| 三支验收全红（单次 1 次调用、size 恒 2764800、width/height 未回填） | `D:\neko-sandbox\_verify\r3_r9_adaptive.py` | R8 的现状与验收口径 |
| "重连不存在"的代码事实（`_connect` 唯一调用点、`connected` 三处赋值） | `D:\neko-sandbox\_verify\r3_r9_noreconnect.py` | R8 的"无自愈路径"；R3 的快照语义 |
| 7 入口结构化留档 | `D:\neko-sandbox\_verify\r3_entries.json` | 未就绪态的全 Err、memory 缺 status/degraded 等运行时形状；接口表交叉核对 |
| 运行时日志（发现/加载/startup/reload/autostart） | `D:\neko-sandbox\logs\server-20261110-*.log`、`...\N.E.K.O\logs\plugin\...arknights_neko_plugin.log` | sbx 的运行时结论；插件侧 0 条 ERROR |

**复跑提醒**：启动 sandbox 时若注入了 `PYTHONPATH`（含 `sitecustomize` 的 safe-delete 钩子），宿主 `ConfigManager` 的清理动作会触发 `SystemExit` 并把整个宿主打死——本轮曾出现一次 `500 start_plugin failed` 的假故障，已归因为工具链。**用 `env -u PYTHONPATH <python>` 启动**。

这条要写进验收纪律：**在排除 `PYTHONPATH` 之前，"宿主 reload 报 500 / 插件加载失败"的结论一律不成立。** 修复阶段如果有人这么报，先排环境变量再排代码。

**建议的回归方式（比"新增用例全绿"更硬）**：把改动版复制进沙箱，用同一套 fixture 跑出**修复前/后实测值对照表**——同一观测点的前后差值能防"用例与实现一起漂"。现有 fixture 已覆盖：竞争与关停后假成功（`r3_sec_claims.py`）、错误归因与双重前缀（`r3_sec_fake0.py`）、缓冲三支（`r3_r9_adaptive.py`）、快照门禁与无重连（`r3_r9_noreconnect.py`），全部只注入 fake lib，不碰真库、不写源仓库。

### 修复前基线（照这三个观测点做前后对照）

**R8 缓冲**（三个观测点，修复后任一支变化即算改善）：

```
size 序列          = [2764800, 2764800, 2764800]   去重后仅 1 个值
单次 capture 调用数 = [1, 1, 1]                     无重试
width/height       = (1280,720) → (1280,720)        未回填
2764800 == 1280*720*3                               按默认下限算出的缓冲
```

**R9 状态页自相矛盾**（完整响应体，修复后期望只有 `last_error` 变成 `""`，其余字段不变——这是最干净的回归基准）：

```json
{"loaded": true, "connected": true, "version": "unknown", "adb_address": "", "uuid": "",
 "width": 1280, "height": 720,
 "last_error": "截图失败：AsstGetImage 返回 0（缓冲区 2764800 字节）",
 "screencap_stuck": false, "status": "connected", "degraded": false}
```

矛盾点：`status=connected` + `degraded=false` + `connected=true` 与 `last_error="截图失败：…"` 同框。

**沙箱 500 误判的判据（比"记得 unset PYTHONPATH"更可操作）**：在宿主日志里 grep `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`——**有这行就是工具链，没有才轮到怀疑产品**。

**一个必须记住的隔离缺口**：上述 shim 指向的临时路径是**真实用户目录**（`OneDrive\文档\`），说明沙箱隔离没有覆盖 Windows Known Folders。后续任何"要删临时文件"的验证据本都可能落到用户的真实文档目录里——跑之前必须确认 `cwd` 与 `tmp` 都在沙箱根下（`%TEMP%` 里操作，或显式指定沙箱内路径）。这条属工具链风险，不是产品缺陷，但会污染用户磁盘，写在这里供修复阶段自查。




