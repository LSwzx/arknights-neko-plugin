"""业务层：**唯一**把 ``EngineError`` 变成人话、**唯一**组织 ``push_message`` 的地方。

X03 · import 方向（静态可断言，这条是硬约束）
---------------------------------------------
本模块**只**允许两种包内 import：

    from ..maa.errors import ...
    from ..maa.adapter import ...

**严禁** ``maa.runtime`` / ``maa.wrapper`` / ``maa.provision``。理由不是洁癖：
``runtime`` 依赖 ``provision``（下载/解压）与 ``wrapper``（ctypes），业务层一旦
import 它，任何"只想渲染一句文案"的用例都会被迫拖入 ctypes 与网络栈；而且
``runtime`` 反过来要能构造 service（启动链路），一封单向链就成环。

因此 runtime 在本文件里没有类型，只有一个**本地窄接口** :class:`RuntimePort`：
用 ``typing.Protocol`` 描述"我确实会调的那六个方法"。这是结构化类型的好处——
``MaaRuntime`` 不必知道 ``service`` 存在，``service`` 也不必知道 ``MaaRuntime`` 的全貌。

> 补记：``docs/01-architecture.md`` §3.5 的示例 import 块曾写成
> ``from ..maa.runtime import EngineState, HealthSnapshot, MaaRuntime``，**与 X03 冲突**，
> 已按本节改写（文档同步修正，不留在"文档说一套、代码做一套"的状态）。

文案从哪来
----------
``docs/01-architecture.md`` §3.5.1 是**唯一**的映射表，实现就是本文件的 :data:`_COPY`
（一行一条）。异常对象只带结构化字段；人话一律由 :func:`render_error` 这张**纯函数表**
生成。**任何 ``parts`` 里都不出现 ``str(exc)`` 或 ``exc.message``** ——
``message`` 含绝对路径（只进日志），``str(exc)`` 虽然已脱敏但它是"日志格式"，
不是"猫娘该说的话"，两者不能混用。

push_message（v2）的三条硬规则
------------------------------
1. **同步调用，不要 await**：``push_message(**kwargs) -> dict``。
2. 判据是 ``result["submitted"]``（``ok`` 只是旧兼容）；被拒原因是稳定码
   ``backpressure`` / ``transport_error`` / ``transport_unavailable``，
   **按码处置，不按文本猜**。
3. 每次都带 ``visibility`` / ``ai_behavior`` / ``source`` / ``priority``。

截图必须是 **image part 直接上行**（``{"type": "image", "data": png, "mime": "image/png"}``），
**禁止给路径**；大图先 ``await upload_image(png, "image/png")`` 再把返回的 part 原样塞进
``parts``。字节来自 ``runtime.capture()``，**已经是 PNG**（``AsstGetImage`` 原生返回 PNG），
本模块只读 IHDR 拿尺寸，**绝不做二次编码/改格式**。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, TypedDict

from ..maa.errors import (
    CaptureFormatUnexpected,
    DownloadFailure,
    EngineError,
    EngineLayoutInvalid,
    EngineLoadFailed,
    EngineNotReady,
    ErrorCode,
    RESOURCE_CODES,
    UnpackFailure,
)
from .logic import AdviceBundle, CompanionLogic, PlayerContext

logger = logging.getLogger(__name__)

__all__ = [
    "TextPart",
    "ImagePart",
    "MessagePart",
    "ErrorCopy",
    "RuntimePort",
    "CaptureOutcome",
    "StatusOutcome",
    "StageOutcome",
    "AdviceOutcome",
    "MemoryOutcome",
    "CompanionService",
    "render_error",
    "push_meta",
    "sanitize_maa_text",
    "DEFAULT_CONNECT_CONFIG",
    "resolve_connect_params",
    "STATE_TEXT",
    "STATE_LABEL",
]


# ---------------------------------------------------------------------------
# 消息片段 / 端口类型
# ---------------------------------------------------------------------------
class TextPart(TypedDict):
    type: str        # "text"
    text: str


class ImagePart(TypedDict, total=False):
    type: str        # "image"
    data: bytes
    mime: str


MessagePart = TextPart | ImagePart
"""``[FACT]`` part 的判别字段是 **``type``**（不是 ``kind``），取值 ``"text"`` / ``"image"``。

小图内联：``{"type": "image", "data": <bytes>, "mime": "image/png"}``；
大图：先 ``part = await ctx.images.upload(data, mime="image/png")`` 再把**返回的 part
原样**放进 ``parts``（不要自己拼 ``data``，否则字节又进了消息包预算）。
"""

PushFn = Callable[..., Mapping[str, Any]]
"""``self.push_message`` 的类型化别名。**同步调用，不 await。**"""

UploadImageFn = Callable[[bytes, str], Awaitable[Mapping[str, Any]]]
"""``(data, mime) -> upload part``。**生命周期钩子里不可调用**（命令循环收不到上传应答），
所以 :class:`CompanionService` 只在 entry 路径上用它。"""

StorePort = Callable[[str], Awaitable[Any]]
"""``store.get(key)`` 的包装：入口把 ``Result`` 解包成普通 awaitable 再注入。"""

ConfigPort = Callable[[], Mapping[str, Any]]
"""``() -> 当前配置快照``。用可调用对象而不是 dict，是为了热更新后不必重建 service。"""

DEFAULT_CONNECT_CONFIG: Final[str] = "General"
"""``AsstAsyncConnect`` 的第 4 个参数的默认值（MAA 自己的默认配置名）。"""

_EXTRAS_PATH_KEY: Final[str] = "path"
"""纯路径形态的 extras 落到哪个键。

对 MuMu12 这类"按安装目录找自带 adb"的配置，MAA 的 extras 只认 ``path``
（``maa/runtime.py::MaaRuntime.connect`` 的文档给了同一个例子
``{"path": "...\\MuMuPlayer-12.0"}``）。这里只描述**用户没写 JSON 时的落点**，
不限制用户自己写 JSON 用别的键 —— 能不能用由 MaaCore 决定，不由我们猜。
"""


def resolve_connect_params(config: Mapping[str, Any]) -> tuple[str, dict[str, str] | None]:
    """把**用户配置**翻译成 ``runtime.connect()`` 的 ``(config, extras)``。

    两个键（都在配置白名单里，见 ``__init__._CONFIG_KEYS``）：

    * ``adb_connect_config``：连接配置名。**不做白名单硬拒绝** —— MAA 上游会新增
      模拟器（``MuMuEmulator12`` / ``LDPlayer`` / ``Waydroid`` …），我们在这里锁死
      一份清单，等于把"上游支持了但我们拒了"变成常态。空串/非字符串只做两件事：
      空串回落 ``"General"``、非字符串 ``str()`` 化（TOML 里写 ``123`` 的人，
      想要的一定是 ``"123"`` 这个配置名，不是让它静默消失）。
    * ``adb_connect_extras``：某个配置需要的额外参数。接受两种形态：

      1. **JSON 对象字符串**：``'{"path": "D:/Program Files/Netease/MuMuPlayer-12.0"}'``
         —— 上游新增键时不必改代码；
      2. **纯路径字符串**：``"D:/Program Files/Netease/MuMuPlayer-12.0"``
         —— 等价于 ``{"path": <它>}``，这是 MuMu12 唯一需要的形态，没必要逼用户手写 JSON。

      也接受**已经是 Mapping** 的形态（宿主若把配置段解析成嵌套表就是这样）。

    **坏值一律降级为空 extras + 一条 warning，绝不抛错**：这两个键只影响"连接走哪条
    路径"，而配置是外部输入 —— 一处手滑（少个大括号、写成 TOML 数组）不该让整个插件
    起不来。降级的后果是"连接退回 General 路径"，用户能从 ``arknights_status`` 的
    ``data.connect_config`` 看到这件事。

    为什么"看起来像 JSON 就必须是 JSON"：以 ``{`` 开头的串如果解析失败还硬当路径用，
    MAA 会拿到一串半截 JSON 当安装目录 —— 那会表现成"连上了但截图很怪"，
    比"没配 extras"难查得多。所以这里宁可丢，也不猜。
    """
    name = _connect_config_name(config.get("adb_connect_config"))
    extras = _connection_extras(config.get("adb_connect_extras"), config_name=name)
    return name, extras


def _connect_config_name(raw: Any) -> str:
    """配置名归一：``None``/空串 → ``"General"``；非字符串 → ``str()``。"""
    if raw is None:
        return DEFAULT_CONNECT_CONFIG
    text = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if not isinstance(raw, str):
        logger.warning("adb_connect_config=%r 不是字符串，按 %r 使用", raw, text)
    return text or DEFAULT_CONNECT_CONFIG


def _connection_extras(raw: Any, *, config_name: str) -> dict[str, str] | None:
    """``adb_connect_extras`` → ``dict[str, str]``；任何坏值都返回 ``None``。"""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return _scalar_mapping(raw, config_name=config_name)
    if not isinstance(raw, str):
        logger.warning(
            "adb_connect_extras 期望字符串或表，实际 %s；已忽略（连接按 %s 走）",
            type(raw).__name__,
            config_name,
        )
        return None

    text = raw.strip()
    if not text:
        return None
    if not text.startswith("{"):
        # 纯路径形态：MuMu12 的典型写法，等价于 {"path": <它>}
        return {_EXTRAS_PATH_KEY: text}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "adb_connect_extras 看起来是 JSON 但解析失败（%s）；已忽略（连接按 %s 走）",
            exc.msg,
            config_name,
        )
        return None
    if not isinstance(parsed, Mapping):
        logger.warning(
            "adb_connect_extras 的 JSON 不是对象（%s）；已忽略（连接按 %s 走）",
            type(parsed).__name__,
            config_name,
        )
        return None
    return _scalar_mapping(parsed, config_name=config_name)


def _scalar_mapping(raw: Mapping[Any, Any], *, config_name: str) -> dict[str, str] | None:
    """只保留"键非空 + 值是标量"的项；嵌套表/列表一律丢弃并记一条 warning。"""
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            logger.warning("adb_connect_extras 里有空键；已跳过（连接按 %s 走）", config_name)
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[name] = str(value)
        elif isinstance(value, str):
            if value.strip():
                out[name] = value
        else:
            logger.warning(
                "adb_connect_extras[%r] 不是标量（%s）；已跳过", name, type(value).__name__
            )
    return out or None


class RuntimePort(Protocol):
    """``MaaRuntime`` 在本模块眼里的**窄接口**（六个成员，一个不多）。

    **为什么不用真类型**：``from ..maa.runtime import MaaRuntime`` 会违反 X03，
    并把 ctypes/网络栈拖进业务层。Protocol 让"依赖倒置"变成一行声明，
    也让用例可以喂一个 200 行的假 runtime —— 不需要真的起引擎。

    注意 ``state`` 的读法：**只读 ``.value`` 字符串，不比较枚举成员**。
    一比较就说明我们在 import 那个枚举（哪怕只比较 ``str`` 成员名），
    契约立刻破。``capture()`` 在 runtime 里已是 ``ensure_ready() -> adapter.capture()``
    的薄编排，所以业务层不需要再持一个 adapter。
    """

    @property
    def state(self) -> Any: ...

    @property
    def download_progress(self) -> int | None: ...

    def status_snapshot(self) -> Mapping[str, Any]: ...

    async def bootstrap(self, *, force: bool = False) -> Any: ...

    async def ensure_ready(self) -> None: ...

    async def capture(self) -> bytes: ...

    async def connect(
        self,
        *,
        address: str,
        adb_path: str = "",
        config: str = DEFAULT_CONNECT_CONFIG,
        extras: Mapping[str, str] | None = None,
    ) -> Any: ...

    async def cancel_download(self) -> None: ...

    async def destroy(self) -> None: ...

    async def purge(self) -> None: ...


# ---------------------------------------------------------------------------
# 文案表：模块级常量，键是 ErrorCode 本身（**不另建第二个枚举**）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ErrorCopy:
    """一条文案模板。``variants`` 用于同一码按 ``kind`` 分档（下载/解压需要）。"""

    text: str
    hint: str = ""
    visibility: tuple[str, ...] = ("chat",)
    ai_behavior: str = "read"
    priority: int = 3
    variants: Mapping[str, "ErrorCopy"] = field(default_factory=dict)


def _variant(text: str, hint: str, *, priority: int = 3, ai: str = "read") -> ErrorCopy:
    """变体条目：不重复声明 visibility（恒 ``("chat",)``，变体只改语气/优先级/动作提示）。"""
    return ErrorCopy(text=text, hint=hint, ai_behavior=ai, priority=priority)


_COPY: Final[Mapping[ErrorCode, ErrorCopy]] = {
    # --- 引擎获取 ---
    ErrorCode.ENGINE_DOWNLOAD_FAILED: ErrorCopy(
        text="下载引擎包失败了喵…（这个包约 295 MB，断在中途就会重来）",
        hint="先看看网络或代理，再点一次 arknights_engine_repair 重试。",
        variants={
            DownloadFailure.NETWORK.value: _variant(
                "下载引擎包失败了喵…（这个包约 295 MB，断在中途就会重来）",
                "先看看网络或代理，再点一次 arknights_engine_repair 重试。",
            ),
            DownloadFailure.THROTTLED.value: _variant(
                "下载源把我们限速了喵",
                "等几分钟再试，我不会自己狂重试（越试越容易被限）。",
            ),
            DownloadFailure.ASSET_GONE.value: _variant(
                "官方那个版本的文件已经找不到了喵",
                # priority 走主条的 3（§3.5.1：download 八档只有 cancelled=2，其余七档统一 3）。
                # "官方没这个文件了"确实没救，但**不是紧急**：用户此刻没有可执行动作，
                # 抬到 4/5 只会把它顶到用户正在说的话前面（S04）。
                "这条只能靠升级插件解决：我不接受任意下载地址，换源必须改常量表并发版。",
            ),
            DownloadFailure.SIZE_MISMATCH.value: _variant(
                "下下来的大小和清单对不上喵",
                "重试一次；还不行换个网络环境再试。",
            ),
            DownloadFailure.NO_SPACE.value: _variant(
                "磁盘放不下喵（要差不多 1.5 GB）",
                # 同上：priority 3。"清空间"是用户**稍后**做的动作，不是当场打断的理由。
                "清一点空间，再点 arknights_engine_repair。",
            ),
            DownloadFailure.REJECTED_SCHEME.value: _variant(
                "下载地址不是 https 或不在白名单里喵，我没敢下",
                # 同上：priority 3。安全边界拦下来了，是**已经安全**的状态，不必抢注意力。
                "这是安全边界，改不了；如果一直这样请把这条报告给维护者。",
            ),
            # ↓ 实测存在"并发锁/权限"导致的写盘失败：它**不是网络问题**。
            DownloadFailure.WRITE_ERROR.value: _variant(
                "本地写入失败喵（文件被占用或没有权限），稍后重试",
                "关掉可能占用那个引擎文件的程序，或者换个时间再点一次。",
            ),
            # ↓ 取消**不是失败**：不标红、不写"故障"、优先级降到 2。
            DownloadFailure.CANCELLED.value: _variant(
                "已暂停喵，随时可以接着下",
                "想继续就点 arknights_engine_repair，我接着下。",
                priority=2,
            ),
        },
    ),
    ErrorCode.ENGINE_HASH_MISMATCH: ErrorCopy(
        text="下载下来的包不对劲（校验值对不上），我已经扔了喵",
        hint="换一个镜像源再下一次；别用被改过的包。",
        priority=4,
    ),
    ErrorCode.ENGINE_UNPACK_FAILED: ErrorCopy(
        text="包装不上喵（里面有东西被我拦下了）",
        hint="换官方 Release 包重新下一次。",
        variants={
            UnpackFailure.BAD_ARCHIVE.value: _variant(
                "包装不上喵（这个 zip 本身是坏的）",
                "换官方 Release 包重新下一次。",
            ),
            UnpackFailure.UNSAFE_MEMBER.value: _variant(
                "包装不上喵（里面有东西被我拦下了）",
                "这是安全事件：包里有越界路径/链接/加密条目，别用它。",
                priority=5,
            ),
            UnpackFailure.LIMIT_EXCEEDED.value: _variant(
                "包装不上喵（超出我允许的体积或条目上限）",
                "这个包不正常，换官方 Release 包重下。",
                priority=5,
            ),
            UnpackFailure.SIZE_DRIFT.value: _variant(
                "解开的大小和声明对不上喵",
                "换官方 Release 包重下。",
                priority=5,
            ),
            UnpackFailure.NO_SPACE.value: _variant(
                "磁盘空间不够喵",
                # 这一档**一个字都不提"重下"**：包是好的，让用户重下等于把磁盘问题伪装成网络问题。
                "清一点空间，再点一次 arknights_engine_repair 就行；目标位置是 {engine_root}。",
            ),
            UnpackFailure.WRITE_ERROR.value: _variant(
                "写不进去喵（目录可能没有写权限）",
                "看看 {engine_root} 的权限，或者换一个可写的位置。",
            ),
        },
    ),
    ErrorCode.ENGINE_LAYOUT_INVALID: ErrorCopy(
        text="解开了，但里面结构不对喵",
        hint="换官方 Release 包重下。",
        priority=4,
    ),
    # --- 引擎装载 ---
    ErrorCode.PLATFORM_UNSUPPORTED: ErrorCopy(
        text="自动安装这个引擎，目前只有 Windows x64 能一条龙搞定喵",
        hint="把 MAA Release 解压后整个放进 {engine_root}，再点 arknights_engine_repair。",
        priority=5,
    ),
    ErrorCode.ENGINE_LOAD_FAILED: ErrorCopy(
        text="引擎加载不起来，多半是缺了同目录的依赖库喵",
        hint="把 MAA Release 整包（含依赖）一起放进 {engine_root}，再重启宿主。",
        priority=5,
    ),
    # --- 资源 / 实例：统一走"重新下一次引擎"，细节各自补 ---
    ErrorCode.RESOURCE_MISSING: ErrorCopy(
        text="引擎资源不对喵，我建议重新下一次引擎",
        hint="点 arknights_engine_repair（会强制重下引擎）。东西该放在：{engine_root}",
        priority=4,
    ),
    ErrorCode.RESOURCE_LOAD_REJECTED: ErrorCopy(
        text="引擎资源不对喵，我建议重新下一次引擎",
        hint="点 arknights_engine_repair（会强制重下引擎）；资源与引擎必须同版本。",
        priority=4,
    ),
    ErrorCode.INSTANCE_CREATE_FAILED: ErrorCopy(
        text="引擎资源不对喵，我建议重新下一次引擎",
        hint="点 arknights_engine_repair（会强制重下引擎）；还不行就重启宿主再试。",
        priority=4,
    ),
    # --- 设备连接 ---
    ErrorCode.ADB_ADDRESS_MISSING: ErrorCopy(
        text="还没告诉我模拟器地址喵，所以我根本没去连",
        hint="在模拟器的 ADB/连接设置里找到 host:port 那一行，原样填进 adb_address。",
        ai_behavior="respond",
    ),
    ErrorCode.ADB_CONNECT_TIMEOUT: ErrorCopy(
        text="连了 {timeout_s} 秒没反应喵",
        hint="先自己跑一次 adb connect {address}，看看通不通。",
        ai_behavior="respond",
        priority=4,
    ),
    ErrorCode.ADB_DISCONNECTED: ErrorCopy(
        text="ADB 掉线了喵（{address}）",
        hint="确认模拟器还开着、端口没变，再点 arknights_reconnect。",
        visibility=("chat", "hud"),
        ai_behavior="respond",
        priority=5,
    ),
    # --- 画面捕获 ---
    ErrorCode.CAPTURE_TIMEOUT: ErrorCopy(
        text="截图卡住了喵，这次没拿到画面",
        hint="先重连一次；还不行把 arknights_status 的 stage 发我。",
        priority=4,
    ),
    ErrorCode.CAPTURE_FAILED: ErrorCopy(
        text="这次没截到喵，可以再试一次",
        hint="直接重试就行，不用去查模拟器（链路是通的）。",
    ),
    ErrorCode.CAPTURE_EMPTY: ErrorCopy(
        text="这次没拿到画面喵（引擎说手里是空的）",
        hint="先重连；还不行把 arknights_status 的 stage 发我。",
        priority=4,
    ),
    ErrorCode.CAPTURE_FORMAT_UNEXPECTED: ErrorCopy(
        text="拿到的图不是正常 PNG，可能是引擎版本不对喵",
        hint="原始字节我存好了：{saved_path}；把这个路径发给开发者。",
        priority=5,
    ),
    # --- 门禁（安全边界，不是故障） ---
    ErrorCode.TASK_NOT_ALLOWED: ErrorCopy(
        text="这个我不能做喵（只帮看，不代打）",
        hint="",
        ai_behavior="respond",
        priority=6,
    ),
    ErrorCode.ENGINE_NOT_READY: ErrorCopy(
        text="引擎现在是「{state}」阶段，这一步做不了喵",
        hint="等它自己走完；或者点 arknights_engine_repair 推它一把。",
    ),
}

STATE_TEXT: Final[Mapping[str, str]] = {
    "absent": "本地还没有可用引擎",
    "downloading": "正在搬引擎",
    "verified": "引擎包下好了、校验通过",
    "unpacked": "引擎解开了、还没装进内存",
    "loaded": "引擎装好了、还没连上设备",
    "ready": "一切就绪",
    "failed": "引擎卡在故障态",
}
"""``EngineState`` 七态 → 一句人话。**键是 ``.value`` 字符串**（不 import 那个枚举）。

七态必须逐个有文案：漏一档的表现是用户看到原始英文枚举名，
而那种"看起来像给开发者看的"文案会被当成 bug 报上来。
"""

STATE_LABEL: Final[Mapping[str, str]] = {
    "absent": "没有引擎",
    "downloading": "正在下载",
    "verified": "已校验、待解包",
    "unpacked": "已解包、待装载",
    "loaded": "已装载、待连接",
    "ready": "就绪",
    "failed": "故障",
}
"""七态的**短名**，专供嵌入句子（``引擎现在是「{state}」阶段``）。

为什么不复用 ``STATE_TEXT``：那是整句，嵌进句子会变成
"引擎还在 正在搬引擎 阶段"这种病句。两张表各司其职，改一张不会污染另一张。
"""

_KIND_DOMAINS: Final[Mapping[ErrorCode, Any]] = {
    ErrorCode.ENGINE_DOWNLOAD_FAILED: DownloadFailure,
    ErrorCode.ENGINE_UNPACK_FAILED: UnpackFailure,
}

_CLIENT_SCOPE_NOTE: Final[str] = (
    "顺便说清楚喵：我只支持国服官服（鹰角官方客户端、中国大陆区服），"
    "外服和渠道服的客户端资源跟官服不是同一套 —— 资源这一关过不去，换成官服客户端就行。"
)
"""资源家族失败的固定补充句：「本插件只支持国服官服」。

**为什么必须有这一句**：MaaCore 没有任何 C API 能查询"当前连的是哪个客户端"（客户端
类型是识别阶段模板匹配出来的内部状态），所以"声明范围"是唯一诚实的表达；而声明如果不
出现在**用户第一次失败**的那条消息里，就等于不存在 —— 外服用户会去反复重启、重下引擎、
换端口，全都无效。

**为什么是文案常量而不是读 ``SUPPORTED_CLIENT_SCOPE``**：那个声明住在 ``maa.runtime``，
而本模块 import 它违反 X03（业务层不许拖入 ctypes/网络栈）。所以这里放的是**人话**，
机器值随 ``status_snapshot()['client_scope']`` 走 ``data``（见 :meth:`CompanionService._snapshot`）。
两处措辞若改动需要一起改 —— 一句在聊天窗，一个在诊断字段，语义必须指向同一个范围。
"""


def _validate_kind_domains(
    table: Mapping[ErrorCode, ErrorCopy],
    domains: Mapping[ErrorCode, Any] = _KIND_DOMAINS,
) -> None:
    """校验"带 kind 的码必须覆盖该 kind 枚举的全部取值"。

    单列成函数（而不是模块级 for 循环）的唯一理由是**可测**：用例要能拿一份
    "故意删掉一个 variant"的表来调它、并断到 ``AssertionError``。
    """
    for code, kind_enum in domains.items():
        variants = table[code].variants
        expected = {member.value for member in kind_enum}
        assert set(variants) == expected, (
            f"{code} 的 variants 必须覆盖 {kind_enum.__name__} 全部取值，"
            f"缺 {sorted(expected - set(variants))}，多 {sorted(set(variants) - expected)}"
        )


# 覆盖全集的不变量放在**生产代码**里，而不是只靠测试：导入期就炸更可靠。
assert set(_COPY) == set(ErrorCode), (
    f"文案表缺 {sorted(code.value for code in set(ErrorCode) - set(_COPY))}；"
    f"多出 {sorted(code.value for code in set(_COPY) - set(ErrorCode))}"
)
_validate_kind_domains(_COPY)


# ---------------------------------------------------------------------------
# 消毒：上游/玩家可控文本进模型前必须过这里（K8）
# ---------------------------------------------------------------------------
_SANITIZE_KEEP_PUNCT: Final[frozenset[str]] = frozenset(
    "，。、；：！？（）【】「」『』《》〈〉…—～·￥％＋－×÷＝"
)


def _is_kept_char(char: str) -> bool:
    """字符白名单。**白名单进、其余全出**——黑名单永远漏（Unicode 太大）。"""
    code = ord(char)
    if char in "\n\r\t":
        return False                      # 换行是新指令块的起点，必须折叠
    if code < 0x20 or code == 0x7F:
        return False                      # 控制字符 / DEL
    if 0x20 <= code <= 0x7E:
        return True                       # ASCII 可见
    if char in _SANITIZE_KEEP_PUNCT:
        return True
    if 0x4E00 <= code <= 0x9FFF:
        return True                       # CJK 统一表意
    if 0x3040 <= code <= 0x30FF:
        return True                       # 日文假名
    if 0xAC00 <= code <= 0xD7AF:
        return True                       # 韩文音节
    return False


_PATH_PLACEHOLDER: Final[str] = "<路径>"
"""路径折叠后的占位符。与 ``maa/errors.py::_display_scalar`` 的 ``<path>`` 同口径
（同一个意思：这里曾经是一个路径；**字面用中文**是因为它会被用户读到）。

``<`` ``>`` 都在 ASCII 可见区（``0x20–0x7E``），所以占位符本身不会被打回空白 ——
但更关键的是：**折叠发生在字符白名单之后**，占位符是后插进去的，压根不经过白名单。
"""

_ZERO_WIDTH_DROP: Final[Mapping[int, None]] = dict.fromkeys(
    (
        0x200B, 0x200C, 0x200D, 0x2060,          # 零宽空格 / 非连接 / 连接 / 词连接
        0xFEFF,                                   # BOM / 零宽不换行空格
        *range(0x202A, 0x202F),                   # bidi 嵌入 / 覆盖 / 隔离 202A–202E
        *range(0x2066, 0x206A),                   # bidi 隔离 2066–2069
    )
)
"""**删除**（不是替换成空格）的不可见字符表。

为什么是删除而不是打空格：白名单把零宽字符打成空格，会把一个 token **劈成两半** ——
``C:\\adb\\adb.exe`` 里塞一个零宽字符就变成 ``C: <路径>``，残留裸盘符；
而且"用零宽字符把敏感词拆开"本身是一种注入花招。先删掉，token 才保持完整，
折叠规则也才看得见整条路径。
"""

_PATH_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"""
      (?<![A-Za-z0-9_])[A-Za-z]:[\\/]      # C:\dir  /  D:/dir（左边界防 localhost:5555）
    | (?<![A-Za-z0-9_])[A-Za-z]:[^\\/\s]   # C:foo（盘符相对，冒号后不是分隔符）
    | \\\\                                 # \\host\share  /  \\?\C:\  /  \\.\Device
    | \\                                   # 任何残留反斜杠（Windows 路径片段，如 a\b.exe）
    | ~[\\/]                               # ~/...  ~\...（家目录）
    | [A-Za-z][A-Za-z0-9+.\-]*://          # file://...  https://...（scheme URI）
    | (?i:(?:\$home|\$\{home\}|%userprofile%|%appdata%)[\\/])   # $HOME 与 %APPDATA%
    | (?i:(?<![A-Za-z0-9_])(?:sig|sv|se|sp|token|key|signature)=)  # 凭据参数（SAS）
    | (?:^|[=:(\["'`,（\[\uFF08])/         # 任意 POSIX 绝对路径（允许 `path=` `(` "--x=" 壳）
    """,
    re.VERBOSE,
)
"""token 里**出现就算路径**的形态表。

为什么要"出现就算"而不是"整 token 相等"：上游文本里的路径常常带壳
（``(C:\adb\adb.exe)``、``path=C:\adb\adb.exe,``、``--arg=/sdcard/a.png``）。
按 token 全等匹配会整片漏掉。代价是 ``a\b`` 这种也会被折 —— 在 MAA 的报错文本里
这仍然比泄漏路径好。

三个刻意的细节（都是实测踩出来的，见 ``02-test-plan.md`` §4.3）：

* **盘符相对要有左边界**（``(?<![A-Za-z0-9_])``）：没有它，``localhost:5555`` 里的
  ``t:5``、``timeout:30`` 里的 ``t:3`` 都会被当成 ``C:foo`` 折掉 —— 折完只剩
  ``<路径> refused``，既没地址也没超时值，比泄漏更难排查，而且 ``timeout`` 会被
  "救命稻草"逻辑搬到占位符后面、``30`` 直接消失。左边界必须赏在 ``:`` 之前那个字符上；
* **POSIX 必须允许"壳前缀"**（``path=`` / ``(`` / ``"`` / ``--arg=``）：上游文本里的
  POSIX 路径几乎总带壳。只写 ``^/`` 时，``/sdcard/a.png`` 能折、``path=/sdcard/a.png``
  原样泄漏 —— 那是**样本形态造成的假绿**，不是规则真的生效；
* **凭据参数不看有没有 ``?``/``&``**：``urllib`` 的异常文本常常是不带 scheme 的裸查询串
  （``token=SECRET`` / ``?sv=…&sig=…``），只有这一条能救它；``[?&]`` 前缀会让
  ``token=…`` 这种**行首**样本永远漏过（实测踩过）。

两类"不是路径、但同样不能出模型"的形态也放在这张表里：

* ``$HOME/`` / ``%APPDATA%\\``：环境变量壳的家目录，展开后就是用户名；
* ``sig=`` / ``sv=`` / ``token=`` 这类**凭据参数**：SAS / 预签名 URL 里的 token，
  泄漏要**轮换凭据**，比泄漏路径高一档。

明显**不做**的两类（加规则只会误伤正常文本）：百分号编码的路径（``C%3A%5C…``，
MAA 不产出）、纯相对路径（``./adb.exe``，不含用户信息）。

**写这张表的一条纪律**：``re.VERBOSE`` 下注释到行尾结束，但**注释末尾的反斜杠仍然会
续行**，把下一行整条规则吞成注释 —— 规则编译得进去、却永远匹配不到（"写了规则但它是
死的"，§4.3 第 5 类假绿）。注释不许以反斜杠结尾。
"""

_DIAG_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?:timed out|timeout|refused|denied|not found|failed|missing"
    r"|error|symbol|unable|invalid)"
    r"(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)
"""诊断关键词：**折叠之后必须还在**。

为什么需要它：路径常常和原因贴在一个 token 里（``C:\\adb\\adb.exe:refused``、
``path=C:\\x.exe,failed``）。整 token 替换会把原因一起吃掉，用户看到
「引擎原话：``<路径>``」等于没有线索 —— 而这段引用存在的唯一意义就是把原因带给他。

那两个 ``(?<![A-Za-z0-9_.])`` / ``(?![A-Za-z0-9_.])`` 边界是**故意收紧**的：
``error.log`` 里的 ``error`` 属于文件名，不是诊断词，不该被捞出来当原因。
"""


def _diag_keywords(token: str) -> tuple[str, ...]:
    """token 里的诊断关键词，按出现顺序去重（大小写不同算同一个）。"""
    seen: list[str] = []
    lowered: set[str] = set()
    for match in _DIAG_KEYWORD_RE.findall(token):
        key = match.lower()
        if key not in lowered:
            lowered.add(key)
            seen.append(match)
    return tuple(seen)


def _fold_paths(text: str) -> str:
    """把每个空白分隔的 token 里的路径**整段**换成 :data:`_PATH_PLACEHOLDER`。

    四个刻意的取舍：

    * **只在 token 边界上折**：路径里的空格会把一个路径拆成两个 token
      （``C:\\Program Files\\x.exe``）。第二个 token 含反斜杠，同样命中
      :data:`_PATH_HINT_RE`，所以两段都被折掉 —— 不需要写一个"猜路径到哪结束"的正则；
    * **续段靠上下文，不靠"含分隔符就算"**（``C:/Program Files/My Company/secret.dll``）：
      第一段折掉之后，**紧跟着的、含 ``/`` 或 ``\\`` 的 token 继续折，遇到纯词 token 停**。
      为什么不用 ``\\S*[\\\\/]\\S*``：那条会把 ``6/22``、``and/or``、``10061`` 全折掉 ——
      MAA 的报错文本里"错误码 10061 / 日期 6/22"比路径还常见。上下文规则能同时满足：
      ``C:/Program Files/My Company/secret.dll`` → ``<路径>``，而
      ``MAA 6/22 refused`` / ``this and/or that`` 原样；
    * **同 token 里的诊断关键词要捞回来**：``C:\\adb\\adb.exe:refused`` →
      ``<路径> refused``。这是"折掉了路径、但留下了原因"；和"什么都不折"是两个极端，
      中间这条才是用户要的；
    * **连续占位符合并成一个**：``C:\\a C:\\b`` 折成 ``<路径> <路径>`` 是噪音，
      用户只需要知道"这里原来是路径"。

    幂等：``<路径>`` 本身不命中任何形态（``<`` ``>`` 不在任何形态里），二次消毒原样返回。
    """
    if not text:
        return ""
    out: list[str] = []
    in_path_run = False               # 上一个 token 是不是刚被折过（续段判据）
    for token in text.split(" "):
        if not token:
            continue
        continued = in_path_run and ("/" in token or "\\" in token)
        if continued or _PATH_HINT_RE.search(token):
            keywords = _diag_keywords(token)
            piece = _PATH_PLACEHOLDER + (" " + " ".join(keywords) if keywords else "")
            in_path_run = True
        else:
            piece = token
            in_path_run = False
        if piece == _PATH_PLACEHOLDER and out and out[-1] == _PATH_PLACEHOLDER:
            continue
        out.append(piece)
    return " ".join(out)


def sanitize_maa_text(text: str, *, limit: int = 120) -> str:
    """把 MAA / adb 的原文洗成"可以给人看、也可以进模型"的一段短文本。

    规则（顺序不可换）：

      0. **删不可见字符**：零宽空格/连接符、bidi 嵌入覆盖符先 ``translate`` **删掉**
         （不是打空格——打空格会把一个 token 劈成两半，残留裸盘符 ``C:``）；
      1. **字符白名单**：保留中日韩/拉丁/数字/常用标点，其余替换为空格
         （控制字符、换行都在"其余"里）；
      2. **空白折叠**：连续空白压成一个空格并 strip（换行折叠是重点：
         多行文本会被模型读成新的指令块）；
      3. **路径折叠**（S08 / K8）：盘符、UNC、``file://``、``$HOME/``、凭据参数、
         POSIX 绝对路径整段换成 ``<路径>``，**同 token 里的诊断关键词（``refused`` /
         ``denied`` / ``timeout`` …）捞回占位符后面**。
         **必须在截断之前**做 —— 否则长路径先被截成半截盘符，形态规则就匹配不到了，
         剩下的半截照样是泄漏；
      4. **截断**：超过 ``limit`` 字就截到 ``limit-1`` 再补 ``…``，
         且**以占位符为原子**（切点落在 ``<路径>`` 中间就退到它的起点）。

    路径折叠这一步不是"锦上添花"：``why`` 这类上游文本里最常见的机器值就是
    路径（``C:\\adb\\adb.exe refused``），它比任何文件内容都更容易暴露用户名与目录结构。

    ``AdbDisconnected.why`` / MAA 回调里的 ``detail`` 这类文本**给用户或模型之前
    必须过它**，且只能作为**引用数据**单独成一段 —— 不拼进猫娘主句，
    也不拼进系统提示词。
    """
    if not text:
        return ""
    raw = text if isinstance(text, str) else str(text)
    if limit <= 0:
        return ""
    raw = raw.translate(_ZERO_WIDTH_DROP)     # 先删不可见字符，再进白名单（否则 token 会被劈开）
    kept = []
    for char in raw:
        kept.append(char if _is_kept_char(char) else " ")
    collapsed = " ".join("".join(kept).split())
    folded = _fold_paths(collapsed)
    return _truncate(folded, limit)


def _truncate(text: str, limit: int) -> str:
    """按 ``limit`` 截断，**不切开占位符**。

    为什么单独成函数：``<路径>`` 被切成 ``<路径…`` 之后，那半个 ``<`` 既不是标签也不是
    文本，模型读到的是"格式坏掉的串"—— 而且它会**原样进模型**。占位符是原子，
    切点落在它中间就退到它的起点（宁可少一段信息，也不留破损标记）。

    退化情形：退到起点后为空（文本全是一个长占位符）→ 返回 ``…``，仍满足
    ``len(out) <= limit``。
    """
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    cut = text[: limit - 1]
    start = 0
    while True:
        start = text.find(_PATH_PLACEHOLDER, start)
        if start < 0:
            break
        end = start + len(_PATH_PLACEHOLDER)
        if start < len(cut) < end:            # 切点落在占位符内部
            cut = text[:start]
            break
        start = end
    cut = cut.rstrip()
    return (cut + "…") if cut else "…"


# ---------------------------------------------------------------------------
# 渲染：异常 → parts（纯函数）
# ---------------------------------------------------------------------------
class _SafeDict(dict):
    """``str.format_map`` 的兜底：字段缺了就写 ``…``，绝不 KeyError。

    用 ``format_map`` + 兜底而不是 f-string 的唯一理由：文案是**数据**（表里一行一条），
    拼字符串的函数一多，"文案在哪"就又变成一个要 grep 的问题。
    """

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "…"


def _copy_for(exc: EngineError) -> ErrorCopy:
    """按 ``code``（+ ``kind``）取文案。缺档时静默回落主条——不抛错。"""
    base = _COPY.get(exc.code) if exc.code is not None else None
    if base is None:                       # code 为 None 的裸 EngineError
        return ErrorCopy(
            text="引擎那边出了点状况喵，我先记下来了",
            hint="再试一次；还不行把 arknights_status 的 stage 发我。",
        )
    kind = getattr(exc, "kind", None)      # 只有下载/解压带 kind
    key = getattr(kind, "value", None)
    if isinstance(key, str) and key in base.variants:
        return base.variants[key]
    return base


def push_meta(exc: EngineError) -> tuple[tuple[str, ...], str, int]:
    """``(visibility, ai_behavior, priority)`` —— 与 :func:`render_error` 同源，不分叉。

    分两个函数是因为返回值形状不同（parts vs 三个标量），但**取值只有一个来源**：
    同一份 :class:`ErrorCopy`。这样"文案改了、优先级没改"这种漂移不会发生。
    """
    copy = _copy_for(exc)
    return copy.visibility, copy.ai_behavior, int(copy.priority)


def render_error(
    exc: EngineError,
    *,
    engine_root: str = "",
    saved_path: str = "",
) -> list[MessagePart]:
    """把异常渲染成 ``parts``。**纯函数**：同样入参永远同样出参，无 I/O、无 push。

    绝不抛错：``_render_error`` 会被定时/后台路径调用，那里抛错就是"没人接的异常"。

    三条特殊分档（都要能断言出"不同 kind 的 parts 互不相等"）：

      * ``EngineLayoutInvalid.shape``：``missing``（缺件→换包重下）与
        ``ambiguous``（命中多份库→清掉多余那份）**两套文案、两套自救动作**；
      * ``EngineLoadFailed``：``symbol`` 非空 = 版本老；否则 = 缺依赖库；
      * ``RESOURCE_CODES``：统一"重新下一次引擎"，细节（searched / load_ok …）另起一段。

    上游可控文本（``why`` / ``detail``）**只做引用数据单独成段**，不拼进主句。
    """
    if not isinstance(exc, EngineError):
        logger.warning("render_error got non-EngineError: %s", type(exc).__name__)
        return [_text("引擎那边出了点状况喵，我先记下来了；再试一次看看。")]

    fields = _SafeDict(
        engine_root=engine_root or "（插件数据目录下的 maa 目录）",
        saved_path=saved_path or "（保存失败，拿不到路径）",
        address=str(getattr(exc, "address", "") or "（未配置）"),
        timeout_s=str(getattr(exc, "timeout_s", "") or "…"),
        state=STATE_LABEL.get(str(getattr(exc, "state", "") or ""), "未知"),
    )

    parts: list[MessagePart] = []

    # --- 特例 1：布局两种形态，文案与自救动作都不同 ---
    if isinstance(exc, EngineLayoutInvalid):
        if exc.shape == "ambiguous":
            parts.append(_text(
                "解开了，但里面有好几份同名的引擎库喵（我不知道该信哪一份，所以一份都没用）。"
            ))
            parts.append(_text(
                "把 {root_hint} 里多余的那一份删掉，只留一份，再点 arknights_engine_repair。"
                .format(root_hint=fields["engine_root"])
            ))
        else:
            # ``missing`` 是**主句里的数据**，是全模块唯一一条"拼进主句"的异常字段，
            # 所以它必须逐项过折叠：现在 provision 只塞 ``resource/`` 这类相对件名，
            # 但没有任何东西阻止将来有人塞 ``str(destp / "resource")`` 进来。
            # 相对件名不含任何路径形态，折叠对它是恒等变换（不会把件名改成 <路径>）。
            missing = ", ".join(
                _fold_paths(str(item)) or str(item) for item in getattr(exc, "missing", ())
            ) or "关键文件"
            parts.append(_text(f"解开了，但里面缺东西喵（缺：{missing}）。"))
            parts.append(_text(
                "换官方 Release 包重新下一次；要放的位置是 {root_hint}。".format(
                    root_hint=fields["engine_root"]
                )
            ))
        return parts

    # --- 特例 2：装载失败两档（symbol / 缺依赖） ---
    if isinstance(exc, EngineLoadFailed):
        if getattr(exc, "symbol", None):
            parts.append(_text("引擎版本太老了喵，接口对不上。"))
            parts.append(_text(
                "换成受支持的 MaaCore 版本，然后**重启宿主**（同进程里已加载的库不会重新映射）。"
            ))
        else:
            parts.append(_text("引擎加载不起来喵，多半是缺了同目录的依赖库。"))
            parts.append(_text(
                "把 MAA Release 整包（连依赖一起）放进 {root_hint}，再重启宿主。".format(
                    root_hint=fields["engine_root"]
                )
            ))
        return parts

    copy = _copy_for(exc)
    main = copy.text.format_map(fields)

    # EngineNotReady 且仍在下载：把进度带上（None **不是** 0%，不许渲染成 0%）
    if exc.code is ErrorCode.ENGINE_NOT_READY:
        progress = getattr(exc, "progress", None)
        if isinstance(progress, int) and not isinstance(progress, bool):
            main = f"{main}（引擎搬了 {progress}%）"
        elif str(getattr(exc, "state", "") or "") == "downloading":
            main = f"{main}（进度还没报上来喵，先别急着以为卡住了）"

    parts.append(_text(main))
    if copy.hint:
        parts.append(_text(copy.hint.format_map(fields)))

    # --- 资源家族：统一的"重下一次"之外，把结构化细节单独给一段 ---
    if exc.code in RESOURCE_CODES:
        detail = _resource_detail(exc)
        if detail:
            parts.append(_text(detail))
        # 资源三兄弟（missing / load_rejected / instance.create_failed）是**同一个根因族**：
        # 资源树跟引擎对不上。外服客户端在这里必然失败，所以这句话三家都带上 ——
        # 只给"路径不存在"那一档加，会让"资源加载被拒"的外服用户继续去重下引擎。
        parts.append(_text(_CLIENT_SCOPE_NOTE))

    # --- 上游可控文本：消毒后**单独成段**，作为引用数据（K8） ---
    upstream = str(getattr(exc, "why", "") or "")
    if upstream:
        quoted = sanitize_maa_text(upstream)
        if quoted:
            parts.append(_text(f"「引擎原话（已消毒，仅作参考）」{quoted}"))

    return parts


def _resource_detail(exc: EngineError) -> str:
    """资源家族的补充细节。**只用结构化字段**，不拼自由文本。

    ``RESOURCE_MISSING`` 会**真的列出找过的位置**（§3.5.1 那一行的要求）：
    手动放置引擎时用户需要知道"我到底该往哪儿放"，而 ``searched`` 是唯一诚实的答案。
    这不违反 K9 —— K9 禁的是把 ``exc.message``（含用户目录的实现细节）搬进聊天，
    而 ``searched`` 是**我们自己的搜索范围**，本来就是给用户看的。列表最多列 3 条。
    """
    code = exc.code
    if code is ErrorCode.RESOURCE_MISSING:
        searched = tuple(getattr(exc, "searched", ()) or ())
        if not searched:
            return "资源目录不存在（那条路径不是目录）。"
        shown = "、".join(str(path) for path in searched[:3])
        more = f"，等 {len(searched)} 处" if len(searched) > 3 else ""
        return f"我找过这些位置：{shown}{more}。"
    if code is ErrorCode.RESOURCE_LOAD_REJECTED:
        load_ok = getattr(exc, "load_ok", None)
        user_dir_ok = getattr(exc, "user_dir_ok", None)
        return f"引擎读资源时返回 load_ok={load_ok} / user_dir_ok={user_dir_ok}。"
    if code is ErrorCode.INSTANCE_CREATE_FAILED:
        loaded = getattr(exc, "resource_loaded", None)
        return f"建实例时引擎说资源已加载={loaded}。"
    return ""


def _text(value: str) -> TextPart:
    return {"type": "text", "text": value}


# ---------------------------------------------------------------------------
# PNG 尺寸（只读 IHDR，**绝不反推、绝不重编码**）
# ---------------------------------------------------------------------------
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_IHDR_LENGTH: Final[int] = 13        # IHDR 的 data 段固定 13 字节
_IHDR_OFFSET: Final[int] = 16        # 8 签名 + 4 长度 + 4 类型
_MIN_PNG_BYTES: Final[int] = _IHDR_OFFSET + 8


def _png_size(png: bytes) -> tuple[int, int] | None:
    """从 IHDR 读 ``(width, height)``；不是合法 PNG 返回 ``None``。

    **只用 IHDR**：用 ``len()`` 反推尺寸是错的（压缩率不是常数），
    用"默认 1280×720"更错（那会让"没读到"和"真是这个尺寸"不可区分）。
    """
    if not isinstance(png, (bytes, bytearray)):
        return None
    data = bytes(png)
    if len(data) < _MIN_PNG_BYTES or not data.startswith(_PNG_SIGNATURE):
        return None
    if data[12:16] != b"IHDR":
        return None
    length = int.from_bytes(data[8:12], "big")
    if length != _IHDR_LENGTH:
        return None
    width = int.from_bytes(data[_IHDR_OFFSET : _IHDR_OFFSET + 4], "big")
    height = int.from_bytes(data[_IHDR_OFFSET + 4 : _IHDR_OFFSET + 8], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


# ---------------------------------------------------------------------------
# Outcome（entry 只读它，不读 service 内部）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """一次截图的结果。**不含任何"给模型的文案以外的自由文本"**。"""

    ok: bool
    image: bytes | None
    width: int
    height: int
    image_format: str
    parts: tuple[MessagePart, ...]
    pushed: bool
    push_failure_reason: str | None
    saved_path: Path | None
    too_large_for_inline: bool
    error: EngineError | None
    user_text: str
    diagnostics: dict[str, Any]

    def to_entry_dict(self) -> dict[str, Any]:
        """entry 的 ``Ok`` 载荷。**图片字节不进这里**（会撑爆 token 预算）。"""
        data: dict[str, Any] = {
            "width": self.width,
            "height": self.height,
            "format": self.image_format,
            "pushed": self.pushed,
        }
        if self.saved_path is not None:
            data["path"] = str(self.saved_path)
        if self.too_large_for_inline:
            data["too_large_for_inline"] = True
        if self.push_failure_reason:
            data["push_failure_reason"] = self.push_failure_reason
        return data


@dataclass(frozen=True, slots=True)
class StatusOutcome:
    """状态/重连/修复三个入口共用的形状。

    ``snapshot`` 是 ``Mapping`` 而不是 ``HealthSnapshot``：``HealthSnapshot`` 定义在
    ``maa.runtime``，为它 import 会违反 X03。因此快照按**字典**在本层流动
    （``runtime.status_snapshot()`` 本来就返回 dict）。
    """

    ok: bool
    snapshot: Mapping[str, Any]
    parts: tuple[MessagePart, ...]
    user_text: str
    diagnostics: dict[str, Any]
    steps: tuple[str, ...] = ()

    def to_entry_dict(self) -> dict[str, Any]:
        data = dict(self.snapshot)
        if self.steps:
            data["steps"] = list(self.steps)
        data["pushed"] = bool(self.parts)
        return data


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """关卡分析结果。``ocr_ok=False`` 是**事实**，不是失败。"""

    ok: bool
    stage: str
    advice: AdviceBundle | None
    ocr_ok: bool
    ocr_error_code: str | None
    parts: tuple[MessagePart, ...]
    user_text: str
    diagnostics: dict[str, Any]

    def to_entry_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "stage": self.stage,
            "tips": list(self.advice.tips) if self.advice is not None else [],
            "ocr_ok": self.ocr_ok,
            "pushed": bool(self.parts),
        }
        if self.ocr_error_code:
            data["ocr_error_code"] = self.ocr_error_code
        return data


@dataclass(frozen=True, slots=True)
class AdviceOutcome:
    """肉鸽建议（纯静态知识，无 I/O）。"""

    ok: bool
    theme: str
    advice: tuple[str, ...]
    parts: tuple[MessagePart, ...]
    user_text: str
    diagnostics: dict[str, Any]

    def to_entry_dict(self) -> dict[str, Any]:
        return {"theme": self.theme, "advice": list(self.advice)}


@dataclass(frozen=True, slots=True)
class MemoryOutcome:
    """陪玩记忆：三个缓存值 + 引擎状态。读失败一律回落 ``None``。"""

    ok: bool
    operators_cache: Any
    depot_cache: Any
    last_stage: Any
    state: str

    def to_entry_dict(self) -> dict[str, Any]:
        return {
            "operators_cache": self.operators_cache,
            "depot_cache": self.depot_cache,
            "last_stage": self.last_stage,
            "state": self.state,
        }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
_PUSH_REJECT_HINT: Final[Mapping[str, str]] = {
    "backpressure": "聊天窗有点挤，这条先没挤进去喵",
    "transport_error": "消息通道打了个嗝，这条没送到喵",
    "transport_unavailable": "现在没有可用的消息通道喵",
}
"""``submitted=False`` 的三个稳定原因码 → 给人看的一句话（只进日志/诊断）。

**不重试**：同一轮里重试只会放大 backpressure；重试与去重是插件策略，
但"立刻重试"不是，它是放大器。
"""

_STABLE_PUSH_REASONS: Final[frozenset[str]] = frozenset(_PUSH_REJECT_HINT)
"""宿主**只保证**这三个 ``reason`` 值是稳定的；白名单外的一律不是"码"。

为什么要归一：下游（``push_failure_reason`` / ``diagnostics``）按码处置。
一旦把宿主的自由文本原样回传，就会凭空出现"第四个稳定码"，
而它明天可能变成另一句话 —— 那不是契约，是翻译。
"""


def _normalize_push_reason(raw: Any) -> str:
    """把宿主给的 ``reason`` 归一成三个稳定码之一（S06）。

    * 白名单内 → 原样返回；
    * 大小写/空白抖动 → 按 ``strip().lower()`` 归一后再判（容忍
      ``"Backpressure"`` / ``" backpressure "``，语义没变）；
    * 缺失 / 非字符串 / 空串 / 白名单外 → ``"transport_error"``。

    **不做模糊匹配**：``"backpressure-ish"`` 不是 ``backpressure``。
    猜错方向比承认"不知道怎么归类"更危险 —— 后者至少是保守的（不重试、不静默）。
    """
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _STABLE_PUSH_REASONS:
            return token
    if raw not in (None, ""):
        logger.info("push_message returned unknown reason %r, folded to transport_error", raw)
    return "transport_error"


class CompanionService:
    """业务层：**唯一**把 EngineError 变成人话、**唯一**组织 push_message 的地方。"""

    __slots__ = (
        "_runtime",
        "_logic",
        "_push_fn",
        "_upload_image",
        "_store_get",
        "_config_get",
        "_cache_dir",
        "_data_dir",
        "_engine_root",
        "_verbose",
        "_max_inline_image_bytes",
        "_last_notice_key",
        "_last_push_key",
        "_last_frame",
    )

    def __init__(
        self,
        *,
        runtime: RuntimePort,
        logic: CompanionLogic,
        push: PushFn,
        cache_dir: Path,
        data_dir: Path,
        engine_root: Path | None = None,
        upload_image: UploadImageFn | None = None,
        store_get: StorePort | None = None,
        config_get: ConfigPort | None = None,
        verbose_diagnostics: bool = False,
        max_inline_image_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        """路径全部**构造注入**（与 runtime 同一条纪律：本层不知道宿主存在）。

        ``engine_root`` 默认 ``data_dir / "maa"`` —— 与 ``data_path("maa")`` 同义。
        **没有第二处可配置的引擎位置**：不读环境变量、不探测注册表/winget。

        ``verbose_diagnostics`` 只影响 **diagnostics**（进 data/日志），
        **不影响 parts**：诊断信息再详细也不许进聊天窗（那里只有猫娘该说的话）。
        """
        self._runtime = runtime
        self._logic = logic
        self._push_fn = push
        self._upload_image = upload_image
        self._store_get = store_get
        self._config_get = config_get or (lambda: {})
        self._cache_dir = Path(cache_dir)
        self._data_dir = Path(data_dir)
        self._engine_root = Path(engine_root) if engine_root is not None else self._data_dir / "maa"
        self._verbose = bool(verbose_diagnostics)
        self._max_inline_image_bytes = max(int(max_inline_image_bytes), 0)

        self._last_notice_key: str | None = None
        self._last_push_key: str | None = None
        self._last_frame: tuple[bytes, tuple[int, int] | None] | None = None

    # ------------------------------------------------------------------
    # 业务入口（7 个，与 __init__.py 的 entry 一一对应）
    # ------------------------------------------------------------------
    async def handle_capture(self, *, save: bool = False) -> CaptureOutcome:
        """截图 → 组 parts → push。**任何 EngineError 都被吸收**，不向上抛。

        图片交付三档（顺序不可换）：

          1. 小图 → **内联 image part**（``data`` 直接上行，不给路径）；
          2. 大图 → ``await upload_image(...)``，把**宿主返回的 part 原样**放进 parts；
          3. upload 不可用/失败 → 落盘 + 在文案里给路径（降级不改主结论）。

        ``save=True`` 只**额外**落一份盘（用户要的是文件），不改变上面的交付路径。
        """
        diagnostics: dict[str, Any] = {"save": bool(save)}
        try:
            png = await self._runtime.capture()
        except EngineError as exc:
            return self._capture_failure(exc, diagnostics)
        except Exception as exc:  # noqa: BLE001 - 入口不许把裸异常留给宿主
            logger.exception("runtime.capture() raised a non-EngineError")
            diagnostics["unexpected"] = type(exc).__name__
            return self._capture_failure(None, diagnostics)

        if not isinstance(png, (bytes, bytearray)) or not png:
            # 空字节不是"成功但没图"：契约里它是故障，得说清楚。
            return self._capture_failure(
                EngineNotReady(state=self._state_value(), required=("capture_bytes",)),
                diagnostics,
            )

        data = bytes(png)
        size = _png_size(data)
        if size is None:
            # 兜底：adapter 已校验魔数，走到这里说明库的产出形态可疑。
            # **绝不重编码**；原样落盘供开发者核对版本。
            saved_bad = self._save_bytes(data, "capture-badformat")
            return self._capture_failure(
                CaptureFormatUnexpected(magic=data[:8], length=len(data), payload=data),
                diagnostics,
                saved_path=saved_bad,
            )

        width, height = size
        too_large = len(data) > self._max_inline_image_bytes
        saved: Path | None = None
        image_parts: list[MessagePart] = []

        if too_large:
            uploaded = await self._try_upload(data)
            if uploaded is not None:
                image_parts.append(uploaded)
            else:
                saved = self._save_bytes(data, "capture")
        else:
            image_parts.append({"type": "image", "data": data, "mime": "image/png"})

        if save:
            saved = saved or self._save_bytes(data, "capture")

        head = f"博士，画面拿到啦喵～ {width}×{height}"
        if too_large and saved is not None:
            head += f"（这张太大，直接塞对话会被丢掉，我先存到 {saved}）"
        parts: list[MessagePart] = [_text(head), *image_parts]

        pushed, reason = await self._push(
            parts,
            source="arknights_capture",
            visibility=("chat",),
            ai_behavior="respond",
            priority=4,
        )

        diagnostics.update(
            {"width": width, "height": height, "bytes": len(data),
             "pushed": pushed, "reason": reason}
        )
        logger.info(
            "capture ok: %dx%d %d bytes saved=%s pushed=%s",
            width, height, len(data), saved, pushed,
        )
        return CaptureOutcome(
            ok=True,
            image=data,
            width=width,
            height=height,
            image_format="png",
            parts=tuple(parts),
            pushed=pushed,
            push_failure_reason=reason,
            saved_path=saved,
            too_large_for_inline=too_large,
            error=None,
            user_text=head,
            diagnostics=diagnostics,
        )

    async def handle_status(self) -> StatusOutcome:
        """只读状态。**恒 ``ok=True``**（"查状态"这件事本身成功了）。

        推送按 ``状态 + 进度 + 错误码`` 去重：同一个百分比不重复推（进度播报要节流）。
        """
        snapshot = self._snapshot()
        state = str(snapshot.get("state") or self._state_value())
        progress = snapshot.get("download_progress", self._download_progress())
        progress_int = progress if isinstance(progress, int) and not isinstance(progress, bool) else None

        lines = [self._state_line(state, progress_int)]
        device = snapshot.get("device")
        if isinstance(device, Mapping):
            lines.append(self._device_line(device))

        error = snapshot.get("error")
        error_code: str | None = None
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, str) and code:
                error_code = code
                lines.append(f"最近一次故障码是 {code} 喵（细节在 data 里，我没往聊天里写路径）。")

        parts: list[MessagePart] = [_text("\n".join(line for line in lines if line))]
        user_text = "；".join(line for line in lines if line)
        dedupe = f"status:{state}:{progress_int}:{error_code}"
        pushed, _reason = await self._push(
            parts,
            source="arknights_status",
            visibility=("chat",),
            ai_behavior="read",
            priority=2,
            dedupe_key=dedupe,
        )
        diagnostics = {"pushed": pushed, "state": state, "progress": progress_int}
        return StatusOutcome(
            ok=True,
            snapshot=snapshot,
            parts=tuple(parts) if pushed else (),
            user_text=user_text,
            diagnostics=diagnostics,
        )

    async def handle_analyze_stage(self, *, stage: str = "", concern: str = "") -> StageOutcome:
        """关卡分析。**显式给了 ``stage`` 时不依赖设备**（纯文本分析即可成功）。

        没给 ``stage`` 时会截一张图（只在本函数内部用，**不上行**），
        但 OCR 结果回收链路本轮**没接**：``ocr_ok=False`` + ``ocr_error_code="ocr.not_wired"``。
        这里刻意**不假装识别成功**——假装的话，用户会拿到一条基于空文本的"建议"，
        看起来很像真的。
        """
        stage_text = (stage or "").strip()
        diagnostics: dict[str, Any] = {"stage": stage_text, "concern": bool(concern)}
        ocr_ok = False
        ocr_error: str | None = None
        ocr_text = ""     # OCR 结果回收未接（v1），所以这里恒为空；不假装有内容。

        if not stage_text:
            try:
                png = await self._runtime.capture()
                self._last_frame = (bytes(png), _png_size(png))
                diagnostics["frame_bytes"] = len(self._last_frame[0])
                # TODO_VERIFY: OCR 链路未接（maa/ 里没有 OCR 结果回收）。
                ocr_error = "ocr.not_wired"
            except EngineError as exc:
                ocr_error = exc.code.value
                diagnostics["capture_error"] = ocr_error
                parts = self._render_error(exc)
                visibility, ai_behavior, priority = push_meta(exc)
                pushed, reason = await self._push(
                    parts,
                    source="arknights_analyze_stage",
                    visibility=visibility,
                    ai_behavior=ai_behavior,
                    priority=priority,
                )
                diagnostics.update({"pushed": pushed, "reason": reason})
                return StageOutcome(
                    ok=False,
                    stage="",
                    advice=None,
                    ocr_ok=False,
                    ocr_error_code=ocr_error,
                    parts=tuple(parts),
                    user_text=self._join_text(parts),
                    diagnostics=diagnostics,
                )

        ctx = PlayerContext(stage=stage_text, concern=concern or "", ocr_text=ocr_text)
        advice = self._logic.decide(ctx)
        # 关卡代号只从"用户明确给的"或"OCR 真读到的"来；两者都没有就留空。
        # **绝不从 advice.summary 里反解** —— 那是把文案当数据结构用。
        resolved_stage = stage_text or (self._logic.knowledge.extract_stage(ocr_text) or "")

        lines = [advice.summary, *advice.tips]
        parts = [_text("\n".join(line for line in lines if line))]
        pushed, reason = await self._push(
            parts,
            source="arknights_analyze_stage",
            visibility=("chat",),
            ai_behavior="respond",
            priority=3,
        )
        diagnostics.update({"pushed": pushed, "reason": reason, "ocr_ok": ocr_ok})
        return StageOutcome(
            ok=True,
            stage=resolved_stage,
            advice=advice,
            ocr_ok=ocr_ok,
            ocr_error_code=ocr_error,
            parts=tuple(parts),
            user_text=self._join_text(parts),
            diagnostics=diagnostics,
        )

    async def handle_reconnect(self) -> StatusOutcome:
        """重连设备。地址与**连接配置**都来自配置（空地址就明确说"没配"、不回落 5555）。

        ``adb_connect_config`` / ``adb_connect_extras`` 在这里原样透传给 ``runtime.connect``
        （解析规则见 :func:`resolve_connect_params`）。**默认值不往下传** ——
        ``RuntimePort`` 的窄接口只有 ``address`` / ``adb_path``，而
        ``config="General"`` / ``extras=None`` 与"不传"在 runtime 里语义完全等价，
        总是传只会让只实现窄接口的端口实现收到不认识的关键字参数。见 :meth:`_connect_kwargs`。

        **这是"用户显式点了重连"**，所以优先走 ``connect_forced()``（= ``connect(force=True)``）：
        否则设备"假活"时（``READY`` 但截图一直超时、没有 ``Disconnect`` 事件）会被幂等
        短路成"什么都没发生"，用户只会认为按钮坏了。``connect_forced`` 是**可选能力**，
        用 ``getattr`` 探测（与 ``handle_engine_repair`` 探测 ``purge`` 同一套写法）：
        端口实现没有它就退回普通 ``connect()``，行为与以前一致，不会因为多一个可选能力
        要求所有实现都改签名。
        """
        config = self._config()
        address = str(config.get("adb_address", "") or "").strip()
        adb_path = str(config.get("adb_path", "") or "").strip()
        connect_config, extras = resolve_connect_params(config)
        kwargs = self._connect_kwargs(
            address=address,
            adb_path=adb_path,
            config=connect_config,
            extras=extras,
        )
        # 能力探测：真 runtime 有 connect_forced，用例替身/老实现没有。
        force_connect = getattr(self._runtime, "connect_forced", None)
        diagnostics: dict[str, Any] = {
            "address_configured": bool(address),
            # 配置名与 extras 的**键**（不含值：值是用户机器上的绝对路径，只进日志）
            "connect_config": connect_config,
            "extras_keys": sorted(extras or {}),
            # False = 这个端口实现没有强制重连能力，走的是普通 connect（可能被幂等短路）
            "forced": callable(force_connect),
        }
        steps: list[str] = ["connect"]
        try:
            try:
                if callable(force_connect):
                    device = await force_connect(**kwargs)
                else:
                    device = await self._runtime.connect(**kwargs)
            except TypeError:
                # 端口实现的 connect 只认 (address, adb_path)：我们多传的 config/extras 被
                # Python 拒了（这也是 _connect_kwargs 只在"用户真的配了"时才下发的理由）。
                # **文案不动**（改文案要动错误分档，收益不抵风险），只留一条能定位的 warning，
                # 其余异常照样往上走。只吞 TypeError —— 扩成 Exception 会把真故障也伪装成这条。
                rejected = [key for key in ("config", "extras") if key in kwargs]
                logger.warning(
                    "runtime.connect() 不接受连接参数（被拒的键：%s）；"
                    "该端口实现只支持 address/adb_path，本次连接参数没有生效",
                    "/".join(rejected) if rejected else "无（kwargs 只有基础参数）",
                )
                raise
        except EngineError as exc:
            steps.append("failed")
            parts = self._render_error(exc)
            visibility, ai_behavior, priority = push_meta(exc)
            pushed, reason = await self._push(
                parts,
                source="arknights_reconnect",
                visibility=visibility,
                ai_behavior=ai_behavior,
                priority=priority,
            )
            diagnostics.update({"pushed": pushed, "reason": reason,
                                "error_code": exc.code.value if exc.code else None})
            return StatusOutcome(
                ok=False,
                snapshot=self._snapshot(),
                parts=tuple(parts),
                user_text=self._join_text(parts),
                diagnostics=diagnostics,
                steps=tuple(steps),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconnect raised a non-EngineError")
            steps.append("failed")
            diagnostics["unexpected"] = type(exc).__name__
            parts = [_text("连设备这一下出了问题喵，我先记下来了；再点一次试试。")]
            return StatusOutcome(
                ok=False,
                snapshot=self._snapshot(),
                parts=tuple(parts),
                user_text=self._join_text(parts),
                diagnostics=diagnostics,
                steps=tuple(steps),
            )

        steps.append("connected")
        text = f"连上啦喵～{self._resolution_text(device)}"
        parts: list[MessagePart] = [_text(text)]
        pushed, reason = await self._push(
            parts,
            source="arknights_reconnect",
            visibility=("chat",),
            ai_behavior="respond",
            priority=3,
        )
        diagnostics.update({"pushed": pushed, "reason": reason})
        return StatusOutcome(
            ok=True,
            snapshot=self._snapshot(),
            parts=tuple(parts),
            user_text=text,
            diagnostics=diagnostics,
            steps=tuple(steps),
        )

    async def handle_engine_repair(self, *, clean: bool = False) -> StatusOutcome:
        """修复/重下引擎。**不接受 URL、不接受路径**（接口级安全约束，见 C8）。

        进度由**后台任务**上报（``progress_sink``），本方法只 kick 一次并回快照：
        它要是 ``await`` 到下载完，280s 的 entry 预算照样会被慢网打穿。
        """
        steps: list[str] = []
        diagnostics: dict[str, Any] = {"clean": bool(clean)}
        try:
            if clean:
                # 显式清盘：只在用户点了"清掉重来"时才走，别在正常重试里顺手删用户文件。
                purge = getattr(self._runtime, "purge", None)
                if purge is not None:
                    await purge()
                    steps.append("purge")
            await self._runtime.bootstrap(force=True)
            steps.append("bootstrap")
        except EngineError as exc:
            steps.append("failed")
            parts = self._render_error(exc)
            visibility, ai_behavior, priority = push_meta(exc)
            pushed, reason = await self._push(
                parts,
                source="arknights_engine_repair",
                visibility=visibility,
                ai_behavior=ai_behavior,
                priority=priority,
            )
            diagnostics.update({"pushed": pushed, "reason": reason,
                                "error_code": exc.code.value if exc.code else None})
            return StatusOutcome(
                ok=False,
                snapshot=self._snapshot(),
                parts=tuple(parts),
                user_text=self._join_text(parts),
                diagnostics=diagnostics,
                steps=tuple(steps),
            )

        snapshot = self._snapshot()
        state = str(snapshot.get("state") or self._state_value())
        progress = snapshot.get("download_progress")
        progress_int = progress if isinstance(progress, int) and not isinstance(progress, bool) else None
        text = self._state_line(state, progress_int)
        parts: list[MessagePart] = [_text(text)]
        # 进度播报节流：同一状态+同一百分比不重复推。
        pushed, reason = await self._push(
            parts,
            source="arknights_engine_repair",
            visibility=("chat",),
            ai_behavior="read",
            priority=3,
            dedupe_key=f"repair:{state}:{progress_int}",
        )
        diagnostics.update({"pushed": pushed, "reason": reason, "state": state})
        return StatusOutcome(
            ok=True,
            snapshot=snapshot,
            parts=tuple(parts) if pushed else (),
            user_text=text,
            diagnostics=diagnostics,
            steps=tuple(steps),
        )

    async def handle_roguelike_advice(
        self,
        *,
        theme: str = "",
        current_operators: Sequence[str] = (),
        hope: int | None = None,
        layer: int | None = None,
    ) -> AdviceOutcome:
        """肉鸽建议：**纯静态知识、无设备、无网络**，所以恒 ``ok=True`` 且不 push。

        不 push 是刻意的：这是"用户问了我才答"的纯文本，主动推一条建议
        只会在聊天窗里制造噪声（与"状态播报"不同，后者有时效性）。
        """
        roster = tuple(str(name).strip() for name in (current_operators or ()) if str(name).strip())
        advice = self._logic.knowledge.roguelike_advice(
            theme=theme or "",
            roster=roster,
            hope=hope if isinstance(hope, int) and not isinstance(hope, bool) else None,
            layer=layer if isinstance(layer, int) and not isinstance(layer, bool) else None,
        )
        return AdviceOutcome(
            ok=True,
            theme=(theme or "").strip(),
            advice=tuple(advice),
            parts=(),
            user_text="；".join(advice),
            diagnostics={"count": len(advice)},
        )

    async def handle_memory(self) -> MemoryOutcome:
        """陪玩记忆：读三个缓存键。**读失败一律回落 ``None``**（缓存不值得让入口失败）。"""
        operators = await self._store_value("operators_cache")
        depot = await self._store_value("depot_cache")
        last_stage = await self._store_value("last_stage")
        return MemoryOutcome(
            ok=True,
            operators_cache=operators,
            depot_cache=depot,
            last_stage=last_stage,
            state=self._state_value(),
        )

    # ------------------------------------------------------------------
    # 内部：渲染 / 推送
    # ------------------------------------------------------------------
    def _render_error(self, exc: EngineError, *, saved_path: Path | None = None) -> list[MessagePart]:
        """查 §3.5.1 的映射表 → 组 ``parts``。**永不抛错**（定时/后台路径也走它）。"""
        try:
            return render_error(
                exc,
                engine_root=str(self._engine_root),
                saved_path=str(saved_path) if saved_path is not None else "",
            )
        except Exception:  # noqa: BLE001 - 渲染失败也不能让入口炸
            logger.exception("render_error failed for code=%s", getattr(exc, "code", None))
            return [_text("引擎那边出了点状况喵，我先记下来了；再试一次看看。")]

    async def _push(
        self,
        parts: Sequence[MessagePart],
        *,
        source: str,
        visibility: Sequence[str] = ("chat",),
        ai_behavior: Literal["respond", "read", "blind"] = "read",
        priority: int = 3,
        dedupe_key: str | None = None,
    ) -> tuple[bool, str | None]:
        """包一层 ``push``：**同步调用**、读 ``submitted``、把 ``reason`` 规范化。

        * ``submitted=False`` 时**不重试**（同轮重试只放大 backpressure），
          只记日志 + 把原因回传给 Outcome；
        * ``push`` 自己抛异常也在这里吞掉——"消息没发出去"绝不能变成"入口失败"；
        * ``dedupe_key`` 相同则直接跳过（进度播报/重复状态靠它节流）。
        """
        if not parts:
            return (False, "empty")
        if dedupe_key is not None and dedupe_key == self._last_push_key:
            return (False, "deduped")

        kwargs: dict[str, Any] = {
            "source": source,
            "visibility": list(visibility),
            "ai_behavior": ai_behavior,
            "parts": list(parts),
            "priority": int(priority),
        }
        try:
            result = self._push_fn(**kwargs)      # 同步：绝不 await
        except Exception:  # noqa: BLE001
            logger.warning("push_message raised; treating as transport_error", exc_info=True)
            return (False, "transport_error")

        submitted, reason = self._interpret_push_result(result)
        if submitted:
            self._last_push_key = dedupe_key
            return (True, None)
        logger.info(
            "push_message rejected: source=%s reason=%s hint=%s",
            source, reason, _PUSH_REJECT_HINT.get(reason, "未归类（按 transport_error 处置）"),
        )
        return (False, reason)

    @staticmethod
    def _interpret_push_result(result: Any) -> tuple[bool, str | None]:
        """读 push 的返回值。判据是 ``submitted``；``ok`` 只是旧兼容。

        ``reason`` **必须归一**（S06）：宿主只保证 ``backpressure`` /
        ``transport_error`` / ``transport_unavailable`` 三个稳定值，
        白名单外的字符串一律回落 ``transport_error`` —— 否则下游会看到
        第四个"稳定码"，而它其实是自由文本。
        """
        if isinstance(result, Mapping):
            if "submitted" in result:
                submitted = bool(result.get("submitted"))
            else:
                submitted = bool(result.get("ok"))
            if submitted:
                return (True, None)
            return (False, _normalize_push_reason(result.get("reason")))
        if isinstance(result, bool):
            return (result, None if result else "transport_error")
        # 拿不到 dict 就无法判断成败：按"没送到"处理，比假装成功安全。
        logger.warning("push_message returned non-mapping: %s", type(result).__name__)
        return (False, "transport_error")

    def _capture_failure(
        self,
        exc: EngineError | None,
        diagnostics: dict[str, Any],
        *,
        saved_path: Path | None = None,
    ) -> CaptureOutcome:
        """截图失败的统一出口（同步：push 由调用方 await 不了，所以这里直接 push）。"""
        if exc is None:
            parts: list[MessagePart] = [
                _text("截图这一步出了点我自己也没算到的问题喵，先记下来了；再试一次看看。")
            ]
            visibility, ai_behavior, priority, error_code = ("chat",), "read", 3, None
        else:
            parts = self._render_error(exc, saved_path=saved_path)
            visibility, ai_behavior, priority = push_meta(exc)
            error_code = exc.code.value if exc.code is not None else None
            diagnostics["error_context"] = _jsonable_context(exc.context)

        # 这里只能同步 push：本方法不是 async，而 _push 里也**没有 await**（push_message 同步）。
        # 节流（S07）：同一个故障码在同一个消息窗里只推一次 —— 连拍三张都失败时，
        # 用户看到的应该是"还是不行"，不是同一句话刷三屏。
        # 注意：**成功路径不去重**（用户就是要连拍），只有这里带 dedupe_key。
        pushed, reason = self._push_sync(
            parts,
            source="arknights_capture",
            visibility=visibility,
            ai_behavior=ai_behavior,
            priority=priority,
            dedupe_key=f"capture:fail:{error_code or 'unexpected'}",
        )
        user_text = self._join_text(parts)
        diagnostics.update({"pushed": pushed, "reason": reason, "error_code": error_code})
        logger.info("capture failed: code=%s pushed=%s", error_code, pushed)
        return CaptureOutcome(
            ok=False,
            image=None,
            width=0,
            height=0,
            image_format="",
            parts=tuple(parts),
            pushed=pushed,
            push_failure_reason=reason,
            saved_path=saved_path,
            too_large_for_inline=False,
            error=exc,
            user_text=user_text,
            diagnostics=diagnostics,
        )

    def _push_sync(
        self,
        parts: Sequence[MessagePart],
        *,
        source: str,
        visibility: Sequence[str],
        ai_behavior: str,
        priority: int,
        dedupe_key: str | None = None,
    ) -> tuple[bool, str | None]:
        """``_push`` 的同步孪生体。

        ``push_message`` 本身是同步的，所以"同步路径"不需要 await；
        但两处共用同一套判据（``_interpret_push_result``）——
        否则"成功/失败怎么判"就会有两个真相。

        ``dedupe_key`` 与 :meth:`_push` **共用同一个** ``_last_push_key``：
        "最近推过什么"只有一个真相，否则状态播报与截图失败会各刷一遍。

        去重语义与 :meth:`_push` **逐条一致**：命中就跳过并返回 ``(False, "deduped")``；
        **只有 ``submitted=True`` 才更新** ``_last_push_key`` ——
        一次失败若也写入 key，会把后续同类消息**永久**节流掉（用户再也收不到）。
        """
        if not parts:
            return (False, "empty")
        if dedupe_key is not None and dedupe_key == self._last_push_key:
            return (False, "deduped")
        try:
            result = self._push_fn(
                source=source,
                visibility=list(visibility),
                ai_behavior=ai_behavior,
                parts=list(parts),
                priority=int(priority),
            )
        except Exception:  # noqa: BLE001
            logger.warning("push_message raised; treating as transport_error", exc_info=True)
            return (False, "transport_error")
        submitted, reason = self._interpret_push_result(result)
        if submitted:
            # 与 :meth:`_push` 逐条一致：**成功就写回** ``dedupe_key``（可能是 None，
            # 那等于"重置节流"—— 故障恢复后再出错应当重新告知用户，不该被旧 key 吞掉）。
            self._last_push_key = dedupe_key
        return (submitted, reason)

    # ------------------------------------------------------------------
    # 内部：状态/快照
    # ------------------------------------------------------------------
    def _state_value(self) -> str:
        """``runtime.state`` 的字符串值。**只读 ``.value``，不比较枚举成员。**"""
        state = getattr(self._runtime, "state", None)
        value = getattr(state, "value", None)
        return value if isinstance(value, str) else "unknown"

    def _download_progress(self) -> int | None:
        """0–100；不在下载时为 ``None``。**``None`` 不是 0%**，渲染时必须分开。"""
        progress = getattr(self._runtime, "download_progress", None)
        if isinstance(progress, int) and not isinstance(progress, bool):
            return progress
        return None

    def _snapshot(self, *, include_engine_root: bool = False) -> dict[str, Any]:
        """取一份**白名单**状态快照，可直接进 entry 的 ``data``（模型读得到）。

        **不是** ``dict(runtime.status_snapshot())``。原始快照里有四类绝不能出模型的东西：

          * ``error`` = ``exc.to_dict()`` —— 里面的 ``message`` 与 ``context`` 含
            绝对路径，``context["url"]`` 甚至可能带 **SAS token**（``?sv=...&sig=...``）；
          * ``library_path`` / ``resource_parent`` / ``searched`` —— 全是绝对路径；
          * ``target`` / ``archive`` / ``root`` —— 同上。

        所以这里**逐键白名单重建**：只挑封闭词表的标量（``state`` / ``kind`` / ``shape``
        / ``progress`` …）。``engine_root`` 是唯一刻意保留的绝对路径，且只给
        ``arknights_status`` / ``arknights_engine_repair`` 两个入口（用户必须知道把包放哪），
        所以它由 ``include_engine_root`` 显式开关，不当默认值。

        白名单而不是黑名单，是因为新增字段的人只会往"看起来有用"的方向加：
        黑名单每漏一次就是一次泄漏，白名单每漏一次只是少给一个字段。
        """
        try:
            raw = dict(self._runtime.status_snapshot() or {})
        except Exception:  # noqa: BLE001 - 快照失败不该连坐入口
            logger.warning("status_snapshot() failed", exc_info=True)
            raw = {}

        safe: dict[str, Any] = {"state": str(raw.get("state") or self._state_value())}

        progress = raw.get("download_progress", self._download_progress())
        if isinstance(progress, int) and not isinstance(progress, bool):
            safe["download_progress"] = progress
        else:
            # 键一定存在，值可以是 None —— "不在下载"与"还没报上来"必须可区分。
            safe["download_progress"] = None

        # release_version 是版本串（如 "6.22.0"），不是路径，可以给。
        version = raw.get("release_version")
        if isinstance(version, str) and version:
            safe["release_version"] = version

        # missing_pieces 是 *件名*（"library"/"resource"/"engine.json"），不是路径。
        pieces = raw.get("missing_pieces")
        if isinstance(pieces, (list, tuple)):
            safe["missing_pieces"] = [str(item) for item in pieces]

        # 三个**封闭词表**的短字符串：范围声明 / 链路四态 / 连接配置名。
        # 它们进 data 的理由都是"让报障能当场排除一个变量"：
        #   * client_scope：识别不准时先排除"外服"（我们只支持国服官服）；
        #   * link_state：断线后 state 是 loaded，光看 state 看不出"为什么退回 loaded"；
        #   * connect_config：模拟器填错配置名时，用户能当场看到"实际走的是哪条连接路径"。
        # 仍然过一遍 sanitize_maa_text：connect_config 是用户可控字符串（理论上能填路径）。
        for key, limit in (("client_scope", 32), ("link_state", 32), ("connect_config", 64)):
            value = raw.get(key)
            if isinstance(value, str) and value:
                safe[key] = sanitize_maa_text(value, limit=limit)

        # extras 下发结果：None（这次连接没配）/ {"keys", "dispatched", "error"}。
        # 这是"配了 MuMu12 却没生效"在用户侧的**唯一可见证据**（C 侧 void + 静默 return，
        # 不会报错），所以必须进 data。键名与错误串都过消毒；路径值本来就没进 runtime 这个字段。
        if "connection_extras" in raw:
            safe["connection_extras"] = _safe_connection_extras(raw.get("connection_extras"))

        device = raw.get("device")
        if isinstance(device, Mapping):
            safe["device"] = _safe_device(device)

        error = raw.get("error")
        if isinstance(error, Mapping):
            cleaned = _safe_error(error)
            if cleaned:
                safe["error"] = cleaned

        if include_engine_root:
            safe["engine_root"] = str(self._engine_root)

        return safe

    def _state_line(self, state: str, progress: int | None) -> str:
        """七态 + 进度 → 一句人话。进度播报**节流**（同百分比不重复推）。

        ``progress is None`` 时**不许**渲染成 0%：None 的意思是"进度还没报上来"，
        说成 0% 会让用户以为卡住了，实际可能只是后台任务还没跑到第一次回调。
        """
        label = STATE_TEXT.get(state, f"未知阶段（{state}）")
        if state == "downloading":
            if progress is None:
                return f"{label}喵（进度还没报上来，先别急着以为卡住了）。"
            key = f"progress:{progress}"
            if not self._claim_notice(key):
                return f"{label}喵，还是 {progress}%。"
            return f"{label}喵，已经 {progress}% 了。"
        return f"{label}喵。"

    def _device_line(self, device: Mapping[str, Any]) -> str:
        connected = bool(device.get("connected"))
        if not connected:
            return "设备：还没连上。"
        resolution = str(device.get("resolution") or "unknown")
        width = device.get("width")
        height = device.get("height")
        if resolution == "known" and isinstance(width, int) and isinstance(height, int):
            return f"设备：已连接（分辨率 {width}×{height}）。"
        # 三态里的 unknown/failed：**不允许**用默认尺寸兜底，直接说"没读到"。
        return "设备：已连接，但分辨率还没读到。"

    def _resolution_text(self, device: Any) -> str:
        """连接成功那句话的尾巴。分辨率三态，不拿默认值冒充。"""
        resolution = getattr(device, "resolution", None)
        status = getattr(resolution, "status", None)
        status_value = getattr(status, "value", "")
        if status_value == "known":
            width = getattr(resolution, "width", None)
            height = getattr(resolution, "height", None)
            if isinstance(width, int) and isinstance(height, int):
                return f"分辨率是 {width}×{height}。"
        return "分辨率还没读到喵（不影响截图，坐标相关的功能先别用）。"

    def _claim_notice(self, key: str) -> bool:
        """同一 key 只放行一次：进度/重复状态靠它节流。"""
        if key == self._last_notice_key:
            return False
        self._last_notice_key = key
        return True

    def _config(self) -> Mapping[str, Any]:
        try:
            config = self._config_get()
        except Exception:  # noqa: BLE001 - 配置读失败不该连坐业务
            logger.warning("config_get() failed; using empty config", exc_info=True)
            return {}
        return config if isinstance(config, Mapping) else {}

    @staticmethod
    def _connect_kwargs(
        *,
        address: str,
        adb_path: str,
        config: str,
        extras: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        """组 ``runtime.connect()`` 的关键字参数：**只把用户真正改过的传下去**。

        ``config == "General"`` / ``extras`` 为空时不下发，因为那与 runtime 的默认值
        逐字等价；而下发默认值会让每一个只实现窄接口（``address`` + ``adb_path``）的
        端口实现炸成 ``TypeError`` —— 把"用户没配"变成"插件坏了"，是最不该发生的降级。
        """
        kwargs: dict[str, Any] = {"address": address, "adb_path": adb_path}
        if config != DEFAULT_CONNECT_CONFIG:
            kwargs["config"] = config
        if extras:
            kwargs["extras"] = dict(extras)
        return kwargs

    # ------------------------------------------------------------------
    # 内部：图片交付
    # ------------------------------------------------------------------
    async def _try_upload(self, png: bytes) -> MessagePart | None:
        """大图走宿主上传；不可用/失败一律返回 ``None``（调用方降级落盘）。

        返回**宿主给的 part 原样**：自己拼 ``{"type":"image","data":...}`` 会把
        同一份字节又塞回消息包预算，等于没上传。
        """
        upload = self._upload_image
        if upload is None:
            return None
        try:
            part = await upload(png, "image/png")
        except Exception:  # noqa: BLE001 - 上传失败不是业务失败
            logger.warning("images.upload failed; falling back to disk", exc_info=True)
            return None
        if not isinstance(part, Mapping):
            logger.warning("images.upload returned non-mapping: %s", type(part).__name__)
            return None
        if part.get("type") != "image":
            logger.warning("images.upload returned unexpected part type: %r", part.get("type"))
            return None
        return dict(part)  # type: ignore[return-value]

    def _save_bytes(self, data: bytes, stem: str) -> Path | None:
        """原样落盘（**不重编码、不改格式**）。失败返回 ``None``，绝不抛。"""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._cache_dir / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            path.write_bytes(data)
            return path
        except OSError:
            logger.warning("保存图片失败", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 内部：杂项
    # ------------------------------------------------------------------
    @staticmethod
    def _join_text(parts: Sequence[MessagePart]) -> str:
        """把 text parts 拼成一句，供 entry 的 ``Err(SdkError(...))`` 复用文案。"""
        chunks: list[str] = []
        for part in parts:
            if part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
        return "\n".join(chunks)

    async def _store_value(self, key: str) -> Any:
        getter = self._store_get
        if getter is None:
            return None
        try:
            return await getter(key)
        except Exception:  # noqa: BLE001 - 缓存读失败回落 None
            logger.warning("store_get(%s) failed", key, exc_info=True)
            return None


_SAFE_CONTEXT_KEYS: Final[frozenset[str]] = frozenset({
    # 关闭词表的机器标量：每个都是"枚举值 / 布尔 / 计数 / 短标识"，没有一个是路径。
    "code", "kind", "shape", "state", "status", "progress", "required",
    "task_type", "address", "timeout_s", "call_id", "ret", "cost_ms",
    "load_ok", "user_dir_ok", "resource_loaded", "symbol", "oserror",
    "buffer_size", "length", "magic", "missing", "version", "actual",
})
"""``context`` 里可以进 ``data`` 的键**全表**（白名单）。

刻意**不在**表里的（安全侧复核结论，别加回来）：
``url``（可能带 SAS token）、``target`` / ``archive`` / ``root`` /
``expected_dir`` / ``library_path`` / ``resource_parent`` / ``user_dir`` /
``searched``（全是绝对路径）、``detail`` / ``why`` / ``message``（上游自由文本，
要给人看必须走 :func:`sanitize_maa_text` 并单独成段）。
"""


def _jsonable_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """``exc.context`` → **白名单**子集（进 ``diagnostics`` 用，模型可能读到）。

    两条同时成立才放行：① 键在 :data:`_SAFE_CONTEXT_KEYS` 里；
    ② 值折叠后仍是标量/短列表。``Path`` **直接丢键**，不折成字符串——
    一旦折成字符串它就成了一条绝对路径，而 ``data`` 里只允许 ``engine_root``。
    """
    cleaned: dict[str, Any] = {}
    for key, value in context.items():
        if key not in _SAFE_CONTEXT_KEYS:
            continue
        folded = _fold_scalar(value)
        if folded is not _DROP:
            cleaned[key] = folded
    return cleaned


class _Drop:
    """``_fold_scalar`` 的"这个值不能出模型"哨兵（用单例，避免与 None 混淆）。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只用于调试
        return "<drop>"


_DROP: Final = _Drop()


def _fold_scalar(value: Any) -> Any:
    """把上下文值折成"可进 data"的形态；折不出来就返回 :data:`_DROP`。

    * 枚举 → ``.value``（``kind`` / ``shape`` 这类键靠它保留）；
    * 字符串 → 过 :func:`sanitize_maa_text` 并截到 64 字（它只该是短标识）；
    * 数字/布尔/None → 原样；
    * 字符串/数字的短序列 → 逐项折叠（``required=("download_complete",)`` 用得上）；
    * ``Path`` / ``bytes`` / 其它对象 → **丢弃**。
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return sanitize_maa_text(value, limit=64)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_fold_scalar(item) for item in value]
        keep = [item for item in items[:6] if item is not _DROP]
        return keep
    inner = getattr(value, "value", None)      # 枚举（DownloadFailure / UnpackFailure / …）
    if isinstance(inner, str):
        return sanitize_maa_text(inner, limit=64)
    if isinstance(inner, int) and not isinstance(inner, bool):
        return inner
    return _DROP


def _safe_device(device: Mapping[str, Any]) -> dict[str, Any]:
    """设备子表：只留连接状态与分辨率三态（没有地址、没有 uuid）。"""
    safe: dict[str, Any] = {"connected": bool(device.get("connected"))}
    resolution = device.get("resolution")
    if isinstance(resolution, str) and resolution:
        safe["resolution"] = resolution
    for key in ("width", "height"):
        value = device.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            safe[key] = value
    return safe


def _safe_connection_extras(value: Any) -> dict[str, Any] | None:
    """``status_snapshot()['connection_extras']`` → 进 ``data`` 的形态。

    ``None`` 原样保留（= 这次连接没配 extras，"没配"与"配了没发出去"必须可区分）。
    非 Mapping 一律折成 ``None``：这个字段只有 runtime 会写，形状不对说明有人在别处
    塞了东西，按"没有信息"处理比原样透传安全。

    ``keys`` / ``error`` 都过 :func:`sanitize_maa_text`：键名本该是 ``path`` 这种短标识，
    但配置是外部输入（``{"C:\\Users\\me": ...}`` 这种键名并非不可能）；``error`` 来自
    异常原文，可能带库路径（``MaaCallError.detail`` 里有 ``library_path``）。
    """
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {"dispatched": bool(value.get("dispatched"))}
    keys = value.get("keys")
    if isinstance(keys, (list, tuple)):
        picked = [sanitize_maa_text(str(key), limit=32) for key in keys[:6]]
        out["keys"] = [key for key in picked if key]
    error = value.get("error")
    if isinstance(error, str) and error.strip():
        out["error"] = sanitize_maa_text(error, limit=120)
    return out


def _safe_error(error: Mapping[str, Any]) -> dict[str, Any]:
    """``exc.to_dict()`` → 只剩 ``code`` + 白名单标量。

    ``message`` 与 ``context`` 一律**不整体透传**（那是 ``to_dict()`` 的职责，
    它只许进日志/落盘）；这里逐键挑，等于把"能不能给模型看"变成一张可审的表。
    """
    safe: dict[str, Any] = {}
    code = error.get("code")
    if isinstance(code, str) and code:
        safe["code"] = code
    context = error.get("context")
    if isinstance(context, Mapping):
        picked = _jsonable_context(context)
        for key, value in picked.items():
            if key == "code":
                continue          # 顶层已经有一个 code，别出两份
            safe[key] = value
    return safe
