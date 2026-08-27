"""Extensible plugin runtime for the Telegram hosting bot.

Plugins are ordinary Python modules that expose ``create_plugin()`` and return
an object implementing :class:`BotPlugin`. A plugin can register handlers,
commands, startup/shutdown hooks, and health checks without modifying the core
worker.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

from telegram import BotCommand
from telegram.ext import Application, BaseHandler

logger = logging.getLogger("UltimateHost.plugins")


class PluginContext(Protocol):
    """The stable services exposed to plugins by the host application."""

    config: Any
    db: Any
    process_mgr: Any
    log_streamer: Any
    ui: Any


HealthCheck = Callable[[], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class PluginSpec:
    name: str
    version: str
    description: str
    commands: tuple[tuple[str, str], ...] = ()
    admin_only: bool = False


class BotPlugin:
    """Base class for plugins.

    Subclasses can override ``handlers`` and lifecycle methods. The host keeps
    plugin failures isolated: one broken extension does not stop the bot from
    loading the remaining extensions.
    """

    spec = PluginSpec("unnamed", "0.0.0", "No description")

    def handlers(self) -> Iterable[BaseHandler]:
        return ()

    async def on_startup(self, application: Application, context: PluginContext) -> None:
        return None

    async def on_shutdown(self, application: Application, context: PluginContext) -> None:
        return None

    async def health(self) -> tuple[bool, str]:
        return True, "ready"


@dataclass
class LoadedPlugin:
    plugin: BotPlugin
    module_name: str
    handlers: list[BaseHandler] = field(default_factory=list)
    enabled: bool = True
    error: Optional[str] = None


class PluginManager:
    """Discover, register, and introspect core and user-provided plugins."""

    BUILTIN_MODULES = (
        "bot_plugins.builtin.diagnostics",
        "bot_plugins.builtin.services",
        "bot_plugins.builtin.admin",
    )

    def __init__(self, application: Application, context: PluginContext):
        self.application = application
        self.context = context
        self.loaded: list[LoadedPlugin] = []

    def _module_names(self) -> list[str]:
        custom = [name.strip() for name in os.getenv("PLUGIN_MODULES", "").split(",") if name.strip()]
        return [*self.BUILTIN_MODULES, *custom]

    def discover(self) -> list[LoadedPlugin]:
        for module_name in self._module_names():
            try:
                module = importlib.import_module(module_name)
                factory = getattr(module, "create_plugin", None)
                if factory is None:
                    raise AttributeError("module must expose create_plugin()")
                plugin = factory()
                if not isinstance(plugin, BotPlugin):
                    raise TypeError("create_plugin() must return BotPlugin")
                handlers = list(plugin.handlers())
                loaded = LoadedPlugin(plugin, module_name, handlers)
                self.loaded.append(loaded)
                for handler in handlers:
                    self.application.add_handler(handler)
                logger.info("Loaded plugin %s v%s (%s)", plugin.spec.name, plugin.spec.version, module_name)
            except Exception as exc:
                logger.exception("Failed to load plugin %s", module_name)
                self.loaded.append(LoadedPlugin(
                    plugin=BotPlugin(),
                    module_name=module_name,
                    enabled=False,
                    error=str(exc),
                ))
        return self.loaded

    async def startup(self) -> None:
        for loaded in self.loaded:
            if not loaded.enabled:
                continue
            try:
                await loaded.plugin.on_startup(self.application, self.context)
            except Exception:
                logger.exception("Startup hook failed for plugin %s", loaded.module_name)

    async def shutdown(self) -> None:
        for loaded in reversed(self.loaded):
            if not loaded.enabled:
                continue
            try:
                await loaded.plugin.on_shutdown(self.application, self.context)
            except Exception:
                logger.exception("Shutdown hook failed for plugin %s", loaded.module_name)

    def bot_commands(self) -> list[BotCommand]:
        commands: list[BotCommand] = []
        for loaded in self.loaded:
            if loaded.enabled:
                commands.extend(BotCommand(command, description) for command, description in loaded.plugin.spec.commands)
        return commands

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": loaded.plugin.spec.name,
                "version": loaded.plugin.spec.version,
                "description": loaded.plugin.spec.description,
                "commands": [command for command, _ in loaded.plugin.spec.commands],
                "enabled": loaded.enabled,
                "error": loaded.error,
            }
            for loaded in self.loaded
        ]

    async def health_report(self) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for loaded in self.loaded:
            if not loaded.enabled:
                report.append({"name": loaded.module_name, "ok": False, "detail": loaded.error or "disabled"})
                continue
            try:
                ok, detail = await loaded.plugin.health()
            except Exception as exc:
                ok, detail = False, str(exc)
            report.append({"name": loaded.plugin.spec.name, "ok": ok, "detail": detail})
        return report
