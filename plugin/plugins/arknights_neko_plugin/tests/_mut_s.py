"""对 S 组用例做进程外变异验牙：每次只改一处，跑指定用例，期望转红。

用法：python tests/_mut_s.py <variant> <case-ids>
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/mutS/plg")


def patch(rel: str, old: str, new: str) -> None:
    path = ROOT / rel
    text = io.open(path, encoding="utf-8").read()
    assert text.count(old) == 1, f"锚点命中 {text.count(old)} 次：{old[:60]!r}"
    io.open(path, "w", encoding="utf-8", newline="\n").write(text.replace(old, new))


variant = sys.argv[1]

if variant == "M-S1":
    # 渲染退化成 str(exc)：路径/栈/机器值全进文案
    patch(
        "companion/service.py",
        '    if not isinstance(exc, EngineError):\n'
        '        logger.warning("render_error got non-EngineError: %s", type(exc).__name__)\n'
        '        return [_text("引擎那边出了点状况喵，我先记下来了；再试一次看看。")]',
        '    return [_text(str(getattr(exc, "message", exc)))]',
    )
elif variant == "M-S2":
    # push 变成"用 await 调"：同步 push_message 会 TypeError → transport_error
    patch(
        "companion/service.py",
        "            result = self._push_fn(**kwargs)      # 同步：绝不 await",
        "            result = await self._push_fn(**kwargs)  # 变异：按旧稿 await",
    )
elif variant == "M-S3":
    # 构造期就推一条"引擎准备好了"：把"钩子不 push"变成可能
    patch(
        "companion/service.py",
        "        self._last_notice_key: str | None = None",
        "        self._push_fn(source='boot', visibility=['chat'], ai_behavior='read', parts=[{'type': 'text', 'text': '喵'}, {'type': 'text', 'text': '再试一次'}], priority=3)\n"
        "        self._last_notice_key: str | None = None",
    )
elif variant == "M-S4":
    # 三个 unpack kind 指向同一份文案：分档失效
    patch(
        "companion/service.py",
        '            UnpackFailure.BAD_ARCHIVE.value: _variant(\n'
        '                "包装不上喵（这个 zip 本身是坏的）",\n'
        '                "换官方 Release 包重新下一次。",\n'
        '            ),',
        '            UnpackFailure.BAD_ARCHIVE.value: _variant(\n'
        '                "包装不上喵（里面有东西被我拦下了）",\n'
        '                "换官方 Release 包重新下一次。",\n'
        '            ),',
    )
elif variant == "M-S5":
    # 拒绝码不再归一：把 reason 原样往上游送（当前实现就是这样，这里改成"归一"看不出来）
    # 反过来：让 submitted 判据失效（读 ok 而不是 submitted）
    patch(
        "companion/service.py",
        '            if "submitted" in result:\n'
        '                submitted = bool(result.get("submitted"))\n'
        '            else:\n'
        '                submitted = bool(result.get("ok"))',
        '            submitted = bool(result.get("ok", True))',
    )
elif variant == "M-S6":
    # BAD_ARCHIVE 整条指向 UNSAFE_MEMBER（文案+hint+优先级全同）：分档真的失效了
    patch(
        "companion/service.py",
        '            UnpackFailure.BAD_ARCHIVE.value: _variant(\n'
        '                "包装不上喵（这个 zip 本身是坏的）",\n'
        '                "换官方 Release 包重新下一次。",\n'
        '            ),',
        '            UnpackFailure.BAD_ARCHIVE.value: _variant(\n'
        '                "包装不上喵（里面有东西被我拦下了）",\n'
        '                "这是安全事件：包里有越界路径/链接/加密条目，别用它。",\n'
        '                priority=5,\n'
        '            ),',
    )
elif variant == "M-S7":
    # 消毒函数退回"只 strip"：换行重新变成指令块边界
    patch(
        "companion/service.py",
        '    collapsed = _PATH_FOLD.sub("<path>", " ".join("".join(kept).split()))',
        '    collapsed = raw.strip()',
    )
else:
    raise SystemExit(f"未知变异 {variant}")

print(f"已注入 {variant}")
