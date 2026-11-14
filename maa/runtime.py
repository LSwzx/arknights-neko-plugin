"""引擎生命周期与状态机。**唯一**允许推进 :class:`EngineState` 的对象。

三条不可让步的边界
------------------
1. **唯一权威位置**：``engine_root``（= ``data_path("maa")``，构造注入）。没有 ``maa_root``、
   没有插件目录下的 ``native/``、不扫 winget / ``%LOCALAPPDATA%`` / "常见安装路径"。
   手动放置 = 用户把解压结果放进这一个目录，``bootstrap()`` 发现三件套齐全就补写
   ``engine.json`` 并进 ``UNPACKED``（不下载、不覆盖）。探测的失败是**静默**的：
   它不会让任何用例变红，只会让"这次到底从哪加载的"变成谜（D6）。
2. **``bootstrap()`` 绝不等下载**：缺引擎时起一条后台任务后立刻返回 ``DOWNLOADING``。
   295 MB 与宿主 ``[plugin_runtime].timeout ≤ 300s`` 必须彻底解耦。
3. **``cancel_download()`` 必须 await**：只 ``cancel()`` 不 await，``finally`` 里的半包清理
   会被下一次 ``bootstrap`` 覆盖，残留的 ``.part`` 会让下一轮 ``verify_sha256`` 直接 mismatch
   ——用户会看到"下载的包被污染了"，而其实是我们没删干净。

失败一律落 ``ABSENT``（不是 ``FAILED``）：半包的 ``.part`` / ``.unpacking-*`` 必须删掉，
原因记在 ``last_error``。这样"没装"与"装坏了"在状态上统一，避免出现"半路卡住"这种
没人处理的第四种状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import logging
import os
import secrets
import shutil
import struct
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .adapter import DeviceProfile, LinkState, MaaAdapter, Resolution
from .errors import (
    AdbDisconnected,
    CaptureTimeout,
    DownloadFailure,
    EngineDownloadFailed,
    EngineError,
    EngineHashMismatch,
    EngineLayoutInvalid,
    EngineLoadFailed,
    EngineNotReady,
    EngineUnpackFailed,
    InstanceCreateFailed,
    PlatformUnsupported,
    ResourceLoadRejected,
    ResourceMissing,
    UnpackFailure,
)
from .provision import (
    KNOWN_RELEASES,
    ReleaseSpec,
    ExtractResult,
    atomic_download,
    safe_extract,
    verify_sha256,
)
from .wrapper import (
    DEFAULT_MAX_BUFFER_GROW,
    AsstHandle,
    AsstInstanceOptionKey,
    MaaCallError,
    MaaCoreWrapper,
    MaaPlatform,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EngineState",
    "RuntimeLayout",
    "HealthSnapshot",
    "ProvisionPorts",
    "MaaRuntime",
    "DEFAULT_INSTANCE_OPTIONS",
    "SUPPORTED_CLIENT_SCOPE",
]

_MARKER_NAME: Final[str] = "engine.json"

DEFAULT_INSTANCE_OPTIONS: Final[Mapping[str, str]] = {
    "TouchMode": "minitouch",
    "AdbLiteEnabled": "1",
    "DeploymentWithPause": "0",
}
"""建实例后要立刻设置的选项（官方 GUI 也设这三项），**值是字符串**。

三条取值理由，改动前请先读：

* ``TouchMode = minitouch``：MAA 各平台的默认就是它，也是官方 GUI 的出厂值。
  ``maatouch`` 更快但要把一个可执行文件推进 ``/data/local/tmp``（属于"在设备上落地文件"，
  超出本插件"只读浏览"的边界）；``adb`` 模式每帧都走 ``adb exec-out``，截图最慢。
* ``AdbLiteEnabled = 1``：**截图稳定性相关**。关掉时 MAA 每次截图都要重连一次 adb，
  连拍场景（我们这个插件就是连拍）会明显抖；打开后复用连接。
  个别模拟器对这个模式兼容性差 —— 这时用 ``instance_options`` 覆盖成 ``"0"``，
  而不是改这里的默认值（默认值要对大多数人是对的）。
* ``DeploymentWithPause = 0``：本插件**不部署**，这一项只影响"暂停时是否自动部署"；
  显式写 0 是为了防止某天有人在别处把它打开，让一个只读插件开始动手。

不存在"引擎自动探测最优值"这回事：C API 只接受调用方给的字符串。
"""

INSTANCE_OPTION_KEYS: Final[Mapping[str, AsstInstanceOptionKey]] = {
    "TouchMode": AsstInstanceOptionKey.TOUCH_MODE,
    "DeploymentWithPause": AsstInstanceOptionKey.DEPLOYMENT_WITH_PAUSE,
    "AdbLiteEnabled": AsstInstanceOptionKey.ADB_LITE_ENABLED,
}
"""选项名 → C 枚举。名字用**可读名**而不是枚举值，是为了让日志/诊断能自解释。"""

SUPPORTED_CLIENT_SCOPE: Final[str] = "cn-official"
"""本插件**只支持国服官服**（官服 = 鹰角官方客户端；国服 = 中国大陆区服）。

为什么是"声明"而不是"检测"：MaaCore **没有任何 C API 能查询当前连接的客户端类型**
（客户端类型是识别阶段模板匹配出来的内部状态，``AsstGetImage`` / ``AsstConnected``
都给不出来）。所以这里不写"检测到外服就抛异常"这种看着很聪明、实际永远不会触发的
代码 —— 那正是上一轮刚清理掉的"死代码假绿"。

真正的防线放在两处：

* **声明**：这个常量进 ``status_snapshot()['client_scope']``，用户报障时一眼能看到
  "我们本来就没打算支持外服"，而不是去猜"为什么识别不准"；
* **文案**：``resource.*`` 一类失败的 hint 里明确写"只支持国服官服"，
  让外服用户**在第一次失败时**就知道原因，而不是反复重启。

为什么不做多资源（``cache`` 增量 / ``global`` 外服）：MAA 的 ``AsstLoadResource``
只接受"包含 ``resource`` 的那一层"，没有按子集加载的 API；要支持多客户端就得
同时分发多份模板（体积翻倍），而我们**一个国服都还没在真机上验过**。
按 ``docs/03-engine-provisioning.md`` 的原则：先锁一个范围把它做对。
"""


class EngineState(enum.Enum):
    ABSENT = "absent"            # 本地没有可用引擎（首次运行 / 用户清空）
    DOWNLOADING = "downloading"  # 正在下载归档
    VERIFIED = "verified"        # 归档落盘且 SHA256 匹配
    UNPACKED = "unpacked"        # 解压完成，磁盘上 lib + resource/ 齐备
    LOADED = "loaded"            # dlopen + 签名绑定 + AsstLoadResource + 实例创建 均成功
    READY = "ready"              # 以上全部 + ADB 已连接
    FAILED = "failed"            # 终态：某一步失败，last_error 非空


_TRANSITIONS: Final[dict[EngineState, frozenset[EngineState]]] = {
    EngineState.ABSENT: frozenset({EngineState.ABSENT, EngineState.DOWNLOADING,
                                   EngineState.UNPACKED, EngineState.FAILED}),
    EngineState.DOWNLOADING: frozenset({EngineState.DOWNLOADING, EngineState.VERIFIED,
                                        EngineState.ABSENT}),
    EngineState.VERIFIED: frozenset({EngineState.UNPACKED, EngineState.ABSENT,
                                     EngineState.FAILED}),
    EngineState.UNPACKED: frozenset({EngineState.LOADED, EngineState.ABSENT,
                                     EngineState.DOWNLOADING, EngineState.FAILED}),
    EngineState.LOADED: frozenset({EngineState.READY, EngineState.ABSENT,
                                   EngineState.DOWNLOADING, EngineState.FAILED}),
    EngineState.READY: frozenset({EngineState.LOADED, EngineState.ABSENT, EngineState.FAILED}),
    EngineState.FAILED: frozenset({EngineState.DOWNLOADING, EngineState.UNPACKED,
                                   EngineState.LOADED, EngineState.ABSENT,
                                   EngineState.FAILED}),
}
"""合法迁移表。**禁止**：``ABSENT→READY`` / ``ABSENT→LOADED`` / ``VERIFIED→READY`` /
``READY→DOWNLOADING`` / ``DOWNLOADING→UNPACKED`` / ``DOWNLOADING→LOADED`` /
``DOWNLOADING→FAILED``。非法迁移由 :meth:`MaaRuntime._set_state` 抛 ``EngineNotReady``，
而不是静默写入。

``DOWNLOADING→FAILED`` 被**刻意**排除：下载失败（网络/限流/磁盘满/哈希不符）与取消
一律回 ``ABSENT``，原因记 ``last_error``。落 ``FAILED`` 意味着"这台机器上引擎坏了、
需要人介入"，而一次网络抖动不该有这种语义——否则用户会被永久钉死在 ``FAILED``，
唯一的出路是重装。``FAILED`` 只留给"文件在磁盘上但装不起来"（布局歧义、dlopen 失败）。
"""


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """磁盘布局的唯一推导点。

    存在的理由（旧实现的教训）：``resource`` 该传哪一层，上一版用"名字像不像 resource"
    猜过一次，猜错的代价是 ``AsstLoadResource`` 恒为 False 而用户看到"ADB 连不上"。
    本类要求**列全搜索路径 + 用磁盘事实判定**，且是全仓库唯一的推导入口。

    搜索范围刻意收窄：只在 ``engine_root`` 本身、``engine_root/bin``、
    ``engine_root/lib`` 三处找核心库（``engine_root`` 本身是主路径）。**命中两处即判
    歧义失败**，不取第一个（否则预放一份同名库就能顶掉真库）。
    """

    engine_root: Path
    library_path: Path | None
    resource_parent: Path | None
    resource_dir: Path | None
    marker_path: Path
    user_dir: Path
    data_dir: Path
    cache_dir: Path
    searched: tuple[Path, ...] = ()

    @classmethod
    def resolve(
        cls,
        *,
        engine_root: Path,
        user_dir: Path,
        data_dir: Path,
        cache_dir: Path,
    ) -> "RuntimeLayout":
        """纯函数：只看磁盘事实，**任一项缺失都不抛错**。"""
        root = Path(engine_root)
        names = MaaCoreWrapper.default_library_names()
        searched = tuple(
            directory / name
            for directory in (root, root / "bin", root / "lib")
            for name in names
        )
        found = [path for path in searched if path.is_file()]
        library_path = found[0] if len(found) == 1 else None

        resource_dir = root / "resource"
        resource_parent = root if resource_dir.is_dir() else None
        return cls(
            engine_root=root,
            library_path=library_path,
            resource_parent=resource_parent,
            resource_dir=resource_dir if resource_dir.is_dir() else None,
            marker_path=root / _MARKER_NAME,
            user_dir=Path(user_dir),
            data_dir=Path(data_dir),
            cache_dir=Path(cache_dir),
            searched=searched,
        )

    def compiled_libraries(self) -> tuple[Path, ...]:
        """实际存在的核心库（可能 >1 → 歧义）。"""
        return tuple(path for path in self.searched if path.is_file())

    def missing_pieces(self) -> Sequence[str]:
        """缺哪些自证件。名字是稳定的诊断键，不是文案。"""
        missing: list[str] = []
        if self.library_path is None:
            missing.append("library")
        if self.resource_parent is None:
            missing.append("resource")
        if not self.marker_path.is_file():
            missing.append(_MARKER_NAME)
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """``health()`` 的返回值：不可变、可序列化、可直接塞进 entry 的 data。"""

    state: EngineState
    connected: bool
    version: str
    library_path: str | None
    resource_parent: str | None
    device: DeviceProfile | None
    resolution: Resolution | None
    image_size: tuple[int, int] | None
    download_progress: int | None
    error_code: str | None
    error_context: dict[str, Any]
    adapter_stats: dict[str, int]


@dataclass(frozen=True, slots=True)
class ProvisionPorts:
    """引导链路的两个可替换端口。**只服务测试**（R 组绝不发真网络、不真解压 295 MB）。

    Args:
        fetcher: ``(release: ReleaseSpec, target: Path, on_progress) -> Path``；
            默认走 :func:`provision.atomic_download`。签名里**只有一个 spec**：URL 由
            ``release.url`` 派生、期望哈希由 ``release.sha256`` 提供 —— "让用户/模型
            指定下载源"在这个接口上根本不存在（``atomic_download`` 已不再接受分开传的
            ``url`` / ``sha256``）。
        extractor: ``(archive: Path, target_dir: Path, release) -> Path | ExtractResult``；
            默认走 :func:`provision.safe_extract`（第三个参数只影响 0 命中时的诊断
            字段）。返回 ``Path`` 时 runtime 会自己定位核心库（只为兼容注入的简单
            解压器，生产路径永远返回 ``ExtractResult``）。
    """

    fetcher: Callable[..., Path] | None = None
    extractor: Callable[..., Any] | None = None


class _ExtractView:
    """注入式 extractor 只返回 ``Path`` 时的最小视图（字段名与 ``ExtractResult`` 对齐）。"""

    __slots__ = ("core_lib", "resource_dir", "selected", "skipped", "excluded", "skipped_sample")

    def __init__(self, root: Path, core_lib: Path | None) -> None:
        self.core_lib = core_lib
        self.resource_dir = root / "resource"
        self.selected = 0
        self.skipped = 0
        self.excluded = 0
        self.skipped_sample = ()


class MaaRuntime:
    """引擎生命周期。**唯一**允许推进 :class:`EngineState` 的对象。

    路径全部构造注入，不接受插件实例：``__init__.py`` 负责把
    ``self.data_path("maa")`` / ``self.cache_path("maa")`` 展开成 ``Path`` 再传进来，
    runtime 本身不知道宿主存在（测试才能断言"产物全部落在注入目录下"）。
    """

    def __init__(
        self,
        *,
        engine_root: Path,
        user_dir: Path,
        data_dir: Path,
        cache_dir: Path,
        release: ReleaseSpec | None,
        progress_sink: Callable[[int, str], None] | None = None,
        provision_ports: "ProvisionPorts | None" = None,
        wrapper_factory: Callable[..., MaaCoreWrapper] | None = None,
        max_image_buffer_bytes: int = 64 * 1024 * 1024,
        max_buffer_grow: int = DEFAULT_MAX_BUFFER_GROW,
        connect_timeout: float = 10.0,
        capture_timeout: float = 8.0,
        resolution_grace: float = 2.0,
        load_timeout: float = 20.0,
        destroy_timeout: float = 5.0,
        instance_options: Mapping[str, str] | None = None,
    ) -> None:
        self._engine_root = Path(engine_root)
        self._user_dir = Path(user_dir)
        self._data_dir = Path(data_dir)
        self._cache_dir = Path(cache_dir)
        self._release = release
        self._progress_sink = progress_sink
        self._ports = provision_ports or ProvisionPorts()
        self._wrapper_factory = wrapper_factory or MaaCoreWrapper

        self._max_image_buffer_bytes = int(max_image_buffer_bytes)
        self._max_buffer_grow = int(max_buffer_grow)
        self._connect_timeout = float(connect_timeout)
        self._capture_timeout = float(capture_timeout)
        self._resolution_grace = float(resolution_grace)
        self._load_timeout = float(load_timeout)
        self._destroy_timeout = float(destroy_timeout)

        # 实例选项：**在默认值上覆盖**，不是"给了就用给的"。
        # 只给 AdbLiteEnabled 一个值时，TouchMode 仍要有值 —— 否则等于把其余开关
        # 交回给 MaaCore 的内部默认，而"部分设置"比"完全不设置"更难排查。
        merged: dict[str, str] = dict(DEFAULT_INSTANCE_OPTIONS)
        if instance_options:
            for name, value in instance_options.items():
                if name not in INSTANCE_OPTION_KEYS:
                    logger.warning("unknown instance option %r ignored", name)
                    continue
                merged[name] = str(value)
        self._instance_options: dict[str, str] = merged
        self._applied_options: dict[str, str] = {}
        self._rejected_options: dict[str, str] = {}

        self._state = EngineState.ABSENT
        self._last_error: EngineError | None = None
        self._progress: int | None = None
        self._download_task: asyncio.Task[None] | None = None
        self._cancel_event: threading.Event | None = None
        self._executor: ThreadPoolExecutor | None = None

        self._wrapper: MaaCoreWrapper | None = None
        self._adapter: MaaAdapter | None = None
        self._handle: AsstHandle = 0
        self._resource_loaded = False
        self._device: DeviceProfile | None = None
        self._address = ""
        self._adb_path = ""
        self._connect_config = "General"
        # --- 销毁路径的可观测面（不参与任何业务判断，只为了让"有没有真的拆干净"
        #     在测试/真机复盘里能被断言，而不是靠读日志） ---
        self._teardown_in_progress = False
        self._stop_calls = 0            # 真的发出过几次 AsstStop
        self._destroy_calls = 0         # 真的发过几次销毁线程（=0 表示从未建实例）
        self._destroy_abandoned = 0     # 放弃等待的次数（>0 就是"已知会崩的窗口"）
        self._detach_invocations = 0    # 主动摘钩子成功了几次（仅放弃路径会 >0）
        # 被放弃的实例：**唯一**被允许的有界泄漏。保活它不只是"方便诊断"——
        # C 线程可能还在里面跑，解引用 wrapper 会让 `_lib` / `add_dll_directory`
        # 句柄提前消失（见 destroy() 的不变量）。
        self._abandoned_wrappers: list[MaaCoreWrapper] = []
        # 上一次**成功**连接用的全部参数（address / adb_path / config / extras）。
        # 只在 READY 时用来做幂等短路：参数一模一样就没必要重连（见 _connect_serialized）。
        # 存参数而不是存"连过了"这个布尔，是因为"用户显式换了设备/配置"必须能重连。
        self._connected_params: (
            tuple[str, str, str, tuple[tuple[str, str], ...]] | None
        ) = None
        # 上一次真连时 extras 的**下发结果**（None = 没配 extras）。
        # 形状：{"keys": tuple[str, ...], "dispatched": bool, "error": str | None}。
        # 只记键名不记值；写进 status_snapshot()['connection_extras']，见 _dispatch_connection_extras。
        self._extras_status: dict[str, Any] | None = None
        # 连接互斥：``adapter.connect()`` 的第一件事是 ``_reset_session()``，
        # 两个 Connect 并发时后一个会把前一个正在等的分辨率/uuid 清掉（"冲掉状态"）。
        # 锁放在 runtime 而不是 adapter：adapter 的调用方只有这里，
        # 而锁跨"发号 → 等回调 → 等分辨率"三段，属于会话级，不是单次 C 调用级。
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 只读快照
    # ------------------------------------------------------------------
    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def last_error(self) -> EngineError | None:
        """最近一次失败的异常。**派生快照**，不是可变共享字段。"""
        return self._last_error

    @property
    def adapter(self) -> MaaAdapter | None:
        return self._adapter

    @property
    def lifecycle_stats(self) -> dict[str, int]:
        """销毁路径的计数器。**只读、纯整数、可直接断言**。

        为什么把它们做成公开属性而不是只写日志：``on_shutdown`` 是宿主最后调用的
        那个钩子，事后没人能再问 ``wrapper.callback_attached``（那时 wrapper 已经是
        ``None``）。"超时放弃后到底有没有摘钩子"这条安全断言必须能在**销毁之后**
        查到，否则它就只是一句注释。

        键的含义：

        * ``stop_calls`` / ``destroy_calls``：真的发出去的 C 调用次数。重复关机的
          正确表现是**各为 1**（不是 2、3），这是幂等性的可断言形式；
        * ``destroy_abandoned``：放弃等待的次数，``> 0`` 就是那个"已知会崩的窗口"；
        * ``detach_invocations``：成功摘钩子的次数，正常路径恒为 0
          （正常路径由 ``wrapper.close()`` 摘，那条路不算"主动摘钩子"）；
        * ``abandoned_wrappers``：被保活的 wrapper 个数，应与 ``destroy_abandoned`` 相等；
        * ``teardown_in_progress``：当前是否正有一次销毁在跑（并发重入的保护标志）。
        """
        return {
            "stop_calls": self._stop_calls,
            "destroy_calls": self._destroy_calls,
            "destroy_abandoned": self._destroy_abandoned,
            "detach_invocations": self._detach_invocations,
            "abandoned_wrappers": len(self._abandoned_wrappers),
            "teardown_in_progress": int(self._teardown_in_progress),
        }

    @property
    def layout(self) -> RuntimeLayout:
        """每次现算：下载/解压会改变磁盘事实，缓存就必然撒谎。"""
        return RuntimeLayout.resolve(
            engine_root=self._engine_root,
            user_dir=self._user_dir,
            data_dir=self._data_dir,
            cache_dir=self._cache_dir,
        )

    @property
    def download_task(self) -> asyncio.Task[None] | None:
        """当前后台引导任务（``None`` 或已完成）。**只读**。"""
        return self._download_task

    @property
    def download_progress(self) -> int | None:
        """0–100；不在 ``DOWNLOADING`` 时为 ``None``。派生自状态，不是独立字段。"""
        if self._state is not EngineState.DOWNLOADING:
            return None
        return self._progress

    def health(self) -> HealthSnapshot:
        # 惰性收敛：断线后不该继续报 READY（理由见 _converge_link_state）。
        self._converge_link_state()
        layout = self.layout
        error = self._last_error
        adapter = self._adapter
        stats: dict[str, int] = {}
        if adapter is not None:
            snapshot = adapter.stats()
            stats = {
                "events_received": snapshot.events_received,
                "events_dropped_late": snapshot.events_dropped_late,
                "events_dropped_broken": snapshot.events_dropped_broken,
                "pending_calls": snapshot.pending_calls,
                "loop_binds": snapshot.loop_binds,
                "resolution_events": snapshot.resolution_events,
            }
        # ``connected`` 不许和链路事实打架：``AsstConnected`` 是 C 侧探针，掉线瞬间
        # 它可能还返回 true（controller 对象还在）。链路已断就是 false —— 这条判据的
        # 语义是"现在能不能用"，不是"对象还在不在"。
        link_down = adapter is not None and adapter.link_state is LinkState.DISCONNECTED
        return HealthSnapshot(
            state=self._state,
            connected=bool(adapter is not None and not link_down and adapter.is_healthy()),
            version=self._wrapper.version if self._wrapper is not None else "",
            library_path=str(layout.library_path) if layout.library_path else None,
            resource_parent=str(layout.resource_parent) if layout.resource_parent else None,
            device=self._device,
            resolution=self._adapter.resolution() if self._adapter is not None else None,
            image_size=None,
            download_progress=self.download_progress,
            error_code=error.code.value if error is not None and error.code else None,
            error_context=dict(error.context) if error is not None else {},
            adapter_stats=stats,
        )

    def status_snapshot(self) -> dict[str, Any]:
        """给 entry 的完整诊断（结构化字段，不含文案）。"""
        # 与 health() 同一个收敛点：status 是用户第一个会看的地方，
        # 断线后它必须显示 loaded + link_state=disconnected，而不是"一切就绪"。
        self._converge_link_state()
        layout = self.layout
        return {
            "state": self._state.value,
            "engine_root": str(self._engine_root),
            "library_path": str(layout.library_path) if layout.library_path else None,
            "resource_parent": str(layout.resource_parent) if layout.resource_parent else None,
            "missing_pieces": list(layout.missing_pieces()),
            "searched": [str(path) for path in layout.searched],
            "release_version": self._release.version if self._release is not None else None,
            "download_progress": self.download_progress,
            # 客户端范围是**声明**，不是检测结果（见 SUPPORTED_CLIENT_SCOPE）：
            # 写进诊断是为了让"识别不准"的报障能第一时间排除"外服"这个变量。
            "client_scope": SUPPORTED_CLIENT_SCOPE,
            "connect_config": self._connect_config,
            "link_state": (
                self._adapter.link_state.value if self._adapter is not None else "unknown"
            ),
            "instance_options": {
                "requested": dict(self._instance_options),
                "applied": dict(self._applied_options),
                "rejected": dict(self._rejected_options),
            },
            # None = 这次连接没配 extras；否则 {"keys", "dispatched", "error"}。
            # dispatched=True 只表示"调用发出去了"（C 侧是 void + 静默 return），
            # 不代表 MaaCore 解析成功 —— 字段名刻意不叫 applied。
            "connection_extras": self._extras_snapshot(),
            "error": self._last_error.to_dict() if self._last_error is not None else None,
            "device": None if self._device is None else {
                "connected": self._device.connected,
                "uuid": self._device.uuid,
                "resolution": self._device.resolution.status.value,
                "width": self._device.resolution.width,
                "height": self._device.resolution.height,
            },
        }

    # ------------------------------------------------------------------
    # 阶段 1~3：获取引擎
    # ------------------------------------------------------------------
    async def bootstrap(self, *, force: bool = False) -> EngineState:
        """幂等主入口：**起任务 / 立即返回，绝不等下载完成**。

        判据顺序：

          1. 已有一条后台任务在跑：``force=False`` 直接返回当前 state；
             ``force=True`` 先 ``cancel()`` 旧任务并 **await 收尾**，再起新任务；
          2. 布局完整（三件套齐全）：不动下载，直接 ``UNPACKED`` → 尝试装载 ``LOADED``；
             若只是 ``engine.json`` 缺失/哈希不符（用户手动放置），**补写标记**后同样进入；
          3. 布局歧义（核心库命中 >1 处）：落 ``FAILED``（``EngineLayoutInvalid``），不下载；
          4. 真缺引擎：``release is None`` → ``PlatformUnsupported``；否则起后台任务 →
             ``DOWNLOADING``。

        除 ``PlatformUnsupported`` 外**不抛错**：所有失败都落成状态 + ``last_error``
        （``on_startup`` 是 lifecycle 钩子，抛错会被宿主判 startup-failed）。
        """
        self._loop_hint()
        task = self._download_task
        if task is not None and not task.done():
            if not force:
                return self._state
            await self.cancel_download()

        self._download_task = None
        if self._state in (EngineState.READY, EngineState.LOADED):
            return self._state

        layout = self.layout
        ambiguous = layout.compiled_libraries()
        if len(ambiguous) > 1:
            self._last_error = EngineLayoutInvalid(
                root=self._engine_root,
                missing=(),
                found=tuple(str(path) for path in ambiguous),
            )
            self._set_state(EngineState.FAILED)
            return self._state

        missing = list(layout.missing_pieces())
        if not missing:
            if self._state is not EngineState.UNPACKED:
                self._set_state(EngineState.UNPACKED)
            await self._load_engine()
            return self._state
        if set(missing) == {_MARKER_NAME}:
            # 手动放置：磁盘事实完整，只缺/错版本标记 → 补写，绝不下载、绝不覆盖用户文件。
            self._write_marker(
                self._engine_root, source="manual", version="unknown", sha256=None
            )
            self._set_state(EngineState.UNPACKED)
            await self._load_engine()
            return self._state

        if self._release is None:
            raise PlatformUnsupported(
                system=_platform_key(), supported=tuple(KNOWN_RELEASES)
            )

        self._start_download()
        return EngineState.DOWNLOADING

    async def cancel_download(self) -> None:
        """取消后台引导任务并 **await 其收尾**，然后 ``state → ABSENT``。

        必须 await：只 ``cancel()`` 不 await 的话，``finally`` 里的清理还没跑完就被
        下一次 ``bootstrap`` 覆盖，半包残留 → 下一轮 ``verify_sha256`` 直接 mismatch。
        """
        task, self._download_task = self._download_task, None
        cancel = self._cancel_event
        self._cancel_event = None
        if cancel is not None:
            cancel.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 收尾阶段的异常不该阻断 shutdown
                logger.debug("download task ended with error while cancelling", exc_info=True)
        if task is not None:
            # 即使任务还没来得及跑第一行，取消这个事实也必须留下来（否则用户点取消后
            # 状态机回到 ABSENT 却没有任何原因，看起来像"什么都没发生"）。
            self._last_error = EngineDownloadFailed(
                url=self._release.url if self._release is not None else "",
                target=self._archive_path(),
                kind=DownloadFailure.CANCELLED,
                detail="cancelled by host/user",
            )
        if self._state is EngineState.DOWNLOADING:
            self._set_state(EngineState.ABSENT)
        self._progress = None
        self._cleanup_partials(self._archive_path(), None)

    async def ensure_ready(self) -> None:
        """业务侧的"我要用引擎"守卫。**不推进状态机**，只断言。

        ``DOWNLOADING`` 时**快速失败**（不等），并把进度带出去；``progress=None``
        表示尚未开始下载，不是 0%（本实现的下载入口会先写 ``progress=0``，
        所以正常路径上拿到的是 0–100）。

        Raises:
            EngineNotReady: ``state`` 未到 READY
            AdbDisconnected: 状态说 READY 但链路已断（``Disconnect`` 事件）或
                ``is_connected()`` 已为假
            EngineLoadFailed: 库根本没加载成功（state 停在 FAILED）
        """
        if self._state is EngineState.READY:
            # 先收敛链路事实：DISCONNECTED 时立刻用**真实原因**失败，
            # 而不是让调用方先去等满 capture_timeout（那会报成"截图卡住"，
            # 把"掉线"这个真因藏起来）。
            link_down = self._converge_link_state()
            if link_down is not None:
                raise link_down
            if not self._is_connected():
                self._set_state(EngineState.LOADED)
                raise AdbDisconnected(
                    address=self._address,
                    why="engine state was READY but is_connected() is false",
                    adb_path=self._adb_path,
                )
            return
        if self._state is EngineState.FAILED and isinstance(self._last_error, EngineLoadFailed):
            raise self._last_error
        required = {
            EngineState.DOWNLOADING: ("download_complete",),
            EngineState.ABSENT: ("engine",),
            EngineState.FAILED: ("engine",),
            EngineState.VERIFIED: ("engine_unpacked",),
            EngineState.UNPACKED: ("engine_loaded",),
            EngineState.LOADED: ("connection",),
        }.get(self._state, ("engine_ready",))
        raise EngineNotReady(
            state=self._state.value,
            required=required,
            progress=self.download_progress,
        )

    async def capture(self) -> bytes:
        """薄编排：``ensure_ready()`` → ``adapter.capture()``。不吞 ``EngineError``。"""
        await self.ensure_ready()
        adapter = self._adapter
        if adapter is None:
            raise EngineNotReady(state=self._state.value, required=("adapter",))
        return await adapter.capture()

    # ------------------------------------------------------------------
    # 后台任务本体
    # ------------------------------------------------------------------
    def _start_download(self) -> None:
        self._cancel_event = threading.Event()
        self._progress = 0
        self._set_state(EngineState.DOWNLOADING)
        self._emit(0, "准备下载引擎")
        self._download_task = asyncio.create_task(self._run_provision(), name="maa-provision")

    async def _run_provision(self) -> None:
        """**后台任务本体**。顺序：下载 → 校验 → 解压 → 写标记 → 原子换入 → 回写状态。

        清理只有一个 ``finally`` 出口（成功路径也要删 ``.part``：残留的 ``.part``
        会让下一次以为"有进度可续"）。失败/取消一律落 ``ABSENT``，原因记 ``last_error``。
        """
        release = self._release
        if release is None:  # bootstrap 已挡，防御性返回
            self._set_state(EngineState.ABSENT)
            return

        archive = self._archive_path()
        staging: Path | None = None
        try:
            archive.parent.mkdir(parents=True, exist_ok=True)
            await self._fetch(release, archive)

            self._apply_progress(55)
            self._set_state(EngineState.VERIFIED)
            await self.verify_archive(archive, expected_sha256=release.sha256)

            self._apply_progress(60)
            self._engine_root.parent.mkdir(parents=True, exist_ok=True)
            staging = self._staging_path()
            await self.unpack(archive, target_dir=staging, release=release)
            # safe_extract 只往 target_dir 里写（且要求它事先不存在），产物根即 staging。
            dest = staging
            self._write_marker(
                dest, source="download", version=release.version, sha256=release.sha256
            )

            self._apply_progress(90)
            await asyncio.to_thread(self._commit, dest)
            staging = None  # 已被整体换入 engine_root
            self._set_state(EngineState.UNPACKED)
            self._apply_progress(100)
            # 后台把最后一段也走完（UNPACKED → LOADED）：否则用户要等下一次 bootstrap
            # 才拿到可用引擎，而"下载完成"在他的视角里就等于"能用了"。
            await self._load_engine()
        except asyncio.CancelledError:
            self._last_error = EngineDownloadFailed(
                url=release.url,
                target=archive,
                kind=DownloadFailure.CANCELLED,
                detail="cancelled by host/user",
            )
            if self._state is EngineState.DOWNLOADING:
                self._set_state(EngineState.ABSENT)
            raise
        except EngineError as exc:
            self._last_error = exc
            if isinstance(exc, EngineHashMismatch):
                # 坏包必须删：留着它，下次会直接复用（P16 的最关键一条）。
                with contextlib.suppress(OSError):
                    archive.unlink(missing_ok=True)
            if self._state in (EngineState.DOWNLOADING, EngineState.VERIFIED):
                self._set_state(EngineState.ABSENT)
        except BaseException as exc:  # noqa: BLE001 - 后台任务不许把异常留给 "never retrieved"
            self._last_error = EngineDownloadFailed(
                url=release.url,
                target=archive,
                kind=DownloadFailure.WRITE_ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )
            if self._state in (EngineState.DOWNLOADING, EngineState.VERIFIED):
                self._set_state(EngineState.ABSENT)
        finally:
            self._progress = None
            self._cleanup_partials(archive, staging)

    async def verify_archive(self, archive: Path, *, expected_sha256: str) -> None:
        """校验归档哈希。

        ``provision.verify_sha256`` **自己抛** ``EngineHashMismatch``（返回实际 hex 只
        是给审计日志/异常上下文用的）；本方法不要重新判一次"对不对"——``bool`` 语义
        在 provision 侧已经作废，再包一层就会变成两份不一致的处置。这里只负责把它
        搬到线程池（295 MB 的流式读盘不能占事件循环）。

        Raises:
            EngineHashMismatch: ``archive`` 不可读、期望值不合法或内容不符
        """
        await asyncio.to_thread(
            verify_sha256,
            archive,
            expected_sha256,
            version=self._release.version if self._release is not None else "",
        )

    async def unpack(
        self,
        archive: Path,
        *,
        target_dir: Path,
        release: ReleaseSpec | None = None,
    ) -> ExtractResult:
        """安全解压并校验布局。

        放行规则与攻击面在 ``provision.safe_extract``（那里是唯一实现点）；本方法只做
        "解完之后必须是可加载的树"这一条运行时检查。

        Args:
            archive: 已通过哈希校验的 zip。
            target_dir: **必须不存在**的落盘目录（``safe_extract`` 用
                ``mkdir(exist_ok=False)`` 原子占位，绝不与已有目录合并）。
            release: 清单条目。传给 ``safe_extract`` 只影响 0 命中时的**诊断字段**
                （用版本 pin 的 ``core_hint`` 而不是通用默认名）；安全行为不随之变化。
                省略时**落到 ``self._release``**（生产路径 ``_run_provision`` 也显式传，
                两条路都指向同一个 ``ReleaseSpec``）。

        Returns:
            ``ExtractResult``；``core_lib`` 已 resolve。注入式 extractor 只返回
            ``Path`` 时，这里补成 :class:`_ExtractView`（字段名与之一致）。

        Raises:
            EngineUnpackFailed / EngineLayoutInvalid
        """
        # 默认落到本 runtime 的清单条目（不再把 None 传下去）：
        # ``safe_extract`` 现在**要求** release 非空 —— 它要靠 release 做版本/尺寸比对，
        # 传 None 会让那道比对静默退化成"没有比对"（形同虚设）。
        effective_release = release if release is not None else self._release
        if effective_release is None:
            # 两条来源都空 → 没有任何版本可以比对。**不能**把 None 继续传给
            # ``safe_extract``：那里的不变量会直接拒绝（ReleaseSpec 是必需的），
            # 那属于"下游才发现"，这里提前用本层的语义说清楚缺什么。
            raise EngineNotReady(state=self._state.value, required=("engine_release",))
        extractor = self._ports.extractor
        if extractor is not None:
            raw = await asyncio.to_thread(extractor, archive, target_dir)
        else:
            # 显式按关键字传 release：位置参数漏传过一次（下游 signature 变更时尤其危险），
            # 写成关键字后少一个参数就是 TypeError 而不是"悄悄用默认值"。
            raw = await asyncio.to_thread(
                safe_extract, archive, target_dir, release=effective_release
            )

        result = raw if hasattr(raw, "core_lib") else _ExtractView(
            Path(raw), self._locate_core_library(Path(raw))
        )
        # 两个分支的产物根目录**都是** target_dir（safe_extract 只会写在它下面，
        # _ExtractView 也是拿 target_dir 构造的）——不去猜 result 上的路径字段。
        dest = target_dir
        core_lib = getattr(result, "core_lib", None)
        missing: list[str] = []
        if core_lib is None or not Path(core_lib).is_file():
            missing.append("library")
        if not (dest / "resource").is_dir():
            missing.append("resource")
        if missing:
            raise EngineLayoutInvalid(root=dest, missing=tuple(missing), found=())
        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 阶段 4：装载（同步，调用方负责丢到线程）
    # ------------------------------------------------------------------
    def load_library(self, *, library_path: Path | None = None) -> MaaCoreWrapper:
        """dlopen + 绑签名。幂等（已加载则直接返回）。

        必须把 ``library_path.parent`` 作为 ``dll_directory`` 传给 ``wrapper.load()``：
        Windows 上 ``add_dll_directory`` 决定同目录依赖能否被解析。

        Raises:
            EngineLoadFailed: 缺文件（``oserror``）/ 缺符号（``symbol``）/ 签名不符（``TypeError``）
        """
        if self._wrapper is not None:
            return self._wrapper

        layout = self.layout
        path = Path(library_path) if library_path is not None else layout.library_path
        if path is None:
            raise EngineLayoutInvalid(
                root=self._engine_root,
                missing=("library",),
                found=tuple(str(item) for item in layout.compiled_libraries()),
            )

        wrapper = self._wrapper_factory(
            library_path=path,
            max_image_buffer_bytes=self._max_image_buffer_bytes,
            max_buffer_grow=self._max_buffer_grow,
        )
        try:
            wrapper.load(dll_directory=path.parent)
        except OSError as exc:
            raise EngineLoadFailed(
                library_path=path, symbol=None, reason="dlopen failed", oserror=str(exc)
            ) from exc
        except AttributeError as exc:
            raise EngineLoadFailed(
                library_path=path, symbol=_symbol_from(exc), reason="missing symbol"
            ) from exc
        except TypeError as exc:
            raise EngineLoadFailed(
                library_path=path, symbol=_symbol_from(exc), reason="signature mismatch"
            ) from exc

        # 回调必须在 create_instance() 之前设置；此刻 adapter 还不存在，
        # 所以先挂一个转发器，等实例建好再接上真正的 adapter。
        wrapper.set_callback(self._on_raw_event)
        self._wrapper = wrapper
        return wrapper

    def _on_raw_event(self, msg: int, details_json: str | None, custom_arg: int | None) -> None:
        adapter = self._adapter
        if adapter is not None:
            adapter._on_thread_event(msg, details_json, custom_arg)

    def load_resource(self, *, parent_dir: Path) -> None:
        """``AsstSetUserDir`` + ``AsstLoadResource``。

        先做**存在性预检**（给 ``ResourceMissing`` 列出 searched），再调 C；
        C 返回 False 抛 ``ResourceLoadRejected``（带 ``load_ok`` / ``user_dir_ok``）。

        Raises:
            ResourceMissing, ResourceLoadRejected, EngineLoadFailed
        """
        wrapper = self._require_wrapper()
        layout = self.layout
        if not Path(parent_dir).is_dir():
            raise ResourceMissing(expected_dir=Path(parent_dir), searched=layout.searched)

        self._user_dir.mkdir(parents=True, exist_ok=True)
        user_dir_ok = True
        try:
            wrapper.set_user_dir(layout.user_dir)
        except MaaCallError:
            user_dir_ok = False
            logger.debug("AsstSetUserDir rejected; continuing", exc_info=True)

        try:
            wrapper.load_resource(Path(parent_dir))
        except MaaCallError as exc:
            raise ResourceLoadRejected(
                resource_parent=Path(parent_dir),
                load_ok=False,
                user_dir_ok=user_dir_ok,
                user_dir=layout.user_dir,
            ) from exc
        self._resource_loaded = True

    def create_instance(self) -> AsstHandle:
        """``AsstCreateEx``。返回 0 抛 ``InstanceCreateFailed``。

        ``[FACT]`` 它只在 ``!inited()``（资源未加载成功）时返回 ``nullptr``，
        所以异常的 ``resource_loaded`` 字段把因果写死，别去猜"MaaCore 内部炸了"。
        """
        wrapper = self._require_wrapper()
        handle = wrapper.create_instance()
        if not handle:
            raise InstanceCreateFailed(
                resource_loaded=self._resource_loaded,
                library_path=self.layout.library_path,
            )
        # 官方 GUI 的下一步就是设实例选项（``AsstProxy.cs``：Create → SetInstanceOption
        # → Connect）。**必须在 Connect 之前**：TouchMode 是连接/截图时才被 controller
        # 读走的，连完再设等于这一会话白设。
        self._apply_instance_options(handle)
        adapter = MaaAdapter(
            wrapper=wrapper,
            handle=handle,
            connect_timeout=self._connect_timeout,
            capture_timeout=self._capture_timeout,
            resolution_grace=self._resolution_grace,
        )
        self._handle = handle
        self._adapter = adapter
        self._set_state(EngineState.LOADED)
        return handle

    def _apply_instance_options(self, handle: AsstHandle) -> dict[str, str]:
        """逐项设实例选项，返回**被接受**的那些。失败的记进诊断，不阻断装载。

        为什么不因为设不上就抛错：这些是"体验调节项"，不是"能不能跑"的前提。
        真正的后果是截图变慢或触摸退化，那属于降级，不是装载失败 ——
        而把一个可用的引擎因为"某项开关不被这个版本支持"判死，才是真正的坏决定。

        也不逐项``raise``：C 侧对"句柄空 / key 越界 / value 不被接受"统一返回 0，
        没有任何区分手段，编造原因就等于撒谎。这里只如实记"哪项没设上"。
        """
        wrapper = self._require_wrapper()
        applied: dict[str, str] = {}
        rejected: dict[str, str] = {}
        for name, value in self._instance_options.items():
            key = INSTANCE_OPTION_KEYS[name]
            try:
                ok = wrapper.set_instance_option(handle, key, value)
            except MaaCallError:
                logger.debug("set_instance_option(%s=%s) raised", name, value, exc_info=True)
                ok = False
            except Exception:  # noqa: BLE001 - 选项是体验项，任何异常都降级为"没设上"
                logger.debug("set_instance_option(%s=%s) failed", name, value, exc_info=True)
                ok = False
            if ok:
                applied[name] = value
            else:
                rejected[name] = value
                logger.debug("instance option not applied: %s=%s", name, value)
        self._applied_options = applied
        self._rejected_options = rejected
        return applied

    async def _load_engine(self) -> None:
        """``UNPACKED → LOADED`` 的三步，整体带超时。**边界捕获，不改异常类型。**"""
        try:
            await asyncio.wait_for(asyncio.to_thread(self._load_chain), timeout=self._load_timeout)
        except asyncio.TimeoutError:
            self._last_error = EngineLoadFailed(
                library_path=self.layout.library_path,
                symbol=None,
                reason=f"load timed out after {self._load_timeout}s",
            )
            self._set_state(EngineState.FAILED)
        except EngineError as exc:
            self._last_error = exc
            if self._state is not EngineState.FAILED:
                self._set_state(EngineState.FAILED)
        except Exception as exc:  # noqa: BLE001 - 边界：装载期的非 EngineError 一律归因到装载
            self._last_error = EngineLoadFailed(
                library_path=self.layout.library_path,
                symbol=None,
                reason="unexpected error while loading",
                oserror=f"{type(exc).__name__}: {exc}",
            )
            self._set_state(EngineState.FAILED)

    def _load_chain(self) -> None:
        if self._wrapper is not None and self._handle:
            self._set_state(EngineState.LOADED)
            return
        self.load_library()
        layout = self.layout
        parent = layout.resource_parent
        if parent is None:
            raise ResourceMissing(
                expected_dir=self._engine_root / "resource", searched=layout.searched
            )
        if not self._resource_loaded:
            self.load_resource(parent_dir=parent)
        if not self._handle:
            self.create_instance()

    # ------------------------------------------------------------------
    # 阶段 5：连接
    # ------------------------------------------------------------------
    async def connect(
        self,
        *,
        address: str,
        adb_path: str = "",
        config: str = "General",
        extras: Mapping[str, str] | None = None,
        force: bool = False,
    ) -> DeviceProfile:
        """连接设备。成功 ``LOADED/READY → READY``；失败**留在 LOADED**（可重试）。

        Args:
            address: ``host:port``。空串直接 ``AdbAddressMissing``（adapter 判）。
            adb_path: adb 可执行文件路径；空串表示"用 MAA 自带的推断"。
            config: **连接配置名**，原样传给 ``AsstAsyncConnect`` 的第 4 个参数。
                官方的取值是 ``General`` / ``MuMuEmulator12`` / ``LDPlayer`` /
                ``BlueStacks`` / ``Nox`` / ``MEmu`` / ``Waydroid`` 等 ——
                不同模拟器要走的连接参数完全不同，写死 ``General`` 会让
                MuMu12 这类必须走 extras 的模拟器"能连但截图慢/不稳"。
            extras: 某个 config 需要的额外参数（如 ``{"path": "...\\MuMuPlayer-12.0"}``）。
                非空时先 ``AsstSetConnectionExtras(config, json)`` 再连 ——
                **顺序不能反**：C 侧是在 ``connect`` 里读这张表的，连完再设等于没设。
            force: **只影响"是否复用已有会话"，不影响任何校验**（见下）。``True`` 时
                跳过幂等短路、无条件真重连（仍走互斥锁、仍由 adapter 重置会话）。
                用于"用户显式点了重连"这种场景：设备可能"假活"（``READY`` 但截图一直
                超时、没有 ``Disconnect`` 事件），这时短路会让按钮看起来没反应。

        ``config`` / ``extras`` 必须是**调用方传进来的**，不能在这里根据
        address 猜模拟器：端口 16384 既可能是 MuMu 也可能是别人转发的，
        猜错比不猜更糟（会静默走错连接路径，表现成"偶发截图失败"）。

        **幂等**：已经在 ``READY``、且这次请求的四个参数与上次成功连接**完全一致**时，
        直接返回缓存下来的 :class:`DeviceProfile`，不发任何 C 调用、不重置会话
        （细节与理由见 :meth:`_connect_serialized`）。参数变了 = 用户要换设备，正常重连。
        ``force=True`` 是唯一"明知参数没变也要重连"的入口 —— 它**不放松**任何前置校验：
        状态不对照样 ``EngineNotReady``、地址为空照样 ``AdbAddressMissing``、并发照样排队。

        Raises:
            AdbAddressMissing, AdbConnectTimeout, AdbDisconnected, EngineNotReady
        """
        # 互斥：并发 Connect 会让后一个的 ``_reset_session()`` 把前一个正在等的
        # 分辨率/uuid 清空（状态被冲掉，然后两个一起超时）。
        async with self._connect_lock:
            return await self._connect_serialized(
                address=address,
                adb_path=adb_path,
                config=config,
                extras=extras,
                force=force,
            )

    async def connect_forced(
        self,
        *,
        address: str,
        adb_path: str = "",
        config: str = "General",
        extras: Mapping[str, str] | None = None,
    ) -> DeviceProfile:
        """``connect(..., force=True)`` 的**可选**入口，供上层"显式重连"用。

        为什么要多一个方法而不是把 ``force`` 塞进 ``RuntimePort.connect``：
        业务层的窄接口（``companion/service.py::RuntimePort``）故意只有
        ``address`` / ``adb_path``（+ 两个可选连接参数），给它加形参会让每一个只实现
        窄接口的端口实现（含用例替身）收到不认识的 keyword，把"用户没配"变成
        ``TypeError``。所以可选能力走**能力探测**（``getattr(port, "connect_forced", None)``，
        与 ``handle_engine_repair`` 探测 ``purge`` 同一套写法）：有就强制重连，没有就退回
        普通 ``connect()``（老实现/替身的行为不变）。
        """
        return await self.connect(
            address=address, adb_path=adb_path, config=config, extras=extras, force=True
        )

    async def _connect_serialized(
        self,
        *,
        address: str,
        adb_path: str,
        config: str,
        extras: Mapping[str, str] | None,
        force: bool,
    ) -> DeviceProfile:
        # 状态检查在锁**内部**：排队期间状态可能已经变了（比如被另一个 Connect
        # 推到了 READY），此时应当按最新状态判，而不是拿进锁前的旧快照。
        if self._state not in (EngineState.LOADED, EngineState.READY):
            raise EngineNotReady(
                state=self._state.value,
                required=("connection",),
                progress=self.download_progress,
            )
        adapter = self._adapter
        if adapter is None:
            raise EngineNotReady(state=self._state.value, required=("adapter",))

        request = _connect_request(
            address=address, adb_path=adb_path, config=config, extras=extras
        )
        if force:
            # 用户显式要求重连：不做任何启发式判断，直接重建会话。
            logger.debug("forced connect: skipping session reuse (address=%s)", address)
        else:
            reused = self._reuse_session(request, adapter)
            if reused is not None:
                return reused

        self._address = address
        self._adb_path = adb_path
        self._connect_config = config

        # 每次**真连**都重置 extras 可观测状态：换了 address/config 之后不能还留着
        # 上一次的 dispatched=True（那等于告诉用户"这次也发出去了"，而这次根本没有）。
        # 短路路径（上面 _reuse_session 命中）不重置 —— 会话没变，上一次的事实仍然成立。
        self._extras_status = None
        if extras:
            self._dispatch_connection_extras(config, extras)

        try:
            profile = await adapter.connect(address=address, adb_path=adb_path, config=config)
        except (AdbDisconnected, CaptureTimeout) as exc:
            self._last_error = exc
            self._drop_session()
            if self._state is EngineState.READY:
                self._set_state(EngineState.LOADED)
            raise
        except EngineError as exc:
            self._last_error = exc
            self._drop_session()
            raise
        self._device = profile
        self._connected_params = request
        self._set_state(EngineState.READY)
        return profile

    def _dispatch_connection_extras(self, config: str, extras: Mapping[str, str]) -> None:
        """下发 ``AsstSetConnectionExtras``，并把**发没发出去**记进 ``_extras_status``。

        为什么非记不可：``[FACT]`` C 侧 ``AsstSetConnectionExtras`` 返回 ``void``，
        JSON 解析失败也只是**静默 return** —— 也就是说"配了 extras 却没生效"这件事，
        C 永远不会告诉我们。上一版用 ``contextlib.suppress(Exception)`` 把 Python 侧
        的失败也一起吞了，结果用户配了 MuMu12 的安装目录而 extras 根本没发出去时，
        状态页一片正常，只能靠"截图有点慢"去猜。现在三态可分：

        * ``None``（字段不写）= 这次连接**没配** extras；
        * ``dispatched=True`` = 调用**发出去了**；
        * ``dispatched=False`` + ``error`` = Python 侧就失败了（符号缺失/库未加载/编码失败）。

        **``dispatched=True`` 只证明"调用发出去了"，不证明 C 侧解析成功** —— 这条必须
        写在字段旁边，否则下一个人会把它读成"extras 已生效"，而 C 侧没有任何回执。

        **只记键名，绝不记值**：值通常是 ``D:/Program Files/...`` 这种绝对路径，
        S08 的纪律对诊断字段同样适用（服务层出口还会再过一遍消毒）。
        """
        status: dict[str, Any] = {
            "keys": tuple(sorted(str(key) for key in extras)),
            "dispatched": False,
            "error": None,
        }
        self._extras_status = status
        try:
            self._require_wrapper().set_connection_extras(
                config, json.dumps(dict(extras), ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 - 体验项：失败只降级 + 记录，不阻断连接
            # 截断是防呆：MaaCallError 的 detail 可能带 library_path（绝对路径），
            # 全量原文只进日志，进 snapshot 的这 160 字由服务层再消毒一遍。
            status["error"] = f"{type(exc).__name__}: {exc}"[:160]
            logger.warning(
                "connection extras NOT dispatched (config=%s, keys=%s): %s: %s",
                config,
                ",".join(status["keys"]),
                type(exc).__name__,
                exc,
            )
            return
        status["dispatched"] = True
        logger.debug(
            "connection extras dispatched (config=%s, keys=%s)", config, status["keys"]
        )

    def _extras_snapshot(self) -> dict[str, Any] | None:
        """``_extras_status`` 的只读快照（``None`` = 这次连接没配 extras）。"""
        status = self._extras_status
        if status is None:
            return None
        return {
            "keys": list(status["keys"]),
            "dispatched": bool(status["dispatched"]),
            "error": status["error"],
        }

    def _reuse_session(
        self,
        request: tuple[str, str, str, tuple[tuple[str, str], ...]],
        adapter: MaaAdapter,
    ) -> DeviceProfile | None:
        """``READY`` + 参数完全一致 → 返回已缓存的 profile（**幂等短路**），否则 ``None``。

        不改会怎样：``adapter.connect()`` 的第一件事是 ``_reset_session()``，它会把
        分辨率 / uuid / 链路状态全部清空。也就是说"再点一次重连"（或任何重试逻辑）
        都会把一个**已经建好、已经拿到分辨率**的会话拆掉重建；网络慢一点就表现为
        "本来好的，点了重连反而没分辨率了"。

        三条边界（每条都对应一个真实需求）：

        * **参数变了必须真重连**：address / adb_path / config / extras 任一项不同，
          都是用户显式要求换设备或换连接路径，短路等于无视他的修改；
        * **非 ``READY`` 不短路**：``LOADED`` 的意思是"还没连上"，此刻手里没有会话可复用；
        * **``DISCONNECTED`` 不许短路**：链路已经断了，缓存里的 profile 是**过期事实**，
          返回它等于把"掉线"报成"已连接"。这种情况必须真重连（``note_link_down()``
          会先把状态沉到 ``LOADED``，所以正常路径上根本走不到这里）。

        ``RECONNECTING`` 仍然短路：adapter 的 ``LinkState`` 文档明确写了"此时不要重发
        Connect" —— MAA 正在自己重连，我们再发一个只会和它打架。链路恢复后
        ``link_state`` 回到 ``CONNECTED``，runtime 的 ``READY`` 不需要任何动作。

        **"假活"场景由 ``force=True`` 兜底，不靠启发式**：设备可能 ``READY`` 但截图一直
        超时、且没有 ``Disconnect`` 事件（于是 ``link_state`` 仍是 ``CONNECTED``）。
        想在这里凭"上次失败过"或"最近一次截图很慢"之类的信号猜"该不该重连"，就要引入
        一堆阈值和时序假设，猜错的代价是**把好好的会话拆掉**。所以本方法只按**可验证的
        事实**短路（状态 + 四参数 + 链路四态）；"用户显式点了重连"这种意图由调用方用
        ``force`` 表达（见 :meth:`connect_forced`）。
        """
        if self._state is not EngineState.READY or self._device is None:
            return None
        if self._connected_params != request:
            return None
        if adapter.link_state is LinkState.DISCONNECTED:
            return None
        logger.debug(
            "connect reused existing session: address=%s config=%s link=%s",
            request[0],
            request[2],
            adapter.link_state.value,
        )
        return self._device

    def _drop_session(self) -> None:
        """会话不再可信：清掉"上次成功参数"与缓存的 profile。

        没有这一步，"连了一次失败"之后 ``status`` 里的 ``device`` 仍然是上一次的
        ``connected=True`` —— 状态机说 ``loaded``、设备区说"已连接"，用户只能猜。
        """
        self._connected_params = None
        self._device = None

    def mark_degraded(self, exc: EngineError) -> None:
        """``READY/LOADED → LOADED``，记录 ``last_error``。由 adapter 异常冒泡处调用。"""
        self._sink_to_loaded(exc)

    def note_link_down(self, reason: str = "") -> AdbDisconnected | None:
        """链路已断：``READY → LOADED`` 并记 ``AdbDisconnected``。不抛错、幂等。

        Args:
            reason: 链路事件带来的原因原文（``adapter.link_reason``）。空串时用一句
                中性描述 —— **不编造**具体原因（"adb refused" 这种话只能来自上游）。

        Returns:
            本次构造的异常（**没有下沉**时返回 ``None``，调用方据此判断"该不该失败"）。

        为什么由 runtime 自己收敛（而不是 adapter 断线时回调）：adapter 是「C 线程 →
        事件循环」的桥，让它反向持有 runtime 会把依赖变成环（adapter → runtime →
        adapter），销毁路径上尤其危险 —— C 线程仍可能在回调里打到已拆掉的 Python 对象。

        所以收敛只发生在 runtime **主动读快照**的地方（:meth:`health` /
        :meth:`status_snapshot` / :meth:`ensure_ready`），见 :meth:`_converge_link_state`。
        """
        if self._state is not EngineState.READY:
            return None
        exc = AdbDisconnected(
            address=self._address,
            why=reason or "link lost (MaaCore reported Disconnect without a reason)",
            adb_path=self._adb_path,
        )
        self._sink_to_loaded(exc)
        logger.info(
            "link down (address=%s, config=%s); state -> loaded", self._address, self._connect_config
        )
        return exc

    def _sink_to_loaded(self, exc: EngineError) -> None:
        """``READY``（或 ``LOADED``）→ ``LOADED`` + 记 ``last_error`` + 丢掉会话缓存。"""
        if self._state not in (EngineState.READY, EngineState.LOADED):
            return
        self._last_error = exc
        self._drop_session()
        if self._state is EngineState.READY:
            self._set_state(EngineState.LOADED)

    def _converge_link_state(self) -> AdbDisconnected | None:
        """惰性收敛：读 adapter 的链路快照，``DISCONNECTED`` 就把 ``READY`` 沉到 ``LOADED``。

        不改会怎样：断线之后 ``_state`` 仍然是 ``READY``，``health()`` / ``status`` 继续
        报"一切就绪"，用户看着"就绪"去点截图，然后等满 8 秒 ``capture_timeout``
        才拿到一句"截图卡住了"—— 而真正的原因（掉线）早就躺在 ``adapter.link_state`` 里。

        为什么"读的时候收敛"而不是"断线时回调"：见 :meth:`note_link_down`。这里额外说明
        **惰性**的代价与为什么可接受：断线到下一次读快照之间，``state`` 仍显示 ``READY``。
        这个窗口不会被误用 —— 任何真的要动设备的路径（``capture()``）第一件事就是
        ``ensure_ready()``，它自己也调本方法，所以"用之前一定已经收敛"。

        Returns:
            下沉时构造的 ``AdbDisconnected``；链路正常 / 已收敛 / 没有 adapter 时为 ``None``。
        """
        adapter = self._adapter
        if adapter is None or self._state is not EngineState.READY:
            return None
        if adapter.link_state is not LinkState.DISCONNECTED:
            return None
        return self.note_link_down(adapter.link_reason)

    # ------------------------------------------------------------------
    # 销毁
    # ------------------------------------------------------------------
    async def destroy(self) -> None:
        """``AsstStop`` → ``AsstDestroy`` → （确认销毁后）``close()``。**永不抛错、可重入。**

        三条 C 侧事实（`[FACT]` 已对 pin 住的 v6.22.0 源码逐条核对
        ``src/MaaCore/AsstCaller.cpp`` / ``Assistant.cpp`` / ``Assistant.h``）：

        ① ``AsstStop(h)`` → ``Assistant::stop(block = true)``：置 ``m_thread_idle``、
           取一次 ``m_mutex``、清 ``m_tasks_list``、清 status 缓存。**不 join 任何线程**，
           所以它今天**不会**长时间阻塞（量级是"抢一次未争用的 mutex + 清两个容器"，
           亚毫秒）。真正会长时间阻塞的只有 ``AsstDestroy``。
           → 把它挪进销毁线程的理由不是"它慢"，而是"销毁路径上不留同步 C 调用"：
             一个子线程、一份超时预算，顺手免疫"某天上游给 stop() 加上 join"。
        ② ``AsstDestroy(h)`` → ``delete h``：``~Assistant()`` 依次 join
           ``m_working_thread`` / ``m_call_thread`` / ``m_msg_thread``，**可能无限阻塞**
           （正在跑的 ``call_proc`` 要等 ADB 超时），且在**函数最后一句**才回调
           ``Destroyed`` —— 即"回调可能来自销毁线程自身"。
        ③ Stop 仍有真实价值：它清空待跑队列，让 ``~Assistant`` 的 join 只等**当前**这一
           条任务，而不是"跑完当前再跑下一条"。

        为此本方法有四个不变量（每一条都对应一个"会崩"的具体场景）：

        * **Stop 与 Destroy 在同一个线程里顺序执行**（:meth:`_teardown_blocking`）。
          分两个线程并发调同一个 handle 是**更糟**的 bug：``delete`` 与 ``stop`` 并发
          = 对同一个 C++ 对象 UAF，比"只慢一点"严重得多。
        * **线程是 daemon + 独立线程，不走 ``asyncio.to_thread``**：``to_thread`` 借的是
          宿主事件循环的默认执行器，一个卡死的 ``AsstDestroy`` 会永久占掉宿主一个
          worker（其它插件跟着排队），并且 ``asyncio.run()`` 收尾时的
          ``shutdown_default_executor()`` 会把它 join 回来 —— 实测"超时 0.1s 的调用"
          让 ``asyncio.run()`` 整整等了 3.01s（[FACT] 本机 Python 3.13.14 实测）。
          daemon 线程不参与这两条 join，退出码不会被一次卡死拖住。
        * **放弃等待时既 detach、也绝不 ``close()``**：``detach_callback()`` 让 C 线程
          落回 trampoline 的空分支（逻辑 UAF 挡住）；而 ``close()`` 会解掉 ``_lib`` 与
          ``add_dll_directory`` 句柄，此刻 C 线程可能还在懒加载依赖。放弃的那一份
          wrapper 进 :attr:`_abandoned_wrappers` 保活 —— 这是**唯一**被允许的有界泄漏，
          而且它有名字、能被 :attr:`lifecycle_stats` 数出来。
        * **``_handle`` 取出后在同一段同步代码里立刻置 0**（取到与置 0 之间没有 await）：
          C 侧 ``AsstDestroy`` 是 ``delete``，delete 两次是 UAF 而不是"多删一个空指针"。
          重复 / 并发调用看到 0 即短路；外层还有 ``_teardown_in_progress`` 挡一次。
        """
        if self._teardown_in_progress:
            # 已经有一次销毁在跑：这一层挡住"并发两次 destroy 各发一次 delete"。
            # 不排队等待是有意的 —— 我们只需要"不重复发 C 调用"，而真正需要等待的
            # 调用方（on_shutdown）本身就是触发它的那一方。
            logger.debug("destroy() re-entered while teardown is in progress; skipping")
            return
        self._teardown_in_progress = True
        try:
            if self._download_task is not None and not self._download_task.done():
                await self.cancel_download()
            adapter, self._adapter = self._adapter, None
            if adapter is not None:
                try:
                    await adapter.aclose()
                except Exception:  # noqa: BLE001
                    logger.debug("adapter aclose failed", exc_info=True)

            wrapper, handle = self._wrapper, self._handle
            self._handle = 0
            abandoned = False
            if wrapper is not None and handle:
                abandoned = not await self._teardown_instance(wrapper, handle)
            if wrapper is not None:
                if abandoned:
                    self._abandoned_wrappers.append(wrapper)
                else:
                    try:
                        wrapper.close()
                    except Exception:  # noqa: BLE001
                        logger.debug("wrapper.close failed", exc_info=True)
            self._wrapper = None
            self._device = None
            self._connected_params = None
            self._resource_loaded = False
        finally:
            executor, self._executor = self._executor, None
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            self._progress = None
            self._download_task = None
            self._teardown_in_progress = False
            self._set_state(EngineState.ABSENT)

    async def _teardown_instance(self, wrapper: MaaCoreWrapper, handle: AsstHandle) -> bool:
        """一个线程、一份预算地跑 ``Stop → Destroy``。**返回"是否确认已销毁"。**

        返回值语义是给 :meth:`destroy` 决定"能不能 ``close()``"用的：
        ``False`` 表示"实例可能还活着、C 线程可能还在跑"，此时解引用库句柄是危险的。

        超时**不取消那个线程**（Python 无法杀线程）：它会在自己返回时安静结束。
        这正是为什么这里用 daemon 线程而不是宿主执行器 —— 见 :meth:`destroy`。
        """
        self._stop_calls += 1
        self._destroy_calls += 1
        future = _spawn_daemon_future(lambda: self._teardown_blocking(wrapper, handle))
        try:
            await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=self._destroy_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            # 放弃等待 = 把实例丢给 C 侧自身析构，而我们这边的 Python 状态已经拆光。
            # 此时 C 线程**仍可能**回调进来（Destroyed 是析构的最后一句），那会打到
            # 已销毁的 adapter/loop 上 —— 逻辑层面的 UAF。
            # 这条必须是 error：它是"已知会崩的窗口"，不是普通告警。
            self._destroy_abandoned += 1
            logger.error(
                "AsstStop/AsstDestroy exceeded %.2fs; abandoning instance. "
                "C threads may still invoke callbacks -> detaching python target "
                "and keeping the library handle alive to avoid use-after-free "
                "(bounded leak by design)",
                self._destroy_timeout,
            )
            self._detach_for_abandon(wrapper)
            return False
        except Exception:  # noqa: BLE001 - shutdown 钩子必须能返回
            # 走到这里的只有"销毁线程抛错"：``destroy_instance`` 唯一的抛错点是
            # ``_require_lib()``（库已被 close），也就是 **AsstDestroy 根本没发出去**。
            # 那就和超时同性质：实例还活着、回调可能还会来。旧实现只记 debug，
            # 等于把"实例没被销毁"这件事静默吞掉（真机上表现成关不掉的后台线程）。
            self._destroy_abandoned += 1
            logger.error(
                "AsstStop/AsstDestroy raised; instance state unknown, treating as abandoned "
                "(detaching python callback target)",
                exc_info=True,
            )
            self._detach_for_abandon(wrapper)
            return False
        return True

    @staticmethod
    def _teardown_blocking(wrapper: MaaCoreWrapper, handle: AsstHandle) -> None:
        """**运行在销毁线程上**：先 Stop，再 Destroy，顺序不可换、不可拆到两个线程。

        ``AsstStop`` 的失败不阻止销毁（销毁优先）；``AsstDestroy`` 的异常向上抛，
        由 :meth:`_teardown_instance` 归入"放弃"分支。
        """
        try:
            wrapper.stop_tasks(handle)
        except Exception:  # noqa: BLE001 - 销毁路径不许因为"停不下来"而中止
            logger.debug("AsstStop before AsstDestroy failed", exc_info=True)
        wrapper.destroy_instance(handle)

    def _detach_for_abandon(self, wrapper: MaaCoreWrapper) -> None:
        """放弃销毁时把 Python 侧回调目标摘成空，**保留 CFUNCTYPE 强引用**（防真 UAF）。

        ``detach_callback`` 在 wrapper 上是可选能力（替身 wrapper 未必实现）：
        探测不到就只留上面那条 error 日志，绝不因为"替身没这个方法"而在关机路径抛错。
        """
        detach = getattr(wrapper, "detach_callback", None)
        if detach is None:
            return
        try:
            detached = detach()
        except Exception:  # noqa: BLE001 - 摘钩子失败也必须继续关机
            logger.debug("detach_callback failed", exc_info=True)
            return
        if detached:
            self._detach_invocations += 1

    async def purge(self) -> None:
        """删除引擎文件（只在 ``arknights_engine_repair --clean`` 时显式调用）。

        "引擎文件"包含**已校验的归档**：它在 ``cache_dir``，与 ``engine_root`` 是两个
        目录，但同样是 295 MB 的占盘。只删解压产物等于 ``--clean`` 名不副实——用户
        disco 满了来清，清完只回收一半。
        """
        await self.destroy()

        def _purge() -> None:
            if self._engine_root.is_symlink():
                self._engine_root.unlink(missing_ok=True)
            elif self._engine_root.exists():
                shutil.rmtree(self._engine_root, ignore_errors=True)
            archive = self._archive_path()
            with contextlib.suppress(OSError):
                archive.unlink(missing_ok=True)
            self._cleanup_partials(archive, None)

        await asyncio.to_thread(_purge)
        self._set_state(EngineState.ABSENT)

    # ------------------------------------------------------------------
    # 下载（线程 + 协作式取消）
    # ------------------------------------------------------------------
    async def _fetch(self, release: ReleaseSpec, archive: Path) -> None:
        loop = asyncio.get_running_loop()
        executor = self._ensure_executor()
        port = self._ports.fetcher

        def on_progress(done: int, total: int | None = None) -> None:
            # ``size`` 是 ``ReleaseSpec`` 的字段；``size_bytes`` 只是兼容别名，不用。
            percent = _percent(done, total, release.size)
            if percent is not None:
                loop.call_soon_threadsafe(self._apply_progress, percent)

        if port is not None:
            future = loop.run_in_executor(executor, port, release, archive, on_progress)
        else:
            cancel = self._cancel_event or threading.Event()
            future = loop.run_in_executor(
                executor, self._fetch_default, release, archive, on_progress, cancel
            )

        wrapped = asyncio.wrap_future(future)
        try:
            await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            if self._cancel_event is not None:
                self._cancel_event.set()
            # 默认真实下载器支持协作式取消：等它收尾，避免它在我们清理之后又写回 .part。
            # 注入的 fake 不保证响应 cancel，就不等（有界泄漏），由 finally 清理兜底。
            if port is None:
                try:
                    await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    logger.debug("download worker did not stop in time", exc_info=True)
            raise

    def _fetch_default(
        self,
        release: ReleaseSpec,
        archive: Path,
        on_progress: Callable[[int, int | None], None],
        cancel: threading.Event,
    ) -> Path:
        """默认真实下载器。URL 与期望哈希**只**来自 ``release``，没有第二个来源。

        ``atomic_download`` 的入参形态是 ``(release, dest, *, on_progress, cancel)``：
        它**不再**接受分开传的 ``url`` / ``sha256`` / ``expected_size``（旧写法会被判成
        ``REJECTED_SCHEME`` 直接拒掉）。这也意味着 ``release.size`` 由它自己读，
        本方法不必再转述一次。
        """
        return atomic_download(
            release,
            archive,
            on_progress=on_progress,
            cancel=cancel,
        )

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="maa-download"
            )
        return self._executor

    def _archive_path(self) -> Path:
        release = self._release
        if release is None:
            return self._cache_dir / "maa-archive.zip"
        return self._cache_dir / release.archive_name

    def _apply_progress(self, percent: int) -> None:
        if self._state is not EngineState.DOWNLOADING:
            return
        self._progress = max(0, min(100, int(percent)))
        self._emit(self._progress, "下载引擎中…")

    # ------------------------------------------------------------------
    # 标记与目录提交
    # ------------------------------------------------------------------
    def _write_marker(
        self,
        root: Path,
        *,
        source: str,
        version: str,
        sha256: str | None,
    ) -> None:
        """原子写 ``engine.json``（临时文件 + ``os.replace``，不要直接 open(...,"w")）。"""
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": source,          # download | manual
            "version": version,        # 手动放置记 "unknown"，不猜
            "sha256": sha256,          # 手动放置为 None
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        fd, temp_name = tempfile.mkstemp(dir=str(root), prefix=".engine-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(temp_name, root / _MARKER_NAME)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(temp_name).unlink(missing_ok=True)
            raise

    def _commit(self, staging: Path) -> None:
        """把解压好的整棵树原子换入 ``engine_root``。

        绝不对链接做替换：``engine_root`` 是 symlink 时直接判布局非法（否则会把用户
        的链接目标整个换掉）。旧目录先改名再换入，失败时回滚。
        """
        root = self._engine_root
        if root.is_symlink():
            raise EngineLayoutInvalid(root=root, missing=(), found=(str(root),))

        backup: Path | None = None
        if root.exists():
            backup = root.with_name(f"{root.name}.old-{os.getpid()}-{id(staging):x}")
            os.replace(root, backup)
        try:
            os.replace(staging, root)
        except OSError as exc:
            if backup is not None and not root.exists():
                with contextlib.suppress(OSError):
                    os.replace(backup, root)
            raise EngineUnpackFailed(
                archive=self._archive_path(),
                target_dir=root,
                kind=UnpackFailure.WRITE_ERROR,
                detail=str(exc),
            ) from exc
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)

    def _staging_path(self) -> Path:
        """与 ``engine_root`` **同级**、名字随机、**事先不存在**的暂存目录路径。

        两条约束缺一不可：

        * 必须同级：同一卷上 ``os.replace`` 才是原子的（跨卷会退化成复制+删除）。
        * 必须**不**存在：``safe_extract`` 用 ``mkdir(exist_ok=False)`` 原子占位，
          已存在的目录一律拒绝（这正是"预放 dest 为符号链接"的唯一挡法）。所以
          这里**不能**用 ``tempfile.mkdtemp()`` —— 它会把目录建出来，紧接着就被
          ``safe_extract`` 判成"解压目标已存在"。

        占位由 ``safe_extract`` 完成，本方法只负责给一个够随机的候选名。
        """
        for _ in range(4):
            candidate = self._engine_root.with_name(
                f"{self._engine_root.name}.unpacking-{secrets.token_hex(6)}"
            )
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise FileExistsError(f"无法分配暂存目录: {self._engine_root}.unpacking-*")

    def _cleanup_partials(self, archive: Path | None, staging: Path | None) -> None:
        """单一清理出口：``.part`` / ``.lock`` / ``.unpacking-*``。成功路径也要跑。"""
        if archive is not None:
            for candidate in (archive.with_name(archive.name + ".part"),
                              archive.with_name(archive.name + ".lock")):
                with contextlib.suppress(OSError):
                    candidate.unlink(missing_ok=True)
            if archive.parent.is_dir():
                for candidate in archive.parent.glob(archive.name + ".*"):
                    if candidate.is_file() and (".part" in candidate.name or ".lock" in candidate.name):
                        with contextlib.suppress(OSError):
                            candidate.unlink(missing_ok=True)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        for parent in (self._engine_root.parent, self._engine_root):
            if not parent.is_dir():
                continue
            for leftover in parent.glob(f"{self._engine_root.name}.unpacking-*"):
                shutil.rmtree(leftover, ignore_errors=True)
            for leftover in parent.glob(f"{self._engine_root.name}.old-*"):
                shutil.rmtree(leftover, ignore_errors=True)

    def _locate_core_library(self, root: Path) -> Path | None:
        """注入式解压器只返回 Path 时的兜底定位（生产路径用 ``ExtractResult.core_lib``）。"""
        names = MaaCoreWrapper.default_library_names()
        found = [
            directory / name
            for directory in (root, root / "bin", root / "lib")
            for name in names
            if (directory / name).is_file()
        ]
        return found[0] if len(found) == 1 else None

    # ------------------------------------------------------------------
    # 状态与工具
    # ------------------------------------------------------------------
    def _set_state(self, new: EngineState) -> None:
        if new is self._state:
            return
        allowed = _TRANSITIONS.get(self._state, frozenset())
        if new not in allowed:
            raise EngineNotReady(state=self._state.value, required=(new.value,))
        self._state = new

    def _is_connected(self) -> bool:
        if self._adapter is not None:
            return self._adapter.is_healthy()
        if self._wrapper is not None and self._handle:
            try:
                return bool(self._wrapper.is_connected(self._handle))
            except MaaCallError:
                return False
        return False

    def _require_wrapper(self) -> MaaCoreWrapper:
        if self._wrapper is None:
            raise EngineLoadFailed(
                library_path=self.layout.library_path,
                symbol=None,
                reason="library is not loaded yet",
            )
        return self._wrapper

    def _emit(self, percent: int, message: str) -> None:
        sink = self._progress_sink
        if sink is None:
            return
        try:
            sink(int(percent), message)
        except Exception:  # noqa: BLE001 - 宿主回调故障不得中断引导
            logger.debug("progress_sink raised", exc_info=True)

    def _loop_hint(self) -> None:
        # 只在 async 语境里被调用；取一次 running loop 以便让"没在事件循环里调 bootstrap"
        # 提前炸成明确错误，而不是在 create_task 里才炸。
        asyncio.get_running_loop()


def _percent(done: int, total: int | None, fallback_total: int) -> int | None:
    limit = total if total and total > 0 else fallback_total
    if not limit or limit <= 0:
        return None
    return max(0, min(99, int(done * 100 / limit)))


def _spawn_daemon_future(func: Callable[[], None]) -> Future[None]:
    """在一条 **daemon** 线程里跑 ``func``，返回可直接 ``asyncio.wrap_future`` 的 Future。

    为什么不用 :func:`asyncio.to_thread`（`[FACT]` 本机 Python 3.13.14 实测）：

    * ``to_thread`` 借的是事件循环的**默认执行器**。``wait_for`` 超时只会取消等待，
      线程继续跑；如果 ``AsstDestroy`` 永不返回，宿主执行器就被永久占掉一个 worker
      （其它插件的 ``to_thread`` 跟着排队）；再叠一次同样的卡死就会继续吃 worker。
    * 更致命的是收尾：``asyncio.run()`` 结束时调 ``loop.shutdown_default_executor()``，
      它**会 join 池里的线程**。实测"超时 0.1s 的 ``to_thread``"让 ``asyncio.run()``
      整整等了 3.01s（= 那次调用的真实耗时）。也就是说 ``wait_for`` 给了我们一个
      "以为已经放弃了"的假象，宿主关停时照样被拖住。
    * ``concurrent.futures`` 的 worker 是**非 daemon** 且注册了 ``threading._register_atexit``，
      进程退出时会 join 它们 —— 一次真卡死可以让进程关不掉。

    daemon 线程不参与上述任何 join：超时之后它就自顾自跑完（或跑不完），
    而关机路径已经返回了。代价是"进程退出时可能有一条线程被强行掐断在 C 里"，
    但那时进程本来就在退出，比"关不掉"好。

    异常会被搬进 Future（而不是打在日志里丢掉），这样"销毁线程抛错"能被
    :meth:`MaaRuntime._teardown_instance` 当成"实例状态未知"来处理。
    """
    future: Future[None] = Future()

    def _runner() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            func()
        except BaseException as exc:  # noqa: BLE001 - 线程里不许有未捕获异常
            future.set_exception(exc)
        else:
            future.set_result(None)

    threading.Thread(target=_runner, name="maa-destroy", daemon=True).start()
    return future



def _connect_request(
    *,
    address: str,
    adb_path: str,
    config: str,
    extras: Mapping[str, str] | None,
) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    """一次连接请求的**可比较指纹**：四个参数，``extras`` 归一成排序后的键值对。

    ``extras`` 必须排序：``dict`` 的迭代顺序取决于插入顺序，而同一个配置解析两次
    得到的键序理论上可以不同（配置来自外部、可能经 JSON/TOML 两次解析）。
    不排序就会把"同一个请求"比成不同 —— 表现为每次重连都真重连，幂等短路形同虚设。
    """
    return (address, adb_path, config, tuple(sorted((extras or {}).items())))


def _symbol_from(exc: BaseException) -> str | None:
    """从 ``AttributeError("MaaCore is missing symbol: AsstX")`` 里取出符号名。"""
    text = str(exc)
    marker = "missing symbol:"
    if marker in text:
        return text.split(marker, 1)[1].strip() or None
    return None


def _platform_key() -> str:
    """``windows-x64`` / ``linux-x86_64`` / ``macos-universal`` —— 与 ``KNOWN_RELEASES`` 同词表。"""
    bits = "x64" if struct.calcsize("P") == 8 else "x86"
    platform = MaaCoreWrapper.platform()
    if platform is MaaPlatform.WINDOWS:
        return f"windows-{bits}"
    if platform is MaaPlatform.DARWIN:
        return "macos-universal"
    return f"linux-{bits}"
