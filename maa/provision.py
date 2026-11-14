"""引擎包获取的安全关键路径：**下载 → 校验 → 解压 → 定位核心库**。

本模块是"不可信字节进入插件目录"的唯一闸门，安全约束写死在实现里而不是文档里：

1. **哈希只来自内置常量表**（``KNOWN_RELEASES``）。本模块**不允许**任何运行时
   拉取校验值的代码路径——运行时去同一个通道拉 ``SHA256SUMS``，等于让攻击者
   同时提供"内容"和"判断内容是否可信的依据"（论证见
   ``docs/03-engine-provisioning.md`` §3.2）。因此 URL 与 sha256 同为常量：
   tag 固定，不用 ``latest``，不访问 ``api.github.com``。
2. **先落 ``.part``、再校验、最后 ``os.replace``**：任何时刻 ``dest`` 要么不存在，
   要么是哈希正确的完整文件；不存在"存在但只写了一半"的中间态（§3.4）。
3. **校验失败必删**：``.part`` 与可疑的旧 ``dest`` 都删（§3.5）。留着坏文件等于
   给下次启动埋"存在即复用"的坑，也等于把投毒样本缓存在用户机器上。
4. **解压是白名单 + 路径校验两件事，缺一不可**（§3.6）。只做白名单会被
   ``resource/../../evil.dll`` 绕过；只做路径校验会把 ``externals/`` 几百 MB
   的 .NET 运行时全解出来。顺序固定：**路径校验 → 硬排除 → 放行**，不可换序
   （换序会让 ``externals/../../evil.dll`` 被"排除"静默跳过，攻击样本在测试里
   永远看不见）。
5. **核心库唯一命中**：候选 ``root/`` / ``root/bin/`` / ``root/lib/``，0 个或 >1 个
   都抛 :class:`EngineLayoutInvalid`，**绝不挑第一个**。猜错的代价是用户只看到
   "加载失败"，永远想不到是解压出了两份。

依赖边界
--------
* 只 import 标准库 + :mod:`maa.errors`；**不 import 任何 Neko SDK**，任何第三方。
* **不定义任何异常类**（主理人裁决）：``raise`` 出的类型恒为
  ``{EngineDownloadFailed, EngineHashMismatch, EngineUnpackFailed,
  EngineLayoutInvalid}``。``tests/test_contract.py::R17`` 用 AST 扫本文件的
  ``raise`` 节点做不变量检查，所以**不要**用工厂函数（``raise _make_x()``）把
  类型藏起来，必须 ``raise EngineXxx(...)`` 直写。
* **不产出用户可见文案**：只给 ``kind`` 枚举 + 结构化字段，文案在
  ``companion/service.py`` 按 ``(code, kind)`` 查表生成（ADR-003）。

平台边界（v1）
--------------
只做 ``windows-x64`` 自动安装。其它平台在 ``KNOWN_RELEASES`` 里**没有条目**，
于是自然落到 ``PlatformUnsupported`` + 手动放置。**本文件不出现任何
``if platform == "darwin"`` 这类行为分支**：平台 → 键是一张映射表（数据），
不是代码路径。
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import hmac
import logging
import os
import shutil
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .errors import (
    DownloadFailure,
    EngineDownloadFailed,
    EngineHashMismatch,
    EngineLayoutInvalid,
    EngineUnpackFailed,
    UnpackFailure,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_HOSTS",
    "EXCLUDED_GLOBS",
    "EXCLUDED_PREFIXES",
    "ReleaseSpec",
    "ExtractResult",
    "KNOWN_RELEASES",
    "MAX_COMPRESSION_RATIO",
    "MAX_ENTRIES",
    "MAX_SINGLE_FILE",
    "MAX_TOTAL_UNCOMPRESSED",
    "DEFAULT_REL_SUBTREES",
    "atomic_download",
    "platform_key",
    "safe_extract",
    "verify_sha256",
]

# ---------------------------------------------------------------------------
# 常量：下载
# ---------------------------------------------------------------------------
CHUNK: Final = 1 << 20  # 1 MiB：既能让哈希/进度平滑，又不至于把内存顶起来
DOWNLOAD_TIMEOUT: Final = 30.0
PROGRESS_INTERVAL: Final = 0.2  # 进度节流（秒）：295 MB 不节流会把 UI 打爆
STALE_LOCK_SECONDS: Final = 300.0
PROVISION_PEAK_BYTES: Final = 1536 * 1024 * 1024
"""引导期的**峰值**磁盘预算（≈1.5 GiB）。

口径见 §3 C9：峰值 = zip（295 MB）+ 解压树 + 老版本共存 + 临时解压层，四者叠加，
所以它**大于**"本次还要写多少字节"。``NO_SPACE`` 的 ``detail`` 报这个数，是因为
用户清盘的目标是峰值，不是剩余字节；它有 ``≈`` 前缀，明确是**预算**不是测量值。
"""
USER_AGENT: Final = "arknights-neko-plugin/1.0 (+https://github.com/MaaAssistantArknights)"

ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({
    "github.com",
    # GitHub Release 资产的两种实际落点（302 之后）。只放这三个是刻意的：
    # 镜像要进来必须改这张表，改表就必须过 review —— 这就是"配置只允许枚举镜像"的落点。
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
"""允许直连的主机（https）。表外的 host 一律 ``REJECTED_SCHEME``，不降级、不重试。"""

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
"""**明文 http 只允许回环地址**。

唯一用途是离线测试与"本机镜像"：``tests`` 用 ``http.server`` 起假包走完整链路，
不碰真实 GitHub。回环不存在"传输被中间人改写"这一威胁；即使存在，完整性仍由
``sha256`` 常量兜住。**不要**把这条扩成"允许任意 http"——那会让
``REJECTED_SCHEME`` 这一档形同虚设。
"""

# ---------------------------------------------------------------------------
# 常量：解压上限（§3.6）。四重上限 + 写出期真实字节二次计数
# ---------------------------------------------------------------------------
MAX_ENTRIES: Final = 200_000
MAX_TOTAL_UNCOMPRESSED: Final = 1_200 * 1024 * 1024
MAX_SINGLE_FILE: Final = 600 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 60
MAX_SIZE_DRIFT: Final = 0.01  # 声明总和与实际写出总和的允许偏差（1%）
MAX_PATH_PARTS: Final = 32
MAX_PART_LENGTH: Final = 255
MAX_PATH_LENGTH: Final = 1024

EXCLUDED_PREFIXES: Final[tuple[str, ...]] = ("externals",)
"""硬排除的顶层目录。

``externals/`` 是 .NET/WPF 运行时，**只有 GUI（``MAA.exe``）需要**，``MaaCore``
作为库被 ctypes 加载时不走它。排除它省掉几百 MB 落盘，但请注意：**排除只缩
小落盘面，不承担安全职责**（安全职责在路径校验）。
"""

EXCLUDED_GLOBS: Final[tuple[str, ...]] = ("MAA.exe", "*.pdb")
"""硬排除的文件名（按 basename 匹配，大小写不敏感）。GUI 可执行与调试符号。"""

_EXCLUDED_PREFIXES_LOWER: Final[frozenset[str]] = frozenset(
    p.lower() for p in EXCLUDED_PREFIXES
)
_EXCLUDED_GLOBS_LOWER: Final[tuple[str, ...]] = tuple(g.lower() for g in EXCLUDED_GLOBS)

INCLUDED_PREFIXES: Final[tuple[str, ...]] = ("resource",)
INCLUDED_FALLBACK_PREFIXES: Final[tuple[str, ...]] = ("bin", "lib")
"""核心库位置未定（U1）时的兜底：``bin/`` / ``lib/`` 整棵子树放行。"""

CORE_BASENAMES: Final[tuple[str, ...]] = (
    "MaaCore.dll",
    "libMaaCore.so",
    "libMaaCore.dylib",
)
"""核心库可能的文件名。**这是数据不是分支**：v1 只有 Windows，但表里留全
三种名字不会让 Windows 上多出任何行为。"""

_WIN_RESERVED: Final[frozenset[str]] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})
_BAD_NAME_CHARS: Final[frozenset[str]] = frozenset('<>:"|?*')


# ---------------------------------------------------------------------------
# 发布清单：**唯一**的 URL 与哈希来源
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReleaseSpec:
    """一个可自动安装的引擎版本（四个安全关键字段都是 pin 住的）。

    Attributes:
        version: 上游 tag（``"v6.22.0"``）。**不是**范围，不做语义比较。
        platform: 平台键（与 :data:`KNOWN_RELEASES` 的键一致）。
        url: **版本化**的资产直链。禁止 ``/releases/latest``（那是可变量）。
        sha256: 小写 hex，64 位。已复核值 → 见 :data:`KNOWN_RELEASES` 注释。
        size: 字节数。与 ``sha256`` 一起构成"内容指纹"，缺一个都能被半包/拼接骗过。
        core_hint: 期望的核心库文件名，**仅用于诊断文案与 0 命中时的 missing**；
            真正的定位靠磁盘事实（:func:`_locate_core`），不靠这个名字猜路径。
        rel_subtrees: 允许出现核心库的相对子树（``""`` = 归档根）。
        archive_name: 资产文件名，仅用于日志与错误的 ``archive`` 字段。
    """

    version: str
    platform: str
    url: str
    sha256: str
    size: int
    core_hint: str
    rel_subtrees: tuple[str, ...]
    archive_name: str = ""
    library_relpath: str = ""
    """核心库相对归档根的**期望**路径。

    **空串 = 未核验（U1），不是"在根目录"**。定位一律走 :func:`_locate_core` 的
    磁盘事实（扫 ``rel_subtrees``）；这个字段只用于诊断与断言，**不要拿它拼路径**。
    U1 揭晓后填真值，也只作为"定位结果的交叉校验"。
    """
    resource_parent_relpath: str = ""
    """``resource/`` 的父目录相对路径（``""`` = 归档根）。

    ``AsstLoadResource`` 接收的是**包含** ``resource`` 的那一层，所以这个字段
    是交给 wrapper 的提示；同样是"未核验时留空"，由 :class:`ExtractResult` 的
    实际落点说话。
    """

    @property
    def size_bytes(self) -> int:
        """``size`` 的只读别名。

        ``runtime`` 早期按 ``size_bytes`` 命名写过调用点。保留一个别名比让两处
        字段名长期并存便宜，但**真相只有一个**：``size`` 是字段，``size_bytes``
        是视图。**不要**给别名加 setter，也不要再引入第三个名字。
        """
        return self.size


KNOWN_RELEASES: Final[dict[str, ReleaseSpec]] = {
    "windows-x64": ReleaseSpec(
        version="v6.22.0",
        platform="windows-x64",
        url=(
            "https://github.com/MaaAssistantArknights/MaaAssistantArknights"
            "/releases/download/v6.22.0/MAA-v6.22.0-win-x64.zip"
        ),
        # 已复核值 · 来源 release API `assets[].digest`（sha256:197e3b4c…）·
        # 复核日期 2026-11-13 · 与 HTTP `Content-Length` 交叉一致（294969951）。
        # 措辞纪律：**不得**在任何用户文案里称它为"官方公布的校验单"——
        # 它是我们自己复核并 pin 住的值，不是上游提供的信任根。
        sha256="197e3b4c35f56ca75e3fc7cf0a6dfd5467f8aace613bd44626448cf6f8b4756c",
        size=294_969_951,
        core_hint="MaaCore.dll",
        rel_subtrees=("", "bin", "lib"),
        archive_name="MAA-v6.22.0-win-x64.zip",
    ),
}
"""**唯一的**发布物清单。增删条目等于改信任根，必须走 review + 人工复核哈希。

为什么只有 ``windows-x64``：v1 只做 Windows x64 自动安装。Linux 无官方 zip 资产、
macOS 的 runtime 包内容未核验（U3）。**不提供跑不通的路径**——没有条目，
运行时自然拿到 ``PlatformUnsupported``，然后引导用户手动放置。
"""

# 平台 → 键的映射。**数据表，不是行为分支**：加平台 = 加一行，不改代码。
_PLATFORM_KEYS: Final[dict[tuple[str, str], str]] = {
    ("win32", "amd64"): "windows-x64",
    ("win32", "x86_64"): "windows-x64",
    # macOS/Linux 在 v1 里**故意没有映射**（不是"忘了写"）：没有映射就没有键，
    # 没有键就落 PlatformUnsupported。禁止在这里加 `if system == "darwin"` 分支。
}


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """把 ``(sys.platform, platform.machine())`` 折成清单键；未知平台返回空串。

    返回空串而不是抛异常：调用方（runtime）要拿"空键"去查表，然后抛
    ``PlatformUnsupported(system=..., supported=KNOWN_RELEASES.keys())`` ——
    这样"支持哪些平台"永远只有一份真相（表），不会在异常与代码里各写一份。
    """
    sysname = (system if system is not None else sys.platform).lower()
    mach = (machine if machine is not None else _machine()).lower()
    return _PLATFORM_KEYS.get((sysname, mach), "")


def _machine() -> str:
    # 延迟 import：`platform` 只在需要时用，减少导入期开销与副作用。
    import platform as _platform

    return _platform.machine()


# ---------------------------------------------------------------------------
# 解压结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ExtractResult:
    """解压产物。``core_lib`` 是**已 resolve** 的绝对路径，直接交给 wrapper。

    ``core_lib`` 的类型标了 ``Path | None`` 是为了与架构文档的签名一致；实际
    **成功路径恒非 None** —— 0 命中/多命中都会抛 :class:`EngineLayoutInvalid`。
    """

    dest: Path
    """解压根目录（``ExtractResult.core_lib`` / ``resource_dir`` 都以它为基准）。"""
    core_lib: Path | None
    resource_dir: Path
    selected: int
    """真正落盘的文件数（不含目录条目）。"""
    skipped: int
    """合法但不在白名单内的条目数（**只记"正常跳过"，绝不混入安全拒绝**）。"""
    excluded: int
    """命中硬排除的条目数。"""
    skipped_sample: tuple[str, ...] = ()
    excluded_sample: tuple[str, ...] = ()
    written_bytes: int = 0
    """实际写出的字节数（用于与声明值比对，也是"落盘量"的唯一真相）。"""


# ---------------------------------------------------------------------------
# 1) 校验
# ---------------------------------------------------------------------------
def verify_sha256(
    path: str | os.PathLike[str],
    expected: str,
    *,
    version: str = "",
) -> str:
    """流式计算 ``sha256`` 并与 ``expected`` 比对，**返回实际值**（小写 hex）。

    返回实际值是刻意的：调用方要么用它记审计日志，要么拿它构造
    :class:`EngineHashMismatch`。返回 ``bool`` 会逼每个调用点各写一遍
    "不符时怎么办"，那是四份不一致的处置。

    Args:
        path: 待校验文件。
        expected: 期望值。允许带 ``"sha256:"`` 前缀（GitHub API 的 ``digest``
            就是这种形态），大小写不敏感。
        version: 仅用于异常上下文，便于把"哪个版本的包坏了"写进日志。

    Returns:
        实际 sha256（小写 hex）。

    Raises:
        EngineHashMismatch: 期望值非法 / 文件不可读 / 摘要不符。**三种都归这一档**：
            对调用方而言"这个文件没能被确认是我们要的那份"是同一件事；区别写进
            ``actual``（非法期望与不可读时为空串，日志里能看出来）。
    """
    want = _normalize_digest(expected)
    archive = Path(path)

    if not want:
        # 期望值本身不合法 = 常量表写坏了。这类错误必须在开发期炸，
        # 但**不能**用 ValueError 逃出去（会穿透成 500），所以仍归 HashMismatch。
        logger.error("verify_sha256: 非法期望值（不是 64 位 hex）: %r", expected)
        raise EngineHashMismatch(
            archive=archive, expected=str(expected), actual="", version=version
        )

    digest = hashlib.sha256()
    try:
        with open(archive, "rb") as fp:
            for chunk in iter(lambda: fp.read(CHUNK), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("verify_sha256: 无法读取 %s: %s", archive, exc)
        raise EngineHashMismatch(
            archive=archive, expected=want, actual="", version=version
        ) from exc

    actual = digest.hexdigest()
    if not hmac.compare_digest(actual, want):
        # 常数时间比较：此处无时序侧信道意义，但代价为零，且避免将来被复用进
        # token 比较时出错。
        raise EngineHashMismatch(
            archive=archive, expected=want, actual=actual, version=version
        )
    return actual


def _normalize_digest(value: str) -> str:
    """``"sha256:ABC…"`` → ``"abc…"``；不合法返回空串（由调用方决定怎么报）。"""
    text = (value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :].strip()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def _is_intact(path: Path, release: ReleaseSpec) -> bool:
    """``path`` 是否就是清单里那一份（存在 + 哈希一致）。不抛异常。"""
    if not path.is_file():
        return False
    try:
        verify_sha256(path, release.sha256, version=release.version)
    except EngineHashMismatch:
        return False
    return True


# ---------------------------------------------------------------------------
# 2) 下载
# ---------------------------------------------------------------------------
class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每次重定向都重新校验目标 URL。

    默认 ``HTTPRedirectHandler`` 会一路跟下去：从 ``github.com`` 跳到
    ``http://evil.example.com`` 也算"合法重定向"。这里在**发起新请求之前**
    复用 :func:`_validate_url`，于是"降级到 http"和"跳到表外 host"都在网络层
    被拒，而不是等字节下完再靠哈希兜。
    """

    def __init__(self, target: Path) -> None:
        self._target = target

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        _validate_url(newurl, self._target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_url(url: str, target: Path) -> None:
    """只放行 ``https + ALLOWED_HOSTS``，以及 ``http + 回环``（见 ``LOOPBACK_HOSTS``）。"""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        parts = None
        port = None

    def _reject(reason: str) -> None:
        raise EngineDownloadFailed(
            url=url,
            target=target,
            kind=DownloadFailure.REJECTED_SCHEME,
            detail=reason,
        )

    if parts is None:
        _reject("URL 无法解析")
        return  # for type checkers
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not host:
        _reject("URL 缺少主机名")
    if parts.username or parts.password:
        _reject("URL 含 userinfo（形如 user@host，用于伪装真实主机）")
    if port is not None and not (1 <= port <= 65535):
        _reject(f"端口越界: {port}")
    if scheme == "https":
        if host not in ALLOWED_HOSTS:
            _reject(f"主机不在白名单: {host}")
        return
    if scheme == "http":
        if host not in LOOPBACK_HOSTS:
            _reject("拒绝明文 http（只允许回环地址用于离线测试/本机镜像）")
        return
    _reject(f"拒绝的协议: {scheme or '(空)'}")


def _build_opener(use_env_proxy: bool, target: Path) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_StrictRedirectHandler(target)]
    if not use_env_proxy:
        # 默认忽略环境代理：T11。需要代理的用户在插件配置里显式开启
        # （此时哈希仍保护完整性，代理只能看到流量元数据）。
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


@contextmanager
def _exclusive_lock(
    lock_path: Path,
    *,
    url: str,
    target: Path,
    stale_after: float = STALE_LOCK_SECONDS,
) -> Iterator[None]:
    """``O_CREAT|O_EXCL`` 独占锁 + 陈旧锁回收。

    回收是**故意**的：进程被 kill 后必须能自愈，否则用户会永久卡在"另一个实例
    正在下载"上，而这个"另一个实例"早就不存在了。

    ``kind`` 的选择要说清楚（这是我唯一觉得枚举不够用的一处）：8 档里没有
    "本地并发冲突"这一档。落 ``WRITE_ERROR`` 是因为它至少不是"内容不可信"、
    也不是"网络问题"，而 service 侧对 ``(download, write_error)`` 的文案应当写成
    **"本地写入/占用问题，稍后重试"** —— 这样"磁盘满之外的写失败"和"另一实例正在
    下载"两种原因都能读通。若以后新增 ``BUSY`` 档，这里应改判；**不要**为了
    "看起来更准"改落 ``NETWORK``（会把用户推去查网络）。
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            age = -1.0
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_after and attempts <= 2:
                logger.warning("回收陈旧下载锁（%.0fs 未更新）: %s", age, lock_path)
                lock_path.unlink(missing_ok=True)
                continue
            raise EngineDownloadFailed(
                url=url,
                target=target,
                kind=DownloadFailure.WRITE_ERROR,
                detail="下载锁被占用（另一实例正在下载同一目标）",
            ) from None
        else:
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            break
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def atomic_download(
    release: ReleaseSpec,
    dest: str | os.PathLike[str],
    expected_sha256: str | None = None,
    *,
    expected_size: int | None = None,
    on_progress: Callable[[int, int | None], None] | None = None,
    cancel: threading.Event | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
    use_env_proxy: bool = False,
) -> Path:
    """把 ``release`` 的归档下到 ``dest``，返回 ``dest``（**只在校验通过后存在**）。

    形状固定为 ``.part`` → 校验 → ``os.replace``：

    * 边下边算 sha256：不二次读盘（省 I/O，也避免"读完到算哈希之间被替换"的窗口）。
    * 大小双向卡：``> size`` 立刻早停（防磁盘炸弹），``< size`` 走 ``SIZE_MISMATCH``。
    * **清理只有一个出口**（末尾 ``finally``）：成功时 ``.part`` 已被 ``os.replace``
      吃掉 → ``unlink(missing_ok=True)`` 自动 no-op；取消 / 中断 / 异常走同一条路径。
      不要"``except`` 里删一次、``finally`` 里再删一次"——多处清理点必然有人漏改。
    * **取消不是失败**：抛 ``kind=CANCELLED``，service 层据此渲染"已暂停"
      （不标红、不计失败、状态落 ``ABSENT``）。取消**不动**已校验通过的旧 ``dest``。

    Args:
        release: 清单条目。URL / sha256 / size 全部来自它，**没有**单独传 URL 的参数——
            接口上不存在"地址"这个入参，就不存在"校验它"这件事。
        dest: 目标文件路径（通常 ``<engine_root>/<archive_name>``）。
        on_progress: ``(downloaded, total)``；``total`` 为 ``None`` 表示服务端没给长度。
        cancel: 协作式取消事件。检查点在每次 ``read()`` **之前**，所以最坏延迟
            = 一次 socket 读的 ``timeout``。
        opener: 仅用于测试注入（伪造 opener / 本地 ``http.server``）。
        timeout: socket 超时。
        use_env_proxy: 是否尊重 ``HTTP(S)_PROXY``。默认 ``False``（T11）。
        expected_sha256 / expected_size: **已废弃，只用于把旧写法变成可读的报错**。
            旧写法 ``atomic_download(url, dest, sha256, expected_size=…)`` 把"地址"和
            "期望值"当两个独立入参，等于允许调用方自己拼一组 (url, sha) —— 那正是
            我们要消灭的形态。现在只接受 ``ReleaseSpec``：URL 与 sha256 出自同一
            个常量表项，**结构上不可能不匹配**。

    Returns:
        校验通过的 ``dest``。

    Raises:
        EngineDownloadFailed: 网络 / 限流 / 资产消失 / 大小不符 / 磁盘满 / 写失败 / 取消 /
            入参形态不对（旧写法）。
        EngineHashMismatch: 内容与 pin 住的 sha256 不符（安全事件，``dest`` 与
            ``.part`` 都不留）。
    """
    target = Path(dest)

    if isinstance(release, str) or expected_sha256 is not None or expected_size is not None:
        raise EngineDownloadFailed(
            url=release.url if isinstance(release, ReleaseSpec) else str(release),
            target=target,
            kind=DownloadFailure.REJECTED_SCHEME,
            detail=(
                "atomic_download 的入参必须是 ReleaseSpec；"
                "旧写法 (url, dest, sha256, expected_size=…) 已废弃 —— "
                "URL 与哈希必须来自同一个常量表项，不允许分开传。"
            ),
        )

    url = release.url

    if not _path_usable(target):
        # 路径里有非法字符/保留名，交给 Python 抛 OSError 会变成 500，先自己拦住。
        raise EngineDownloadFailed(
            url=url,
            target=target,
            kind=DownloadFailure.WRITE_ERROR,
            detail=f"目标路径不可用: {target}",
        )

    _validate_url(url, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.part")
    lock_path = target.with_name(f"{target.name}.lock")

    # 已存在且哈希正确 → 直接复用，不发一个字节（C13：opener 被调用即失败）。
    if _is_intact(target, release):
        logger.info("复用已校验的归档: %s", target)
        if on_progress is not None:
            on_progress(release.size, release.size)
        return target

    with _exclusive_lock(lock_path, url=url, target=target):
        # 双检：等锁期间另一个实例可能已经下完了。
        if _is_intact(target, release):
            return target
        if target.exists() or target.is_symlink():
            # 存在但哈希不对：按"不可信"处理，先删（§3.5）。
            logger.warning("删除哈希不符的旧归档: %s", target)
            _unlink_quietly(target)
        # 上次崩溃残留的 .part：内容不可信，删。
        if part.exists():
            logger.warning("删除上次残留的 .part: %s", part)
            _unlink_quietly(part)

        received = 0
        digest = hashlib.sha256()
        # 节流状态是**调用内的局部变量**，不是模块级全局：两个并发下载共用一个
        # 全局时间戳会互相把对方的进度吞掉（看起来像"卡住了"）。
        last_report = 0.0
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    # 禁掉压缩传输：否则 Content-Length 与文件字节数对不上，
                    # 而我们校验的是**文件**的指纹。
                    "Accept-Encoding": "identity",
                },
            )
            open_fn = opener.open if opener is not None else _build_opener(
                use_env_proxy, target
            ).open
            resp = open_fn(req, timeout=timeout)
            try:
                expected_size = release.size
                declared = _content_length(getattr(resp, "headers", None))
                if declared is not None and declared != expected_size:
                    raise EngineDownloadFailed(
                        url=url,
                        target=target,
                        kind=DownloadFailure.SIZE_MISMATCH,
                        detail=f"Content-Length {declared} != 期望 {expected_size}",
                    )
                with open(part, "xb") as out:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise EngineDownloadFailed(
                                url=url,
                                target=target,
                                kind=DownloadFailure.CANCELLED,
                                detail="用户取消（不是失败）",
                            )
                        buf = resp.read(CHUNK)
                        if not buf:
                            break
                        received += len(buf)
                        if received > expected_size:
                            raise EngineDownloadFailed(
                                url=url,
                                target=target,
                                kind=DownloadFailure.SIZE_MISMATCH,
                                detail=(
                                    f"响应字节超出期望大小（{expected_size}），已中止"
                                ),
                            )
                        digest.update(buf)
                        out.write(buf)
                        if on_progress is not None:
                            now = time.monotonic()
                            if now - last_report >= PROGRESS_INTERVAL:
                                last_report = now
                                on_progress(received, expected_size)
                    out.flush()
                    os.fsync(out.fileno())  # 先落盘再改名：断电不产生"名字对内容空"
            finally:
                _close_quietly(resp)

            if received != release.size:
                raise EngineDownloadFailed(
                    url=url,
                    target=target,
                    kind=DownloadFailure.SIZE_MISMATCH,
                    detail=f"大小不符: 收到 {received}, 期望 {release.size}",
                )

            actual = digest.hexdigest()
            want = _normalize_digest(release.sha256)
            if not want or not hmac.compare_digest(actual, want):
                # 内容不可信 → 连旧 dest 一起删（不能留一个"看起来能用"的旧版本
                # 让用户以为升级成功了）。
                _unlink_quietly(target)
                raise EngineHashMismatch(
                    archive=target,
                    expected=want or release.sha256,
                    actual=actual,
                    version=release.version,
                )

            os.replace(part, target)  # ← 成功标志：此后 part 已不存在
            if on_progress is not None:
                on_progress(received, received)
            return target

        except urllib.error.HTTPError as exc:  # 注意：HTTPError 是 URLError 子类，先拦
            if exc.code in (404, 410):
                kind = DownloadFailure.ASSET_GONE
            elif exc.code in (403, 429):
                kind = DownloadFailure.THROTTLED
            else:
                kind = DownloadFailure.NETWORK
            raise EngineDownloadFailed(
                url=url,
                target=target,
                kind=kind,
                status=exc.code,
                detail=str(getattr(exc, "reason", "")),
            ) from exc
        except urllib.error.URLError as exc:  # DNS / 连接失败 / TLS
            raise EngineDownloadFailed(
                url=url,
                target=target,
                kind=DownloadFailure.NETWORK,
                detail=str(exc.reason),
            ) from exc
        except OSError as exc:
            # 裸 OSError 绝不许逃出本模块：runtime 只 except EngineError，
            # 裸异常会穿透成 500。ENOSPC 单独成档（"清磁盘" ≠ "改权限"）。
            kind = (
                DownloadFailure.NO_SPACE
                if exc.errno == errno.ENOSPC
                else DownloadFailure.WRITE_ERROR
            )
            raise EngineDownloadFailed(
                url=url,
                target=target,
                kind=kind,
                # 只有 NO_SPACE 才带容量数字：EACCES/EROFS 是权限问题，
                # 给它写"还剩多少 MB"会把人引去清磁盘（错误的自救方向）。
                detail=_io_failure_detail(
                    target.parent,
                    exc,
                    need_bytes=release.size,
                    with_capacity=kind is DownloadFailure.NO_SPACE,
                ),
            ) from exc
        finally:
            # 唯一清理点。成功时 part 已被 os.replace 吃掉 → no-op；
            # 其余所有路径（含 CancelledError / KeyboardInterrupt）都删干净。
            part.unlink(missing_ok=True)


def _content_length(headers: Any) -> int | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Content-Length")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _close_quietly(resp: Any) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - 关闭失败不该盖住真正的失败原因
            logger.debug("关闭响应失败", exc_info=True)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("删除失败（可能是被占用）: %s", path, exc_info=True)


def _to_mb(value: int | float) -> int:
    """字节 → MB（截断，不作四舍五入）。"""
    return int(value // (1024 * 1024))


def _io_failure_detail(
    base: Path,
    exc: OSError,
    *,
    need_bytes: int = 0,
    with_capacity: bool,
) -> str:
    """把 ``OSError`` 变成能指导用户动作的 ``detail``。

    规则（R09 / §4.3）：

    * **``NO_SPACE`` 必须带真实探测到的容量**：``shutil.disk_usage`` 读 ``base`` 所在卷，
      写成 ``free=…MB total=…MB need≈…MB``。用户看到"磁盘满"却不知道要清 200 MB
      还是 2 GB，最后往往被推去重下一个 295 MB 的包 —— 这条就是为此存在的。
    * **探测失败就不写数字**（保留原始 errno 文本）。**绝不填 0，也绝不估算**：
      一个错的数字比没有数字更糟，它会让用户按 0 MB 的缺口去清盘，然后再次失败。
    * **``WRITE_ERROR`` / ``EACCES`` / ``EROFS`` 不加容量数字**：那是权限/只读问题，
      给它看"还剩多少 MB"会把人引向错误的自救动作。这类只回原始错误。

    Args:
        base: 用来探测容量的路径（通常是**已存在**的父目录）。
        exc: 原始 ``OSError``。
        need_bytes: 本次大概需要写多少字节；``0`` 表示"预检没跑到"。
        with_capacity: 是否允许带容量数字（只有 ``NO_SPACE`` 为真）。
    """
    raw = str(exc) or type(exc).__name__
    if not with_capacity:
        return raw

    probe = Path(base)
    # 父目录理论上已存在；万一被并发删掉，向上找第一个存在的祖先再探测，
    # 而不是直接放弃（放弃会让这条 detail 退化成"没有数字"）。
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        logger.warning("disk_usage 探测失败，detail 不带容量数字: %s", probe, exc_info=True)
        return raw

    need = max(PROVISION_PEAK_BYTES, int(need_bytes or 0))
    return (
        f"free={_to_mb(usage.free)}MB total={_to_mb(usage.total)}MB "
        f"need≈{_to_mb(need)}MB (原错误: {raw})"
    )


def _path_usable(path: Path) -> bool:
    """粗校验目标路径可用（不替代 OS，只拦"一定会炸"的形态）。"""
    try:
        text = str(path)
    except Exception:  # noqa: BLE001
        return False
    return bool(text) and "\x00" not in text


# ---------------------------------------------------------------------------
# 3) 解压
# ---------------------------------------------------------------------------
DEFAULT_REL_SUBTREES: Final[tuple[str, ...]] = ("", "bin", "lib")
"""核心库候选子树。``""`` = 归档根（真实包就是这样：没有 ``MAA/`` 包裹层）。"""

# 这里**故意没有** `_DEFAULT_LAYOUT`（历史见 `safe_extract` 内的注释）：
# 曾经有一个"未收到 release 时的默认布局视图"，它只降级诊断字段、候选子树与真实
# 条目一致，看起来无害。但它让 `safe_extract(archive, dest)` 这种**两参调用**可以
# 正常跑完，于是 `runtime.unpack()` 把版本 / `core_hint` 比对**静默地等于没做**，
# 而且没有任何一条用例会因此变红。现在第三个参数没有默认值：漏传就是调用点
# `TypeError`，问题在开发期就暴露，而不是变成线上"解压成功但引擎加载不上"。


def safe_extract(
    archive: str | os.PathLike[str],
    dest: str | os.PathLike[str],
    release: ReleaseSpec,
) -> ExtractResult:
    """把 ``archive`` 安全解压到 ``dest``（``dest`` 必须不存在）。

    三道闸，顺序不可换：

    1. **路径校验**（对**所有**条目，含被排除的）：拒绝绝对路径、``..``、盘符、
       UNC、NTFS ADS、Windows 保留名、尾点/尾空格、反斜杠、符号链接条目、加密条目。
       这一步必须跑在"排除"之前，否则 ``externals/../../evil.dll`` 会被"排除
       externals"静默跳过——攻击样本在测试里永远看不见（A15）。
    2. **硬排除**：``externals/**``、``MAA.exe``、``*.pdb``。
    3. **白名单放行**：``resource/**``、归档根目录文件、``bin/**``、``lib/**``。
       其余计入 ``skipped``（合法、只是不需要）。**``skipped`` 只装"正常跳过"**，
       被拒绝的条目一律抛异常，绝不混进去——否则安全事件会伪装成"跳过了些无关文件"。

    Args:
        archive: 已通过 sha256 校验的 zip。
        dest: 目标目录，**必须不存在**（`exist_ok=False`）。绝不与已有目录合并：
            "预放 dest 为链接"是唯一能被这条挡住的攻击。
        release: 清单条目，提供 ``rel_subtrees`` / ``core_hint`` / 版本比对依据。
            **必填（没有默认值）**：漏传在调用点就是 ``TypeError``。曾经它可以省略，
            代价是"没有版本比对"这种退化**不会让任何用例变红**——只会让线上多一类
            "解压成功但加载不上"的谜案。

    Returns:
        :class:`ExtractResult`。``core_lib`` 是已 resolve 的绝对路径。

    Raises:
        EngineUnpackFailed: 包损坏 / 条目不安全 / 超上限 / 实际字节与声明不符 /
            磁盘满 / 写失败。
        EngineLayoutInvalid: 布局不符（缺 ``resource/``、核心库 0 命中或多命中）。
    """
    zp = Path(archive)
    destp = Path(dest)

    # 关于 `release`：它是**必填参数**，这里**没有** `if release is None: raise ...`
    # 这样的分支，理由有两条，都是被实测逼出来的：
    #
    # 1. 历史：本函数曾允许 `release=None` 并退化成 `_DEFAULT_LAYOUT`，于是
    #    `runtime.unpack()` 把 extractor 按两参调用时"谁也没发现 release 丢了"——
    #    校验那道比对（版本 / core_hint）静默地等于没做。失败的表现是"解压成功但
    #    引擎加载不上"，排查方向会被完全带偏。
    # 2. R17 把"运行期 raise 的类型"钉死为那四个 `Engine*`，豁免面只有两个导入期
    #    自检函数。而"调用方漏传清单条目"是**我们自己的契约违例**，不是用户可遭遇的
    #    运行期故障：给它挑任何一个 `UnpackFailure` 档位都是在撒谎（说"包损坏"或
    #    "写失败"会把排查方向指错），所以正确的做法不是"换一种异常"，而是**让这条
    #    路径不存在**——参数必填，漏传在调用点就是 `TypeError`，开发期立刻暴露。
    #
    # 也**不用** `assert`：`python -O` 会把整条剥掉，安全相关的不变量不接受
    # "被一个优化开关关掉"。必填参数是解释器层面的约束，`-O` 拿不掉。

    if destp.exists() or destp.is_symlink() or _is_junction(destp):
        raise EngineUnpackFailed(
            archive=zp,
            target_dir=destp,
            kind=UnpackFailure.WRITE_ERROR,
            detail="解压目标已存在（拒绝与已有目录合并）",
        )

    destp.parent.mkdir(parents=True, exist_ok=True)
    created_root = False
    # 供 `except` 分支算"还需要多少空间"：预检没跑完就 ENOSPC 时它是 0，
    # 于是 detail 里的 need 退回峰值口径（不是编一个数，见 `_io_failure_detail`）。
    declared_selected = 0
    try:
        destp.mkdir(parents=False, exist_ok=False)  # 原子占位：已存在则 FileExistsError
        created_root = True
        if os.name != "nt":
            # T8 的一半：解压目录只给当前用户（Windows 上无对应语义，由宿主 ACL 负责）。
            os.chmod(destp, 0o700)
        dest_real = os.path.realpath(destp)

        with zipfile.ZipFile(zp) as zf:
            infos = zf.infolist()
            zip_size = max(1, zp.stat().st_size)

            # ---- 静态预检：体积/条目上限 + 名称校验（早失败，不落盘） ----
            if len(infos) > MAX_ENTRIES:
                raise EngineUnpackFailed(
                    archive=zp,
                    target_dir=destp,
                    kind=UnpackFailure.LIMIT_EXCEEDED,
                    detail=f"条目数 {len(infos)} > {MAX_ENTRIES}",
                )

            declared_total = 0
            declared_selected = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise EngineUnpackFailed(
                        archive=zp,
                        target_dir=destp,
                        kind=UnpackFailure.UNSAFE_MEMBER,
                        member=info.filename,
                        detail="拒绝加密条目",
                    )
                if _is_unsafe_mode(info):
                    raise EngineUnpackFailed(
                        archive=zp,
                        target_dir=destp,
                        kind=UnpackFailure.UNSAFE_MEMBER,
                        member=info.filename,
                        detail="拒绝符号链接/特殊文件条目",
                    )
                parts = _check_member_name(info.filename, zp, destp)
                declared_total += info.file_size
                if info.is_dir():
                    continue
                if _is_excluded(parts):
                    continue
                if not _is_included(parts):
                    continue
                if info.file_size > MAX_SINGLE_FILE:
                    raise EngineUnpackFailed(
                        archive=zp,
                        target_dir=destp,
                        kind=UnpackFailure.LIMIT_EXCEEDED,
                        member=info.filename,
                        detail=f"单文件 {info.file_size} > {MAX_SINGLE_FILE}",
                    )
                declared_selected += info.file_size

            # 上限按**要落盘的字节**算，而不是全包声明量：`externals/` 被排除，
            # 把它算进来会让真实包（295 MB zip）在预检阶段被误判成 zip bomb。
            if declared_selected > MAX_TOTAL_UNCOMPRESSED:
                raise EngineUnpackFailed(
                    archive=zp,
                    target_dir=destp,
                    kind=UnpackFailure.LIMIT_EXCEEDED,
                    detail=f"待解压总量 {declared_selected} > {MAX_TOTAL_UNCOMPRESSED}",
                )
            if declared_selected / zip_size > MAX_COMPRESSION_RATIO:
                raise EngineUnpackFailed(
                    archive=zp,
                    target_dir=destp,
                    kind=UnpackFailure.LIMIT_EXCEEDED,
                    detail=(
                        f"压缩比 {declared_selected / zip_size:.1f}x > "
                        f"{MAX_COMPRESSION_RATIO}x（疑似 zip bomb）"
                    ),
                )

            # ---- 逐条目写出：真实字节二次计数 ----
            selected = skipped = excluded = 0
            skipped_sample: list[str] = []
            excluded_sample: list[str] = []
            written_total = 0

            for info in infos:
                parts = _check_member_name(info.filename, zp, destp)  # 二次校验：防 TOCTOU
                if _is_excluded(parts):
                    if not info.is_dir():
                        excluded += 1
                        if len(excluded_sample) < 8:
                            excluded_sample.append(info.filename)
                    continue
                if not _is_included(parts):
                    if not info.is_dir():
                        skipped += 1
                        if len(skipped_sample) < 8:
                            skipped_sample.append(info.filename)
                    continue

                if info.is_dir():
                    _ensure_dir_no_symlink(
                        destp, parts, dest_real, zp, destp
                    )
                    continue

                parent = _ensure_dir_no_symlink(
                    destp, parts[:-1], dest_real, zp, destp
                )
                target = parent / parts[-1]
                if target.is_symlink() or _is_junction(target):
                    raise EngineUnpackFailed(
                        archive=zp,
                        target_dir=destp,
                        kind=UnpackFailure.UNSAFE_MEMBER,
                        member=info.filename,
                        detail="落点已是链接（疑似 TOCTOU 换向）",
                    )

                written_here = 0
                try:
                    with zf.open(info) as src, open(target, "xb") as out:
                        while True:
                            buf = src.read(CHUNK)
                            if not buf:
                                break
                            written_here += len(buf)
                            written_total += len(buf)
                            if written_here > MAX_SINGLE_FILE:
                                raise EngineUnpackFailed(
                                    archive=zp,
                                    target_dir=destp,
                                    kind=UnpackFailure.LIMIT_EXCEEDED,
                                    member=info.filename,
                                    detail="写出字节超单文件上限",
                                )
                            if written_here > info.file_size * (
                                1 + MAX_SIZE_DRIFT
                            ) + CHUNK:
                                raise EngineUnpackFailed(
                                    archive=zp,
                                    target_dir=destp,
                                    kind=UnpackFailure.SIZE_DRIFT,
                                    member=info.filename,
                                    detail="写出字节超限（声明值与实际不符）",
                                )
                            out.write(buf)
                except (zipfile.BadZipFile, zlib.error) as exc:
                    raise EngineUnpackFailed(
                        archive=zp,
                        target_dir=destp,
                        kind=UnpackFailure.BAD_ARCHIVE,
                        member=info.filename,
                        detail=f"读取条目失败: {exc}",
                    ) from exc

                selected += 1
                # 仅在 POSIX 上还原可执行位；Windows 忽略。
                if os.name != "nt":
                    mode = (info.external_attr >> 16) & 0o777
                    if mode & 0o111:
                        os.chmod(target, 0o755)

            # ---- 布局断言：不能"什么都没解出来还报成功" ----
            if selected == 0:
                raise EngineLayoutInvalid(
                    root=destp,
                    missing=["<白名单未命中任何条目>"],
                    found=[],
                )
            resource_dir = destp / "resource"
            if _is_link(resource_dir) or not resource_dir.is_dir():
                raise EngineLayoutInvalid(
                    root=destp, missing=["resource/"], found=[]
                )
            if (destp / "externals").exists():
                # 硬排除被改坏的硬信号：宁可失败，也不要"悄悄解出了几百 MB"。
                raise EngineLayoutInvalid(
                    root=destp, missing=["<externals 不应被解出>"], found=[]
                )

            core_lib = _locate_core(destp, release)

            if (
                declared_selected
                and abs(written_total - declared_selected) > declared_selected * MAX_SIZE_DRIFT
            ):
                raise EngineUnpackFailed(
                    archive=zp,
                    target_dir=destp,
                    kind=UnpackFailure.SIZE_DRIFT,
                    detail=(
                        f"解压字节与声明不符: 声明 {declared_selected} "
                        f"实际 {written_total}"
                    ),
                )

        logger.info(
            "解压完成: selected=%d skipped=%d excluded=%d bytes=%d",
            selected,
            skipped,
            excluded,
            written_total,
        )
        return ExtractResult(
            dest=destp,
            core_lib=core_lib,
            resource_dir=resource_dir,
            selected=selected,
            skipped=skipped,
            excluded=excluded,
            skipped_sample=tuple(skipped_sample),
            excluded_sample=tuple(excluded_sample),
            written_bytes=written_total,
        )

    except BaseException as exc:
        # 清理只有这一个出口（成功时已经 return，走不到这里）。
        if created_root:
            shutil.rmtree(destp, ignore_errors=True)
        if isinstance(exc, (zipfile.BadZipFile, zlib.error)):
            raise EngineUnpackFailed(
                archive=zp,
                target_dir=destp,
                kind=UnpackFailure.BAD_ARCHIVE,
                detail=f"不是合法的 zip: {exc}",
            ) from exc
        if isinstance(exc, OSError):
            # 裸 OSError 不许逃出本模块。先判 ENOSPC，再兜 WRITE_ERROR：
            # 混成一档就会出现"磁盘满了让用户去重下 295 MB"。
            kind = (
                UnpackFailure.NO_SPACE
                if exc.errno == errno.ENOSPC
                else UnpackFailure.WRITE_ERROR
            )
            raise EngineUnpackFailed(
                archive=zp,
                target_dir=destp,
                kind=kind,
                # 只有 NO_SPACE 带容量数字（权限错误给容量 = 指错自救方向）。
                detail=_io_failure_detail(
                    destp.parent,
                    exc,
                    need_bytes=declared_selected,
                    with_capacity=kind is UnpackFailure.NO_SPACE,
                ),
            ) from exc
        raise


def _locate_core(dest: Path, release: ReleaseSpec) -> Path:
    """按磁盘事实定位核心库：**必须唯一命中**。

    候选位置限定为 ``release.rel_subtrees``（``root/``、``root/bin/``、
    ``root/lib/``）。0 个 → missing 带出 ``core_hint``；>1 个 → ``missing=()``
    且 ``found`` 全量带出。**绝不"挑第一个"**：猜错时用户只会看到"加载失败"，
    永远想不到是解压出了两份。``missing`` 是否为空就是"缺东西"与"有歧义"的判据，
    文案层靠它区分两种完全不同的自救动作。
    """
    candidates: set[Path] = set()
    for subtree in release.rel_subtrees:
        base = dest if not subtree else dest / subtree
        if _is_link(base) or not base.is_dir():
            continue
        for name in CORE_BASENAMES:
            hit = base / name
            if hit.is_file() and not _is_link(hit):
                candidates.add(hit.resolve())

    if len(candidates) != 1:
        raise EngineLayoutInvalid(
            root=dest,
            missing=[] if candidates else [release.core_hint],
            found=sorted(str(p) for p in candidates),
        )
    return next(iter(candidates))


# ---------------------------------------------------------------------------
# 路径 / 条目校验（解压的安全核心）
# ---------------------------------------------------------------------------
def _check_member_name(
    name: str, archive: Path, target_dir: Path
) -> tuple[str, ...]:
    """把条目名规范化成安全 ``parts``；不安全直接抛 ``UNSAFE_MEMBER``。

    这里**不做"修正后继续"**：任何需要修正的名字都直接拒绝。因为"修一下还能用"
    意味着实现里存在一条"攻击者能控制落点"的代码路径，而这条路径平时不执行、
    只在被攻击时执行——它永远得不到足够的测试。

    刻意**不抽工厂函数**：``raise EngineUnpackFailed(...)`` 逐处直写，这样
    ``tests`` 的 AST 不变量（R17）能看见每个 raise 的类型。
    """
    if not name or "\x00" in name:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail="空名字或含 NUL",
        )
    if "\\" in name:
        # 不把 `\` 规范化成 `/`：Windows API 会把它当分隔符，两边理解不一致就是穿越。
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail="含反斜杠（拒绝而非规范化）",
        )
    if name.startswith("/"):
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail="绝对路径",
        )
    if len(name) >= 2 and name[1] == ":":
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail="含盘符",
        )
    if len(name) > MAX_PATH_LENGTH:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail="条目名过长",
        )

    parts = tuple(p for p in name.split("/") if p not in ("", "."))
    if not parts:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail="规范化后为空",
        )
    if len(parts) > MAX_PATH_PARTS:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=name,
            detail=f"层级过深（>{MAX_PATH_PARTS}）",
        )
    for part in parts:
        if part == "..":
            raise EngineUnpackFailed(
                archive=archive,
                target_dir=target_dir,
                kind=UnpackFailure.UNSAFE_MEMBER,
                member=name,
                detail="含 .. 路径段",
            )
        _check_part(part, name, archive, target_dir)
    return parts


def _check_part(part: str, full: str, archive: Path, target_dir: Path) -> None:
    """单段名校验：非法字符 / 尾点尾空格 / Windows 保留名 / 超长。"""
    if len(part) > MAX_PART_LENGTH:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=full,
            detail=f"路径段过长: {part[:32]}…",
        )
    if any(ch in _BAD_NAME_CHARS for ch in part):
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=full,
            detail=f"非法字符（含 NTFS ADS 冒号）: {part!r}",
        )
    if any(ord(ch) < 0x20 for ch in part):
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=full,
            detail="含控制字符",
        )
    if part.endswith(" ") or part.endswith("."):
        # Windows 会静默去掉尾点/尾空格：`a.` 与 `a` 落到同一个文件，
        # 于是"先校验名字再写盘"可以被两个不同名字绕成同一落点。
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=full,
            detail="路径段以点或空格结尾",
        )
    stem = part.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=full,
            detail=f"Windows 保留名: {part!r}",
        )


def _is_unsafe_mode(info: zipfile.ZipInfo) -> bool:
    """拒绝符号链接 / 设备 / FIFO 条目（``external_attr`` 高 16 位是 unix mode）。"""
    mode = info.external_attr >> 16
    if not mode:
        return False
    if stat.S_ISLNK(mode):
        return True
    return bool(
        stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISSOCK(mode)
    )


def _is_excluded(parts: tuple[str, ...]) -> bool:
    """硬排除：``externals/**`` + ``MAA.exe`` + ``*.pdb``（按 basename，大小写不敏感）。

    **排除不是安全机制**：它只缩落盘面。安全来自 :func:`_check_member_name`，
    所以这个函数永远跑在名字校验**之后**。
    """
    if parts[0].lower() in _EXCLUDED_PREFIXES_LOWER:
        return True
    base = parts[-1].lower()
    return any(fnmatch.fnmatch(base, pattern) for pattern in _EXCLUDED_GLOBS_LOWER)


def _is_included(parts: tuple[str, ...]) -> bool:
    """白名单：``resource/**`` + 归档根文件 + ``bin/**`` / ``lib/**``。"""
    head = parts[0].lower()
    if head in INCLUDED_PREFIXES or head in INCLUDED_FALLBACK_PREFIXES:
        return True
    return len(parts) == 1  # 归档根目录下的散文件（真实包就是这样：无 MAA/ 包裹层）


def _is_link(path: Path) -> bool:
    """符号链接**或** junction。

    Windows 上 ``Path.is_symlink()`` 对目录 junction 返回 ``False``（3.12 之前），
    而 junction 同样能把落点换到任意目录。``os.path.isjunction`` 在 3.12+ 才有，
    所以用 ``getattr`` 探测，不写版本分支。
    """
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except OSError:
            return False
    return False


_is_junction = _is_link  # 别名：语义上"是链接类对象"，避免调用点各写一遍


def _ensure_dir_no_symlink(
    base: Path,
    rel_parts: tuple[str, ...],
    dest_real: str,
    archive: Path,
    target_dir: Path,
) -> Path:
    """逐级创建目录，**每一级都拒绝链接**，并断言最终落点仍在 ``dest`` 内。

    为什么不能用 ``mkdir(parents=True, exist_ok=True)``：它**会穿过**已存在的
    目录链接/junction —— 同机进程只要在解压窗口内把 ``resource/`` 换成指向
    ``C:\\Windows\\System32`` 的链接，后续写入就落到系统目录（T15）。这是本地
    提权/持久化的经典形态，而且不需要任何"恶意 zip"。
    """
    cur = base
    for part in rel_parts:
        cur = cur / part
        if _is_link(cur):
            raise EngineUnpackFailed(
                archive=archive,
                target_dir=target_dir,
                kind=UnpackFailure.UNSAFE_MEMBER,
                member="/".join(rel_parts),
                detail=f"目录组件是链接（疑似 TOCTOU 换向）: {cur}",
            )
        if cur.exists():
            if not cur.is_dir():
                raise EngineUnpackFailed(
                    archive=archive,
                    target_dir=target_dir,
                    kind=UnpackFailure.WRITE_ERROR,
                    member="/".join(rel_parts),
                    detail=f"同名路径不是目录: {cur}",
                )
        else:
            try:
                cur.mkdir()
            except FileExistsError:
                # 竞态：有人抢先建了（可能是链接）。**不允许**"建不上就当作已存在"
                # 直接放行 —— 重新做一遍链接与目录检查。
                if _is_link(cur) or not cur.is_dir():
                    raise EngineUnpackFailed(
                        archive=archive,
                        target_dir=target_dir,
                        kind=UnpackFailure.UNSAFE_MEMBER,
                        member="/".join(rel_parts),
                        detail="目录创建竞态：落点变成链接/非目录",
                    ) from None
    _ensure_contained(cur, dest_real, archive, target_dir, "/".join(rel_parts))
    return cur


def _ensure_contained(
    path: Path,
    dest_real: str,
    archive: Path,
    target_dir: Path,
    member: str,
) -> None:
    """realpath 前缀断言：``path`` 解析后必须仍在 ``dest_real`` 之内。

    ``_check_member_name`` 已经挡掉 ``..`` / 绝对路径，这里是**第三道**：
    万一将来有人加了"规范化"或"修正"逻辑，或某个平台的路径语义和 POSIX 不同，
    这一条还能兜住。用 ``commonpath`` 而不是 ``str.startswith``：后者会被
    ``dest-evil/`` 这类同前缀目录绕过。
    """
    root = os.path.normcase(os.path.realpath(dest_real))
    try:
        real = os.path.normcase(os.path.realpath(path))
        common = os.path.commonpath([root, real])
    except (OSError, ValueError) as exc:  # 跨盘符 / 不可解析 → 一律视为逃逸
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=member,
            detail=f"路径无法解析为同一卷: {path}（{exc}）",
        ) from exc
    if common != root:
        raise EngineUnpackFailed(
            archive=archive,
            target_dir=target_dir,
            kind=UnpackFailure.UNSAFE_MEMBER,
            member=member,
            detail=f"落点逃逸 dest: {path}",
        )


# ---------------------------------------------------------------------------
# 导入期自检：把"注释里的承诺"变成"导入就炸"的约束
# ---------------------------------------------------------------------------
def _assert_release_table() -> None:
    """清单表自检：URL 形态、哈希格式、size、平台键一致。

    放在导入期而不是测试里：常量表是全信任根，写坏一行等于把校验关掉。
    宁可让插件启动失败（开发期立刻发现），也不要带着一个"看起来在跑"的错表上线。
    """
    if not KNOWN_RELEASES:
        raise RuntimeError("KNOWN_RELEASES 为空：v1 至少要有 windows-x64 一项")
    for key, release in KNOWN_RELEASES.items():
        if release.platform != key:
            raise RuntimeError(f"KNOWN_RELEASES[{key!r}].platform 不一致: {release.platform!r}")
        if "/releases/latest" in release.url or "/releases/download/" not in release.url:
            raise RuntimeError(
                f"KNOWN_RELEASES[{key!r}].url 必须是版本化直链（禁止 latest）: {release.url}"
            )
        if _normalize_digest(release.sha256) != release.sha256:
            raise RuntimeError(f"KNOWN_RELEASES[{key!r}].sha256 不是 64 位小写 hex")
        if release.size <= 0:
            raise RuntimeError(f"KNOWN_RELEASES[{key!r}].size 必须为正")
        if "api.github.com" in release.url:
            raise RuntimeError("禁止在运行时访问 api.github.com")
        try:
            _validate_url(release.url, Path(release.archive_name or "<archive>"))
        except EngineDownloadFailed as exc:
            raise RuntimeError(
                f"KNOWN_RELEASES[{key!r}].url 未通过白名单校验: {exc.context.get('detail')}"
            ) from exc
        for subtree in release.rel_subtrees:
            if subtree.startswith(("/", "\\")) or ".." in subtree.split("/"):
                raise RuntimeError(f"KNOWN_RELEASES[{key!r}].rel_subtrees 非法: {subtree!r}")


_DOWNLOAD_KINDS_RAISED: Final[frozenset[DownloadFailure]] = frozenset({
    DownloadFailure.NETWORK,
    DownloadFailure.THROTTLED,
    DownloadFailure.ASSET_GONE,
    DownloadFailure.SIZE_MISMATCH,
    DownloadFailure.NO_SPACE,
    DownloadFailure.REJECTED_SCHEME,
    DownloadFailure.WRITE_ERROR,
    DownloadFailure.CANCELLED,
})
_UNPACK_KINDS_RAISED: Final[frozenset[UnpackFailure]] = frozenset({
    UnpackFailure.BAD_ARCHIVE,
    UnpackFailure.UNSAFE_MEMBER,
    UnpackFailure.LIMIT_EXCEEDED,
    UnpackFailure.SIZE_DRIFT,
    UnpackFailure.NO_SPACE,
    UnpackFailure.WRITE_ERROR,
})
"""本模块**实际会抛**的 kind 全集。

与枚举做**相等**断言（不是子集）：枚举加了一档而 provision 不产它，说明文案表
会多出一条永远走不到的"死文案"；反过来枚举少了一档而这里写了它，是编译期就该
发现的错。两者都该在导入期炸，而不是在某个用户取消下载时才发现少了分支。
"""


def _assert_kind_coverage() -> None:
    missing_download = set(DownloadFailure) - set(_DOWNLOAD_KINDS_RAISED)
    extra_download = set(_DOWNLOAD_KINDS_RAISED) - set(DownloadFailure)
    missing_unpack = set(UnpackFailure) - set(_UNPACK_KINDS_RAISED)
    extra_unpack = set(_UNPACK_KINDS_RAISED) - set(UnpackFailure)
    if missing_download or extra_download:
        raise RuntimeError(
            "provision 的 DownloadFailure 覆盖与枚举不一致："
            f"未产出={sorted(k.value for k in missing_download)} "
            f"越界={sorted(k.value for k in extra_download)}"
        )
    if missing_unpack or extra_unpack:
        raise RuntimeError(
            "provision 的 UnpackFailure 覆盖与枚举不一致："
            f"未产出={sorted(k.value for k in missing_unpack)} "
            f"越界={sorted(k.value for k in extra_unpack)}"
        )


_assert_release_table()
_assert_kind_coverage()
