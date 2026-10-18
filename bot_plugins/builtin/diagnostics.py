from __future__ import annotations

from html import escape
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot_plugins.core import BotPlugin, PluginContext, PluginSpec


class DiagnosticsPlugin(BotPlugin):
    spec = PluginSpec(
        name="diagnostics",
        version="1.0.0",
        description="Health, plugin inventory, and host statistics.",
        commands=(("health", "Run host health checks"), ("plugins", "List loaded plugins"), ("stats", "Show host statistics")),
        admin_commands=("stats",),
    )

    def __init__(self) -> None:
        self.context: PluginContext | None = None

    def handlers(self):
        return (
            CommandHandler("health", self.health_command),
            CommandHandler("plugins", self.plugins_command),
            CommandHandler("stats", self.stats_command),
        )

    async def on_startup(self, application, context: PluginContext) -> None:
        self.context = context

    async def health_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        manager = context.bot_data["plugin_manager"]
        report = await manager.health_report()
        is_admin = self.context is not None and update.effective_user.id in self.context.config.ADMIN_IDS
        visible_names = {item["name"] for item in manager.catalog(include_admin=is_admin)}
        report = [item for item in report if item["name"] in visible_names]
        healthy = sum(1 for item in report if item["ok"])
        lines = ["<b>🩺 HOST HEALTH</b>", ""]
        for item in report:
            icon = "🟢" if item["ok"] else "🔴"
            lines.append(f"{icon} <b>{escape(str(item['name']))}</b> — {escape(str(item['detail']))}")
        lines.extend(["", f"<b>{healthy}/{len(report)}</b> checks passing"])
        await update.message.reply_html("\n".join(lines))

    async def plugins_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        manager = context.bot_data["plugin_manager"]
        is_admin = self.context is not None and update.effective_user.id in self.context.config.ADMIN_IDS
        lines = ["<b>🧩 LOADED PLUGINS</b>", ""]
        for item in manager.catalog(include_admin=is_admin):
            status = "🟢 enabled" if item["enabled"] else f"🔴 disabled: {escape(str(item['error']))}"
            commands = ", ".join(f"/{command}" for command in item["commands"]) or "no commands"
            lines.append(f"<b>{escape(item['name'])}</b> <code>v{escape(item['version'])}</code>\n{escape(item['description'])}\n{status} · {escape(commands)}\n")
        await update.message.reply_html("\n".join(lines))

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.context is None:
            await update.effective_message.reply_text("Diagnostics are still starting. Try again in a moment.")
            return
        if update.effective_user.id not in self.context.config.ADMIN_IDS:
            await update.effective_message.reply_text("⛔ Admin access required.")
            return
        stats = self.context.db.get_stats()
        await update.message.reply_html(
            "<b>📊 HOST STATISTICS</b>\n\n"
            f"Services: <code>{stats['total']}</code>\n"
            f"Running: <code>{stats['running']}</code>\n"
            f"Stopped: <code>{stats['stopped']}</code>\n"
            f"Errors: <code>{stats['error']}</code>\n"
            f"Users: <code>{stats['users']}</code>"
        )

    async def health(self) -> tuple[bool, str]:
        if self.context is None:
            return False, "context not initialized"
        return True, "diagnostics ready"


def create_plugin() -> BotPlugin:
    return DiagnosticsPlugin()
