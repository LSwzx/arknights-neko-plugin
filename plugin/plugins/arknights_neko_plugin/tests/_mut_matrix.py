"""§4 变异矩阵的进程内验牙盘（**不改仓库里的 `maa/`**）。

做法：把模块源码读进内存 → 字符串替换出变异体 → `exec` 成新模块对象换掉
`sys.modules["maa.xxx"]`（同时补上 `maa` 包上的属性，否则 `from maa import x`
会拿到旧对象）→ 跑**文档指定**的用例 → 还原。

为什么整条链一起重建：`maa.wrapper ← maa.adapter ← maa.runtime ← maa.provision`
之间是导入期取值的，只换一个模块会让另一层继续引用旧对象，出现"假存活"。

用法：
    python tests/_mut_matrix.py            # 跑全部已实施的变异
    python tests/_mut_matrix.py M02 M31    # 只跑指定几条
    python tests/_mut_matrix.py --list     # 只看登记表

退出码：0 = 全部被杀；1 = 有存活；2 = 有变异的锚点在源码里找不到（变异本身失效）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import test_contract as T  # noqa: E402
import _runner as R  # noqa: E402

PATHS = {
    "maa.errors": ROOT / "maa" / "errors.py",
    "maa.wrapper": ROOT / "maa" / "wrapper.py",
    "maa.provision": ROOT / "maa" / "provision.py",
    "maa.adapter": ROOT / "maa" / "adapter.py",
    "maa.runtime": ROOT / "maa" / "runtime.py",
}
CHAIN = list(PATHS)
BY_ID = {c.id: c.fn for c in R.REGISTRY}


# --------------------------------------------------------------------------- #
# 变异载体
# --------------------------------------------------------------------------- #
class AnchorMissing(AssertionError):
    pass


def _exec_module(name: str, source: str, path: Path):
    spec = importlib.util.spec_from_loader(name, loader=None, origin=str(path))
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(path)
    mod.__package__ = name.rpartition(".")[0]
    sys.modules[name] = mod
    exec(compile(source, str(path), "exec"), mod.__dict__)  # noqa: S102 - 这就是变异器本体
    pkg = sys.modules.get("maa")
    if pkg is not None:
        setattr(pkg, name.rpartition(".")[1], mod)
    return mod


def build_chain(overrides: dict[str, list[tuple[str, str]]]):
    """按依赖顺序重建整条链；`overrides` 里给的替换必须**唯一命中**，否则抛 AnchorMissing。"""
    saved = {name: sys.modules.get(name) for name in CHAIN}
    pkg = sys.modules.get("maa")
    saved_attrs = (
        {n.rpartition(".")[1]: getattr(pkg, n.rpartition(".")[1], None) for n in CHAIN}
        if pkg is not None
        else {}
    )
    try:
        for name in CHAIN:
            source = PATHS[name].read_text(encoding="utf-8")
            for old, new in overrides.get(name, ()):
                hits = source.count(old)
                if hits != 1:
                    raise AnchorMissing(
                        f"{name} 的变异锚点命中 {hits} 次（必须 1 次）：{old[:70]!r}"
                    )
                source = source.replace(old, new, 1)
            _exec_module(name, source, PATHS[name])
    finally:
        pass
    return saved, saved_attrs, pkg


def restore(saved, saved_attrs, pkg):
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    if pkg is not None:
        for short, mod in saved_attrs.items():
            if mod is None:
                if hasattr(pkg, short):
                    delattr(pkg, short)
            else:
                setattr(pkg, short, mod)


# --------------------------------------------------------------------------- #
# 运行 + 判红
# --------------------------------------------------------------------------- #
def run_case(fn) -> tuple[bool, str]:
    """返回 `(是否变红, 一句话原因)`。R 组多为 `async def`：**必须 await**，
    否则只创建一个协程对象、什么都没跑 —— 那是"假存活"的经典来源。"""
    try:
        if inspect.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
    except BaseException as e:  # noqa: BLE001
        first = str(e).splitlines()[0] if str(e) else ""
        return True, f"{type(e).__name__}: {first[:150]}"
    return False, ""


# --------------------------------------------------------------------------- #
# 变异登记表：`patches` 为 {模块: [(旧, 新), …]}；`targets` 为文档指定的用例编号
# --------------------------------------------------------------------------- #
MUTATIONS: list[dict] = [
    dict(
        id="M02", desc="缓冲数组 c_ubyte → c_byte（字节 >127 变负数）",
        patches={"maa.wrapper": [("(ctypes.c_ubyte * size)()", "(ctypes.c_byte * size)()")]},
        targets=["W03", "W04"],
    ),
    dict(
        id="M03", desc="bytes(buf[:got]) → bytes(buf)（忽略 got）",
        patches={"maa.wrapper": [("return bytes(buffer[:got])", "return bytes(buffer)")]},
        targets=["W03", "W19"],
    ),
    dict(
        id="M04", desc="哨兵判定挪到 0 < got <= buff_size 之后",
        patches={"maa.wrapper": [
            ("            if got == sentinel:",
             "            if got == sentinel and 0 < got <= size:  # 变异：哨兵退到成功窗之后"),
        ]},
        # 文档写的是 W05/W10，实测是**文档选错受害用例**：W05 的哨兵 deliberately
        # 落在 (0, buffer] 内（12345），两种顺序走同一分支；W10 的 got==0 也走不到哨兵。
        # 真正被这个变异改掉的行为是"不再倍增重试"→ 受害的是 W07/W08。
        targets=["W07", "W08"],
    ),
    dict(
        id="M05", desc="删除 got > buffer_size 的防御分支",
        patches={"maa.wrapper": [("            if got > size:", "            if False:  # 变异")]},
        targets=["W20"],
    ),
    dict(
        id="M06", desc="去掉 MAX_IMAGE_BUFFER_BYTES 夹逼（单点，实测等价）",
        patches={"maa.wrapper": [
            ("            if size > self._max_image_buffer_bytes:",
             "            if False:  # 变异：去掉字节上限"),
        ]},
        targets=["W09"],
    ),
    dict(
        id="M06b", desc="两处预算判定一起拆（真回归的形态）",
        patches={"maa.wrapper": [
            ("            if size > self._max_image_buffer_bytes:",
             "            if False:  # 变异：去掉字节上限"),
            ("                if size * 2 > self._max_image_buffer_bytes:",
             "                if False:  # 变异：去掉倍增预算"),
        ]},
        targets=["W09"],
    ),
    dict(
        id="M07", desc="重试次数上限 +1（多倍增一次）",
        patches={"maa.wrapper": [
            ("                if grows >= self._max_buffer_grow:",
             "                if grows > self._max_buffer_grow:  # 变异"),
        ]},
        targets=["W08"],
    ),
    dict(
        id="M08", desc="删除 got == 0 分支（并入成功路径）",
        patches={"maa.wrapper": [("            if got == 0:", "            if False:  # 变异")]},
        targets=["W10"],
    ),
    dict(
        id="M09", desc="AsstGetImage.restype 写成 c_size_t（实测等价：64 位平台上同宽）",
        patches={"maa.wrapper": [
            ('            ("AsstGetImage", ctypes.c_uint64,',
             '            ("AsstGetImage", ctypes.c_size_t,'),
        ]},
        targets=["W12", "W14"],
    ),
    dict(
        id="M09b", desc="AsstGetImage.restype 写成 c_uint32（真截断）",
        patches={"maa.wrapper": [
            ('            ("AsstGetImage", ctypes.c_uint64,',
             '            ("AsstGetImage", ctypes.c_uint32,'),
        ]},
        targets=["W12", "W14"],
    ),
    dict(
        id="M10", desc="空句柄不预检，直接调 AsstGetImage",
        patches={"maa.wrapper": [
            ("        if not handle:\n            # 空句柄是",
             "        if False:  # 变异：不预检空句柄\n            # 空句柄是"),
        ]},
        targets=["W11"],
    ),
    dict(
        id="M47", desc="adapter 第一层白名单删掉",
        patches={"maa.adapter": [
            ("        if task_type not in READONLY_TASK_TYPES:",
             "        if False:  # 变异：拆掉 adapter 层白名单"),
        ]},
        targets=["W26"],
    ),
    dict(
        id="M48", desc="READONLY_TASK_TYPES 补进 Infrast（写任务被放行）",
        patches={"maa.adapter": [
            ('    "Screencap", "OperBox", "Depot", "Debug",',
             '    "Screencap", "OperBox", "Depot", "Debug", "Infrast",'),
        ]},
        targets=["W26", "W27"],
    ),
    dict(
        id="M22", desc="去掉 bootstrap 的 in-flight 复用（每次都取消重来）",
        patches={"maa.runtime": [
            ("            if not force:\n                return self._state",
             "            if False:  # 变异：不复用 in-flight\n                return self._state"),
        ]},
        targets=["R05"],
    ),
    dict(
        id="M29", desc="purge() 只删归档不删解压产物",
        patches={"maa.runtime": [
            ("            elif self._engine_root.exists():\n"
             "                shutil.rmtree(self._engine_root, ignore_errors=True)",
             "            elif self._engine_root.exists():\n"
             "                pass  # 变异：不删解压树"),
        ]},
        targets=["R14"],
    ),
    dict(
        id="M30", desc="RuntimeLayout.resolve() 只搜 engine_root 一层",
        patches={"maa.runtime": [
            ('        names = MaaCoreWrapper.default_library_names()\n'
             "        searched = tuple(\n"
             "            directory / name\n"
             '            for directory in (root, root / "bin", root / "lib")',
             "        names = MaaCoreWrapper.default_library_names()\n"
             "        searched = tuple(\n"
             "            directory / name\n"
             "            for directory in (root,)  # 变异：只搜一层"),
        ]},
        targets=["R15"],
    ),
    dict(
        id="M31", desc="禁止迁移不抛 EngineNotReady（静默 return）",
        patches={"maa.runtime": [
            ("            raise EngineNotReady(state=self._state.value, required=(new.value,))",
             "            return  # 变异：禁止迁移静默跳过"),
        ]},
        targets=["R16"],
    ),
    dict(
        id="M-P4", desc="拆掉压缩比判据（四条 LIMIT_EXCEEDED 共用 kind，只有对照组能分辨）",
        patches={"maa.provision": [
            ("            if declared_selected / zip_size > MAX_COMPRESSION_RATIO:",
             "            if False:  # 变异：拆掉压缩比判据"),
        ]},
        targets=["P04"],
    ),
    dict(
        id="M35", desc="哈希不匹配时不删目标（实测等价：下载前的预清已删过）",
        patches={"maa.provision": [
            ("                _unlink_quietly(target)\n                raise EngineHashMismatch(",
             "                raise EngineHashMismatch("),
        ]},
        targets=["P16"],
    ),
    dict(
        id="M35b", desc="下载前不预清哈希不符的旧归档（真回归的形态）",
        patches={"maa.provision": [
            ('        if target.exists() or target.is_symlink():\n'
             '            # 存在但哈希不对：按"不可信"处理，先删（§3.5）。\n'
             '            logger.warning("删除哈希不符的旧归档: %s", target)\n'
             "            _unlink_quietly(target)",
             "        if False:  # 变异：旧归档原样留着\n"
             '            logger.warning("删除哈希不符的旧归档: %s", target)\n'
             "            _unlink_quietly(target)"),
        ]},
        targets=["P16"],
    ),
    dict(
        id="M36", desc="复用只判 dest.exists()、不复核哈希",
        patches={"maa.provision": [
            ("    if not path.is_file():\n        return False\n    try:\n"
             "        verify_sha256(path, release.sha256, version=release.version)\n"
             "    except EngineHashMismatch:\n        return False\n    return True",
             "    return path.is_file()  # 变异：只判存在"),
        ]},
        targets=["P22"],
    ),
    dict(
        id="M35c", desc="两处删除一起拆（预清 + 哈希不符兜底）—— 真回归的形态",
        patches={"maa.provision": [
            ("                _unlink_quietly(target)\n                raise EngineHashMismatch(",
             "                raise EngineHashMismatch("),
            ("        if target.exists() or target.is_symlink():",
             "        if False:  # 变异：旧归档原样留着"),
        ]},
        targets=["P16"],
    ),
    dict(
        id="M38", desc="atomic_download 去掉 .lock 互斥",
        patches={"maa.provision": [
            ("    with _exclusive_lock(lock_path, url=url, target=target):",
             "    with contextlib.nullcontext():  # 变异：去掉互斥"),
        ]},
        targets=["P20"],
    ),
    dict(
        id="M39", desc="清理写成 except Exception（漏掉 KeyboardInterrupt）",
        patches={"maa.provision": [
            ("        finally:\n"
             "            # 唯一清理点。成功时 part 已被 os.replace 吃掉 → no-op；\n"
             "            # 其余所有路径（含 CancelledError / KeyboardInterrupt）都删干净。\n"
             "            part.unlink(missing_ok=True)",
             "        except Exception:  # 变异：只兜 Exception\n"
             "            part.unlink(missing_ok=True)\n"
             "            raise"),
        ]},
        targets=["P19"],
    ),
    dict(
        id="M40", desc="ExtractResult 不记 skipped / skipped_sample",
        patches={"maa.provision": [
            ("            skipped=skipped,", "            skipped=0,  # 变异"),
            ("            skipped_sample=tuple(skipped_sample),",
             "            skipped_sample=(),  # 变异"),
        ]},
        targets=["P10"],
    ),
    dict(
        id="M41", desc="on_progress 异常打断 finally 清理",
        patches={"maa.provision": [
            ("            # 唯一清理点。成功时 part 已被 os.replace 吃掉 → no-op；\n"
             "            # 其余所有路径（含 CancelledError / KeyboardInterrupt）都删干净。\n"
             "            part.unlink(missing_ok=True)",
             "            # 变异：异常在飞时跳过清理（模拟回调异常打断 finally）\n"
             "            import sys as _sys\n"
             "            if _sys.exc_info()[0] is None:\n"
             "                part.unlink(missing_ok=True)"),
        ]},
        targets=["P26", "P19"],
    ),
]


def main(argv: list[str]) -> int:
    wanted = [a for a in argv if not a.startswith("-")]
    if "--list" in argv:
        for m in MUTATIONS:
            print(f"{m['id']}\t{','.join(m['targets'])}\t{m['desc']}")
        print(f"共 {len(MUTATIONS)} 条")
        return 0

    selected = [m for m in MUTATIONS if not wanted or m["id"] in wanted]
    killed = 0
    alive: list[str] = []
    broken: list[str] = []

    for m in selected:
        print(f"\n[{m['id']}] {m['desc']}")
        try:
            saved, saved_attrs, pkg = build_chain(m["patches"])
        except AnchorMissing as e:
            print(f"  [坏变异] 锚点失效：{e}")
            broken.append(m["id"])
            continue
        try:
            hit = False
            for cid in m["targets"]:
                fn = BY_ID.get(cid)
                if fn is None:
                    print(f"  [坏变异] 用例 {cid} 未登记")
                    broken.append(m["id"])
                    break
                red, why = run_case(fn)
                print(f"  {'[杀]' if red else '[存活]'} {cid}: {why or '仍然绿'}")
                hit = hit or red
            if hit:
                killed += 1
            else:
                alive.append(f"{m['id']}（指定用例全绿：{','.join(m['targets'])}）")
        finally:
            restore(saved, saved_attrs, pkg)

    total = len(selected)
    print(f"\n变异总数 {total}，杀死 {killed}，存活 {len(alive)}，坏变异 {len(broken)}")
    for a in alive:
        print(f"  ✗ 存活 {a}")
    for b in broken:
        print(f"  ! 锚点失效 {b}")
    return 0 if killed == total else (2 if broken else 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
