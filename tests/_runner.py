"""零依赖测试收集器（无 pytest 也能跑）。

设计目标只有一条：**任何"没跑到"的情况都必须自己喊出来**。
上一版事故是 `main()` 里手写 `tests = [("A1", fn), ...]` 清单，新增 `test_*` 忘了登记
→ pytest 能跑、脚本入口静默跳过、报告仍然 100%。

这里用两道闸：
1. 注册靠装饰器（`@case("W01")`），**不可能忘**；
2. 收集期跑四条一致性断言，任何一条不满足直接 `exit 2`（绝不当成通过）。

退出码
------
0  全部通过
1  有失败
2  收集期不一致（比失败更严重：说明"跑的数量"本身就不可信）
3  有未实现 / 被阻塞（不算通过，但也不是回归失败）
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

CASE_PREFIX = "test_"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_COLLECT = 2
EXIT_INCOMPLETE = 3

PASS = "通过"
FAIL = "失败"
UNIMPL = "未实现"
BLOCKED = "阻塞"


class Unimplemented(AssertionError):
    """用例已登记但正文还没写。

    刻意继承 `AssertionError`：pytest 下它是红的，不会被当成 skip 静默漏掉。
    """


class Blocked(RuntimeError):
    """前置条件不满足（依赖模块还没落盘 / 缺真机样本）。**不是**回归失败。"""


@dataclass
class Case:
    id: str
    name: str
    fn: Callable[[], None]
    is_async: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: list[Case] = []


def case(cid: str, *tags: str) -> Callable:
    """注册一条用例。原样返回函数，所以 pytest 也能收集同一批 `test_*`。"""

    def deco(fn: Callable) -> Callable:
        REGISTRY.append(
            Case(
                id=cid,
                name=fn.__name__,
                fn=fn,
                is_async=asyncio.iscoroutinefunction(fn),
                tags=tags,
            )
        )
        return fn

    return deco


def check_registry(module) -> list[str]:
    """四条一致性断言。返回问题列表，空列表 = 干净。

    **按模块作用域**比较：只拿"定义在本模块里的用例"与"本模块的 `test_*`"比。
    理由：`REGISTRY` 是全局的（`test_contract` 与 `test_service` 共用一个 `_runner`），
    若拿全局登记表去比单个模块的命名空间，两个模块互相会被判成"登记了不存在的函数"。
    归属靠 `Case.fn.__module__` 判定 —— 这是函数自带的、不会漂移的事实。
    """
    problems: list[str] = []
    mod_name = getattr(module, "__name__", "")

    funcs = {
        n: v
        for n, v in vars(module).items()
        if n.startswith(CASE_PREFIX) and callable(v)
    }
    own = [c for c in REGISTRY if getattr(c.fn, "__module__", None) == mod_name]
    registered = {c.name: c.id for c in own}

    # 1) 有 test_* 但没登记 —— 上一版事故的直接成因
    for name in sorted(set(funcs) - set(registered)):
        problems.append(f"未登记（会被静默跳过）: {name}")

    # 2) 登记了但模块里找不到这个函数（改名/删除后忘了同步）
    for name in sorted(set(registered) - set(funcs)):
        problems.append(f"登记了不存在的函数: {name}")

    # 3) 编号重复（**全局**：跨模块重复同样致命）
    ids = [c.id for c in REGISTRY]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        problems.append(f"编号重复: {dup}")

    # 4) 函数名与编号不对应（文档会悄悄错位）
    for c in own:
        want = f"{CASE_PREFIX}{c.id.lower()}_"
        if not c.name.startswith(want):
            problems.append(f"函数名与编号不符: {c.id} -> {c.name}（应形如 {want}...）")

    return problems


def _run_one(c: Case) -> tuple[str, str, float]:
    start = time.perf_counter()
    try:
        if c.is_async:
            asyncio.run(c.fn())
        else:
            c.fn()
    except Unimplemented as e:
        return UNIMPL, str(e), time.perf_counter() - start
    except Blocked as e:
        return BLOCKED, str(e), time.perf_counter() - start
    except BaseException as e:  # noqa: BLE001 - 收集器就该抓全部，包括 SystemExit
        detail = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc().strip().splitlines()
        if tb:
            detail += "\n      " + "\n      ".join(tb[-4:])
        return FAIL, detail, time.perf_counter() - start
    return PASS, "", time.perf_counter() - start


def _select(argv: list[str]) -> tuple[list[Case], bool]:
    """支持 `--list`、`--id W01,W05`、`--only W`。"""
    if "--list" in argv:
        return [], True
    ids: set[str] | None = None
    only: str | None = None
    for i, a in enumerate(argv):
        if a == "--id" and i + 1 < len(argv):
            ids = {x.strip() for x in argv[i + 1].split(",") if x.strip()}
        elif a.startswith("--id="):
            ids = {x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()}
        elif a == "--only" and i + 1 < len(argv):
            only = argv[i + 1].strip()
        elif a.startswith("--only="):
            only = a.split("=", 1)[1].strip()

    picked = REGISTRY
    if ids is not None:
        picked = [c for c in picked if c.id in ids]
    if only:
        picked = [c for c in picked if c.id.startswith(only)]
    return list(picked), False


def _make_stdout_safe() -> None:
    """让 `print()` 在任何控制台编码下都不会抛 `UnicodeEncodeError`。

    为什么需要（2026-11-14 实测）：GitHub Actions 的 `windows-latest` 默认用
    **cp1252** 作为 stdout 编码，而用例 ID / 用例名 / 失败信息全是中文，
    `print(f"[{status}] {c.id} ...")` 会直接抛 `UnicodeEncodeError`，
    堆栈落在 `main() -> print(head)`。后果比"输出乱码"严重得多：
    明明用例跑完了，报告却变成"进程异常退出"，读者只会以为测试没跑起来。

    处理规则（分流，而不是一刀切转 UTF-8）：
    * stdout 不是终端（CI 的管道、`> log.txt`）→ 改 UTF-8：GitHub 日志按 UTF-8 渲染，
      中文正常显示；
    * stdout 是终端（Windows 老 conhost 是 cp936/cp1252）→ **不动 encoding**，
      只把错误策略改成 `backslashreplace`。硬转 UTF-8 会在 GBK 控制台上输出乱码，
      而 `\\u4e2d` 这种转义至少能看懂，且不会中断整轮测试。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被替换成非 TextIOWrapper（如某些宿主捕获器）
            continue
        try:
            if stream.isatty():
                reconfigure(errors="backslashreplace")
            else:
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):  # 已写过数据 / 流已关闭：保持原样即可
            pass


def main(module, argv: list[str] | None = None) -> int:
    _make_stdout_safe()
    argv = list(sys.argv[1:] if argv is None else argv)

    selected, listing = _select(argv)
    if listing:
        for c in REGISTRY:
            print(f"{c.id}\t{c.name}")
        print(f"共 {len(REGISTRY)} 条")
        return EXIT_OK

    # 收集期闸门先跑：数量本身不可信时，后面所有结论都不算数
    problems = check_registry(module)
    if problems:
        print("收集期不一致（exit 2）——报告里的数字不可信，先修这里：")
        for p in problems:
            print("  -", p)
        return EXIT_COLLECT

    if not selected:
        print("没有匹配的用例（--id/--only 写错了？）")
        return EXIT_COLLECT

    counts = {PASS: 0, FAIL: 0, UNIMPL: 0, BLOCKED: 0}
    failed: list[tuple[str, str]] = []
    unimplemented: list[str] = []
    blocked: list[tuple[str, str]] = []

    for c in selected:
        status, detail, cost = _run_one(c)
        counts[status] += 1
        head = f"[{status}] {c.id}  {cost:6.2f}s  {c.name}"
        if status == PASS:
            print(head)
        elif status == FAIL:
            print(head)
            print("      " + detail.replace("\n", "\n      "))
            failed.append((c.id, detail))
        elif status == UNIMPL:
            print(f"{head}   ← {detail}")
            unimplemented.append(c.id)
        else:
            print(f"{head}   ← {detail}")
            blocked.append((c.id, detail))

    total = len(selected)
    print()
    print(
        f"汇总：通过 {counts[PASS]} / {total}，失败 {counts[FAIL]}，"
        f"未实现 {counts[UNIMPL]}，阻塞 {counts[BLOCKED]}"
    )
    if unimplemented:
        print("未实现（不是通过）:", " ".join(unimplemented))
    if blocked:
        print("阻塞:", " ".join(cid for cid, _ in blocked))

    if counts[FAIL]:
        return EXIT_FAIL
    if counts[UNIMPL] or counts[BLOCKED]:
        return EXIT_INCOMPLETE
    return EXIT_OK


def selftest() -> int:
    """对收集器自己要有一把刀：喂它一个"多出未登记函数"的假模块，它必须报错。"""
    from types import ModuleType

    global REGISTRY
    saved = REGISTRY
    REGISTRY = []
    try:
        mod = ModuleType("_selftest_mod")

        @case("W01")
        def test_w01_ok() -> None:
            return None

        mod.test_w01_ok = test_w01_ok

        assert check_registry(mod) == [], check_registry(mod)

        def test_w02_ghost() -> None:
            raise AssertionError("本该跑但会被跳过")

        mod.test_w02_ghost = test_w02_ghost
        problems = check_registry(mod)
        assert any("未登记" in p for p in problems), problems
    finally:
        REGISTRY = saved
    print("收集器自检通过")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print("这是收集器模块，请运行 tests/test_contract.py")
    raise SystemExit(EXIT_COLLECT)
