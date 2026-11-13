"""引擎侧异常体系：稳定错误码（``ErrorCode``）+ 带结构化上下文的异常族。

设计边界（不可违反）
--------------------
1. **零外部依赖、零包内依赖**：本模块只 import ``enum`` / ``pathlib`` / ``typing`` /
   ``collections.abc``。它被 ``wrapper`` / ``provision`` / ``runtime`` / ``adapter`` /
   ``companion.service`` **同时**依赖，任何一条反向 import 都会立刻成环。
2. **不放用户可见文案**：``message`` 只给日志看。用户看到的每一句话由
   ``companion/service.py`` 按 ``ErrorCode``（以及 ``kind``）查表生成（ADR-003）。
3. **不使用继承层级表达家族**：家族用码集合 ``PROVISION_CODES`` / ``CAPTURE_CODES``
   判定（``exc.code in PROVISION_CODES``），不要 ``isinstance``。家族会增员，
   码集合只加一行，继承树每加一个码都要改一次。
4. **构造参数一律关键字、一律显式**：漏传在写代码时就变成 ``TypeError``，
   而不是在用户面前变成一个缺字段的兜底文案。
5. ``__init__`` 内不做任何 I/O。

上下文与 ``to_dict()``
----------------------
``context`` 是只读映射，保留写入时的原值（``Path`` 仍是 ``Path``，``kind`` 仍是枚举），
便于测试直接 ``exc.context["target"] == p`` / ``exc.kind is DownloadFailure.NO_SPACE``。
``to_dict()`` 负责把同一份数据转成 JSON 友好的形态（``Path`` → ``str``，
``Enum`` → ``.value``，``bytes`` → ``.hex()``，``Sequence`` → ``list``），
供 entry 的 ``data`` 字段与日志使用。

``str(exc)`` 与隐私（K9）
-------------------------
``str(exc)`` 会被用户粘进 issue 与聊天窗，所以它**只渲染** ``code`` + 白名单标量字段
（``type(exc)._DISPLAY_FIELDS``），**不含**绝对路径、用户名、MAA 原文。
``message`` 仍是给日志的长句（含路径），因此**不要**把 ``exc.message`` 透给模型或用户；
要全量诊断请用 ``to_dict()``。判据不能靠"记得别打路径"，只能靠"路径字段不在白名单里"——
``_display_scalar`` 对 ``Path`` 只会渲染 ``<path>``，写错了也泄漏不出去。
"""

from __future__ import annotations

import enum
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

__all__ = [
    "ErrorCode",
    "EngineError",
    "DownloadFailure",
    "UnpackFailure",
    "PROVISION_CODES",
    "CAPTURE_CODES",
    "RESOURCE_CODES",
    "EngineDownloadFailed",
    "EngineHashMismatch",
    "EngineUnpackFailed",
    "EngineLayoutInvalid",
    "PlatformUnsupported",
    "EngineLoadFailed",
    "ResourceMissing",
    "ResourceLoadRejected",
    "InstanceCreateFailed",
    "AdbAddressMissing",
    "AdbConnectTimeout",
    "AdbDisconnected",
    "CaptureTimeout",
    "CaptureFailed",
    "CaptureEmpty",
    "CaptureFormatUnexpected",
    "TaskNotAllowed",
    "EngineNotReady",
]


# ---------------------------------------------------------------------------
# 稳定错误码
# ---------------------------------------------------------------------------
class ErrorCode(enum.Enum):
    """稳定错误码。

    值一旦发布不得修改：``tests/test_contract.py`` 与 ``service`` 的文案表都以它为键。
    **不要**拿 ``str(exc)`` 做分派——它是给人读的短串，分派一律用 ``exc.code``。
    """

    # --- 引擎获取（runtime 的下载/校验/解压三段） ---
    ENGINE_DOWNLOAD_FAILED: Final = "engine.download_failed"
    ENGINE_HASH_MISMATCH: Final = "engine.hash_mismatch"
    ENGINE_UNPACK_FAILED: Final = "engine.unpack_failed"
    ENGINE_LAYOUT_INVALID: Final = "engine.layout_invalid"

    # --- 引擎装载 ---
    PLATFORM_UNSUPPORTED: Final = "engine.platform_unsupported"
    ENGINE_LOAD_FAILED: Final = "engine.load_failed"
    RESOURCE_MISSING: Final = "resource.missing"
    RESOURCE_LOAD_REJECTED: Final = "resource.load_rejected"
    INSTANCE_CREATE_FAILED: Final = "instance.create_failed"

    # --- 设备连接 ---
    ADB_ADDRESS_MISSING: Final = "adb.address_missing"
    ADB_CONNECT_TIMEOUT: Final = "adb.connect_timeout"
    ADB_DISCONNECTED: Final = "adb.disconnected"

    # --- 画面捕获 ---
    CAPTURE_TIMEOUT: Final = "capture.timeout"
    CAPTURE_FAILED: Final = "capture.failed"
    CAPTURE_EMPTY: Final = "capture.empty"
    CAPTURE_FORMAT_UNEXPECTED: Final = "capture.format_unexpected"

    # --- 门禁类（安全边界，不是环境故障） ---
    TASK_NOT_ALLOWED: Final = "guard.task_not_allowed"
    ENGINE_NOT_READY: Final = "guard.engine_not_ready"


# ---------------------------------------------------------------------------
# 细分判别枚举（稳定值：service 查表用，测试断言用）
# ---------------------------------------------------------------------------
class DownloadFailure(enum.Enum):
    """下载失败的细分。重试/文案语义以 ``03-engine-provisioning.md`` §4 为权威。

    ``CANCELLED`` 与其它档的处置**完全不同**：用户主动取消不是失败——不标红、
    不计失败计数、不写"故障"文案，一律呈现为"已暂停"，可直接重试。
    没有这一档，取消就只能落进 ``NETWORK``，用户下次启动会看到
    "上次因为网络错误没下完"——凭空造出一条故障。
    """

    NETWORK = "network"                     # 连接失败/DNS/超时        → 可重试
    THROTTLED = "throttled"                 # 403/429                   → 长退避后可重试
    ASSET_GONE = "asset_gone"               # 404 / tag 被撤销           → 不可重试，需升级插件
    SIZE_MISMATCH = "size_mismatch"         # 字节数 != spec.size        → 可人工重试 1 次
    NO_SPACE = "no_space"                   # ENOSPC                    → 不可重试，需用户清理（约 1.5 GB）
    REJECTED_SCHEME = "rejected_scheme"     # 非 https / 非白名单主机 / 重定向降级 → 不可重试
    WRITE_ERROR = "write_error"             # 本地写盘失败               → 可重试
    CANCELLED = "cancelled"                 # 用户/宿主取消              → **不是失败**，呈现"已暂停"


class UnpackFailure(enum.Enum):
    """解压失败的细分。

    ``BAD_ARCHIVE``（包坏了）/ ``UNSAFE_MEMBER`` ``LIMIT_EXCEEDED``（疑似攻击）/
    ``NO_SPACE`` ``WRITE_ERROR``（本机写盘问题）三类必须走**三段不同文案**：
    前者让用户重下，中者是安全事件，后者让用户清盘或改权限。合档的代价是
    用户被推去重下一个 295 MB 的包，而真实原因是磁盘满。
    """

    BAD_ARCHIVE = "bad_archive"             # 不是合法 zip / CRC 错
    UNSAFE_MEMBER = "unsafe_member"         # zip slip / symlink / 保留名 / 加密条目
    LIMIT_EXCEEDED = "limit_exceeded"       # 条目数 / 单文件 / 总量 / 压缩比超限
    SIZE_DRIFT = "size_drift"               # 声明值与实际写出字节不符（>1%）
    NO_SPACE = "no_space"                   # ENOSPC → 需清盘（文案要带真实缺口）
    WRITE_ERROR = "write_error"             # ENOSPC 之外的写盘失败（EACCES/EROFS/目标被占）


# ---------------------------------------------------------------------------
# 家族：用码集合，不用继承
# ---------------------------------------------------------------------------
PROVISION_CODES: Final[frozenset[ErrorCode]] = frozenset({
    ErrorCode.ENGINE_DOWNLOAD_FAILED,
    ErrorCode.ENGINE_HASH_MISMATCH,
    ErrorCode.ENGINE_UNPACK_FAILED,
    ErrorCode.ENGINE_LAYOUT_INVALID,
    ErrorCode.ENGINE_LOAD_FAILED,
})
"""引导/装载家族。判定用 ``exc.code in PROVISION_CODES``，**不要**用 isinstance
（该家族会增员，码集合只加一行，继承树每加一个码都要改一次）。"""

CAPTURE_CODES: Final[frozenset[ErrorCode]] = frozenset({
    ErrorCode.CAPTURE_TIMEOUT,
    ErrorCode.CAPTURE_FAILED,
    ErrorCode.CAPTURE_EMPTY,
    ErrorCode.CAPTURE_FORMAT_UNEXPECTED,
    ErrorCode.ADB_DISCONNECTED,
    ErrorCode.ADB_CONNECT_TIMEOUT,
    ErrorCode.ADB_ADDRESS_MISSING,
})
"""设备/画布家族。"""

RESOURCE_CODES: Final[frozenset[ErrorCode]] = frozenset({
    ErrorCode.RESOURCE_MISSING,
    ErrorCode.RESOURCE_LOAD_REJECTED,
    ErrorCode.INSTANCE_CREATE_FAILED,
})
"""资源/实例家族："东西在磁盘上，但不对"。

与 ``PROVISION_CODES`` 的差别在**处置**：这一族的标准自救动作是
**重新下载一次引擎**（``bootstrap(force=True)``），不是查网络、也不是清盘。
``service`` 用 ``exc.code in RESOURCE_CODES`` 选那条文案，别按 isinstance 判。
"""


# ---------------------------------------------------------------------------
# 只读上下文映射
# ---------------------------------------------------------------------------
class _FrozenContext(Mapping[str, Any]):
    """``context`` 的载体：写入后不可改，但能与 dict 比较（测试要 ``== {...}``）。

    不用 ``types.MappingProxyType`` 是为了守住"本模块只 import 四类库"这条硬约束；
    代价是这十行代码，收益是异常上下文不会被下游顺手改写。
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = None  # type: ignore[assignment]  # 可变语义 → 不可哈希


def _jsonable(value: Any) -> Any:
    """把上下文值转成 ``json.dumps`` 能吃的形态。只做类型折叠，不做语义解释。"""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


_DISPLAY_TEXT_LIMIT: Final = 40


def _display_scalar(value: Any) -> str:
    """把白名单字段渲染成**短、无路径**的一段文字（K9）。

    与 ``_jsonable`` 的分工很明确：``_jsonable`` 为结构化日志保留全量，本函数只服务
    ``str(exc)`` ——"看得出是哪种失败"就够了。``Path`` 在这里**没有**渲染分支，
    只会变成 ``<path>``：即使有人误把路径字段写进 ``_DISPLAY_FIELDS``，也泄漏不出去。
    """
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Path):
        return "<path>"
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()[:16]
    if isinstance(value, str):
        clipped = value if len(value) <= _DISPLAY_TEXT_LIMIT else value[:_DISPLAY_TEXT_LIMIT] + "…"
        return repr(clipped)
    if isinstance(value, (tuple, list, set, frozenset)):
        return "[" + ", ".join(_display_scalar(item) for item in value) + "]"
    return f"<{type(value).__name__}>"


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------
class EngineError(Exception):
    """所有引擎侧异常的基类。

    设计约定（不可违反）：

    1. ``context`` 只放**可诊断的结构化字段**（path / version / expected / actual /
       address…），不放人话、不放表情、不放语气词。用户可见文案在 service 层查表生成。
    2. ``str(exc)`` 只用于**日志**，不得直接透给模型或聊天窗。
    3. 子类用**关键字参数**构造，字段名进 ``context``，``__init__`` 内不做 I/O。

    字段除了在 ``context`` 里，也可以按属性名直接读（``exc.url`` / ``exc.kind``）——
    这是为了调用点写起来短，不是第二份真相：属性查找直接委托给 ``context``。
    """

    code: ClassVar[ErrorCode | None] = None

    #: ``__str__`` 允许渲染的字段名。只放**关闭词表**的机器标量
    #: （kind/status/symbol/state/address/…）；路径、``detail``/``why``/``member``
    #: 这类自由文本（MAA 原文、压缩包内文件名）**一律不在**此列（K9）。
    _DISPLAY_FIELDS: ClassVar[tuple[str, ...]] = ()

    def __init__(self, message: str, /, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self._context = _FrozenContext(context)

    @property
    def context(self) -> Mapping[str, Any]:
        """只读的结构化上下文。原值保留（Path 是 Path，枚举是枚举）。"""
        return self._context

    def __getattr__(self, name: str) -> Any:
        # 只代理非下划线名字；下划线一律走正常查找，避免干扰 pickle/copy 的探测。
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._context[name]
        except KeyError:
            raise AttributeError(f"{type(self).__name__!s} has no field {name!r}") from None

    def to_dict(self) -> dict[str, Any]:
        """``{'code': 'engine.load_failed', 'message': ..., 'context': {...}}``。

        供 entry 的 ``data`` 与日志使用；``context`` 里的非 JSON 类型已折叠。
        """
        return {
            "code": self.code.value if self.code is not None else None,
            "message": self.message,
            "context": {key: _jsonable(value) for key, value in self._context.items()},
        }

    def _display_pairs(self) -> Iterator[tuple[str, Any]]:
        """``__str__`` 的字段来源。子类可覆写以补派生字段（见 ``EngineLayoutInvalid.shape``）。"""
        for name in type(self)._DISPLAY_FIELDS:
            if name in self._context:
                yield name, self._context[name]

    def __str__(self) -> str:
        parts = [self.code.value if self.code is not None else type(self).__name__]
        for name, value in self._display_pairs():
            parts.append(f"{name}={_display_scalar(value)}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# 18 个叶子异常
# 参数表逐字抄自 docs/01-architecture.md §2.2；全部关键字参数，漏传即 TypeError。
# ---------------------------------------------------------------------------
class EngineDownloadFailed(EngineError):
    """下载失败。``kind`` 决定处置（可重试 / 不可重试 / 不是失败）。"""

    code: ClassVar[ErrorCode] = ErrorCode.ENGINE_DOWNLOAD_FAILED
    _DISPLAY_FIELDS = ("kind", "status")

    def __init__(
        self,
        *,
        url: str,
        target: Path,
        kind: DownloadFailure,
        status: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(
            f"engine download failed: kind={kind.value} url={url!r} target={str(target)!r}",
            url=url,
            target=target,
            kind=kind,
            status=status,
            detail=detail,
        )


class EngineHashMismatch(EngineError):
    """归档内容与 pin 住的 sha256 不符。安全事件。

    ``actual`` 只取**前 8 位**入聊天，全量入审计日志（由 service 负责截断）。
    """

    code: ClassVar[ErrorCode] = ErrorCode.ENGINE_HASH_MISMATCH
    _DISPLAY_FIELDS = ("version", "actual")

    def __init__(
        self,
        *,
        archive: Path,
        expected: str,
        actual: str,
        version: str = "",
    ) -> None:
        super().__init__(
            f"engine hash mismatch: version={version!r} archive={str(archive)!r}",
            archive=archive,
            expected=expected,
            actual=actual,
            version=version,
        )


class EngineUnpackFailed(EngineError):
    """解压失败。``kind`` 区分包损坏 / 疑似攻击 / 本机写盘问题三类。"""

    code: ClassVar[ErrorCode] = ErrorCode.ENGINE_UNPACK_FAILED
    _DISPLAY_FIELDS = ("kind",)

    def __init__(
        self,
        *,
        archive: Path,
        target_dir: Path,
        kind: UnpackFailure,
        member: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(
            f"engine unpack failed: kind={kind.value} archive={str(archive)!r} "
            f"target={str(target_dir)!r} member={member!r}",
            archive=archive,
            target_dir=target_dir,
            kind=kind,
            member=member,
            detail=detail,
        )


class EngineLayoutInvalid(EngineError):
    """解压后布局不对。两种形态共用本码，判据写死在这里，下游别再各自解释一遍：

    ① ``missing != ()`` ⇒ 包里**缺东西**（缺核心库或缺 ``resource/``）；
    ② ``missing == ()`` 且 ``len(found) > 1`` ⇒ **歧义**：同名核心库在多处命中。
       **绝不取第一个**——否则预放一份同名库就能顶掉真库。

    两种形态的自救动作不同（①换包 / ②清掉多余那份），所以文案必须分开。
    """

    code: ClassVar[ErrorCode] = ErrorCode.ENGINE_LAYOUT_INVALID

    def __init__(
        self,
        *,
        root: Path,
        missing: Sequence[str],
        found: Sequence[str] = (),
    ) -> None:
        ambiguous = not missing and len(found) > 1
        super().__init__(
            (
                f"engine layout ambiguous: root={str(root)!r} found={list(found)!r}"
                if ambiguous
                else f"engine layout invalid: root={str(root)!r} "
                f"missing={list(missing)!r} found={list(found)!r}"
            ),
            root=root,
            missing=tuple(missing),
            found=tuple(found),
        )

    @property
    def ambiguous(self) -> bool:
        """``missing`` 为空但核心库命中 >1 处 = 歧义（不是缺件）。"""
        return not self._context["missing"] and len(self._context["found"]) > 1

    @property
    def shape(self) -> str:
        """``"ambiguous"`` / ``"missing"``——``service`` 直接拿它查文案表。"""
        return "ambiguous" if self.ambiguous else "missing"

    def _display_pairs(self) -> Iterator[tuple[str, Any]]:
        yield "shape", self.shape
        yield "missing", self._context["missing"]


class PlatformUnsupported(EngineError):
    """本平台没有**自动安装**通道，不代表这台机器绝对跑不了（手动放置仍可用）。"""

    code: ClassVar[ErrorCode] = ErrorCode.PLATFORM_UNSUPPORTED
    _DISPLAY_FIELDS = ("system", "supported")

    def __init__(self, *, system: str, supported: Sequence[str]) -> None:
        super().__init__(
            f"platform unsupported for auto provisioning: system={system!r}",
            system=system,
            # 键集语义，用 frozenset 保存：既能与 set/frozenset 比较，也不会被顺手改。
            supported=frozenset(supported),
        )


class EngineLoadFailed(EngineError):
    """核心库装载失败。

    ``symbol != None`` ⇒ 版本过旧/接口变更；``oserror`` ⇒ dlopen 失败（缺依赖 dll）。
    """

    code: ClassVar[ErrorCode] = ErrorCode.ENGINE_LOAD_FAILED
    _DISPLAY_FIELDS = ("symbol",)

    def __init__(
        self,
        *,
        library_path: Path | None,
        symbol: str | None = None,
        reason: str = "",
        oserror: str | None = None,
    ) -> None:
        super().__init__(
            f"engine load failed: library={library_path!s} symbol={symbol!r} "
            f"oserror={oserror!r}",
            library_path=library_path,
            symbol=symbol,
            reason=reason,
            oserror=oserror,
        )


class ResourceMissing(EngineError):
    """磁盘上那条路径**不是目录**（不是名字猜错）。``searched`` 是实际找过的路径。"""

    code: ClassVar[ErrorCode] = ErrorCode.RESOURCE_MISSING
    _DISPLAY_FIELDS = ()

    def __init__(self, *, expected_dir: Path, searched: Sequence[Path] = ()) -> None:
        super().__init__(
            f"resource missing: expected={str(expected_dir)!r}",
            expected_dir=expected_dir,
            searched=tuple(searched),
        )


class ResourceLoadRejected(EngineError):
    """路径对、目录存在，但 ``AsstLoadResource`` 返回 False ⇒ 资源内容坏/版本不匹配。"""

    code: ClassVar[ErrorCode] = ErrorCode.RESOURCE_LOAD_REJECTED
    _DISPLAY_FIELDS = ("load_ok", "user_dir_ok")

    def __init__(
        self,
        *,
        resource_parent: Path,
        load_ok: bool,
        user_dir_ok: bool,
        user_dir: Path | None = None,
    ) -> None:
        super().__init__(
            f"resource load rejected: parent={str(resource_parent)!r}",
            resource_parent=resource_parent,
            load_ok=load_ok,
            user_dir_ok=user_dir_ok,
            user_dir=user_dir,
        )


class InstanceCreateFailed(EngineError):
    """``AsstCreateEx`` 返回空句柄。

    ``[FACT]`` 它只在 ``!inited()``（即资源未加载成功）时返回 ``nullptr``，
    所以 ``resource_loaded`` 字段把这条因果写死：看到 False 就别去猜"MaaCore 内部炸了"。
    """

    code: ClassVar[ErrorCode] = ErrorCode.INSTANCE_CREATE_FAILED
    _DISPLAY_FIELDS = ("resource_loaded",)

    def __init__(self, *, resource_loaded: bool, library_path: Path | None) -> None:
        super().__init__(
            f"instance create failed: resource_loaded={resource_loaded} "
            f"library={library_path!s}",
            resource_loaded=resource_loaded,
            library_path=library_path,
        )


class AdbAddressMissing(EngineError):
    """**不是故障，是未配置**。绝不回落 5555（回落会把"没配"伪装成"连不上"）。"""

    code: ClassVar[ErrorCode] = ErrorCode.ADB_ADDRESS_MISSING
    _DISPLAY_FIELDS = ("config_key",)

    def __init__(self, *, config_key: str = "adb_address") -> None:
        super().__init__(
            f"adb address not configured: config_key={config_key!r}",
            config_key=config_key,
        )


class AdbConnectTimeout(EngineError):
    """超时 ≠ 连错：一个说明"等不到终态事件"，一个说明"设备明确拒绝"。"""

    code: ClassVar[ErrorCode] = ErrorCode.ADB_CONNECT_TIMEOUT
    _DISPLAY_FIELDS = ("address", "timeout_s")

    def __init__(self, *, address: str, timeout_s: float, adb_path: str = "") -> None:
        super().__init__(
            f"adb connect timeout: address={address!r} timeout_s={timeout_s!r}",
            address=address,
            timeout_s=timeout_s,
            adb_path=adb_path,
        )


class AdbDisconnected(EngineError):
    """连接被拒/掉线。``why`` 保留 MAA 原文（用户要看的就是那句原文）。"""

    code: ClassVar[ErrorCode] = ErrorCode.ADB_DISCONNECTED
    # why 是 MAA 原文（可能含 adb 路径/设备名）→ 只进 context，用户可见的那句
    # 由 service 的 sanitize_maa_text() 处理（字符白名单 + 截断），不在 __str__ 里。
    _DISPLAY_FIELDS = ("address",)

    def __init__(self, *, address: str, why: str = "", adb_path: str = "") -> None:
        super().__init__(
            f"adb disconnected: address={address!r} why={why!r}",
            address=address,
            why=why,
            adb_path=adb_path,
        )


class CaptureTimeout(EngineError):
    """等 ``AsyncCallInfo(Screencap)`` 超时。``call_id`` 用于在日志里对上"哪次调用"。"""

    code: ClassVar[ErrorCode] = ErrorCode.CAPTURE_TIMEOUT
    _DISPLAY_FIELDS = ("timeout_s", "call_id", "address")

    def __init__(
        self,
        *,
        timeout_s: float,
        address: str,
        call_id: int | None = None,
    ) -> None:
        super().__init__(
            f"capture timeout: timeout_s={timeout_s!r} address={address!r} "
            f"call_id={call_id!r}",
            timeout_s=timeout_s,
            address=address,
            call_id=call_id,
        )


class CaptureFailed(EngineError):
    """``AsyncCallInfo{what:"Screencap", ret:false}``：链路活着，但这次没截到。"""

    code: ClassVar[ErrorCode] = ErrorCode.CAPTURE_FAILED
    _DISPLAY_FIELDS = ("call_id", "ret", "cost_ms")

    def __init__(
        self,
        *,
        call_id: int,
        ret: bool,
        cost_ms: float | None = None,
    ) -> None:
        super().__init__(
            f"capture failed: call_id={call_id!r} ret={ret!r} cost_ms={cost_ms!r}",
            call_id=call_id,
            ret=ret,
            cost_ms=cost_ms,
        )


class CaptureEmpty(EngineError):
    """``AsstGetImage`` 返回 ``0``。

    ``[INFERENCE]`` 唯一现实路径是 ``Assistant::get_image()`` 拿到空 vector，
    即 controller 不存在/未初始化 —— 也就是**未连接**。不要解释成"模拟器还在启动画面"。
    """

    code: ClassVar[ErrorCode] = ErrorCode.CAPTURE_EMPTY
    _DISPLAY_FIELDS = ("buffer_size", "address")

    def __init__(self, *, buffer_size: int, address: str) -> None:
        super().__init__(
            f"capture empty: buffer_size={buffer_size!r} address={address!r}",
            buffer_size=buffer_size,
            address=address,
        )


class CaptureFormatUnexpected(EngineError):
    """首 8 字节不是 PNG 签名 ⇒ 库版本可疑。**绝不二次编码兜底**。

    ``payload`` 携带原始字节（service 负责落盘），但 ``to_dict()`` **只输出
    ``payload_size`` + ``payload_head_hex``（前 16 字节）**：整块缓冲区 hex 进日志
    会撑爆日志，也会把用户画面内容写进日志。落盘与日志是两件事，别混。
    """

    code: ClassVar[ErrorCode] = ErrorCode.CAPTURE_FORMAT_UNEXPECTED
    _DISPLAY_FIELDS = ("magic", "length")

    _HEAD_HEX_BYTES: ClassVar[int] = 16

    def __init__(self, *, magic: bytes, length: int, payload: bytes = b"") -> None:
        super().__init__(
            f"capture format unexpected: magic={bytes(magic).hex()} length={length!r}",
            magic=bytes(magic),
            length=length,
            payload=bytes(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        context = data["context"]
        payload = self._context.get("payload") or b""
        context.pop("payload", None)  # 绝不全量 hex
        context["payload_size"] = len(payload)
        context["payload_head_hex"] = bytes(payload[: self._HEAD_HEX_BYTES]).hex()
        return data


class TaskNotAllowed(EngineError):
    """只读白名单拒绝的写任务。安全边界，不是环境故障。"""

    code: ClassVar[ErrorCode] = ErrorCode.TASK_NOT_ALLOWED
    _DISPLAY_FIELDS = ("task_type",)

    def __init__(self, *, task_type: str, allowlist: Sequence[str]) -> None:
        super().__init__(
            f"task type not allowed: task_type={task_type!r}",
            task_type=task_type,
            # 与 READONLY_TASK_TYPES 同型，测试可直接 `exc.allowlist == READONLY_TASK_TYPES`。
            allowlist=frozenset(allowlist),
        )


class EngineNotReady(EngineError):
    """状态机前置断言失败（``state`` 是 ``EngineState.value``）。

    ``progress`` 只在引擎仍在下载时才有意义（0–100）；其它状态留 ``None``。
    """

    code: ClassVar[ErrorCode] = ErrorCode.ENGINE_NOT_READY
    _DISPLAY_FIELDS = ("state", "progress")

    def __init__(
        self,
        *,
        state: str,
        required: Sequence[str],
        progress: int | None = None,
    ) -> None:
        super().__init__(
            f"engine not ready: state={state!r} required={list(required)!r}",
            state=state,
            required=tuple(required),
            progress=progress,
        )
