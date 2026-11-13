"""``MaaCore`` 的纯 ctypes 绑定层。

一个 C 函数对应一个方法，返回**原始值**。本类没有任何策略：不重试、不超时、
不熔断、不打文案、不记业务结论、不抛业务异常。唯一的"if"只允许出现在三处
（都属于机械解码）：

  ① 平台 → 库名/加载器映射；
  ② 返回值 == ``NULL_SIZE`` / ``0`` → 抛 :class:`MaaCallError`；
  ③ 缓冲区倍增（MAA 不告诉我们图片需要多大，只能机械倍增，上限来自构造参数）。

``MaaCallError`` 是本模块唯一的自定义异常，**不允许越过 adapter**：
adapter 负责把它翻译成 ``errors.py`` 里的业务异常。

几条写死在实现里的源码事实（改动前请先复核 ``include/AsstCaller.h``）：

* ``AsstBool`` 是 ``uint8_t``，不是 C 的 ``bool`` ⇒ argtypes/restype 用
  ``ctypes.c_ubyte``。用 ``c_bool`` 读 ``0x80`` 会抛 ``ValueError``；
* ``AsstSize`` 是 ``uint64``，``NullSize = (AsstSize)-1``，到 Python 必然是
  ``2**64 - 1``，**不能拿 -1 去比**；
* ``AsstGetImage`` 对「缓冲不足 / 句柄为空 / 未 inited / buff 为空」统一返回
  ``NullSize``，对 ``data_size == 0`` 返回 ``0``；本模块**不做区分猜测***；
* ``AsstLoadResource`` 内部拼 ``os_path / "resource"``，所以参数必须是
  **包含 resource 的那一层**（见 ``load_resource`` 的 ``parent_dir`` 命名）。
"""

from __future__ import annotations

import ctypes
import enum
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

__all__ = [
    "AsstHandle",
    "AsstMsgId",
    "AsstSize",
    "AsyncCallId",
    "AsstBool",
    "NULL_SIZE",
    "INVALID_ID",
    "PNG_SIGNATURE",
    "MaaPlatform",
    "MaaCallStatus",
    "MaaCallError",
    "MaaCoreWrapper",
    "RawCallback",
    "DllLoader",
]

AsstHandle = int          # c_void_p 转成的整数；0 表示空句柄
AsstMsgId = int
AsstSize = int            # c_uint64
AsyncCallId = int         # c_int32
AsstBool = int            # [FACT] include/AsstCaller.h: typedef uint8_t AsstBool

NULL_SIZE: Final[int] = (1 << 64) - 1
"""[FACT] AsstCaller.cpp: ``NullSize = static_cast<AsstSize>(-1)``。"""

INVALID_ID: Final[int] = 0
"""[FACT] AsstCaller.cpp: ``InvalidId = 0``。"""

PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
"""``AsstGetImage`` 的输出应当是 PNG（`[FACT]` 内部走 ``cv::imencode(".png")``）。

本模块只用它做常量的唯一定义处；魔数校验属于 adapter（wrapper 只给字节）。
"""

# 两条图片缓冲上限，缺一不可（架构 §3.2 get_image）：
#   只有字节上限时，初始 buffer 很小时会先撞次数上限；
#   只有次数上限时，一个畸形大图能让单次分配吃满内存。
DEFAULT_MAX_IMAGE_BUFFER_BYTES: Final[int] = 64 * 1024 * 1024
"""单次图片缓冲分配的硬上限（64 MiB）。"""

DEFAULT_MAX_BUFFER_GROW: Final[int] = 2
"""缓冲区最多倍增几次（默认 2 次，即最多调用 C 侧 1 + 2 次）。"""

DEFAULT_IMAGE_BUFFER_BYTES: Final[int] = 1 << 20
"""调用方没给 ``buffer_size`` 时的初始缓冲（1 MiB）。

**为什么初始是 1 MiB 而不是更小**：``AsstGetImage`` 出的是 PNG，而 PNG 最坏情况
（不可压缩的噪声图）可达 RGBA 尺寸量级，64 KiB 起步几乎必然要倍增两次以上，
恰好撞上"次数上限"这条保险丝。1 MiB 只是**起点**，不是估计值——真实大小 MAA
不提供任何查询接口，只能靠倍增兜底，硬上限 64 MiB 才是真正的安全边界。
"""


class MaaPlatform(enum.Enum):
    WINDOWS = "windows"
    DARWIN = "darwin"
    LINUX = "linux"


class MaaCallStatus(enum.Enum):
    """C 返回值的机械解码结果。只描述"返回了什么"，不描述"为什么"。

    ``[FACT]`` ``AsstGetImage`` 对「句柄为空 / 未 inited / buff 为空 / 缓冲不足」
    四种情况**统一返回 NullSize**，调用方无法区分。本枚举因此**故意**只有一个
    ``NULL_SIZE``，不做四选一的猜测（区分三义是 adapter/service 的文案问题，
    而它们只能按"缓冲不足或句柄问题"这一档说话）。
    """

    NULL_SIZE = "null_size"
    EMPTY = "empty"
    INVALID_ID = "invalid_id"
    REJECTED = "rejected"


class MaaCallError(RuntimeError):
    """wrapper 唯一的自定义异常。**不允许越过 adapter。**

    Args:
        api: 出问题的 C 函数名（如 ``"AsstGetImage"``），用于日志对账。
        status: 机械解码结果，见 :class:`MaaCallStatus`。
        detail: 结构化诊断（buffer_size / got / address…）。只放事实，不放文案。
    """

    def __init__(
        self,
        api: str,
        status: MaaCallStatus,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.api = api
        self.status = status
        self.detail: dict[str, Any] = dict(detail or {})
        text = f"{api} -> {status.value}"
        if self.detail:
            text = f"{text} ({self.detail})"
        super().__init__(text)


RawCallback = Callable[[int, str | None, int | None], None]
"""C 回调的 Python 形态：``(msg_id, details_json_or_None, custom_arg_as_int)``。

**只做 str 解码，不做 json.loads**（解析属于 adapter）。
"""

DllLoader = Callable[[str], Any]
"""``(abs_path: str) -> 库对象``。签名对齐 ``ctypes.CDLL``，便于直接传 ``ctypes.WinDLL``。"""


# ``typedef void(ASST_CALL* AsstApiCallback)(AsstMsgId msg, const char* details_json, void* custom_arg);``
_CALLBACK_TYPE = ctypes.CFUNCTYPE(
    None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p
)

_CALLBACK_KEEPALIVE_WARN_AT: Final[int] = 100

_KEEPALIVE_WARNED = False

_CALLBACK_KEEPALIVE: list[Any] = []
"""CFUNCTYPE 实例的模块级强引用名单，**永不注销**。

``[FACT]`` C 侧在 ``Assistant`` 生命周期内持有该函数指针；一旦 Python 侧
CFUNCTYPE 被 GC，调用即 use-after-free（进程崩溃，无法用 try/except 接住）。
GC 时机不可预测，所以这里不设"安全注销点"——一个 CFUNCTYPE 只有几十字节。

名单**只增不减**：超过 100 条时告警一次（提示有泄漏），但绝不截断——
截断等于重新引入 UAF（`Destroyed` 回调本身还要用到它）。
"""


def _encode_path(value: str | bytes | Path) -> bytes:
    """``c_char_p`` 的入参编码。MAA 侧按 UTF-8 解析路径。"""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


class MaaCoreWrapper:
    """MaaCore 的 Python 侧镜像：一个 C 函数对应一个方法，返回原始值。

    本类没有任何策略：不重试、不超时、不熔断、不打文案、不记业务结论。

    Args:
        library_path: 核心库路径。会先转成**绝对路径**再交给加载器
            （`[FACT]` CPython 3.8+ 只有绝对路径才启用 ``DLL_LOAD_DIR``，
            裸名不会让"被加载 DLL 自身目录"参与依赖解析）。
        max_image_buffer_bytes: 单次图片缓冲分配的字节上限。
        max_buffer_grow: 缓冲区最多倍增次数（= ``get_image`` 最多多试几次）。
        preferred_loader: 加载器偏好，``"windll"`` / ``"cdll"``（大小写不敏感）；
            ``None`` 时按平台默认（Windows → ``WinDLL``，其余 → ``CDLL``）。
        dll_loader: **测试接缝**。默认 ``None`` 时用 ``ctypes.WinDLL``/``ctypes.CDLL``，
            传入则调用它拿库对象。存在的唯一理由：让 W 组用例能在没有真
            ``MaaCore.dll`` 的机器上跑。它不是"策略注入"——只被调用一次（``load()`` 内），
            之后没有任何分支依赖它的身份。生产代码永远不传。
    """

    def __init__(
        self,
        *,
        library_path: Path,
        max_image_buffer_bytes: int = DEFAULT_MAX_IMAGE_BUFFER_BYTES,
        max_buffer_grow: int = DEFAULT_MAX_BUFFER_GROW,
        preferred_loader: str | None = None,
        dll_loader: "DllLoader | None" = None,
    ) -> None:
        if max_image_buffer_bytes <= 0:
            raise ValueError("max_image_buffer_bytes must be positive")
        if max_buffer_grow < 0:
            raise ValueError("max_buffer_grow must be >= 0")
        self._library_path = Path(library_path)
        self._max_image_buffer_bytes = int(max_image_buffer_bytes)
        self._max_buffer_grow = int(max_buffer_grow)
        self._preferred_loader = preferred_loader
        self._dll_loader = dll_loader

        self._lib: Any | None = None
        self._dll_dir_handle: Any | None = None
        self._callback: Any | None = None
        self._callback_target: RawCallback | None = None
        # 观测字段：``AsstSetConnectionExtras`` 是 void 且解析失败静默 return，
        # 这里只保留"被调用过几次"这个事实，供上层诊断（不解析 JSON）。
        self.connection_extras_calls = 0

    # --- 平台与库文件（静态，无 IO） ---
    @staticmethod
    def platform() -> MaaPlatform:
        """当前平台。只有三种取值；未知平台按 POSIX 处理（Linux）。"""
        if sys.platform == "win32":
            return MaaPlatform.WINDOWS
        if sys.platform == "darwin":
            return MaaPlatform.DARWIN
        return MaaPlatform.LINUX

    @staticmethod
    def default_library_names() -> tuple[str, ...]:
        """三平台的默认库名（顺序固定，便于测试逐项断言）。"""
        return ("MaaCore.dll", "libMaaCore.dylib", "libMaaCore.so")

    @staticmethod
    def candidate_library_paths(root: Path) -> tuple[Path, ...]:
        """``root/``、``root/bin/``、``root/lib/`` 三处 —— 不同发行包布局不同。"""
        base = Path(root)
        dirs = (base, base / "bin", base / "lib")
        return tuple(directory / name for directory in dirs
                     for name in MaaCoreWrapper.default_library_names())

    # --- 装载 ---
    def load(self, *, dll_directory: Path | None = None) -> None:
        """dlopen + 绑定全部 argtypes/restype。已加载时幂等返回。

        ``dll_directory``：Windows 下传入**核心库所在目录**。本方法会在其内部
        ``os.add_dll_directory(str(dll_directory))`` 并把返回句柄保存在实例上
        （``self._dll_dir_handle``），直到 :meth:`close`。未传时回落到核心库的父目录。

        ``[FACT]`` 四条决定实现方式的事实（旧实现"临时改 PATH"在 3.8+ 上**无效**）：

          ① Python 3.8 起 Windows 上 ctypes 加载 DLL 不再搜索 ``PATH`` 与 CWD，
             只在系统路径、DLL 自身目录、以及 ``add_dll_directory`` 注册的目录里找依赖；
          ② ``add_dll_directory()`` 的句柄一旦被 GC 或 ``close()``，该目录**立即**失效
             —— 所以句柄必须由长生命周期对象持有；
          ③ ``CDLL`` 必须传**绝对路径**才会启用 ``DLL_LOAD_DIR``；
          ④ **不得传 ``winmode=0``**（会退回会搜 CWD 的不安全历史搜索顺序）。

        Raises:
            OSError: dlopen 失败（``winerror`` 126=缺依赖/缺 VC++ 运行库；193=架构不符）
            AttributeError: 符号缺失（由 runtime 捕获并归因到 ``symbol``）
            TypeError: 签名不符（argtypes/restype 与库对不上）
        """
        if self._lib is not None:
            return

        absolute = Path(os.path.abspath(str(self._library_path)))
        try:
            if MaaCoreWrapper.platform() is MaaPlatform.WINDOWS:
                directory = Path(dll_directory) if dll_directory is not None else absolute.parent
                directory = Path(os.path.abspath(str(directory)))
                # 目录不存在时不在此时炸：交给 dlopen 报它自己的 OSError（错误更准）。
                if directory.is_dir():
                    self._dll_dir_handle = os.add_dll_directory(str(directory))
            self._lib = self._resolve_loader()(str(absolute))
            self._bind()
        except BaseException:
            # 失败路径也必须释放 dll_directory 句柄（W24：不许泄漏）。
            self._lib = None
            self._release_dll_dir_handle()
            raise

    def close(self) -> None:
        """仅解除 Python 侧引用并释放 ``_dll_dir_handle``。幂等。

        动态库无法真正卸载，不做释放承诺。

        顺序必须是「先放 CDLL 引用，再关 dll_directory 句柄」——反过来会让
        **延迟加载**的依赖（MaaCore 在 ``AsstLoadResource`` 时才 dlopen 的东西）找不到。

        CFUNCTYPE 不在这里释放：见 ``_CALLBACK_KEEPALIVE``（UAF 防护）。
        """
        self._lib = None
        self._callback = None
        self._release_dll_dir_handle()

    def _release_dll_dir_handle(self) -> None:
        handle, self._dll_dir_handle = self._dll_dir_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # 已关闭/进程退出中：没有可做的补救
                logger.debug("add_dll_directory handle already closed", exc_info=True)

    def _resolve_loader(self) -> DllLoader:
        if self._dll_loader is not None:
            return self._dll_loader
        preference = (self._preferred_loader or "").strip().lower()
        win_dll = getattr(ctypes, "WinDLL", None)
        if preference in {"windll", "win32", "stdcall"} and win_dll is not None:
            return win_dll
        if preference in {"cdll", "cdecl"}:
            return ctypes.CDLL
        if MaaCoreWrapper.platform() is MaaPlatform.WINDOWS and win_dll is not None:
            return win_dll
        return ctypes.CDLL

    def _bind(self) -> None:
        """绑定全部 C 函数签名。符号缺失 → ``AttributeError``，签名不符 → ``TypeError``。

        ``AsstBool`` 一律 ``c_ubyte``（`[FACT]` ``typedef uint8_t AsstBool``）；
        ``AsstSize`` 一律 ``c_uint64``（不然 64 位返回值会被截断成 32 位，哨兵判定全错）。
        """
        lib = self._require_lib()

        bindings: tuple[tuple[str, Any, tuple[Any, ...] | None, bool], ...] = (
            # name, restype, argtypes, required
            ("AsstSetUserDir", ctypes.c_ubyte, (ctypes.c_char_p,), True),
            ("AsstLoadResource", ctypes.c_ubyte, (ctypes.c_char_p,), True),
            ("AsstCreateEx", ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_void_p), True),
            ("AsstDestroy", None, (ctypes.c_void_p,), True),
            ("AsstAsyncConnect", ctypes.c_int32,
             (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
              ctypes.c_ubyte), True),
            ("AsstAsyncScreencap", ctypes.c_int32, (ctypes.c_void_p, ctypes.c_ubyte), True),
            ("AsstGetImage", ctypes.c_uint64,
             (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64), True),
            ("AsstGetImageBgr", ctypes.c_uint64,
             (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64), True),
            ("AsstGetVersion", ctypes.c_char_p, (), True),
            ("AsstConnected", ctypes.c_ubyte, (ctypes.c_void_p,), True),
            ("AsstStop", ctypes.c_ubyte, (ctypes.c_void_p,), True),
            ("AsstAppendTask", ctypes.c_int32,
             (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p), True),
            ("AsstStart", ctypes.c_ubyte, (ctypes.c_void_p,), True),
            # 可选符号：老版本 MaaCore 没有它们，缺了要能继续跑。
            ("AsstGetNullSize", ctypes.c_uint64, (), False),
            ("AsstSetConnectionExtras", None, (ctypes.c_char_p, ctypes.c_char_p), False),
            ("AsstSetStaticOption", ctypes.c_ubyte, (ctypes.c_int32, ctypes.c_char_p), False),
        )
        for name, restype, argtypes, required in bindings:
            function = getattr(lib, name, None)
            if function is None:
                if required:
                    raise AttributeError(f"MaaCore is missing symbol: {name}")
                logger.debug("optional MaaCore symbol absent: %s", name)
                continue
            function.restype = restype
            if argtypes is not None:
                function.argtypes = argtypes

    @property
    def version(self) -> str:
        """``AsstGetVersion()``；任何失败（未加载/符号缺失/解码失败）返回 ``'unknown'``。"""
        lib = self._lib
        if lib is None:
            return "unknown"
        try:
            raw = lib.AsstGetVersion()
        except Exception:  # noqa: BLE001 - 版本号是纯诊断信息，永不因它中断装载
            logger.debug("AsstGetVersion failed", exc_info=True)
            return "unknown"
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        if raw is None:
            return "unknown"
        return str(raw)

    # --- 回调与实例 ---
    def set_callback(self, handler: RawCallback) -> None:
        """设置 C 回调的 Python 目标。

        CFUNCTYPE 实例**永久保持强引用**（模块级名单），只在首次调用时创建；
        重复调用只替换 Python 侧目标，不重建 CFUNCTYPE（重建会让 C 侧旧指针悬空，
        换回调的瞬间就 UAF）。必须在 :meth:`create_instance` 之前调用。
        """
        self._callback_target = handler
        if self._callback is None:
            callback = _CALLBACK_TYPE(self._trampoline)
            _CALLBACK_KEEPALIVE.append(callback)
            self._callback = callback
            self._warn_keepalive_once()

    @staticmethod
    def _warn_keepalive_once() -> None:
        """名单超长只告警一次（K3）：告警是提示，截断才是事故。"""
        global _KEEPALIVE_WARNED
        if len(_CALLBACK_KEEPALIVE) > _CALLBACK_KEEPALIVE_WARN_AT and not _KEEPALIVE_WARNED:
            _KEEPALIVE_WARNED = True
            logger.warning(
                "callback keepalive list has %d entries; CFUNCTYPE objects are never freed (UAF guard)",
                len(_CALLBACK_KEEPALIVE),
            )

    def _trampoline(
        self,
        msg: int,
        details_json: bytes | None,
        custom_arg: int | None,
    ) -> None:
        """C 线程入口。只做 str 解码与转发，**绝不让异常穿回 C 侧**。"""
        target = self._callback_target
        if target is None:
            return
        text = details_json.decode("utf-8", errors="replace") if isinstance(
            details_json, bytes) else details_json
        arg = int(custom_arg) if custom_arg is not None else None
        try:
            target(int(msg), text, arg)
        except Exception:  # noqa: BLE001 - C 线程里抛异常 = 未定义行为
            logger.debug("callback handler raised", exc_info=True)

    def create_instance(self) -> AsstHandle:
        """``AsstCreateEx(cb, NULL)`` → handle。

        返回 ``0`` 表示失败（`[FACT]` 只在 ``!inited()``，即资源未加载成功时）。
        本方法不把 ``0`` 翻译成异常：句柄是"事实"，要不要报错由 runtime 决定
        （它才知道 ``resource_loaded`` 这个因果字段）。
        """
        lib = self._require_lib()
        if self._callback is None:
            # 调用方忘了 set_callback：给一个空目标也比拿空指针去 create 安全。
            logger.debug("create_instance called before set_callback; installing no-op callback")
            self.set_callback(lambda _msg, _details, _arg: None)
        handle = lib.AsstCreateEx(self._callback, ctypes.c_void_p(0))
        return int(handle) if handle else 0

    def destroy_instance(self, handle: AsstHandle) -> None:
        """``AsstDestroy(handle)``。``0``/``None`` 直接返回（`[FACT]` C 侧同样短路）。

        `[FACT]` 实现是 ``delete handle``，析构函数会 join 三条内部线程，因此
        **可能长时间阻塞**——调用方必须在工作线程里跑并自带超时，本方法不提供超时。
        """
        if not handle:
            return
        self._require_lib().AsstDestroy(handle)

    # --- 进程级 ---
    def set_user_dir(self, path: Path) -> None:
        """``AsstSetUserDir``。返回 False 抛 ``MaaCallError(REJECTED)``。

        调用顺序由上层负责：`[FACT]` ``AsstLoadResource`` 会在 UserDir 为空时把它
        绑到资源父目录，所以 **``set_user_dir`` 必须先于 ``load_resource``**，
        否则 MAA 的日志/调试目录会写进资源目录。
        """
        ok = self._require_lib().AsstSetUserDir(_encode_path(path))
        if not ok:
            raise MaaCallError(
                "AsstSetUserDir", MaaCallStatus.REJECTED, detail={"path": str(path)}
            )

    def load_resource(self, parent_dir: Path) -> None:
        """``AsstLoadResource(parent_dir)``。

        参数名刻意叫 **parent_dir**，不是 ``resource_dir``。
        `[FACT]` ``src/MaaCore/AsstCaller.cpp`` 的 ``AsstLoadResource`` 内部写死了
        ``auto res_path = os_path / "resource"_p;``，传进来的必须是**包含 resource
        的那一层**。这个命名是防止第二个人再踩一次 ``resource/resource`` 的主要手段。

        Raises:
            MaaCallError(REJECTED): C 返回 False（目录对但资源内容坏/版本不匹配）。
                参数语义/路径是否存在由 runtime 负责判断，本层不做文件系统检查。
        """
        ok = self._require_lib().AsstLoadResource(_encode_path(parent_dir))
        if not ok:
            raise MaaCallError(
                "AsstLoadResource",
                MaaCallStatus.REJECTED,
                detail={"parent_dir": str(parent_dir)},
            )

    # --- 异步调用（只发号，不等结果） ---
    def async_connect(
        self,
        handle: AsstHandle,
        *,
        adb_path: str,
        address: str,
        config: str,
    ) -> AsyncCallId:
        """``AsstAsyncConnect(handle, adb_path, address, config, block=False)``。

        永远传 ``block=False``：``block=True`` 会在**当前线程**里同步等待
        （``Assistant::append_async_call`` → ``wait_async_id``），把调用线程变成
        可能永久阻塞的孤儿。返回 ``0``（InvalidId）抛 ``MaaCallError(INVALID_ID)``。
        """
        call_id = self._require_lib().AsstAsyncConnect(
            handle,
            _encode_path(adb_path),
            _encode_path(address),
            _encode_path(config),
            0,
        )
        if int(call_id) == INVALID_ID:
            raise MaaCallError(
                "AsstAsyncConnect",
                MaaCallStatus.INVALID_ID,
                detail={"address": address, "adb_path": adb_path},
            )
        return int(call_id)

    def async_screencap(self, handle: AsstHandle) -> AsyncCallId:
        """``AsstAsyncScreencap(handle, block=False)``。返回 ``0`` 抛 ``INVALID_ID``。"""
        call_id = self._require_lib().AsstAsyncScreencap(handle, 0)
        if int(call_id) == INVALID_ID:
            raise MaaCallError("AsstAsyncScreencap", MaaCallStatus.INVALID_ID)
        return int(call_id)

    # --- 取图 ---
    def get_image(
        self,
        handle: AsstHandle,
        *,
        buffer_size: int | None = None,
    ) -> bytes:
        """``AsstGetImage`` → **PNG 字节，原样返回**。

        绝不对返回值做任何图像编码/解码；本模块不 import cv2/PIL/numpy，
        从结构上杜绝"把 PNG 当裸像素二次编码"那个历史 bug。

        **缓冲倍增策略（两条上限，缺一不可）**：

        1. **字节上限** ``max_image_buffer_bytes`` —— 单次分配不得越过它；
        2. **次数上限** ``max_buffer_grow`` —— 最多倍增这么多次。

        只有字节上限时，初始 buffer 很小时会先撞次数上限；只有次数上限时，
        一个畸形大图能让单次分配吃满内存。**任一上限先撞到就停止倍增并抛错。**

        第三条硬规则：``buffer_size * 2 > max_image_buffer_bytes`` 时**直接抛错**，
        不再倍增 —— 此时"再试一次也一定失败"，而失败会被上层读成"缓冲不足"。
        旧实现把"我要不到这么大的缓冲"错报成"ADB 已断开"，让用户去查模拟器端口。

        判定顺序（刻意如此，不可调换）：

          ① ``got == null_size`` → 缓冲不足（**必须先判**，否则哨兵会被当图像数据）；
          ② ``got == 0`` → 未拿到画面（controller 未初始化/未连接）；
          ③ ``got > buffer_size`` → 库越界（拒绝，绝不把整个 buf 读出来）；
          ④ 其余（``0 < got <= buffer_size``）→ 成功，``got == buffer_size`` 也是成功。

        Raises:
            MaaCallError(REJECTED): 句柄为空（未 create / create 失败），**不调用 C**。
            MaaCallError(NULL_SIZE): 缓冲不足（倍增后仍不足）/ 库返回越界值。
            MaaCallError(EMPTY): C 返回 0（见架构 §3.2.4：等价于"未连接"）。
        """
        return self._read_image("AsstGetImage", handle, buffer_size=buffer_size)

    def get_image_bgr(
        self,
        handle: AsstHandle,
        *,
        buffer_size: int | None = None,
    ) -> bytes:
        """``AsstGetImageBgr`` → 裸 BGR 像素（`[FACT]` 该符号真实存在）。

        **不得作为 message part 上行**：它没有宽高信息，任何直接塞进 image part
        的代码都会在图上暴露出来。保留它的唯一用途是做"PNG / RGB 混淆"的对照物，
        以及本地落盘调试（两者字节不同，只有 PNG 那份能过魔数校验）。
        """
        return self._read_image("AsstGetImageBgr", handle, buffer_size=buffer_size)

    def _read_image(
        self,
        api: str,
        handle: AsstHandle,
        *,
        buffer_size: int | None,
    ) -> bytes:
        if not handle:
            # 空句柄是"调用被拒"，不是"缓冲不足"：绝不发 C 调用，也不让上游
            # 把两者混成一句"缓冲区不足"。
            raise MaaCallError(api, MaaCallStatus.REJECTED, detail={"handle": 0})

        lib = self._require_lib()
        function = getattr(lib, api)
        sentinel = self.null_size()

        size = DEFAULT_IMAGE_BUFFER_BYTES if buffer_size is None else int(buffer_size)
        grows = 0
        while True:
            if size <= 0:
                raise MaaCallError(
                    api, MaaCallStatus.NULL_SIZE,
                    detail={"buffer_size": size, "reason": "non_positive_buffer"},
                )
            if size > self._max_image_buffer_bytes:
                raise MaaCallError(
                    api, MaaCallStatus.NULL_SIZE,
                    detail={
                        "buffer_size": size,
                        "max_image_buffer_bytes": self._max_image_buffer_bytes,
                        "reason": "buffer_budget_exceeded",
                    },
                )

            buffer = (ctypes.c_ubyte * size)()
            got = int(function(handle, buffer, size))

            # ① 哨兵先判：C 侧返回 NullSize 时 buffer 里可能一个字节都没写，
            #    先判成功判定就会把一坨零字节当成图像交出去（旧实现的错法）。
            if got == sentinel:
                if grows >= self._max_buffer_grow:
                    raise MaaCallError(
                        api, MaaCallStatus.NULL_SIZE,
                        detail={
                            "buffer_size": size,
                            "grows": grows,
                            "max_buffer_grow": self._max_buffer_grow,
                            "reason": "grow_limit_reached",
                        },
                    )
                if size * 2 > self._max_image_buffer_bytes:
                    # 剩余预算不足：再试一次也一定失败，直接停。
                    raise MaaCallError(
                        api, MaaCallStatus.NULL_SIZE,
                        detail={
                            "buffer_size": size,
                            "max_image_buffer_bytes": self._max_image_buffer_bytes,
                            "reason": "no_budget_to_grow",
                        },
                    )
                size *= 2
                grows += 1
                continue

            # ② 返回值 0：data_size == 0，不是"成功但零字节"。
            if got == 0:
                raise MaaCallError(
                    api, MaaCallStatus.EMPTY, detail={"buffer_size": size}
                )

            # ③ 库违规越界：拒绝，绝不把整个 buffer 读出来当图像。
            if got > size:
                raise MaaCallError(
                    api, MaaCallStatus.NULL_SIZE,
                    detail={
                        "got": got,
                        "buffer_size": size,
                        "reason": "returned_more_than_buffer",
                    },
                )

            # ④ 成功（got == size 也是成功：塞满不是失败的信号）。
            return bytes(buffer[:got])

    # --- 状态与哨兵 ---
    def is_connected(self, handle: AsstHandle) -> bool:
        """``AsstConnected(handle)``。

        `[FACT]` ``Assistant::connected()`` 返回 ``inited()``，即 controller 存在且
        已初始化 —— 这是**不依赖回调事件**的权威健康探针。
        """
        if not handle:
            return False
        return bool(self._require_lib().AsstConnected(handle))

    def null_size(self) -> int:
        """``AsstGetNullSize()`` 的返回值；符号缺失时回落常量 ``NULL_SIZE``。

        两者按定义相等（``2**64 - 1``）。每次现取而不是缓存，是为了让"库的哨兵
        与本地常量不一致"这种畸形版本在**第一次比较**就暴露（W05/W12）。
        """
        lib = self._lib
        function = getattr(lib, "AsstGetNullSize", None) if lib is not None else None
        if function is None:
            return NULL_SIZE
        try:
            return int(function())
        except Exception:  # noqa: BLE001 - 哨兵探测失败退回等值常量
            logger.debug("AsstGetNullSize failed; falling back to NULL_SIZE", exc_info=True)
            return NULL_SIZE

    def stop_tasks(self, handle: AsstHandle) -> bool:
        """``AsstStop(handle)``。

        `[FACT]` 它**不打断**正在执行的 ``call_proc`` 项（只清任务列表与缓存），
        所以返回 True 不等于"已经停了"。
        """
        if not handle:
            return False
        return bool(self._require_lib().AsstStop(handle))

    # --- 任务发号（C 函数 1:1，不做白名单；白名单在 adapter 的第一行） ---
    def append_task(self, handle: AsstHandle, task_type: str, params: str) -> int:
        """``AsstAppendTask(handle, type, params)`` → task id；``0`` 表示被拒。"""
        return int(
            self._require_lib().AsstAppendTask(
                handle, _encode_path(task_type), _encode_path(params)
            )
        )

    def start(self, handle: AsstHandle) -> bool:
        """``AsstStart(handle)``。"""
        if not handle:
            return False
        return bool(self._require_lib().AsstStart(handle))

    def set_connection_extras(self, name: str, extras: str | bytes) -> None:
        """``AsstSetConnectionExtras(name, extras)``。C 侧返回 void。

        `[FACT]` JSON 解析失败时 C 侧**静默 return**，调用方无法得知失败。
        因此本方法只把"被调用过"记进 ``connection_extras_calls`` 并留一条 debug 日志，
        既不抛错，也不给出"已生效"的信号。
        """
        lib = self._require_lib()
        function = getattr(lib, "AsstSetConnectionExtras", None)
        self.connection_extras_calls += 1
        if function is None:
            logger.debug("AsstSetConnectionExtras absent; extras %r ignored", name)
            return
        function(_encode_path(name), _encode_path(extras))
        logger.debug("AsstSetConnectionExtras(%r) dispatched (void api)", name)

    def set_static_option(self, key: int, value: str) -> bool:
        """``AsstSetStaticOption(key, value)``。未知 key 由 C 侧返回 0。"""
        lib = self._require_lib()
        function = getattr(lib, "AsstSetStaticOption", None)
        if function is None:
            return False
        return bool(function(int(key), _encode_path(value)))

    # --- 内部 ---
    def _require_lib(self) -> Any:
        lib = self._lib
        if lib is None:
            raise MaaCallError(
                "load",
                MaaCallStatus.REJECTED,
                detail={"reason": "library is not loaded", "library_path": str(self._library_path)},
            )
        return lib
