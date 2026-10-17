"""Public HTTP keep-alive checks with Telegram alerts and per-user ownership."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot_plugins.core import BotPlugin, PluginContext, PluginSpec

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        # Don't follow redirects: a public endpoint must not redirect the worker
        # into a private network or a cloud metadata endpoint.
        return None


def validate_monitor_url(raw_url: str, resolve: bool = False) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use a complete HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("The target port is invalid.") from exc
    if parsed.query or parsed.fragment:
        raise ValueError("Query strings and fragments are not supported for safety.")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Private and local network targets are not allowed.")

    addresses: set[str] = set()
    try:
        literal = ipaddress.ip_address(hostname)
        addresses.add(str(literal))
    except ValueError:
        if resolve:
            try:
                addresses.update(record[4][0] for record in socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM))
            except OSError as exc:
                raise ValueError("The target hostname could not be resolved.") from exc

    if resolve and not addresses:
        raise ValueError("The target hostname could not be resolved.")
    if addresses:
        for address in addresses:
            try:
                if not ipaddress.ip_address(address).is_global:
                    raise ValueError("Private and reserved IP addresses are not allowed.")
            except ValueError as exc:
                if str(exc) == "Private and reserved IP addresses are not allowed.":
                    raise
                raise ValueError("The target hostname resolved to an invalid address.") from exc

    return parsed.geturl()


def _probe_url(raw_url: str, timeout: int) -> tuple[bool, int | None, int, str | None]:
    started = time.monotonic()
    try:
        url = validate_monitor_url(raw_url, resolve=True)
        request = urllib.request.Request(url, headers={"User-Agent": "HostingBot-KeepAlive/1.0"}, method="GET")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                status_code = response.getcode()
        except urllib.error.HTTPError as response_error:
            status_code = response_error.code
        latency_ms = round((time.monotonic() - started) * 1000)
        ok = status_code is not None and 200 <= status_code < 400
        return ok, status_code, latency_ms, None if ok else f"HTTP {status_code}"
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        return False, None, latency_ms, str(exc)[:180]


class KeepAlivePlugin(BotPlugin):
    spec = PluginSpec(
        name="keepalive",
        version="1.0.0",
        description="Monitor public HTTP endpoints, pause checks, and receive down/recovery alerts.",
        commands=(("keepalive", "Manage endpoint keep-alive monitors"),),
    )

    def __init__(self) -> None:
        self.context: PluginContext | None = None
        self.application = None
        self._sweep_lock = asyncio.Lock()
        self._in_flight: set[str] = set()

    def handlers(self):
        return (CommandHandler("keepalive", self.keepalive_command),)

    async def on_startup(self, application, context: PluginContext) -> None:
        self.context = context
        self.application = application
        application.job_queue.run_repeating(
            self.check_due_monitors,
            interval=context.config.KEEPALIVE_SWEEP_INTERVAL,
            first=5,
            name="keepalive-monitor-sweep",
        )

    def _allowed(self, monitor: dict, user_id: int) -> bool:
        return monitor["owner_id"] == user_id or (
            self.context is not None and user_id in self.context.config.ADMIN_IDS
        )

    async def keepalive_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.context is None or update.effective_user is None or update.effective_message is None:
            return
        user_id = update.effective_user.id
        args = context.args
        action = args[0].lower() if args else "list"

        if action == "help":
            await update.effective_message.reply_text(self._help_text())
            return
        if action == "list":
            await self._list_monitors(update, user_id)
            return
        if action == "add":
            await self._add_monitor(update, user_id, args[1:])
            return
        if action in {"remove", "pause", "resume", "check"}:
            if len(args) < 2:
                await update.effective_message.reply_text(f"Usage: /keepalive {action} <monitor_id>")
                return
            await self._operate(update, user_id, action, args[1])
            return
        await update.effective_message.reply_text(self._help_text())

    async def _list_monitors(self, update: Update, user_id: int) -> None:
        if self.context is None:
            return
        is_admin = user_id in self.context.config.ADMIN_IDS
        monitors = self.context.db.get_keepalives(None if is_admin else user_id)
        if not monitors:
            await update.effective_message.reply_html(
                "<b>💓 KEEP-ALIVE MONITORS</b>\n\nNo targets yet. Add one with:\n"
                "<code>/keepalive add https://your-site.example/health 5</code>\n\n"
                "Checks run at the interval you choose. You'll get an alert when a target goes down or recovers."
            )
            return

        lines = ["<b>💓 KEEP-ALIVE MONITORS</b>", ""]
        for monitor in monitors[:20]:
            status = "⏸ paused" if not monitor["enabled"] else {"up": "🟢 up", "down": "🔴 down", "pending": "🟡 pending"}.get(monitor["status"], "⚪ unknown")
            last_check = monitor["last_checked_at"] or "not checked yet"
            lines.append(
                f"{status} <b>{escape(monitor['name'])}</b> <code>{escape(monitor['monitor_id'])}</code>\n"
                f"{escape(monitor['url'])}\n"
                f"Every {int(monitor['interval_seconds']) // 60} min · last check: <code>{escape(last_check[:19])}</code>"
            )
        lines.extend(["", "Use <code>/keepalive help</code> for commands."])
        await update.effective_message.reply_html("\n\n".join(lines))

    async def _add_monitor(self, update: Update, user_id: int, args: list[str]) -> None:
        if self.context is None:
            return
        if not args:
            await update.effective_message.reply_text("Usage: /keepalive add <https://url/health> [minutes]")
            return
        raw_url = args[0]
        try:
            url = validate_monitor_url(raw_url)
            interval_minutes = int(args[1]) if len(args) > 1 else 5
        except (ValueError, TypeError) as exc:
            await update.effective_message.reply_text(f"Invalid monitor details: {exc}")
            return
        interval_seconds = interval_minutes * 60
        if interval_seconds < MIN_INTERVAL_SECONDS or interval_seconds > MAX_INTERVAL_SECONDS:
            await update.effective_message.reply_text("Choose an interval between 1 and 1440 minutes.")
            return

        hostname = urlsplit(url).hostname or "endpoint"
        monitor = self.context.db.add_keepalive(user_id, hostname, url, interval_seconds)
        await update.effective_message.reply_html(
            f"✅ Added <b>{escape(hostname)}</b>\nID: <code>{escape(monitor['monitor_id'])}</code>\n"
            f"Checking every {interval_minutes} minute(s). I'll alert you if it goes down or recovers."
        )
        asyncio.create_task(self._check_one(monitor, notify=True))

    async def _operate(self, update: Update, user_id: int, action: str, monitor_id: str) -> None:
        if self.context is None:
            return
        monitor = self.context.db.get_keepalive(monitor_id)
        if monitor is None or not self._allowed(monitor, user_id):
            await update.effective_message.reply_text("Monitor not found or access denied.")
            return

        if action == "remove":
            self.context.db.delete_keepalive(monitor_id)
            await update.effective_message.reply_text(f"🗑 Removed keep-alive monitor {monitor_id}.")
        elif action in {"pause", "resume"}:
            enabled = action == "resume"
            self.context.db.update_keepalive(monitor_id, enabled=int(enabled), status="pending" if enabled else "paused", last_checked_at=None if enabled else monitor["last_checked_at"])
            await update.effective_message.reply_text(f"{'▶️ Resumed' if enabled else '⏸ Paused'} monitor {monitor_id}.")
            if enabled:
                asyncio.create_task(self._check_one(self.context.db.get_keepalive(monitor_id), notify=True))
        else:
            if not monitor["enabled"]:
                await update.effective_message.reply_text("This monitor is paused. Resume it before checking.")
                return
            result = await self._check_one(monitor, notify=False)
            icon = "🟢" if result[0] else "🔴"
            status = f"HTTP {result[1]}" if result[1] else (result[3] or "request failed")
            await update.effective_message.reply_text(f"{icon} {monitor['name']}: {status} · {result[2]} ms")

    async def _check_one(self, monitor: dict, notify: bool = True):
        if self.context is None or monitor is None:
            return False, None, 0, "Monitor unavailable"
        monitor_id = monitor["monitor_id"]
        if monitor_id in self._in_flight:
            return False, None, 0, "Check already in progress"

        self._in_flight.add(monitor_id)
        try:
            old_status = monitor["status"]
            result = await asyncio.to_thread(
                _probe_url,
                monitor["url"],
                self.context.config.KEEPALIVE_TIMEOUT_SECONDS,
            )
            ok, status_code, latency_ms, error = result
            next_status = "up" if ok else "down"
            self.context.db.update_keepalive(
                monitor_id,
                status=next_status,
                last_checked_at=datetime.now().isoformat(),
                last_status_code=status_code,
                last_latency_ms=latency_ms,
                consecutive_failures=0 if ok else int(monitor["consecutive_failures"]) + 1,
                success_count=int(monitor["success_count"]) + (1 if ok else 0),
                failure_count=int(monitor["failure_count"]) + (0 if ok else 1),
                last_error=error,
            )
            transitioned = (not ok and old_status != "down") or (ok and old_status == "down")
            if notify and transitioned:
                label = "recovered" if ok else "is down"
                detail = f"HTTP {status_code}" if status_code else (error or "request failed")
                try:
                    await self.application.bot.send_message(
                        chat_id=monitor["owner_id"],
                        text=f"{'🟢' if ok else '🔴'} Keep-alive alert: {escape(monitor['name'])} {label} ({escape(detail)}).",
                        parse_mode="HTML",
                    )
                except Exception:
                    # Telegram send failures should not interrupt monitor checks.
                    pass
            return result
        finally:
            self._in_flight.discard(monitor_id)

    async def check_due_monitors(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.context is None or self._sweep_lock.locked():
            return
        async with self._sweep_lock:
            now = datetime.now()
            due: list[dict] = []
            for monitor in self.context.db.get_keepalives():
                if not monitor["enabled"]:
                    continue
                last_checked = monitor["last_checked_at"]
                is_due = not last_checked
                if last_checked:
                    try:
                        is_due = now - datetime.fromisoformat(last_checked) >= timedelta(seconds=int(monitor["interval_seconds"]))
                    except ValueError:
                        is_due = True
                if is_due:
                    due.append(monitor)

            semaphore = asyncio.Semaphore(10)

            async def bounded_check(monitor: dict) -> None:
                async with semaphore:
                    await self._check_one(monitor)

            await asyncio.gather(*(bounded_check(monitor) for monitor in due))

    async def health(self) -> tuple[bool, str]:
        if self.context is None:
            return False, "keep-alive monitor not initialized"
        count = len(self.context.db.get_keepalives())
        return True, f"scheduler ready · {count} monitors"

    @staticmethod
    def _help_text() -> str:
        return (
            "💓 Keep-alive commands:\n"
            "/keepalive — list monitors\n"
            "/keepalive add <https://url/health> [minutes]\n"
            "/keepalive check <id> — run now\n"
            "/keepalive pause <id> — stop scheduled checks\n"
            "/keepalive resume <id> — restart checks\n"
            "/keepalive remove <id> — delete monitor\n\n"
            "Intervals are 1–1440 minutes. Only public HTTP(S) targets are allowed."
        )


def create_plugin() -> BotPlugin:
    return KeepAlivePlugin()
