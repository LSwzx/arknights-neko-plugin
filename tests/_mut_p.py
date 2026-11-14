"""P 组变异验牙：只挑三条"守卫型"用例。每个变异都必须让指定用例红。"""
import importlib
import sys
from pathlib import Path

import _bootstrap
import test_contract as T

# 同 `_mut_a.py`：`maa.*` 不写成 import 语句，避免 first-party 判定随 cwd 翻转。
P = importlib.import_module("maa.provision")

# `import _bootstrap` 的价值全在 import 期副作用（把插件根塞进 sys.path），
# pyflakes 只按"名字有没有被读取"判断，所以真的读一次；CI 的 `--ignore-noqa`
# 让 `# noqa: F401` 失效，这是唯一不靠注释的写法。
PLUGIN_ROOT = _bootstrap.PLUGIN_ROOT


def run(fn, name):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"  [杀] {name}: {type(e).__name__}: {first[:110]}")
        return True
    print(f"  [存活!] {name}")
    return False


results = []

# M-P1：把"先判路径、后判排除"反过来（穿越样本被排除逻辑静默跳过）
orig_check = P._check_member_name


def lenient(name, zp, destp):
    # 变异：完全跳过穿越检测（等价于"先判排除、后判路径"的反向顺序）
    parts = tuple(p for p in name.replace("\\", "/").split("/") if p not in ("", "."))
    return parts or ("x",)


orig_dir = P._ensure_dir_no_symlink


def naive_dir(destp, parts, dest_real, zp, tdir):
    # 变异：写出阶段也不再校验（两处防线一起拆掉）
    target = Path(destp).joinpath(*parts) if parts else Path(destp)
    target.mkdir(parents=True, exist_ok=True)
    return target


P._check_member_name = lenient
P._ensure_dir_no_symlink = naive_dir
try:
    results.append(("M-P1 两处路径校验都拆掉", run(T.test_p11_path_check_precedes_exclusion, "P11")))
    results.append(("M-P1b 两处路径校验都拆掉（P01）", run(T.test_p01_malicious_member_names_rejected, "P01")))
finally:
    P._check_member_name = orig_check
    P._ensure_dir_no_symlink = orig_dir

# M-P2：哈希比较恒真（"校验了但等于没校验"）
orig_cmp = P.hmac.compare_digest
P.hmac.compare_digest = lambda a, b: True
try:
    results.append(("M-P2 哈希比较恒真", run(T.test_p16_hash_mismatch_deletes_dest_and_part, "P16")))
finally:
    P.hmac.compare_digest = orig_cmp

# M-P3：取消归到 network（用户会看到"网络错误"并反复重试）
orig_dl = P.atomic_download


def wrong_cancel(release, dest, **kw):
    try:
        return orig_dl(release, dest, **kw)
    except P.EngineDownloadFailed as exc:
        if getattr(getattr(exc, "kind", None), "value", "") == "cancelled":
            raise P.EngineDownloadFailed(
                url=release.url, target=Path(dest),
                kind=P.DownloadFailure.NETWORK, detail="cancelled",
            ) from None
        raise


P.atomic_download = wrong_cancel
try:
    results.append(("M-P3 取消归成 network", run(T.test_p25_cancel_leaves_no_residue, "P25")))
finally:
    P.atomic_download = orig_dl

print()
print(f"变异总数 {len(results)}，杀死 {sum(1 for _n, ok in results if ok)}")
for name, ok in results:
    print(f"  {'✓' if ok else '✗'} {name}")
sys.exit(0 if all(ok for _n, ok in results) else 1)
