"""Harness Plugin 的格式适配、不可变存储和管理入口。"""

from typing import TYPE_CHECKING

from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.model import PluginError
from harness_agent.plugins.resources import PluginResourceAsset, PluginResourceSnapshot

if TYPE_CHECKING:
    from harness_agent.plugins.runtime import (
        PluginRuntimeCatalog,
        PluginRuntimeError,
        PluginRuntimeManager,
    )

__all__ = [
    "PluginError",
    "PluginManager",
    "PluginResourceAsset",
    "PluginResourceSnapshot",
    "PluginRuntimeCatalog",
    "PluginRuntimeError",
    "PluginRuntimeManager",
]


def __getattr__(name: str):
    """只在 Host 需要执行 runtime 时加载其可选 LangChain 依赖。"""
    if name in {"PluginRuntimeCatalog", "PluginRuntimeError", "PluginRuntimeManager"}:
        from harness_agent.plugins.runtime import (
            PluginRuntimeCatalog,
            PluginRuntimeError,
            PluginRuntimeManager,
        )

        return {
            "PluginRuntimeCatalog": PluginRuntimeCatalog,
            "PluginRuntimeError": PluginRuntimeError,
            "PluginRuntimeManager": PluginRuntimeManager,
        }[name]
    raise AttributeError(name)
