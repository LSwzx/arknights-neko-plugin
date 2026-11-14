"""明日方舟陪玩插件 —— 宿主接线层（**纯路由**）。

本文件只允许做五件事（``docs/01-architecture.md`` §5.1）：

  ① ``@neko_plugin`` 类声明与 ``__init__`` 里存引用；
  ② 两个 lifecycle 钩子（构造 / 销毁，**永不抛错**——钩子抛错会被宿主判 ``startup-failed``）；
  ③ 每个 entry 一行转调 ``CompanionService``；
  ④ ``Outcome → Ok/Err`` 的固定映射（:meth:`_reply`，全文件唯一一处）；
  ⑤ 配置读取。

**禁止**：故障归因 ``if``、用户文案字符串、``ctypes``、``EngineError`` 之外的业务 ``except``。
判据很硬：这里出现的每一个中文字符串都只应该是"服务层没起来"这类**接线级**兜底；
任何"因为 A 所以 B"的判断都属于 service，不属于本文件。

``data`` 里的 ``engine_root`` 是**真实**的 ``data_path("maa")`` 绝对路径：
手动放置引擎时用户要照着它去放（D6 的唯一口径），不许写"去找你本机的 MAA"。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    unwrap_or,
)

from .companion import CompanionService
from .companion.logic import CompanionLogic
from .companion.service import resolve_connect_params
from .maa.errors import EngineError
from .maa.provision import KNOWN_RELEASES, platform_key
from .maa.runtime import MaaRuntime

logger = logging.getLogger(__name__)

# --- store 键（缓存口径与旧版一致，便于老用户升级后仍有记忆） ---
STORE_OPERATORS: Final = "operators_cache"
STORE_DEPOT: Final = "depot_cache"
STORE_LAST_STAGE: Final = "last_stage"

# --- 入口超时预算（秒） ---
# 取值口径见 docs/01-architecture.md §5.2：宿主 [plugin_runtime].timeout 上限 300；
# 280 是"贴着 300 留余量"，非引用值。
TIMEOUT_CAPTURE: Final = 30
TIMEOUT_RECOGNIZE: Final = 60
TIMEOUT_RECONNECT: Final = 45
TIMEOUT_REPAIR: Final = 280

# 允许从配置里读的键（白名单：配置是外部输入，不整包透传）。
# 后两个键（连接配置名 / 连接 extras）由 `CompanionService.handle_reconnect()` 与
# `_connect_quietly()` 共同使用，解析规则只有一份：`companion.service.resolve_connect_params`。
_CONFIG_KEYS: Final = (
    "adb_address",
    "adb_path",
    "adb_connect_config",
    "adb_connect_extras",
    "verbose_diagnostics",
    "max_inline_image_mb",
)


class _StoreBridge:
    """把 ``self.store``（返回 ``Result``）桥接成普通 async KV。

    为什么需要这层：``store.get`` 返回的是 ``Result`` 包装，业务层不该知道 SDK 的结果类型。
    """

    __slots__ = ("_store",)

    def __init__(self, plugin_store: Any) -> None:
        self._store = plugin_store

    async def get(self, key: str, default: Any = None) -> Any:
        if self._store is None:
            return default
        return unwrap_or(await self._store.get(key), default)

    async def set(self, key: str, value: Any) -> None:
        if self._store is None:
            return
        unwrap_or(await self._store.set(key, value), None)


@neko_plugin
class ArknightsNekoCompanion(NekoPluginBase):
    """明日方舟陪玩插件（plugin_id = ``arknights_neko_plugin``）。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._runtime: MaaRuntime | None = None
        self._service: CompanionService | None = None
        self._config: dict[str, Any] = {}
        self._store = _StoreBridge(getattr(self, "store", None))
        self._last_progress: int | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @lifecycle(id="startup")
    async def on_startup(self, **_: Any) -> Any:
        """读配置 → 造 runtime/service → ``bootstrap()``（起后台任务后**立即返回**）。

        **永不抛错**：钩子抛错会被宿主判 ``startup-failed``；所有失败都收敛成
        ``state`` + ``last_error``，由 ``arknights_status`` 呈现。
        """
        try:
            await self._boot()
        except Exception:  # noqa: BLE001 - 钩子绝不能把异常交给宿主
            logger.exception("startup failed; plugin stays degraded")
        return Ok(self._startup_data())

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_: Any) -> Any:
        """先取消下载（**必须 await**），再销毁实例。顺序不可换。

        只 ``cancel()`` 不 ``await`` 的话，``CancelledError`` 会在 ``finally`` 的清理
        还没跑完时被下一次 ``bootstrap`` 覆盖，半包残留 → 下次校验直接 mismatch。
        """
        runtime = self._runtime
        if runtime is not None:
            try:
                await runtime.cancel_download()
            except Exception:  # noqa: BLE001 - shutdown 钩子必须能返回
                logger.warning("cancel_download failed during shutdown", exc_info=True)
            try:
                await runtime.destroy()      # 契约：destroy() 永不抛错
            except Exception:  # noqa: BLE001
                logger.warning("destroy failed during shutdown", exc_info=True)
        self._service = None
        self._runtime = None
        return Ok({"state": "stopped"})

    # ------------------------------------------------------------------
    # 入口 1：截图
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_capture",
        timeout=TIMEOUT_CAPTURE,
        name="截图",
        description="截取当前游戏画面并推到聊天窗（小图内联，大图先上传；也可另存到缓存目录）。",
    )
    @llm_tool(
        name="arknights_capture",
        description="截取明日方舟当前游戏画面看看。用户说「现在什么样 / 帮我看一眼 / 截个图」时使用。"
        "这一步只读，不会替用户点任何按钮。",
        timeout=TIMEOUT_CAPTURE,
        parameters={
            "type": "object",
            "properties": {
                "save": {
                    "type": "boolean",
                    "description": "是否另外存一份到缓存目录并返回路径，默认 false。",
                }
            },
        },
    )
    async def arknights_capture(self, save: bool = False, **_: Any) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_capture(save=bool(save)))

    # ------------------------------------------------------------------
    # 入口 2：引擎状态
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_status",
        name="引擎状态",
        description="查看引擎七态、下载进度、设备连接与最近一次故障码。**恒成功**（查状态本身不会失败）。",
    )
    async def arknights_status(self, **_: Any) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_status())

    # ------------------------------------------------------------------
    # 入口 3：关卡分析
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_analyze_stage",
        timeout=TIMEOUT_RECOGNIZE,
        name="关卡分析",
        description="给一个关卡代号就能分析；不给则尝试从当前画面识别（本轮识别链路未接，会如实说明）。",
    )
    @llm_tool(
        name="arknights_analyze_stage",
        description="分析某一关怎么打、卡在哪。用户提到具体关卡（如 1-7）或说自己卡关时使用。"
        "只读，不会代打。",
        timeout=TIMEOUT_RECOGNIZE,
        parameters={
            "type": "object",
            "properties": {
                "stage": {"type": "string", "description": "关卡代号，如 1-7 / CE-5；留空则尝试看画面。"},
                "concern": {"type": "string", "description": "用户的具体顾虑，如「漏怪」「撑不住」。"},
            },
        },
    )
    async def arknights_analyze_stage(self, stage: str = "", concern: str = "", **_: Any) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_analyze_stage(stage=stage, concern=concern))

    # ------------------------------------------------------------------
    # 入口 4：肉鸽建议
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_roguelike_advice",
        timeout=TIMEOUT_CAPTURE,
        name="肉鸽建议",
        description="集成战略（肉鸽）的启发式建议，最多三条。**纯静态知识，不联网、不读设备**。",
    )
    @llm_tool(
        name="arknights_roguelike_advice",
        description="给集成战略/肉鸽局的构筑建议。用户说「肉鸽怎么走 / 这个主题选谁」时使用。"
        "只给思路，不碰设备。",
        timeout=TIMEOUT_CAPTURE,
        parameters={
            "type": "object",
            "properties": {
                "theme": {"type": "string", "description": "主题名，不确定就留空。"},
                "current_operators": {"type": "array", "items": {"type": "string"},
                                      "description": "当前手上的干员（名字）。"},
                "hope": {"type": "integer", "description": "当前希望值；不知道就别填。"},
                "layer": {"type": "integer", "description": "当前层数；不知道就别填。"},
            },
        },
    )
    async def arknights_roguelike_advice(
        self,
        theme: str = "",
        current_operators: list[str] | None = None,
        hope: int | None = None,
        layer: int | None = None,
        **_: Any,
    ) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_roguelike_advice(
            theme=theme,
            current_operators=tuple(current_operators or ()),
            hope=hope,
            layer=layer,
        ))

    # ------------------------------------------------------------------
    # 入口 5：陪玩记忆
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_memory",
        name="陪玩记忆",
        description="汇总已缓存的干员 / 仓库 / 最近关卡。读失败一律回落 null，不报错。",
    )
    async def arknights_memory(self, **_: Any) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_memory())

    # ------------------------------------------------------------------
    # 入口 6：重连
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_reconnect",
        timeout=TIMEOUT_RECONNECT,
        name="重新连接",
        description="按配置里的 adb_address 重连设备。地址为空会明确说「没去连」，绝不回落 5555。",
    )
    async def arknights_reconnect(self, **_: Any) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_reconnect())

    # ------------------------------------------------------------------
    # 入口 7：引擎修复
    # ------------------------------------------------------------------
    @plugin_entry(
        id="arknights_engine_repair",
        timeout=TIMEOUT_REPAIR,
        name="修复引擎",
        description="重试 / 强制重下 / 卡住时恢复引擎。**不接受 URL、不接受路径**（安全边界）。"
        "只 kick 一次并回报快照，进度由后台任务上报。",
    )
    async def arknights_engine_repair(self, clean: bool = False, **_: Any) -> Any:
        service = self._service
        if service is None:
            return self._no_service()
        return self._reply(await service.handle_engine_repair(clean=bool(clean)))

    # ------------------------------------------------------------------
    # 接线辅助（**不含业务判断**：只做 Ok/Err 映射与兜底）
    # ------------------------------------------------------------------
    def _reply(self, outcome: Any) -> Any:
        """``Outcome → Ok/Err`` 的**唯一**映射点。

        ``ok`` 为假时把 service 已经生成好的 ``user_text``（已脱敏、已是猫娘语气）
        直接交给 ``SdkError``；本文件**不加工**这段文字。
        """
        if outcome.ok:
            return Ok(outcome.to_entry_dict())
        text = getattr(outcome, "user_text", "") or "这一步没成喵，再试一次看看。"
        return Err(SdkError(text))

    def _no_service(self) -> Any:
        """服务层没起来的兜底（startup 极早期失败时）。**只有这一句文案**。"""
        return Err(SdkError("引擎还没准备好喵，先调一次 arknights_status 看看卡在哪。"))

    async def _boot(self) -> None:
        """构造 runtime / service 并起引导。异常在调用方（on_startup）里被收敛。"""
        self._config = await self._read_config()

        data_dir = Path(self.data_path())
        engine_root = Path(self.data_path("maa"))     # 唯一权威位置（不探测别处）
        cache_dir = Path(self.cache_path("maa"))

        runtime = MaaRuntime(
            engine_root=engine_root,
            user_dir=engine_root,                     # MAA 的日志/调试图落在引擎目录里
            data_dir=data_dir,
            cache_dir=cache_dir,
            release=KNOWN_RELEASES.get(platform_key()),   # 不支持的平台为 None → PlatformUnsupported
            progress_sink=self._on_progress,
        )
        self._runtime = runtime
        self._service = CompanionService(
            runtime=runtime,
            logic=CompanionLogic(),
            push=self.push_message,                   # 同步可调用对象，service 不 await 它
            cache_dir=cache_dir,
            data_dir=data_dir,
            engine_root=engine_root,
            upload_image=self._upload_image,
            store_get=self._store.get,
            config_get=lambda: self._config,
            verbose_diagnostics=bool(self._config.get("verbose_diagnostics", False)),
            max_inline_image_bytes=_inline_limit_bytes(self._config),
        )

        try:
            await runtime.bootstrap()                 # 起后台任务后立即返回，绝不等下载
        except EngineError as exc:
            logger.warning("bootstrap failed: %s", exc)
            return
        if _state_value(runtime) == "loaded":
            await self._connect_quietly(runtime)

    async def _connect_quietly(self, runtime: MaaRuntime) -> None:
        """启动期连接：失败只记日志（``state`` 留在 ``loaded``，不是 ``failed``）。

        ``adb_connect_config`` / ``adb_connect_extras`` 与 ``arknights_reconnect`` 走
        **同一个解析函数**（``resolve_connect_params``）：启动时连上用的是哪条连接路径，
        之后重连不能换成另一条 —— 否则 MuMu12 用户会看到"启动好好的，点一次重连就变慢"。

        **不使用 ``force``**：启动期要的就是幂等。宿主热重载时 ``bootstrap()`` 可能被
        重复调用，此时"已经在 READY 且参数没变"应当什么都不做 —— 强制重连等于白断一次链。
        ``force`` 只属于用户显式点重连那条路（``CompanionService.handle_reconnect``）。
        """
        config = self._config
        connect_config, extras = resolve_connect_params(config)
        try:
            await runtime.connect(
                address=str(config.get("adb_address", "") or ""),
                adb_path=str(config.get("adb_path", "") or ""),
                config=connect_config,
                extras=extras,
            )
        except EngineError as exc:
            logger.info("startup connect not established: %s", exc)

    async def _read_config(self) -> dict[str, Any]:
        """读配置。业务段可能是 ``[arknights]``，也可能平铺；两种都认，只取白名单键。"""
        try:
            dumped = await self.config.dump()
        except Exception:  # noqa: BLE001 - 配置 IPC 抖动不该连坐整个插件启动
            logger.warning("config.dump() failed; using defaults", exc_info=True)
            return {}
        if not isinstance(dumped, Mapping):
            return {}
        section = dumped.get("arknights")
        source = section if isinstance(section, Mapping) else dumped
        return {key: source[key] for key in _CONFIG_KEYS if source.get(key) is not None}

    def _on_progress(self, percent: int, message: str) -> None:
        """``progress_sink``：只记录 + 记日志。

        **不调 ``report_status``**：该 API 的签名与可用时机在本仓没有核实过，
        宁可少一次上报，也不猜着调（猜错的表现是 startup 期间一条莫名异常）。
        真正的进度呈现走 ``arknights_status`` → ``runtime.download_progress``。
        """
        self._last_progress = percent if isinstance(percent, int) else None
        logger.debug("engine progress %s%% %s", percent, message)

    async def _upload_image(self, data: bytes, mime: str) -> Mapping[str, Any]:
        """``ctx.images.upload`` 的薄包装。**只在 entry 路径上被调用**（钩子里不可用）。"""
        return await self.ctx.images.upload(data, mime=mime)

    def _startup_data(self) -> dict[str, Any]:
        """startup 的返回载荷：只回**结构化**字段，不回文案、不回路径细节。

        **本方法自己绝不能再抛**：``on_startup`` 的 ``try`` 包住的是 ``_boot()``，
        而本方法在 ``try`` **之外**执行 —— 如果这里因为 ``data_path()`` 不可用而抛，
        那个异常会越过钩子直接落到宿主，结果就是 ``startup-failed``。
        这正是"钩子永不抛错"最容易被绕过的一条缝。
        """
        runtime = self._runtime
        try:
            engine_root = str(Path(self.data_path("maa")))
        except Exception:  # noqa: BLE001 - 宿主 API 不可用时只丢这个字段
            logger.warning("data_path('maa') unavailable while reporting startup", exc_info=True)
            engine_root = ""
        return {
            "state": _state_value(runtime),
            "engine_root": engine_root,
            "service_ready": self._service is not None,
            "config_keys": sorted(self._config),
        }


def _state_value(runtime: MaaRuntime | None) -> str:
    """``EngineState`` 的字符串值。**只读 ``.value``**（不比较枚举成员，也不 import 语义）。"""
    if runtime is None:
        return "absent"
    value = getattr(runtime.state, "value", None)
    return value if isinstance(value, str) else "absent"


def _inline_limit_bytes(config: Mapping[str, Any]) -> int:
    """``max_inline_image_mb`` → 字节。缺省/非法一律回落 2 MB（**不放大也不缩小**）。"""
    default = 2 * 1024 * 1024
    raw = config.get("max_inline_image_mb", 2)
    try:
        megabytes = float(raw)
    except (TypeError, ValueError):
        logger.warning("max_inline_image_mb=%r 不是数字，回落 2 MB", raw)
        return default
    if megabytes <= 0:
        logger.warning("max_inline_image_mb=%r 非正，回落 2 MB", raw)
        return default
    return int(megabytes * 1024 * 1024)
