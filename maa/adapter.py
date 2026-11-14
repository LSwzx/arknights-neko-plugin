"""把 MaaCore 的 C 线程回调桥接成 asyncio 可 ``await`` 的对象。

职责边界
--------
* 只做「线程 → 事件循环」与「call_id → Future」两件事；
* 所有超时、取消、迟到事件的处置都在本类，外部看不到线程；
* **不推进 EngineState，不出文案，不碰 ctypes**（C 调用全部经由 ``wrapper``）；
* 只读白名单的第一层守卫在本类（runtime 再防一层）。

三条结构性事实（决定了本文件的形状）
------------------------------------
1. `[FACT]` 回调运行在 MaaCore 的 C++ 线程（``Assistant::msg_proc``）上，不是我们的线程，
   也不属于任何事件循环。跨线程唯一被文档化的入口是 ``loop.call_soon_threadsafe()``。
   因此 :meth:`MaaAdapter._on_thread_event` 只做「解析 → 入队 → 敲门」，
   **绝不** ``await`` / 建 Task / 碰 ``asyncio.Queue`` / ``Future.set_result``。
2. `[FACT]` ``AsyncCallInfo`` 自带 ``async_call_id``，所以按 id 精确派发；查不到的 id
   一律计 ``events_dropped_late`` 并丢弃。这条把"迟到回调覆盖上一次失败原因"
   从"靠版本号猜谁权威"变成了"结构上写不进去"。
3. `[FACT]` 连接终态的权威判据是 ``AsyncCallInfo{what:"Connect"}.details.ret``，
   不是 ``ConnectionInfo{what:"Connected"}``：``Controller::connect()`` 只有在
   ``create_controller`` 成功**并成功截过一张图**之后才返回 true，而 ``Connected``
   只是过程中的一条通知，顺序不保证。``ResolutionGot`` / ``ResolutionError`` /
   ``UuidGot`` 只记录，不参与终态判定。

分辨率三态
----------
``KNOWN``（事件给了可信 w/h）/ ``FAILED``（``ResolutionError``）/ ``UNKNOWN``（还没到）。
**绝不用 0×0 或 1280×720 填默认值**：那会让"没读到"和"真是这个尺寸"不可区分，
后续所有坐标都会错。``Resolution`` 在构造期就禁止这种填充。
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from .errors import (
    AdbAddressMissing,
    AdbConnectTimeout,
    AdbDisconnected,
    CaptureEmpty,
    CaptureFailed,
    CaptureFormatUnexpected,
    CaptureTimeout,
    EngineError,
    EngineNotReady,
    TaskNotAllowed,
)
from .wrapper import (
    PNG_SIGNATURE,
    AsstHandle,
    AsyncCallId,
    MaaCallError,
    MaaCallStatus,
    MaaCoreWrapper,
)

logger = logging.getLogger(__name__)

__all__ = [
    "READONLY_TASK_TYPES",
    "ALLOWED_DEBUG_MODES",
    "ResolutionStatus",
    "Resolution",
    "DeviceProfile",
    "MaaEvent",
    "EventHandler",
    "UNSUBSCRIBE",
    "AdapterStats",
    "MaaAdapter",
]

READONLY_TASK_TYPES: Final[frozenset[str]] = frozenset({
    "Screencap", "OperBox", "Depot", "Debug",
})
"""只读白名单：**唯一**定义处。runtime 从这里 import（adapter 不 import runtime，故无环）。

放 adapter 而不是 runtime 的理由：adapter 是唯一真正发号的地方（``AsstAppendTask``
的调用点），白名单贴着调用点最不容易漂移；runtime 再 import 一次做第二层防御。

**``Recruit`` 不在这里**：``Recruit`` 任务的参数可触发游戏内确认动作，属于"能改行为"
的一类，按"初版绝不执行写操作"的硬要求，**拒绝是唯一安全默认**（W26 也把它列在写任务表里）。
待主理人/安全侧定稿后再决定是否放回。
"""

ALLOWED_DEBUG_MODES: Final[frozenset[str]] = frozenset({"ocr"})
"""``Debug`` 任务只允许 OCR 模式。其它 mode 可能触发游戏内交互，必须拒。"""

STATE_ABSENT: Final[str] = "absent"
"""``EngineState`` 七态之一（adapter 不能 import runtime：runtime 会 import adapter，成环）。

`EngineNotReady.state` 只允许七态字面量：``absent / downloading / verified / unpacked /
loaded / ready / failed``。这里的值与 ``EngineState.ABSENT.value`` 必须相等，
由 X 组的元用例守（两处都是全小写，任何大小写漂移都会被断言抓住）。"""

EventHandler = Callable[["MaaEvent"], None]
"""在事件循环线程上被调用；实现方禁止阻塞。"""

UNSUBSCRIBE = Callable[[], None]

_MAX_INBOX = 4096
"""回调缓冲上界。满了丢最旧的（不入业务判断，只保证内存有界）。"""


class _ResolutionInvariantError(AssertionError, ValueError):
    """``Resolution`` 构造期不变量的违反。

    同时继承 ``AssertionError`` 与 ``ValueError``：架构 §3.3 写的是"违反即 ValueError"，
    测试计划 A05 要求"必须 AssertionError"。两个都是"构造期不变量"的合法表达，
    这里用双继承让**两种断言写法都能接住**，而不是让文档与用例二选一。
    """


class ResolutionStatus(enum.Enum):
    """分辨率**只有三种**状态，没有第四种。

    旧实现用默认 1280×720 兜底，导致"没读到分辨率"和"分辨率真是 1280×720"不可区分，
    状态页显示"已获取"而后续坐标全错。三态让"不知道"变成一个**能说出来**的值。
    """

    KNOWN = "known"      # 收到 ResolutionGot，w/h 可信
    FAILED = "failed"    # 收到 ResolutionError（MAA 此时给 width=height=0）
    UNKNOWN = "unknown"  # 事件还没到（不是失败，只是没到）


@dataclass(frozen=True, slots=True)
class Resolution:
    """分辨率快照。构造期校验，非法组合**写不出来**。

    不变量：
      * ``status is KNOWN`` ⇒ ``width``/``height`` 均为正整数；
      * ``status is not KNOWN`` ⇒ ``width is height is None``（**禁止**用 0 或默认值占位）。
    """

    status: ResolutionStatus
    width: int | None = None
    height: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status is ResolutionStatus.KNOWN:
            if not (_is_positive_int(self.width) and _is_positive_int(self.height)):
                raise _ResolutionInvariantError(
                    f"KNOWN resolution requires positive width/height, got {self.width!r}x{self.height!r}"
                )
        elif self.width is not None or self.height is not None:
            raise _ResolutionInvariantError(
                f"{self.status.value} resolution must not carry width/height, got {self.width!r}x{self.height!r}"
            )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """一次连接的结果。

    ``resolution`` 是三态（绝不用默认值填）；``reason`` 只在失败时有意义；
    ``call_id`` 用来在日志里对上"哪一次 Connect"。
    """

    connected: bool
    uuid: str
    resolution: Resolution
    reason: str = ""
    call_id: int = 0


@dataclass(frozen=True, slots=True)
class MaaEvent:
    """一条已解析的回调事件。``details`` 是内层 ``details`` 对象（可能为空）。"""

    msg: int
    what: str | None
    async_call_id: int | None
    details: Mapping[str, Any]
    uuid: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AdapterStats:
    events_received: int
    events_dropped_late: int      # 迟到的、call_id 已不在挂起表的
    events_dropped_broken: int    # JSON 解析失败/非 dict/无载荷
    pending_calls: int
    loop_binds: int
    resolution_events: int        # ResolutionGot + ResolutionError 合计


@dataclass(slots=True)
class _CallSlot:
    call_id: AsyncCallId
    what: str
    future: asyncio.Future[Mapping[str, Any]]
    deadline: float
    abandoned: bool = False


class MaaAdapter:
    """把 MaaCore 的 C 线程回调桥接成 asyncio 可 await 的对象。

    Args:
        wrapper: 纯 ctypes 绑定层（本类不直接碰 ``ctypes``）。
        handle: ``create_instance()`` 得到的实例句柄；0 表示还没起来。
        connect_timeout: 等 ``AsyncCallInfo(Connect)`` 的上限（秒）。
        capture_timeout: 等 ``AsyncCallInfo(Screencap)`` 的上限（秒）。
        resolution_grace: 连接成功后，额外等分辨率事件的宽限（秒）；等不到就用 ``UNKNOWN``。
        max_pending_calls: 同时挂起的 async call 上限，超过即拒绝（不排队）。
    """

    def __init__(
        self,
        *,
        wrapper: MaaCoreWrapper,
        handle: AsstHandle,
        connect_timeout: float,
        capture_timeout: float,
        resolution_grace: float,
        max_pending_calls: int = 32,
    ) -> None:
        self._wrapper = wrapper
        self._handle = int(handle or 0)
        self._connect_timeout = float(connect_timeout)
        self._capture_timeout = float(capture_timeout)
        self._resolution_grace = float(resolution_grace)
        self._max_pending_calls = int(max_pending_calls)

        self._lock = threading.Lock()
        self._inbox: deque[MaaEvent] = deque(maxlen=_MAX_INBOX)
        self._pending: dict[AsyncCallId, _CallSlot] = {}
        self._handlers: list[EventHandler] = []

        self._loop: asyncio.AbstractEventLoop | None = None
        self._resolution_event: asyncio.Event | None = None

        self._resolution = Resolution(ResolutionStatus.UNKNOWN)
        self._uuid = ""
        self._connect_failed_why = ""
        self._address = ""
        self._adb_path = ""

        self._dispatch_errors = 0
        self._events_received = 0
        self._dropped_late = 0
        self._dropped_broken = 0
        self._loop_binds = 0
        self._resolution_events = 0

        # 最近一次由**本次调用**记录下来的失败。只读快照；迟到事件永远到不了这里
        # （它们在 _dispatch 里按 call_id 就被丢弃了）。
        self._last_error: EngineError | None = None
        self._error_seq = 0

    # --- 生命周期 ---
    def bind_loop(self) -> asyncio.AbstractEventLoop:
        """绑定/重绑当前事件循环。每个公开 async 方法的**第一条语句**都调用它。

        重绑规则：若 ``asyncio.get_running_loop()`` 与缓存 loop 不同，则换绑并递增
        ``stats.loop_binds``；**已挂起的 Future 不迁移**（跨 loop 的 Future 不可用），
        其 ``_CallSlot.abandoned`` 置 True，并尽力把 ``AdbDisconnected`` 送到旧等待者。
        """
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            self._drain_pending_inbox(loop)
            return loop

        previous, self._loop = self._loop, loop
        self._loop_binds += 1
        self._resolution_event = asyncio.Event()
        if self._resolution.status is not ResolutionStatus.UNKNOWN:
            self._resolution_event.set()
        if previous is not None:
            self._abandon_pending(previous)
        self._drain_pending_inbox(loop)
        return loop

    def _drain_pending_inbox(self, loop: asyncio.AbstractEventLoop) -> None:
        """若 C 线程在 loop 未绑定期间攒了事件，补一次敲门（不丢数据）。"""
        with self._lock:
            has_events = bool(self._inbox)
        if has_events:
            loop.call_soon(self._dispatch)

    def _abandon_pending(self, previous: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot.abandoned = True
            if slot.future.done():
                continue
            try:
                slot.future.set_exception(
                    AdbDisconnected(
                        address=self._address, why="event loop rebound", adb_path=self._adb_path
                    )
                )
            except RuntimeError:
                # 旧 loop 已关闭：连 call_soon 都进不去，只能取消，等超时兜底。
                slot.future.cancel()

    def on_event(self, handler: EventHandler) -> UNSUBSCRIBE:
        """订阅全部事件（供 runtime 观察 InternalError / InitFailed / Destroyed）。"""
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._handlers.remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    async def aclose(self) -> None:
        """取消全部挂起 Future、清空队列。**不**调 ``AsstDestroy``（那是 runtime 的事）。"""
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
            self._inbox.clear()
            self._handlers.clear()
        for slot in slots:
            slot.abandoned = True
            if not slot.future.done():
                slot.future.cancel()

    # --- 连接 ---
    async def connect(
        self,
        *,
        address: str,
        adb_path: str,
        config: str = "General",
    ) -> DeviceProfile:
        """发起异步连接并等待**终态**。

        判据顺序（刻意如此）：

          1. ``address.strip() == ""`` → 立刻抛 ``AdbAddressMissing``（**不发任何 C 调用**）；
          2. 句柄为 0 → ``EngineNotReady``（没实例就没得连，不是"连不上"）；
          3. ``wrapper.async_connect(...)`` 返回 0 → ``AdbDisconnected``；
          4. 等 ``AsyncCallInfo{what:"Connect", async_call_id==id}``：
             ``details.ret is True`` → 连接成功（**终态判据**）；
             ``details.ret is False`` → ``AdbDisconnected(why=<error>)``；
          5. 超 ``connect_timeout`` 未见 AsyncCallInfo → ``AdbConnectTimeout``；
          6. 成功后再在 ``resolution_grace`` 内等分辨率事件，结果填进 ``DeviceProfile``：
             ``ResolutionGot`` → ``KNOWN``；``ResolutionError`` → ``FAILED``；
             两个都没来 → ``UNKNOWN``。**三种都不是连接失败**，绝不因此 raise。

        ``[FACT]`` ``AsyncCallInfo(Connect).ret == True`` 只证明"连上且首次截图成功"，
        **不证明分辨率已回填** —— 没有任何 C API 能读 ``get_scale_size()``，
        所以除了事件没有第二条获取途径；本方法也不把 ret 当分辨率来源。

        Raises:
            AdbAddressMissing, AdbDisconnected, AdbConnectTimeout, EngineNotReady
        """
        self.bind_loop()
        text = (address or "").strip()
        if not text:
            raise self._fault(AdbAddressMissing())
        if not self._handle:
            raise self._fault(EngineNotReady(state=STATE_ABSENT, required=("handle",)))

        self._address = text
        self._adb_path = adb_path
        self._reset_session()

        try:
            call_id = self._wrapper.async_connect(
                self._handle, adb_path=adb_path, address=text, config=config
            )
        except MaaCallError as exc:
            raise self._fault(self._translate(exc, call_id=0)) from exc

        slot = self._register(call_id, "Connect", self._connect_timeout)
        details = await self._await_call(
            slot,
            on_timeout=lambda: AdbConnectTimeout(
                address=text, timeout_s=self._connect_timeout, adb_path=adb_path
            ),
        )

        if details.get("ret") is not True:
            why = self._failure_reason(details)
            raise self._fault(AdbDisconnected(address=text, why=why, adb_path=adb_path))

        resolution = await self._await_resolution(self._resolution_grace)
        return DeviceProfile(
            connected=True,
            uuid=self._uuid,
            resolution=resolution,
            reason="",
            call_id=call_id,
        )

    def _reset_session(self) -> None:
        """每次 Connect 都是新会话：分辨率/uuid/失败原因都必须从头来。"""
        self._resolution = Resolution(ResolutionStatus.UNKNOWN)
        self._uuid = ""
        self._connect_failed_why = ""
        self._resolution_event = asyncio.Event()

    async def wait_resolution(self, *, timeout: float) -> Resolution:
        """等待分辨率落定。

        返回三态；**超时返回 ``Resolution(UNKNOWN)``，不抛错** —— "没读到分辨率"
        是事实，不是异常。若已有 ``FAILED`` 记录则直接返回它，不重复等。
        """
        self.bind_loop()
        return await self._await_resolution(timeout)

    def resolution(self) -> Resolution:
        """同步读当前分辨率快照（不等待）。供 ``runtime.health()`` 用。"""
        return self._resolution

    # --- 捕获 ---
    async def capture(self) -> bytes:
        """完整一次截图：发号 → 等 AsyncCallInfo(Screencap) → ``AsstGetImage`` → 校验魔数。

        返回 **PNG 原样字节**；不做任何二次编码。

        Raises:
            EngineNotReady: 句柄为空（实例没起来）。
            AdbDisconnected: ``async_screencap`` 返回 InvalidId(0)。
            CaptureTimeout: 超过 ``capture_timeout`` 未见 AsyncCallInfo。
            CaptureFailed: ``AsyncCallInfo.ret != True``，或拿图时缓冲不足/被拒。
            CaptureEmpty: ``AsstGetImage`` 返回 0（`[INFERENCE]` 等价于未连接）。
            CaptureFormatUnexpected: 首 8 字节不是 PNG 签名（**绝不兜底重编码**）。
        """
        self.bind_loop()
        call_id = await self._trigger_screencap()
        return self._fetch_image(call_id)

    async def screencap_only(self) -> None:
        """只触发截图、不取图（供"预热/探活"用，避免多一次 imencode）。

        仍然等 ``AsyncCallInfo(Screencap)``：不等就会留下一个无人认领的
        ``async_call_id``，被 ``_dispatch`` 记成"迟到事件"，把真实统计污染成噪声。
        """
        self.bind_loop()
        await self._trigger_screencap()

    async def _trigger_screencap(self) -> AsyncCallId:
        if not self._handle:
            raise self._fault(EngineNotReady(state=STATE_ABSENT, required=("handle",)))
        try:
            call_id = self._wrapper.async_screencap(self._handle)
        except MaaCallError as exc:
            raise self._fault(self._translate(exc, call_id=0)) from exc

        slot = self._register(call_id, "Screencap", self._capture_timeout)
        details = await self._await_call(
            slot,
            on_timeout=lambda: CaptureTimeout(
                timeout_s=self._capture_timeout, address=self._address, call_id=call_id
            ),
        )
        if details.get("ret") is not True:
            cost = details.get("cost")
            raise self._fault(
                CaptureFailed(
                    call_id=call_id,
                    ret=False,
                    cost_ms=float(cost) if isinstance(cost, (int, float)) else None,
                )
            )
        return call_id

    def _fetch_image(self, call_id: AsyncCallId) -> bytes:
        try:
            data = self._wrapper.get_image(self._handle)
        except MaaCallError as exc:
            raise self._fault(self._translate(exc, call_id=call_id)) from exc
        if len(data) < len(PNG_SIGNATURE) or not data.startswith(PNG_SIGNATURE):
            # 原始字节必须随异常带出：service 负责落盘（adapter 不做文件 I/O）。
            # 为什么不在这里落盘：本类没有 cache_dir，加参数就是改 §3.3 的构造契约。
            raise self._fault(
                CaptureFormatUnexpected(magic=data[:8], length=len(data), payload=data)
            )
        return data

    # --- 任务发号（只读白名单第一层，**先校验后触碰 wrapper**） ---
    async def append_readonly_task(self, task_type: str, *, mode: str | None = None) -> int:
        """追加一个只读任务并返回 task id（``0`` 表示 C 侧拒绝）。

        守卫必须在**任何 wrapper 调用之前**：写任务（``Fight`` / ``Infrast`` /
        ``Recruit`` / ``Mall`` / ``Award`` / ``Roguelike`` / ``Copilot`` …）一律
        ``TaskNotAllowed``；``Debug`` 只放行 ``mode="ocr"``。这条是本项目唯一
        "错了会越过安全边界"的地方，所以拒绝时 fake lib 的调用计数必须仍是 0。

        本方法**只 append、不 start**：启动时机属于调用方（真跑起来之前还要过
        runtime 的第二层白名单）。
        """
        self.bind_loop()
        # ⚠️ 下面是安全边界：不得下移到 wrapper 或"先 append 再检查"。
        allowlist = tuple(sorted(READONLY_TASK_TYPES))
        if task_type not in READONLY_TASK_TYPES:
            raise self._fault(TaskNotAllowed(task_type=task_type, allowlist=allowlist))
        if task_type == "Debug" and mode not in ALLOWED_DEBUG_MODES:
            raise self._fault(TaskNotAllowed(task_type=task_type, allowlist=allowlist))
        if not self._handle:
            raise self._fault(EngineNotReady(state=STATE_ABSENT, required=("handle",)))

        params = json.dumps({"mode": mode}) if task_type == "Debug" else "{}"
        return self._wrapper.append_task(self._handle, task_type, params)

    # --- 健康与统计 ---
    def is_healthy(self) -> bool:
        """``wrapper.is_connected(handle)`` —— 同步、无锁、不依赖事件。

        `[FACT]` ``AsstConnected`` 返回 ``Assistant::connected()``，即 controller
        存在且已初始化；这是**不依赖回调**的权威探针。
        """
        if not self._handle:
            return False
        try:
            return bool(self._wrapper.is_connected(self._handle))
        except MaaCallError:
            logger.debug("is_connected probe failed", exc_info=True)
            return False

    def stats(self) -> AdapterStats:
        with self._lock:
            pending = len(self._pending)
            return AdapterStats(
                events_received=self._events_received,
                events_dropped_late=self._dropped_late,
                events_dropped_broken=self._dropped_broken,
                pending_calls=pending,
                loop_binds=self._loop_binds,
                resolution_events=self._resolution_events,
            )

    @property
    def last_error(self) -> EngineError | None:
        """最近一次**由本对象记录**的失败；只读快照。

        只有"正在等结果的那次调用"会写它（``_fault``）。迟到事件在 ``_dispatch``
        里按 call_id 就被丢弃，走不到这里 —— 这正是上一版"孤儿线程覆盖 last_error"
        的结构性修复。
        """
        return self._last_error

    @property
    def dispatch_errors(self) -> int:
        """派发期被吞掉的异常数（K4）：订阅方抛错不打断派发，但必须可观测。"""
        return self._dispatch_errors

    @property
    def error_seq(self) -> int:
        """单调递增的失败序号（仅递增，不参与"谁更权威"的比较）。

        存在意义是让"这次失败是哪一次提交的"在日志/测试里可对账；
        协程之间的顺序由 asyncio 保证，不需要版本号比对。
        """
        return self._error_seq

    # --- 内部（仅供单测断言，前缀下划线） ---
    def _on_thread_event(
        self,
        msg: int,
        details_json: str | bytes | None,
        custom_arg: int | None,
    ) -> None:
        """**运行在 MaaCore 的 C++ 线程上**。只做三件事：

        ① ``json.loads``（失败 → 计 ``dropped_broken`` 并 return，**绝不抛**）；
        ② 追加到线程安全缓冲（deque + Lock）；
        ③ ``loop.call_soon_threadsafe(self._dispatch)`` 唤醒（loop 未绑定/已关闭时
           把事件留在缓冲里，等下一次 ``bind_loop()`` 补敲门）。
        """
        if not details_json:
            with self._lock:
                self._dropped_broken += 1
            return
        try:
            raw = json.loads(details_json)
        except Exception:  # noqa: BLE001 - C 线程里抛异常 = 未定义行为
            with self._lock:
                self._dropped_broken += 1
            return
        if not isinstance(raw, dict):
            with self._lock:
                self._dropped_broken += 1
            return

        details = raw.get("details")
        if not isinstance(details, dict):
            details = {}
        raw_id = raw.get("async_call_id")
        event = MaaEvent(
            msg=int(msg),
            what=raw.get("what") if isinstance(raw.get("what"), str) else None,
            async_call_id=raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None,
            details=details,
            uuid=raw.get("uuid") if isinstance(raw.get("uuid"), str) else None,
            raw=raw,
        )
        with self._lock:
            self._inbox.append(event)
            self._events_received += 1
            loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch)
        except RuntimeError:
            # loop 已关闭：事件仍在 deque 里，不会丢；下一次 bind_loop 补敲门。
            logger.debug("event loop is closed; event kept in inbox", exc_info=True)

    def _dispatch(self) -> None:
        """**运行在事件循环线程上**。按 ``async_call_id`` 派发挂起 Future，
        再广播给 on_event 订阅者。未知 call_id → 计 ``dropped_late`` 并丢弃。"""
        while True:
            with self._lock:
                if not self._inbox:
                    return
                event = self._inbox.popleft()

            try:
                self._dispatch_one(event)
            except BaseException:  # noqa: BLE001 - K4：派发循环不得被任何订阅方打断
                self._dispatch_errors += 1
                logger.debug("dispatch failed for event %r", event.what, exc_info=True)

    def _dispatch_one(self, event: MaaEvent) -> None:
        """处理单条事件：先按 id 派发 Future（未命中即迟到），再记事实、再广播。"""
        late = False
        if event.async_call_id is not None:
            with self._lock:
                slot = self._pending.pop(event.async_call_id, None)
            if slot is None or slot.abandoned or slot.future.done():
                late = True
                with self._lock:
                    self._dropped_late += 1
            elif not slot.future.done():
                slot.future.set_result(event.details)

        self._observe(event)
        if late:
            logger.debug("dropped late async event id=%s", event.async_call_id)
            return
        self._broadcast(event)

    def _observe(self, event: MaaEvent) -> None:
        """记录"只该记、不该判"的事实：uuid 与分辨率三态。

        **不做终态判定**：终态只有 ``AsyncCallInfo.ret`` 一条路。
        """
        if event.uuid and not self._uuid:
            self._uuid = event.uuid

        what = event.what
        if what == "ResolutionGot":
            width, height = _resolution_dims(event.details)
            resolution: Resolution
            if _is_positive_int(width) and _is_positive_int(height):
                resolution = Resolution(ResolutionStatus.KNOWN, width, height)
            else:
                # 形状不对就当失败记录，绝不用 0×0 造一个"已知"。
                resolution = Resolution(
                    ResolutionStatus.FAILED,
                    reason=str(event.raw.get("why") or "ResolutionGot without positive width/height"),
                )
            self._set_resolution(resolution)
        elif what == "ResolutionError":
            reason = str(
                event.raw.get("why")
                or event.details.get("why")
                or event.details.get("error")
                or "Get resolution failed"
            )
            self._set_resolution(Resolution(ResolutionStatus.FAILED, reason=reason))
        elif what == "UuidGot":
            uuid = event.details.get("uuid") or event.raw.get("uuid")
            if isinstance(uuid, str) and uuid:
                self._uuid = uuid
        elif what == "ConnectFailed":
            why = event.raw.get("why") or event.details.get("why")
            if isinstance(why, str) and why:
                self._connect_failed_why = why

    def _set_resolution(self, resolution: Resolution) -> None:
        self._resolution = resolution
        self._resolution_events += 1
        event = self._resolution_event
        if event is not None and not event.is_set():
            event.set()

    def _broadcast(self, event: MaaEvent) -> None:
        with self._lock:
            handlers = tuple(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except BaseException:  # noqa: BLE001 - 订阅方故障不得打断事件派发（K4）
                self._dispatch_errors += 1
                logger.debug("event handler raised", exc_info=True)

    def _register(self, call_id: AsyncCallId, what: str, timeout: float) -> _CallSlot:
        loop = self._loop
        if loop is None:  # bind_loop() 已保证，防御性兜底
            raise self._fault(EngineNotReady(state=STATE_ABSENT, required=("event_loop",)))
        with self._lock:
            if len(self._pending) >= self._max_pending_calls:
                raise self._fault(
                    AdbDisconnected(
                        address=self._address,
                        why=f"too many pending async calls (max_pending_calls={self._max_pending_calls})",
                        adb_path=self._adb_path,
                    )
                )
            slot = _CallSlot(
                call_id=call_id,
                what=what,
                future=loop.create_future(),
                deadline=loop.time() + max(float(timeout), 0.0),
            )
            self._pending[call_id] = slot
        return slot

    async def _await_call(
        self,
        slot: _CallSlot,
        *,
        on_timeout: Callable[[], EngineError],
    ) -> Mapping[str, Any]:
        """等一次 async call 的终态；超时/取消时把 slot 标记 abandoned 并**必定**摘除。

        ``asyncio.shield`` 保证外层取消不会连带取消 slot.future（否则 ``_dispatch``
        里 ``set_result`` 会撞上已取消的 Future）。超时后 future 还活着但没人等，
        于是**迟到事件只会走"查不到 id"那条路**，不可能覆盖任何共享状态。
        """
        loop = self._loop
        assert loop is not None
        remaining = max(slot.deadline - loop.time(), 0.0)
        try:
            return await asyncio.wait_for(asyncio.shield(slot.future), remaining)
        except (asyncio.TimeoutError, TimeoutError):
            slot.abandoned = True
            raise self._fault(on_timeout()) from None
        except asyncio.CancelledError:
            slot.abandoned = True
            raise
        finally:
            with self._lock:
                if self._pending.get(slot.call_id) is slot:
                    self._pending.pop(slot.call_id, None)

    async def _await_resolution(self, timeout: float) -> Resolution:
        """在三态上等一个结果；**超时返回 UNKNOWN，不抛错**。"""
        if self._resolution.status is not ResolutionStatus.UNKNOWN:
            return self._resolution
        event = self._resolution_event
        if event is None or timeout <= 0:
            return self._resolution
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return self._resolution
        return self._resolution

    def _failure_reason(self, details: Mapping[str, Any]) -> str:
        for key in ("error", "why", "message"):
            value = details.get(key)
            if isinstance(value, str) and value:
                return value
        return self._connect_failed_why or "AsyncCallInfo(Connect) returned ret=false"

    def _translate(self, exc: MaaCallError, *, call_id: AsyncCallId) -> EngineError:
        """把 wrapper 的机械异常翻译成业务异常。**不允许 MaaCallError 越过本类。**"""
        address = self._address
        if exc.status is MaaCallStatus.EMPTY:
            buffer_size = exc.detail.get("buffer_size")
            return CaptureEmpty(
                buffer_size=int(buffer_size) if isinstance(buffer_size, int) else 0,
                address=address,
            )
        if exc.status is MaaCallStatus.NULL_SIZE:
            # 缓冲不足 / 句柄失效 / 未 inited 在 C 侧是同一个哨兵，不猜。
            # 特别注意：**绝不能**报成 AdbDisconnected（旧实现就这么错，害用户去查端口）。
            return CaptureFailed(call_id=call_id, ret=False, cost_ms=None)
        return AdbDisconnected(
            address=address,
            why=f"{exc.api} -> {exc.status.value} {exc.detail}".strip(),
            adb_path=self._adb_path,
        )

    def _fault(self, exc: EngineError) -> EngineError:
        """记录（单调序号 + 只读快照）并返回异常，便于 ``raise self._fault(...)``。"""
        self._error_seq += 1
        self._last_error = exc
        return exc


def _resolution_dims(details: Mapping[str, Any]) -> tuple[Any, Any]:
    """``ResolutionGot`` 的宽高。`[FACT]` MAA 把它放在内层 ``details`` 里；
    部分版本平铺在同一层，两种都认（只认嵌套会让默认值悄悄留着）。"""
    nested = details.get("details")
    if isinstance(nested, Mapping):
        return nested.get("width"), nested.get("height")
    return details.get("width"), details.get("height")
