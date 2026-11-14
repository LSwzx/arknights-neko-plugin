"""A 组 + X04 的变异验牙（进程内打补丁，不改任何文件）。

每个变异都必须让**指定的那条用例红**。若变异后仍绿，说明用例没压在要点上。
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_contract as T


def run(case_fn, name):
    try:
        case_fn()
    except BaseException as e:  # noqa: BLE001
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"  [杀] {name}: {type(e).__name__}: {first[:110]}")
        return True
    print(f"  [存活!] {name}")
    return False


def patch(obj, attr, factory):
    orig = getattr(obj, attr)
    setattr(obj, attr, factory(orig))
    return lambda: setattr(obj, attr, orig)


results = []

import maa.adapter as A  # noqa: E402

# ---------------------------------------------------------------- M-A1 (A09)
def m_a1(orig):
    async def connect(self, *, address, adb_path, config="General"):
        if not (address or "").strip():
            address = "127.0.0.1:5555"   # 变异：空地址悄悄回落 5555（F17 的旧 bug）
        return await orig(self, address=address, adb_path=adb_path, config=config)
    return connect


restore = patch(A.MaaAdapter, "connect", m_a1)
try:
    results.append(("M-A1 空地址回落 5555", run(T.test_a09_empty_address_never_invents_port, "A09")))
finally:
    restore()

# ---------------------------------------------------------------- M-A2 (A14)
def m_a2(orig):
    def d1(self, event):
        with self._lock:
            if (
                event.async_call_id is not None
                and event.async_call_id not in self._pending
                and len(self._pending) == 1
            ):
                sid, slot = next(iter(self._pending.items()))
                self._pending.pop(sid, None)
                if not slot.future.done():
                    slot.future.set_result(event.details)   # 变异：盲扫队列，迟到事件喂给当前等待者
        return orig(self, event)
    return d1


restore = patch(A.MaaAdapter, "_dispatch_one", m_a2)
try:
    results.append(("M-A2 迟到事件盲扫喂给当前等待者", run(T.test_a14_stale_events_do_not_satisfy_next_task, "A14")))
finally:
    restore()

# ---------------------------------------------------------------- M-A3 (A06)
def m_a3(orig):
    async def ar(self, timeout):
        got = await orig(self, timeout)
        if got.status.value == "unknown":
            return A.Resolution(A.ResolutionStatus.KNOWN, 1280, 720)  # 变异：用默认 1280x720 兜底
        return got
    return ar


restore = patch(A.MaaAdapter, "_await_resolution", m_a3)
try:
    results.append(("M-A3 分辨率默认 1280x720 兜底", run(T.test_a06_async_call_ret_true_counts_as_connected, "A06")))
finally:
    restore()

# ---------------------------------------------------------------- M-A4 (A15)
def m_a4(orig):
    import _fakelib as F
    def fetch(self, call_id):
        try:
            return orig(self, call_id)
        except A.CaptureFormatUnexpected as exc:
            return F.make_png()   # 变异：非 PNG 就二次编码兜底（旧 maa_adapter 的坑）
    return fetch


restore = patch(A.MaaAdapter, "_fetch_image", m_a4)
try:
    results.append((
        "M-A4 非 PNG 二次编码兜底",
        run(T.test_a15_non_png_magic_keeps_raw_bytes_and_never_reencodes, "A15"),
    ))
finally:
    restore()

# ---------------------------------------------------------------- M-A5 (A13)
def m_a5(orig):
    import json
    def bad(self, msg, details_json, custom_arg):
        json.loads(details_json)      # 变异：不 try/except，坏载荷直接抛穿到 C 线程
        return orig(self, msg, details_json, custom_arg)
    return bad


restore = patch(A.MaaAdapter, "_on_thread_event", m_a5)
try:
    results.append(("M-A5 坏载荷抛穿到 C 线程", run(T.test_a13_malformed_payload_is_swallowed, "A13")))
finally:
    restore()

# ---------------------------------------------------------------- M-A6 (A03)
def m_a6(orig):
    def d1(self, event):
        out = orig(self, event)
        if event.what == "Connected":
            with self._lock:
                slots = list(self._pending.values())
                self._pending.clear()
            for slot in slots:
                if not slot.future.done():
                    slot.future.set_result({"ret": True})   # 变异：收到 Connected 就当终态
        return out
    return d1


restore = patch(A.MaaAdapter, "_dispatch_one", m_a6)
try:
    results.append(("M-A6 Connected 当终态提前返回", run(T.test_a03_connected_does_not_return_early, "A03")))
finally:
    restore()

# ---------------------------------------------------------------- M-A7 (A11)
def m_a7(orig):
    def observe(self, event):
        out = orig(self, event)
        err = event.details.get("error")
        if isinstance(err, str) and err:
            self._last_error = A.CaptureFailed(call_id=-1, ret=False)  # 变异：迟到事件改写 last_error
        return out
    return observe


restore = patch(A.MaaAdapter, "_observe", m_a7)
try:
    results.append(("M-A7 迟到事件覆盖 last_error", run(T.test_a11_late_error_does_not_overwrite_timeout, "A11")))
finally:
    restore()

# ---------------------------------------------------------------- M-X1 (X04)
try:
    from maa.runtime import EngineState  # noqa: E402
    allowed = {s.value for s in EngineState}
except Exception as exc:  # noqa: BLE001 - runtime 暂时 import 不动时，用文档里的七态兜底
    print(f"  [注] maa.runtime 导入失败（{type(exc).__name__}: {exc}），改用七态字面量")
    allowed = {"absent", "downloading", "verified", "unpacked", "loaded", "ready", "failed"}
src = Path(T.__file__).with_name("..") / "maa" / "adapter.py"
orig_src = src.read_text(encoding="utf-8")
mut = orig_src.replace('STATE_ABSENT: Final[str] = "absent"', 'STATE_ABSENT: Final[str] = "unloaded"')
assert mut != orig_src, "变异没生效：没找到 STATE_ABSENT 的赋值行"
found, unresolvable, bad = T._engine_state_violations(mut, allowed)
print(f"  [{'杀' if bad else '存活!'}] M-X1 STATE_ABSENT 改成 'unloaded' -> bad={bad}")
results.append(("M-X1 状态常量改成 unloaded", bool(bad)))

mut2 = orig_src.replace(
    'raise self._fault(EngineNotReady(state=STATE_ABSENT, required=("handle",)))',
    'raise self._fault(EngineNotReady(state=_dyn_state(), required=("handle",)))',
    1,
)
found2, unresolvable2, _bad2 = T._engine_state_violations(mut2, allowed)
print(f"  [{'杀' if unresolvable2 else '存活!'}] M-X2 state 用函数调用拼 -> unresolvable={unresolvable2}")
results.append(("M-X2 state 不可静态解析", bool(unresolvable2)))

print()
print(f"变异总数 {len(results)}，杀死 {sum(1 for _n, ok in results if ok)}")
for name, ok in results:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _n, ok in results) else 1)
