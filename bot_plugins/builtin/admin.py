from __future__ import annotations

from html import escape

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot_plugins.core import BotPlugin, PluginContext, PluginSpec


class AdminPlugin(BotPlugin):
    spec = PluginSpec(
        name="admin",
        version="1.1.0",
        description="Admin-only operational summary and restart controls.",
        commands=(("admin", "Show admin operations"), ("restartall", "Restart running production services")),
        admin_only=True,
    )

    def __init__(self) -> None:
        self.context: PluginContext | None = None

    def handlers(self):
        return (
            CommandHandler("admin", self.admin_command),
            CommandHandler("restartall", self.restart_all_command),
        )

    async def on_startup(self, application, context: PluginContext) -> None:
        self.context = context

    def _authorized(self, update: Update) -> bool:
        return self.context is not None and update.effective_user.id in self.context.config.ADMIN_IDS

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.effective_message.reply_text("⛔ Admin access required.")
            return
        stats = self.context.db.get_stats()
        await update.effective_message.reply_html(
            "<b>🔴 ADMIN OPERATIONS</b>\n\n"
            f"Services: <code>{stats['total']}</code> · Running: <code>{stats['running']}</code>\n"
            "Use <code>/restartall</code> to restart running or failed production services.\n"
            "Use the inline admin panel from /start for cleanup and broadcast tools."
        )

    async def restart_all_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.effective_message.reply_text("⛔ Admin access required.")
            return
        bots = [
            bot for bot in self.context.db.get_all_bots()
            if bot.mode == "production" and bot.status in {"running", "error"}
        ]
        if not bots:
            await update.effective_message.reply_text("No running or failed production services to restart.")
            return

        await update.effective_message.reply_html(
            f"🟡 Restarting <code>{len(bots)}</code> running or failed production services…"
        )
        succeeded = 0
        failed = 0
        results: list[str] = []
        for bot in bots:
            success, message = await self.context.process_mgr.restart(bot)
            if success:
                succeeded += 1
                self.context.db.update_bot(
                    bot.bot_id,
                    status="running",
                    pid=bot.pid,
                    log_file=bot.log_file,
                    restart_count=bot.restart_count + 1,
                    last_error=None,
                )
                results.append(f"🟢 {escape(bot.name, quote=False)}")
            else:
                failed += 1
                still_running = self.context.process_mgr.is_alive(bot.bot_id)
                self.context.db.update_bot(
                    bot.bot_id,
                    status="running" if still_running else "error",
                    pid=bot.pid if still_running else None,
                    last_error=message[:200],
                )
                results.append(f"🔴 {escape(bot.name, quote=False)}")
                failure_notifier = getattr(self.context, "failure_notifier", None)
                if failure_notifier is not None:
                    await failure_notifier(context, bot, "Hosted bot restart-all operation failed", message)

        visible_results = results[:20]
        omitted = len(results) - len(visible_results)
        report = (
            "<b>RESTART RESULTS</b>\n\n"
            f"Succeeded: <code>{succeeded}</code> · Failed: <code>{failed}</code>\n\n"
            + "\n".join(visible_results)
        )
        if omitted:
            report += f"\n…and {omitted} more service(s)."
        await update.effective_message.reply_html(report)

    async def health(self) -> tuple[bool, str]:
        return True, "admin guard ready"


def create_plugin() -> BotPlugin:
    return AdminPlugin()
