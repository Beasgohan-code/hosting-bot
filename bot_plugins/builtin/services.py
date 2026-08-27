from __future__ import annotations

from html import escape
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot_plugins.core import BotPlugin, PluginContext, PluginSpec


class ServicesPlugin(BotPlugin):
    spec = PluginSpec(
        name="services",
        version="1.0.0",
        description="Telegram-native service inventory and status lookup.",
        commands=(("services", "List your hosted services"), ("service", "Inspect a service by ID")),
    )

    def __init__(self) -> None:
        self.context: PluginContext | None = None

    def handlers(self):
        return (
            CommandHandler("services", self.services_command),
            CommandHandler("service", self.service_command),
        )

    async def on_startup(self, application, context: PluginContext) -> None:
        self.context = context

    def _is_admin(self, user_id: int) -> bool:
        return self.context is not None and user_id in self.context.config.ADMIN_IDS

    def _allowed(self, bot, user_id: int) -> bool:
        return bot is not None and (bot.owner_id == user_id or self._is_admin(user_id))

    async def services_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.context is None:
            await update.message.reply_text("Services are still starting. Try again in a moment.")
            return
        user_id = update.effective_user.id
        bots = self.context.db.get_all_bots() if self._is_admin(user_id) else self.context.db.get_user_bots(user_id)
        if not bots:
            await update.message.reply_text("📭 No hosted services yet. Use /start to deploy one.")
            return
        lines = ["<b>📋 HOSTED SERVICES</b>", ""]
        for bot in bots:
            icon = {"running": "🟢", "stopped": "⏹", "error": "🔴", "deploying": "🟡"}.get(bot.status, "⚪")
            lines.append(f"{icon} <b>{escape(bot.name)}</b> <code>{escape(bot.bot_id)}</code>\nStatus: <code>{escape(bot.status.upper())}</code> · Mode: <code>{escape(bot.mode.upper())}</code>")
        lines.append("\nUse <code>/service BOT_ID</code> for details.")
        await update.message.reply_html("\n".join(lines))

    async def service_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.context is None:
            await update.message.reply_text("Services are still starting. Try again in a moment.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /service <bot_id>\nUse /services to list available IDs.")
            return
        bot = self.context.db.get_bot(context.args[0])
        if not self._allowed(bot, update.effective_user.id):
            await update.message.reply_text("❌ Service not found or access denied.")
            return
        resource = self._resource(bot)
        await update.message.reply_html(
            f"<b>🔧 {escape(bot.name)}</b>\n\n"
            f"ID: <code>{escape(bot.bot_id)}</code>\n"
            f"Status: <b>{escape(bot.status.upper())}</b>\n"
            f"Mode: <code>{escape(bot.mode.upper())}</code>\n"
            f"PID: <code>{bot.pid or 'none'}</code>\n"
            f"Restarts: <code>{bot.restart_count}</code>\n"
            f"CPU: <code>{escape(str(resource.get('cpu', 'n/a')))}</code> · MEM: <code>{escape(str(resource.get('mem', 'n/a')))}</code>\n"
            f"Created: <code>{escape(bot.created_at[:19])}</code>\n\n"
            f"GitHub: <a href=\"{escape(bot.github_url)}\">open repository</a>"
        )

    @staticmethod
    def _resource(bot) -> dict:
        try:
            import json
            return json.loads(bot.resource_usage or "{}")
        except Exception:
            return {}

    async def health(self) -> tuple[bool, str]:
        return True, "service commands ready"


def create_plugin() -> BotPlugin:
    return ServicesPlugin()
