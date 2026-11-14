"""假 MaaCore：不需要 C 编译器、不需要 MaaCore.dll、不需要真机。

三个刻意的设计（都是上一版踩过的坑）
------------------------------------
1. `AsstGetImage` 必须用 `ctypes.memmove` **真的写进** wrapper 传进来的那块缓冲。
   如果只返回一个长度、不写字节，那么 wrapper 里 `bytes(buf[:got])` 的
   越界 / 负长度 / 截断问题**永远测不出来**。
2. 每个符号是普通 Python 对象 `_FakeFunc`，可以自由写 `.argtypes` / `.restype`。
   `ctypes.CFUNCTYPE` 的实例**不允许**写这两个属性 —— 用它就测不了
   "wrapper 有没有声明正确的类型"（W14/W22 全靠这个）。
3. 每个调用都进 `call_log`。W21（`AsstSetUserDir` 必须先于 `AsstLoadResource`）
   和 W26（写任务被拒时 `AsstAppendTask` 调用数必须为 0）都只认这个日志。
"""

from __future__ import annotations

import ctypes
import struct
import zlib

NULL_SIZE_DEFAULT = (1 << 64) - 1


# --------------------------------------------------------------------------- #
# 造样本
# --------------------------------------------------------------------------- #
def make_png(width: int = 8, height: int = 6, rgba: tuple[int, int, int, int] = (0, 0, 0, 255)) -> bytes:
    """造一张**真 PNG**（签名 + IHDR + IDAT + IEND）。W04 靠它验证 `0x89` 不被当负值。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# 往 wrapper 的缓冲里写字节
# --------------------------------------------------------------------------- #
def _pointer_target(buf):
    """把 wrapper 传进来的各种缓冲形态统一成"能 memmove 进去的目标"。"""
    if isinstance(buf, ctypes.c_void_p):
        if not buf.value:
            raise RuntimeError("fake 收到空指针：wrapper 没分配缓冲就直接传了 NULL")
        return buf.value, "memmove"
    if hasattr(buf, "_obj"):  # ctypes.byref(...) / ctypes.pointer(...)
        return _pointer_target(buf._obj)
    if isinstance(buf, (bytearray, memoryview)):
        return buf, "slice"
    if isinstance(buf, bytes):
        raise RuntimeError("wrapper 传了不可写的 bytes 当输出缓冲")
    return buf, "memmove"  # create_string_buffer / (c_char*n)()


def write_into(buf, payload: bytes) -> None:
    if not payload:
        return
    target, kind = _pointer_target(buf)
    if kind == "memmove":
        ctypes.memmove(target, payload, len(payload))
    else:
        target[: len(payload)] = payload


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    if isinstance(value, ctypes.Array):
        return bytes(value).split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
    if isinstance(value, str):
        return value
    return repr(value)


# --------------------------------------------------------------------------- #
# 符号替身
# --------------------------------------------------------------------------- #
class _FakeFunc:
    """模拟 CDLL 里的函数对象：可写 `argtypes` / `restype`，调用透传到 Python 实现。"""

    def __init__(self, name: str, impl):
        self.name = name
        self._impl = impl
        self.argtypes = None
        self.restype = None
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __repr__(self) -> str:  # pragma: no cover - 只为报错可读
        return f"<_FakeFunc {self.name} calls={len(self.calls)}>"


# `get_image_plan` 里可用的取值
#   **硬规矩：每一条返回值都必须对应一个已核实的 [FACT]；没有出处的不许造。**
#   上一轮的教训：`"grow"` 曾经返回 `buff_size + 1`（"超长长度"），而真实 MAA 在装不下时
#   返回的是 **NullSize 哨兵**（F5）。假库一撒谎，测试就会稳定地替 bug 背书 ——
#   绿得很漂亮，但咬的是自己想象出来的库。
#   "auto"     → 装得下返 `len(payload)`，装不下返哨兵（= 真实 MAA 的行为）
#   "grow"     → 同 "auto"（历史名字；F5 语义：装不下走哨兵，逼 wrapper 倍增）
#   "oversize" → 返 `buff_size + 1`（**故意不守规矩**的库：W20 专用）
#   "exact"    → 返 `buff_size`（塞满但合法，W19 用）
#   "zero"     → 返 0（W10）
#   "null"     → 返 null_size 哨兵（W05/W08/W09）
#   int        → 直接返回这个数（W06 用）


class FakeMaaCore:
    """替身 DLL。属性名就是 C 符号名，直接当 `dll_loader` 的返回值交给 wrapper。"""

    def __init__(
        self,
        *,
        payload: bytes | None = None,
        bgr_payload: bytes | None = None,
        get_image_plan: list | None = None,
        null_size: int = NULL_SIZE_DEFAULT,
        create_returns: int = 1,
        load_resource_returns: int = 1,
        connect_returns: int = 1,
        screencap_returns: int = 1,
        connected_returns: int = 1,
        start_returns: int = 1,
        stop_returns: int = 1,
        append_task_returns: int = 1,
        instance_option_returns: int = 1,
        destroy_delay: float = 0.0,
        version: bytes = b"v0.0.0-fake",
    ) -> None:
        self.payload = make_png() if payload is None else payload
        self.bgr_payload = bgr_payload if bgr_payload is not None else b"\x10\x20\x30" * 16
        self.get_image_plan = list(get_image_plan or [])
        self.null_size = null_size

        self.create_returns = create_returns
        self.load_resource_returns = load_resource_returns
        self.connect_returns = connect_returns
        self.screencap_returns = screencap_returns
        self.connected_returns = connected_returns
        self.start_returns = start_returns
        self.stop_returns = stop_returns
        self.append_task_returns = append_task_returns
        self.instance_option_returns = instance_option_returns
        # 模拟"AsstDestroy 会 join 三条内部线程、可能长时间阻塞"（R20 用）
        self.destroy_delay = float(destroy_delay)
        self.version = version

        self.call_log: list[str] = []
        self.loaded_resource_parents: list[str] = []
        self.user_dirs: list[str] = []
        self.appended_tasks: list[str] = []
        self.destroyed_handles: list = []
        self.connection_extras: list[tuple[str, str]] = []
        self.connect_configs: list[str] = []
        # ``(key, value)``：key 是 C 枚举的 int，value 是字符串（不是 bool）
        self.instance_options: list[tuple[int, str]] = []
        self.instance_option_handles: list = []
        # 每次 AsstGetImage 的 buff_size（W29 用它断言"最大分配"与"倍增梯度"）
        self.image_buff_sizes: list[int] = []
        self.detach_calls = 0
        self._img_calls = 0

        self._fns: dict[str, _FakeFunc] = {}
        self._install()

    # -- 内部 -------------------------------------------------------------- #
    def _log(self, name: str) -> None:
        self.call_log.append(name)

    def _next_image_return(self, buff_size: int):
        plan = self.get_image_plan
        idx = self._img_calls
        self._img_calls += 1
        item = plan[min(idx, len(plan) - 1)] if plan else "auto"
        if item in ("auto", "grow"):
            # [FACT] F5：真实 MAA 在 `data_size > buff_size` 时返回 **NullSize 哨兵**，
            # 不会返回一个"比缓冲还大"的长度。所以"装不下"必须走哨兵这条路，
            # 否则我们测的就不是 MAA，而是我们自己想象出来的库。
            if len(self.payload) > int(buff_size):
                return self.null_size
            return len(self.payload)
        if item == "oversize":
            # 故意不守规矩的库（返回长度 > 缓冲）。真实 MAA 不会这样，
            # 这条分支只服务 W20："万一库越界，wrapper 必须拒绝"。
            return int(buff_size) + 1
        if item == "exact":
            return buff_size
        if item == "zero":
            return 0
        if item == "null":
            return self.null_size
        if isinstance(item, int):
            return item
        raise ValueError(f"未知的 get_image_plan 取值: {item!r}")

    def _get_image(self, handle, buff, buff_size, *, bgr: bool):
        self._log("AsstGetImageBgr" if bgr else "AsstGetImage")
        self.image_buff_sizes.append(int(buff_size))
        ret = self._next_image_return(int(buff_size))
        if ret in (0, self.null_size):
            return ret
        if ret > int(buff_size):
            # 真实库越界时并不会替我们检查；这里**故意不写**，
            # 逼 wrapper 自己拒掉"返回长度 > 缓冲"的情况（W20）。
            return ret
        payload = self.bgr_payload if bgr else self.payload
        write_into(buff, payload[:ret])
        return ret

    def _install(self) -> None:
        def fn(name, impl):
            self._fns[name] = _FakeFunc(name, impl)
            setattr(self, name, self._fns[name])

        fn("AsstGetVersion", lambda: self.version)
        fn("AsstGetNullSize", lambda: self.null_size)

        fn("AsstSetUserDir", self._asst_set_user_dir)
        fn("AsstLoadResource", self._asst_load_resource)

        fn("AsstCreate", self._asst_create)
        fn("AsstCreateEx", lambda cb, arg: self._asst_create())
        fn("AsstDestroy", self._asst_destroy)

        fn("AsstGetImage", lambda h, buff, size: self._get_image(h, buff, size, bgr=False))
        fn("AsstGetImageBgr", lambda h, buff, size: self._get_image(h, buff, size, bgr=True))

        fn("AsstAsyncConnect", self._asst_async_connect)
        fn("AsstAsyncScreencap", self._asst_async_screencap)
        fn("AsstConnected", lambda h: self.connected_returns)
        # ``AsstStop`` 必须进 ``call_log``：R20 要断言"Stop 先于 Destroy"这条
        # **顺序**契约，而顺序只能从调用日志读到。以前用裸 lambda（不记日志），
        # 于是"Stop 到底有没有在 Destroy 之前被调用"在 fake 层面根本无法观测 ——
        # 一条安全断言被静默降级成"读代码的人相信它是这样"。
        fn("AsstStop", self._asst_stop)
        fn("AsstAppendTask", self._asst_append_task)
        fn("AsstStart", lambda h: self.start_returns)
        fn("AsstSetConnectionExtras", self._asst_set_connection_extras)
        fn("AsstSetInstanceOption", self._asst_set_instance_option)
        fn("AsstLog", lambda level, msg: self._log("AsstLog"))

    def _asst_set_user_dir(self, path):
        self._log("AsstSetUserDir")
        self.user_dirs.append(_as_text(path))
        return 1

    def _asst_load_resource(self, path):
        self._log("AsstLoadResource")
        self.loaded_resource_parents.append(_as_text(path))
        return self.load_resource_returns

    def _asst_create(self):
        self._log("AsstCreateEx")
        return self.create_returns

    def _asst_stop(self, handle):
        """``AsstStop``：**必须记日志**（R20 的顺序断言只认 ``call_log``）。"""
        self._log("AsstStop")
        return self.stop_returns

    def _asst_destroy(self, handle):
        self._log("AsstDestroy")
        self.destroyed_handles.append(handle)
        if self.destroy_delay > 0:
            import time as _time
            _time.sleep(self.destroy_delay)
        return None

    def _asst_set_instance_option(self, handle, key, value):
        self._log("AsstSetInstanceOption")
        self.instance_options.append((int(key), _as_text(value)))
        self.instance_option_handles.append(handle)
        return self.instance_option_returns

    def _asst_async_connect(self, handle, adb_path, address, config, block):
        self._log("AsstAsyncConnect")
        self.connect_configs.append(_as_text(config))
        if block not in (0, None):
            # 真实契约要求 block=False；记下来供用例断言
            self.call_log.append(f"BLOCK={block}")
        return self.connect_returns

    def _asst_async_screencap(self, handle, block):
        self._log("AsstAsyncScreencap")
        if block not in (0, None):
            self.call_log.append(f"BLOCK={block}")
        return self.screencap_returns

    def _asst_append_task(self, handle, task_type, params):
        self._log("AsstAppendTask")
        self.appended_tasks.append(_as_text(task_type))
        return self.append_task_returns

    def _asst_set_connection_extras(self, name, value):
        self._log("AsstSetConnectionExtras")
        self.connection_extras.append((_as_text(name), _as_text(value)))
        return 1

    # -- 观测面 ------------------------------------------------------------ #
    def f(self, symbol: str) -> _FakeFunc:
        return self._fns[symbol]

    def calls_of(self, symbol: str) -> int:
        return self._fns[symbol].call_count

    def index_of(self, symbol: str) -> int:
        try:
            return self.call_log.index(symbol)
        except ValueError:
            return -1

    def as_loader(self):
        """塞给 `MaaCoreWrapper(dll_loader=...)`：不关心路径，一律返回自己。"""

        def _load(_path: str):
            return self

        return _load


class MissingSymbolsFake(FakeMaaCore):
    """可以"缺符号"的假库：访问被摘掉的符号时抛 `AttributeError`（等价于老版本库）。"""

    def __init__(self, missing: set[str] | frozenset[str], **kw) -> None:
        self._missing = set(missing)
        super().__init__(**kw)

    def __getattr__(self, name):  # pragma: no cover - 只有缺符号路径会走到
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(f"{type(self).__name__} 没有符号 {name!r}（模拟老版本库）")

    def _install(self) -> None:
        super()._install()
        for name in self._missing:
            self._fns.pop(name, None)
            object.__setattr__(self, name, None)
            try:
                delattr(type(self), name)
            except AttributeError:
                pass
            # delattr 在实例上：_install 用 setattr 放在实例 __dict__ 里
            self.__dict__.pop(name, None)
