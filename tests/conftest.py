"""pytest 收集开关：只让 pytest 收集 `test_smoke.py`。

为什么需要这个文件（2026-11-14）
--------------------------------
市场发布检查 `neko_plugin_cli check -r` 会**用 pytest 跑一遍 tests/**。而本仓库的
两套真用例不是 pytest 用例：

* 它们有自己的注册表、收集期闸门与退出码语义（`tests/_runner.py`：0 全绿 /
  1 有失败 / 2 收集期不一致 / 3 有未实现或阻塞），入口是
  ``python tests/test_contract.py``，不是 pytest 的 fixture 模型；
* 其中若干条是 **Windows 专属机制**回归（`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`、
  `GetModuleHandle`、`_dll_dir_handle` 生命周期），在 Linux runner 上必然红。

被 pytest 误收集的后果是：check -r 报"tests failed with exit code 1"，
而实际既不是回归、也不是本插件的问题 —— 是入口搞错了（实测 6 failed / 121 passed，
失败的 6 条全是 Windows 专属或按 POSIX 路径断言）。

所以这里显式只保留 pytest 形状的冒烟用例；两套真用例仍按文档跑：

    python tests/test_contract.py     # 110 条
    python tests/test_service.py      # 16 条

`_mut_*.py` / `_fakelib.py` / `_runner.py` / `_bootstrap.py` 不匹配 `test_*.py`，
本来就不会被收集，列在这里只是把意图写死，防止将来有人改文件名时踩回去。
"""

collect_ignore = [
    "test_contract.py",
    "test_service.py",
    "_bootstrap.py",
    "_fakelib.py",
    "_runner.py",
    "_mut_a.py",
    "_mut_matrix.py",
    "_mut_p.py",
    "_mut_r.py",
    "_mut_s.py",
]
