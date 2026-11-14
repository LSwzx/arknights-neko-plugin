"""把插件根与 tests 目录放进 `sys.path`，供用例文件在**文件顶部**直接 import。

为什么要有这个模块（2026-11-14）
--------------------------------
CI 的上游 verify 是这样调 ruff 的（`plugin-market-verify.yml@main`）::

    uvx ruff==0.12.4 check --ignore-noqa --isolated \\
        --select E4,E7,E9,F,I --target-version py311 --line-length 120

两个参数决定了"加注释白名单"这条路走不通：

* ``--ignore-noqa`` —— 所有 ``# noqa`` **一律失效**，写 ``# noqa: E402`` 等于没写；
* ``--isolated`` —— 仓库里的 ``ruff.toml`` **完全不读**，所以
  ``[lint.per-file-ignores]`` 也挡不住任何东西（实测：CI 报 38 条，
  本地 ``ruff check .`` 只报 29 条，差的 9 条正是所有带 noqa 的行）。

于是"先执行语句把 sys.path 塞好、再 import"这个老写法在 CI 里必然触发
``E402 module level import not at top of file``，且无法抑制。唯一的出路是
**把这个副作用搬进另一个模块的 import 期**：

    用例文件里：  import _bootstrap      # 副作用：sys.path 就绪
                  import maa.wrapper     # 紧跟其后，仍在文件顶部

这样整份文件的 import 全部连续位于顶部，E402 从根上消失 —— 不是被注释盖住，
而是真的没有"import 落在语句之后"这件事。

用法
----
``import _bootstrap`` 之后即可 ``import maa.*`` / ``import companion.*`` /
``import _fakelib``；需要路径时用 ``_bootstrap.PLUGIN_ROOT`` /
``_bootstrap.TESTS_DIR``，不要再用 ``Path(__file__).resolve().parents[1]``
另算一份（两处算路径迟早会不一致）。

注意：``tests/`` 自身在 ``sys.path`` 里，是 Python 以脚本方式运行
``python tests/test_contract.py`` 时自动加上的（``sys.path[0]`` = 脚本所在目录），
所以 ``import _bootstrap`` 本身不需要任何前置引导。被别的脚本
``import test_contract`` 时，那个脚本也必须先 ``import _bootstrap``。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

# 插件根在前（`maa` / `companion` 能被找到），tests 目录在后（`_fakelib` /
# `_runner` 能被找到）。用 insert(0) 按相反顺序压栈，最终顺序如上。
for _path in (str(TESTS_DIR), str(PLUGIN_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
