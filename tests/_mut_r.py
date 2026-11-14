"""R 组变异验牙（进程内打补丁，不改文件）。只挑三条"守卫型"用例。"""
import asyncio
import importlib
import sys

import _bootstrap
import test_contract as T

# 同 `_mut_a.py`：`maa.*` 不写成 import 语句，避免 first-party 判定随 cwd 翻转。
M = importlib.import_module("maa.runtime")

# 同 `_mut_p.py`：读一次是为了让"副作用导入"不被 pyflakes 判成 F401。
PLUGIN_ROOT = _bootstrap.PLUGIN_ROOT


def run(coro_fn, name):
    import inspect
    try:
        if inspect.iscoroutinefunction(coro_fn):
            asyncio.run(coro_fn())      # R 组用例是 async def：不 await 等于什么都没跑
        else:
            coro_fn()
    except BaseException as e:  # noqa: BLE001
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"  [杀] {name}: {type(e).__name__}: {first[:110]}")
        return True
    print(f"  [存活!] {name}")
    return False


results = []

# M-R1：ensure_ready 在 DOWNLOADING 下先等一会（= "截图请求会阻塞"）
orig_ensure = M.MaaRuntime.ensure_ready


async def slow_ensure(self):
    if self.state is M.EngineState.DOWNLOADING:
        await asyncio.sleep(0.25)      # 变异：阻塞等待
    return await orig_ensure(self)


M.MaaRuntime.ensure_ready = slow_ensure
try:
    results.append(("M-R1 ensure_ready 阻塞 0.25s", run(T.test_r18_capture_fails_fast_while_downloading, "R18")))
finally:
    M.MaaRuntime.ensure_ready = orig_ensure

# M-R2：bootstrap 在返回前等下载（= 旧实现，295MB 拖死宿主）
orig_bootstrap = M.MaaRuntime.bootstrap


async def waiting_bootstrap(self, *, force=False):
    state = await orig_bootstrap(self, force=force)
    if state is M.EngineState.DOWNLOADING:
        await asyncio.sleep(1.3)       # 变异：等下载（模拟在钩子里 await 295MB）
    return state


M.MaaRuntime.bootstrap = waiting_bootstrap
try:
    results.append(("M-R2 bootstrap 返回前等下载", run(T.test_r12_bootstrap_returns_downloading_and_dedupes, "R12")))
finally:
    M.MaaRuntime.bootstrap = orig_bootstrap

# M-R3：verify_archive 变空操作（= 缺哈希/坏包静默放行）
orig_verify = M.MaaRuntime.verify_archive


async def no_verify(self, archive, *, expected_sha256):
    return None                        # 变异：跳过校验


M.MaaRuntime.verify_archive = no_verify
try:
    results.append(("M-R3 跳过哈希校验", run(T.test_r01_hash_mismatch_deletes_file, "R01")))
    results.append(("M-R4 跳过哈希校验（缺 digest）", run(T.test_r11_missing_digest_is_rejected, "R11")))
finally:
    M.MaaRuntime.verify_archive = orig_verify

print()
print(f"变异总数 {len(results)}，杀死 {sum(1 for _n, ok in results if ok)}")
for name, ok in results:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _n, ok in results) else 1)
