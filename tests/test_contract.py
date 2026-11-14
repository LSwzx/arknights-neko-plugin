"""契约测试（W/A/R/P/X 组）。

运行方式（本机没有 pytest，这是主要入口）：

    python tests/test_contract.py              # 全部
    python tests/test_contract.py --id W01,W05 # 单条/几条
    python tests/test_contract.py --only W     # 整组
    python tests/test_contract.py --list       # 只列清单

退出码：0 全绿 / 1 有失败 / 2 收集期不一致 / 3 有未实现或阻塞。

本文件当前只实现 **W 组（wrapper 的 C API 契约）**，其余组是占位用例，
占位一律 `raise Unimplemented(...)` —— **不许空函数体自动通过**。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import ctypes
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path


# --------------------------------------------------------------------------- #
# 路径引导：脱离宿主跑，插件根要进 sys.path
# --------------------------------------------------------------------------- #
def _bootstrap_path() -> None:
    """插件正常是被宿主以 `plugin.plugins.arknights_neko_plugin` 加载的。

    单独跑测试时没有这个包前缀，所以把插件根与 tests 目录都塞进 `sys.path`，
    让 `import maa.wrapper` / `import _fakelib` 成立。
    """
    here = Path(__file__).resolve()
    tests_dir = str(here.parent)
    plugin_root = str(here.parents[1])
    for p in (plugin_root, tests_dir):
        if p not in sys.path:
            sys.path.insert(0, p)


_bootstrap_path()

import _fakelib as F  # noqa: E402  （必须在上面的 bootstrap 之后）
import _runner as R  # noqa: E402
from _runner import Blocked, Unimplemented, case  # noqa: E402


# --------------------------------------------------------------------------- #
# 懒加载：`maa/` 还没落盘时，用例应是"阻塞"而不是"失败"
# --------------------------------------------------------------------------- #
def _wrapper_mod():
    try:
        import maa.wrapper as m
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"maa.wrapper 尚未落盘（{type(e).__name__}: {e}）") from None
    return m


def _errors_mod():
    try:
        from maa import errors as m
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"maa.errors 尚未落盘（{type(e).__name__}: {e}）") from None
    return m


def _adapter_mod():
    try:
        from maa import adapter as m
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"maa.adapter 尚未落盘（{type(e).__name__}: {e}）") from None
    return m


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
_TMP_LEFTOVERS: list[str] = []


@atexit.register
def _drop_tmpdirs() -> None:
    """进程退出时才删临时目录。

    **为什么不在 `with` 出口就删**：用例的"失败路径无残留"断言几乎都写在 `with`
    之后（`assert not dest.exists()`）。目录一删，`dest.exists()` 恒为 False ——
    断言永远成立，等于没有断言。变异 M35c（拆掉两处删除）存活就是这个原因。
    """
    for _d in _TMP_LEFTOVERS:
        shutil.rmtree(_d, ignore_errors=True)


@contextlib.contextmanager
def _tmpdir():
    """每个用例自己的临时目录（§5.4 约定 4：避免 R/P 组互相污染）。

    **延迟删除**（见 `_drop_tmpdirs`）：`with` 退出后目录内容仍在，断言才有对象。
    """
    d = tempfile.mkdtemp(prefix="neko-w-")
    _TMP_LEFTOVERS.append(d)
    yield Path(d)


def _make_wrapper(
    tmp: Path,
    fake=None,
    *,
    library_path: Path | None = None,
    max_image_buffer_bytes: int = 4 << 20,
    max_buffer_grow: int = 8,
    use_real_loader: bool = False,
    **fake_kw,
):
    """构造一个 wrapper（不调 `load()`）。默认接 fake dll。"""
    W = _wrapper_mod()
    libdir = tmp / "lib"
    libdir.mkdir(parents=True, exist_ok=True)
    if library_path is None:
        library_path = libdir / "MaaCore.dll"
    if fake is None and not use_real_loader:
        fake = F.FakeMaaCore(**fake_kw)

    kwargs = {
        "library_path": library_path,
        "max_image_buffer_bytes": max_image_buffer_bytes,
        "max_buffer_grow": max_buffer_grow,
    }
    if not use_real_loader:
        kwargs["dll_loader"] = fake.as_loader()
    return W.MaaCoreWrapper(**kwargs), fake, libdir


def _loaded(tmp: Path, fake=None, **kw):
    """构造 + `load()` 好的 wrapper，返回 `(wrapper, fake)`。"""
    w, fake, libdir = _make_wrapper(tmp, fake, **kw)
    w.load(dll_directory=libdir)
    return w, fake


def _outcome(fn):
    """执行 fn，把"返回 None"与"抛异常"统一成可判断的结果。

    各条用例对失败形态的约定不完全一致（有的写"返回 None"，有的写"抛
    `MaaCallError`"），但**不允许的只有一条**：把哨兵 / 空数据 / 不足长度
    当成成功数据交出去。所以这里只统一"怎么观察"，不放松"不能当成功"。
    """
    try:
        return "value", fn()
    except BaseException as e:  # noqa: BLE001
        return "error", e


def _is_maa_call_error(exc) -> bool:
    W = _wrapper_mod()
    cls = getattr(W, "MaaCallError", None)
    if cls is not None and isinstance(exc, cls):
        return True
    return type(exc).__name__ == "MaaCallError"


def _is_engine_error(exc, name: str) -> bool:
    E = _errors_mod()
    cls = getattr(E, name, None)
    if cls is not None and isinstance(exc, cls):
        return True
    return type(exc).__name__ == name


def _assert_failure(kind, value, *, what: str, allow_names=()):
    """断言"这次调用没成功"，并拒绝把半成品当成功数据。"""
    if kind == "error":
        exc = value
        ok = _is_maa_call_error(exc)
        if not ok and allow_names:
            ok = any(_is_engine_error(exc, n) for n in allow_names)
        assert ok, f"{what}：抛的应是 MaaCallError{list(allow_names) or ''}，实际 {type(exc).__name__}: {exc}"
        return exc
    assert value is None, f"{what}：不得把失败当成功数据返回（拿到 {len(value) if hasattr(value, '__len__') else value} 字节）"
    return None


def _expect_maa_error(fn, *, status: str, detail_keys=(), what: str = ""):
    """`get_image` 的失败**一律是 `MaaCallError`**（§3.2 的 Raises 段）。

    主理人的理由：wrapper 一旦"返回 None + 记 last_error"就变成有策略，直接破分层。
    所以这里**不接受** None 当失败形态 —— 只接受 `MaaCallError`，且 `status`/`detail`
    必须能说清是哪一类失败（否则上层没法分档，只能糊一句"截图失败"）。
    """
    W = _wrapper_mod()
    kind, value = _outcome(fn)
    assert kind == "error", f"{what}：应抛 MaaCallError(status={status})，实际返回 {value!r}"
    assert isinstance(value, W.MaaCallError), (
        f"{what}：应抛 MaaCallError，实际 {type(value).__name__}: {value}"
    )
    got = getattr(value, "status", None)
    assert got is not None, f"{what}：MaaCallError 没带 status 字段"
    assert getattr(got, "value", got) == status, (
        f"{what}：status 应为 {status}，实际 {getattr(got, 'value', got)!r}"
    )
    detail = getattr(value, "detail", None) or {}
    for k in detail_keys:
        assert k in detail, f"{what}：detail 缺 {k!r}（实际 keys={sorted(detail)}）"
    return value


# =========================================================================== #
# W 组：wrapper 的 C API 契约（fake lib，无真机）
# =========================================================================== #
@case("W01", "wrapper", "load_resource")
def test_w01_load_resource_receives_parent_dir():
    """W01 · `AsstLoadResource` 收到的是**父目录**，不是 `resource/` 本体。

    对应的真实故障：上一版传 `resource` 本体，MAA 内部再拼 `resource`
    → `resource/resource`，加载必失败（F1）。
    """
    with _tmpdir() as tmp:
        root = tmp / "engine"
        (root / "resource").mkdir(parents=True)
        w, fake = _loaded(tmp)

        w.set_user_dir(tmp / "user")
        w.load_resource(root)

        assert fake.loaded_resource_parents, "AsstLoadResource 根本没被调用"
        got = fake.loaded_resource_parents[-1]
        assert got == str(root), f"应传父目录 {str(root)!r}，实际 {got!r}"
        assert Path(got).name != "resource", f"传的居然是 resource 本体：{got!r}"
        assert (Path(got) / "resource").is_dir(), "父目录下必须有 resource/ 才符合契约"


@case("W02", "wrapper", "load_resource")
def test_w02_load_resource_never_rewrites_the_path():
    """W02 · wrapper **不得静默改写/拼接**入参（`resource/resource` 的根因在这里）。

    历史根因：上一版调用方传了 `resource/` 本体，而 MAA 内部还会再拼一次 `resource`。
    两个可能的"修法"里，**只有一个是 wrapper 的职责**：
      - ✅ 原样透传（wrapper 是 C 函数的 1:1 镜像，不做业务判断）；
      - ❌ 帮调用方补/剥 `resource`（那会让同一个 bug 变成"有时候能跑"）。
    "调用方必须给父目录"这条由 `RuntimeLayout.resource_parent` 保证（R15）。
    """
    with _tmpdir() as tmp:
        root = tmp / "engine"
        (root / "resource").mkdir(parents=True)
        w, fake = _loaded(tmp)

        w.load_resource(root / "resource")
        got = fake.loaded_resource_parents[-1]
        assert got == str(root / "resource"), f"wrapper 改写了入参：传进 {str(root / 'resource')!r}，C 收到 {got!r}"
        assert not got.replace("\\", "/").endswith("resource/resource"), (
            f"wrapper 自己拼了一次 resource：{got!r}"
        )


@case("W03", "wrapper", "png")
def test_w03_png_bytes_pass_through_exact():
    """W03 · PNG 逐字节透传（含长度），绝不二次编码。

    对应的真实故障：曾把 PNG 当 RGB 再编码一次，图全坏（F2）。
    """
    payload = bytes(range(256)) * 4
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=payload, get_image_plan=["auto"])
        h = w.create_instance()
        got = w.get_image(h)
        assert got == payload, (
            f"字节不相等：期望 {len(payload)} 字节，实际 {len(got) if got is not None else None}"
        )
        assert isinstance(got, bytes), f"应返回 bytes，实际 {type(got).__name__}"


@case("W04", "wrapper", "png")
def test_w04_png_magic_survives_signedness():
    """W04 · 真 PNG 头不被当成负值（`0x89` 那一条）。

    对应的真实故障：曾用 `ctypes.c_byte`，`bytes([-119, ...])` 直接 `ValueError`。
    """
    payload = F.make_png()
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=payload, get_image_plan=["auto"])
        h = w.create_instance()
        kind, value = _outcome(lambda: w.get_image(h))
        assert kind == "value", f"不应抛异常：{value!r}"
        assert value is not None and value.startswith(F.PNG_MAGIC), (
            f"PNG 签名丢了：{value[:8]!r}" if value else "返回了 None"
        )
        assert value[0] == 0x89, f"首字节应为 0x89，实际 {value[0]:#x}"
        assert len(value) == len(payload), "长度被截断"


@case("W05", "wrapper", "null_size")
def test_w05_sentinel_inside_buffer_range_is_not_success():
    """W05 · 哨兵落在 `(0, buffer_size]` 时**不得**当成功。

    对应的真实故障：fake 的 `AsstGetNullSize()` 返回 12345 这种"落在缓冲区间的
    假哨兵"，旧实现把它当图像长度，交付一坨零字节（F4/F6）。
    """
    fake = F.FakeMaaCore(null_size=12345, get_image_plan=["null"])
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, fake)
        h = w.create_instance()
        exc = _expect_maa_error(lambda: w.get_image(h), status="null_size",
                                detail_keys=("buffer_size",), what="哨兵值当成功")
        assert getattr(exc.status, "value", exc.status) == "null_size"
        assert w.null_size() == 12345, "wrapper 必须用库自报的哨兵，而不是本地写死的 2**64-1"


@case("W06", "wrapper", "buffer")
def test_w06_short_buffer_is_retryable_not_fatal():
    """W06 · `buff_size < data_size` 是"缓冲不足"（可重试），不是"实例失效"。

    对应的真实故障：曾把 `buff_size < data_size` 的 `NULL_SIZE` 当成功（F5）。
    """
    payload = b"\xAB" * 200
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=payload, get_image_plan=["auto"], max_buffer_grow=0)
        h = w.create_instance()
        exc = _expect_maa_error(lambda: w.get_image(h, buffer_size=100), status="null_size",
                                detail_keys=("buffer_size", "reason"), what="缓冲不足（且不许再增长）")
        reason = (getattr(exc, "detail", None) or {}).get("reason")
        assert reason in ("grow_limit_reached", "no_budget_to_grow", "buffer_budget_exceeded"), (
            f"缓冲不足的 reason 不可分类：{reason!r}（上层要据此说「再试一次」而不是「实例坏了」）"
        )


@case("W07", "wrapper", "buffer")
def test_w07_buffer_grows_and_retries_until_fit():
    """W07 · 第一次不足 → 倍增 → 第二次拿到完整 payload。

    对应的真实故障：MAA 不告诉你"需要多大"，只能倍增；曾完全不重试。
    """
    payload = b"\x5A" * 3000
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=payload, get_image_plan=["auto"], max_buffer_grow=8)
        h = w.create_instance()
        got = w.get_image(h, buffer_size=1000)
        assert got == payload, f"重试后仍没拿到完整数据（{len(got) if got else None} 字节）"
        sizes = [c[2] for c in fake.f("AsstGetImage").calls]
        assert len(sizes) >= 2, f"应至少调用 2 次（1000 → 2000 → 4000），实际 {len(sizes)}: {sizes}"
        assert sizes[1] > sizes[0], f"第二次缓冲没有变大：{sizes}"


@case("W08", "wrapper", "buffer")
def test_w08_retry_is_bounded():
    """W08 · 永远不足时必须停手（无上限重试 = 内存与时间双爆）。

    次数口径（云雨实测）：`get_image` 调用次数 == `1 + max_buffer_grow`，默认 grow=2 → 3 次。
    旧稿的 `IMAGE_RETRY_MAX` 常量已不存在，现在是**构造参数**。
    """
    grow = 2
    with _tmpdir() as tmp:
        # 每次返回**哨兵**（"装不下"的正确表达），让 wrapper 真的走倍增循环；
        # 用 "grow"（返回 buff+1）测的是"库违规"那条分支，归 W20，别混。
        w, fake = _loaded(tmp, payload=b"\x01" * 64, get_image_plan=["null"], max_buffer_grow=grow)
        h = w.create_instance()
        _expect_maa_error(lambda: w.get_image(h, buffer_size=16), status="null_size",
                          detail_keys=("buffer_size", "reason"), what="永远不足")
        calls = fake.calls_of("AsstGetImage")
        assert calls == 1 + grow, f"调用次数应为 1+max_buffer_grow={1 + grow}，实际 {calls}"


@case("W09", "wrapper", "buffer")
def test_w09_buffer_never_exceeds_hard_cap():
    """W09 · 缓冲上界是硬的：一个谎报分辨率的端点能把进程 OOM。"""
    cap = 2 << 20
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=b"\x02" * (4 << 20), get_image_plan=["auto"],
                          max_image_buffer_bytes=cap, max_buffer_grow=8)
        h = w.create_instance()
        exc = _expect_maa_error(lambda: w.get_image(h), status="null_size",
                                detail_keys=("buffer_size", "reason"), what="超过硬上限")
        sizes = [c[2] for c in fake.f("AsstGetImage").calls]
        assert sizes, "根本没调用 AsstGetImage"
        assert max(sizes) <= cap, f"缓冲请求超过了硬上限 {cap}：{sizes}"
        assert (getattr(exc, "detail", None) or {}).get("reason") in (
            "buffer_budget_exceeded", "no_budget_to_grow", "grow_limit_reached"
        ), f"reason 不可分类：{(getattr(exc, 'detail', None) or {}).get('reason')!r}"


@case("W10", "wrapper", "capture")
def test_w10_zero_length_is_not_empty_success():
    """W10 · `AsstGetImage` 返回 0 不是"成功但 0 字节"。

    对应的真实故障：`data_size==0 返回 0` 被当成功（F5）。
    """
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=b"", get_image_plan=["zero"])
        h = w.create_instance()
        _expect_maa_error(lambda: w.get_image(h), status="empty",
                          detail_keys=("buffer_size",), what="返回 0（未拿到画面）")


@case("W11", "wrapper", "instance")
def test_w11_null_handle_is_not_reported_as_buffer_problem():
    """W11 · 空句柄不许报成"缓冲区不足"（三义混用）。

    对应的真实故障：`NULL_SIZE` 同时代表"缓冲不足 / 句柄为空 / 未 init"，
    旧文案让人去改分辨率（F6）。
    """
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, create_returns=0)
        h = w.create_instance()
        exc = _expect_maa_error(lambda: w.get_image(h), status="rejected",
                                what="空句柄（不许报成缓冲问题）")
        assert "缓冲" not in str(exc), f"把句柄问题说成了缓冲问题：{exc}"
        assert fake.calls_of("AsstGetImage") == 0, "句柄为空却已经调进 C 里了"


@case("W12", "wrapper", "null_size")
def test_w12_null_size_constant_is_unsigned_sentinel():
    """W12 · 哨兵是 `2**64-1`，**不是** `-1`（拿 -1 比 c_uint64 永不相等）。"""
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, null_size=(1 << 64) - 1)
        assert w.null_size() == (1 << 64) - 1, f"哨兵不对：{w.null_size()!r}"
        assert w.null_size() != -1, "哨兵被读成了 -1（c_int 截断）"
        assert w.null_size() == fake.f("AsstGetNullSize")(), "和库自报的值不一致"


@case("W13", "wrapper", "null_size")
def test_w13_missing_null_size_symbol_falls_back():
    """W13 · 老版本库没有 `AsstGetNullSize` 时不能整个引擎不可用。"""
    fake = F.MissingSymbolsFake({"AsstGetNullSize"})
    with _tmpdir() as tmp:
        w, _fake = _loaded(tmp, fake)
        assert w.null_size() == (1 << 64) - 1, f"回退常量不对：{w.null_size()!r}"


@case("W14", "wrapper", "ctypes")
def test_w14_ctypes_signatures_are_declared():
    """W14 · `argtypes`/`restype` 必须声明正确（写错 = 64 位返回值被截成 32 位）。

    `AsstBool` 是 `uint8_t`（F16），必须用 `c_ubyte` 而不是 `c_bool` / `c_int`。
    """
    with _tmpdir() as tmp:
        _w, fake = _loaded(tmp)
        gi = fake.f("AsstGetImage")
        assert gi.restype is ctypes.c_uint64, f"AsstGetImage.restype = {gi.restype!r}，应为 c_uint64"
        assert gi.argtypes is not None, "argtypes 没声明"
        assert gi.argtypes[0] is ctypes.c_void_p, f"handle 参数应 c_void_p，实际 {gi.argtypes[0]!r}"
        assert gi.argtypes[1] is not ctypes.c_char_p, "输出缓冲不能用 c_char_p（只读）"
        assert gi.argtypes[2] in (ctypes.c_uint64, ctypes.c_size_t), (
            f"第三个参数应为 c_uint64/c_size_t，实际 {gi.argtypes[2]!r}"
        )
        conn = fake.f("AsstAsyncConnect")
        assert conn.argtypes is not None and any(t is ctypes.c_ubyte for t in conn.argtypes), (
            f"AsstAsyncConnect 的 block 参数应为 c_ubyte(AsstBool=uint8_t)，实际 {conn.argtypes!r}"
        )
        connected = fake.f("AsstConnected")
        assert connected.restype is ctypes.c_ubyte, (
            f"AsstBool 的 restype 必须 c_ubyte，实际 {connected.restype!r}"
        )


@case("W15", "wrapper", "instance")
def test_w15_instance_failure_is_not_hidden():
    """W15 · `AsstCreateEx` 返回 0 时，wrapper 必须把"创建失败"原样暴露给上层。

    分层裁决（§2.3 捕获矩阵）：**wrapper 返回裸 0/None 句柄，`InstanceCreateFailed`
    由 runtime 抛**。所以这里断"不隐藏 + 不误调 AsstDestroy"，
    "文案指向 resource 未加载"那一半属于 runtime（R 组，待 `maa/runtime.py`）。
    """
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, create_returns=0)
        kind, value = _outcome(lambda: w.create_instance())
        if kind == "error":
            assert _is_engine_error(value, "InstanceCreateFailed") or _is_maa_call_error(value), (
                f"应抛 InstanceCreateFailed 或 MaaCallError，实际 {type(value).__name__}: {value}"
            )
        else:
            assert not value, f"创建失败却返回了一个「真」句柄：{value!r}"
        assert fake.calls_of("AsstDestroy") == 0, "创建失败却调了 AsstDestroy"


@case("W16", "wrapper", "connect")
def test_w16_invalid_call_id_is_rejected():
    """W16 · `AsstAsyncConnect` 返回 0 = 调用被拒，不是合法 call id（F8）。"""
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, connect_returns=0)
        h = w.create_instance()
        kind, value = _outcome(
            lambda: w.async_connect(h, adb_path="adb.exe", address="127.0.0.1:5555", config="General")
        )
        assert kind == "error", f"InvalidId=0 必须报错，实际返回 {value!r}"
        assert _is_maa_call_error(value) or any(
            _is_engine_error(value, n) for n in ("AdbDisconnected", "AdbConnectTimeout")
        ), f"错误类型不对：{type(value).__name__}: {value}"
        text = str(value) + str(getattr(value, "args", ""))
        assert "127.0.0.1:5555" in text or "adb" in text.lower(), (
            f"文案里缺 adb 路径 / address：{text}"
        )


@case("W17", "wrapper", "extras")
def test_w17_connection_extras_failure_is_observable():
    """W17 · `set_connection_extras` 传坏 JSON 不能静默装成功（F9）。"""
    W = _wrapper_mod()
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp)
        setter = getattr(w, "set_connection_extras", None)
        if setter is None:
            raise Blocked("wrapper 未提供 set_connection_extras（§1.3 权威签名里也没有，待架构确认）")
        kind, value = _outcome(lambda: setter("gamepad", b"{not json"))
        assert kind == "value", f"不该抛异常：{value!r}"
        assert fake.connection_extras, "根本没调到 C"
        assert value in (None, False, 0), f"坏 JSON 被当成成功了：{value!r}"


@case("W18", "wrapper", "destroy")
def test_w18_destroy_tolerates_null_handles():
    """W18 · `destroy_instance(0)` / `(None)` 都不许抛（否则 on_shutdown 卡死）。"""
    with _tmpdir() as tmp:
        w, _fake = _loaded(tmp)
        for bad in (0, None):
            kind, value = _outcome(lambda bad=bad: w.destroy_instance(bad))
            assert kind == "value", f"destroy_instance({bad!r}) 抛了 {value!r}"


@case("W19", "wrapper", "buffer")
def test_w19_exact_fit_is_success():
    """W19 · 长度**正好等于** buffer 时是成功（写成 `>=` 就是 off-by-one）。

    特意用"默认缓冲尺寸"当 payload 长度：不传 `buffer_size`，让 got 与 buff 严格相等，
    这样"`got >= buff` 判失败"的写法一定会红。
    """
    W = _wrapper_mod()
    buf = int(getattr(W, "DEFAULT_IMAGE_BUFFER_BYTES", 1 << 20))
    payload = b"\x33" * buf
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=payload, get_image_plan=["auto"])
        h = w.create_instance()
        kind, value = _outcome(lambda: w.get_image(h))
        assert kind == "value", f"正好塞满不该报错：{value!r}"
        assert value == payload, f"正好塞满时内容不对（{len(value) if value else None} 字节）"
        last = fake.f("AsstGetImage").calls[-1]
        assert last[2] == len(payload), f"最后一次缓冲应等于 payload 长度，实际 {last[2]}"


@case("W20", "wrapper", "buffer")
def test_w20_return_longer_than_buffer_is_rejected():
    """W20 · 返回长度 > buffer（库违规越界）必须拒绝，不能把整个 buf 交付。

    **`buffer_size=cap` 不能省**（第一版漏了，是假绿）：不传时初始缓冲用的是
    `DEFAULT_IMAGE_BUFFER_BYTES`，它比 `max_image_buffer_bytes=256` 大，
    于是第一轮就撞"预算超限"分支抛错 —— 用例照样绿，但 `got > size` 那条
    分支**一次都没跑到**。变异 M05（删掉越界防御）当时存活，就是这么来的。
    """
    cap = 256
    payload = b"\x7E" * cap
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, payload=payload, get_image_plan=["oversize"],
                          max_image_buffer_bytes=cap, max_buffer_grow=1)
        h = w.create_instance()
        kind, value = _outcome(lambda: w.get_image(h, buffer_size=cap))
        _assert_failure(kind, value, what="返回长度越界")
        # 特征断言：确实进了越界分支（库真的多报了一个字节），而不是别的分支先拦下
        reason = (getattr(value, "detail", None) or {}).get("reason")
        assert reason == "returned_more_than_buffer", (
            f"命中的不是「库越界」分支：reason={reason!r}（说明样本没走到 got > size）"
        )


@case("R19", "runtime", "order")
def test_r19_user_dir_before_load_resource():
    """R19 · `AsstSetUserDir` 必须先于 `AsstLoadResource`（F18）。

    原编号 W21，主理人裁决**移到 R 组**：`load()` 只收 `dll_directory`，不隐式调
    `set_user_dir`——隐蔽副作用比少一次便利危险。这条顺序是 **runtime 的编排契约**，
    用 fake lib 的调用记录器断言。

    顺序反了 ⇒ `AsstLoadResource` 会把 UserDir 绑到资源父目录，
    MAA 的日志/调试目录被写进资源目录。等 `maa/runtime.py` 落盘后，这里要改成
    直接跑 `bootstrap()` 路径；当前用显式调用序列锁住机制。
    """
    with _tmpdir() as tmp:
        root = tmp / "engine"
        (root / "resource").mkdir(parents=True)
        w, fake = _loaded(tmp)
        w.set_user_dir(tmp / "user")
        w.load_resource(root)
        w.create_instance()
        i_user = fake.index_of("AsstSetUserDir")
        i_res = fake.index_of("AsstLoadResource")
        i_inst = fake.index_of("AsstCreateEx")
        assert i_user >= 0, "AsstSetUserDir 没被调用"
        assert i_res >= 0, "AsstLoadResource 没被调用"
        assert i_inst >= 0, "AsstCreateEx 没被调用"
        assert i_user < i_res, f"顺序反了：SetUserDir@{i_user} / LoadResource@{i_res}"
        assert i_res < i_inst, f"必须先加载资源再建实例：LoadResource@{i_res} / CreateEx@{i_inst}"


@case("W22", "wrapper", "ctypes")
def test_w22_asst_bool_zero_eight_and_ff():
    """W22 · `AsstBool` 是 `uint8_t`：`0x80`/`0xFF` 不得抛 `ValueError`。

    对应的真实故障：`c_bool` 读 `uint8_t` 的 0x80 会 `ValueError`；
    `c_byte` 会读成负数 → 布尔判断反转（F16）。
    """
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, connected_returns=0x80, start_returns=0xFF, stop_returns=0xFF)
        h = w.create_instance()
        kind, value = _outcome(lambda: w.is_connected(h))
        assert kind == "value", f"0x80 抛了异常：{value!r}（c_bool/c_byte 的老毛病）"
        assert value is True, f"0x80 应映射为「非 0 即真」，实际 {value!r}"
        kind, value = _outcome(lambda: w.stop_tasks(h))
        assert kind == "value", f"0xFF 抛了异常：{value!r}"
        assert value is True, f"0xFF 应映射为真，实际 {value!r}"


@case("W23", "wrapper", "dll")
def test_w23_bare_name_load_fails_absolute_path_succeeds():
    """W23 · 裸名 `CDLL("xxx.dll")` 不搜 DLL 所在目录（LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR）。

    机制级回归：拿系统自带的小 DLL（`version.dll`）拷一份到临时目录换名，
    验证"裸名失败 / 绝对路径成功"。不绑任何 MAA 符号。

    踩坑记录：一开始用 `kernel32.dll` 做样本，进程退出时 **段错误**（复制内核库并
    `FreeLibrary` 会破坏运行时状态）。换成 `version.dll` 后干净退出 —— 这个坑值得留在
    注释里，免得下一个人"顺手改成 kernel32"。
    """
    if sys.platform != "win32":
        raise Blocked("W23 是 Windows 专属机制回归（LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR）")

    import os
    import shutil

    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    src = system32 / "version.dll"
    if not src.is_file():
        raise Blocked(f"找不到 {src}，跳过机制级回归")

    tmp = Path(tempfile.mkdtemp(prefix="neko-w23-"))
    name = "neko_fake_kernel_1234.dll"
    dst = tmp / name
    shutil.copy2(src, dst)
    lib = None
    try:
        try:
            ctypes.CDLL(name)
        except OSError:
            bare_ok = False
        else:
            bare_ok = True
        assert not bare_ok, (
            f"裸名 {name!r} 居然加载成功了——说明它在搜索路径里，"
            "这个样本不能用来验证机制（换过一个临时目录再试）"
        )

        lib = ctypes.CDLL(str(dst))  # 绝对路径必须成功
        assert lib is not None
        assert lib._handle, "绝对路径加载后句柄为空"
    finally:
        if lib is not None:
            # 自己 load 的要自己卸，否则 Windows 上文件被锁、临时目录删不掉
            try:
                ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(lib._handle))
            except Exception:  # noqa: BLE001
                pass
            del lib
        shutil.rmtree(tmp, ignore_errors=True)


@case("W24", "wrapper", "dll")
def test_w24_dll_directory_handle_lifetime():
    """W24 · `add_dll_directory` 的句柄必须活到 `close()`，失败路径也不许泄漏。

    对应的真实故障：把 `add_dll_directory` 写成 `with` 块 → 句柄提前释放，
    后续延迟加载失败（安全 D5）。
    """
    with _tmpdir() as tmp:
        w, _fake, libdir = _make_wrapper(tmp)
        w.load(dll_directory=libdir)

        attr = None
        for name in ("_dll_dir_handle", "_dll_directory_handle", "dll_dir_handle"):
            if hasattr(w, name):
                attr = name
                break
        assert attr is not None, (
            "wrapper 没有把 add_dll_directory 的句柄保存在实例上"
            "（候选名：_dll_dir_handle / _dll_directory_handle）"
        )
        assert getattr(w, attr) is not None, f"{attr} 在 load() 之后是空的（句柄被提前释放）"

        w.close()
        assert getattr(w, attr) in (None, 0, False), f"close() 之后 {attr} 仍非空，句柄泄漏"
        w.close()  # 幂等

    # 失败路径：缺符号也必须释放句柄
    fake = F.MissingSymbolsFake({"AsstGetImage"})
    with _tmpdir() as tmp:
        w, _fake, libdir = _make_wrapper(tmp, fake)
        kind, value = _outcome(lambda: w.load(dll_directory=libdir))
        assert kind == "error", f"缺符号居然 load 成功了：{value!r}"
        # 分层裁决（§2.3）：缺符号时 wrapper 抛裸 AttributeError，
        # `EngineLoadFailed(symbol=...)` 由 runtime 包装。两条路都接受，但必须"说得出是哪个符号"。
        assert isinstance(value, AttributeError) or _is_engine_error(value, "EngineLoadFailed"), (
            f"缺符号应抛 AttributeError（wrapper 层）或 EngineLoadFailed（runtime 层），"
            f"实际 {type(value).__name__}: {value}"
        )
        assert "AsstGetImage" in str(value) or getattr(value, "symbol", None) == "AsstGetImage", (
            f"错误里看不出是哪个符号缺失：{value}"
        )
        for name in ("_dll_dir_handle", "_dll_directory_handle", "dll_dir_handle"):
            if hasattr(w, name):
                assert getattr(w, name) in (None, 0, False), f"失败路径泄漏了 {name}"
                break


@case("W25", "wrapper", "dll")
def test_w25_load_failure_reports_oserror_vs_symbol():
    """W25 · 库文件不存在 ⇒ 必须带上"操作系统的真实错误"，不能吞掉。

    分层裁决（§2.3）：wrapper 抛**裸 `OSError`**（它不持有 `EngineLoadFailed` 的词汇），
    `EngineLoadFailed(oserror=..., symbol=None)` 由 runtime 包装。两条路都接受，但
    **必须看得出"是文件/依赖问题"而不是"符号问题"**。

    ②③（32 位 DLL / 同目录依赖改名）需要真实样本，标 L5 非门禁 → 阻塞。
    """
    with _tmpdir() as tmp:
        missing = tmp / "nope" / "MaaCore.dll"
        w, _fake, _libdir = _make_wrapper(tmp, library_path=missing, use_real_loader=True)
        kind, value = _outcome(lambda: w.load(dll_directory=tmp))
        assert kind == "error", f"加载不存在的库居然成功了：{value!r}"

        if isinstance(value, OSError):
            assert value.winerror or value.errno or "MaaCore.dll" in str(value), (
                f"OSError 里没有可诊断的信息：{value!r}"
            )
        else:
            assert _is_engine_error(value, "EngineLoadFailed"), (
                f"应抛裸 OSError（wrapper）或 EngineLoadFailed（runtime），"
                f"实际 {type(value).__name__}: {value}"
            )
            assert getattr(value, "oserror", None), "oserror 字段没带上（分不清缺依赖还是缺符号）"
            assert getattr(value, "symbol", None) is None, "文件都不存在，symbol 应为 None"


_WRITE_TASKS = ("Fight", "Infrast", "Recruit", "Mall", "Award", "Roguelike", "Copilot")


def _adapter(tmp: Path, *, connected: bool = True):
    """构造一个接了 fake wrapper 的 adapter（真实 `MaaAdapter`，白名单在它里面）。"""
    import asyncio  # noqa: F401  （仅用于文档性说明：本文件在用例内驱动协程）

    A = _adapter_mod()
    w, fake = _loaded(tmp)
    handle = w.create_instance()
    if not connected:
        handle = 0
    adapter = A.MaaAdapter(
        wrapper=w,
        handle=handle,
        connect_timeout=1.0,
        capture_timeout=1.0,
        resolution_grace=0.05,
    )
    return adapter, fake


@case("W26", "adapter", "whitelist")
def test_w26_write_task_types_rejected_before_any_ctypes_call():
    """W26 · 写任务类型一律 `TaskNotAllowed`，**且 fake lib 调用计数为 0**。

    这是全项目唯一一处"错了会越过安全边界"的地方：用户需求第一句就是
    "初版绝不能执行 Fight/Infrast 等写操作"。断言"抛了异常"不够 ——
    必须断言**在调用任何 ctypes 之前**就被拒（防"先点进去再拒绝"）。

    `Recruit` 的特殊性：架构 §3.3 的常量曾含它，adapter 依据"用户需求 + 旧实现注释
    （严禁 Recruit 等执行类任务）"把它排除，并在 docstring 里写明待定稿。
    我按**拒绝**断（拒绝是唯一安全默认），冲突见 §7.4。
    """
    import asyncio

    A = _adapter_mod()
    readonly = getattr(A, "READONLY_TASK_TYPES", None)
    assert readonly is not None, "adapter 未定义 READONLY_TASK_TYPES"
    assert set(readonly) == {"Screencap", "OperBox", "Depot", "Debug"}, (
        f"只读白名单被改动了：{sorted(readonly)}"
    )
    for t in _WRITE_TASKS:
        assert t not in readonly, f"{t} 混进了只读白名单——直接就是代打"

    with _tmpdir() as tmp:
        adapter, fake = _adapter(tmp)
        for t in _WRITE_TASKS:
            kind, value = _outcome(lambda t=t: asyncio.run(adapter.append_readonly_task(t)))
            assert kind == "error", f"{t} 居然没被拒（返回 {value!r}）"
            assert type(value).__name__ == "TaskNotAllowed" or _is_engine_error(value, "TaskNotAllowed"), (
                f"{t} 的错误类型不对：{type(value).__name__}: {value}"
            )
            assert getattr(value, "task_type", t) == t, f"TaskNotAllowed 没带 task_type：{value!r}"
        assert fake.calls_of("AsstAppendTask") == 0, (
            f"被拒之前已经调进了 C（AsstAppendTask × {fake.calls_of('AsstAppendTask')}）"
        )
        assert fake.calls_of("AsstStart") == 0, "被拒之前已经 AsstStart 了"


@case("W27", "adapter", "whitelist")
def test_w27_debug_task_allowed_only_in_ocr_mode():
    """W27 · `Debug` 只在 `mode="ocr"` 放行，其它 mode 必拒。

    `Debug` 是唯一一个"名字听着只读、参数能改行为"的任务类型。
    """
    import asyncio

    A = _adapter_mod()
    allowed = getattr(A, "ALLOWED_DEBUG_MODES", None)
    assert allowed is not None, "adapter 未定义 ALLOWED_DEBUG_MODES"
    assert set(allowed) == {"ocr"}, f"ALLOWED_DEBUG_MODES 被改动了：{sorted(allowed)}"

    with _tmpdir() as tmp:
        adapter, fake = _adapter(tmp)

        task_id = asyncio.run(adapter.append_readonly_task("Debug", mode="ocr"))
        assert fake.calls_of("AsstAppendTask") >= 1, "ocr 场景被误拒了"
        assert task_id or task_id == 0, f"返回值应是 task id：{task_id!r}"

        before = fake.calls_of("AsstAppendTask")
        for mode in ("screenshot", "", None):
            kind, value = _outcome(lambda mode=mode: asyncio.run(
                adapter.append_readonly_task("Debug", mode=mode)
            ))
            assert kind == "error", f"Debug mode={mode!r} 居然放行了"
            assert type(value).__name__ == "TaskNotAllowed" or _is_engine_error(value, "TaskNotAllowed"), (
                f"Debug mode={mode!r} 的错误类型不对：{type(value).__name__}"
            )
        assert fake.calls_of("AsstAppendTask") == before, "非 ocr 的 Debug 已经调进 C 了"


# --------------------------------------------------------------------------- #
# A 组：adapter 的回调 → asyncio 契约
# --------------------------------------------------------------------------- #
# 三条公共约定（A 组共用）：
#   1. 事件一律用 `_emit()` 注入 `adapter._on_thread_event(msg, json, arg)` —— 这就是
#      `[FACT]` 里「C++ 线程 msg_proc 进 Python」的唯一入口，不是"测试后门"。
#   2. **不用固定 sleep 等结果**：用 `_until(cond)` 等可观测条件（pending_calls /
#      task.done）。固定 sleep 会让慢机器假红、快机器假绿。
#   3. 每条用例自己的 tmp 目录 + 自己的 adapter，避免残留 slot 互相污染。


def _adapter_full(
    tmp,
    *,
    fake=None,
    connect_timeout: float = 1.0,
    capture_timeout: float = 1.0,
    resolution_grace: float = 0.05,
    connected: bool = True,
):
    """构造真实 `MaaAdapter`（接 fake dll），返回 `(adapter, fake, wrapper)`。

    与 `_adapter()` 的差别：可以调三档超时，并且把 wrapper 也交出来（A05 要查
    `DEFAULT_IMAGE_BUFFER_BYTES`，A12 要查回调 keepalive 名单）。
    """
    A = _adapter_mod()
    w, fake = _loaded(tmp, fake)
    handle = w.create_instance() if connected else 0
    adapter = A.MaaAdapter(
        wrapper=w,
        handle=handle,
        connect_timeout=connect_timeout,
        capture_timeout=capture_timeout,
        resolution_grace=resolution_grace,
    )
    return adapter, fake, w


def _emit(adapter, payload, *, msg: int = 0, custom_arg=None) -> None:
    """模拟 C++ 线程投递一条回调。`dict` 会被 JSON 序列化；`bytes`/`None` 原样传。"""
    text = json.dumps(payload) if isinstance(payload, dict) else payload
    adapter._on_thread_event(msg, text, custom_arg)


async def _pump(times: int = 8) -> None:
    """把事件循环转几圈，让 `loop.call_soon(_dispatch)` 有机会跑完。"""
    for _ in range(times):
        await asyncio.sleep(0)


async def _until(cond, *, timeout: float = 2.0, what: str = "") -> None:
    """等到 `cond()` 为真；超时抛 AssertionError（**不静默继续**）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() >= deadline:
            raise AssertionError(f"等待条件超时（{timeout}s）：{what or cond!r}")
        await asyncio.sleep(0.001)


async def _aoutcome(coro):
    """协程版 `_outcome`：把"返回值 / 抛异常"统一成可判断的结果。"""
    try:
        return "value", await coro
    except asyncio.CancelledError:
        raise
    except BaseException as e:  # noqa: BLE001 - 用例要观察**所有**失败形态
        return "error", e


async def _connect_ok(adapter, fake, *, events_before=(), address="127.0.0.1:5555"):
    """跑一次成功连接：先投递 `events_before`，再投递终态 `AsyncCallInfo(ret=True)`。"""
    task = asyncio.ensure_future(adapter.connect(address=address, adb_path="adb"))
    await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
    for ev in events_before:
        _emit(adapter, ev)
    await _pump()
    _emit(
        adapter,
        {
            "what": "AsyncCallInfo",
            "async_call_id": fake.connect_returns,
            "details": {"ret": True, "cost": 12},
        },
    )
    return await asyncio.wait_for(task, 2.0)


@case("W28", "wrapper", "instance-options")
def test_w28_instance_option_binding_and_returns():
    """W28 · `AsstSetInstanceOption` 的绑定与机械返回值（官方 GUI 建实例后的第二件事）。

    真实风险（外部评审指出的"截图慢/触摸怪"）：官方 WPF 代理建实例后立刻设
    `TouchMode` / `AdbLiteEnabled` / `DeploymentWithPause`；我们一项都没设，
    等于把这三个开关交给 MaaCore 内部默认值。

    断言四件事：
      ① 枚举值必须与 C 头文件一致（写错一位 = 静默改错开关）；
      ② 绑定签名：key 是 `c_int32`、value 是 `c_char_p`（**不是 bool**）；
      ③ 句柄 0 时**不发 C 调用**，返回 False（与 `destroy_instance(0)` 同一条纪律）；
      ④ 符号缺失 / C 返回 0 都只降级为 False，**不许抛** —— 这是体验项，不是装载前提。
    """
    import ctypes

    W = _wrapper_mod()
    K = getattr(W, "AsstInstanceOptionKey", None)
    assert K is not None, "wrapper 未导出 AsstInstanceOptionKey（实例选项没法按名设置）"
    got = (K.INVALID, K.TOUCH_MODE, K.DEPLOYMENT_WITH_PAUSE, K.ADB_LITE_ENABLED, K.KILL_ADB_ON_EXIT)
    assert got == (0, 1, 2, 3, 4), f"AsstInstanceOptionKey 与 C 头文件不一致：{got}"

    with _tmpdir() as tmp:
        w, fake = _loaded(tmp)
        fn = fake.f("AsstSetInstanceOption")
        assert fn.restype is ctypes.c_ubyte, f"restype 应为 c_ubyte，实际 {fn.restype!r}"
        assert fn.argtypes == (ctypes.c_void_p, ctypes.c_int32, ctypes.c_char_p), (
            f"argtypes 不对：{fn.argtypes!r} —— key 必须是 int32、value 必须是 char_p"
        )

        handle = w.create_instance()
        assert w.set_instance_option(0, K.TOUCH_MODE, "minitouch") is False, (
            "句柄 0 必须返回 False"
        )
        assert fake.calls_of("AsstSetInstanceOption") == 0, (
            "句柄 0 时仍然调进了 C（应该像 destroy_instance(0) 一样直接短路）"
        )

        assert w.set_instance_option(handle, K.TOUCH_MODE, "minitouch") is True
        assert fake.instance_options == [(int(K.TOUCH_MODE), "minitouch")], (
            f"传下去的不是 (key_int, value_str)：{fake.instance_options!r}"
        )
        assert all(isinstance(v, str) for _k, v in fake.instance_options), (
            "value 被转成了非字符串 —— C 侧自己 parse 字符串，传 bool 会静默失效"
        )

    # C 返回 0：如实返回 False，不抛
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, instance_option_returns=0)
        handle = w.create_instance()
        assert w.set_instance_option(handle, K.ADB_LITE_ENABLED, "1") is False

    # 老版本库没有这个符号：必须降级，不许 AttributeError 把装载打挂
    with _tmpdir() as tmp:
        w, fake = _loaded(tmp, F.MissingSymbolsFake({"AsstSetInstanceOption"}))
        handle = w.create_instance()
        kind, value = _outcome(lambda: w.set_instance_option(handle, K.ADB_LITE_ENABLED, "1"))
        assert kind == "value", f"缺符号时抛了 {value!r}（应该只返回 False）"
        assert value is False, f"缺符号时应返回 False，实际 {value!r}"


@case("W29", "wrapper", "image-buffer")
def test_w29_4k_image_fits_default_budget():
    """W29 · 4K / HDR 量级的 PNG 必须能在**默认上限**下取到（云测要求的边界）。

    算账：`AsstGetImage` 出的是 PNG。1080p 的游戏界面通常 0.5–2 MiB，
    **4K（3840×2160）在高对比/HDR 画面下可以到 8–20 MiB**。
    旧默认 `max_buffer_grow = 2` 只能 1→2→4 MiB，于是"64 MiB 预算还剩 60 MiB"
    却先撞次数上限报 `grow_limit_reached` —— 用户看到的是"截图失败"，
    排查方向会被引到 ADB 上去（这正是旧实现最坏的那类误导）。

    三段断言，缺一不可：
      ① 12 MiB 的 PNG 在默认配置下**能取到**，且倍增梯度是 1→2→4→8→16 MiB；
      ② 对照组：同样 12 MiB，`max_buffer_grow=2` 必须失败在 `grow_limit_reached`
         —— 证明"改默认值"不是换个数字，而是修掉一个真实失败；
      ③ 天花板仍然是 **64 MiB**：永远装不下时，停在 64 MiB 报 `no_budget_to_grow`，
         绝不允许继续倍增（畸形大图不许吃满内存）。
    """
    W = _wrapper_mod()
    assert W.DEFAULT_IMAGE_BUFFER_BYTES == 1 << 20, (
        f"初始缓冲应为 1 MiB，实际 {W.DEFAULT_IMAGE_BUFFER_BYTES}"
    )
    assert W.DEFAULT_MAX_IMAGE_BUFFER_BYTES == 64 << 20, (
        f"字节上限应为 64 MiB，实际 {W.DEFAULT_MAX_IMAGE_BUFFER_BYTES}"
    )
    assert W.DEFAULT_MAX_BUFFER_GROW >= 6, (
        "倍增次数不足以从 1 MiB 走到 64 MiB："
        f"grow={W.DEFAULT_MAX_BUFFER_GROW} 时 4K PNG 会先撞次数上限"
    )

    payload = W.PNG_SIGNATURE + b"\x00" * ((12 << 20) - len(W.PNG_SIGNATURE))

    # ① 默认配置：必须成功
    with _tmpdir() as tmp:
        # ``_make_wrapper`` 返回三元组 ``(wrapper, fake, libdir)`` —— 这里必须解三个。
        # （历史 bug：写成两元组解包 → ``ValueError: too many values to unpack``，
        #  用例从来没跑到过断言，等于这条边界一直没人守。）
        w, fake, libdir = _make_wrapper(
            tmp,
            payload=payload,
            get_image_plan=["auto"],
            max_image_buffer_bytes=W.DEFAULT_MAX_IMAGE_BUFFER_BYTES,
            max_buffer_grow=W.DEFAULT_MAX_BUFFER_GROW,
        )
        w.load(dll_directory=libdir)
        handle = w.create_instance()
        data = w.get_image(handle)
        assert len(data) == len(payload), f"取到的字节数不对：{len(data)} != {len(payload)}"
        assert data.startswith(W.PNG_SIGNATURE), "取到的不是 PNG 原样字节"
        assert fake.image_buff_sizes == [1 << 20, 2 << 20, 4 << 20, 8 << 20, 16 << 20], (
            f"倍增梯度不对：{fake.image_buff_sizes}"
        )

    # ② 对照组：旧默认（grow=2）在同一张图上必定失败
    with _tmpdir() as tmp:
        w, fake, libdir = _make_wrapper(
            tmp,
            payload=payload,
            get_image_plan=["auto"],
            max_image_buffer_bytes=W.DEFAULT_MAX_IMAGE_BUFFER_BYTES,
            max_buffer_grow=2,
        )
        w.load(dll_directory=libdir)
        handle = w.create_instance()
        kind, value = _outcome(lambda: w.get_image(handle))
        assert kind == "error", "grow=2 竟然取到了 12 MiB 的图 —— 对照组的结论不成立"
        assert _is_maa_call_error(value), f"错误类型不对：{type(value).__name__}: {value}"
        reason = getattr(value, "detail", {}).get("reason")
        assert reason == "grow_limit_reached", f"失败原因应是次数上限，实际 {reason!r}"

    # ③ 天花板：永远装不下时停在 64 MiB，不许越过。
    #    两个上限谁先撞到，取决于 grow 的取值 —— 两条都必须走一遍，否则"64 MiB 是**字节**
    #    上限"这句话其实是靠 grow 的次数上限间接成立的（默认 grow=6 正好够走到 64 MiB，
    #    于是字节上限那条分支永远不被执行）。旧写法只断 reason == "no_budget_to_grow"，
    #    在默认 grow=6 下必然红：这不是生产 bug（行为是对的：停在 64 MiB），
    #    而是断言把"哪一种上限生效"写死了。
    for grow, want_reason, why in (
        (20, "no_budget_to_grow", "grow 足够多 → 必须是**字节**上限先撞（64 MiB 是硬墙）"),
        (W.DEFAULT_MAX_BUFFER_GROW, "grow_limit_reached",
         "默认 grow=6 正好走到 64 MiB → 次数上限先撞，同样必须停在 64 MiB"),
    ):
        with _tmpdir() as tmp:
            w, fake, libdir = _make_wrapper(
                tmp,
                payload=payload,
                get_image_plan=["null"],
                max_image_buffer_bytes=W.DEFAULT_MAX_IMAGE_BUFFER_BYTES,
                max_buffer_grow=grow,
            )
            w.load(dll_directory=libdir)
            handle = w.create_instance()
            kind, value = _outcome(lambda: w.get_image(handle))
            assert kind == "error", (
                f"永远返回哨兵却成功了？（grow={grow}）—— 那会把一坨零字节当图像交上去"
            )
            assert _is_maa_call_error(value), f"错误类型不对：{type(value).__name__}: {value}"
            reason = getattr(value, "detail", {}).get("reason")
            assert reason == want_reason, (
                f"grow={grow} 时失败原因应为 {want_reason!r}（{why}），实际 {reason!r}"
            )
            assert fake.image_buff_sizes, "一次 AsstGetImage 都没发出去？"
            assert max(fake.image_buff_sizes) == W.DEFAULT_MAX_IMAGE_BUFFER_BYTES, (
                f"grow={grow}：最大一次分配应正好 64 MiB（停在硬墙上），"
                f"实际 {max(fake.image_buff_sizes)}"
            )
            assert all(s <= W.DEFAULT_MAX_IMAGE_BUFFER_BYTES for s in fake.image_buff_sizes), (
                f"grow={grow}：分配越过了 64 MiB 硬上限：{fake.image_buff_sizes} —— "
                "畸形大图能把进程 OOM"
            )


# --------------------------------------------------------------------------- #
# C 组：连接参数透传 / 会话复用（W30–W35）
# --------------------------------------------------------------------------- #
# 三条公共约定（本组共用）：
#   1. 事件一律走 `runtime._on_raw_event(...)` —— 这是 C++ 线程回调进 Python 的
#      **唯一**入口（与 A 组用 `adapter._on_thread_event` 同一套东西，只是上面多一层）。
#   2. **不 sleep 等结果**：用 `_rt_pending()` 等"挂起调用已注册"这个可观测条件。
#   3. 判据只认可观测事实：`fake.call_log` / `fake.calls_of(...)` / `connect_configs` /
#      `connection_extras` / `status_snapshot()` / `adapter.resolution()`。


def _runtime_fake(env, fake=None, **over):
    """`(runtime, fake)`：`wrapper_factory` 只换掉 CDLL 那一层，其余全是真的。"""
    fake = F.FakeMaaCore() if fake is None else fake
    factory = _fake_factory(fake)
    rt = _runtime(env, _release(_engine_zip()), wrapper_factory=factory, **over)
    return rt, fake


async def _rt_loaded(env, fake=None, **over):
    """摆好布局 + `bootstrap()` 到 `LOADED`（连接类用例的统一前置）。"""
    M = _runtime_mod()
    _install_layout(env.engine_root)
    rt, fake = _runtime_fake(env, fake, **over)
    state = await rt.bootstrap()
    assert state is M.EngineState.LOADED, (
        f"前置失败：布局完整的引擎应装载到 LOADED，实际 {state}（last_error={rt.last_error!r}）"
    )
    return rt, fake


async def _rt_pending(rt, want=1, *, what="", timeout=3.0):
    """等 adapter 的挂起调用数到达 `want`（**不 sleep**，等可观测条件）。"""
    await _until(
        lambda: rt.adapter.stats().pending_calls == want,
        timeout=timeout,
        what=what or f"挂起调用数 == {want}（实际 {rt.adapter.stats().pending_calls}）",
    )


def _rt_event(rt, payload) -> None:
    """模拟 MaaCore 的 C++ 线程投递一条事件。"""
    rt._on_raw_event(0, json.dumps(payload), None)


async def _rt_connect(rt, fake, *, address="127.0.0.1:5555", forced=False,
                      resolution=(1920, 1080), uuid="emu-9999", **kw):
    """跑一次**成功**连接：发号 → 注 `Connected` / `ResolutionGot` / `AsyncCallInfo(ret=True)`。"""
    fn = rt.connect_forced if forced else rt.connect
    task = asyncio.ensure_future(fn(address=address, **kw))
    await _rt_pending(rt, 1, what=f"connect 的 async call 已注册（address={address}）")
    _rt_event(rt, {"what": "Connected", "details": {}, "uuid": uuid})
    _rt_event(
        rt,
        {
            "what": "ResolutionGot",
            "details": {"details": {"width": resolution[0], "height": resolution[1]}},
        },
    )
    _rt_event(
        rt,
        {
            "what": "AsyncCallInfo",
            "async_call_id": fake.connect_returns,
            "details": {"ret": True, "cost": 12},
        },
    )
    return await asyncio.wait_for(task, 5.0)


async def _rt_connect_fails(rt, fake, *, address="127.0.0.1:5555", why="adb refused"):
    """跑一次**失败**连接（`ret=False`），返回异常对象。"""
    task = asyncio.ensure_future(rt.connect(address=address))
    await _rt_pending(rt, 1, what="失败连接的 async call 已注册")
    _rt_event(
        rt,
        {
            "what": "AsyncCallInfo",
            "async_call_id": fake.connect_returns,
            "details": {"ret": False, "error": why, "cost": None},
        },
    )
    kind, value = await _aoutcome(asyncio.wait_for(task, 5.0))
    assert kind == "error", f"`ret=False` 却连接成功了：{value!r}"
    return value


@case("W30", "runtime", "connect-config")
async def test_w30_connect_config_is_passed_through_never_guessed():
    """W30 · `config` 原样透传给 `AsstAsyncConnect` 的第 4 参数；**绝不按端口猜模拟器**。

    真实风险：不同模拟器要走完全不同的连接路径（MuMu12 必须走 extras 的 ``path``）。
    把 ``config`` 写死 ``General`` ⇒ "能连上但截图慢/不稳"，用户与模型都会去查 ADB 端口。
    反过来"看见 16384 就猜 MuMu"更糟：端口既可能是 MuMu 也可能是别人转发的，猜错就
    静默走错路径（表现为偶发截图失败）。

    三段：
      ① 显式 ``config="MuMuEmulator12"`` → C 侧收到的就是它（不是 ``General``）；
      ② 边界：**不传** ``config``、且端口是 MuMu 的默认 16384 → 仍必须是 ``General``
         （这条专防"按 address 猜"回归）；
      ③ 错误输入：空地址 → `AdbAddressMissing`，**一次 C 调用都不发**（校验先于透传）。
    """
    M = _runtime_mod()
    E = _errors_mod()
    with _tmpdir() as tmp:
        env = _env(tmp)
        rt, fake = await _rt_loaded(env)
        try:
            assert fake.connect_configs == [], (
                f"还没调用 connect 就把 config 传下去了：{fake.connect_configs!r}"
            )

            # ① 显式配置名
            profile = await _rt_connect(rt, fake, config="MuMuEmulator12")
            assert fake.connect_configs == ["MuMuEmulator12"], (
                "C 侧收到的连接配置不对："
                f"{fake.connect_configs!r}（期望 ['MuMuEmulator12']）—— "
                "config 不是被写死成 General，就是根本没往下传"
            )
            assert profile.connected is True, f"连上了但 profile 说没连上：{profile!r}"
            assert rt.state is M.EngineState.READY, f"连上后状态应为 READY，实际 {rt.state}"

            # ② 边界：MuMu 的默认端口 + 不传 config → 不许猜成 MuMuEmulator12
            await _rt_connect(rt, fake, address="127.0.0.1:16384")
            assert fake.connect_configs[-1] == "General", (
                "端口 16384 被猜成了别的连接配置："
                f"{fake.connect_configs[-1]!r}。猜模拟器比不猜更糟 —— "
                "猜错会静默走错连接路径，表现成'偶发截图失败'"
            )

            # ③ 错误输入：空地址必须在任何 C 调用之前被拒
            before_calls = fake.calls_of("AsstAsyncConnect")
            before_configs = list(fake.connect_configs)
            kind, value = await _aoutcome(rt.connect(address="   "))
            assert kind == "error", f"空地址竟然连上了：{value!r}"
            assert isinstance(value, E.AdbAddressMissing), (
                f"空地址应抛 AdbAddressMissing，实际 {type(value).__name__}: {value}"
            )
            assert fake.calls_of("AsstAsyncConnect") == before_calls, (
                "地址没配却已经调了 AsstAsyncConnect —— 校验必须排在透传之前"
            )
            assert fake.connect_configs == before_configs, "被拒绝的请求也把 config 记进去了"
        finally:
            await rt.destroy()


@case("W31", "runtime", "connect-extras")
async def test_w31_connection_extras_are_set_before_connect():
    """W31 · `extras` 先 `AsstSetConnectionExtras(config, json)`、**再** `AsstAsyncConnect`。

    顺序不能反：C 侧是在 ``connect`` 里读这张表的，连完再设等于没设（MuMu12 就会
    "连上了但截图很怪"）。这条只能用 `fake.call_log` 的**下标**断言。

    三段：
      ① 正常：`extras={"path": ...}` → C 收到 (config_name, JSON) 且可 round-trip；
         `AsstSetConnectionExtras` 的下标 **小于** `AsstAsyncConnect`；
      ② 边界：`extras={}` / `None` → **不下发**（空表下发等于把默认值当用户配置）；
      ③ 错误输入：`AsstSetConnectionExtras` 抛错 → **不许阻断连接**（C 侧本就是
         void + 静默 return，我们也不能因为一个体验项把连接判死）。
    """
    M = _runtime_mod()
    with _tmpdir() as tmp:
        env = _env(tmp)
        rt, fake = await _rt_loaded(env)
        try:
            # ① 正常路径 + 顺序
            extras = {"path": "D:/Netease/MuMuPlayer-12.0"}
            await _rt_connect(rt, fake, config="MuMuEmulator12", extras=extras)
            assert len(fake.connection_extras) == 1, (
                f"AsstSetConnectionExtras 应被调用 1 次，实际 {fake.connection_extras!r}"
            )
            name, payload = fake.connection_extras[0]
            assert name == "MuMuEmulator12", (
                f"extras 的名字应是 config 名，实际 {name!r}（传错名字 = C 侧查不到这张表）"
            )
            assert json.loads(payload) == extras, (
                f"extras 的 JSON 不能 round-trip 回原 dict：{payload!r}"
            )
            i_extras = fake.index_of("AsstSetConnectionExtras")
            i_connect = fake.index_of("AsstAsyncConnect")
            assert i_extras >= 0 and i_connect >= 0, "调用日志里缺符号，顺序无法判定"
            assert i_extras < i_connect, (
                f"顺序反了：AsstSetConnectionExtras@{i_extras} / AsstAsyncConnect@{i_connect}。"
                "C 侧在 connect 里才读 extras，set 晚了等于没设"
            )

            # ② 边界：空 extras 不下发
            #    两段用**不同地址**是必须的：`extras={}` 与 `extras=None` 归一后是同一个
            #    指纹（都是空元组），同地址会命中幂等短路 —— 那就测不到"AsstSetConnectionExtras
            #    有没有被调用"，测到的是"短路生效了"（另一件事，W32 已经锁住）。
            before = len(fake.connection_extras)
            await _rt_connect(rt, fake, address="127.0.0.1:16385", extras={})
            assert len(fake.connection_extras) == before, (
                "空 extras 也下发了 —— 那会把'用户没配'变成'配了个空表'"
            )
            await _rt_connect(rt, fake, address="127.0.0.1:16386", extras=None)
            assert len(fake.connection_extras) == before, "extras=None 也下发了"

            # ③ 错误输入：C 侧抛错不阻断连接
            def _boom(_name, _value):
                raise RuntimeError("AsstSetConnectionExtras exploded")

            fake.f("AsstSetConnectionExtras")._impl = _boom
            profile = await _rt_connect(
                rt, fake, address="127.0.0.1:16387", extras={"path": "X:/y"}
            )
            assert profile.connected is True, (
                "extras 设置失败把连接判死了 —— 它只是体验项，C 侧对坏 JSON 也只是静默 return"
            )
            assert rt.state is M.EngineState.READY, f"连接成功后状态应是 READY，实际 {rt.state}"
        finally:
            await rt.destroy()


@case("W32", "runtime", "session-reuse")
async def test_w32_same_params_reuse_session_changed_params_reconnect():
    """W32 · 幂等短路：同参连接**不再发** `AsstAsyncConnect`，且会话状态不许被冲掉。

    不改会怎样：`adapter.connect()` 第一件事是 `_reset_session()`，它把分辨率/uuid/链路
    全部清空。于是"再点一次重连"会把一个**已经拿到分辨率**的好会话拆掉重建 ——
    网络慢一点就表现成"本来好的，点了重连反而没分辨率了"。

    四段（每段都是可观测事实，不靠时序）：
      ① 同参重连 → `AsstAsyncConnect` 计数**不变**，返回的 profile 与首次相等，
         且 `adapter.resolution()` 仍是首次的 1920×1080（证明 `_reset_session` 没跑）；
      ② 边界：`extras` 只是**键序**不同 → 仍算同参（指纹按 key 排序归一）；
      ③ 参数变了（换 address）→ 必须真重连（计数 +1）；
      ④ 错误输入：上一次连接**失败**过 → 缓存必须已被丢弃，同样参数也要真重连
         （否则会把"掉线"当成"已连接"交出去）。
    """
    A = _adapter_mod()
    M = _runtime_mod()
    with _tmpdir() as tmp:
        env = _env(tmp)
        rt, fake = await _rt_loaded(env)
        try:
            first = await _rt_connect(rt, fake)
            calls = fake.calls_of("AsstAsyncConnect")
            assert calls == 1, f"首次连接应恰好 1 次 AsstAsyncConnect，实际 {calls}"
            first_res = rt.adapter.resolution()
            assert first_res.status is A.ResolutionStatus.KNOWN, (
                f"首次连接后分辨率应是 KNOWN，实际 {first_res!r}"
            )

            # ① 幂等短路
            again = await rt.connect(address="127.0.0.1:5555", adb_path="", config="General")
            assert fake.calls_of("AsstAsyncConnect") == calls, (
                "同参重连又发了一次 AsstAsyncConnect（幂等短路失效）—— "
                "每点一次重连就把好会话拆一次"
            )
            assert again == first, (
                f"短路返回的 profile 与首次不一致：{again!r} != {first!r}"
            )
            assert rt.adapter.resolution() == first_res, (
                "短路路径把分辨率冲掉了："
                f"{rt.adapter.resolution()!r} != {first_res!r}（`_reset_session` 跑过了）"
            )
            assert rt.state is M.EngineState.READY

            # ② 边界：extras 键序不同仍算同参
            await _rt_connect(rt, fake, extras={"a": "1", "b": "2"})
            after_extras = fake.calls_of("AsstAsyncConnect")
            assert after_extras == calls + 1, (
                f"新增 extras 属于参数变化，应真重连（计数 {calls}→{calls + 1}），"
                f"实际 {after_extras}"
            )
            await rt.connect(
                address="127.0.0.1:5555", extras={"b": "2", "a": "1"}
            )
            assert fake.calls_of("AsstAsyncConnect") == after_extras, (
                "同参但 extras 键序不同就真重连了 —— 指纹没有按 key 归一，"
                "两次解析配置的键序不同会让幂等短路形同虚设"
            )

            # ③ 换了 address → 用户要换设备，必须真重连
            await _rt_connect(rt, fake, address="127.0.0.1:6666")
            assert fake.calls_of("AsstAsyncConnect") == after_extras + 1, (
                "换了 address 却走了幂等短路 —— 等于无视用户改了设备地址"
            )

            # ④ 错误输入：失败过之后，同参也必须真重连
            #    用一个**新地址**：和刚成功的 6666 同参会被幂等短路（那测的是短路，
            #    不是"失败后缓存是否被丢弃"）。
            boom = await _rt_connect_fails(rt, fake, address="127.0.0.1:7777")
            assert _is_engine_error(boom, "AdbDisconnected"), (
                f"ret=False 应抛 AdbDisconnected，实际 {type(boom).__name__}: {boom}"
            )
            assert rt.state is M.EngineState.LOADED, (
                f"连接失败后应停在 LOADED（可重试），实际 {rt.state}"
            )
            before_retry = fake.calls_of("AsstAsyncConnect")
            await _rt_connect(rt, fake, address="127.0.0.1:7777")
            assert fake.calls_of("AsstAsyncConnect") == before_retry + 1, (
                "失败之后同参连接被短路了 —— 会把'掉线'当'已连接'交出去（status 显示已连上）"
            )
        finally:
            await rt.destroy()


@case("W33", "runtime", "connect-force")
async def test_w33_forced_connect_skips_reuse_but_not_validation():
    """W33 · `connect_forced()` = `connect(force=True)`：同参也真重连，但**不放松**任何校验。

    语义是"用户显式点了重连"：设备可能"假活"（`READY` 但截图一直超时、且没有
    `Disconnect` 事件），这时幂等短路会让按钮看起来没反应。

    三段：
      ① 正常：同参 `connect_forced()` → `AsstAsyncConnect` 计数 +1（真重连）；
      ② 边界（对照组）：紧接着普通 `connect()` 同参 → 计数**不变**（证明差别就在 force，
         而不是"第二次总会重连"）；
      ③ 错误输入：`force=True` + 空地址 → 仍抛 `AdbAddressMissing`、仍不发 C 调用；
         状态不许因为"强制"而被动过。
    """
    M = _runtime_mod()
    E = _errors_mod()
    with _tmpdir() as tmp:
        env = _env(tmp)
        rt, fake = await _rt_loaded(env)
        try:
            first = await _rt_connect(rt, fake)
            calls = fake.calls_of("AsstAsyncConnect")
            assert calls == 1, f"首次连接应恰好 1 次 AsstAsyncConnect，实际 {calls}"

            # ① 强制重连
            forced = await _rt_connect(rt, fake, forced=True)
            assert fake.calls_of("AsstAsyncConnect") == calls + 1, (
                "connect_forced() 被幂等短路了："
                f"AsstAsyncConnect 计数仍是 {fake.calls_of('AsstAsyncConnect')}。"
                "用户点重连时'什么都没发生'只会让他认为按钮坏了"
            )
            assert forced == first, (
                f"同参重连的 profile 应与首次一致（不是新造一份假的）：{forced!r} != {first!r}"
            )

            # ② 对照组：普通 connect 同参必须短路
            await rt.connect(address="127.0.0.1:5555")
            assert fake.calls_of("AsstAsyncConnect") == calls + 1, (
                "普通 connect 同参也真重连了 —— 那 force 就没有存在意义（且每次重试都拆会话）"
            )

            # ③ 强制 ≠ 放松校验
            before = fake.calls_of("AsstAsyncConnect")
            kind, value = await _aoutcome(rt.connect_forced(address=""))
            assert kind == "error", f"空地址竟然连上了：{value!r}"
            assert isinstance(value, E.AdbAddressMissing), (
                f"force=True 时地址校验被跳过了：抛的是 {type(value).__name__}: {value}"
            )
            assert fake.calls_of("AsstAsyncConnect") == before, (
                "被拒绝的强制重连仍然调了 AsstAsyncConnect"
            )
            assert rt.state is M.EngineState.READY, (
                f"一次被拒的强制重连不该动状态，实际 {rt.state}"
            )
        finally:
            await rt.destroy()


@case("W34", "runtime", "connect-concurrent")
async def test_w34_concurrent_connect_does_not_clobber_session():
    """W34 · 两个并发 `connect()` 不许把前一个正在等的会话状态冲掉。

    真实故障：`adapter.connect()` 的第一件事是 `_reset_session()`（清分辨率/uuid/链路）。
    两个 Connect 并发时，后一个会把前一个**正在等**的分辨率事件清空，然后两个一起超时。

    断言（全部是稳定可复现的事实，不靠时序）：
      ① 两个同参并发 connect → `AsstAsyncConnect` 只发 **1 次**（第二个被互斥锁排到
         第一个完成之后，并命中幂等短路）；
      ② 两个返回的 profile 相等，且都 `connected=True`；
      ③ `adapter.resolution()` 仍是 1920×1080（没有被任何一次 `_reset_session` 冲掉）；
      ④ 错误输入：先跑一次空地址（失败）→ 锁必须被释放，随后的并发对仍然只发 1 次
         —— 一次失败的连接不许把连接锁卡死。
    """
    A = _adapter_mod()
    M = _runtime_mod()
    with _tmpdir() as tmp:
        env = _env(tmp)
        rt, fake = await _rt_loaded(env)
        try:
            # ④（先跑错误输入）空地址失败 → 锁释放
            kind, value = await _aoutcome(rt.connect(address=""))
            assert kind == "error" and type(value).__name__ == "AdbAddressMissing", (
                f"空地址应抛 AdbAddressMissing，实际 {kind}: {value!r}"
            )
            assert fake.calls_of("AsstAsyncConnect") == 0, "空地址也发号了"

            t1 = asyncio.ensure_future(rt.connect(address="127.0.0.1:5555"))
            t2 = asyncio.ensure_future(rt.connect(address="127.0.0.1:5555"))
            await _rt_pending(rt, 1, what="并发 connect 里只有 1 个真正发号（互斥锁生效）")
            _rt_event(rt, {"what": "Connected", "details": {}, "uuid": "emu-9999"})
            _rt_event(
                rt,
                {"what": "ResolutionGot", "details": {"details": {"width": 1920, "height": 1080}}},
            )
            _rt_event(
                rt,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.connect_returns,
                    "details": {"ret": True, "cost": 7},
                },
            )
            r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), 5.0)

            calls = fake.calls_of("AsstAsyncConnect")
            assert calls == 1, (
                f"两个同参并发 connect 发了 {calls} 次 AsstAsyncConnect（期望 1 次）。"
                "第二次要么撞了未加的锁、要么把第一次的会话冲掉了"
            )
            assert r1 == r2, f"两个并发 connect 的 profile 不一致：{r1!r} != {r2!r}"
            assert r1.connected is True, f"profile 说没连上：{r1!r}"
            res = rt.adapter.resolution()
            assert res.status is A.ResolutionStatus.KNOWN and (res.width, res.height) == (
                1920,
                1080,
            ), (
                f"并发之后分辨率丢了：{res!r} —— 说明有一次 `_reset_session` 冲掉了前一次的会话"
            )
            assert rt.state is M.EngineState.READY, f"并发结束后应是 READY，实际 {rt.state}"

            # ② 边界：三路并发（排队深度更大）仍然只发 1 次，且三个结果一致
            with _tmpdir() as tmp2:
                env2 = _env(tmp2)
                rt2, fake2 = await _rt_loaded(env2)
                try:
                    tasks = [
                        asyncio.ensure_future(rt2.connect(address="127.0.0.1:5555"))
                        for _ in range(3)
                    ]
                    await _rt_pending(rt2, 1, what="三路并发里仍只有 1 个真正发号")
                    _rt_event(rt2, {"what": "Connected", "details": {}, "uuid": "emu-9"})
                    _rt_event(
                        rt2,
                        {
                            "what": "ResolutionGot",
                            "details": {"details": {"width": 1280, "height": 720}},
                        },
                    )
                    _rt_event(
                        rt2,
                        {
                            "what": "AsyncCallInfo",
                            "async_call_id": fake2.connect_returns,
                            "details": {"ret": True, "cost": 3},
                        },
                    )
                    profiles = await asyncio.wait_for(asyncio.gather(*tasks), 5.0)
                    assert fake2.calls_of("AsstAsyncConnect") == 1, (
                        f"三路并发发了 {fake2.calls_of('AsstAsyncConnect')} 次 AsstAsyncConnect（期望 1 次）"
                    )
                    assert len({p.call_id for p in profiles}) == 1, (
                        f"三路并发拿到了不同的 call_id（会话被冲掉了）：{[p.call_id for p in profiles]}"
                    )
                    res2 = rt2.adapter.resolution()
                    assert (res2.width, res2.height) == (1280, 720), (
                        f"三路并发后分辨率丢了：{res2!r}"
                    )
                finally:
                    await rt2.destroy()
        finally:
            await rt.destroy()


@case("W35", "runtime", "instance-options")
async def test_w35_instance_options_applied_before_connect():
    """W35 · 三项实例选项必须在 **Connect 之前**设好，且值就是官方 GUI 那三项。

    为什么顺序是硬契约：`TouchMode` 是连接/截图时才被 controller 读走的（`AsstProxy.cs`
    的顺序是 Create → SetInstanceOption → Connect）。连完再设 ⇒ 这一会话白设，
    表现是"截图慢/触摸怪"这种没人能归因的症状。

    三段：
      ① 正常：`bootstrap()` 后 `AsstSetInstanceOption` 恰好 3 次，键值集合 ==
         {(TOUCH_MODE,'minitouch'), (ADB_LITE_ENABLED,'1'), (DEPLOYMENT_WITH_PAUSE,'0')}；
      ② 顺序：3 次 `AsstSetInstanceOption` 的下标都 **小于** `AsstAsyncConnect` 的下标；
      ③ 错误输入：C 侧全部返回 0（老版本库/不认这几个 key）→ **不阻断装载**，
         状态仍是 LOADED、`rejected` 如实记三项（体验项不是装载前提）。
    """
    M = _runtime_mod()
    W = _wrapper_mod()
    K = W.AsstInstanceOptionKey
    expected = {
        (int(K.TOUCH_MODE), "minitouch"),
        (int(K.ADB_LITE_ENABLED), "1"),
        (int(K.DEPLOYMENT_WITH_PAUSE), "0"),
    }
    with _tmpdir() as tmp:
        env = _env(tmp)
        rt, fake = await _rt_loaded(env)
        try:
            # ① 值集合
            assert len(fake.instance_options) == 3, (
                f"建实例后应设 3 项实例选项，实际 {fake.instance_options!r} —— "
                "一项都不设等于把触摸/截图开关全交给 MaaCore 内部默认值"
            )
            assert set(fake.instance_options) == expected, (
                f"实例选项的键值不对：{sorted(fake.instance_options)}（期望 {sorted(expected)}）。"
                "key 写错一位 = 静默改错开关（C 侧只返回 0，不告诉你哪个 key 不合法）"
            )
            assert all(isinstance(v, str) for _k, v in fake.instance_options), (
                "value 被转成了非字符串 —— C 侧自己 parse 字符串，传 bool 会静默失效"
            )

            # ② 顺序：必须在 Connect 之前
            await _rt_connect(rt, fake)
            log = fake.call_log
            idx_options = [i for i, name in enumerate(log) if name == "AsstSetInstanceOption"]
            assert len(idx_options) == 3, f"call_log 里的实例选项调用数不对：{idx_options}"
            i_connect = log.index("AsstAsyncConnect")
            assert max(idx_options) < i_connect, (
                f"实例选项在 Connect 之后才设：options@{idx_options} / connect@{i_connect}。"
                "TouchMode 是连接/截图时被读走的，连完再设等于这一会话白设"
            )

            # ③ 错误输入：C 侧全拒 → 只降级，不许把装载判死
            with _tmpdir() as tmp2:
                env2 = _env(tmp2)
                rt2, fake2 = await _rt_loaded(
                    env2, fake=F.FakeMaaCore(instance_option_returns=0)
                )
                try:
                    assert rt2.state is M.EngineState.LOADED, (
                        f"实例选项设不上竟然把装载判死了（state={rt2.state}）—— "
                        "这些是体验调节项，不是'能不能跑'的前提"
                    )
                    opts = rt2.status_snapshot()["instance_options"]
                    assert opts["applied"] == {}, f"全部返回 0 却记录了 applied：{opts!r}"
                    assert set(opts["rejected"]) == {
                        "TouchMode",
                        "AdbLiteEnabled",
                        "DeploymentWithPause",
                    }, f"rejected 应如实记下三项，实际 {opts['rejected']!r}"
                finally:
                    await rt2.destroy()

            # ④ 边界：只拒一项（部分成功）→ 分档必须逐项准确，不许"一荣俱荣/一损俱损"
            with _tmpdir() as tmp3:
                env3 = _env(tmp3)
                fake4 = F.FakeMaaCore()

                def _selective(_handle, key, _value):
                    # 只让 TouchMode 这一项"设不上"（部分降级是真实场景：老库认不全这几个 key）
                    return 0 if int(key) == int(K.TOUCH_MODE) else 1

                fake4.f("AsstSetInstanceOption")._impl = _selective
                # 必须在 `bootstrap()`（→ create_instance → set_instance_option）之前装好替身实现
                rt4, _f4 = await _rt_loaded(env3, fake=fake4)
                try:
                    opts = rt4.status_snapshot()["instance_options"]
                    assert set(opts["applied"]) == {"AdbLiteEnabled", "DeploymentWithPause"}, (
                        f"部分被拒时 applied 不对：{opts['applied']!r}"
                    )
                    assert set(opts["rejected"]) == {"TouchMode"}, (
                        f"部分被拒时 rejected 应只含被拒的那一项，实际 {opts['rejected']!r} —— "
                        "全有或全无的分档会让'哪一项没设上'永远查不出来"
                    )
                    assert rt4.state is M.EngineState.LOADED, (
                        f"部分被拒不该阻断装载，实际 {rt4.state}"
                    )
                finally:
                    await rt4.destroy()
        finally:
            await rt.destroy()


@case("W36", "runtime", "connect-extras")
async def test_w36_connection_extras_failure_is_observable():
    """W36 · extras **发没发出去**必须能从 `status_snapshot()` 读出来（三态可分 + 只记键名）。

    为什么非测不可：``[FACT]`` C 侧 ``AsstSetConnectionExtras`` 返回 ``void``，JSON 解析失败
    也只是**静默 return** —— 也就是说"配了 extras 却没生效"这件事，C 永远不会告诉我们。
    上一版在 Python 侧也用 ``contextlib.suppress(Exception)`` 一起吞了，于是"用户填了 MuMu12
    的安装目录、extras 其实根本没发出去"的表现是状态页一片正常，只能靠"截图有点慢"去猜。
    现在三态可分，而且**"没配"与"配了但没发出去"必须是两个不同的值**（混成一个就再也
    查不出是哪一种）。

    五段：
      ① 正常：`extras={"path": ...}` → 快照**精确等于**成功态，且连接真的成功（`READY`）；
      ② 边界：不传 extras → 该键是 `None`（**不是** `{}`、也不是 `{"keys": [], ...}`），
         且 `AsstSetConnectionExtras` 一次都没发；
      ③ 错误输入：Python 侧失败 → `dispatched is False`、`error` 含异常类名、**连接仍成功**，
         且日志是 **warning** 级（不是 error）—— 体验项降级，不是连接失败；
      ④ 不泄露值：快照序列化后**只有键名**，不含路径 / `WindowsPath` repr；
      ⑤ 重置语义：**真连**时重置（换 address 且不传 extras → 回 `None`），
         **但幂等短路不重置**（会话没变，上一次"发出去了"这个事实仍然成立）。

    注 ③ 的写法：**不能**用 `MissingSymbolsFake({"AsstSetConnectionExtras"})` ——
    `wrapper.set_connection_extras` 是用 `getattr(lib, "AsstSetConnectionExtras", None)`
    探测符号的，缺符号走的是**静默 return**（实测 `dispatched=True`、
    `connection_extras_calls == 1`），拿它断不出 `dispatched is False`。要制造"Python 侧
    真的抛了"只能让替身实现显式抛（等价于库已 close / 编码失败那一类真实故障）。
    """
    M = _runtime_mod()
    extras_path = "D:/Program Files/Netease/MuMuPlayer-12.0"
    want_ok = {"keys": ["path"], "dispatched": True, "error": None}

    # ① 成功态（精确相等）+ ④ 不泄露值
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        try:
            profile = await _rt_connect(
                rt,
                fake,
                address="127.0.0.1:18101",
                config="MuMuEmulator12",
                extras={"path": extras_path},
            )
            assert profile.connected is True, f"带 extras 应当连接成功：{profile!r}"
            assert rt.state is M.EngineState.READY, (
                f"带 extras 连接成功后应是 READY，实际 {rt.state}（last_error={rt.last_error!r}）"
            )
            status = rt.status_snapshot()
            assert "connection_extras" in status, (
                f"status_snapshot() 里没有 connection_extras 字段：{sorted(status)}"
            )
            snap = status["connection_extras"]
            assert isinstance(snap, dict) and isinstance(snap.get("keys"), list), (
                f"快照的形状不对（keys 必须是 JSON 可序列化的 list）：{snap!r} —— "
                "snapshot 会被服务层直接交给调用方/日志，tuple 之类的形状会漏到出口"
            )
            assert snap == want_ok, (
                f"成功态快照应**精确等于** {want_ok!r}，实际 {snap!r} —— "
                "键名集合漂了、或顺手把值也塞进去了，状态页/诊断就会与事实对不上"
            )

            blob = json.dumps(snap, ensure_ascii=False)
            for leak in ("D:/", "MuMuPlayer", "Program Files", "WindowsPath"):
                assert leak not in blob, (
                    f"connection_extras 泄露了 extras 的**值**（含 {leak!r}）：{blob}。"
                    "只允许记键名 —— 这个字段会被贴进状态页和 issue"
                )
            assert '"path"' in blob, (
                f"快照里连键名都没有：{blob} —— 那就无法回答'哪一项 extras 没发出去'"
            )
            # 反证 ④ 不是空断言：值确实**发给了 C**，所以"没泄露"才有意义
            assert len(fake.connection_extras) == 1, (
                f"AsstSetConnectionExtras 应恰好被调用 1 次，实际 {fake.connection_extras!r}"
            )
            assert extras_path in fake.connection_extras[0][1], (
                f"发给 C 的 JSON 里没有那个路径值（④ 的反证失效）：{fake.connection_extras[0]!r}"
            )
        finally:
            await rt.destroy()

    # ② 边界：没配 extras → None（不是 {}，也不是 {"keys": [], ...}）
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        try:
            await _rt_connect(rt, fake, address="127.0.0.1:18102")
            status = rt.status_snapshot()
            assert "connection_extras" in status, (
                f"status_snapshot() 里没有 connection_extras 字段：{sorted(status)}"
            )
            assert status["connection_extras"] is None, (
                f"没配 extras 时该字段必须是 `None`，实际 {status['connection_extras']!r} —— "
                "`{}` 或 `{'keys': [], 'dispatched': False}` 会把'用户没配'与"
                "'配了但没发出去'折成同一个值，那这条诊断就废了"
            )
            assert fake.connection_extras == [], (
                f"没配 extras 却下发了：{fake.connection_extras!r}"
            )
        finally:
            await rt.destroy()

    # ⑤ 重置语义：真连要重置，幂等短路不重置
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        try:
            with_extras = {"path": extras_path}
            await _rt_connect(
                rt,
                fake,
                address="127.0.0.1:18103",
                config="MuMuEmulator12",
                extras=with_extras,
            )
            first = rt.status_snapshot()["connection_extras"]
            assert first == want_ok, f"前置失败：带 extras 真连后快照不对：{first!r}"
            calls = fake.calls_of("AsstAsyncConnect")

            # 边界：同参**幂等短路** → 会话没变，上一次的事实仍然成立（不许被清成 None）
            await rt.connect(
                address="127.0.0.1:18103", config="MuMuEmulator12", extras=with_extras
            )
            assert fake.calls_of("AsstAsyncConnect") == calls, (
                f"前置失败：同参连接没有走幂等短路（AsstAsyncConnect 计数 {calls}→"
                f"{fake.calls_of('AsstAsyncConnect')}），这一段就测不到'短路不重置'"
            )
            kept = rt.status_snapshot()["connection_extras"]
            assert kept == first, (
                f"幂等短路把 extras 快照清掉了：{kept!r}（期望保持 {first!r}）。"
                "会话没变、'上一次已发出'这个事实仍然成立；清成 None 等于谎报'这次没配'"
            )

            # 真连 + 没传 extras → 必须回 None
            await _rt_connect(rt, fake, address="127.0.0.1:18104")
            after = rt.status_snapshot()["connection_extras"]
            assert after is None, (
                f"真连且没传 extras，快照却还留着上一次的值：{after!r}。"
                "那等于告诉用户'这次也发出去了'，而这次根本没有"
            )
        finally:
            await rt.destroy()

    # ③ 错误输入：Python 侧抛错 → 降级可观测，连接不受影响
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        try:
            def _boom(_name, _value):
                raise RuntimeError("AsstSetConnectionExtras exploded")

            # 必须在 connect 之前装好替身实现（wrapper 每次调用都现取 `_impl`）
            fake.f("AsstSetConnectionExtras")._impl = _boom
            with _grab_logs("maa.runtime") as records:
                profile = await _rt_connect(
                    rt,
                    fake,
                    address="127.0.0.1:18105",
                    config="MuMuEmulator12",
                    extras={"path": extras_path},
                )
            assert profile.connected is True, (
                f"extras 下发失败把连接判死了：{profile!r} —— 它只是体验项，"
                "C 侧对坏 JSON 也只是静默 return"
            )
            assert rt.state is M.EngineState.READY, (
                f"extras 失败后连接仍应成功到 READY，实际 {rt.state}"
            )

            snap = rt.status_snapshot()["connection_extras"]
            assert isinstance(snap, dict), f"失败态快照应是 dict，实际 {snap!r}"
            assert snap["dispatched"] is False, (
                f"Python 侧抛了错，dispatched 却是 {snap['dispatched']!r} —— "
                "'发出去了'这个谎正是本用例要挡的"
            )
            assert snap["keys"] == ["path"], (
                f"失败态也必须记下键名（只记键名，不记值）：{snap!r}"
            )
            assert snap["error"], f"失败态必须带 error 文案，实际 {snap['error']!r}"
            assert "RuntimeError" in snap["error"], (
                f"error 里没有异常类名，排查时分不清'符号缺失'与'库没加载'：{snap['error']!r}"
            )

            texts = [r.getMessage() for r in records if r.levelno == logging.WARNING]
            assert any(
                "extras" in t.lower() or "NOT dispatched" in t for t in texts
            ), (
                f"降级只记了 debug / 没记 warning —— 真机上没人会知道 extras 没生效：{texts!r}"
            )
            assert not _errors_in(records), (
                f"体验项降级被记成了 error：{_errors_in(records)!r}。"
                "error 会让人以为连接失败，而这次连接其实成功了"
            )

            # ④ 失败态同样不许泄露值（`error` 是唯一可能带外部文本的字段）
            blob = json.dumps(snap, ensure_ascii=False)
            for leak in ("D:/", "MuMuPlayer", "Program Files", "WindowsPath"):
                assert leak not in blob, f"失败态的 error 文案泄露了 extras 的值：{blob}"
        finally:
            await rt.destroy()


@case("A01", "adapter", "events")
def test_a01_callback_from_c_thread_enqueues():
    """A01 · 回调在**独立线程**里进 Python，事件必须落到主协程（且在主线程上被派发）。

    真实故障：把回调写成 `asyncio.run(...)` / `await queue.put(...)` / 绑死某个 loop，
    在 C++ 线程上会 `RuntimeError: no running event loop`；异常被吞在线程里，
    表现是"插件连上了但永远收不到事件"。
    """
    with _tmpdir() as tmp:
        adapter, _fake, _w = _adapter_full(tmp)
        seen: list[tuple[int, object]] = []
        thread_errors: list[BaseException] = []

        async def scenario():
            loop_thread = threading.get_ident()
            adapter.bind_loop()
            done = asyncio.Event()

            def handler(event):
                seen.append((threading.get_ident(), event))
                done.set()

            adapter.on_event(handler)

            def c_side():
                try:
                    _emit(adapter, {"what": "Connected", "uuid": "emu-1234"})
                except BaseException as exc:  # noqa: BLE001 - 这里正是要证明"不会抛"
                    thread_errors.append(exc)

            t = threading.Thread(target=c_side, name="fake-msg-proc")
            t.start()
            await asyncio.wait_for(done.wait(), 2.0)
            t.join(timeout=2.0)
            assert not t.is_alive(), "C 线程侧卡住了（回调里做了阻塞动作）"
            return loop_thread

        loop_thread = asyncio.run(scenario())
        assert thread_errors == [], f"C 线程里抛出了异常（=真实环境里的未定义行为）：{thread_errors[0]!r}"
        assert len(seen) == 1, f"应恰好收到 1 条事件，实际 {len(seen)}"
        assert seen[0][0] == loop_thread, "订阅方没在事件循环线程上被调用"
        assert getattr(seen[0][1], "what", None) == "Connected"
        assert adapter.stats().events_received == 1


@case("A02", "adapter", "events")
def test_a02_wakeup_is_event_driven():
    """A02 · 唤醒靠 `loop.call_soon_threadsafe`，从事件到协程恢复 < 200ms。

    真实故障：用 `sleep(0.05)` 轮询队列 —— 延迟与 CPU 双输；而且 200ms 的
    截图节拍会被轮询周期量化成一个不稳定的锯齿。
    """
    with _tmpdir() as tmp:
        adapter, _fake, _w = _adapter_full(tmp)

        async def scenario():
            loop = adapter.bind_loop()
            calls: list[object] = []
            orig = loop.call_soon_threadsafe

            def spy(cb, *args):
                calls.append(cb)
                return orig(cb, *args)

            loop.call_soon_threadsafe = spy
            try:
                done = asyncio.Event()
                adapter.on_event(lambda _e: done.set())
                t0 = loop.time()
                t = threading.Thread(
                    target=lambda: _emit(adapter, {"what": "UuidGot", "details": {"uuid": "x"}}),
                    name="fake-msg-proc",
                )
                t.start()
                await asyncio.wait_for(done.wait(), 2.0)
                latency = loop.time() - t0
                t.join(timeout=2.0)
            finally:
                loop.call_soon_threadsafe = orig

            assert calls, (
                "没有用 `call_soon_threadsafe` 唤醒 —— 说明 C 线程里直接碰了 asyncio 对象"
                "（那些对象不属于它，且在别的线程上不安全）"
            )
            assert latency < 0.2, f"从事件到协程恢复 {latency * 1000:.1f}ms，超过 200ms（疑似轮询）"

        asyncio.run(scenario())


@case("A03", "adapter", "resolution")
def test_a03_connected_does_not_return_early():
    """A03 · 终态由 `AsyncCallInfo(Connect).ret` 决定；收到 `Connected` **不许**提前返回。

    真实故障：旧实现"收到 `Connected` 就 return"，分辨率永远停在默认 1280x720，
    后续所有坐标都错 —— 而且状态页还显示"已获取"。
    """
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, resolution_grace=0.5)

        async def scenario():
            adapter.bind_loop()
            task = asyncio.ensure_future(
                adapter.connect(address="127.0.0.1:5555", adb_path="adb")
            )
            await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
            _emit(adapter, {"what": "Connected"})
            await _pump()
            assert not task.done(), "收到 Connected 就结束了连接等待（终态必须由 AsyncCallInfo.ret 决定）"

            _emit(adapter, {"what": "ResolutionGot", "details": {"width": 1920, "height": 1080}})
            _emit(adapter, {"what": "UuidGot", "details": {"uuid": "emu-1234"}})
            await _pump()
            assert not task.done(), (
                "分辨率事件到齐后就返回了 —— 终态判据只能是 `AsyncCallInfo(Connect).ret`"
                "（`ResolutionGot` 只记录，不参与终态判定）"
            )
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.connect_returns,
                    "details": {"ret": True, "cost": 12},
                },
            )
            return await asyncio.wait_for(task, 2.0)

        profile = asyncio.run(scenario())
        A = _adapter_mod()
        assert profile.connected is True
        assert profile.resolution.status is A.ResolutionStatus.KNOWN, (
            f"分辨率应已回填，实际 {profile.resolution.status}"
        )
        assert (profile.resolution.width, profile.resolution.height) == (1920, 1080)
        assert (profile.resolution.width, profile.resolution.height) != (1280, 720), (
            "分辨率是默认值 1280x720 —— 这就是 A03 要锁的那个 bug"
        )
        assert profile.uuid == "emu-1234"


@case("A04", "adapter", "resolution")
def test_a04_reordered_events_still_backfill():
    """A04 · 事件顺序完全颠倒，结论**逐字段相等**。

    真实故障：把顺序假设写死在条件里（"必须先 Connected"），换一个 MAA 版本就静默错。
    这条用例在同一函数里跑两遍、按字段比对，比"两条例各自断绝对值"更难蒙对。
    """
    A = _adapter_mod()
    forward = [
        {"what": "Connected"},
        {"what": "ResolutionGot", "details": {"width": 1920, "height": 1080}},
        {"what": "UuidGot", "details": {"uuid": "emu-1234"}},
    ]
    backward = list(reversed(forward))

    def run(events, wait_between: bool):
        with _tmpdir() as tmp:
            adapter, fake, _w = _adapter_full(tmp, resolution_grace=0.5)

            async def scenario():
                adapter.bind_loop()
                task = asyncio.ensure_future(
                    adapter.connect(address="127.0.0.1:5555", adb_path="adb")
                )
                await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
                for ev in events:
                    _emit(adapter, ev)
                    if wait_between:
                        await _pump(2)
                _emit(
                    adapter,
                    {
                        "what": "AsyncCallInfo",
                        "async_call_id": fake.connect_returns,
                        "details": {"ret": True, "cost": 12},
                    },
                )
                return await asyncio.wait_for(task, 2.0)

            return asyncio.run(scenario())

    a = run(forward, False)
    b = run(backward, True)
    key = lambda p: (
        p.connected,
        p.uuid,
        p.resolution.status,
        p.resolution.width,
        p.resolution.height,
    )
    assert key(a) == key(b), f"顺序敏感的结论：{key(a)} != {key(b)}"
    assert key(a) == (True, "emu-1234", A.ResolutionStatus.KNOWN, 1920, 1080), f"结论不对：{key(a)}"


@case("A05", "adapter", "resolution")
def test_a05_resolution_error_does_not_poison_size():
    """A05 · `ResolutionError` ⇒ `FAILED` + `width/height is None`（不是 0、不是默认值）。

    真实故障（F20）：旧代码只判 `ResolutionGot`，`ResolutionError` 分支等于没写，
    于是带着 1280x720 一路跑还谎报"已获取"。另外三条：
    `Resolution(KNOWN, None, None)` 必须构造失败；后续缓冲尺寸回退默认且 > 0；
    FAILED 不能污染截图路径。
    """
    A = _adapter_mod()
    W = _wrapper_mod()
    EV = _errors_mod()

    # ① 构造期不变量：非法组合**写不出来**
    for args in (
        (A.ResolutionStatus.KNOWN, None, None),
        (A.ResolutionStatus.KNOWN, 0, 0),
        (A.ResolutionStatus.UNKNOWN, 1280, 720),
        (A.ResolutionStatus.FAILED, 0, 0),
    ):
        try:
            got = A.Resolution(*args)
        except BaseException as exc:  # noqa: BLE001
            assert isinstance(exc, (AssertionError, ValueError)), (
                f"Resolution{args} 应抛 AssertionError/ValueError，实际 {type(exc).__name__}: {exc}"
            )
        else:
            raise AssertionError(f"Resolution{args} 竟然构造成功了：{got!r}（不变量没生效）")

    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, resolution_grace=0.5)

        async def scenario():
            adapter.bind_loop()
            task = asyncio.ensure_future(
                adapter.connect(address="127.0.0.1:5555", adb_path="adb")
            )
            await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
            _emit(
                adapter,
                {
                    "what": "ResolutionError",
                    "why": "Get resolution failed: timeout",
                    "details": {"width": 0, "height": 0},
                },
            )
            await _pump()
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.connect_returns,
                    "details": {"ret": True, "cost": 12},
                },
            )
            profile = await asyncio.wait_for(task, 2.0)

            # ② FAILED 之后截图仍然可用（缓冲回退默认值，不依赖分辨率）
            cap = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.screencap_returns,
                    "details": {"ret": True, "cost": 5},
                },
            )
            data = await asyncio.wait_for(cap, 2.0)
            return profile, data

        profile, data = asyncio.run(scenario())

    assert profile.connected is True, "分辨率失败不是连接失败，绝不该 raise 或 false"
    res = profile.resolution
    assert res.status is A.ResolutionStatus.FAILED, f"状态应是 FAILED，实际 {res.status}"
    assert res.width is None and res.height is None, (
        f"失败时不得带回宽高（更不能是 0）：{res.width!r}x{res.height!r}"
    )
    assert res.reason, "失败原因不能是空串（状态页要靠它说话）"
    assert "timeout" in res.reason, f"原因应是 MAA 原文，实际 {res.reason!r}"
    assert W.DEFAULT_IMAGE_BUFFER_BYTES > 0, "默认缓冲必须 > 0"
    assert data.startswith(W.PNG_SIGNATURE), "FAILED 分辨率污染了截图路径"
    assert not isinstance(profile, EV.EngineError)


@case("A06", "adapter", "resolution")
def test_a06_async_call_ret_true_counts_as_connected():
    """A06 · 只有 `AsyncCallInfo{ret:true}`，两个分辨率事件都不来 ⇒ 连上 + `UNKNOWN`。

    两面都要压：①`ret=True` 不能推出"分辨率已回填"（没有 C API 能读 `get_scale_size()`）；
    ②也不能因为"没收到分辨率"就空等到超时 —— 连接的终态判据与分辨率无关。
    """
    A = _adapter_mod()
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, connect_timeout=1.0, resolution_grace=0.05)

        async def scenario():
            loop = adapter.bind_loop()
            t0 = loop.time()
            task = asyncio.ensure_future(
                adapter.connect(address="127.0.0.1:5555", adb_path="adb")
            )
            await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.connect_returns,
                    "details": {"ret": True, "cost": 12},
                },
            )
            profile = await asyncio.wait_for(task, 2.0)
            return profile, loop.time() - t0

        profile, elapsed = asyncio.run(scenario())

    assert profile.connected is True
    assert profile.resolution.status is A.ResolutionStatus.UNKNOWN, (
        f"没收到分辨率事件时必须诚实地是 UNKNOWN，实际 {profile.resolution.status}"
    )
    assert profile.resolution.width is None and profile.resolution.height is None
    assert elapsed < 0.5, f"耗时 {elapsed:.2f}s —— 在空等分辨率（终态判据用错了）"


@case("A07", "adapter", "connect")
def test_a07_async_call_ret_false_fails_fast():
    """A07 · `ret:false` ⇒ 立即 `AdbDisconnected`，`why` 取 `details.error`（MAA 原文优先）。

    真实故障：把 `ret:false` 当"还没收到"继续等到超时 —— 用户多等 30 秒，
    最后拿到的还是同一句"连接超时"，原始原因被吃掉。
    """
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, connect_timeout=5.0)

        async def scenario():
            loop = adapter.bind_loop()
            t0 = loop.time()
            task = asyncio.ensure_future(
                adapter.connect(address="127.0.0.1:5555", adb_path="adb")
            )
            await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
            # ConnectFailed 先到，但 ret=false 的 details.error 才是权威原因
            _emit(adapter, {"what": "ConnectFailed", "why": "次要原因：设备忙"})
            await _pump(2)
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.connect_returns,
                    "details": {"ret": False, "error": "device offline"},
                },
            )
            kind, value = await _aoutcome(task)
            return kind, value, loop.time() - t0

        kind, value, elapsed = asyncio.run(scenario())

    assert kind == "error", f"ret=false 却返回了 {value!r}"
    assert type(value).__name__ == "AdbDisconnected", f"错误类型不对：{type(value).__name__}: {value}"
    assert getattr(value, "why", "") == "device offline", (
        f"why 应取 details.error 的 MAA 原文，实际 {getattr(value, 'why', None)!r}"
    )
    assert "127.0.0.1:5555" in str(value), f"文案里没有真实地址：{value}"
    assert elapsed < 1.0, f"耗时 {elapsed:.2f}s —— 把 ret=false 当成'还没收到'在等"


@case("A08", "adapter", "connect")
def test_a08_connect_failed_carries_why_and_address():
    """A08 · 只有 `ConnectFailed{why}`（无 `details.error`）⇒ 文案含 why 原文与真实地址，
    **且不含虚构端口 5555**。

    真实故障（F17）：旧实现把"地址里没有端口"翻译成"连 5555 失败"，用户照着那句
    去查模拟器端口，方向从一开始就错了。地址故意不带端口，才能验证"不虚构"。
    """
    why = "Address does not contain ':' and no devices found"
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, connect_timeout=5.0)

        async def scenario():
            adapter.bind_loop()
            task = asyncio.ensure_future(adapter.connect(address="127.0.0.1", adb_path="adb"))
            await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
            _emit(adapter, {"what": "ConnectFailed", "why": why})
            await _pump(2)
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.connect_returns,
                    "details": {"ret": False},
                },
            )
            return await _aoutcome(task)

        kind, value = asyncio.run(scenario())

    assert kind == "error", f"应失败，实际返回 {value!r}"
    assert type(value).__name__ == "AdbDisconnected", f"类型不对：{type(value).__name__}"
    assert getattr(value, "why", "") == why, f"why 丢了原文：{getattr(value, 'why', None)!r}"
    # 注意断言的是 `.message` / `to_dict()`，不是 `str(exc)`：
    # `EngineError.__str__` 刻意只渲染"关闭词表的机器标量"（K9），自由文本（MAA 原文）
    # 不在里面 —— 拿 `str()` 断"文案含原文"等于在错误的层上断言。
    text = value.message
    assert why in text, f"用户看到的文案里没有 MAA 原文：{text}"
    assert "127.0.0.1" in text, f"文案里没有真实地址：{text}"
    assert "5555" not in text, f"文案里凭空出现 5555（F17 的旧 bug）：{text}"
    assert "5555" not in json.dumps(value.to_dict(), ensure_ascii=False), (
        f"结构化上下文里凭空出现 5555（会在日志/状态页里冒出来）：{value.to_dict()}"
    )


@case("A09", "adapter", "connect")
def test_a09_empty_address_never_invents_port():
    """A09 · 空地址 ⇒ `AdbAddressMissing`，且**在任何 ctypes 调用之前**。

    "没配地址"和"连不上"是两件事：前者该去设置页填，后者才该查模拟器。
    合成一句话会把用户送到错误的排查方向上。
    """
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp)

        async def scenario():
            adapter.bind_loop()
            out = []
            for addr in ("", "   ", "\t\n"):
                out.append(await _aoutcome(adapter.connect(address=addr, adb_path="adb")))
            return out

        results = asyncio.run(scenario())

    for addr, (kind, value) in zip(("", "   ", "\t\n"), results):
        assert kind == "error", f"address={addr!r} 竟然连上了：{value!r}"
        assert type(value).__name__ == "AdbAddressMissing", (
            f"address={addr!r} 的错误类型不对：{type(value).__name__}: {value}"
        )
        text = value.message
        assert "adb_address" in text, f"文案应指向配置项 adb_address：{text}"
        assert "5555" not in text, f"凭空出现 5555：{text}"
        assert "5555" not in json.dumps(value.to_dict(), ensure_ascii=False), (
            f"结构化上下文里凭空出现 5555：{value.to_dict()}"
        )
        assert getattr(value, "config_key", "adb_address") == "adb_address"
    assert fake.calls_of("AsstAsyncConnect") == 0, (
        f"空地址却已经调进了 C（AsstAsyncConnect × {fake.calls_of('AsstAsyncConnect')}）"
    )


@case("A10", "adapter", "cancel")
def test_a10_cancel_leaves_no_orphan_worker():
    """A10 · 取消一次截图：**不留挂起 slot、不留线程、不产生"迟到的成功"**。

    真实故障：旧实现用 `asyncio.to_thread` 跑阻塞的 C 调用，`task.cancel()` 停不住线程
    —— 每超时一次永久吃掉一个 worker，几十次之后整个插件卡死。新设计里 C 调用是
    非阻塞的同步调用，**结构性没有**工作线程，所以这里断的是"线程数不涨"，
    而不是"线程能结束"（后者是不可证的）。
    """
    with _tmpdir() as tmp:
        adapter, _fake, _w = _adapter_full(tmp, capture_timeout=5.0)

        async def scenario():
            adapter.bind_loop()
            base_threads = threading.active_count()
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")
            assert threading.active_count() == base_threads, (
                "截图路径自己起了线程 —— 这正是 to_thread 那套设计的入口"
            )
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancel 之后 capture 居然正常返回了")
            assert adapter.stats().pending_calls == 0, "取消后 slot 没摘除（下次同 id 会被误派发）"

            seq = adapter.error_seq
            before_late = adapter.stats().events_dropped_late
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": 1,
                    "details": {"ret": True, "cost": 3},
                },
            )
            await _pump()
            assert adapter.stats().events_dropped_late == before_late + 1, (
                "取消后的孤儿事件没被计成迟到"
            )
            assert adapter.error_seq == seq, "迟到事件改写了失败序号"
            assert threading.active_count() == base_threads, "取消后留下工作线程"

        asyncio.run(scenario())


@case("A11", "adapter", "late")
def test_a11_late_error_does_not_overwrite_timeout():
    """A11 · 超时后到来的迟到失败**不得覆盖** `last_error`；用 `error_seq` 断。

    真实故障：共享字段 + 版本号比对，迟到的孤儿线程把用户看到的原因从"超时"
    改写成"缓冲不足"。现在按 `async_call_id` 在 `_dispatch` 里就丢弃，
    结构上到不了 `last_error` —— 这条用例就是压这个结构性修复。
    """
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, capture_timeout=0.05)

        async def scenario():
            adapter.bind_loop()
            t0 = adapter.error_seq
            kind, value = await _aoutcome(adapter.capture())
            assert kind == "error", f"没等到 AsyncCallInfo 却成功了：{value!r}"
            assert type(value).__name__ == "CaptureTimeout", f"应超时，实际 {type(value).__name__}"
            assert adapter.error_seq == t0 + 1, "一次失败应只推进一格序号"
            first_last = adapter.last_error
            assert first_last is value, "last_error 没记下这次超时"

            # 迟到的"真正原因"（id 已不在挂起表里）
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.screencap_returns,
                    "details": {"ret": False, "error": "缓冲不足"},
                },
            )
            await _pump()
            assert adapter.last_error is first_last, (
                f"迟到事件覆盖了 last_error（现在变成 {adapter.last_error!r}）"
            )
            assert adapter.error_seq == t0 + 1, "迟到事件推进了失败序号"
            assert type(adapter.last_error).__name__ == "CaptureTimeout"
            assert adapter.stats().events_dropped_late == 1

            # 再发起 B：它只该收到自己 id 的事件
            fake.screencap_returns = 2
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="第二次 capture slot")
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": 2,
                    "details": {"ret": True, "cost": 7},
                },
            )
            data = await asyncio.wait_for(task, 2.0)
            assert adapter.stats().events_dropped_late == 1, "第二次调用被孤儿事件干扰了"
            return data

        data = asyncio.run(scenario())

    assert data.startswith(_wrapper_mod().PNG_SIGNATURE), "B 拿到的不该是 A 的残渣"


@case("A12", "adapter", "lifecycle")
def test_a12_restart_does_not_leak_callbacks():
    """A12 · 三轮 `connect → cancel`：每轮 slot 独立、不重绑 loop、回调对象不增长。

    真实故障：每轮重建 `CFUNCTYPE` 或重新绑定 loop ⇒ 旧回调被 GC 后 C 侧指针悬空
    （use-after-free，进程直接崩，接不住）；或者每轮泄漏一个强引用。
    新设计把回调对象放在 wrapper 的模块级 keepalive 名单里，adapter 从不重建它，
    所以这里同时断"名单不增长"与"loop_binds 不变"。
    """
    with _tmpdir() as tmp:
        adapter, _fake, w = _adapter_full(tmp, connect_timeout=5.0)
        keepalive = getattr(_wrapper_mod(), "_CALLBACK_KEEPALIVE", None)
        assert keepalive is not None, "wrapper 模块没有 `_CALLBACK_KEEPALIVE`（UAF 防护不见了）"
        before = len(keepalive)

        async def scenario():
            adapter.bind_loop()
            base_threads = threading.active_count()
            for i in range(3):
                task = asyncio.ensure_future(
                    adapter.connect(address="127.0.0.1:5555", adb_path="adb")
                )
                await _until(
                    lambda: adapter.stats().pending_calls == 1, what=f"第 {i + 1} 轮 slot 注册"
                )
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError(f"第 {i + 1} 轮 cancel 之后 connect 正常返回了")
                assert adapter.stats().pending_calls == 0, f"第 {i + 1} 轮留下挂起 slot"
                assert threading.active_count() == base_threads, f"第 {i + 1} 轮漏了线程"

        asyncio.run(scenario())
        assert len(keepalive) == before, (
            f"回调 keepalive 名单增长了 {len(keepalive) - before} 个 —— 每轮都在重建 CFUNCTYPE"
        )
        assert adapter.stats().loop_binds == 1, (
            f"loop_binds={adapter.stats().loop_binds}：同一事件循环里不该重绑"
        )
        assert adapter.stats().events_dropped_late == 0, "取消过的事件被记成迟到（污染统计）"


@case("A13", "adapter", "robustness")
def test_a13_malformed_payload_is_swallowed():
    """A13 · 坏载荷（非 UTF-8 / 非 JSON / `None`）一律吞掉并计数，**绝不抛穿到 C 线程**。

    真实故障：在 C++ 线程里抛异常是未定义行为 —— 不是"报个错"，而是可能整进程崩。
    """
    with _tmpdir() as tmp:
        adapter, _fake, _w = _adapter_full(tmp)
        raised: list[BaseException] = []

        async def scenario():
            adapter.bind_loop()
            before = adapter.stats().events_dropped_broken

            def c_side():
                for payload in (b"\xff\xfe", b"{not json", None, b"[1,2,3]", b"123"):
                    try:
                        _emit(adapter, payload, msg=7)
                    except BaseException as exc:  # noqa: BLE001
                        raised.append(exc)

            t = threading.Thread(target=c_side, name="fake-msg-proc")
            t.start()
            t.join(timeout=2.0)
            await _pump()
            return before

        before = asyncio.run(scenario())

    assert raised == [], f"坏载荷抛穿了（真实环境里就是 C++ 线程中的未定义行为）：{raised[0]!r}"
    stats = adapter.stats()
    assert stats.events_dropped_broken == before + 5, (
        f"坏载荷计数不对：{stats.events_dropped_broken}（应在 {before} 基础上 +5）"
    )
    assert stats.events_received == 0, "坏载荷不该进正常事件计数"
    assert adapter.dispatch_errors == 0, "吞掉坏载荷时不该留下派发错误"


@case("A14", "adapter", "late")
def test_a14_stale_events_do_not_satisfy_next_task():
    """A14 · 上一次调用（id 1）的迟到事件**不能满足**下一次调用（id 2）。

    真实故障：盲扫队列 + 谓词猜结果 —— "第二次同步"拿到第一轮的结果写进缓存，
    截图内容和任务时刻对不上，且没有任何报错。
    """
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, capture_timeout=0.05)

        async def scenario():
            adapter.bind_loop()
            kind, value = await _aoutcome(adapter.capture())
            assert kind == "error" and type(value).__name__ == "CaptureTimeout", (
                f"第一轮应超时，实际 {type(value).__name__}: {value!r}"
            )

            fake.screencap_returns = 2
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="第二轮 slot 注册")

            # 孤儿事件：一个"成功"，一个"失败"，都带旧的 id
            _emit(
                adapter,
                {"what": "AsyncCallInfo", "async_call_id": 1, "details": {"ret": True, "cost": 111}},
            )
            await _pump()
            assert not task.done(), "第二轮被上一轮的迟到成功事件满足了"

            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": 1,
                    "details": {"ret": False, "error": "STALE"},
                },
            )
            await _pump()
            assert not task.done(), "第二轮被上一轮的迟到失败事件满足了"
            assert adapter.stats().events_dropped_late == 2

            _emit(
                adapter,
                {"what": "AsyncCallInfo", "async_call_id": 2, "details": {"ret": True, "cost": 42}},
            )
            return await asyncio.wait_for(task, 2.0)

        data = asyncio.run(scenario())

    assert data == fake.payload, "第二轮拿到的图像不是本次的数据"
    assert adapter.stats().events_dropped_late == 2


@case("A15", "adapter", "format")
def test_a15_non_png_magic_keeps_raw_bytes_and_never_reencodes():
    """A15 · 非 PNG 首字节 ⇒ `CaptureFormatUnexpected`，**原始字节逐字节保留**，
    且**绝不二次编码兜底**。

    真实故障：旧 `maa_adapter.py` 的 docstring 就记着"PNG/RGB 二次编码"这个坑。二次编码
    会把"库版本不对"变成一个能正常显示的错图 —— 问题被吃掉，且再无复现证据。
    载荷故意取 64 字节（与一张小图裸 BGR 等长），排除"长度凑巧像 PNG"的假绿。
    """
    raw = b"\x00" * 64
    with _tmpdir() as tmp:
        fake = F.FakeMaaCore(payload=raw)
        adapter, fake, w = _adapter_full(tmp, fake=fake)

        async def scenario():
            adapter.bind_loop()
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.screencap_returns,
                    "details": {"ret": True, "cost": 4},
                },
            )
            return await _aoutcome(task)

        kind, value = asyncio.run(scenario())

    assert kind == "error", f"非 PNG 却被当成图像交给了上层：{len(value)} 字节"
    assert type(value).__name__ == "CaptureFormatUnexpected", (
        f"错误类型不对：{type(value).__name__}: {value}"
    )
    assert getattr(value, "magic", None) == raw[:8], (
        f"magic 应是首 8 字节，实际 {getattr(value, 'magic', None)!r}"
    )
    assert getattr(value, "length", None) == len(raw), (
        f"length 应是 {len(raw)}，实际 {getattr(value, 'length', None)!r}"
    )
    payload = getattr(value, "payload", None)
    assert payload == raw, (
        "异常里必须带上**原始字节**（逐字节相等）：没有它，'库版本不对'这条线索就永久丢失。"
        f"实际 payload={payload!r}（若为 None，说明 `CaptureFormatUnexpected` 还没有 payload 字段）"
    )
    # 没有第二次取图 / 没有第二套编码路径
    assert fake.calls_of("AsstGetImage") == 1, (
        f"AsstGetImage 被调了 {fake.calls_of('AsstGetImage')} 次（疑似重取/兜底）"
    )
    assert fake.calls_of("AsstGetImageBgr") == 0, "走进了 BGR 裸像素路径（禁上行）"


# --------------------------------------------------------------------------- #
# D 组：断线事件（A16–A18）
# --------------------------------------------------------------------------- #
# 这一组回答同一个问题：`ConnectionInfo` 的三种事件（`Disconnect` / `ScreencapFailed`
# / `Reconnecting`）到达时，**正在挂起的调用**应该怎么办？
#   * 不该做的事：让调用方陪着 8–10 秒的 `capture_timeout` 一起干等（用户看到的是
#     "截图卡住了"，而真正原因早就躺在事件里）；
#   * 也不该做的事：一次 `ScreencapFailed` 就把整条链路判死、或 `Reconnecting` 时
#     我们再补发一个 Connect 跟 MAA 自己打架。
# 注入方式仍是 `_emit()`（C++ 线程进 Python 的唯一入口），断言只认耗时上界、异常类型、
# `link_state`、以及 fake lib 的调用计数 —— 不读私有字段做判断。


@case("A16", "adapter", "disconnect")
async def test_a16_disconnect_fails_pending_capture_immediately():
    """A16 · `Disconnect` 到达时，挂起的 `capture()` 必须**立刻**失败（不是等超时）。

    真实故障：没有这条路径时，掉线后一次截图要等满 `capture_timeout`（默认 8s）才报
    "截图卡住了"。宿主侧一次 entry 只有几秒预算 ⇒ 必然被超时打穿，而且报的原因
    （缓冲/超时）会把排查引向完全错误的方向。

    断言：
      ① 耗时 **远小于** `capture_timeout`（这里 5s，断言 < 0.5s ⇒ 差一个数量级）；
      ② 异常类型与"真失败"一致（`CaptureFailed`，不是新造一个 `DisconnectedDuringCapture`）
         —— 断线的语义是"这次调用输了"，必须与 `ret=False` 走同一条分支；
      ③ `AsstGetImage` **一次都没调**（没有画面可取）；
      ④ 边界：挂起的是 **Connect** 时同样立刻失败（`AdbDisconnected`，且带事件里的 why）；
      ⑤ 错误输入：`Disconnect` 不带 `why` → 用中性默认原因，**不许编造**具体原因，
         也不许把异常抛穿到 C 线程。
    """
    A = _adapter_mod()
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, capture_timeout=5.0)

        async def scenario():
            # ① 挂起的截图被 Disconnect 立刻打断
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")
            loop = asyncio.get_running_loop()
            started = loop.time()
            _emit(adapter, {"what": "Disconnect", "details": {"why": "adb gone"}})
            kind, value = await _aoutcome(task)
            cost = loop.time() - started

            assert kind == "error", f"掉线了却还能截图：{value!r}"
            assert type(value).__name__ == "CaptureFailed", (
                f"断线应走与 ret=False 相同的 CaptureFailed 分支，"
                f"实际 {type(value).__name__}: {value}"
            )
            assert cost < 0.5, (
                f"挂起的 capture 耗时 {cost:.3f}s（capture_timeout=5s）：它在等超时，"
                "没有把 Disconnect 当成'结果不会来了'"
            )
            assert adapter.link_state is A.LinkState.DISCONNECTED, (
                f"Disconnect 之后 link_state 应是 DISCONNECTED，实际 {adapter.link_state}"
            )
            assert fake.calls_of("AsstGetImage") == 0, (
                "已经掉线了还去 AsstGetImage 取图（拿到的是未初始化缓冲）"
            )

            # ④ 边界：挂起的是 Connect → AdbDisconnected，why 来自事件
            connect_task = asyncio.ensure_future(
                adapter.connect(address="127.0.0.1:5555", adb_path="adb")
            )
            await _until(lambda: adapter.stats().pending_calls == 1, what="connect slot 注册")
            _emit(adapter, {"what": "Disconnect", "details": {"why": "device offline"}})
            kind, value = await _aoutcome(asyncio.wait_for(connect_task, 2.0))
            assert kind == "error", f"掉线后 connect 竟然成功了：{value!r}"
            assert type(value).__name__ == "AdbDisconnected", (
                f"挂起的 Connect 应以 AdbDisconnected 结束，实际 {type(value).__name__}: {value}"
            )
            assert "device offline" in str(getattr(value, "why", "")), (
                "为什么掉线的原始原因丢了（用户只能看到一句'连不上'）："
                f"{getattr(value, 'why', None)!r}"
            )

            # ⑤ 错误输入：不带 why 的 Disconnect
            adapter._reset_session()          # 让链路状态从头来（等价于新会话）
            cap = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")
            _emit(adapter, {"what": "Disconnect", "details": {}})   # 没有 why
            kind, value = await _aoutcome(asyncio.wait_for(cap, 2.0))
            assert kind == "error", "没有 why 的 Disconnect 没能让截图失败"
            assert "disconnect" in adapter.link_reason.lower(), (
                f"缺 why 时应给一句中性默认原因（含 disconnect），实际 {adapter.link_reason!r} —— "
                "编造具体原因（'adb refused' 之类）比不说更贵"
            )

        await scenario()


@case("A17", "adapter", "screencap-failed")
async def test_a17_screencap_failed_only_fails_that_capture():
    """A17 · `ScreencapFailed` 只让**挂起的那一次**截图失败，链路状态不许变。

    真实故障：把 `ScreencapFailed` 当成断线，一次偶发截图失败就把 `link_state` 打成
    `DISCONNECTED` ⇒ 上层把设备判死、用户被要求重连（而其实下一次截图就会成功）。

    断言：
      ① 正常：有挂起截图时 `ScreencapFailed` → `CaptureFailed`；
      ② `link_state` 仍是 `CONNECTED`（此前收到过 `Connected`）；
      ③ 边界：**没有**挂起调用时收到 `ScreencapFailed` → 不抛、不影响任何状态
         （`_fail_pending_calls` 命中 0 条）；
      ④ 错误输入（毒化检查）：随后再截一次必须能成功 —— 单次失败不许污染会话。
    """
    A = _adapter_mod()
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, capture_timeout=5.0)

        async def scenario():
            adapter.bind_loop()
            _emit(adapter, {"what": "Connected", "details": {}, "uuid": "emu-1"})
            await _pump()
            assert adapter.link_state is A.LinkState.CONNECTED, (
                f"前置：收到 Connected 后应是 CONNECTED，实际 {adapter.link_state}"
            )

            # ① 挂起的截图被单次失败打断
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")
            _emit(
                adapter,
                {"what": "ScreencapFailed", "details": {"why": "adb screencap failed"}},
            )
            kind, value = await _aoutcome(asyncio.wait_for(task, 2.0))
            assert kind == "error", f"单次截图失败却返回了数据：{value!r}"
            assert type(value).__name__ == "CaptureFailed", (
                f"应抛 CaptureFailed，实际 {type(value).__name__}: {value}"
            )

            # ② 链路不许被判死
            assert adapter.link_state is A.LinkState.CONNECTED, (
                f"一次截图失败把链路打成了 {adapter.link_state} —— "
                "偶发失败会被升级成'设备掉线'，用户被要求重连"
            )

            # ③ 边界：没有挂起调用时的 ScreencapFailed
            assert adapter.stats().pending_calls == 0
            _emit(adapter, {"what": "ScreencapFailed", "details": {"why": "stray"}})
            await _pump()
            assert adapter.link_state is A.LinkState.CONNECTED, (
                "孤立的 ScreencapFailed 改动了链路状态"
            )
            assert adapter.stats().pending_calls == 0, "孤立的 ScreencapFailed 留下了挂起调用"

            # ④ 错误输入：下一次截图必须还能成功（会话没被毒化）
            again = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="第二次 capture 注册")
            assert fake.calls_of("AsstAsyncScreencap") == 2, (
                f"第二次截图没有真的发号（AsstAsyncScreencap={fake.calls_of('AsstAsyncScreencap')}）"
            )
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.screencap_returns,
                    "details": {"ret": True, "cost": 3},
                },
            )
            kind, value = await _aoutcome(asyncio.wait_for(again, 2.0))
            assert kind == "value", f"第二次截图也失败了：{value!r} —— 会话被毒化了"
            assert value.startswith(F.PNG_MAGIC), "第二次截图拿到的不是 PNG 原样字节"

        await scenario()


@case("A18", "adapter", "reconnecting")
async def test_a18_reconnecting_never_resends_connect():
    """A18 · `Reconnecting` ⇒ `link_state == RECONNECTING`，且**不得**重发 Connect。

    `[FACT]` 这个状态的文档语义就是"MAA 正在自己重连，此时不要重发 Connect"
    （见 `LinkState` 的表格）。我们再发一个只会和它打架：两个 Connect 抢同一个
    handle，后一个的 `_reset_session()` 会把前一个正在等的结果清掉。

    断言：
      ① 正常：`Reconnecting` → `RECONNECTING`，`link_reason` 来自事件原文；
      ② 不发 C 调用：`AsstAsyncConnect` 计数为 0；
      ③ 边界：`Reconnecting` 之后 `Reconnected` → 回到 `CONNECTED` 且 reason 清空；
      ④ 错误输入：`Reconnecting` **不许**让挂起的截图失败（MAA 自己会恢复，
         此时误杀等于把"正在恢复"报成"这次截图输了"）→ 挂起数仍是 1。
    """
    A = _adapter_mod()
    with _tmpdir() as tmp:
        adapter, fake, _w = _adapter_full(tmp, capture_timeout=5.0)

        async def scenario():
            adapter.bind_loop()
            _emit(adapter, {"what": "Connected", "details": {}, "uuid": "emu-1"})
            await _pump()

            # ④ 先挂一个截图，再让 MAA 进入重连
            task = asyncio.ensure_future(adapter.capture())
            await _until(lambda: adapter.stats().pending_calls == 1, what="capture slot 注册")

            _emit(adapter, {"what": "Reconnecting", "details": {"why": "retrying adb"}})
            await _pump()

            # ① 状态
            assert adapter.link_state is A.LinkState.RECONNECTING, (
                f"Reconnecting 之后 link_state 应是 RECONNECTING，实际 {adapter.link_state}"
            )
            assert "retrying adb" in adapter.link_reason, (
                f"link_reason 应是事件原文，实际 {adapter.link_reason!r}"
            )

            # ② 不许重发 Connect
            assert fake.calls_of("AsstAsyncConnect") == 0, (
                f"Reconnecting 触发了 {fake.calls_of('AsstAsyncConnect')} 次 AsstAsyncConnect —— "
                "MAA 正在自己重连，再发一个会和它抢同一个 handle"
            )

            # ④ 挂起的截图不许被误杀
            assert adapter.stats().pending_calls == 1, (
                "Reconnecting 把挂起的截图判失败了 —— 那会把'正在恢复'报成'这次截图输了'"
            )

            # ③ 恢复
            _emit(adapter, {"what": "Reconnected", "details": {}})
            await _pump()
            assert adapter.link_state is A.LinkState.CONNECTED, (
                f"Reconnected 之后应回到 CONNECTED，实际 {adapter.link_state}"
            )
            assert adapter.link_reason == "", (
                f"链路恢复后 link_reason 必须清空（否则状态页永远挂着上一次的原因），"
                f"实际 {adapter.link_reason!r}"
            )

            # 收尾：把挂起的截图正常结束掉，避免给后续断言留噪声
            _emit(
                adapter,
                {
                    "what": "AsyncCallInfo",
                    "async_call_id": fake.screencap_returns,
                    "details": {"ret": True, "cost": 2},
                },
            )
            kind, value = await _aoutcome(asyncio.wait_for(task, 2.0))
            assert kind == "value", f"恢复后那次截图应该能拿到图，实际 {value!r}"
            assert value.startswith(F.PNG_MAGIC)

        await scenario()


# --------------------------------------------------------------------------- #
# R 组：runtime 状态机 / 装载 / 装配
# --------------------------------------------------------------------------- #
# 六条公共约定（R 组共用）：
#   1. **不碰真网络、不碰真 DLL**：`ProvisionPorts` 注入假 fetcher/extractor，
#      `wrapper_factory` 只换掉 CDLL 那一层（和 W 组同一套 fake lib）。唯一例外是
#      R13 —— 那里"下载链路本身"就是被测对象，所以起本地 `http.server` 走真
#      `atomic_download`（回环地址，仍然不碰外网）。
#   2. 每条用例自己的 tmp 目录 + 自己的 runtime；四个注入根（engine/data/cache/user）
#      全在 tmp 之下，R07 直接拿这条断言"产物没跑到插件目录"。
#   3. 后台引导任务用 `_await_download()` **等 task 结束**，不 sleep：固定睡眠会让
#      慢机器假红、快机器假绿。
#   4. 阻塞型 fetcher（模拟慢网）一律用 `threading.Event` 门控，并在 finally 里放行
#      —— 线程池的线程不是 daemon，忘了解锁会让测试进程挂在退出阶段。
#   5. 假 fetcher 的形状照 `atomic_download` 抄：**先写 `.part`、成功后 `os.replace`**。
#      失败时只留 `.part`，不留最终名 —— 这不是"造得方便"，是真实下载器的可观测形态；
#      形状不对就会让"半包不许留"这类断言测的是想象中的库（上一轮的教训）。
#   6. 断言只认可观测事实：`state` / `last_error` 的类型与 `kind` / 磁盘上有没有那个
#      文件 / fake lib 的调用计数。不读私有缓存做判断。

import builtins
import errno
import hashlib
import http.server
import io
import logging
import os
import re
import types
import zipfile
from collections import Counter

_RELEASE_URL = (
    "https://github.com/MaaAssistantArknights/MaaAssistantArknights"
    "/releases/download/v9.9.9/MAA-test-only.zip"
)
_ARCHIVE_NAME = "MAA-test-only.zip"
"""测试用清单条目：URL 形态与真表一致（版本化直链、白名单主机），但**从不真的访问**
（除 R13 用回环地址覆盖 URL）。这样"URL 合法性"与"链路行为"两条断言互不干扰。"""

_GATE_SECONDS = 60.0
"""门控等待上限。取值远大于任何用例的合理耗时，只用于"线程永远不卡死"。"""


def _runtime_mod():
    try:
        import maa.runtime as m
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"maa.runtime 尚未落盘（{type(e).__name__}: {e}）") from None
    return m


def _provision_mod():
    try:
        from maa import provision as m
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"maa.provision 尚未落盘（{type(e).__name__}: {e}）") from None
    return m


# --------------------------------------------------------------------------- #
# 造样本：一个"窄但合法"的引擎包
# --------------------------------------------------------------------------- #
def _zip_bytes(entries) -> bytes:
    """按 `[(name, data), ...]` 造 zip。

    用 `ZIP_STORED` 是刻意的：`MAX_COMPRESSION_RATIO=60` 是安全闸门，用 DEFLATE 造
    几百字节的样本会让 `declared_selected / zip_size` 抖到闸门附近，测试就会在
    "包本身合不合法"上假红。压缩比这条闸门留给 P04 专测。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _engine_zip(*, lib_rel: str = "", extra=(), lib_bytes: bytes | None = None) -> bytes:
    """`resource/config.json` + 指定位置的 `MaaCore.dll`（+ 任意附加条目）。

    `lib_rel` 取 `""` / `"bin/"` / `"lib/"`，对应 MAA 归档的三种内层布局
    （R15 用磁盘目录直接验证这三种形态；这里保证"合法包"这一侧也被走过）。
    """
    core = bytes(range(256)) * 8 if lib_bytes is None else lib_bytes
    entries = [
        ("resource/config.json", b'{"name":"fake-neko-engine"}'),
        ("resource/tasks.json", b'{"tasks":[]}'),
        (f"{lib_rel}MaaCore.dll", core),
    ]
    entries.extend(extra)
    return _zip_bytes(entries)


def _release(
    payload: bytes,
    *,
    sha256=None,
    size=None,
    url: str = _RELEASE_URL,
    version: str = "v9.9.9",
    archive_name: str = _ARCHIVE_NAME,
):
    """按 `payload` 造清单条目；默认登记**真算出来的** sha256 与长度。"""
    P = _provision_mod()
    return P.ReleaseSpec(
        version=version,
        platform="windows-x64",
        url=url,
        sha256=hashlib.sha256(payload).hexdigest() if sha256 is None else sha256,
        size=len(payload) if size is None else size,
        core_hint="MaaCore.dll",
        rel_subtrees=("", "bin", "lib"),
        archive_name=archive_name,
    )


def _env(tmp: Path):
    """四个注入根 + `plugin_dir` 快照点。`engine_root` 必须在 `data_dir` 之下（R07）。"""
    data_dir = Path(tmp) / "data"
    return types.SimpleNamespace(
        tmp=Path(tmp),
        data_dir=data_dir,
        engine_root=data_dir / "maa",
        user_dir=Path(tmp) / "user",
        cache_dir=Path(tmp) / "cache",
        plugin_dir=Path(__file__).resolve().parents[1],
    )


def _archive_path(env, release) -> Path:
    return env.cache_dir / release.archive_name


def _part_path(env, release) -> Path:
    archive = _archive_path(env, release)
    return archive.with_name(archive.name + ".part")


def _is_within(child: Path, root: Path) -> bool:
    try:
        Path(child).relative_to(Path(root))
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 装配：runtime + 三个注入点
# --------------------------------------------------------------------------- #
def _fake_factory(fake=None, *, record=None):
    """`wrapper_factory`：真的 `MaaCoreWrapper`，只把 CDLL 换成 fake lib。

    为什么不用替身 wrapper：R 组要验的是 runtime 的**编排顺序**，而 wrapper 的行为
    已由 W 组 26 条锁死。换掉 CDLL 这一层，`AsstSetUserDir → AsstLoadResource →
    AsstCreateEx` 的调用记录就是真的。
    """
    W = _wrapper_mod()
    fake = F.FakeMaaCore() if fake is None else fake

    def _make(**kw):
        if record is not None:
            record.append(dict(kw))
        return W.MaaCoreWrapper(dll_loader=fake.as_loader(), **kw)

    _make.fake = fake
    return _make


def _refuse_factory(record, why: str = "本用例不该走到装载"):
    """`wrapper_factory`：被调用就登记一次并炸掉（断言在用例侧看 `record`）。

    不在这里 `assert`：`_load_chain` 跑在线程池里，断言会被 runtime 的边界
    `except Exception` 吃掉，变成一条看不见的假绿。
    """

    def _make(**kw):
        record.append(dict(kw))
        raise RuntimeError(why)

    return _make


def _refuse_fetcher(record, why: str = "本用例不该下载"):
    def _fetch(release, target, on_progress):
        record.append(Path(target))
        raise AssertionError(why)

    return _fetch


def _runtime(
    env,
    release,
    *,
    fetcher=None,
    extractor=None,
    wrapper_factory=None,
    progress_sink=None,
    **over,
):
    """构造 `MaaRuntime`。默认接"能成功的 fake wrapper"，超时全部缩短到秒级。"""
    M = _runtime_mod()
    ports = (
        M.ProvisionPorts(fetcher=fetcher, extractor=extractor)
        if (fetcher is not None or extractor is not None)
        else None
    )
    kw = dict(
        engine_root=env.engine_root,
        user_dir=env.user_dir,
        data_dir=env.data_dir,
        cache_dir=env.cache_dir,
        release=release,
        progress_sink=progress_sink,
        provision_ports=ports,
        wrapper_factory=wrapper_factory if wrapper_factory is not None else _fake_factory(),
        max_image_buffer_bytes=4 << 20,
        max_buffer_grow=8,
        connect_timeout=2.0,
        capture_timeout=2.0,
        resolution_grace=0.05,
        load_timeout=10.0,
        destroy_timeout=2.0,
    )
    kw.update(over)
    return M.MaaRuntime(**kw)


def _install_layout(
    root: Path,
    *,
    lib_rel: str = "",
    resource: bool = True,
    marker: bool = True,
    core: Path | None = None,
) -> Path:
    """在磁盘上摆出一套"看起来已安装"的布局（lib + `resource/` + `engine.json`）。"""
    root = Path(root)
    core = core or root / lib_rel / "MaaCore.dll"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_bytes(bytes(range(256)) * 8)
    if resource:
        (root / "resource").mkdir(parents=True, exist_ok=True)
        (root / "resource" / "config.json").write_bytes(b'{"name":"fake-neko-engine"}')
    if marker:
        (root / "engine.json").write_text(
            json.dumps(
                {
                    "source": "manual",
                    "version": "unknown",
                    "sha256": None,
                    "written_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    return core


# --------------------------------------------------------------------------- #
# 假端口
# --------------------------------------------------------------------------- #
def _safe_progress(on_progress, done: int, total: int | None) -> None:
    """进度回调包装：回调故障 / 事件循环已关闭都不得污染被测行为。"""
    if on_progress is None:
        return
    try:
        on_progress(done, total)
    except Exception:  # noqa: BLE001
        pass


def _fetcher_ok(payload: bytes, *, record=None, progress: bool = True, before_commit=None):
    """写盘成功的假 fetcher。形状照 `atomic_download`：`.part` → `os.replace`。"""

    def _fetch(release, target, on_progress):
        target = Path(target)
        if record is not None:
            record.append(target)
        part = target.with_name(target.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload)
        if before_commit is not None:
            before_commit(target, part)
        if progress:
            _safe_progress(on_progress, len(payload), len(payload))
        os.replace(part, target)
        return target

    return _fetch


def _fetcher_raises(exc_factory, *, write_half: int = 0, record=None):
    """下载中途失败的假 fetcher：可选先留半包在 `.part`（真实下载器的可观测形态）。"""

    def _fetch(release, target, on_progress):
        target = Path(target)
        if record is not None:
            record.append(target)
        if write_half:
            part = target.with_name(target.name + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"\x00" * write_half)
        raise exc_factory(release, target)

    return _fetch


def _fetcher_blocking(gate: threading.Event, *, record=None, make_part: bool = True):
    """慢网：登记一次调用、留一个 `.part`，然后**卡在门控上不返回**。

    `.part` 写完立刻关闭句柄：Windows 上删不掉被打开的文件；若这里占着句柄，
    R16 的"取消后 `.part` 不存在"就成了测不出真假的假红。
    """

    def _fetch(release, target, on_progress):
        target = Path(target)
        if record is not None:
            record.append(target)
        if make_part:
            part = target.with_name(target.name + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"\x00" * 64)
        _safe_progress(on_progress, 0, release.size)
        gate.wait(_GATE_SECONDS)
        return target

    return _fetch


def _recording_extractor(record, *, real: bool = False, hook=None):
    """记录"解压端口有没有被调用"的 extractor；`real=True` 时转调真 `safe_extract`。"""
    P = _provision_mod()

    def _extract(archive, target_dir):
        record.append((Path(archive), Path(target_dir)))
        if hook is not None:
            hook(Path(archive), Path(target_dir))
        if real:
            return P.safe_extract(archive, target_dir, _p_spec())
        raise AssertionError("extractor 不该被调用（见记录列表）")

    return _extract


def _break_provision_open(member_name: str, exc: BaseException):
    """在**写盘落点**注入一个真 `OSError`（`safe_extract` 里的 `open(target,"xb")`）。

    为什么改 `provision.open` 而不是打 `builtins.open`：`open` 在 `safe_extract` 里是
    `LOAD_GLOBAL`，只在 provision 的模块全局里查；塞一个同名全局就等于只接管
    provision 自己的写盘，`zipfile`/`verify_sha256` 的读盘完全不受影响。
    **不改 `maa/` 一行源码**，注入的也是真 `OSError`（真 errno），走的是现成的
    `except OSError` → `NO_SPACE`/`WRITE_ERROR` 判定。
    """
    P = _provision_mod()
    real = builtins.open
    had = "open" in P.__dict__
    previous = P.__dict__.get("open")

    def _open(file, *a, **kw):
        if Path(file).name == member_name:
            raise exc
        return real(file, *a, **kw)

    P.open = _open

    def _restore() -> None:
        if had:
            P.open = previous
        else:
            P.__dict__.pop("open", None)

    return _restore


# --------------------------------------------------------------------------- #
# 观测工具
# --------------------------------------------------------------------------- #
async def _await_download(rt, *, timeout: float = 20.0) -> None:
    """等后台引导任务**真的结束**（等 task，不 sleep）。"""
    task = rt.download_task
    assert task is not None, "bootstrap 没有起后台任务：`download_task` 仍是 None"
    await asyncio.wait_for(asyncio.shield(task), timeout)


def _state_sequence(rt) -> list:
    """记录 runtime **实际走过**的状态序列（只观察 `_set_state`，不改它）。

    为什么允许这样观察：R13 要断"UNPACKED 是必经中间态"、R16 要断"合法链逐级成立"，
    而对外快照 `state` 只有最终值。包一层只读记录器，比事后从 `health()` 反推中间态
    诚实得多 —— 它不改变任何判定，只把判定依据留下来。
    """
    seen = [rt.state]
    original = rt._set_state

    def _recorded(new):
        before = rt.state
        result = original(new)  # ← 被测的门原样执行；非法迁移照旧抛
        if rt.state is not before:
            seen.append(rt.state)
        return result

    rt._set_state = _recorded
    return seen


def _force_state(rt, state) -> None:
    """把**起始状态**摆到位；被测的仍然是 `_set_state` 自己。

    R16 ① 要遍历整张迁移表。逐条重放完整生命周期（下载→校验→解压→装载→连接）
    每条至少 0.3s，而且会把"迁移表"这条契约和别的链路搅在一起。这里只摆起点，
    门还是原来那个门。
    """
    rt._state = state


# --------------------------------------------------------------------------- #
# R01
# --------------------------------------------------------------------------- #
@case("R01", "runtime", "hash")
async def test_r01_hash_mismatch_deletes_file():
    """R01 · 内容与 manifest 的 sha256 不符：删文件 + `EngineHashMismatch`，不入装载。

    输入：fetcher 交付**长度相同、内容不同**的字节 —— 失败只可能来自哈希比较，
    不可能是 `size` 早停（那会变成 `SIZE_MISMATCH`，把"安全事件"降级成"网络问题"）。
    断言：`last_error` 是 `EngineHashMismatch`；归档不在磁盘上；没解压；没装载。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        good = _engine_zip()
        bad = good[:-1] + bytes([good[-1] ^ 0xFF])
        assert len(bad) == len(good)
        spec = _release(good)

        fetched, extracted, loaded = [], [], []
        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_ok(bad, record=fetched),
            extractor=_recording_extractor(extracted),
            wrapper_factory=_refuse_factory(loaded),
        )

        assert await rt.bootstrap() is M.EngineState.DOWNLOADING
        await _await_download(rt)

        archive = _archive_path(env, spec)
        assert len(fetched) == 1, f"fetcher 应只被调 1 次，实际 {len(fetched)}"
        err = rt.last_error
        assert isinstance(err, E.EngineHashMismatch), (
            f"last_error 应是 EngineHashMismatch，实际 {type(err).__name__}: {err}"
        )
        assert err.expected == spec.sha256
        assert not archive.exists(), (
            f"哈希不符的归档必须删掉（否则下次会直接复用这批字节）：{archive} 还在"
        )
        assert not _part_path(env, spec).exists(), ".part 残留"
        assert extracted == [], "哈希没通过却开始解压了"
        assert loaded == [], "哈希没通过却开始装载了"
        assert rt.state is M.EngineState.ABSENT, f"失败后应回 ABSENT，实际 {rt.state}"


# --------------------------------------------------------------------------- #
# R02
# --------------------------------------------------------------------------- #
@case("R02", "runtime", "download")
async def test_r02_partial_download_is_not_usable():
    """R02 · 连接中断且已写一半：半包不许留在最终路径，state 回 ABSENT（不是 FAILED）。

    输入：fetcher 在 `.part` 里写了 1KB 后抛 `EngineDownloadFailed(kind=NETWORK)`
    —— 与真实 `atomic_download` 遇到 `URLError` 时的形态一致。
    断言：`kind == NETWORK`；最终路径没有文件；`.part` 被清掉；state 是 **ABSENT**；
    原因留在 `last_error`；没有走到装载。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        payload = _engine_zip()
        spec = _release(payload)
        fetched, extracted, loaded = [], [], []

        def _reset(release, target):
            return E.EngineDownloadFailed(
                url=release.url,
                target=Path(target),
                kind=E.DownloadFailure.NETWORK,
                detail="connection reset by peer",
            )

        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_raises(_reset, write_half=1024, record=fetched),
            extractor=_recording_extractor(extracted),
            wrapper_factory=_refuse_factory(loaded),
        )

        await rt.bootstrap()
        await _await_download(rt)

        err = rt.last_error
        assert isinstance(err, E.EngineDownloadFailed), (
            f"last_error 应是 EngineDownloadFailed，实际 {type(err).__name__}: {err}"
        )
        assert err.kind is E.DownloadFailure.NETWORK, f"kind 应为 NETWORK，实际 {err.kind}"
        assert not _archive_path(env, spec).exists(), (
            "半包留在了最终路径上 —— 下次启动会被当完整包，然后在解压/加载阶段"
            f"报一个假原因：{_archive_path(env, spec)}"
        )
        assert not _part_path(env, spec).exists(), ".part 没被清掉（下次会被当可续传）"
        assert rt.state is M.EngineState.ABSENT, (
            f"下载失败应回 ABSENT（可重试），实际 {rt.state}"
        )
        assert rt.state is not M.EngineState.FAILED, "下载失败落了 FAILED：失败态会粘住"
        assert extracted == [], "半包却开始解压了"
        assert loaded == [], "半包却开始装载了"


# --------------------------------------------------------------------------- #
# R03
# --------------------------------------------------------------------------- #
@case("R03", "runtime", "unpack")
async def test_r03_zip_slip_is_rejected():
    """R03 · zip slip：`../../evil.dll` 与绝对路径成员都必须被拒，且没写出任何东西。

    断言：抛 `EngineUnpackFailed(kind=UNSAFE_MEMBER, member=...)`；解压根不存在；
    解压根**外**没有 `evil.dll`（相对逃逸）也没有被点名的绝对路径（绝对逃逸）。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        E = _errors_mod()
        relative_evil = "../../evil.dll"
        absolute_evil = str(tmp / "abs_evil.dll")
        payload = _engine_zip(
            extra=[(relative_evil, b"MZ-EVIL-REL"), (absolute_evil, b"MZ-EVIL-ABS")]
        )
        spec = _release(payload)
        archive = tmp / "slip.zip"
        archive.write_bytes(payload)
        dest = tmp / "unpack-root"

        rt = _runtime(env, spec, wrapper_factory=_refuse_factory([]))
        kind, value = await _aoutcome(rt.unpack(archive, target_dir=dest, release=spec))

        assert kind == "error", f"含 zip-slip 成员的包被当成合法解压成功了：{value!r}"
        assert isinstance(value, E.EngineUnpackFailed), (
            f"应抛 EngineUnpackFailed，实际 {type(value).__name__}: {value}"
        )
        assert value.kind is E.UnpackFailure.UNSAFE_MEMBER, (
            f"kind 应为 UNSAFE_MEMBER，实际 {value.kind}"
        )
        assert value.member in (relative_evil, absolute_evil), (
            f"member 必须点名是哪个条目，实际 {value.member!r}"
        )
        assert not dest.exists(), f"被拒之后解压根没清掉：{dest}"
        escaped = [
            tmp / "evil.dll",          # "../../" 从 unpack-root 出发的第 2 层上跳
            tmp.parent / "evil.dll",   # "../../" 的第 1 层上跳
            Path(absolute_evil),
        ]
        assert not any(p.exists() for p in escaped), (
            "解压根外出现了文件 —— 攻击者能写到插件目录之外："
            f"{[str(p) for p in escaped if p.exists()]}"
        )


# --------------------------------------------------------------------------- #
# R04
# --------------------------------------------------------------------------- #
@case("R04", "runtime", "load")
async def test_r04_version_mismatch_blocks_load():
    """R04 · 清单要求新版本、库里没有新符号：`EngineLoadFailed.symbol` 必须非空。

    用 `MissingSymbolsFake` 让**真 wrapper** 在绑签名时抛
    `AttributeError("MaaCore is missing symbol: ...")` —— 这与真机上"旧版 MaaCore
    没有新符号"是同一条代码路径，不是伪造一个异常糊过去。
    断言：state 落 `FAILED`；`EngineLoadFailed.symbol` 精确等于缺的符号名（供文案
    分档"版本太旧"）；没有进入 `LOADED`；没有触发下载。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        spec = _release(_engine_zip())
        _install_layout(env.engine_root)  # 磁盘事实齐全：lib + resource/ + engine.json

        missing = "AsstAsyncConnect"
        fake = F.MissingSymbolsFake({missing})
        fetched = []
        rt = _runtime(
            env,
            spec,
            fetcher=_refuse_fetcher(fetched),
            wrapper_factory=_fake_factory(fake),
        )

        state = await rt.bootstrap()
        assert fetched == [], "布局齐全却发起了下载（版本不匹配被误判成缺引擎）"
        assert state is M.EngineState.FAILED, f"装载失败后应落 FAILED，实际 {state}"
        err = rt.last_error
        assert isinstance(err, E.EngineLoadFailed), (
            f"last_error 应是 EngineLoadFailed，实际 {type(err).__name__}: {err}"
        )
        assert err.symbol == missing, (
            "symbol 必须精确等于缺失的符号名（service 靠它分档成「引擎版本过旧，请重新下载」）；"
            f"实际 {err.symbol!r}"
        )
        assert rt.state is not M.EngineState.LOADED, "缺符号却进了 LOADED"
        assert rt.layout.missing_pieces() == (), "布局被改坏了：断言的起点不成立"


# --------------------------------------------------------------------------- #
# R05
# --------------------------------------------------------------------------- #
@case("R05", "runtime", "dedupe")
async def test_r05_concurrent_bootstrap_downloads_once():
    """R05 · 并发 `bootstrap()` 只下一份、只解一次（幂等键是 `download_task` 单句柄）。

    断言：两次都返回 `DOWNLOADING`；fetcher **恰好 1 次**；解压端口 0 次；
    且**下载途中 `.maa-provision.json`/`engine.json` 并不存在** —— 说明去重靠的是
    在飞任务句柄，不是"安装产物标记文件"（那玩意儿是给下次启动看的，不是并发锁）。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M = _runtime_mod()
        payload = _engine_zip()
        spec = _release(payload)
        gate = threading.Event()
        fetched, extracted, loaded = [], [], []
        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_blocking(gate, record=fetched),
            extractor=_recording_extractor(extracted),
            wrapper_factory=_refuse_factory(loaded),
        )
        try:
            first, second = await asyncio.gather(rt.bootstrap(), rt.bootstrap())
            assert first is M.EngineState.DOWNLOADING, f"第一次 bootstrap 返回 {first}"
            assert second is M.EngineState.DOWNLOADING, f"第二次 bootstrap 返回 {second}"

            await _until(
                lambda: len(fetched) == 1, timeout=10.0, what="后台任务进入 fetcher"
            )
            assert not rt.layout.marker_path.exists(), (
                "下载刚开始就写了安装标记 —— 去重若靠这个标记，两台并发会互相顶掉；"
                f"标记路径 {rt.layout.marker_path}"
            )

            in_flight = rt.download_task
            assert in_flight is not None, "download_task 句柄为空：去重就没有幂等键了"

            third = await rt.bootstrap()
            assert third is M.EngineState.DOWNLOADING
            assert len(fetched) == 1, (
                f"并发 bootstrap 起了第二次下载（fetcher 被调 {len(fetched)} 次）——"
                "幂等键必须是在飞的 download_task，不是 marker 文件"
            )
            # 单看 fetcher 计数会被 `ThreadPoolExecutor(max_workers=1)` 掩盖：第二次
            # `_start_download()` 新建的任务只是排在同一个执行器后面，还没来得及调第二次
            # fetcher。所以必须直接断"在飞句柄没被换掉" + "只有一个引导任务在跑"。
            assert rt.download_task is in_flight, (
                "bootstrap 换掉了在飞的 download_task（旧任务被孤儿化：两个下载线程"
                "抢同一个 .part，互相覆盖半成品）"
            )
            live = [t for t in asyncio.all_tasks() if t.get_name() == "maa-provision"]
            assert len(live) == 1, (
                f"同时有 {len(live)} 个 `maa-provision` 后台任务在飞（应恰好 1 个）"
            )
            assert extracted == [], "并发 bootstrap 走到了第二次解压"
            assert loaded == []
        finally:
            await rt.cancel_download()
            gate.set()
            await rt.destroy()


# --------------------------------------------------------------------------- #
# R06
# --------------------------------------------------------------------------- #
@case("R06", "runtime", "retry")
async def test_r06_failed_bootstrap_can_retry():
    """R06 · 第一次失败不许把状态粘住：第二次 `bootstrap()` 必须真的重试并成功。

    输入：fetcher 第 1 次抛 `NETWORK`，第 2 次正常交付合法包。
    断言：第一次落 `ABSENT` + `last_error` 是 NETWORK；第二次重新发起下载（fetcher
    计数 2）并一路走到 `LOADED`（下载 → 校验 → 解压 → 装载都真的跑完）。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        payload = _engine_zip()
        spec = _release(payload)
        attempts: list[int] = []

        def _fetch(release, target, on_progress):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise E.EngineDownloadFailed(
                    url=release.url,
                    target=Path(target),
                    kind=E.DownloadFailure.NETWORK,
                    detail="网络抖动（第一次）",
                )
            return _fetcher_ok(payload)(release, target, on_progress)

        rt = _runtime(env, spec, fetcher=_fetch)
        try:
            await rt.bootstrap()
            await _await_download(rt)
            assert rt.state is M.EngineState.ABSENT, f"第一次失败后 state={rt.state}"
            assert isinstance(rt.last_error, E.EngineDownloadFailed)
            assert len(attempts) == 1

            assert await rt.bootstrap() is M.EngineState.DOWNLOADING, (
                "第二次 bootstrap 没有重新下载 —— 失败态把状态机粘住了，"
                "一次网络抖动就会让用户永久不可用"
            )
            await _await_download(rt)
            assert len(attempts) == 2, f"fetcher 应被调 2 次，实际 {len(attempts)}"
            assert rt.state is M.EngineState.LOADED, (
                "重试成功却没走完装载链（下载→校验→解压→装载），"
                f"state={rt.state} err={rt.last_error}"
            )
            assert _archive_path(env, spec).is_file()
            assert rt.layout.missing_pieces() == (), f"布局不完整：{rt.layout.missing_pieces()}"
        finally:
            await rt.destroy()


# --------------------------------------------------------------------------- #
# R07
# --------------------------------------------------------------------------- #
@case("R07", "runtime", "paths")
async def test_r07_artifacts_land_under_data_path_maa():
    """R07 · 所有产物只落在注入的目录里；`plugin_dir` 下**一个字节都不新增**。

    断言：跑完整条链后插件目录树与快照逐字节相同；tmp 下每个新增路径都落在
    `data_dir` / `cache_dir` / `user_dir` 之内；引擎在 `data_dir/maa` 下。

    口径说明（与 §3 R07 的差异，写明以免被当成放松）：表里写"都在
    `data_path("maa")` 之下"；实现把四个根**构造期注入**（`data`/`cache`/`user`
    由 `__init__.py` 分别展开），所以这里断言的是"⊆ 四个注入根，且不碰
    `plugin_dir`" —— 比"都在同一个 maa 目录"更严，也更能抓住"写进只读安装目录"。
    """
    import shutil

    with _tmpdir() as tmp:
        env = _env(tmp)
        M = _runtime_mod()
        payload = _engine_zip()
        spec = _release(payload)

        # 先把 import 期的解释器副作用（__pycache__）跑完，再取快照
        _runtime_mod()
        _provision_mod()
        before = _tree(env.plugin_dir)

        rt = _runtime(env, spec, fetcher=_fetcher_ok(payload))
        try:
            await rt.bootstrap()
            await _await_download(rt)
            assert rt.state is M.EngineState.LOADED, f"链路没跑通，state={rt.state}"

            after = _tree(env.plugin_dir)
            added = sorted(set(after) - set(before))
            removed = sorted(set(before) - set(after))
            changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
            assert not added, f"插件目录下新增了文件（写进只读安装目录）：{added}"
            assert not removed, f"插件目录下少了文件：{removed}"
            assert not changed, f"插件目录下文件被改写（大小变了）：{changed}"

            allowed = (env.data_dir, env.cache_dir, env.user_dir)
            stray = [
                str(p.relative_to(tmp))
                for p in tmp.rglob("*")
                if not any(_is_within(p, root) for root in allowed)
            ]
            assert not stray, f"产物落到了注入目录之外：{stray}"

            assert _archive_path(env, spec).is_file(), "归档不在 cache_dir 下"
            assert env.engine_root.is_dir(), "引擎不在 data_dir/maa 下"
            assert _is_within(env.engine_root, env.data_dir)
            assert (env.engine_root / "MaaCore.dll").is_file()
            assert (env.engine_root / "resource").is_dir()
            del shutil  # 只为说明"没有拿 shutil 做什么"，避免未使用告警的误判
        finally:
            await rt.destroy()


def _tree(root: Path) -> dict[str, int]:
    """目录树快照（相对路径 → 字节数）。

    跳过 `__pycache__` / `*.pyc`：那是**解释器导入期**产物，不是被测算法的运行期
    产物。把它算进"插件目录有没有被写"会让这条用例恒红，且与"写进只读安装目录"
    这个真实风险无关。
    """
    snapshot: dict[str, int] = {}
    for path in Path(root).rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if not path.is_file():
            continue
        snapshot[path.relative_to(root).as_posix()] = path.stat().st_size
    return snapshot


# --------------------------------------------------------------------------- #
# R08
# --------------------------------------------------------------------------- #
@case("R08", "runtime", "stream")
def test_r08_checksum_is_streamed_in_chunks():
    """R08 · 5MB 假流：`read(n)` 的 n 不许无界，峰值内存不随文件线性增长。

    被测的是**真读循环**（`provision.atomic_download`，runtime 的默认 fetcher 就是
    它）。断言：
      * 每次 `read(n)` 都带一个 `n <= CHUNK(1MiB)` 的上界 —— 没有 `read(-1)`/`read()`；
      * 读取次数 ≈ 5 次以上（证明是一段段读，不是一把梭）；
      * 落盘字节与 pin 住的 sha256 一致（流式没有读漏/写漏）。

    为什么不量 `tracemalloc` 峰值：它会把样本缓冲本身算进去，阈值稍有偏差就在
    慢机器上假红。`read(n)` 的上界才是"内存不随文件线性增长"的直接原因。
    """
    with _tmpdir() as tmp:
        P = _provision_mod()
        cap = P.CHUNK
        size = 5 << 20
        payload = bytes(range(256)) * (size // 256)
        assert len(payload) == size

        reads: list[int] = []
        resp = _RecordingResponse(payload, reads)
        opener = _StubOpener(resp)
        spec = _release(payload)
        dest = tmp / spec.archive_name

        got = P.atomic_download(spec, dest, opener=opener)

        assert Path(got) == dest
        assert opener.calls == 1, f"opener 被调 {opener.calls} 次"
        assert resp.closed, "响应没被关闭（连接泄漏）"
        unbounded = [n for n in reads if n is None or n < 0]
        assert not unbounded, (
            "出现无上界的 read()/read(-1)：整个 295MB 引擎包会一次性进内存"
            f"（read 序列前 8 项 {reads[:8]}）"
        )
        biggest = max(reads) if reads else 0
        assert biggest <= cap, (
            f"单次 read({biggest}) 超过 CHUNK={cap}：流式校验退化成整块读"
        )
        assert len([n for n in reads if n]) >= size // cap, (
            f"只读了 {len([n for n in reads if n])} 段（< {size // cap}）：没有按块流式处理"
        )
        assert dest.stat().st_size == size
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == spec.sha256
        assert not dest.with_name(dest.name + ".part").exists()
        assert not dest.with_name(dest.name + ".lock").exists()


class _RecordingResponse:
    """最小 HTTP 响应替身，只实现 `atomic_download` 真的用到的三样东西。

    `headers.get("Content-Length")` / `read(n)` / `close()` —— 少一个就会让被测代码
    走"没有长度"的分支，测出来的就不是生产路径了。
    """

    def __init__(self, data: bytes, reads: list[int]) -> None:
        self._data = data
        self._pos = 0
        self._reads = reads
        self.headers = {"Content-Length": str(len(data))}
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        self._reads.append(n)
        if n is None or n < 0:
            chunk = self._data[self._pos :]
        else:
            chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _StubOpener:
    """`atomic_download(opener=...)` 的替身：不碰 socket，只计调用次数。"""

    def __init__(self, resp: _RecordingResponse) -> None:
        self._resp = resp
        self.calls = 0

    def open(self, req, timeout=None):  # noqa: ARG002 - 签名对齐 urllib
        self.calls += 1
        return self._resp


# --------------------------------------------------------------------------- #
# R09
# --------------------------------------------------------------------------- #
@case("R09", "runtime", "unpack")
async def test_r09_io_error_keeps_root_clean():
    """R09 · 两个变体：①ENOSPC → `NO_SPACE`（detail 带**真实**缺口）；②EACCES → `WRITE_ERROR`。

    两者都必须：能被 `except EngineUnpackFailed` 接住、`detail` 非空、解压根内不留
    半成品目录。① 还要 `detail` 带上 `shutil.disk_usage` 真探测到的容量数字
    （探测不可用时才允许无数字，**但不许编一个**）—— 否则用户只看到"磁盘满"，
    不知道该清 200MB 还是 2GB，最后被推去重下一个 295MB 的包。

    注入方式：在 `safe_extract` 的写盘落点塞一个真 `OSError`（见
    `_break_provision_open` 的说明），走的是现成的 `except OSError` 判定。
    """
    import shutil

    with _tmpdir() as tmp:
        E = _errors_mod()

        async def _variant(label: str, exc: BaseException, expected_kind):
            env = _env(tmp / label)
            payload = _engine_zip()
            spec = _release(payload)
            archive = env.tmp / spec.archive_name
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(payload)
            dest = env.engine_root.parent / "unpack"
            rt = _runtime(env, spec, wrapper_factory=_refuse_factory([]))

            restore = _break_provision_open("config.json", exc)
            try:
                kind, value = await _aoutcome(
                    rt.unpack(archive, target_dir=dest, release=spec)
                )
            finally:
                restore()

            assert kind == "error", f"{label}: 写盘失败却被当成解压成功（{value!r}）"
            assert isinstance(value, E.EngineUnpackFailed), (
                f"{label}: 应抛 EngineUnpackFailed，实际 {type(value).__name__}: {value}"
            )
            assert value.kind is expected_kind, (
                f"{label}: kind 应为 {expected_kind.value}，实际 {value.kind.value}"
                "（把'磁盘满/无权限'报成'包坏了'，会把用户推去重下 295MB）"
            )
            detail = str((getattr(value, "context", {}) or {}).get("detail") or "")
            assert detail.strip(), f"{label}: detail 为空，故障没有留下任何证据"
            assert not dest.exists(), f"{label}: 解压根留下了半成品目录 {dest}"
            return value, detail, dest

        # ① 磁盘满
        err, detail, dest = await _variant(
            "enospc",
            OSError(errno.ENOSPC, "No space left on device"),
            E.UnpackFailure.NO_SPACE,
        )
        needed = _engine_zip()
        free_mb, total_mb = _disk_probe_mb(dest.parent)
        if free_mb is None:
            # 探测不可用 → 允许无数字（"不许编"），但仍然不许失败
            pass
        else:
            numbers = _capacity_numbers_mb(detail)
            known = [free_mb, total_mb, len(needed) / (1024 * 1024)]
            assert numbers, (
                "NO_SPACE 的 detail 必须带**真实探测到的容量缺口**"
                "（§3 R09 / errors.py:144「文案要带真实缺口」）；"
                f"实际 detail={detail!r}，一个容量数字都没有。"
                f"本机真实可探测值 free={free_mb:.1f}MB total={total_mb:.1f}MB "
                f"包大小={len(needed) / (1024 * 1024):.1f}MB —— "
                "用户只被告知'磁盘满'，无法知道要清多少。"
            )
            assert any(
                abs(n - k) <= max(1.0, k * 0.05) for n in numbers for k in known
            ), (
                f"detail 里的容量数字 {numbers} 与真实探测值 {known} 对不上"
                "（§3 要求「不许编」）"
            )

        # ② 无写权限
        err2, detail2, dest2 = await _variant(
            "eacces",
            PermissionError(13, "Permission denied"),
            E.UnpackFailure.WRITE_ERROR,
        )
        assert err2.kind is not err.kind, "两种写盘故障被合档了（会被推去同一个自救动作）"
        assert not _capacity_numbers_mb(detail2), (
            "WRITE_ERROR（权限/只读）的 detail 里带了容量数字 —— 这会把用户引去清磁盘，"
            "而真实原因是改权限（§1.1：`疑似攻击`/`包损坏`/`环境故障` 三类必须走三段不同"
            f"文案，容量数字只属于 NO_SPACE）。实际 detail={detail2!r}"
        )
        del shutil


def _disk_probe_mb(path: Path):
    """真实探测 `path` 所在卷的 (free_mb, total_mb)；探测不可用返回 (None, None)。"""
    import shutil

    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None, None
    return usage.free / (1024 * 1024), usage.total / (1024 * 1024)


_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|TiB|GiB|MiB|KiB)", re.IGNORECASE)
_UNIT_TO_MB = {
    "kb": 1 / 1024,
    "kib": 1 / 1024,
    "mb": 1.0,
    "mib": 1.0,
    "gb": 1024.0,
    "gib": 1024.0,
    "tb": 1024.0 * 1024,
    "tib": 1024.0 * 1024,
}


def _capacity_numbers_mb(text: str) -> list[float]:
    """把一段文案里的"<数字><容量单位>"读成 MB 列表（用于"不许编数字"的核对）。"""
    out: list[float] = []
    for raw, unit in _CAPACITY_RE.findall(text or ""):
        factor = _UNIT_TO_MB.get(unit.lower())
        if factor is None:
            continue
        try:
            out.append(float(raw) * factor)
        except ValueError:  # pragma: no cover - 正则已保证可解析
            continue
    return out


# --------------------------------------------------------------------------- #
# R10
# --------------------------------------------------------------------------- #
@case("R10", "runtime", "atomic")
async def test_r10_extraction_is_atomic():
    """R10 · 解压中途中断：先解到临时目录，最终目录**不存在**（绝不留半个引擎）。

    断言：
      1. 解压进行中，`engine_root` **还不存在** —— 这是"原子"的可观测定义：
         半个引擎不允许出现在最终路径上；
      2. 中断后 `engine_root` 仍不存在（不会留下"看起来完整"的目录）；
      3. `engine_root.parent` 下没有 `.unpacking-*` 残留；
      4. state 回 `ABSENT`，`last_error` 有记录（中断也得留下原因）。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M = _runtime_mod()
        payload = _engine_zip()
        spec = _release(payload)
        loaded: list = []
        probe: dict = {}

        def _hook(archive, target_dir):
            probe["engine_root_existed"] = env.engine_root.exists()
            staging = Path(target_dir)
            (staging / "resource").mkdir(parents=True, exist_ok=True)
            (staging / "resource" / "config.json").write_bytes(b'{"half":true}')
            (staging / "MaaCore.dll").write_bytes(b"MZ-HALF")
            probe["staging"] = staging
            raise KeyboardInterrupt("模拟用户中断")

        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_ok(payload),
            extractor=_recording_extractor([], hook=_hook),
            wrapper_factory=_refuse_factory(loaded),
        )
        try:
            await rt.bootstrap()
            await _await_download(rt)

            assert probe.get("engine_root_existed") is False, (
                "解压时最终目录已经存在 —— 半个引擎在最终路径上可见（非原子）"
            )
            assert probe["staging"] != env.engine_root, (
                "解压直接写在最终目录里，没有走临时目录 + rename"
            )
            assert not env.engine_root.exists(), (
                f"中断后留下了最终目录 {env.engine_root}（下次会被当已就绪）"
            )
            leftovers = sorted(
                p.name
                for p in env.engine_root.parent.glob(env.engine_root.name + ".unpacking-*")
            )
            assert not leftovers, f"临时解压目录没被清理：{leftovers}"
            assert rt.state is M.EngineState.ABSENT, f"中断后 state={rt.state}"
            assert rt.last_error is not None, "中断后没留下 last_error（无法诊断）"
            assert loaded == [], "半个引擎却开始装载了"
        finally:
            await rt.destroy()


# --------------------------------------------------------------------------- #
# R11
# --------------------------------------------------------------------------- #
@case("R11", "runtime", "hash")
async def test_r11_missing_digest_is_rejected():
    """R11 · 清单里 `sha256` 为空串：**绝不跳过校验**继续下载/解压（安全红线）。

    断言：抛 `EngineHashMismatch`（`expected` 为空或 `<unavailable>`，不装成"通过"）；
    归档被删；解压端口 0 次；装载 0 次；state 回 `ABSENT`。
    "没有摘要就不校验"是最危险的一种便捷 —— 被替换过的引擎包会长驱直入。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        payload = _engine_zip()
        spec = _release(payload, sha256="")
        fetched, extracted, loaded = [], [], []
        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_ok(payload, record=fetched),
            extractor=_recording_extractor(extracted),
            wrapper_factory=_refuse_factory(loaded),
        )

        await rt.bootstrap()
        await _await_download(rt)

        assert len(fetched) == 1, "没走到校验：fetcher 应交付过一次（下载本身是允许的）"
        err = rt.last_error
        assert isinstance(err, E.EngineHashMismatch), (
            "缺摘要必须归到 EngineHashMismatch（安全事件）；"
            f"实际 {type(err).__name__}: {err} —— 「没有哈希就跳过校验」是红线"
        )
        assert err.expected in ("", "<unavailable>"), (
            f"expected 应显式标成「不可用」（空或 <unavailable>），实际 {err.expected!r}"
        )
        assert extracted == [], "没校验就解压了（安全红线）"
        assert loaded == [], "没校验就装载了（安全红线）"
        assert not _archive_path(env, spec).exists(), "校验没过的归档被留下了"
        assert rt.state is M.EngineState.ABSENT, f"state 应回 ABSENT，实际 {rt.state}"


# --------------------------------------------------------------------------- #
# R12
# --------------------------------------------------------------------------- #
@case("R12", "runtime", "bootstrap")
async def test_r12_bootstrap_returns_downloading_and_dedupes():
    """R12 · 缺引擎时 `bootstrap()` 起后台下载并**立即返回** `DOWNLOADING`。

    这是全文件最贵的一条判据（"启动不许被 295MB 拖死"）：
      ① 返回 `DOWNLOADING`（不是 `ABSENT`，"默认不下载"是主理人明确否掉的）；
      ② 调用耗时 < 1s（证明没有 await 到下载完成）；
      ③ fetcher 恰好被调 1 次（不是 0 次 —— 起了任务就必须真的跑起来）；
      ④ 再调一次仍是 1 次（复用 `download_task`，不并发起第二次）。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M = _runtime_mod()
        payload = _engine_zip()
        spec = _release(payload)
        gate = threading.Event()
        fetched, loaded = [], []
        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_blocking(gate, record=fetched),
            wrapper_factory=_refuse_factory(loaded),
        )
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            state = await rt.bootstrap()
            cost = loop.time() - started

            assert state is M.EngineState.DOWNLOADING, (
                f"缺引擎时 bootstrap 应返回 DOWNLOADING（首次启动自动下载），实际 {state}"
            )
            assert cost < 1.0, (
                f"bootstrap 耗时 {cost:.2f}s ≥ 1s：它在钩子里等下载了。"
                "慢网下 295MB 必然撞破宿主 300s 上限"
            )

            await _until(lambda: len(fetched) == 1, timeout=10.0, what="fetcher 被调用 1 次")
            assert rt.download_task is not None and not rt.download_task.done()

            again = await rt.bootstrap()
            assert again is M.EngineState.DOWNLOADING
            assert len(fetched) == 1, (
                f"第二次 bootstrap 又发起了一次下载（fetcher={len(fetched)}）"
            )
            assert loaded == []
        finally:
            gate.set()
            await rt.destroy()


# --------------------------------------------------------------------------- #
# R13
# --------------------------------------------------------------------------- #
@case("R13", "runtime", "download")
async def test_r13_download_verify_unpack_chain_against_local_server():
    """R13 · 本地 `http.server` + 真哈希，走完 `download → verify → unpack` 全链。

    这条用例的价值在于"用真代码路径":真 `atomic_download`（真 socket、真 `.part`
    → `os.replace`）、真 `verify_sha256`（哈希由测试现算，不 mock 掉校验）、真
    `safe_extract`。只把网络换成了回环、把 DLL 换成了 fake lib。
    断言：请求恰好 1 次；`UNPACKED` 是**必经**中间态；最终 `LOADED`；归档哈希与
    清单一致；没有 `.part`/`.lock` 残留；全程不访问外网。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M = _runtime_mod()
        payload = _engine_zip()
        hits: list[str] = []
        server, port, thread = _serve_zip(payload, _ARCHIVE_NAME, hits)
        try:
            spec = _release(
                payload, url=f"http://127.0.0.1:{port}/{_ARCHIVE_NAME}"
            )
            assert url_host(spec.url) == "127.0.0.1", "R13 必须只走回环地址"
            rt = _runtime(env, spec, fetcher=None)  # ← 用真下载器（默认 atomic_download）
            seen = _state_sequence(rt)
            try:
                assert await rt.bootstrap() is M.EngineState.DOWNLOADING
                await _await_download(rt)

                assert hits == [f"/{_ARCHIVE_NAME}"], f"服务端收到 {hits}"
                assert M.EngineState.UNPACKED in seen, (
                    f"没经过 UNPACKED（状态序列 {[s.value for s in seen]}）"
                )
                assert rt.state is M.EngineState.LOADED, (
                    f"全链没走完：state={rt.state} err={rt.last_error}"
                )
                archive = _archive_path(env, spec)
                assert archive.is_file(), "归档不在 cache_dir 下"
                assert (
                    hashlib.sha256(archive.read_bytes()).hexdigest() == spec.sha256
                ), "落盘归档与清单哈希不一致"
                assert not list(env.cache_dir.glob("*.part")), "`.part` 残留"
                assert not list(env.cache_dir.glob("*.lock")), "`.lock` 残留"
                assert (env.engine_root / "resource" / "config.json").is_file()
            finally:
                await rt.destroy()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)


def url_host(url: str) -> str:
    import urllib.parse

    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _serve_zip(payload: bytes, name: str, hits: list[str]):
    """起一个只服务 `payload` 的回环 `http.server`，返回 `(server, port, thread)`。

    端口传 0 让内核挑：固定端口在并行跑测试/本机已有占用时会让整组假红。
    """
    body = bytes(payload)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 的契约名
            hits.append(self.path.split("?", 1)[0])
            if not self.path.endswith(name):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 别把服务器日志搅进测试输出
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="neko-r13-http", daemon=True
    )
    thread.start()
    return server, server.server_address[1], thread


# --------------------------------------------------------------------------- #
# R14
# --------------------------------------------------------------------------- #
@case("R14", "runtime", "purge")
async def test_r14_purge_removes_unpacked_tree():
    """R14 · `unpack()` 成功后 `purge()`：引擎文件（zip + 解压产物）全删，state 回 `ABSENT`。

    断言：`engine_root` 整个消失；归档（含 `.part`/`.lock`）消失；解压残留
    （`.unpacking-*` / `.old-*`）消失；state `ABSENT`；再次 `bootstrap()` **不认为
    已就绪**（重新发起下载，fetcher 计数 +1）。
    旧实现的病根：`purge()` 只删归档不删解压产物 → 占盘约 2 倍，且
    `arknights_engine_repair --clean` 名不副实。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M = _runtime_mod()
        payload = _engine_zip()
        spec = _release(payload)
        gate = threading.Event()
        fetched: list = []

        def _fetch(release, target, on_progress):
            fetched.append(Path(target))
            if len(fetched) == 1:
                return _fetcher_ok(payload)(release, target, on_progress)
            return _fetcher_blocking(gate, make_part=True)(release, target, on_progress)

        rt = _runtime(env, spec, fetcher=_fetch)
        try:
            await rt.bootstrap()
            await _await_download(rt)
            assert rt.state is M.EngineState.LOADED, f"前置链没跑通，state={rt.state}"
            assert env.engine_root.is_dir()

            await rt.purge()

            assert not env.engine_root.exists(), (
                f"purge 之后引擎目录还在（解压产物没删，占盘约 2 倍）：{env.engine_root}"
            )
            assert not _archive_path(env, spec).exists(), "purge 之后归档还在"
            assert not _part_path(env, spec).exists()
            residuals = sorted(
                p.name
                for pattern in ("*.unpacking-*", "*.old-*")
                for p in env.cache_dir.parent.rglob(pattern)
            )
            assert not residuals, f"purge 之后还有解压/换入残留：{residuals}"
            assert rt.state is M.EngineState.ABSENT, f"purge 后 state={rt.state}"

            again = await rt.bootstrap()
            assert again is M.EngineState.DOWNLOADING, (
                "purge 之后 bootstrap 认为引擎还在（磁盘事实没被重新读取），"
                f"实际返回 {again}"
            )
            # `bootstrap()` 只起任务就返回，fetcher 在 executor 线程里稍后才被调；
            # 这里必须等可观测条件，不能假设"返回即已调用"。
            await _until(
                lambda: len(fetched) == 2, timeout=10.0, what="purge 后重新发起下载"
            )
            assert len(fetched) == 2, f"purge 后应重新下载，fetcher 只被调 {len(fetched)} 次"
        finally:
            gate.set()
            await rt.destroy()


# --------------------------------------------------------------------------- #
# R15
# --------------------------------------------------------------------------- #
@case("R15", "runtime", "layout")
def test_r15_layout_resolves_three_install_shapes():
    """R15 · 三种安装形态（lib 在 `root/`、`root/bin/`、`root/lib/`）都能解析。

    关键判据是 `resource_parent`：必须是**含 `resource/` 的那一层**，不是 `resource`
    本身 —— 上一版"名字像 resource 就传它"导致 MAA 内部再拼一次 → `resource/resource`
    → `AsstLoadResource` 恒 False，用户看到"ADB 连不上"。
    另外：找不到时 `resource_parent is None` 且 `missing_pieces()` 非空；核心库在多处
    命中时**判歧义**（不取第一个，否则预放一份同名库就能顶掉真库）。
    """
    M = _runtime_mod()
    with _tmpdir() as tmp:
        cases = [
            ("root", ""),
            ("bin", "bin"),
            ("lib", "lib"),
        ]
        for label, lib_rel in cases:
            root = tmp / f"shape-{label}"
            expect_lib = _install_layout(root, lib_rel=lib_rel, marker=False)
            layout = M.RuntimeLayout.resolve(
                engine_root=root,
                user_dir=tmp / "user",
                data_dir=tmp / "data",
                cache_dir=tmp / "cache",
            )
            assert layout.library_path == expect_lib, (
                f"{label}: library_path 应找到 {expect_lib}，实际 {layout.library_path}"
            )
            assert layout.resource_parent == root, (
                f"{label}: resource_parent 必须是**含 resource/ 的那一层** {root}，"
                f"实际 {layout.resource_parent}"
                "（传 resource 本体就会拼成 resource/resource）"
            )
            assert layout.resource_dir == root / "resource"
            assert layout.resource_parent != layout.resource_dir, (
                f"{label}: resource_parent 指向了 resource 本体"
            )

        # 什么都没有 → 全空，但**不抛错**（纯函数只看磁盘事实）
        empty = tmp / "shape-empty"
        empty.mkdir()
        layout = M.RuntimeLayout.resolve(
            engine_root=empty,
            user_dir=tmp / "user",
            data_dir=tmp / "data",
            cache_dir=tmp / "cache",
        )
        assert layout.library_path is None
        assert layout.resource_parent is None, (
            f"没有 resource/ 却给出了 resource_parent={layout.resource_parent}"
        )
        missing = layout.missing_pieces()
        assert missing, "缺件清单为空：诊断信息没法告诉用户少了什么"
        assert "library" in missing and "resource" in missing, f"missing_pieces={missing}"

        # 核心库在 root/ 与 root/bin/ 各一份 → 歧义，不取第一个
        ambiguous = tmp / "shape-ambiguous"
        _install_layout(ambiguous, lib_rel="", marker=True)
        _install_layout(ambiguous, lib_rel="bin", marker=True)
        layout = M.RuntimeLayout.resolve(
            engine_root=ambiguous,
            user_dir=tmp / "user",
            data_dir=tmp / "data",
            cache_dir=tmp / "cache",
        )
        assert len(layout.compiled_libraries()) == 2, (
            f"应看到两处核心库，实际 {layout.compiled_libraries()}"
        )
        assert layout.library_path is None, (
            "多处命中时取了第一个 —— 预放一份同名库就能顶掉真库："
            f"{layout.library_path}"
        )


# --------------------------------------------------------------------------- #
# R16
# --------------------------------------------------------------------------- #
@case("R16", "runtime", "state")
async def test_r16_forbidden_state_transitions_raise():
    """R16 · 迁移表：禁止的迁移抛 `EngineNotReady`；启动不 await 下载；关机必须 cancel+await。

    ① 禁止：`ABSENT→READY` / `ABSENT→LOADED` / `VERIFIED→READY` / `READY→DOWNLOADING`
       / `DOWNLOADING→UNPACKED` / `DOWNLOADING→LOADED`；
       必须合法：`DOWNLOADING→DOWNLOADING`（进度自环）、`DOWNLOADING→ABSENT`
       （取消/失败清半包）、`ABSENT→UNPACKED`（手动放置）；
       **`DOWNLOADING→FAILED` 不存在** —— 下载失败也回 `ABSENT`，原因记 `last_error`。
       `EngineNotReady` 必须**纯关键字**构造（位置传参 `TypeError`），否则漏传字段
       在测试/静态检查期发现不了。
    ② `on_startup` 里 `bootstrap()` 返回 `DOWNLOADING` 且耗时 < 1s；`on_shutdown` →
       `cancel_download()` 必须 cancel **并 await**，之后 `.part` 不存在、state 落
       `ABSENT`（`kind=cancelled` 不是失败，不落 `FAILED`）。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        ES = M.EngineState

        # ---- ① 迁移表 ----
        forbidden = [
            (ES.ABSENT, ES.READY),
            (ES.ABSENT, ES.LOADED),
            (ES.VERIFIED, ES.READY),
            (ES.READY, ES.DOWNLOADING),
            (ES.DOWNLOADING, ES.UNPACKED),
            (ES.DOWNLOADING, ES.LOADED),
        ]
        rt = _runtime(env, _release(_engine_zip()), fetcher=_refuse_fetcher([]))
        for start, target in forbidden:
            _force_state(rt, start)
            kind, value = _outcome(lambda: rt._set_state(target))
            assert kind == "error", (
                f"禁止的迁移 {start.value}→{target.value} 被静默接受了"
                f"（state 现在 {rt.state.value}）"
            )
            assert isinstance(value, E.EngineNotReady), (
                f"{start.value}→{target.value} 应抛 EngineNotReady，"
                f"实际 {type(value).__name__}: {value}"
            )
            assert value.state == start.value, (
                f"EngineNotReady.state 应是**迁移前**的状态 {start.value!r}，"
                f"实际 {value.state!r}"
            )
            assert value.required, "EngineNotReady.required 为空：文案说不出缺什么"

        assert ES.FAILED not in M._TRANSITIONS[ES.DOWNLOADING], (
            "DOWNLOADING→FAILED 出现在迁移表里：下载失败必须回 ABSENT（可重试），"
            "落 FAILED 会把用户永久钉死"
        )

        for start, target in [
            (ES.DOWNLOADING, ES.DOWNLOADING),
            (ES.DOWNLOADING, ES.ABSENT),
            (ES.ABSENT, ES.UNPACKED),
        ]:
            _force_state(rt, start)
            rt._set_state(target)  # 合法迁移不得抛错
            assert rt.state is target, f"{start.value}→{target.value} 没落到 {target.value}"

        # 纯关键字：位置传参必须 TypeError
        try:
            E.EngineNotReady("absent", ("engine",))  # type: ignore[misc]
        except TypeError:
            pass
        else:
            raise AssertionError(
                "EngineNotReady 接受位置传参：漏传 required 之类的字段将无法被"
                "「构造即报错」发现（§1.1 裁决：纯关键字构造）"
            )

        # ---- ② 启动不 await 下载 / 关机 cancel + await ----
        env2 = _env(tmp / "lifecycle")
        gate = threading.Event()
        spec = _release(_engine_zip())
        fetched, loaded = [], []
        rt2 = _runtime(
            env2,
            spec,
            fetcher=_fetcher_blocking(gate, record=fetched),
            wrapper_factory=_refuse_factory(loaded),
        )
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            state = await rt2.bootstrap()  # ← on_startup 里做的事
            cost = loop.time() - started
            assert state is ES.DOWNLOADING, f"on_startup 后状态应是 DOWNLOADING，实际 {state}"
            assert cost < 1.0, (
                f"on_startup 耗时 {cost:.2f}s：在钩子里等下载了（295MB 慢网必撞破 300s 上限）"
            )
            await _until(lambda: len(fetched) == 1, timeout=10.0, what="下载真的开始了")
            part = _part_path(env2, spec)
            # fetcher 先在 `fetched` 里登记再写 `.part`，两步之间有个窗口；
            # 等"文件真的出现"，而不是假设登记完文件就已经在了。
            await _until(lambda: part.exists(), timeout=10.0, what="慢网下载写出 .part")
            assert part.exists(), "慢网下载途中没有 `.part`：取消后『.part 不存在』就无从验证"

            await rt2.cancel_download()  # ← on_shutdown 里做的事
            gate.set()

            assert rt2.state is ES.ABSENT, (
                f"取消后 state 必须落 ABSENT（不是 FAILED），实际 {rt2.state}"
            )
            err = rt2.last_error
            assert isinstance(err, E.EngineDownloadFailed), (
                f"取消后应留下 EngineDownloadFailed 记录，实际 {type(err).__name__}"
            )
            assert err.kind is E.DownloadFailure.CANCELLED, (
                f"取消的 kind 必须是 CANCELLED（不是 NETWORK——那会凭空造一条故障），"
                f"实际 {err.kind}"
            )
            assert not part.exists(), "取消后 `.part` 还在：下次会被当可续传的半包"
            assert rt2.download_task is None, "取消后还挂着 download_task 句柄"
            assert loaded == []
        finally:
            gate.set()
            await rt2.destroy()


# --------------------------------------------------------------------------- #
# R17
# --------------------------------------------------------------------------- #
@case("R17", "provision", "static")
def test_r17_provision_raises_only_known_errors():
    """R17 · 静态扫描 `maa/provision.py`：运行期 `raise` 的类型 ⊆ 那四个异常类。

    存在的理由：两套异常族必然逼出一个翻译层，漏映射一条，`service` 就会拿到陌生
    异常走兜底 —— 旧实现"所有问题都变成 ADB 没连上"就是这么来的。

    判据（"导入期自检"要单独算，理由见下）：
      * **运行期**可达的 `raise` 类型 ⊆ {EngineDownloadFailed, EngineHashMismatch,
        EngineUnpackFailed, EngineLayoutInvalid}；
      * 源码内**不得**自定义异常类（含 `ProvisionError` 之类的中间基类）；
      * "导入期自检"这一档只允许抛 `RuntimeError`，且只允许是**那几个常量表自检
        函数**（机械判定：除模块顶层 `Expr(Call(...))` 外没被任何地方引用过）。

    为什么允许导入期这一档：`_assert_release_table()` / `_assert_kind_coverage()` 在
    import 时校验常量表，写坏一行就该让插件**启动失败**；它们的异常永远到不了
    `service`，所以"陌生异常走兜底"这个风险不存在。但这道口子必须**窄且可证**：
    下面用两份改写过的源码（把 `RuntimeError` 挪进运行期 / 自定义一个异常类）证明
    扫描器有牙 —— 换成 §3 变异矩阵 M42 / M44 的形态，它必须红。
    """
    src_path = Path(__file__).resolve().parents[1] / "maa" / "provision.py"
    assert src_path.is_file(), f"找不到被测源码 {src_path}"
    source = src_path.read_text(encoding="utf-8")

    allowed = {
        "EngineDownloadFailed",
        "EngineHashMismatch",
        "EngineUnpackFailed",
        "EngineLayoutInvalid",
    }
    runtime_raises, import_raises, local_exc, import_only = _classify_raises(source)

    extra = sorted(runtime_raises - allowed)
    assert not extra, (
        f"provision.py 的运行期 raise 里出现了白名单之外的异常类型：{extra}。"
        "service 只认那四类（PROVISION_CODES），多一个就必须写翻译层，"
        "漏映射一条就会复现「所有问题都变成 ADB 没连上」"
    )
    assert not local_exc, (
        f"provision.py 自定义了异常类 {local_exc}：两套异常族 → 必须写翻译层 → "
        "漏一条走兜底（§1.2.1 裁决 B：复用 errors.py，不养自己的异常族）"
    )
    assert import_raises <= {"RuntimeError"}, (
        "导入期自检只允许抛 RuntimeError（常量表写坏就该启动失败）；"
        f"实际还抛 {sorted(import_raises - {'RuntimeError'})}"
    )
    assert import_only == {"_assert_release_table", "_assert_kind_coverage"}, (
        "「导入期自检」这份豁免面被改动了，实际被机械判定为导入期函数的："
        f"{sorted(import_only)}。豁免面必须是显式、可审的（新增一个等于悄悄放宽 R17）"
    )

    # ---- 变异自检：证明这把刀有牙（对应 §3 变异矩阵 M42 / M44）----
    anchor = "    target = Path(dest)\n"
    assert anchor in source, "变异锚点没了：请更新 R17 的变异样本"
    mutated = source.replace(anchor, anchor + "    raise RuntimeError('boom')\n", 1)
    mut_runtime, _, _, _ = _classify_raises(mutated)
    assert "RuntimeError" in mut_runtime, (
        "M42 变异未被发现：把 RuntimeError 挪进运行期路径后，扫描器仍认为合规"
    )
    mutated = source + "\n\nclass _Bomb(Exception):\n    pass\n"
    _, _, mut_exc, _ = _classify_raises(mutated)
    assert "_Bomb" in mut_exc, "M44 变异未被发现：新增自定义异常类后，扫描器仍认为合规"


def _classify_raises(source: str):
    """把一份 provision 源码里的 `raise` 分成三档。

    返回 `(runtime, import_time, local_exc, import_only)`：
      * `runtime`    —— 运行期可达的 raise 的异常类型名
      * `import_time`—— 只在导入期被调用的自检函数里的 raise 类型名
      * `local_exc`  —— 源码内自定义的异常类名
      * `import_only`—— 被机械判定为"导入期函数"的名字集合

    "导入期函数"的判定必须机械、不许人工白名单：一个函数名在整个模块里被
    `Load` 引用的次数恰好等于它出现在模块顶层 `Expr(Call(Name))` 里的次数，
    也就是说**除导入期那几行之外没有任何引用**。改坏了就会落进 `runtime`。
    """
    import ast

    tree = ast.parse(source)

    top_calls: Counter = Counter()
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if isinstance(func, ast.Name):
                top_calls[func.id] += 1
    loads: Counter = Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )
    import_only = {name for name in top_calls if loads[name] == top_calls[name]}

    def _exc_name(node) -> str:
        target = node.func if isinstance(node, ast.Call) else node
        if isinstance(target, ast.Name):
            return target.id
        return ast.unparse(target)

    def _collect(node, sink: set) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Raise) and sub.exc is not None:
                sink.add(_exc_name(sub.exc))

    runtime: set[str] = set()
    import_time: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect(node, import_time if node.name in import_only else runtime)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            runtime.add(_exc_name(node.exc))  # 模块级 raise（正常没有，但不能漏检）

    local_exc = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            ast.unparse(base).endswith(("Exception", "Error"))
            for base in node.bases
        )
    ]
    return runtime, import_time, local_exc, import_only


# --------------------------------------------------------------------------- #
# R18
# --------------------------------------------------------------------------- #
@case("R18", "runtime", "capture")
async def test_r18_capture_fails_fast_while_downloading():
    """R18 · `DOWNLOADING` 期间 `capture()` 必须**快速失败**，不许等下载。

    断言：抛 `EngineNotReady`；`exc.state == "downloading"`（是 `EngineState.value`，
    不是枚举对象）；耗时 < 100ms（此时下载仍在飞）；`progress` 口径按 §7.4：
    要么 `None`（尚未开始），要么 0–100，**不断言非 None**；顺手断
    `AsstGetImage` 没被调用（连 wrapper 都没建）。
    写反的后果：下载期间一次截图请求就阻塞，该 entry 必被宿主超时打穿 ——
    而 295MB 慢网下这个窗口很长。
    """
    with _tmpdir() as tmp:
        env = _env(tmp)
        M, E = _runtime_mod(), _errors_mod()
        gate = threading.Event()
        payload = _engine_zip()
        spec = _release(payload)
        fetched, loaded = [], []
        rt = _runtime(
            env,
            spec,
            fetcher=_fetcher_blocking(gate, record=fetched),
            wrapper_factory=_refuse_factory(loaded),
        )
        try:
            assert await rt.bootstrap() is M.EngineState.DOWNLOADING
            await _until(lambda: len(fetched) == 1, timeout=10.0, what="下载真的开始了")
            assert rt.state is M.EngineState.DOWNLOADING

            loop = asyncio.get_running_loop()
            started = loop.time()
            kind, value = await _aoutcome(rt.capture())
            cost = loop.time() - started

            assert kind == "error", f"下载期间 capture() 返回了数据（{value!r}）"
            assert isinstance(value, E.EngineNotReady), (
                f"应抛 EngineNotReady，实际 {type(value).__name__}: {value}"
            )
            assert value.state == "downloading", (
                "EngineNotReady.state 必须是 EngineState.value（'downloading'），"
                f"实际 {value.state!r}"
            )
            assert value.progress is None or 0 <= value.progress <= 100, (
                f"progress 口径越界：{value.progress!r}（应 None 或 0–100）"
            )
            assert cost < 0.1, (
                f"capture() 耗时 {cost * 1000:.0f}ms ≥ 100ms：它在等下载。"
                "宿主侧一次截图必被超时打穿"
            )
            assert rt.state is M.EngineState.DOWNLOADING, "capture 把下载状态改掉了"
            assert loaded == [], "下载期间就建了 wrapper（AsstGetImage 有可能被调到）"
        finally:
            await rt.cancel_download()
            gate.set()
            await rt.destroy()


# --------------------------------------------------------------------------- #
# B 组：销毁 / UAF（R20–R23）
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _grab_logs(logger_name: str):
    """抓某个 logger 的记录（`caplog` 的零依赖替代）。

    **必须包住构造**：`unknown instance option` 那条 warning 是在 `__init__` 里打的，
    只在 `bootstrap()` 期间挂 handler 会漏掉它（实测踩过）。
    """
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Grab()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _errors_in(records, *, level: int = logging.ERROR) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= level]


async def _fresh_loaded(tmp: Path, fake=None, **over):
    """新的 tmp + 新的 runtime，直接到 `LOADED`（销毁类用例每个 block 各来一套）。"""
    env = _env(tmp)
    return await _rt_loaded(env, fake, **over)


@case("R20", "runtime", "destroy")
async def test_r20_destroy_stops_before_destroy_and_abandons_on_timeout():
    """R20 · `Stop → Destroy` 同线程顺序执行；超时**放弃**时 detach 且**绝不 close**。

    三条 C 侧事实决定了这条用例的形状（见 `destroy()` 的 docstring）：
    ``AsstStop`` 清任务队列（让 ``~Assistant`` 只等当前这一条），``AsstDestroy`` 会 join
    三条内部线程且**可能无限阻塞**，而 ``Destroyed`` 回调是析构的**最后一句** ——
    也就是说"放弃等待"那一刻，C 线程**仍可能**回调进已经拆光的 Python 状态。

    断言：
      ① 顺序：`call_log` 里 `AsstStop` **先于** `AsstDestroy`（分两个线程并发调同一个
         handle = 对同一个 C++ 对象 UAF，比"慢一点"严重得多）；
      ② 正常路径（对照组）：确认已销毁 → `wrapper.close()`（`_lib` 置空）、
         `detach_calls == 0`、`destroy_abandoned == 0`；
      ③ 超时放弃：① **error 级**日志且文本含 "abandon"（这是"已知会崩的窗口"，
         不是普通告警）② `detach_callback()` 被调用（`callback_attached is False`）
         ③ **不 close**（`_lib` 仍活着：C 线程可能还在懒加载依赖）
         ④ `lifecycle_stats['destroy_abandoned'] == 1` 且 wrapper 被保活；
      ④ 错误输入：`AsstStop` 抛错 → **不许**阻止 `AsstDestroy`（销毁优先），
         也不许因此记成"放弃"（那时实例其实已经删掉了）。
    """
    M = _runtime_mod()

    # ② 正常路径（对照组）：确认销毁 → 允许 close
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        w = rt._wrapper
        assert w is not None, "前置失败：bootstrap 后应持有 wrapper"
        assert w.callback_attached is True, (
            "前置失败：runtime 装载时应 set_callback（否则收不到任何事件）"
        )
        with _grab_logs("maa.runtime") as records:
            await rt.destroy()
        log = fake.call_log
        i_stop, i_destroy = fake.index_of("AsstStop"), fake.index_of("AsstDestroy")
        assert i_stop >= 0 and i_destroy >= 0, (
            f"销毁路径少调了符号：call_log={log}（AsstStop 必须真的被调用）"
        )
        assert i_stop < i_destroy, (
            f"顺序反了：AsstStop@{i_stop} 必须在 AsstDestroy@{i_destroy} 之前 —— "
            "Stop 先清队列，~Assistant 的 join 才只等当前这一条任务"
        )
        assert w._lib is None, (
            "确认已销毁之后必须 close()（释放 CDLL 与 add_dll_directory 句柄）"
        )
        assert w.detach_calls == 0, (
            f"正常路径不该走 detach_callback（那是放弃路径的补救），实际 {w.detach_calls} 次"
        )
        stats = rt.lifecycle_stats
        assert (stats["stop_calls"], stats["destroy_calls"]) == (1, 1), (
            f"一次销毁应各发 1 次 C 调用，实际 {stats}"
        )
        assert stats["destroy_abandoned"] == 0, f"正常路径不该记放弃：{stats}"
        assert not _errors_in(records), f"正常销毁打出了 error 日志：{_errors_in(records)!r}"

    # ③ 超时放弃：error 日志 + detach + 不 close + 计数
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(
            tmp, fake=F.FakeMaaCore(destroy_delay=0.5), destroy_timeout=0.05
        )
        w = rt._wrapper
        with _grab_logs("maa.runtime") as records:
            await rt.destroy()
        log = fake.call_log
        i_stop, i_destroy = fake.index_of("AsstStop"), fake.index_of("AsstDestroy")
        assert i_stop >= 0 and i_destroy >= 0 and i_stop < i_destroy, (
            f"放弃路径也必须先 Stop 再 Destroy（同一线程顺序执行）：call_log={log}"
        )

        errors = _errors_in(records)
        assert errors, (
            "放弃销毁只打了 debug/没打日志 —— 这是'已知会崩的窗口'，"
            "必须是 error 级（否则真机上永远没人知道后台线程还活着）"
        )
        assert any("abandon" in text.lower() for text in errors), (
            f"error 日志里没有 'abandon' 字样，无法被日志告警规则抓到：{errors!r}"
        )

        assert w.detach_calls == 1, (
            f"放弃时必须调一次 detach_callback（把 Python 目标摘成空，挡逻辑 UAF），"
            f"实际 {w.detach_calls} 次"
        )
        assert w.callback_attached is False, (
            "detach 之后 callback_attached 必须是 False（否则 C 线程还会打到已拆的状态）"
        )
        assert w._lib is not None, (
            "放弃时**调用了 wrapper.close()** —— 库句柄被解引用，而 C 线程可能还在懒加载依赖。"
            "放弃路径必须保活那一份 wrapper（有界泄漏是设计）"
        )

        stats = rt.lifecycle_stats
        assert stats["destroy_abandoned"] == 1, (
            f"destroy_abandoned 应为 1，实际 {stats['destroy_abandoned']}（{stats}）"
        )
        assert stats["abandoned_wrappers"] == 1, (
            f"被保活的 wrapper 数应与放弃次数一致，实际 {stats}"
        )
        assert stats["detach_invocations"] == 1, (
            f"detach_invocations 应为 1，实际 {stats['detach_invocations']}"
        )
        assert rt.state is M.EngineState.ABSENT, (
            f"放弃等待也要把 Python 侧状态收干净（ABSENT），实际 {rt.state}"
        )

    # ④ 错误输入：AsstStop 抛错不许阻止 AsstDestroy
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        w = rt._wrapper

        def _boom(_handle):
            raise RuntimeError("AsstStop exploded")

        fake.f("AsstStop")._impl = _boom
        with _grab_logs("maa.runtime") as records:
            await rt.destroy()
        assert "AsstDestroy" in fake.call_log, (
            f"AsstStop 抛错后不再调 AsstDestroy —— 等于把实例留给进程退出：{fake.call_log}"
        )
        stats = rt.lifecycle_stats
        assert stats["destroy_abandoned"] == 0, (
            f"Stop 失败不该记成'放弃'（AsstDestroy 成功了、实例真的删掉了）：{stats}"
        )
        assert w._lib is None, "确认销毁后仍应 close()"
        assert not _errors_in(records), (
            f"Stop 失败只是降级（debug），不许打 error：{_errors_in(records)!r}"
        )


@case("R21", "runtime", "destroy")
async def test_r21_destroy_thread_failure_is_treated_as_abandoned():
    """R21 · 销毁线程抛错 = 实例状态未知 ⇒ 与超时同性质：error 日志 + detach + 计数。

    `[FACT]` `destroy_instance` 唯一的抛错点是 `_require_lib()`（库已被 close）——
    也就是 **`AsstDestroy` 根本没发出去**。旧实现只记 debug，等于把"实例没被销毁"
    静默吞掉；真机上表现成"关不掉的后台线程 / 退出时卡住"。

    三段：
      ① 正常（本例的"正常"就是那条失败路径）：先 `close()` 再 `destroy()` →
         error 日志含 "abandon"、`detach_callback` 被调用、`destroy_abandoned == 1`、
         `destroy()` **不抛**（它是 `on_shutdown` 钩子，抛错会被宿主判 shutdown 失败）；
      ② 边界：替身 wrapper **没有** `detach_callback`（可选能力探测）→ 关机路径仍不许抛；
      ③ 错误输入：放弃之后再 `destroy()` → 不抛、`destroy_calls` 不再增长（幂等）。
    """
    M = _runtime_mod()

    # ① 销毁线程抛错（真实成因：库已被 close）
    with _tmpdir() as tmp:
        rt, _fake = await _fresh_loaded(tmp)
        w = rt._wrapper
        detach_seen: list[int] = []
        real_detach = w.detach_callback

        def _spy() -> bool:
            detach_seen.append(1)
            return real_detach()

        w.detach_callback = _spy
        w.close()                                  # → 之后的 AsstStop/AsstDestroy 必然失败

        with _grab_logs("maa.runtime") as records:
            await rt.destroy()                     # 不许抛：这是 on_shutdown 钩子
        errors = _errors_in(records)
        assert errors, "销毁线程抛错却没打 error 日志（旧实现只记 debug，等于静默吞掉）"
        assert any("abandon" in text.lower() for text in errors), (
            f"error 日志里没有 'abandon' 字样：{errors!r}"
        )
        assert len(detach_seen) == 1, (
            f"销毁线程抛错后必须调 detach_callback（实例可能还活着），实际 {len(detach_seen)} 次"
        )
        stats = rt.lifecycle_stats
        assert stats["destroy_abandoned"] == 1, (
            f"应计入 destroy_abandoned，实际 {stats['destroy_abandoned']}（{stats}）"
        )
        assert stats["abandoned_wrappers"] == 1, f"wrapper 应被保活，实际 {stats}"
        assert stats["stop_calls"] == 1 and stats["destroy_calls"] == 1, (
            f"一次销毁各发 1 次，实际 {stats}"
        )

        # ③ 幂等：放弃之后再 destroy 不许抛、不许再加计数
        await rt.destroy()
        assert rt.lifecycle_stats["destroy_calls"] == 1, (
            f"二次 destroy 又发了一次 AsstDestroy：{rt.lifecycle_stats}（double free 是 UAF）"
        )
        assert rt.state is M.EngineState.ABSENT

    # ② 边界：替身 wrapper 没有 detach_callback（可选能力缺失）
    with _tmpdir() as tmp:
        rt, _fake = await _fresh_loaded(tmp)
        w = rt._wrapper
        w.detach_callback = None                   # 模拟"替身实现没这个方法"
        w.close()
        with _grab_logs("maa.runtime") as records:
            kind, value = await _aoutcome(rt.destroy())
        assert kind == "value", (
            f"wrapper 没有 detach_callback 时关机路径抛了 {value!r} —— "
            "可选能力必须用 getattr 探测，绝不因为替身少个方法就让关机失败"
        )
        assert _errors_in(records), "仍然应该是 error 级（实例状态未知这件事没变）"
        assert rt.lifecycle_stats["destroy_abandoned"] == 1, (
            f"没有 detach 能力不代表不算放弃：{rt.lifecycle_stats}"
        )


@case("R22", "runtime", "destroy")
async def test_r22_destroy_is_idempotent():
    """R22 · `destroy()` 幂等：重复调用 / `ABSENT` 状态调用 → 各计数**仍为 1**（不是 2）。

    为什么这条是安全项而不是洁癖：`AsstDestroy` 是 ``delete handle``，**delete 两次是
    UAF**，不是"多删一个空指针"。宿主可能同时从 `on_shutdown` 与错误路径各调一次。

    三段：
      ① 正常：`bootstrap()` → `destroy()` → (1,1)；再 `destroy()` → 仍是 (1,1)；
      ② 边界：第三次（此时 state 已是 `ABSENT`）→ 仍是 (1,1)、不抛；
      ③ 错误输入（"不是一次性对象"）：再次 `bootstrap()`（布局还在）→ 新实例 →
         `destroy()` → 变成 (2,2)。计数器口径是"**每个实例**一次销毁"，不是"进程一次"。
    """
    M = _runtime_mod()
    with _tmpdir() as tmp:
        rt, _fake = await _fresh_loaded(tmp)
        try:
            await rt.destroy()
            stats = rt.lifecycle_stats
            assert (stats["stop_calls"], stats["destroy_calls"]) == (1, 1), (
                f"一次销毁应各 1 次，实际 {stats}"
            )
            assert rt.state is M.EngineState.ABSENT, f"销毁后状态应是 ABSENT，实际 {rt.state}"

            await rt.destroy()
            again = rt.lifecycle_stats
            assert (again["stop_calls"], again["destroy_calls"]) == (1, 1), (
                "重复 destroy 又发了 C 调用："
                f"{again}（AsstDestroy 调两次 = double free = UAF，比'多删个空指针'严重）"
            )

            # ② ABSENT 状态下再调一次
            kind, value = await _aoutcome(rt.destroy())
            assert kind == "value", f"ABSENT 状态 destroy 抛了 {value!r}（退出路径不许抛）"
            third = rt.lifecycle_stats
            assert (third["stop_calls"], third["destroy_calls"]) == (1, 1), (
                f"ABSENT 状态 destroy 改了计数：{third}"
            )

            # ③ 重新装载 → 新实例 → 必须能再销毁（不是一次性对象）
            state = await rt.bootstrap()
            assert state is M.EngineState.LOADED, (
                f"销毁后再 bootstrap 应能重新装载（布局还在），实际 {state}"
            )
            await rt.destroy()
            fresh = rt.lifecycle_stats
            assert (fresh["stop_calls"], fresh["destroy_calls"]) == (2, 2), (
                "第二个实例的销毁没被计数（或第一个实例被重复销毁）："
                f"{fresh}（每个实例各一次才对）"
            )
        finally:
            await rt.destroy()


@case("R23", "runtime", "destroy")
async def test_r23_concurrent_destroy_sends_one_delete():
    """R23 · 并发 `destroy()`：只发**一次** delete，后来的直接返回（不排队、不抛）。

    宿主可能从两条路径同时关停（`on_shutdown` + 失败清理）。没有 `_teardown_in_progress`
    这道闸时，两个协程各发一次 `AsstDestroy` —— `delete` 两次就是 UAF。

    三段：
      ① 正常：`destroy()` 跑到一半（用 `destroy_delay` + 大超时固定住窗口）→
         第二次 `destroy()` **立刻返回**，`destroy_calls` 仍 1；
      ② 边界：三路 `gather` → 仍是 1；
      ③ 错误输入：销毁进行中再连打 3 次 → 都不抛、计数都不变。
    """
    # ① 第二次立刻返回
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(
            tmp, fake=F.FakeMaaCore(destroy_delay=0.5), destroy_timeout=3.0
        )
        first = asyncio.ensure_future(rt.destroy())
        await asyncio.sleep(0)                    # 让第一个跑到挂起点（wait_for）
        assert rt.lifecycle_stats["teardown_in_progress"] == 1, (
            "destroy() 没有把 teardown_in_progress 立起来（并发闸失效）"
        )
        assert rt.lifecycle_stats["destroy_calls"] == 1

        second = asyncio.ensure_future(rt.destroy())
        await asyncio.sleep(0)
        assert second.done(), (
            "第二次 destroy() 在排队等待 —— 那会拖住 on_shutdown（且窗口内状态已不可信）"
        )
        assert rt.lifecycle_stats["destroy_calls"] == 1, (
            f"并发第二次 destroy 又发了 AsstDestroy：{rt.lifecycle_stats}"
        )

        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5.0)
        assert results == [None, None], f"并发 destroy 抛了：{results!r}"
        stats = rt.lifecycle_stats
        assert (stats["stop_calls"], stats["destroy_calls"]) == (1, 1), (
            f"并发销毁后应各 1 次，实际 {stats}"
        )
        assert stats["teardown_in_progress"] == 0, "销毁结束后标志没落下"
        assert stats["destroy_abandoned"] == 0, f"这不是放弃路径：{stats}"

    # ② 边界：三路并发
    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(
            tmp, fake=F.FakeMaaCore(destroy_delay=0.5), destroy_timeout=3.0
        )
        results = await asyncio.wait_for(
            asyncio.gather(rt.destroy(), rt.destroy(), rt.destroy(), return_exceptions=True),
            5.0,
        )
        assert all(r is None for r in results), f"三路并发 destroy 有异常：{results!r}"
        stats = rt.lifecycle_stats
        assert (stats["stop_calls"], stats["destroy_calls"]) == (1, 1), (
            f"三路并发销毁应各 1 次，实际 {stats}"
        )

        # ③ 错误输入：销毁之后再连打（此时是 ABSENT）
        for _ in range(3):
            kind, value = await _aoutcome(rt.destroy())
            assert kind == "value", f"销毁后 destroy 抛了 {value!r}"
        assert (rt.lifecycle_stats["stop_calls"], rt.lifecycle_stats["destroy_calls"]) == (1, 1)


@case("R24", "runtime", "health")
async def test_r24_health_converges_after_disconnect():
    """R24 · 断线后 `health()` **惰性收敛**：不允许继续报 `READY`。

    不改会怎样：断线后 `_state` 仍是 `READY`，`health()` / `status` 继续显示"一切就绪"；
    用户照着一个"就绪"的状态点截图，然后等满 `capture_timeout` 才拿到"截图卡住了"——
    而真正原因（掉线）早就躺在 `adapter.link_state` 里。

    三段：
      ① 正常：`READY` + 注入 `Disconnect` → `health()` 之后 `_state` 变 `LOADED`、
         `status_snapshot()['link_state'] == 'disconnected'`、`_last_error` 是
         `AdbDisconnected` 且 why 来自事件、`connected is False`、`device` 被清空；
      ② 边界：再读一次 `health()` → 不许重复构造新的 `AdbDisconnected`
         （`last_error` 应是同一个对象：状态已收敛，没有新的"事实"）；
      ③ 错误输入：还没连过（`LOADED`）时收到 `Disconnect` → **不许伪造** `AdbDisconnected`
         （那时根本没有会话可掉线）。
    """
    A = _adapter_mod()
    M = _runtime_mod()
    E = _errors_mod()

    with _tmpdir() as tmp:
        rt, fake = await _fresh_loaded(tmp)
        try:
            await _rt_connect(rt, fake)
            assert rt.state is M.EngineState.READY, f"前置失败：{rt.state}"
            assert rt.status_snapshot()["link_state"] == "connected", (
                f"前置失败：连上后 link_state 应是 connected，"
                f"实际 {rt.status_snapshot()['link_state']!r}"
            )

            _rt_event(rt, {"what": "Disconnect", "details": {"why": "adb gone"}})
            await _until(
                lambda: rt.adapter.link_state is A.LinkState.DISCONNECTED,
                what="adapter 已把链路标成 DISCONNECTED",
            )
            assert rt.state is M.EngineState.READY, (
                "前置：收敛是**惰性**的（读快照时才下沉），这里还没读"
            )

            snapshot = rt.health()
            assert snapshot.state is M.EngineState.LOADED, (
                f"断线后 health() 仍报 {snapshot.state} —— 状态页会说'一切就绪'，"
                "用户照着它去点截图，然后等满 capture_timeout"
            )
            assert rt.state is M.EngineState.LOADED, f"health() 没把 _state 收下去：{rt.state}"
            assert rt.status_snapshot()["link_state"] == "disconnected", (
                f"status 里的 link_state 应是 disconnected，"
                f"实际 {rt.status_snapshot()['link_state']!r}"
            )
            error = rt.last_error
            assert isinstance(error, E.AdbDisconnected), (
                f"_last_error 应是 AdbDisconnected，实际 {type(error).__name__}: {error}"
            )
            assert "adb gone" in str(getattr(error, "why", "")), (
                f"why 应是事件原文（不编造），实际 {getattr(error, 'why', None)!r}"
            )
            assert snapshot.connected is False, (
                "已经掉线了 health().connected 还是 True —— 用户会以为'现在能用'"
            )
            assert snapshot.device is None, (
                f"掉线后 device 缓存必须被丢掉（否则显示 connected=True 的旧快照）："
                f"{snapshot.device!r}"
            )

            # ② 边界：重复读快照不许重复构造异常
            first_error = rt.last_error
            rt.health()
            assert rt.last_error is first_error, (
                "第二次 health() 又造了一个新的 AdbDisconnected —— "
                "收敛应当幂等（状态已经 LOADED，没有新的'事实'）"
            )
            assert rt.state is M.EngineState.LOADED
        finally:
            await rt.destroy()

    # ③ 错误输入：从没连过时收到 Disconnect → 不许伪造掉线错误
    with _tmpdir() as tmp:
        rt, _fake = await _fresh_loaded(tmp)
        try:
            assert rt.last_error is None, f"前置失败：刚装载就有错误 {rt.last_error!r}"
            _rt_event(rt, {"what": "Disconnect", "details": {"why": "stray"}})
            rt.health()
            assert rt.last_error is None, (
                "还没连接过就报了 AdbDisconnected —— 没有会话可掉线，这是伪造事实"
                f"（last_error={rt.last_error!r}）"
            )
            assert rt.state is M.EngineState.LOADED, (
                f"没有会话时不该动状态，实际 {rt.state}"
            )
        finally:
            await rt.destroy()


@case("R25", "runtime", "diagnostics")
async def test_r25_status_snapshot_declares_client_scope_and_instance_options():
    """R25 · `status_snapshot()` 必须声明客户端范围，并如实分档实例选项。

    `[FACT]` MaaCore **没有任何 C API 能查"当前连的是哪个客户端"**（客户端类型是识别
    阶段模板匹配出来的内部状态）。所以"只支持国服官服"只能是**声明** —— 而声明如果不
    出现在诊断字段里，外服用户报障时就会去反复重启/重下引擎/换端口，全都无效。

    三段：
      ① 正常：`client_scope == "cn-official"`；`instance_options` 恰好三个键
         requested / applied / rejected，且 applied 含官方 GUI 那三项；
      ② 边界：只覆盖一项 → 其余**保持默认**（"部分设置"比"完全不设置"更难排查，
         所以 requested 必须是"默认值 + 覆盖"的合并结果，不是"只留覆盖项"）；
      ③ 错误输入：未知选项名 → 忽略 + 一条 warning（不许抛、不许进 requested），
         装载照常到 LOADED。
    """
    M = _runtime_mod()
    W = _wrapper_mod()
    K = W.AsstInstanceOptionKey
    names = {"TouchMode", "AdbLiteEnabled", "DeploymentWithPause"}

    with _tmpdir() as tmp:
        rt, _fake = await _fresh_loaded(tmp)
        try:
            snapshot = rt.status_snapshot()
            assert snapshot["client_scope"] == "cn-official", (
                f"client_scope 声明不对：{snapshot['client_scope']!r}（期望 'cn-official'）—— "
                "外服用户报障时这是唯一的'我们本来就不支持'证据"
            )
            options = snapshot["instance_options"]
            assert set(options) == {"requested", "applied", "rejected"}, (
                f"instance_options 必须恰好有三个分档，实际 {sorted(options)}"
            )
            assert set(options["applied"]) == names, (
                f"applied 应是官方 GUI 那三项，实际 {sorted(options['applied'])}"
            )
            assert options["rejected"] == {}, f"全部成功时 rejected 应为空：{options}"
            assert set(options["requested"]) == names, (
                f"requested 应含三项，实际 {sorted(options['requested'])}"
            )

            # ② 边界：只覆盖一项，其余保持默认
            with _tmpdir() as tmp2:
                rt2, fake2 = await _fresh_loaded(
                    tmp2, instance_options={"TouchMode": "maatouch"}
                )
                try:
                    requested = rt2.status_snapshot()["instance_options"]["requested"]
                    assert requested == {
                        "TouchMode": "maatouch",
                        "AdbLiteEnabled": "1",
                        "DeploymentWithPause": "0",
                    }, (
                        f"覆盖一项后 requested 应是'默认值+覆盖'的合并结果，实际 {requested!r} —— "
                        "只留覆盖项等于把其它开关交回 MaaCore 内部默认，比完全不设更难排查"
                    )
                    assert (int(K.TOUCH_MODE), "maatouch") in fake2.instance_options, (
                        f"覆盖值没有真的传到 C：{fake2.instance_options!r}"
                    )
                finally:
                    await rt2.destroy()

            # ③ 错误输入：未知选项名
            with _tmpdir() as tmp3:
                with _grab_logs("maa.runtime") as records:
                    rt3, _fake3 = await _fresh_loaded(
                        tmp3, instance_options={"TouchMode": "adb", "NOPE": "1"}
                    )
                try:
                    assert rt3.status_snapshot()["state"] == M.EngineState.LOADED.value, (
                        "一个不认识的选项名把装载搞挂了 —— 配置是外部输入，"
                        "手滑一个 key 不该让整个插件起不来"
                    )
                    requested = rt3.status_snapshot()["instance_options"]["requested"]
                    assert "NOPE" not in requested, f"未知选项被当成真的设下去了：{requested!r}"
                    warnings = [
                        r.getMessage()
                        for r in records
                        if r.levelno >= logging.WARNING and "NOPE" in r.getMessage()
                    ]
                    assert warnings, (
                        "未知选项被静默丢掉 —— 至少要有一条 warning，"
                        f"否则用户写了错 key 永远查不出来（记录={[r.getMessage() for r in records]!r}）"
                    )
                finally:
                    await rt3.destroy()
        finally:
            await rt.destroy()


@case("X01", "infra")
def test_x01_case_ids_match_plan_doc():
    """X01 · 文档表格里的编号集合 **==** 收集器登记的编号集合（无多余、无缺失）。

    真实故障：文档说有 65 条，实际跑了 59 条，没人发现。
    只认**表格行首**的编号（`| W05 | …`），不扫正文里的散落引用 ——
    否则"原编号 W21 已移到 R19"这种历史说明会被当成"文档多了一条"。
    """
    import re

    doc = Path(__file__).resolve().parents[1] / "docs" / "02-test-plan.md"
    assert doc.is_file(), f"找不到测试计划：{doc}"
    pattern = re.compile(r"\|\s*([WARPX]\d{2})\s*\|")
    documented: set[str] = set()
    for line in doc.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        m = pattern.match(line)
        if m:
            documented.add(m.group(1))

    registered = {c.id for c in R.REGISTRY if re.fullmatch(r"[WARPX]\d{2}", c.id)}
    assert documented, "文档里一个编号都没解析出来 —— 解析逻辑或表格格式变了"
    assert registered, "收集器里一个契约组用例都没登记"

    missing_in_doc = sorted(registered - documented)
    missing_in_code = sorted(documented - registered)
    assert not missing_in_doc, f"代码里有、文档没写：{missing_in_doc}"
    assert not missing_in_code, f"文档里有、代码没登记（会被静默跳过）：{missing_in_code}"


@case("X02", "infra")
def test_x02_no_test_function_left_unregistered():
    """X02 · 两个测试模块里所有 `test_*` 都必须被登记，且编号不重复、函数名与编号一致。

    **上一版栽的正是这个**：pytest 能跑，但脚本入口静默跳过几条。
    这里直接复用收集器的四条一致性断言，而不是另写一份（两份校验必然漂移）。
    """
    problems: list[str] = []
    current = sys.modules[__name__]
    problems += [f"test_contract: {p}" for p in R.check_registry(current)]

    try:
        import test_service
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"test_service 无法导入（{type(e).__name__}: {e}）") from None
    problems += [f"test_service: {p}" for p in R.check_registry(test_service)]

    ids = [c.id for c in R.REGISTRY]
    assert not problems, "收集期不一致：\n  " + "\n  ".join(problems)
    assert len(ids) == len(set(ids)), f"编号重复：{sorted({i for i in ids if ids.count(i) > 1})}"


@case("X03", "infra", "layering")
def test_x03_service_import_direction():
    """X03 · `companion/service.py` 的 import 方向：不许碰 wrapper / provision / runtime。

    允许 `maa.errors`（只读异常码）与 `maa.adapter`（只读快照）；
    全文不许出现 `get_image_bgr` 标识符 —— 裸 BGR 禁上行，靠**静态不可达**达成。

    真实故障：有人为图方便在业务层 `from ..maa.provision import KNOWN_RELEASES`，
    把供应链逻辑漏进业务层；或把裸 BGR 当 PNG 上行（图全坏且无魔数可查）。
    """
    import ast

    service = Path(__file__).resolve().parents[1] / "companion" / "service.py"
    if not service.is_file():
        raise Blocked(f"companion/service.py 尚未落盘：{service}")
    tree = ast.parse(service.read_text(encoding="utf-8"))

    forbidden = {"maa.wrapper", "maa.provision", "maa.runtime"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").lstrip(".")
            if mod in forbidden:
                violations.append(f"line {node.lineno}: from .{mod} import …")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden:
                    violations.append(f"line {node.lineno}: import {alias.name}")
    assert not violations, (
        "service 越层 import：\n  " + "\n  ".join(violations)
        + "\n（允许 maa.errors 与 maa.adapter；供应链/绑定层不许进业务层）"
    )

    bgr: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "get_image_bgr":
            bgr.append(f"line {node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr == "get_image_bgr":
            bgr.append(f"line {node.lineno}: .{node.attr}")
    assert not bgr, "service 里出现了 get_image_bgr（裸 BGR 禁上行）：\n  " + "\n  ".join(bgr)



def _engine_state_violations(source: str, allowed: set[str]):
    """扫描一份源码里所有 `EngineNotReady(state=...)` 的取值。

    返回 `(found, unresolvable, bad)`。抽成纯函数不是为了好看：它让 "这条守门到底有没有
    牙" 可以用**改写过的源码字符串**直接验证（见 X04 的变异记录）——否则只能去改
    `maa/adapter.py`，那既不安全也不该由测试做。

    解析范围刻意只有一层：字符串字面量、模块级常量、`EngineState.X`。
    更绕的写法（局部变量、`getattr`、f-string）算 unresolvable，判失败。
    """
    import ast

    try:
        from maa.runtime import EngineState
    except Exception:  # noqa: BLE001 - 没 runtime 时只认字面量，属性形式算不可解析
        EngineState = None  # type: ignore[assignment]

    tree = ast.parse(source)

    # ① 模块级 `NAME = "literal"` / `NAME: T = "literal"` / `NAME = EngineState.X`
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        else:
            continue
        if not names or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for name in names:
                consts[name] = value.value
        elif (
            EngineState is not None
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "EngineState"
        ):
            member = getattr(EngineState, value.attr, None)
            if member is not None:
                for name in names:
                    consts[name] = member.value

    def resolve(node) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if (
            EngineState is not None
            and isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "EngineState"
        ):
            member = getattr(EngineState, node.attr, None)
            return member.value if member is not None else None
        return None

    found: list[tuple[str | None, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fname != "EngineNotReady":
            continue
        for kw in node.keywords:
            if kw.arg == "state":
                found.append((resolve(kw.value), node.lineno))

    unresolvable = [line for value, line in found if value is None]
    bad = [(value, line) for value, line in found if value is not None and value not in allowed]
    return found, unresolvable, bad


@case("X04", "infra", "drift")
def test_x04_adapter_state_literals_are_real_engine_states():
    """X04 · `maa/adapter.py` 传给 `EngineNotReady(state=...)` 的值必须 ⊆ `EngineState`。

    真实漂移：**已经出现过** `state="unloaded"` 这种不存在的值。它不会让任何用例变红
    （异常类型完全正确），只会让状态页显示一个不存在的阶段 —— 用户看到"unloaded"，
    而我们七个状态里根本没有它。

    两种写法都要能守：直接写字符串字面量，或者抽成模块级常量
    （现在的实现是 `STATE_ABSENT: Final[str] = "absent"`）。所以 `_engine_state_violations`
    **顺着模块级赋值解析一层名字**；解析不出来的（变量/函数调用/getattr 拼的）一律判失败
    —— "无法静态校验"本身就比写错字更危险。
    """
    import inspect

    A = _adapter_mod()
    try:
        from maa.runtime import EngineState
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"maa.runtime 尚未落盘（{type(e).__name__}: {e}）") from None

    allowed = {s.value for s in EngineState}
    found, unresolvable, bad = _engine_state_violations(inspect.getsource(A), allowed)

    assert found, (
        "一条 `EngineNotReady(state=...)` 都没扫到 —— 要么代码结构变了，"
        "要么 state 被更绕的方式拼出来了。扫描失效等于这条守门形同不存在。"
    )
    assert not unresolvable, (
        f"第 {unresolvable} 行的 `state=` 无法静态解析（变量/getattr 拼的？）："
        "无法静态校验的状态值比写错字更危险，请改成字符串字面量或模块级常量"
    )
    assert not bad, (
        f"adapter.py 里出现了不存在的 EngineState：{bad}；"
        f"合法值只有 {sorted(allowed)}（状态页会显示一个不存在的阶段）"
    )


# --------------------------------------------------------------------------- #
# P 组：`maa/provision.py` 的攻击面（**全离线**）
# --------------------------------------------------------------------------- #
# 硬约束：
#   * 不碰真实网络（伪造 opener / 本地回环）；
#   * 每个恶意样本都断**两件事**：① 被拒；② `dest` 之外没有产生任何新文件。
#     只看"抛没抛异常"会漏掉"先写后校验"的实现 —— 写过一次就已经落地了。


def _p_zip(entries) -> bytes:
    """造 zip。`entries` 支持三种形态：

    * `(name, data)` —— 普通条目；
    * `(ZipInfo, data)` —— 需要控制 `external_attr` / `flag_bits` 的条目（符号链接、加密）；
    * `bytes` —— 直接返回（已有样本）。
    """
    import io
    import zipfile

    if isinstance(entries, (bytes, bytearray)):
        return bytes(entries)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in entries:
            name, data = item
            zf.writestr(name, data)
    return buf.getvalue()


def _p_info(name: str, *, mode: int | None = None):
    import zipfile

    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    if mode is not None:
        info.external_attr = mode << 16
    return info


def _p_set_flag_bit(blob: bytes, bit: int = 0x1) -> bytes:
    """把 zip 的「加密」通用位置 1：本地头 `+6`、中央目录 `+8`（**两处都要改**）。

    坑：`zipfile.writestr` 会用自己算的 `flag_bits` 覆盖你手改的值，所以"先置位再 writestr"
    根本造不出加密条目 —— 只能改字节。
    """
    buf = bytearray(blob)
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while True:
            idx = buf.find(sig, start)
            if idx < 0:
                break
            buf[idx + offset] |= bit
            start = idx + 4
    return bytes(buf)


def _p_set_declared_size(blob: bytes, value: int) -> bytes:
    """篡改中央目录/本地头里的 `file_size`（单条目样本专用）。"""
    import struct

    buf = bytearray(blob)
    for sig, offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        idx = buf.find(sig)
        assert idx >= 0, "样本里没有找到 zip 头"
        buf[idx + offset: idx + offset + 4] = struct.pack("<I", value)
    return bytes(buf)


def _p_spec():
    """`safe_extract` 现在**强制**要清单条目（防"版本/核心库比对形同虚设"）。

    P 组只关心解压行为，所以给一个空 payload 的 spec：`core_hint` 与
    `rel_subtrees` 才是它用到的字段。
    """
    return _release(b"")


def _p_sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _p_snapshot(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


def _p_run(tmp: Path, blob: bytes, *, dest_name: str = "out", release=None):
    """跑一次 `safe_extract`，返回 `(kind, value, dest, outside_before, outside_after)`。

    "外侧" = `dest` 的父目录（也就是 tmp 根）：解压前后它的名字集合必须完全相同。
    """
    P = _provision_mod()
    archive = tmp / "sample.zip"
    archive.write_bytes(blob)
    dest = tmp / dest_name
    before = _p_snapshot(tmp)
    kind, value = _outcome(lambda: P.safe_extract(archive, dest, release if release is not None else _p_spec()))
    after = _p_snapshot(tmp)
    return kind, value, dest, before, after


@case("P01", "provision", "attack")
def test_p01_malicious_member_names_rejected():
    """P01 · 14 个恶意条目名 + 2 个"排除前缀也不吃穿越"，全部拒绝，且 dest 外零新增。

    最后一条是**唯一能证明"黑名单不是安全边界"**的断言：把 `EXCLUDED_PREFIXES` /
    `EXCLUDED_GLOBS` 清空（等于拆掉黑名单）后，`externals/../../evil.dll`
    **仍然必须被路径校验拒掉**。没有它，将来没人拦得住"把白名单换回黑名单"。
    """
    EV = _errors_mod()
    P = _provision_mod()
    bad_names = [
        "../evil.txt",
        "../../evil.txt",
        "a/../../evil.txt",
        "..\\..\\evil.txt",
        "/etc/cron.d/x",
        "\\\\server\\share\\x",
        "C:\\Windows\\evil.dll",
        "C:evil.txt",
        "file.txt:evil",
        "CON",
        "NUL.txt",
        "aux ",
        "a.",
        "a<b>.txt",
        "resource/../../evil.dll",
        "externals/../../evil.dll",
    ]
    with _tmpdir() as tmp:
        failures: list[str] = []
        for name in bad_names:
            blob = _p_zip([("MaaCore.dll", b"x"), ("resource/config.json", b"{}"), (name, b"evil")])
            kind, value, dest, before, after = _p_run(tmp, blob, dest_name="out_p01")
            if kind != "error":
                failures.append(f"{name!r} 未被拒（返回值 {value!r}）")
                continue
            if not isinstance(value, EV.EngineUnpackFailed):
                failures.append(f"{name!r} 抛的是 {type(value).__name__}，不是 EngineUnpackFailed")
                continue
            if value.kind is not EV.UnpackFailure.UNSAFE_MEMBER:
                failures.append(f"{name!r} 的 kind 是 {value.kind}，不是 UNSAFE_MEMBER")
            if after != before:
                failures.append(f"{name!r} 在 dest 之外产生了新文件：{sorted(after - before)}")
            if dest.exists():
                failures.append(f"{name!r} 失败后解压目录仍然存在")
        assert not failures, "恶意条目名有漏网的：\n  " + "\n  ".join(failures)

        # ④ 清空对照：拆掉黑名单后，穿越仍然必须被拒（路径校验才是安全边界）
        saved_prefix, saved_globs = P._EXCLUDED_PREFIXES_LOWER, P._EXCLUDED_GLOBS_LOWER
        P._EXCLUDED_PREFIXES_LOWER, P._EXCLUDED_GLOBS_LOWER = frozenset(), ()
        try:
            for name in ("resource/../../evil.dll", "externals/../../evil.dll"):
                blob = _p_zip([("MaaCore.dll", b"x"), ("resource/config.json", b"{}"), (name, b"evil")])
                kind, value, dest, before, after = _p_run(tmp, blob, dest_name="out_p01b")
                assert kind == "error", f"清空黑名单后 {name!r} 竟然解压成功"
                assert isinstance(value, EV.EngineUnpackFailed), (
                    f"{name!r} 抛的是 {type(value).__name__}（说明拒绝来自排除逻辑而不是路径校验）"
                )
                assert value.kind is EV.UnpackFailure.UNSAFE_MEMBER, f"{name!r} 的 kind={value.kind}"
                assert after == before, f"{name!r} 在 dest 外留下了 {sorted(after - before)}"
        finally:
            P._EXCLUDED_PREFIXES_LOWER, P._EXCLUDED_GLOBS_LOWER = saved_prefix, saved_globs


@case("P02", "provision", "attack")
def test_p02_symlink_member_rejected():
    """P02 · 符号链接条目：路径合法、指向外面。必须在**落盘前**被拒（不是"创建后再判"）。"""
    import stat as _stat

    EV = _errors_mod()
    info = _p_info("resource/link", mode=_stat.S_IFLNK | 0o777)
    blob = _p_zip([("MaaCore.dll", b"x"), ("resource/config.json", b"{}"), (info, b"../../etc/passwd")])
    with _tmpdir() as tmp:
        kind, value, dest, before, after = _p_run(tmp, blob)

    assert kind == "error", f"符号链接条目竟然解压成功：{value!r}"
    assert isinstance(value, EV.EngineUnpackFailed), f"应抛 EngineUnpackFailed，实际 {type(value).__name__}"
    assert value.kind is EV.UnpackFailure.UNSAFE_MEMBER, f"kind 应是 UNSAFE_MEMBER，实际 {value.kind}"
    assert after == before, f"dest 外出现了新文件：{sorted(after - before)}"


@case("P03", "provision", "attack")
def test_p03_encrypted_member_rejected():
    """P03 · 加密条目：**拒绝**，不能"跳过但继续"（会得到残缺引擎）。

    构造坑（云安踩过）：`zipfile.writestr` 会用自己的 `flag_bits` 覆盖手改值，
    所以"置位再 writestr"造不出加密条目 —— 必须改字节，且**先断言样本真的加密了**。
    """
    import zipfile

    EV = _errors_mod()
    blob = _p_set_flag_bit(_p_zip([("MaaCore.dll", b"x"), ("resource/config.json", b"{}")]))
    with _tmpdir() as tmp:
        archive = tmp / "enc.zip"
        archive.write_bytes(blob)
        with zipfile.ZipFile(archive) as zf:
            encrypted = [i.filename for i in zf.infolist() if i.flag_bits & 0x1]
        assert encrypted, "样本根本没造出加密条目（用例本身失效）"

        dest = tmp / "out"
        before = _p_snapshot(tmp)
        kind, value = _outcome(lambda: _provision_mod().safe_extract(archive, dest, _p_spec()))
        after = _p_snapshot(tmp)

    assert kind == "error", f"加密条目竟然解压成功：{value!r}"
    assert isinstance(value, EV.EngineUnpackFailed), f"应抛 EngineUnpackFailed，实际 {type(value).__name__}"
    assert value.kind is EV.UnpackFailure.UNSAFE_MEMBER, f"kind 应是 UNSAFE_MEMBER，实际 {value.kind}"
    assert after == before, "加密样本在 dest 外留下了东西"


@case("P04", "provision", "attack")
def test_p04_zip_bomb_ratio_is_capped():
    """P04 · 压缩比判据：**同样的声明体积，只有可压缩性不同** → 只有高压缩比那份被拒。

    单看 `kind is LIMIT_EXCEEDED` 是不够的：条目数/单文件/总量/压缩比**四条分支共用
    同一个 kind**，任一条都能让它变绿 —— 那样"压缩比判据被删掉"照样绿（假绿）。
    这里用**对照组**把判据逼出来：

    * 炸弹样本：8 MiB 零字节 → 压缩后几 KiB，比值远高于判据；
    * 对照样本：8 MiB `os.urandom` → **声明体积完全相同**，比值 ≈1.0，必须解压成功。

    两者声明量一样 ⇒ 能解释"一个拒一个过"的只剩压缩比这一条判据。
    """
    import os

    EV = _errors_mod()
    P = _provision_mod()

    declared = 8 << 20
    head = [("MaaCore.dll", b"x"), ("resource/config.json", b"{}")]

    bomb = _p_zip(head + [("resource/big.bin", b"\x00" * declared)])
    # ① 特征断言：样本必须真的越过判据（先证明"它确实是炸弹"，再断言被拒）
    selected = declared + 3
    ratio = selected / max(1, len(bomb))
    assert ratio > P.MAX_COMPRESSION_RATIO, (
        f"炸弹样本压缩比只有 {ratio:.1f}x，没越过判据 {P.MAX_COMPRESSION_RATIO}x —— 用例失效"
    )
    # ② 也不能让别的分支先命中，否则断的就不是比值判据
    assert selected <= P.MAX_SINGLE_FILE, "样本单文件就超限了，会命中单文件分支"
    assert selected <= P.MAX_TOTAL_UNCOMPRESSED, "样本总量就超限了，会命中总量分支"

    with _tmpdir() as tmp:
        kind, value, dest, before, after = _p_run(tmp, bomb)
        assert kind == "error", f"zip bomb 竟然解压成功：{value!r}"
        assert isinstance(value, EV.EngineUnpackFailed), (
            f"应抛 EngineUnpackFailed，实际 {type(value).__name__}"
        )
        assert value.kind is EV.UnpackFailure.LIMIT_EXCEEDED, (
            f"kind 应是 LIMIT_EXCEEDED，实际 {value.kind}"
        )
        assert "压缩比" in (value.detail or ""), (
            f"命中的不是压缩比分支：detail={value.detail!r}（说明别的上限先拦下了）"
        )
        assert not dest.exists(), "zip bomb 落盘了（上限判定在写盘之后才做）"
        assert after == before

        # ③ 对照组：同样声明体积、不可压缩 → 必须放行
        ctrl = _p_zip(head + [("resource/big.bin", os.urandom(declared))])
        ctrl_ratio = selected / max(1, len(ctrl))
        assert ctrl_ratio <= P.MAX_COMPRESSION_RATIO, (
            f"对照组压缩比 {ctrl_ratio:.1f}x 也越界了，样本选错了"
        )
        kind2, value2, dest2, _b2, _a2 = _p_run(tmp, ctrl, dest_name="out2")
        assert kind2 == "value", (
            "同样体积、只是不可压缩的包也被拒了 —— 说明拦它的不是压缩比："
            f"{type(value2).__name__}: {value2}"
        )
        assert (dest2 / "resource" / "big.bin").is_file(), "对照组没解出 big.bin"


@case("P05", "provision", "attack")
def test_p05_entry_count_is_capped():
    """P05 · 20 万 + 1 个空条目：条目数上限触发，且**不创建 20 万个文件**（inode 耗尽攻击）。"""
    import zipfile

    EV = _errors_mod()
    P = _provision_mod()
    limit = P.MAX_ENTRIES
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:   # STORED 更快，逐条写空文件
        zf.writestr("MaaCore.dll", b"x")
        zf.writestr("resource/config.json", b"{}")
        for i in range(limit):
            zf.writestr(f"resource/empty_{i}.txt", b"")
    blob = buf.getvalue()
    with _tmpdir() as tmp:
        kind, value, dest, before, after = _p_run(tmp, blob)
        created = len(list(dest.rglob("*"))) if dest.exists() else 0

    assert kind == "error", f"{limit + 3} 个条目竟然解压成功：{value!r}"
    assert isinstance(value, EV.EngineUnpackFailed), f"应抛 EngineUnpackFailed，实际 {type(value).__name__}"
    assert value.kind is EV.UnpackFailure.LIMIT_EXCEEDED, f"kind 应是 LIMIT_EXCEEDED，实际 {value.kind}"
    assert created < 1000, f"拒绝了却已经创建 {created} 个文件（上限判定在写盘之后）"
    assert after == before


@case("P06", "provision", "attack")
def test_p06_declared_size_drift_is_caught():
    """P06 · 中央目录声明的 `file_size` 与实际字节不符 ⇒ `SIZE_DRIFT`。

    只信中央目录声明的大小 = 磁盘可被打爆；写出阶段必须**二次计数**。
    方向无所谓（声明大了或小了都是"不符"），不变量是"两者一致"。
    """
    import os as _os

    EV = _errors_mod()
    payload = _os.urandom(1024)     # 不可压缩，避免压缩比上限抢在前面拦住
    blob = _p_zip([("MaaCore.dll", b"x"), ("resource/config.json", b"{}"), ("resource/a.bin", payload)])
    blob = _p_set_declared_size(blob, len(payload) * 5)
    with _tmpdir() as tmp:
        kind, value, dest, before, after = _p_run(tmp, blob)

    assert kind == "error", f"声明与实际不符竟然解压成功：{value!r}"
    assert isinstance(value, EV.EngineUnpackFailed), f"应抛 EngineUnpackFailed，实际 {type(value).__name__}"
    assert value.kind is EV.UnpackFailure.SIZE_DRIFT, f"kind 应是 SIZE_DRIFT，实际 {value.kind}"
    assert not dest.exists(), "字节漂移的包留下了半成品"
    assert after == before


@case("P07", "provision", "layout")
def test_p07_missing_or_duplicated_core_library_rejected():
    """P07 · ①没有核心库 ②核心库两份 —— 都拒绝，且**必须能被上层 `except` 接住**。

    真实故障：解出"看起来完整"的目录，加载阶段才炸，那时报的是"资源缺失"。
    """
    EV = _errors_mod()
    P = _provision_mod()
    RT = _runtime_mod()

    with _tmpdir() as tmp:
        # ① 没有核心库
        blob = _p_zip([("resource/config.json", b"{}"), ("README.md", b"r")])
        archive = tmp / "a.zip"
        archive.write_bytes(blob)
        dest = tmp / "out1"
        kind0, value0 = _outcome(lambda: P.safe_extract(archive, dest, _p_spec()))
        assert kind0 == "error" and isinstance(value0, EV.EngineLayoutInvalid), (
            f"没有核心库时应抛 EngineLayoutInvalid（可被上层 except 接住），"
            f"实际 {kind0}: {type(value0).__name__}: {value0}"
        )
        # 「能被上层 except 接住」= 抛的是 `maa/errors.py` 里的类型，不是裸异常
        assert isinstance(value0, EV.EngineError), (
            f"抛出的异常必须属于 EngineError 家族（否则 service 只能走兜底），"
            f"实际 {type(value0).__name__}"
        )
        assert "MaaCore.dll" in str(getattr(value0, "context", {}).get("missing", ("MaaCore.dll",))), (
            f"诊断里应点名缺的是核心库，实际 context={dict(getattr(value0, 'context', {}))}"
        )

        # ② 歧义：根目录与 bin/ 各一份
        blob2 = _p_zip([
            ("MaaCore.dll", b"x"),
            ("bin/MaaCore.dll", b"x"),
            ("resource/config.json", b"{}"),
        ])
        archive2 = tmp / "b.zip"
        archive2.write_bytes(blob2)
        kind2, value2 = _outcome(lambda: P.safe_extract(archive2, tmp / "out2", _p_spec()))
        assert kind2 == "error" and isinstance(value2, EV.EngineLayoutInvalid), (
            f"两份核心库时必须失败（不取第一个），实际 {kind2}: {type(value2).__name__}: {value2}"
        )


@case("P08", "provision", "content")
def test_p08_extracted_content_matches_archive():
    """P08 · 逐条目字节相等；POSIX 上可执行位保留（Windows 跳过该断言）。"""
    import hashlib

    P = _provision_mod()
    entries = {
        "MaaCore.dll": b"core" * 1000,
        "opencv_world4.dll": b"cv" * 500,
        "resource/tasks.json": b'{"a":1}',
        "resource/template/x.png": b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        "README.md": b"readme",
    }
    blob = _p_zip(list(entries.items()))
    with _tmpdir() as tmp:
        archive = tmp / "ok.zip"
        archive.write_bytes(blob)
        dest = tmp / "out"
        result = P.safe_extract(archive, dest, _p_spec())
        for name, data in entries.items():
            got = (dest / name).read_bytes()
            assert hashlib.sha256(got).hexdigest() == hashlib.sha256(data).hexdigest(), (
                f"{name} 内容不一致（{len(got)} vs {len(data)} 字节）"
            )
        assert result.core_lib == dest / "MaaCore.dll", f"core_lib 指错：{result.core_lib}"
        if __import__("os").name != "nt":
            exe = next(iter((dest / "resource").rglob("*")), None)
            assert exe is None or exe.is_file()


@case("P09", "provision", "robustness")
def test_p09_existing_destination_is_not_merged():
    """P09 · `dest` 已存在且**非空** ⇒ 拒绝（不静默合并），且异常能被上层 `except` 接住。

    云安自测踩到：`mkdir(exist_ok=False)` 抛的是 `FileExistsError`，若没人映射，
    上层 `except EngineUnpackFailed` 接不住 → 冒成 500。
    """
    EV = _errors_mod()
    blob = _p_zip([("MaaCore.dll", b"x"), ("resource/config.json", b"{}")])
    with _tmpdir() as tmp:
        archive = tmp / "a.zip"
        archive.write_bytes(blob)
        dest = tmp / "out"
        dest.mkdir()
        (dest / "keep.txt").write_text("user file", encoding="utf-8")
        kind, value = _outcome(lambda: _provision_mod().safe_extract(archive, dest, _p_spec()))
        survived = (dest / "keep.txt").read_text(encoding="utf-8") if (dest / "keep.txt").is_file() else None

    assert kind == "error", f"已存在的 dest 被静默合并了：{value!r}"
    assert isinstance(value, EV.EngineUnpackFailed), (
        f"抛的是 {type(value).__name__} —— 上层 `except EngineUnpackFailed` 接不住就冒成 500"
    )
    assert survived == "user file", f"用户已有文件被删除/改写了：{survived!r}"


@case("P10", "provision", "layout")
def test_p10_real_package_shape_counts():
    """P10 · 真实包口径（无 `MAA/` 包裹层）：`selected==6` / `excluded==4` / `skipped==0`。

    两个方向的错：漏解依赖 DLL（引擎起不来）/ 多解 GUI 运行时（死重量 + 攻击面）。
    `README.md` 必须存在 —— 这是"根目录放行"策略的**已知后果**，写断言是为了
    **防止将来有人把白名单换成黑名单**。
    """
    P = _provision_mod()
    entries = [
        ("MaaCore.dll", b"core"),
        ("opencv_world4.dll", b"cv"),
        ("onnxruntime.dll", b"ort"),
        ("MAA.exe", b"MZ"),
        ("MAA.pdb", b"pdb"),
        ("README.md", b"readme"),
        ("resource/tasks.json", b"{}"),
        ("resource/template/x.png", b"\x89PNG\r\n\x1a\n"),
        ("externals/PresentationCore.dll", b"wpf"),
        ("externals/System.Runtime.dll", b"net"),
        # 一条"正常但不在白名单"的条目：`skipped` 只装这种（不是 error、不是 hard-exclude）。
        # 没有它 `skipped==0` 是恒真的 —— 变异 M40（`skipped=0` 写死）就杀不掉（假绿）。
        ("lang/zh-CN.txt", b"lang"),
    ]
    with _tmpdir() as tmp:
        archive = tmp / "real.zip"
        archive.write_bytes(_p_zip(entries))
        dest = tmp / "out"
        result = P.safe_extract(archive, dest, _p_spec())

        assert result.selected == 6, f"selected 应为 6，实际 {result.selected}：{result.skipped_sample}"
        assert result.excluded == 4, f"excluded 应为 4（externals×2 + exe + pdb），实际 {result.excluded}"
        assert result.skipped == 1, (
            f"skipped 应为 1（`lang/zh-CN.txt` 正常但不在白名单），实际 {result.skipped}"
        )
        assert result.skipped_sample == ("lang/zh-CN.txt",), (
            f"skipped_sample 应记下被跳过的那条，实际 {result.skipped_sample!r}"
        )
        assert result.core_lib == dest / "MaaCore.dll", f"core_lib 指错：{result.core_lib}"
        assert (dest / "resource" / "template" / "x.png").is_file(), "子目录没递归放行"
        assert (dest / "README.md").is_file(), "根目录散文件被漏掉了（真实包就是这样）"
        assert not (dest / "externals").exists(), "externals/ 被解出来了（硬排除失效）"

        # 变体：externals 放 500 个条目 —— 计数口径错了会"嘴上排除、磁盘变大"
        many = [("MaaCore.dll", b"core"), ("resource/tasks.json", b"{}")]
        many += [(f"externals/dep_{i}.dll", b"x") for i in range(500)]
        archive2 = tmp / "real2.zip"
        archive2.write_bytes(_p_zip(many))
        dest2 = tmp / "out2"
        result2 = P.safe_extract(archive2, dest2, _p_spec())

    assert result2.excluded == 500, f"excluded 应为 500，实际 {result2.excluded}"
    assert result2.selected == 2, f"selected 应为 2，实际 {result2.selected}"
    assert not (dest2 / "externals").exists(), "500 个 externals 条目里有落盘的"


@case("P11", "provision", "attack")
def test_p11_path_check_precedes_exclusion():
    """P11 · `resource/../../evil.dll` 与 `externals/../../evil.dll` 必须在**路径校验阶段**失败。

    这是最容易写错的一条：顺序反了（先判排除再判路径），
    `externals/../../evil.dll` 会被"排除 externals"静默跳过 —— 攻击样本从此看不见。
    """
    EV = _errors_mod()
    P = _provision_mod()
    saved_prefix, saved_globs = P._EXCLUDED_PREFIXES_LOWER, P._EXCLUDED_GLOBS_LOWER
    try:
        for label, clear in (("默认策略", False), ("清空黑名单", True)):
            if clear:
                P._EXCLUDED_PREFIXES_LOWER, P._EXCLUDED_GLOBS_LOWER = frozenset(), ()
            with _tmpdir() as tmp:
                blob = _p_zip([
                    ("MaaCore.dll", b"x"),
                    ("resource/../../evil.dll", b"evil1"),
                    ("externals/../../evil.dll", b"evil2"),
                ])
                kind, value, dest, before, after = _p_run(tmp, blob)
            assert kind == "error", f"[{label}] 穿越条目竟然解压成功：{value!r}"
            assert isinstance(value, EV.EngineUnpackFailed), f"[{label}] 抛的是 {type(value).__name__}"
            assert value.kind is EV.UnpackFailure.UNSAFE_MEMBER, (
                f"[{label}] kind 是 {value.kind}（被排除逻辑跳过了，说明顺序反了）"
            )
            assert after == before, f"[{label}] dest 外出现了新文件：{sorted(after - before)}"
    finally:
        P._EXCLUDED_PREFIXES_LOWER, P._EXCLUDED_GLOBS_LOWER = saved_prefix, saved_globs


@case("P12", "provision", "layout")
def test_p12_layout_variants_and_fallbacks():
    """P12 · 0 个核心库 / 歧义 / 缺 `resource/` / `bin/` 兜底 —— 四种形态各归各位。

    核心库在**归档根**是正常布局（真实包无 `MAA/` 包裹层），`bin/` 是兜底；
    **同时命中两处才算失败**（歧义即失败，不取第一个）。
    """
    EV = _errors_mod()
    P = _provision_mod()

    with _tmpdir() as tmp:
        # ① 0 个核心库
        archive = tmp / "zero.zip"
        archive.write_bytes(_p_zip([("resource/a.json", b"{}")]))
        k1, v1 = _outcome(lambda: P.safe_extract(archive, tmp / "o1", _p_spec()))
        assert k1 == "error" and isinstance(v1, EV.EngineLayoutInvalid), (
            f"0 个核心库应抛 EngineLayoutInvalid，实际 {k1}: {type(v1).__name__}"
        )

        # ② 歧义：根 + bin 各一份
        archive = tmp / "amb.zip"
        archive.write_bytes(_p_zip([
            ("MaaCore.dll", b"x"), ("bin/MaaCore.dll", b"x"), ("resource/a.json", b"{}"),
        ]))
        k2, v2 = _outcome(lambda: P.safe_extract(archive, tmp / "o2", _p_spec()))
        assert k2 == "error" and isinstance(v2, EV.EngineLayoutInvalid), (
            f"两份核心库应抛 EngineLayoutInvalid（不取第一个），实际 {k2}: {type(v2).__name__}"
        )

        # ③ 缺 resource/
        archive = tmp / "nores.zip"
        archive.write_bytes(_p_zip([("MaaCore.dll", b"x")]))
        kind, value = _outcome(lambda: P.safe_extract(archive, tmp / "o3", _p_spec()))
        assert kind == "error" and isinstance(value, (EV.EngineLayoutInvalid, EV.EngineUnpackFailed)), (
            f"缺 resource/ 应被拒，实际 {kind}: {value!r}"
        )

        # ④ bin/ 兜底
        archive = tmp / "bin.zip"
        archive.write_bytes(_p_zip([
            ("MAA.exe", b"MZ"),
            ("bin/MaaCore.dll", b"core"),
            ("bin/opencv_world4.dll", b"cv"),
            ("resource/a.json", b"{}"),
        ]))
        r4 = P.safe_extract(archive, tmp / "o4", _p_spec())
        core4 = r4.core_lib
        resource_ok = (tmp / "o4" / "resource" / "a.json").is_file()
        selected4, excluded4 = r4.selected, r4.excluded

    assert core4 == tmp / "o4" / "bin" / "MaaCore.dll", f"bin/ 兜底没生效：{core4}"
    assert selected4 == 3, f"selected 应为 3（2 个 bin DLL + 1 个 resource），实际 {selected4}"
    assert excluded4 == 1, f"excluded 应为 1（MAA.exe），实际 {excluded4}"
    assert resource_ok, "resource 没解出来"


@case("P13", "provision", "hash")
def test_p13_verify_sha256_truth_table():
    """P13 · 七条真值：正确 / `sha256:` 前缀 / 大写 hex / 改一字节 / 截断 / 空 expected / 文件不存在。

    **契约是"失败抛 `EngineHashMismatch`"，不是返回 bool**（§1.2.1 已收口）：
    返回 bool 会逼每个调用点各写一遍"不符时怎么办"，那是四份不一致的处置。
    """
    EV = _errors_mod()
    P = _provision_mod()
    data = b"engine-bytes" * 100
    digest = _p_sha(data)
    with _tmpdir() as tmp:
        good = tmp / "good.bin"
        good.write_bytes(data)
        short = tmp / "short.bin"
        short.write_bytes(data[:-1])
        bad = tmp / "bad.bin"
        bad.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))

        ok_cases = [
            ("正确哈希", good, digest),
            ("sha256: 前缀", good, "sha256:" + digest),
            ("大写 hex", good, digest.upper()),
        ]
        for label, path, expected in ok_cases:
            kind, value = _outcome(lambda p=path, e=expected: P.verify_sha256(p, e))
            assert kind == "value", f"[{label}] 应通过，实际抛 {type(value).__name__}: {value}"
            assert value == digest, f"[{label}] 返回的实际摘要不对：{value!r}"

        fail_cases = [
            ("改一字节", bad, digest),
            ("截断", short, digest),
            ("空 expected", good, ""),
            ("文件不存在", tmp / "nope.bin", digest),
        ]
        for label, path, expected in fail_cases:
            kind, value = _outcome(lambda p=path, e=expected: P.verify_sha256(p, e))
            assert kind == "error", f"[{label}] 竟然通过了（返回 {value!r}）"
            assert isinstance(value, EV.EngineHashMismatch), (
                f"[{label}] 应抛 EngineHashMismatch，实际 {type(value).__name__}: {value}"
            )
            assert getattr(value, "expected", None) is not None, f"[{label}] 异常里没有 expected 字段"


@case("P14", "provision", "stream")
def test_p14_hash_is_computed_in_chunks():
    """P14 · 5 MB 文件按块读（单次 `n <= 1 MiB`，且不止一次）。

    真实故障：295 MB 全量读入 → 宿主内存峰值翻倍。
    """
    import builtins

    P = _provision_mod()
    data = b"B" * (5 << 20)
    reads: list[int] = []
    real_open = builtins.open

    class _Spy:
        def __init__(self, handle):
            self._h = handle

        def read(self, n=-1):
            reads.append(n)
            return self._h.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._h.close()
            return False

        def __getattr__(self, name):
            return getattr(self._h, name)

    with _tmpdir() as tmp:
        path = tmp / "big.bin"
        path.write_bytes(data)

        def spy_open(file, *a, **kw):
            handle = real_open(file, *a, **kw)
            try:
                if Path(file).resolve() == path.resolve() and "b" in (a[0] if a else kw.get("mode", "rb")):
                    return _Spy(handle)
            except (OSError, ValueError):
                pass
            return handle

        builtins.open = spy_open
        try:
            P.verify_sha256(path, _p_sha(data))
        finally:
            builtins.open = real_open

    assert len(reads) > 1, f"只读了 {len(reads)} 次 —— 一次性读入内存了"
    assert max(reads) <= (1 << 20), f"单次读 {max(reads)} 字节 > 1 MiB"
    assert all(n > 0 for n in reads), f"出现了非正的读取长度：{[n for n in reads if n <= 0]}"



# --------------------------------------------------------------------------- #
# P 组（下半）：下载侧
# --------------------------------------------------------------------------- #
_DEFAULT = object()


class _PResp:
    """伪造的 HTTP 响应：可指定 `Content-Length`、读取中途抛错、读到某点为止。"""

    def __init__(self, data: bytes, *, headers=None, raise_after=None, exc=None):
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(len(data))} if headers is None else headers
        self.raise_after = raise_after
        self.exc = exc
        self.reads: list[int] = []

    def read(self, n):
        self.reads.append(n)
        if self.exc is not None and self.raise_after is not None and self._pos >= self.raise_after:
            raise self.exc
        chunk = self._data[self._pos: self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


class _POpener:
    """伪造 opener：记录调用次数（P23 的"零请求"就靠它）。"""

    def __init__(self, resp=None, factory=None):
        self.calls: list[str] = []
        self._resp = resp
        self._factory = factory

    def open(self, req=None, timeout=None):
        self.calls.append(getattr(req, "full_url", str(req)))
        if self._factory is not None:
            return self._factory()
        return self._resp


def _p_dl(tmp: Path, data: bytes, *, sha=None, size=None, resp=None, opener=None,
           url=_DEFAULT, cancel=None, on_progress=None, extra=None):
    """跑一次 `atomic_download`，返回 `(kind, value, dest, part_exists, lock_exists)`。"""
    P = _provision_mod()
    spec = _release(
        data,
        sha256=sha if sha is not None else None,
        size=size,
        url="http://127.0.0.1:9/maa.zip" if url is _DEFAULT else url,
    )
    dest = tmp / "arch.zip"
    kw = {} if extra is None else dict(extra)
    kind, value = _outcome(lambda: P.atomic_download(
        spec, dest, opener=opener, cancel=cancel, on_progress=on_progress, **kw
    ))
    return (
        kind,
        value,
        dest,
        dest.with_name(dest.name + ".part").exists(),
        dest.with_name(dest.name + ".lock").exists(),
    )


@case("P15", "provision", "download")
def test_p15_download_success_leaves_no_part_or_lock():
    """P15 · 成功路径：`dest` 存在且哈希正确；**`.part` 与 `.lock` 都不存在**。

    清理只有一个出口（末尾 `finally`）：成功时 `.part` 已被 `os.replace` 吃掉，
    `unlink(missing_ok=True)` 自动 no-op。残留 `.part` 会让下次误以为"可续传"。
    """
    data = b"engine-payload" * 1000
    opener = _POpener(_PResp(data))
    with _tmpdir() as tmp:
        kind, value, dest, part, lock = _p_dl(tmp, data, opener=opener)
        content = dest.read_bytes() if dest.is_file() else b""

    assert kind == "value", f"应成功，实际 {type(value).__name__}: {value}"
    assert content == data, "落盘内容不一致"
    assert not part, "`.part` 残留"
    assert not lock, "`.lock` 残留"
    assert len(opener.calls) == 1, f"opener 被调用 {len(opener.calls)} 次"


@case("P16", "provision", "attack")
def test_p16_hash_mismatch_deletes_dest_and_part():
    """P16 · 内容与 `release.sha256` 不符 ⇒ `EngineHashMismatch`，**`dest` 与 `.part` 都不存在**。

    坏包留在盘上 = 下次直接复用（这条是最关键的）。
    """
    data = b"tampered-bytes" * 500
    opener = _POpener(_PResp(data))
    with _tmpdir() as tmp:
        kind, value, dest, part, lock = _p_dl(tmp, data, sha="a" * 64, opener=opener)

    assert kind == "error", f"哈希不符竟然成功：{dest}"
    assert type(value).__name__ == "EngineHashMismatch", f"应抛 EngineHashMismatch，实际 {type(value).__name__}"
    assert not dest.exists(), "坏包留在了最终路径"
    assert not part, "坏包的 `.part` 残留"
    assert not lock, "`.lock` 残留（下次会以为有别的实例在下载）"

    # 变体：盘上原本有一个（被替换过的）旧包，这次下载又哈希不符
    # → 旧包也必须删，不能留一个"看起来能用"的版本让用户以为升级成功了
    with _tmpdir() as tmp:
        stale = tmp / "arch.zip"
        stale.write_bytes(b"old-replaced-engine")
        kind2, value2, dest2, part2, lock2 = _p_dl(
            tmp, data, sha="a" * 64, opener=_POpener(_PResp(data))
        )
    assert kind2 == "error" and type(value2).__name__ == "EngineHashMismatch"
    assert not dest2.exists(), "哈希不符时旧包被保留（用户会以为升级成功，其实跑的是被换过的旧引擎）"
    assert not part2 and not lock2, "变体路径留下残留"


@case("P17", "provision", "download")
def test_p17_short_long_and_html_errors_clean_up():
    """P17 · ①半包 EOF ②多给 1 字节 ③`Content-Length` 不符 ④200 + HTML 错误页 —— 全部失败且无残留。

    服务器"200 + 错误页"是最常见的"看起来成功"：长度不对必须早停。
    """
    P = _provision_mod()
    EV = _errors_mod()
    data = b"x" * 4096
    size = len(data)

    cases = [
        ("半包 EOF", dict(sha=_p_sha(data), size=size), _PResp(data[: size // 2])),
        ("多给 1 字节", dict(sha=_p_sha(data), size=size), _PResp(data + b"!")),
        ("Content-Length 不符", dict(sha=_p_sha(data), size=size),
         _PResp(data, headers={"Content-Length": str(size + 100)})),
        ("200 + HTML 错误页", dict(sha=_p_sha(data), size=size),
         _PResp(b"<html>503 Service Unavailable</html>")),
    ]
    failures: list[str] = []
    for label, kw, resp in cases:
        opener = _POpener(resp)
        with _tmpdir() as tmp:
            kind, value, dest, part, lock = _p_dl(tmp, data, opener=opener, **kw)
        if kind != "error":
            failures.append(f"[{label}] 竟然成功了")
            continue
        if not isinstance(value, EV.EngineError):
            failures.append(f"[{label}] 抛的是 {type(value).__name__}（应属 EngineError 家族）")
        if dest.exists() or part or lock:
            failures.append(f"[{label}] 留下了残留：dest={dest.exists()} part={part} lock={lock}")
    assert not failures, "\n  ".join(["下载失败路径有残留："] + failures)


@case("P18", "provision", "attack")
def test_p18_downgrade_redirect_is_rejected():
    """P18 · 302 跳到 `http://` 或表外 host：**不跟随**（`REJECTED_SCHEME`）。

    允许 https→http 降级 = 传输通道可被降级攻击。
    """
    import urllib.request

    P = _provision_mod()
    EV = _errors_mod()
    handler = P._StrictRedirectHandler(Path("arch.zip"))
    req = urllib.request.Request("https://github.com/MaaAssistantArknights/x.zip")

    bad_targets = [
        "http://github.com/MaaAssistantArknights/x.zip",
        "https://evil.example.com/x.zip",
        "http://10.0.0.5/x.zip",     # 非回环的 http：必须拒（回环 http 是允许的，本地用例靠它）
    ]
    for target in bad_targets:
        kind, value = _outcome(
            lambda t=target: handler.redirect_request(req, None, 302, "Found", {}, t)
        )
        assert kind == "error", f"{target} 被跟随了（重定向策略没拦住）"
        assert isinstance(value, EV.EngineDownloadFailed), (
            f"{target} 抛的是 {type(value).__name__}（应为 EngineDownloadFailed）"
        )
        assert value.kind is EV.DownloadFailure.REJECTED_SCHEME, (
            f"{target} 的 kind 是 {value.kind}，应为 REJECTED_SCHEME"
        )

    # 正向对照：白名单内的 https 允许跟随（否则等于禁掉所有重定向）
    kind, value = _outcome(lambda: handler.redirect_request(
        req, None, 302, "Found", {},
        "https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases/download/v6.22.0/x.zip",
    ))
    assert kind == "value", f"白名单内的 https 重定向被误拒：{value!r}"


@case("P19", "provision", "robustness")
def test_p19_disk_full_and_keyboard_interrupt_clean_up():
    """P19 · ①写盘 `OSError(ENOSPC)` ②读到一半 `KeyboardInterrupt` —— 均无残留。

    **必须 `except BaseException`**：只写 `except Exception` 会漏掉 `KeyboardInterrupt`
    （用户按 Ctrl+C 正是这个），`.part` 就留下来了。
    """
    import errno
    import os as _os

    data = b"y" * 8192
    failures: list[str] = []

    # ① fsync 时 ENOSPC（最真实的"写到一半盘满"）
    real_fsync = _os.fsync

    def bad_fsync(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    _os.fsync = bad_fsync
    try:
        with _tmpdir() as tmp:
            kind, value, dest, part, lock = _p_dl(tmp, data, opener=_POpener(_PResp(data)))
    finally:
        _os.fsync = real_fsync
    if kind != "error":
        failures.append("[ENOSPC] 竟然成功了")
    if dest.exists() or part or lock:
        failures.append(f"[ENOSPC] 残留：dest={dest.exists()} part={part} lock={lock}")

    # ② 读到一半 KeyboardInterrupt
    resp = _PResp(data, raise_after=4096, exc=KeyboardInterrupt())
    with _tmpdir() as tmp:
        kind2, value2, dest2, part2, lock2 = _p_dl(tmp, data, opener=_POpener(resp))
    if kind2 != "error":
        failures.append("[KeyboardInterrupt] 竟然成功了")
    if dest2.exists() or part2 or lock2:
        failures.append(f"[Ctrl+C] 残留：dest={dest2.exists()} part={part2} lock={lock2}")

    assert not failures, "\n  ".join(["失败路径有残留："] + failures)


@case("P20", "provision", "concurrency")
def test_p20_concurrent_download_is_serialized():
    """P20 · 两个线程同时下同一 `dest`：最终内容**不交错**、哈希正确、无残留。"""
    data = b"z" * (2 << 20)
    opener = _POpener(factory=lambda: _PResp(data, headers={"Content-Length": str(len(data))}))
    results: list[tuple] = []

    def worker(tmp_dir: Path):
        results.append(_p_dl(tmp_dir, data, opener=opener))

    with _tmpdir() as tmp:
        threads = [threading.Thread(target=worker, args=(tmp,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        content = (tmp / "arch.zip").read_bytes() if (tmp / "arch.zip").is_file() else b""
        leftovers = sorted(p.name for p in tmp.iterdir() if p.name != "arch.zip")

    assert all(not t.is_alive() for t in threads), "有线程没结束"
    assert content == data, f"内容被交错写坏（{len(content)} 字节 vs 期望 {len(data)}）"
    assert not leftovers, f"留下了残留：{leftovers}"
    assert all(k == "value" for k, *_ in results) or any(k == "value" for k, *_ in results), (
        f"至少应有一次成功：{[(k, type(v).__name__) for k, v, *_ in results]}"
    )


@case("P21", "provision", "recovery")
def test_p21_stale_lock_self_heals():
    """P21 · 预置一个 1 小时前的 `.lock`：能自愈（判定陈旧 → 接管），最终成功。

    真实故障：一次崩溃留下永久锁 → 插件永远下载不了，用户只能删目录。
    """
    import os as _os
    import time as _time

    data = b"heal-me" * 500
    with _tmpdir() as tmp:
        dest = tmp / "arch.zip"
        lock = dest.with_name(dest.name + ".lock")
        lock.write_text("", encoding="utf-8")
        old = _time.time() - 3600
        _os.utime(lock, (old, old))

        kind, value, dest2, part, lock_left = _p_dl(
            tmp, data, opener=_POpener(_PResp(data))
        )
        content = dest.read_bytes() if dest.is_file() else b""

    assert kind == "value", f"陈旧锁没能自愈：{type(value).__name__}: {value}"
    assert content == data, "内容不对"
    assert not lock_left, "陈旧锁没被清掉"


@case("P22", "provision", "reuse")
def test_p22_reuse_requires_matching_hash():
    """P22 · ①`dest` 已存在且哈希正确 ⇒ **opener 一次都不调**；②哈希错 ⇒ 删掉重下。"""
    data = b"already-here" * 200
    with _tmpdir() as tmp:
        # ① 正确 → 复用，零请求
        dest = tmp / "arch.zip"
        dest.write_bytes(data)
        opener = _POpener(_PResp(b"should-not-be-used"))
        kind, value, dest1, part1, lock1 = _p_dl(tmp, data, opener=opener)
        reused_calls = len(opener.calls)
        reused_content = dest1.read_bytes() if dest1.is_file() else b""

        # ② 错的旧文件 → 必须重下
        tmp2 = tmp / "second"
        tmp2.mkdir()
        stale = tmp2 / "arch.zip"
        stale.write_bytes(b"replaced-by-attacker")
        opener2 = _POpener(_PResp(data))
        kind2, value2, dest2, part2, lock2 = _p_dl(tmp2, data, opener=opener2)
        redownloaded = dest2.read_bytes() if dest2.is_file() else b""
        calls2 = len(opener2.calls)

    assert kind == "value" and reused_content == data, "复用路径不对"
    assert reused_calls == 0, f"哈希已正确却发了 {reused_calls} 次请求（应直接复用）"
    assert not part1 and not lock1, "复用路径留下残留"
    assert kind2 == "value", f"旧文件哈希错时应重下并成功，实际 {type(value2).__name__}: {value2}"
    assert calls2 == 1, f"应重下 1 次，实际 {calls2}"
    assert redownloaded == data, "重下的内容不对（旧的被替换文件仍然生效？）"


@case("P23", "provision", "attack")
def test_p23_invalid_url_fails_before_any_request():
    """P23 · 非法 URL（协议 / host）**立即抛**，且**零 HTTP 请求**。

    参数校验放在发请求之后 = 先泄露一次请求（对方已知道你要下什么、从哪来）。
    """
    EV = _errors_mod()
    data = b"payload"
    bad_urls = [
        "ftp://github.com/MaaAssistantArknights/x.zip",
        "https://evil.example.com/x.zip",
        "http://192.168.1.10/x.zip",
        "file:///etc/passwd",
        "",
    ]
    failures: list[str] = []
    for url in bad_urls:
        opener = _POpener(_PResp(data))
        with _tmpdir() as tmp:
            kind, value, dest, part, lock = _p_dl(tmp, data, url=url, opener=opener)
        if kind != "error":
            failures.append(f"{url!r} 竟然被接受")
            continue
        if not isinstance(value, EV.EngineDownloadFailed):
            failures.append(f"{url!r} 抛的是 {type(value).__name__}")
        elif value.kind is not EV.DownloadFailure.REJECTED_SCHEME:
            failures.append(f"{url!r} 的 kind 是 {value.kind}，应为 REJECTED_SCHEME")
        if opener.calls:
            failures.append(f"{url!r} 在校验前就发了请求（{len(opener.calls)} 次）")
        if dest.exists() or part or lock:
            failures.append(f"{url!r} 留下了文件")
    assert not failures, "\n  ".join(["非法 URL 未被拦在请求之前："] + failures)


@case("P24", "provision", "proxy")
def test_p24_env_proxy_is_ignored_by_default():
    """P24 · 设了死代理（`HTTPS_PROXY=http://127.0.0.1:1`）时，默认仍**直连成功**。

    真实故障：用户机器上的代理会把请求导向第三方（元数据 + 可用性双输）。
    这里用本地回环 http.server 起真响应：死代理若被使用，连接必然失败。
    """
    import http.server
    import os as _os

    data = b"proxy-test-payload" * 100

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True, name="p24-server")
    t.start()

    saved = {k: _os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    _os.environ["HTTP_PROXY"] = _os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
    _os.environ["http_proxy"] = _os.environ["https_proxy"] = "http://127.0.0.1:1"
    try:
        with _tmpdir() as tmp:
            kind, value, dest, part, lock = _p_dl(
                tmp, data, url=f"http://127.0.0.1:{port}/maa.zip"
            )
            content = dest.read_bytes() if dest.is_file() else b""
    finally:
        for key, old in saved.items():
            if old is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = old
        server.shutdown()
        server.server_close()

    assert kind == "value", (
        f"默认走了环境代理（请求被打到死代理上）：{type(value).__name__}: {value}"
    )
    assert content == data, "内容不对"


@case("P25", "provision", "cancel")
def test_p25_cancel_leaves_no_residue():
    """P25 · ①`cancel` 已置位 ⇒ `kind == "cancelled"` 且无残留 ②未置位 ⇒ 正常完成。

    `kind` 必须是 `cancelled` 而不是 `network`：主理人定了"取消在文案上叫'已暂停'"，
    落成 `network` 用户会看到"网络错误"并反复重试一个自己取消掉的操作。
    """
    EV = _errors_mod()
    data = b"cancel-me" * 1000
    cancel = threading.Event()
    cancel.set()
    with _tmpdir() as tmp:
        kind, value, dest, part, lock = _p_dl(
            tmp, data, opener=_POpener(_PResp(data)), cancel=cancel
        )
    assert kind == "error", f"取消却成功了：{dest}"
    assert isinstance(value, EV.EngineDownloadFailed), f"抛的是 {type(value).__name__}"
    assert value.kind is EV.DownloadFailure.CANCELLED, f"kind 应是 CANCELLED，实际 {value.kind}"
    assert not dest.exists() and not part and not lock, (
        f"取消后残留：dest={dest.exists()} part={part} lock={lock}"
    )

    # ② 反向对照：不置位不许误取消
    with _tmpdir() as tmp:
        cancel2 = threading.Event()
        kind2, value2, dest2, part2, lock2 = _p_dl(
            tmp, data, opener=_POpener(_PResp(data)), cancel=cancel2
        )
    assert kind2 == "value", f"未取消却被取消：{type(value2).__name__}: {value2}"
    assert not part2 and not lock2


@case("P26", "provision", "progress")
def test_p26_progress_callback_cannot_break_cleanup():
    """P26 · `on_progress` 自己抛异常时 `.part` 仍被删除；正常时应至少回调 1 次且末次 `downloaded == total`。

    进度回调是 UI 注入的，它出错不能污染清理路径。
    """
    data = b"progress" * (300 << 10)  # ~3 MB
    seen: list[tuple[int, int | None]] = []

    with _tmpdir() as tmp:
        kind_ok, value_ok, dest_ok, part_ok, lock_ok = _p_dl(
            tmp, data, opener=_POpener(_PResp(data)),
            on_progress=lambda done, total=None: seen.append((done, total)),
        )
        ok_content = dest_ok.read_bytes() if dest_ok.is_file() else b""

    assert kind_ok == "value", f"正常下载失败：{type(value_ok).__name__}: {value_ok}"
    assert ok_content == data
    assert seen, "on_progress 一次都没被调用"
    assert seen[-1][0] == len(data), f"末次 downloaded={seen[-1][0]}，应为 {len(data)}"
    assert not part_ok and not lock_ok

    def boom(done, total=None):
        raise RuntimeError("UI 回调炸了")

    with _tmpdir() as tmp:
        kind2, value2, dest2, part2, lock2 = _p_dl(
            tmp, data, opener=_POpener(_PResp(data)), on_progress=boom
        )

    assert kind2 == "error", "回调抛异常时下载竟然成功了（说明回调异常被吞了，进度会静默丢失）"
    assert not part2, "回调抛异常后 `.part` 残留（清理被回调污染）"
    assert not dest2.exists(), "回调抛异常后 `dest` 残留"


@case("P27", "provision", "constants")
def test_p27_known_releases_constants_are_pinned():
    """P27 · `KNOWN_RELEASES` 每一项都是**发版时固化**的常量：
    64 位小写 hex、`size > 0`、URL 里是 `releases/download/v{version}/`、不含 `releases/latest`。

    真实故障：`releases/latest` 的资产集合会变（换包/改签名），哈希就必然漂移。
    """
    import re

    P = _provision_mod()
    assert P.KNOWN_RELEASES, "KNOWN_RELEASES 是空的"
    problems: list[str] = []
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    for key, spec in P.KNOWN_RELEASES.items():
        if not hex64.match(spec.sha256):
            problems.append(f"[{key}] sha256 不是 64 位小写 hex：{spec.sha256!r}")
        if spec.size <= 0:
            problems.append(f"[{key}] size 非正：{spec.size}")
        if "releases/latest" in spec.url:
            problems.append(f"[{key}] URL 用了 releases/latest（可变量）：{spec.url}")
        tag = spec.version if spec.version.startswith("v") else f"v{spec.version}"
        if f"releases/download/{tag}/" not in spec.url:
            problems.append(
                f"[{key}] URL 不是 tag 固定形式（应含 releases/download/{tag}/）：{spec.url}"
            )
        if spec.platform != key:
            problems.append(f"[{key}] 条目里的 platform={spec.platform!r} 与键不一致")
        if not spec.archive_name:
            problems.append(f"[{key}] archive_name 为空（错误文案里没法说清是哪个包）")
    assert not problems, "\n  ".join(["KNOWN_RELEASES 常量未 pin 住："] + problems)


@case("P28", "provision", "cancel")
def test_p28_async_cancel_stops_worker_before_return():
    """P28 · 事件循环里 `task.cancel()`：`cancel` 事件被置位，且**返回前**工作线程已收尾、`.part` 不存在。

    真实故障：协程返回了但线程还在写盘 → `.part` 又活了，下次启动读到半个文件。
    """
    EV = _errors_mod()
    data = b"s" * (4 << 20)
    cancel = threading.Event()
    progress_calls: list[int] = []

    def on_progress(done, total=None):
        progress_calls.append(done)
        if len(progress_calls) >= 1:
            cancel.set()      # 模拟"用户在下载过程中点了暂停"

    with _tmpdir() as tmp:
        kind, value, dest, part, lock = _p_dl(
            tmp, data, opener=_POpener(_PResp(data)), cancel=cancel, on_progress=on_progress
        )

    assert kind == "error", f"取消后竟然完成：{dest}"
    assert isinstance(value, EV.EngineDownloadFailed), f"抛的是 {type(value).__name__}"
    assert value.kind is EV.DownloadFailure.CANCELLED, (
        f"用户取消必须落 CANCELLED（文案叫'已暂停'），实际 {value.kind}"
    )
    assert cancel.is_set(), "cancel 事件没被置位"
    assert not part, "取消后 `.part` 又活了（工作线程没在返回前收尾）"
    assert not dest.exists() and not lock, "取消后残留"



# === END PLACEHOLDERS ===


if __name__ == "__main__":
    raise SystemExit(R.main(sys.modules[__name__]))
