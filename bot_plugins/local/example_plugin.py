"""Copy this pattern to add a project-local plugin.

Enable it with:
    PLUGIN_MODULES=bot_plugins.local.example_plugin
"""

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot_plugins.core import BotPlugin, PluginSpec


class ExamplePlugin(BotPlugin):
    spec = PluginSpec(
        name="example",
        version="1.0.0",
        description="Reference plugin for project-local extensions.",
        commands=(("ping", "Check that the bot is responding"),),
    )

    def handlers(self):
        return (CommandHandler("ping", self.ping),)

    async def ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html("<b>🏓 pong</b> · plugin runtime is online")


def create_plugin() -> BotPlugin:
    return ExamplePlugin()
