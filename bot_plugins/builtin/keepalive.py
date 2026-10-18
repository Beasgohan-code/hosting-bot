"""Public HTTP keep-alive checks with Telegram alerts and per-user ownership."""

from __future__ import annotations

import asyncio
import concurrent.futures
import http.client
import ipaddress
import logging
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlsplit, urlunsplit

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot_plugins.core import BotPlugin, PluginContext, PluginSpec

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60
MAX_MONITORS_PER_USER = 20
MAX_MONITORS_TOTAL = 500
DNS_LOOKUP_TIMEOUT_SECONDS = 5
logger = logging.getLogger("UltimateHost.keepalive")
_dns_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="keepalive-dns")
_BLOCKED_IPV4_RANGES = tuple(ipaddress.ip_network(cidr) for cidr in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.31.196.0/24",
    "192.52.193.0/24", "192.88.99.0/24", "192.168.0.0/16", "192.175.48.0/24",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))
_BLOCKED_IPV6_RANGES = tuple(ipaddress.ip_network(cidr) for cidr in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3ffe::/16", "3fff::/20",
))
_IPV6_GLOBAL_UNICAST_RANGE = ipaddress.ip_network("2000::/3")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float):
        super().__init__(hostname, port, timeout=timeout)
        self.pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.pinned_address, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: float):
        super().__init__(hostname, port, timeout=timeout, context=ssl.create_default_context())
        self.pinned_address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.pinned_address, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _canonical_monitor_url(parsed) -> str:
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(hostname)
        hostname = ip.compressed.lower()
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("The target hostname is invalid.") from exc
    authority = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", "", ""))


def _is_public_ip_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return not any(ip in network for network in _BLOCKED_IPV4_RANGES)
    return ip in _IPV6_GLOBAL_UNICAST_RANGE and not any(ip in network for network in _BLOCKED_IPV6_RANGES)


def _resolve_public_addresses(parsed) -> list[str]:
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("The target hostname is invalid.") from exc
    try:
        addresses = [str(ipaddress.ip_address(hostname))]
    except ValueError:
        future = _dns_executor.submit(socket.getaddrinfo, hostname, parsed.port, 0, socket.SOCK_STREAM)
        try:
            records = future.result(timeout=DNS_LOOKUP_TIMEOUT_SECONDS)
            addresses = list(dict.fromkeys(record[4][0] for record in records))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ValueError("The target hostname lookup timed out.") from exc
        except OSError as exc:
            raise ValueError("The target hostname could not be resolved.") from exc

    if not addresses:
        raise ValueError("The target hostname could not be resolved.")
    for address in addresses:
        try:
            public = _is_public_ip_address(address)
        except ValueError as exc:
            raise ValueError("The target hostname resolved to an invalid address.") from exc
        if not public:
            raise ValueError("Private and reserved IP addresses are not allowed.")
    return addresses


def validate_monitor_url(raw_url: str, resolve: bool = False) -> str:
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        raise ValueError("The target URL is too long or invalid.")
    try:
        parsed = urlsplit(raw_url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The target URL or port is invalid.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("Use a complete HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    if port == 0:
        raise ValueError("The target port is invalid.")
    if parsed.query or parsed.fragment:
        raise ValueError("Query strings and fragments are not supported for safety.")

    normalized_hostname = hostname.lower().rstrip(".")
    if "%" in normalized_hostname:
        raise ValueError("IPv6 zone identifiers are not allowed.")
    if normalized_hostname == "localhost" or normalized_hostname.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Private and local network targets are not allowed.")

    try:
        literal = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        literal = None
    if literal is not None and not _is_public_ip_address(str(literal)):
        raise ValueError("Private and reserved IP addresses are not allowed.")
    elif resolve:
        _resolve_public_addresses(parsed)

    return _canonical_monitor_url(parsed)


def _request_pinned(parsed, address: str, timeout: float) -> int:
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    connection = connection_type(hostname, port, address, timeout)
    try:
        connection.request(
            "GET",
            parsed.path or "/",
            headers={
                "Host": parsed.netloc,
                "User-Agent": "HostingBot-KeepAlive/1.0",
                "Accept": "*/*",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        status_code = response.status
        response.close()
        return status_code
    finally:
        connection.close()


def _probe_url(raw_url: str, timeout: int) -> tuple[bool, int | None, int, str | None]:
    started = time.monotonic()
    try:
        url = validate_monitor_url(raw_url)
        parsed = urlsplit(url)
        addresses = _resolve_public_addresses(parsed)
        deadline = started + timeout
        last_error: Exception | None = None
        for address in addresses:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                status_code = _request_pinned(parsed, address, remaining)
                latency_ms = round((time.monotonic() - started) * 1000)
                ok = 200 <= status_code < 300
                error = None if ok else f"Redirect response is not followed (HTTP {status_code})." if 300 <= status_code < 400 else f"HTTP {status_code}"
                return ok, status_code, latency_ms, error
            except Exception as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    break
        raise last_error or TimeoutError("Monitor request timed out.")
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
        self._add_lock = asyncio.Lock()
        self._in_flight: set[str] = set()
        self._background_tasks: set[asyncio.Task] = set()

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

    async def on_shutdown(self, application, context: PluginContext) -> None:
        pending = list(self._background_tasks)
        if not pending:
            return
        _, still_running = await asyncio.wait(pending, timeout=15)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    def _spawn_check(self, monitor: dict | None) -> None:
        if monitor is None:
            return
        task = asyncio.create_task(self._check_one(monitor, notify=True))
        self._background_tasks.add(task)

        def on_done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error("Background monitor check failed", exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(on_done)

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
        if len(args) > 2:
            await update.effective_message.reply_text("Usage: /keepalive add <https://url/health> [minutes]")
            return
        user_monitors = self.context.db.get_keepalives(user_id)
        if len(user_monitors) >= MAX_MONITORS_PER_USER:
            await update.effective_message.reply_text(f"You can have at most {MAX_MONITORS_PER_USER} keep-alive monitors.")
            return
        if len(self.context.db.get_keepalives()) >= MAX_MONITORS_TOTAL:
            await update.effective_message.reply_text("The worker has reached its global keep-alive monitor capacity.")
            return

        raw_url = args[0]
        try:
            interval_minutes = int(args[1]) if len(args) > 1 else 5
        except (ValueError, TypeError) as exc:
            await update.effective_message.reply_text(f"Invalid monitor interval: {exc}")
            return
        interval_seconds = interval_minutes * 60
        if interval_seconds < MIN_INTERVAL_SECONDS or interval_seconds > MAX_INTERVAL_SECONDS:
            await update.effective_message.reply_text("Choose an interval between 1 and 1440 minutes.")
            return
        try:
            url = await asyncio.to_thread(validate_monitor_url, raw_url, True)
        except (ValueError, TypeError) as exc:
            await update.effective_message.reply_text(f"Invalid monitor URL: {exc}")
            return
        hostname = urlsplit(url).hostname or "endpoint"
        async with self._add_lock:
            user_monitors = self.context.db.get_keepalives(user_id)
            all_monitors = self.context.db.get_keepalives()
            if len(user_monitors) >= MAX_MONITORS_PER_USER or len(all_monitors) >= MAX_MONITORS_TOTAL:
                await update.effective_message.reply_text("The keep-alive monitor capacity has been reached.")
                return
            if any(existing["url"] == url for existing in user_monitors):
                await update.effective_message.reply_text("You already have a monitor for this URL.")
                return
            monitor = self.context.db.add_keepalive(user_id, hostname, url, interval_seconds)
        await update.effective_message.reply_html(
            f"✅ Added <b>{escape(hostname)}</b>\nID: <code>{escape(monitor['monitor_id'])}</code>\n"
            f"Checking every {interval_minutes} minute(s). I'll alert you if it goes down or recovers."
        )
        self._spawn_check(monitor)

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
                self._spawn_check(self.context.db.get_keepalive(monitor_id))
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
            result = await asyncio.to_thread(
                _probe_url,
                monitor["url"],
                self.context.config.KEEPALIVE_TIMEOUT_SECONDS,
            )
            ok, status_code, latency_ms, error = result
            current = self.context.db.get_keepalive(monitor_id)
            if current is None:
                return result

            enabled = bool(current["enabled"])
            old_status = current["status"]
            next_status = ("up" if ok else "down") if enabled else "paused"
            self.context.db.update_keepalive(
                monitor_id,
                status=next_status,
                last_checked_at=datetime.now(timezone.utc).isoformat(),
                last_status_code=status_code,
                last_latency_ms=latency_ms,
                consecutive_failures=0 if ok else int(current["consecutive_failures"]) + 1,
                success_count=int(current["success_count"]) + (1 if ok else 0),
                failure_count=int(current["failure_count"]) + (0 if ok else 1),
                last_error=error,
            )
            transitioned = enabled and ((not ok and old_status != "down") or (ok and old_status == "down"))
            if notify and transitioned:
                label = "recovered" if ok else "is down"
                detail = f"HTTP {status_code}" if status_code else (error or "request failed")
                try:
                    await self.application.bot.send_message(
                        chat_id=current["owner_id"],
                        text=f"{'🟢' if ok else '🔴'} Keep-alive alert: {escape(current['name'])} {label} ({escape(detail)}).",
                        parse_mode="HTML",
                    )
                except Exception:
                    # Alert delivery must not interrupt the monitor state transition.
                    logger.exception("Telegram keep-alive alert delivery failed for %s", monitor_id)
            return result
        finally:
            self._in_flight.discard(monitor_id)

    async def check_due_monitors(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.context is None or self._sweep_lock.locked():
            return
        async with self._sweep_lock:
            now = datetime.now(timezone.utc)
            due: list[dict] = []
            for monitor in self.context.db.get_keepalives():
                if not monitor["enabled"]:
                    continue
                last_checked = monitor["last_checked_at"]
                is_due = not last_checked
                if last_checked:
                    try:
                        checked_at = datetime.fromisoformat(last_checked)
                        if checked_at.tzinfo is None:
                            checked_at = checked_at.replace(tzinfo=timezone.utc)
                        is_due = now - checked_at >= timedelta(seconds=int(monitor["interval_seconds"]))
                    except (TypeError, ValueError):
                        is_due = True
                if is_due:
                    due.append(monitor)

            semaphore = asyncio.Semaphore(10)

            async def bounded_check(monitor: dict) -> None:
                async with semaphore:
                    try:
                        await self._check_one(monitor)
                    except Exception:
                        logger.exception("Scheduled keep-alive check failed for %s", monitor["monitor_id"])

            await asyncio.gather(*(bounded_check(monitor) for monitor in due))

    async def health(self) -> tuple[bool, str]:
        if self.context is None:
            return False, "keep-alive monitor not initialized"
        return True, "scheduler ready"

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
