"""冒烟测试：只验"这个目录确实是一个可识别的 N.E.K.O 插件包"。

为什么需要它（2026-11-14）：市场发布检查 `neko_plugin_cli check -r` 在 strict 模式下
把 `tests/test_smoke.py` 列为必需文件（`validate_cmd.py::_check_optional_file`），
缺了直接 exit 1。本文件按官方模板（`templates/generator.py::_render_smoke_test`）的形状写，
只断言清单自洽，**不导入 SDK、不建运行时**——它要能在任何机器上零依赖跑过，
真正的行为验证在 `tests/test_contract.py` 与 `tests/test_service.py`。
"""
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_exists() -> None:
    manifest = PLUGIN_ROOT / "plugin.toml"
    assert manifest.is_file(), f"plugin.toml 不在插件根：{manifest}"
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "arknights_neko_plugin"' in text, "plugin.toml 的 id 与目录/市场登记不一致"
    assert "entry = \"plugin.plugins.arknights_neko_plugin:ArknightsNekoCompanion\"" in text, (
        "plugin.toml 的 entry 指向变了：宿主按这个字符串找入口，写错就是启动即失败"
    )


if __name__ == "__main__":
    test_plugin_manifest_exists()
    print("smoke ok: plugin.toml 存在且 id/entry 自洽")
