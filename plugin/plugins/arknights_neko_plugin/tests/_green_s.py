"""在临时副本上把 S 组报出的 4 处缺口补掉，验证"13/13 是可达的"。

这不是交付代码，只是给"用例是不是写得太严"这个质疑准备的证据：
补完之后 S 组必须全绿；补之前红的正好是那四条。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])


def patch(rel: str, old: str, new: str) -> None:
    path = ROOT / rel
    text = io.open(path, encoding="utf-8").read()
    assert text.count(old) == 1, f"[{rel}] 锚点命中 {text.count(old)} 次：{old[:70]!r}"
    io.open(path, "w", encoding="utf-8", newline="\n").write(text.replace(old, new))


SVC = "companion/service.py"

# --- 1) 下载三档的 priority 回到 §3.5.1 的 3（表里只点名 cancelled=2） ---
patch(
    SVC,
    '            DownloadFailure.ASSET_GONE.value: _variant(\n'
    '                "官方那个版本的文件已经找不到了喵",\n'
    '                "这条只能靠升级插件解决：我不接受任意下载地址，换源必须改常量表并发版。",\n'
    '                priority=4,\n'
    '            ),',
    '            DownloadFailure.ASSET_GONE.value: _variant(\n'
    '                "官方那个版本的文件已经找不到了喵",\n'
    '                "这条只能靠升级插件解决：我不接受任意下载地址，换源必须改常量表并发版。",\n'
    '            ),',
)
patch(
    SVC,
    '                "磁盘放不下喵（要差不多 1.5 GB）",\n'
    '                "清一点空间，再点 arknights_engine_repair。",\n'
    '                priority=4,\n',
    '                "磁盘放不下喵（要差不多 1.5 GB）",\n'
    '                "清一点空间，再点 arknights_engine_repair。",\n',
)
patch(
    SVC,
    '                "这是安全边界，改不了；如果一直这样请把这条报告给维护者。",\n'
    '                priority=5,\n',
    '                "这是安全边界，改不了；如果一直这样请把这条报告给维护者。",\n',
)

# --- 2) 拒绝码归一：只允许三个稳定码 ---
patch(
    SVC,
    '            raw = result.get("reason")\n'
    '            reason = raw if isinstance(raw, str) and raw else "transport_error"\n'
    '            return (False, reason)',
    '            raw = result.get("reason")\n'
    '            reason = raw if isinstance(raw, str) and raw in _PUSH_REJECT_CODES else "transport_error"\n'
    '            return (False, reason)',
)
patch(
    SVC,
    '_PUSH_REJECT_HINT: Final[Mapping[str, str]] = {',
    '_PUSH_REJECT_CODES: Final[frozenset[str]] = frozenset(\n'
    '    {"backpressure", "transport_error", "transport_unavailable"}\n'
    ')\n\n'
    '_PUSH_REJECT_HINT: Final[Mapping[str, str]] = {',
)

# --- 3) 截图失败路径也走 dedupe（同一条消息不刷屏） ---
patch(
    SVC,
    '        if not parts:\n'
    '            return (False, "empty")\n'
    '        try:\n'
    '            result = self._push_fn(\n',
    '        if not parts:\n'
    '            return (False, "empty")\n'
    '        dedupe_key = f"{source}:" + "|".join(\n'
    '            str(part.get("text") or part.get("type")) for part in parts\n'
    '        )\n'
    '        if dedupe_key == self._last_push_key:\n'
    '            return (False, "deduped")\n'
    '        try:\n'
    '            result = self._push_fn(\n',
)
patch(
    SVC,
    '        except Exception:  # noqa: BLE001\n'
    '            logger.warning("push_message raised; treating as transport_error", exc_info=True)\n'
    '            return (False, "transport_error")\n'
    '        return self._interpret_push_result(result)',
    '        except Exception:  # noqa: BLE001\n'
    '            logger.warning("push_message raised; treating as transport_error", exc_info=True)\n'
    '            return (False, "transport_error")\n'
    '        submitted, reason = self._interpret_push_result(result)\n'
    '        if submitted:\n'
    '            self._last_push_key = dedupe_key\n'
    '        return (submitted, reason)',
)

# --- 4) 上游自由文本里的路径折叠成 <path>（K8：进模型前不带盘符路径） ---
patch(
    SVC,
    'def sanitize_maa_text(text: str, *, limit: int = 120) -> str:',
    '_PATH_FOLD: Final = __import__("re").compile(\n'
    '    r"[A-Za-z]:\\\\[^\\s\\u3000]*|/(?:home|Users|tmp|var|opt|root|etc|mnt)/[^\\s\\u3000]*"\n'
    ')\n\n\n'
    'def sanitize_maa_text(text: str, *, limit: int = 120) -> str:',
)
patch(
    SVC,
    '    collapsed = " ".join("".join(kept).split())',
    '    collapsed = _PATH_FOLD.sub("<path>", " ".join("".join(kept).split()))',
)

print("已注入 4 处修复")
