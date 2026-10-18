#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           🤖 ULTIMATE TELEGRAM BOT HOSTING BOT v3.1              ║
║              Termux / VPS / Web-Ready Edition                    ║
╠══════════════════════════════════════════════════════════════════╣
║  Fixes: Auto-creates dirs, no /opt required, works everywhere    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import time
import asyncio
import sqlite3
import logging
import re
import shutil
import signal
import stat
import subprocess
import uuid
import zipfile
from urllib.parse import urlsplit
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from enum import Enum
from collections import defaultdict, deque
from types import SimpleNamespace

from bot_plugins.core import PluginManager

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, InputFile
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION — works on Termux, VPS, Replit, anywhere
# ═══════════════════════════════════════════════════════════════════

class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: List[int] = [int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip().isdigit()]

    # Auto-detect base dir: uses ./hosting_data in same folder as script
    BASE_DIR: Path = Path(os.getenv("HOSTING_DATA_DIR", str(Path(__file__).parent / "hosting_data"))).expanduser().resolve()
    LOGS_DIR: Path = BASE_DIR / "logs"
    REPOS_DIR: Path = BASE_DIR / "repos"
    BACKUPS_DIR: Path = BASE_DIR / "backups"
    DB_PATH: Path = BASE_DIR / "hosting.db"

    MAX_BOTS_PER_USER: int = 5
    AUTO_HEAL_INTERVAL: int = max(5, int(os.getenv("AUTO_HEAL_INTERVAL", "15")))
    AUTO_HEAL_MAX_RESTARTS: int = max(1, int(os.getenv("AUTO_HEAL_MAX_RESTARTS", "5")))
    AUTO_HEAL_WINDOW_SECONDS: int = max(60, int(os.getenv("AUTO_HEAL_WINDOW_SECONDS", "600")))
    KEEPALIVE_SWEEP_INTERVAL: int = max(5, int(os.getenv("KEEPALIVE_SWEEP_INTERVAL", "15")))
    KEEPALIVE_TIMEOUT_SECONDS: int = max(2, int(os.getenv("KEEPALIVE_TIMEOUT_SECONDS", "10")))
    HEALTH_HOST: str = os.getenv("HEALTH_HOST", "0.0.0.0")
    HEALTH_PORT: int = max(0, int(os.getenv("HEALTH_PORT", "8788")))
    LOG_TAIL_LINES: int = 50
    DEPLOY_TIMEOUT: int = 120

    @classmethod
    def init_dirs(cls):
        for directory in (cls.BASE_DIR, cls.LOGS_DIR, cls.REPOS_DIR, cls.BACKUPS_DIR):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass

# Create dirs FIRST before logging tries to write there
Config.init_dirs()
MAX_PROCESS_LOG_BYTES = 10 * 1024 * 1024
PROCESS_LOG_BACKUPS = 3
MAX_BACKUP_INPUT_BYTES = 100 * 1024 * 1024
MAX_BACKUP_FILES = 20_000
MAX_BACKUPS_PER_BOT = 5
MAX_BACKUP_TELEGRAM_BYTES = 20 * 1024 * 1024

# ═══════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(Config.BASE_DIR / "hosting_master.log"))
    ]
)
logger = logging.getLogger("UltimateHost")


class WorkerHealthHandler(BaseHTTPRequestHandler):
    """Minimal dependency-free liveness/readiness endpoint for containers."""

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route not in {"/healthz", "/livez", "/readyz"}:
            self.send_error(404)
            return

        ready = bool(getattr(self.server, "ready", False))
        payload = {
            "service": "hosting-bot-telegram-worker",
            "ok": True,
            "ready": ready,
            "uptimeSeconds": max(0, int(time.monotonic() - self.server.started_monotonic)),
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        status = 503 if route == "/readyz" and not ready else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug("health endpoint: %s", format % args)


class WorkerHealthServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.server = None
        self.thread: Optional[Thread] = None

    def start(self):
        if self.port == 0:
            logger.info("Worker health endpoint disabled")
            return
        self.server = ThreadingHTTPServer((self.host, self.port), WorkerHealthHandler)
        self.server.daemon_threads = True
        self.server.ready = False
        self.server.started_monotonic = time.monotonic()
        self.thread = Thread(target=self.server.serve_forever, name="worker-health-http", daemon=True)
        self.thread.start()
        logger.info("Worker health endpoint listening on %s:%s", self.host, self.port)

    def set_ready(self, ready: bool):
        if self.server is not None:
            self.server.ready = ready

    def stop(self):
        if self.server is None:
            return
        self.server.ready = False
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.server = None
        self.thread = None

# ═══════════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ═══════════════════════════════════════════════════════════════════

class BotStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DEPLOYING = "deploying"
    MAINTENANCE = "maintenance"
    RESTARTING = "restarting"

class BotMode(Enum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"
    MAINTENANCE = "maintenance"

@dataclass
class HostedBot:
    bot_id: str
    name: str
    owner_id: int
    github_url: str
    status: str
    mode: str
    created_at: str
    updated_at: str
    pid: Optional[int] = None
    log_file: Optional[str] = None
    env_vars: str = "{}"
    resource_usage: str = "{}"
    last_error: Optional[str] = None
    restart_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: tuple) -> "HostedBot":
        keys = ['bot_id', 'name', 'owner_id', 'github_url', 'status', 'mode',
                'created_at', 'updated_at', 'pid', 'log_file', 'env_vars',
                'resource_usage', 'last_error', 'restart_count']
        return cls(**dict(zip(keys, row)))

# ═══════════════════════════════════════════════════════════════════
# DATABASE LAYER
# ═══════════════════════════════════════════════════════════════════

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(str(Config.DB_PATH), check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        try:
            Config.DB_PATH.chmod(0o600)
        except OSError:
            pass
        self._init_tables()

    def _init_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                bot_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                github_url TEXT NOT NULL,
                status TEXT DEFAULT 'stopped',
                mode TEXT DEFAULT 'production',
                created_at TEXT,
                updated_at TEXT,
                pid INTEGER,
                log_file TEXT,
                env_vars TEXT DEFAULT '{}',
                resource_usage TEXT DEFAULT '{}',
                last_error TEXT,
                restart_count INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT,
                user_id INTEGER,
                action TEXT,
                timestamp TEXT,
                details TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keepalive_monitors (
                monitor_id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL DEFAULT 300,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_status_code INTEGER,
                last_latency_ms INTEGER,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS keepalive_owner_idx ON keepalive_monitors(owner_id)")
        self.conn.commit()

    def add_bot(self, bot: HostedBot):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO bots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bot.bot_id, bot.name, bot.owner_id, bot.github_url, bot.status,
            bot.mode, bot.created_at, bot.updated_at, bot.pid, bot.log_file,
            bot.env_vars, bot.resource_usage, bot.last_error, bot.restart_count
        ))
        self.conn.commit()
        if bot.status == BotStatus.RUNNING.value:
            self._audit(bot.bot_id, bot.owner_id, "CREATE", f"Deployed {bot.name}")
        else:
            self._audit(bot.bot_id, bot.owner_id, "DEPLOY_FAILED", f"Deployment failed for {bot.name}")

    def update_bot(self, bot_id: str, **kwargs):
        allowed = {
            "name", "owner_id", "github_url", "status", "mode", "pid", "log_file",
            "env_vars", "resource_usage", "last_error", "restart_count",
        }
        changes = {key: value for key, value in kwargs.items() if key in allowed}
        if not changes:
            return
        sets = ", ".join([f"{key}=?" for key in changes])
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE bots SET {sets}, updated_at=? WHERE bot_id=?",
            [*changes.values(), datetime.now().isoformat(), bot_id],
        )
        self.conn.commit()

    def get_bot(self, bot_id: str) -> Optional[HostedBot]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,))
        row = cursor.fetchone()
        return HostedBot.from_row(row) if row else None

    def get_user_bots(self, user_id: int) -> List[HostedBot]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM bots WHERE owner_id=?", (user_id,))
        return [HostedBot.from_row(r) for r in cursor.fetchall()]

    def delete_bot(self, bot_id: str):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM bots WHERE bot_id=?", (bot_id,))
        self.conn.commit()

    def get_all_bots(self) -> List[HostedBot]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM bots")
        return [HostedBot.from_row(r) for r in cursor.fetchall()]

    def get_stats(self) -> dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*), status FROM bots GROUP BY status")
        status_counts = {r[1]: r[0] for r in cursor.fetchall()}
        cursor.execute("SELECT COUNT(DISTINCT owner_id) FROM bots")
        total_users = cursor.fetchone()[0] or 0
        return {
            "total": sum(status_counts.values()),
            "running": status_counts.get("running", 0),
            "stopped": status_counts.get("stopped", 0),
            "error": status_counts.get("error", 0),
            "users": total_users
        }

    def add_keepalive(self, owner_id: int, name: str, url: str, interval_seconds: int) -> dict:
        monitor_id = f"ka_{uuid.uuid4().hex[:8]}"
        created_at = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO keepalive_monitors
                (monitor_id, owner_id, name, url, interval_seconds, enabled, status, created_at)
            VALUES (?, ?, ?, ?, ?, 1, 'pending', ?)
        """, (monitor_id, owner_id, name, url, interval_seconds, created_at))
        self.conn.commit()
        self._audit(monitor_id, owner_id, "KEEPALIVE_CREATE", f"Added monitor {name}")
        return self.get_keepalive(monitor_id)

    def get_keepalive(self, monitor_id: str) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM keepalive_monitors WHERE monitor_id=?", (monitor_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        keys = [item[0] for item in cursor.description]
        return dict(zip(keys, row))

    def get_keepalives(self, owner_id: Optional[int] = None) -> List[dict]:
        cursor = self.conn.cursor()
        if owner_id is None:
            cursor.execute("SELECT * FROM keepalive_monitors ORDER BY created_at DESC")
        else:
            cursor.execute("SELECT * FROM keepalive_monitors WHERE owner_id=? ORDER BY created_at DESC", (owner_id,))
        keys = [item[0] for item in cursor.description]
        return [dict(zip(keys, row)) for row in cursor.fetchall()]

    def update_keepalive(self, monitor_id: str, **kwargs):
        allowed = {
            "name", "url", "interval_seconds", "enabled", "status", "last_checked_at",
            "last_status_code", "last_latency_ms", "consecutive_failures", "success_count",
            "failure_count", "last_error",
        }
        changes = {key: value for key, value in kwargs.items() if key in allowed}
        if not changes:
            return
        assignments = ", ".join(f"{key}=?" for key in changes)
        self.conn.execute(
            f"UPDATE keepalive_monitors SET {assignments} WHERE monitor_id=?",
            [*changes.values(), monitor_id],
        )
        self.conn.commit()

    def delete_keepalive(self, monitor_id: str) -> bool:
        monitor = self.get_keepalive(monitor_id)
        if not monitor:
            return False
        self.conn.execute("DELETE FROM keepalive_monitors WHERE monitor_id=?", (monitor_id,))
        self.conn.commit()
        self._audit(monitor_id, monitor["owner_id"], "KEEPALIVE_DELETE", f"Removed monitor {monitor['name']}")
        return True

    def _audit(self, bot_id: str, user_id: int, action: str, details: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO audit_log (bot_id, user_id, action, timestamp, details)
            VALUES (?, ?, ?, ?, ?)
        """, (bot_id, user_id, action, datetime.now().isoformat(), details))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

db = Database()


def can_manage_bot(bot: Optional[HostedBot], user_id: int) -> bool:
    return bot is not None and (bot.owner_id == user_id or user_id in Config.ADMIN_IDS)


def validate_github_repo_url(raw_url: str) -> str:
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        raise ValueError("GitHub repository URL is invalid or too long.")
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("GitHub repository URL is invalid.") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower().rstrip(".") != "github.com"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
    ):
        raise ValueError("Use a public HTTPS GitHub repository URL without credentials, query strings, or fragments.")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Use a GitHub repository URL in the form https://github.com/owner/repository.")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    name_pattern = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
    if not name_pattern.fullmatch(owner) or not name_pattern.fullmatch(repository) or repository in {".", ".."}:
        raise ValueError("The GitHub owner or repository name is invalid.")
    return f"https://github.com/{owner}/{repository}.git"


def _validate_deployment_env(raw: str, password: str = "") -> dict[str, str]:
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Stored environment variables are invalid JSON.") from exc
    if not isinstance(parsed, dict) or len(parsed) > 100:
        raise ValueError("Environment must be an object with no more than 100 variables.")

    env: dict[str, str] = {}
    total_bytes = 0
    for key, value in parsed.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key):
            raise ValueError("Environment variable names must use letters, digits, and underscores.")
        if key in {"HOME", "PATH", "BOT_ID"}:
            raise ValueError(f"Environment variable {key} is reserved by the worker.")
        if not isinstance(value, str) or any(char in value for char in ("\x00", "\n", "\r")):
            raise ValueError(f"Environment variable {key} must be a single-line string.")
        if len(value.encode("utf-8")) > 8192:
            raise ValueError(f"Environment variable {key} exceeds the 8 KB limit.")
        env[key] = value
        total_bytes += len(key) + len(value.encode("utf-8"))
    if password:
        if any(char in password for char in ("\x00", "\n", "\r")) or len(password.encode("utf-8")) > 8192:
            raise ValueError("Deployment password must be a single-line value no longer than 8 KB.")
        env["DEPLOY_PASSWORD"] = password
        total_bytes += len("DEPLOY_PASSWORD") + len(password.encode("utf-8"))
    if total_bytes > 64 * 1024:
        raise ValueError("Combined environment variables exceed the 64 KB limit.")
    return env


def _minimal_child_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    return {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INPUT": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


async def _read_capped_stream(stream, limit: int = 1024 * 1024) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            del output[:len(output) - limit]


async def _terminate_command_group(process) -> None:
    pgid = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 2
    while group_exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if group_exists():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _run_bounded_command(command: list[str], cwd: Path, env: dict[str, str], timeout: int):
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_task = asyncio.create_task(_read_capped_stream(process.stdout))
    stderr_task = asyncio.create_task(_read_capped_stream(process.stderr))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        stdout, stderr = await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await _terminate_command_group(process)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    return process.returncode, stdout, stderr


# ═══════════════════════════════════════════════════════════════════
# PROCESS MANAGER
# ═══════════════════════════════════════════════════════════════════

class ProcessManager:
    def __init__(self):
        self.processes: Dict[str, subprocess.Popen] = {}
        self.log_threads: Dict[str, Thread] = {}
        self._lock = asyncio.Lock()
        self._deploying: set[str] = set()
        self.auto_heal_attempts: Dict[str, deque[float]] = defaultdict(deque)

    def begin_auto_heal(self, bot_id: str) -> tuple[bool, int]:
        now = time.monotonic()
        attempts = self.auto_heal_attempts[bot_id]
        while attempts and now - attempts[0] > Config.AUTO_HEAL_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= Config.AUTO_HEAL_MAX_RESTARTS:
            return False, len(attempts)
        attempts.append(now)
        return True, len(attempts)

    @staticmethod
    def _safe_bot_id(bot_id: str) -> bool:
        return bool(re.fullmatch(r"bot_[A-Za-z0-9_-]{3,80}", bot_id))

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    async def _terminate_process_group(self, pgid: int, process: Optional[subprocess.Popen] = None) -> bool:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("Could not signal process group %s: %s", pgid, exc)
            return False

        deadline = time.monotonic() + 2
        while self._group_exists(pgid) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self._group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning("Could not force-stop process group %s: %s", pgid, exc)
                return False

        if process is not None:
            try:
                await asyncio.to_thread(process.wait, timeout=3)
            except subprocess.TimeoutExpired:
                return False
        return not self._group_exists(pgid)

    def _verified_persisted_process(self, bot: HostedBot) -> bool:
        if not bot.pid or bot.pid <= 1 or not self._safe_bot_id(bot.bot_id):
            return False
        try:
            if os.getpgid(bot.pid) != bot.pid:
                return False
            expected_dir = (Config.REPOS_DIR / bot.bot_id).resolve()
            current_dir = Path(f"/proc/{bot.pid}/cwd").resolve(strict=True)
            command_line = Path(f"/proc/{bot.pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
            return current_dir == expected_dir and f"{expected_dir}/" in command_line and ".py" in command_line
        except (OSError, ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _rotate_process_log(log_file: Path) -> None:
        if log_file.is_symlink():
            log_file.unlink()
        for index in range(PROCESS_LOG_BACKUPS, 0, -1):
            source = log_file if index == 1 else Path(f"{log_file}.{index - 1}")
            destination = Path(f"{log_file}.{index}")
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if source.is_symlink():
                source.unlink()
            elif source.is_file():
                os.replace(source, destination)

    def _stream_process_log(self, bot_id: str, process: subprocess.Popen, log_file: Path) -> None:
        stream = process.stdout
        if stream is None:
            return
        handle = None
        size = 0
        try:
            if log_file.is_symlink():
                log_file.unlink()
            if log_file.exists() and log_file.stat().st_size >= MAX_PROCESS_LOG_BYTES:
                self._rotate_process_log(log_file)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(log_file, flags, 0o600)
            handle = os.fdopen(descriptor, "ab", buffering=0)
            size = log_file.stat().st_size
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if handle is None:
                    continue
                try:
                    if size and size + len(chunk) > MAX_PROCESS_LOG_BYTES:
                        handle.close()
                        self._rotate_process_log(log_file)
                        descriptor = os.open(log_file, flags, 0o600)
                        handle = os.fdopen(descriptor, "ab", buffering=0)
                        size = 0
                    handle.write(chunk)
                    size += len(chunk)
                except OSError:
                    logger.exception("Could not write logs for %s; continuing to drain output", bot_id)
                    if handle:
                        handle.close()
                        handle = None
        except OSError:
            logger.exception("Could not open logs for %s; continuing to drain output", bot_id)
            while stream.read(8192):
                pass
        finally:
            if handle is not None:
                handle.close()
            stream.close()

    async def _join_log_thread(self, bot_id: str) -> None:
        thread = self.log_threads.pop(bot_id, None)
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 3)

    async def deploy(self, bot: HostedBot, password: str = "") -> tuple[bool, str]:
        if not self._safe_bot_id(bot.bot_id):
            return False, "Invalid service identifier."
        try:
            repo_url = validate_github_repo_url(bot.github_url)
            env_dict = _validate_deployment_env(bot.env_vars, password)
        except ValueError as exc:
            return False, str(exc)

        stale_process: Optional[subprocess.Popen] = None
        async with self._lock:
            if bot.bot_id in self._deploying:
                return False, "A deployment is already in progress for this service."
            current_process = self.processes.get(bot.bot_id)
            if current_process and current_process.poll() is None:
                return False, "This service is already running. Stop or restart it first."
            stale_process = current_process
            self._deploying.add(bot.bot_id)

        bot_dir = Config.REPOS_DIR / bot.bot_id
        log_file = Config.LOGS_DIR / f"{bot.bot_id}.log"
        bot.log_file = str(log_file)
        staging_dir = Config.REPOS_DIR / f".{bot.bot_id}.stage-{uuid.uuid4().hex[:10]}"
        build_home = Config.REPOS_DIR / f".{bot.bot_id}.home-{uuid.uuid4().hex[:10]}"
        backup_dir = Config.REPOS_DIR / f".{bot.bot_id}.backup-{uuid.uuid4().hex[:10]}"
        moved_old_repo = False
        installed_new_repo = False
        successful = False
        launch_process: Optional[subprocess.Popen] = None

        try:
            if stale_process is not None:
                if not await self._terminate_process_group(stale_process.pid, stale_process):
                    return False, "A previous process group could not be stopped safely."
                await self._join_log_thread(bot.bot_id)
                async with self._lock:
                    if self.processes.get(bot.bot_id) is stale_process:
                        self.processes.pop(bot.bot_id, None)
            if bot.pid and self._verified_persisted_process(bot):
                return False, "This service is already running. Stop or restart it first."
            Config.REPOS_DIR.mkdir(parents=True, exist_ok=True)
            Config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

            build_env = _minimal_child_environment(build_home)
            code, stdout, stderr = await _run_bounded_command(
                ["git", "clone", "--depth", "1", "--no-tags", "--", repo_url, str(staging_dir)],
                Config.REPOS_DIR,
                build_env,
                timeout=60,
            )
            if code != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace")[-2000:]
                return False, f"Git clone failed:\n{detail or 'Git exited unsuccessfully.'}"

            env_path = staging_dir / ".env"
            if env_path.is_symlink():
                env_path.unlink()
            elif env_path.exists() and not env_path.is_file():
                return False, "The repository contains an invalid .env path."
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(env_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
                for key, value in env_dict.items():
                    env_file.write(f"{key}={value}\n")
            try:
                env_path.chmod(0o600)
            except OSError:
                pass

            venv_dir = staging_dir / ".venv"
            if venv_dir.is_symlink():
                venv_dir.unlink()
            elif venv_dir.exists():
                shutil.rmtree(venv_dir)
            code, _stdout, stderr = await _run_bounded_command(
                [sys.executable, "-m", "venv", str(venv_dir)],
                staging_dir,
                build_env,
                timeout=30,
            )
            if code != 0:
                return False, f"Python environment setup failed:\n{stderr.decode('utf-8', errors='replace')[-2000:]}"

            req_file = staging_dir / "requirements.txt"
            if req_file.is_symlink():
                return False, "Symlinked requirements.txt files are not allowed."
            if req_file.is_file():
                code, _stdout, stderr = await _run_bounded_command(
                    [str(venv_dir / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--requirement", str(req_file)],
                    staging_dir,
                    build_env,
                    timeout=Config.DEPLOY_TIMEOUT,
                )
                if code != 0:
                    return False, f"Dependency installation failed:\n{stderr.decode('utf-8', errors='replace')[-2000:]}"

            entry_point = next((staging_dir / name for name in ("main.py", "bot.py", "app.py", "run.py") if (staging_dir / name).is_file() and not (staging_dir / name).is_symlink()), None)
            if entry_point is None:
                py_files = sorted(file for file in staging_dir.glob("*.py") if file.is_file() and not file.is_symlink())
                if len(py_files) != 1:
                    return False, "Add a main.py entry point, or include exactly one root-level Python file."
                entry_point = py_files[0]
            entry_relative = entry_point.relative_to(staging_dir)

            if bot_dir.is_symlink():
                bot_dir.unlink()
            elif bot_dir.exists():
                os.replace(bot_dir, backup_dir)
                moved_old_repo = True
            os.replace(staging_dir, bot_dir)
            installed_new_repo = True

            bot_home_root = Config.BASE_DIR / "bot_homes"
            if bot_home_root.is_symlink():
                return False, "The service home directory is not safe to use."
            bot_home_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            bot_home = bot_home_root / bot.bot_id
            if bot_home.is_symlink() or (bot_home.exists() and not bot_home.is_dir()):
                bot_home.unlink()
            run_env = _minimal_child_environment(bot_home)
            run_env.update(env_dict)
            run_env["BOT_ID"] = bot.bot_id
            run_env["HOME"] = str(bot_home)
            run_env["PATH"] = "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin"
            launch_process = subprocess.Popen(
                [str(bot_dir / ".venv" / "bin" / "python"), str(bot_dir / entry_relative)],
                cwd=str(bot_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=run_env,
                start_new_session=True,
                close_fds=True,
                bufsize=0,
            )
            log_thread = Thread(
                target=self._stream_process_log,
                args=(bot.bot_id, launch_process, log_file),
                name=f"service-log-{bot.bot_id}",
                daemon=True,
            )
            self.log_threads[bot.bot_id] = log_thread
            log_thread.start()
            async with self._lock:
                self.processes[bot.bot_id] = launch_process

            await asyncio.sleep(3)
            async with self._lock:
                still_registered = self.processes.get(bot.bot_id) is launch_process
            if not still_registered:
                return False, "The deployment was stopped before startup completed."
            if launch_process.poll() is not None:
                return False, "The service exited during startup. Check its logs."

            bot.github_url = repo_url
            bot.pid = launch_process.pid
            bot.log_file = str(log_file)
            bot.status = BotStatus.RUNNING.value
            bot.last_error = None
            successful = True
            return True, f"Deployed!\nPID: {launch_process.pid}\nEntry: {entry_relative.name}"

        except asyncio.TimeoutError:
            return False, f"Deployment timed out (limit {Config.DEPLOY_TIMEOUT}s)."
        except Exception as exc:
            logger.exception("Deployment failed for %s", bot.bot_id)
            return False, f"Deployment error: {str(exc)[:500]}"
        finally:
            if not successful:
                if launch_process is not None:
                    await self._terminate_process_group(launch_process.pid, launch_process)
                    await self._join_log_thread(bot.bot_id)
                    async with self._lock:
                        if self.processes.get(bot.bot_id) is launch_process:
                            self.processes.pop(bot.bot_id, None)
                if installed_new_repo and (bot_dir.exists() or bot_dir.is_symlink()):
                    try:
                        if bot_dir.is_symlink():
                            bot_dir.unlink()
                        else:
                            shutil.rmtree(bot_dir)
                    except OSError:
                        logger.exception("Could not remove failed deployment directory for %s", bot.bot_id)
                if moved_old_repo and backup_dir.exists():
                    try:
                        os.replace(backup_dir, bot_dir)
                    except OSError:
                        logger.exception("Could not restore prior deployment for %s", bot.bot_id)
            elif backup_dir.exists():
                try:
                    shutil.rmtree(backup_dir)
                except OSError:
                    logger.warning("Could not remove prior deployment backup for %s", bot.bot_id)

            if staging_dir.exists() or staging_dir.is_symlink():
                try:
                    if staging_dir.is_symlink():
                        staging_dir.unlink()
                    else:
                        shutil.rmtree(staging_dir)
                except OSError:
                    logger.warning("Could not remove deployment staging directory for %s", bot.bot_id)
            if build_home.exists() or build_home.is_symlink():
                try:
                    if build_home.is_symlink():
                        build_home.unlink()
                    else:
                        shutil.rmtree(build_home)
                except OSError:
                    logger.warning("Could not remove temporary build home for %s", bot.bot_id)
            async with self._lock:
                self._deploying.discard(bot.bot_id)

    async def refresh_checkout(self, bot: HostedBot) -> tuple[bool, str]:
        if not self._safe_bot_id(bot.bot_id):
            return False, "Invalid service identifier."
        try:
            repo_url = validate_github_repo_url(bot.github_url)
            env_dict = _validate_deployment_env(bot.env_vars)
        except ValueError as exc:
            return False, str(exc)

        stale_process: Optional[subprocess.Popen] = None
        async with self._lock:
            if bot.bot_id in self._deploying:
                return False, "A deployment is already in progress for this service."
            current_process = self.processes.get(bot.bot_id)
            if current_process and current_process.poll() is None:
                return False, "Stop the service before refreshing its checkout."
            stale_process = current_process
            self._deploying.add(bot.bot_id)

        bot_dir = Config.REPOS_DIR / bot.bot_id
        staging_dir = Config.REPOS_DIR / f".{bot.bot_id}.refresh-{uuid.uuid4().hex[:10]}"
        build_home = Config.REPOS_DIR / f".{bot.bot_id}.refresh-home-{uuid.uuid4().hex[:10]}"
        backup_dir = Config.REPOS_DIR / f".{bot.bot_id}.refresh-backup-{uuid.uuid4().hex[:10]}"
        moved_old_repo = False
        installed_new_repo = False
        successful = False
        try:
            if stale_process is not None:
                if not await self._terminate_process_group(stale_process.pid, stale_process):
                    return False, "The previous process group could not be stopped safely."
                await self._join_log_thread(bot.bot_id)
                async with self._lock:
                    if self.processes.get(bot.bot_id) is stale_process:
                        self.processes.pop(bot.bot_id, None)
            if bot.pid and self._verified_persisted_process(bot):
                return False, "Stop the service before refreshing its checkout."
            Config.REPOS_DIR.mkdir(parents=True, exist_ok=True)
            build_env = _minimal_child_environment(build_home)
            code, stdout, stderr = await _run_bounded_command(
                ["git", "clone", "--depth", "1", "--no-tags", "--", repo_url, str(staging_dir)],
                Config.REPOS_DIR,
                build_env,
                timeout=60,
            )
            if code != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace")[-2000:]
                return False, f"Git clone failed: {detail or 'git exited unsuccessfully.'}"

            env_path = staging_dir / ".env"
            if env_path.is_symlink():
                env_path.unlink()
            elif env_path.exists() and not env_path.is_file():
                return False, "The repository contains an invalid .env path."
            descriptor = os.open(
                env_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
                for key, value in env_dict.items():
                    env_file.write(f"{key}={value}\n")
            try:
                env_path.chmod(0o600)
            except OSError:
                pass

            if bot_dir.is_symlink():
                bot_dir.unlink()
            elif bot_dir.exists():
                os.replace(bot_dir, backup_dir)
                moved_old_repo = True
            os.replace(staging_dir, bot_dir)
            installed_new_repo = True
            bot.github_url = repo_url
            successful = True
            return True, "Repository refreshed from GitHub. The service remains stopped."
        except asyncio.TimeoutError:
            return False, "Repository refresh timed out."
        except Exception as exc:
            logger.exception("Repository refresh failed for %s", bot.bot_id)
            return False, f"Repository refresh failed: {str(exc)[:400]}"
        finally:
            if not successful:
                if installed_new_repo and (bot_dir.exists() or bot_dir.is_symlink()):
                    try:
                        if bot_dir.is_symlink():
                            bot_dir.unlink()
                        else:
                            shutil.rmtree(bot_dir)
                    except OSError:
                        logger.exception("Could not remove failed checkout for %s", bot.bot_id)
                if moved_old_repo and backup_dir.exists():
                    try:
                        os.replace(backup_dir, bot_dir)
                    except OSError:
                        logger.exception("Could not restore checkout for %s", bot.bot_id)
            elif backup_dir.exists():
                try:
                    shutil.rmtree(backup_dir)
                except OSError:
                    logger.warning("Could not remove prior checkout for %s", bot.bot_id)
            for temporary in (staging_dir, build_home):
                if temporary.exists() or temporary.is_symlink():
                    try:
                        if temporary.is_symlink() or not temporary.is_dir():
                            temporary.unlink()
                        else:
                            shutil.rmtree(temporary)
                    except OSError:
                        logger.warning("Could not clean temporary path for %s", bot.bot_id)
            async with self._lock:
                self._deploying.discard(bot.bot_id)

    async def stop(self, bot_id: str) -> bool:
        if not self._safe_bot_id(bot_id):
            return False
        async with self._lock:
            process = self.processes.get(bot_id)

        if process is None:
            bot = db.get_bot(bot_id)
            if bot is None or not bot.pid:
                return True
            if not self._verified_persisted_process(bot):
                try:
                    os.kill(bot.pid, 0)
                except OSError:
                    return True
                logger.warning("Refusing to signal unverified PID %s for %s", bot.pid, bot_id)
                return False
            pid = bot.pid
        else:
            pid = process.pid

        try:
            stopped = await self._terminate_process_group(pid, process)
            if not stopped:
                logger.error("Process group %s did not stop cleanly for %s", pid, bot_id)
            return stopped
        except Exception:
            logger.exception("Stop error for %s", bot_id)
            return False
        finally:
            async with self._lock:
                self.processes.pop(bot_id, None)
            await self._join_log_thread(bot_id)

    async def stop_all(self) -> None:
        """Stop children at worker shutdown but retain desired running state for recovery."""
        for bot in db.get_all_bots():
            if bot.pid and bot.status == BotStatus.RUNNING.value:
                await self.stop(bot.bot_id)

    async def restart(self, bot: HostedBot, password: str = "") -> tuple[bool, str]:
        if not await self.stop(bot.bot_id):
            return False, "The existing process could not be stopped safely; no replacement was started."
        bot.pid = None
        return await self.deploy(bot, password)

    def is_alive(self, bot_id: str) -> bool:
        process = self.processes.get(bot_id)
        if process:
            return process.poll() is None
        bot = db.get_bot(bot_id)
        return bool(bot and self._verified_persisted_process(bot))

process_mgr = ProcessManager()

# ═══════════════════════════════════════════════════════════════════
# LOG STREAMER
# ═══════════════════════════════════════════════════════════════════

class LogStreamer:
    def __init__(self):
        self._watchers: Dict[str, asyncio.Task] = {}

    async def tail(self, bot_id: str, lines: int = 50) -> str:
        bot = db.get_bot(bot_id)
        if not bot or not bot.log_file or not re.fullmatch(r"bot_[A-Za-z0-9_-]{3,80}", bot_id):
            return "📭 No logs available."

        expected_path = (Config.LOGS_DIR / f"{bot_id}.log").resolve()
        try:
            log_path = Path(bot.log_file)
            if log_path.is_symlink() or log_path.resolve() != expected_path:
                return "❌ The stored log path is not safe to read."
            if not log_path.is_file():
                return "📭 Log file not found."
            line_count = max(1, min(int(lines), 200))

            def read_tail() -> str:
                with log_path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    start = max(0, size - 64 * 1024)
                    handle.seek(start)
                    data = handle.read(64 * 1024)
                if start:
                    data = data.split(b"\n", 1)[-1]
                return b"\n".join(data.splitlines()[-line_count:]).decode("utf-8", errors="replace")[-3800:]

            return await asyncio.to_thread(read_tail)
        except Exception as exc:
            logger.warning("Could not read logs for %s: %s", bot_id, exc)
            return "❌ Log read error."

    def format_log_message(self, bot_id: str, content: str, live: bool = False) -> str:
        bot = db.get_bot(bot_id)
        status_emoji = "🟢" if bot and bot.status == "running" else "🔴"
        mode_str = escape(bot.mode.upper() if bot else "UNKNOWN", quote=False)
        name = escape(bot.name if bot else bot_id, quote=False)
        content = "".join(char for char in content if char in "\n\r\t" or ord(char) >= 32)
        max_content = 3400
        if len(content) > max_content:
            content = "...[truncated]...\n" + content[-max_content:]
        line_count = len(content.splitlines())
        safe_content = escape(content, quote=False)

        header = (
            f"{status_emoji} <b>📜 LIVE LOGS: {name}</b>\n"
            f"<i>Mode: <code>{mode_str}</code> | "
            f"{'🔴 LIVE STREAM' if live else '📦 Snapshot'} | "
            f"Lines: {line_count}</i>\n\n"
        )
        return header + f"<pre><code class='language-bash'>{safe_content}</code></pre>"

log_streamer = LogStreamer()

# ═══════════════════════════════════════════════════════════════════
# UI FACTORY
# ═══════════════════════════════════════════════════════════════════

class UI:
    @staticmethod
    def blockquote(text: str, expandable: bool = True) -> str:
        tag = "blockquote expandable" if expandable else "blockquote"
        return f"<{tag}>{escape(text, quote=False)}</{tag.split()[0]}>"

    @staticmethod
    def code_block(text: str, lang: str = "bash") -> str:
        safe_lang = lang if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", lang) else "text"
        return f"<pre><code class='language-{safe_lang}'>{escape(text, quote=False)}</code></pre>"

    @staticmethod
    def main_menu(user_id: int) -> InlineKeyboardMarkup:
        is_admin = user_id in Config.ADMIN_IDS
        buttons = [
            [InlineKeyboardButton("🚀 DEPLOY NEW BOT", callback_data="nav_deploy")],
            [InlineKeyboardButton("📋 MY BOTS", callback_data="nav_list"),
             InlineKeyboardButton("📊 STATUS", callback_data="nav_status")],
            [InlineKeyboardButton("💓 KEEP-ALIVE", callback_data="nav_keepalive")],
            [InlineKeyboardButton("⚙️ SETTINGS", callback_data="nav_settings"),
             InlineKeyboardButton("❓ HELP", callback_data="nav_help")],
        ]
        if is_admin:
            buttons.append([InlineKeyboardButton("🔴 ADMIN PANEL", callback_data="nav_admin")])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def bot_card(bot: HostedBot) -> str:
        emoji_map = {
            "running": "🟢", "stopped": "⏹", "error": "🔴",
            "deploying": "🟡", "maintenance": "🔧", "restarting": "🔄"
        }
        e = emoji_map.get(bot.status, "⚪")
        try:
            res = json.loads(bot.resource_usage) if bot.resource_usage else {}
            cpu = res.get("cpu", "N/A")
            mem = res.get("mem", "N/A")
        except:
            cpu, mem = "N/A", "N/A"

        return (
            f"{e} <b>{escape(bot.name, quote=False)}</b> <code>[{escape(bot.mode.upper(), quote=False)}]</code>\n"
            f"├ ID: <code>{escape(bot.bot_id, quote=False)}</code>\n"
            f"├ Status: <b>{escape(bot.status.upper(), quote=False)}</b>\n"
            f"├ PID: <code>{bot.pid or 'None'}</code>\n"
            f"├ Restarts: <code>{bot.restart_count}</code>\n"
            f"├ CPU: {escape(str(cpu), quote=False)} | MEM: {escape(str(mem), quote=False)}\n"
            f"└ Created: <i>{escape(str(bot.created_at[:19]), quote=False)}</i>\n"
        )

    @staticmethod
    def bot_actions(bot_id: str, status: str) -> InlineKeyboardMarkup:
        running = status == "running"
        buttons = [
            [
                InlineKeyboardButton("⏹ STOP" if running else "▶️ START", 
                                   callback_data=f"act_stop_{bot_id}" if running else f"act_start_{bot_id}"),
                InlineKeyboardButton("🔄 RESTART", callback_data=f"act_restart_{bot_id}")
            ],
            [
                InlineKeyboardButton("📜 LOGS", callback_data=f"act_logs_{bot_id}"),
                InlineKeyboardButton("🔴 LIVE LOGS", callback_data=f"act_livelogs_{bot_id}")
            ],
            [
                InlineKeyboardButton("⚙️ MODE", callback_data=f"act_mode_{bot_id}"),
                InlineKeyboardButton("🔧 ENV VARS", callback_data=f"act_env_{bot_id}")
            ],
            [
                InlineKeyboardButton("🔄 GIT PULL", callback_data=f"act_pull_{bot_id}"),
                InlineKeyboardButton("💾 BACKUP", callback_data=f"act_backup_{bot_id}")
            ],
            [
                InlineKeyboardButton("🗑 DELETE BOT", callback_data=f"act_delete_{bot_id}")
            ],
            [InlineKeyboardButton("🔙 BACK TO LIST", callback_data="nav_list")]
        ]
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def mode_selector(bot_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 PRODUCTION (Auto-Heal)", callback_data=f"mode_prod_{bot_id}")],
            [InlineKeyboardButton("🟡 DEVELOPMENT (Debug)", callback_data=f"mode_dev_{bot_id}")],
            [InlineKeyboardButton("🔧 MAINTENANCE (Offline)", callback_data=f"mode_maint_{bot_id}")],
            [InlineKeyboardButton("🔙 BACK", callback_data=f"nav_manage_{bot_id}")]
        ])

    @staticmethod
    def confirm_delete(bot_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ YES, DELETE", callback_data=f"del_confirm_{bot_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"nav_manage_{bot_id}")]
        ])

    @staticmethod
    def admin_panel() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")],
            [InlineKeyboardButton("🧹 CLEANUP LOGS", callback_data="admin_cleanup")],
            [InlineKeyboardButton("🔄 RESTART ALL", callback_data="admin_restart_all")],
            [InlineKeyboardButton("📊 SYSTEM INFO", callback_data="admin_sysinfo")],
            [InlineKeyboardButton("🔙 BACK", callback_data="nav_main")]
        ])

    @staticmethod
    def log_actions(bot_id: str, live: bool = False) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton("🔄 REFRESH", callback_data=f"act_logs_{bot_id}")],
            [InlineKeyboardButton("🔴 LIVE STREAM", callback_data=f"act_livelogs_{bot_id}")],
            [InlineKeyboardButton("⬇️ DOWNLOAD FULL", callback_data=f"act_logdl_{bot_id}")],
            [InlineKeyboardButton("🔙 BACK", callback_data=f"nav_manage_{bot_id}")]
        ]
        if live:
            buttons[1] = [InlineKeyboardButton("⏹ STOP LIVE", callback_data=f"act_logs_{bot_id}")]
        return InlineKeyboardMarkup(buttons)

ui = UI()


def redact_sensitive_log_text(text: str) -> str:
    sensitive_markers = (
        "token", "secret", "password", "passwd", "credential", "api_key",
        "access_key", "private_key", "authorization", "cookie", "database_url",
        "db_url", "mongo_uri", "redis_url", "dsn",
    )
    redacted_lines = []
    for original_line in text.splitlines():
        line = original_line
        matched_secret = False
        for separator in ("=", ":"):
            pieces = line.split(separator)
            for index in range(len(pieces) - 1):
                key = separator.join(pieces[:index + 1]).strip().lower().replace("-", "_").replace(" ", "_")
                if any(marker in key for marker in sensitive_markers):
                    line = separator.join(pieces[:index + 1]) + separator + "[REDACTED]"
                    matched_secret = True
                    break
            if matched_secret:
                break

        lowered = line.lower()
        search_from = 0
        while True:
            bearer_index = lowered.find("bearer ", search_from)
            if bearer_index < 0:
                break
            value_start = bearer_index + len("bearer ")
            value_end = value_start
            while value_end < len(line) and not line[value_end].isspace() and line[value_end] not in ",;":
                value_end += 1
            line = line[:value_start] + "[REDACTED]" + line[value_end:]
            lowered = line.lower()
            search_from = value_start + len("[REDACTED]")

        for part in line.split():
            candidate = part.strip(".,;()[]{}<>\"'")
            if ":" not in candidate:
                continue
            prefix, suffix = candidate.split(":", 1)
            if prefix.isdigit() and 6 <= len(prefix) <= 12 and len(suffix) >= 20 and all(char.isalnum() or char in "-_" for char in suffix):
                line = line.replace(candidate, "[REDACTED_TELEGRAM_TOKEN]")
        redacted_lines.append("".join(char for char in line if ord(char) >= 32 or char in (chr(9), chr(10), chr(13))))
    return chr(10).join(redacted_lines)


async def notify_admins_about_bot_failure(
    context: ContextTypes.DEFAULT_TYPE,
    bot: HostedBot,
    title: str,
    reason: str,
) -> None:
    if not Config.ADMIN_IDS:
        logger.error("%s for %s; no ADMIN_IDS are configured", title, bot.bot_id)
        return

    try:
        logs = await log_streamer.tail(bot.bot_id, 50)
    except Exception:
        logger.exception("Could not collect failure logs for %s", bot.bot_id)
        logs = "Log collection failed. Check the worker volume manually."
    logs = redact_sensitive_log_text(logs)
    reason = "".join(char for char in reason[:400] if ord(char) >= 32 or char in (chr(9), chr(10), chr(13)))
    newline = chr(10)

    def build_message(log_text: str, reason_text: str) -> str:
        return (
            f"🚨 <b>{escape(title, quote=False)}</b>{newline}{newline}"
            f"Service: <b>{escape(bot.name, quote=False)}</b>{newline}"
            f"ID: <code>{escape(bot.bot_id, quote=False)}</code> · Owner: <code>{bot.owner_id}</code>{newline}"
            f"Reason: <code>{escape(reason_text, quote=False)}</code>{newline}{newline}"
            f"<b>Recent logs</b>{newline}{ui.code_block(log_text, 'text')}"
        )

    message = build_message(logs, reason)
    if len(message) > 3800:
        low, high = 0, len(logs)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = build_message(logs[-middle:] if middle else "", reason)
            if len(candidate) <= 3800:
                low = middle
            else:
                high = middle - 1
        clipped_logs = logs[-low:] if low else ""
        if low < len(logs):
            clipped_logs = "...[truncated]..." + newline + clipped_logs if clipped_logs else "...[truncated]..."
        message = build_message(clipped_logs, reason)

    for admin_id in Config.ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.warning("Could not DM admin %s about %s", admin_id, bot.bot_id, exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# CONVERSATION STATES
# ═══════════════════════════════════════════════════════════════════

(
    ST_IDLE, ST_DEPLOY_URL, ST_DEPLOY_NAME, ST_DEPLOY_PASS,
    ST_DEPLOY_ENV, ST_DEPLOY_CONFIRM, ST_ENV_EDIT, ST_BROADCAST
) = range(8)

# ═══════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db._audit("SYSTEM", user.id, "START", f"User {user.username} started bot")

    welcome = (
        f"<b>🤖 ULTIMATE HOSTING BOT v3.1</b>\n\n"
        f"Welcome, {user.mention_html()}!\n\n"
        f"{ui.blockquote('Deploy and manage trusted GitHub bot workers with process-group controls, crash-loop protection, and endpoint health monitoring.')}\n\n"
        f"<b>🚀 Quick Start:</b>\n"
        f"1. Click <b>DEPLOY NEW BOT</b>\n"
        f"2. Paste your GitHub URL\n"
        f"3. Inspect its logs and health from Telegram\n\n"
        f"<b>⚡ Operations:</b>\n"
        f"• Live log streaming with auto-refresh\n"
        f"• Bounded auto-heal with restart circuit breaker\n"
        f"• Scheduled HTTP keep-alive checks and alerts\n"
        f"• Production, development, and maintenance modes\n"
        f"• Resource monitoring and one-click backups\n"
        f"• Use <code>/keepalive help</code> to monitor a URL"
    )

    await update.message.reply_html(welcome, reply_markup=ui.main_menu(user.id))

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if not args:
        user_bots = db.get_user_bots(user_id)
        if not user_bots:
            await update.message.reply_text("You have no bots. Deploy one first!")
            return
        buttons = [[InlineKeyboardButton(b.name, callback_data=f"act_logs_{b.bot_id}")] 
                   for b in user_bots]
        buttons.append([InlineKeyboardButton("🔙 Menu", callback_data="nav_main")])
        await update.message.reply_text("Select bot:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    bot_id = args[0]
    try:
        lines = int(args[1]) if len(args) > 1 and args[1].isdigit() else Config.LOG_TAIL_LINES
    except ValueError:
        lines = Config.LOG_TAIL_LINES
    lines = max(1, min(lines, 200))

    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await update.message.reply_text("❌ Bot not found or access denied.")
        return

    logs = await log_streamer.tail(bot_id, lines)
    text = log_streamer.format_log_message(bot_id, logs)

    await update.message.reply_html(text, reply_markup=ui.log_actions(bot_id))

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ui.main_menu(update.effective_user.id))
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════════════

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "nav_main":
        await query.edit_message_text(
            "<b>🤖 ULTIMATE HOSTING BOT</b>\n\nMain Menu:",
            parse_mode="HTML",
            reply_markup=ui.main_menu(user_id)
        )

    elif data == "nav_deploy":
        example_quote = ui.blockquote("Example: https://github.com/username/mybot\n\nThe repo must contain a main.py or bot.py and optionally requirements.txt")
        await query.edit_message_text(
            f"<b>🚀 Deploy New Bot</b>\n\n"
            f"Step 1/4: Send me the <b>GitHub Repository URL</b>\n\n"
            f"{example_quote}\n\n"
            "Type /cancel to abort.",
            parse_mode="HTML"
        )
        return ST_DEPLOY_URL

    elif data == "nav_keepalive":
        await query.edit_message_text(
            "<b>💓 KEEP-ALIVE MONITORS</b>\n\n"
            "Monitor public health endpoints on a schedule and receive an alert when they go down or recover.\n\n"
            "<code>/keepalive add https://your-site.example/health 5</code>\n"
            "<code>/keepalive</code> lists your targets. Use <code>/keepalive help</code> for pause, resume, check, and remove commands.",
            parse_mode="HTML",
            reply_markup=ui.main_menu(user_id),
        )

    elif data == "nav_list":
        await show_bot_list(query, user_id)

    elif data == "nav_status":
        await show_status(query)

    elif data == "nav_settings":
        await query.edit_message_text(
            f"<b>⚙️ Global Settings</b>\n\n"
            f"Max bots per user: <code>{Config.MAX_BOTS_PER_USER}</code>\n"
            f"Auto-heal interval: <code>{Config.AUTO_HEAL_INTERVAL}s</code>\n"
            f"Log tail lines: <code>{Config.LOG_TAIL_LINES}</code>\n\n"
            f"{ui.blockquote('Settings are applied globally to all hosted bots.')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="nav_main")
            ]])
        )

    elif data == "nav_help":
        await query.edit_message_text(
            f"<b>❓ Help Center</b>\n\n"
            f"<b>Commands:</b>\n"
            f"/start - Main menu\n"
            f"/logs [bot_id] [lines] - View logs\n"
            f"/cancel - Cancel operation\n\n"
            f"<b>Modes:</b>\n"
            f"🟢 <b>Production</b> - Auto-restart, optimized\n"
            f"🟡 <b>Development</b> - Debug mode, verbose\n"
            f"🔧 <b>Maintenance</b> - Bot offline\n\n"
            f"{ui.blockquote('Need help? Contact the admin.')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="nav_main")
            ]])
        )

    elif data == "nav_admin":
        if user_id not in Config.ADMIN_IDS:
            await query.answer("⛔ Admin only!", show_alert=True)
            return

        stats = db.get_stats()
        text = (
            f"<b>🔴 ADMIN PANEL</b>\n\n"
            f"Total Bots: <code>{stats['total']}</code>\n"
            f"Running: <code>{stats['running']}</code>\n"
            f"Stopped: <code>{stats['stopped']}</code>\n"
            f"Errors: <code>{stats['error']}</code>\n"
            f"Unique Users: <code>{stats['users']}</code>\n\n"
            f"{ui.blockquote('Use with caution. Actions affect all users.')}"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=ui.admin_panel())

    elif data.startswith("nav_manage_"):
        bot_id = data.split("_", 2)[2]
        await show_bot_manage(query, bot_id, user_id)

    elif data.startswith("act_start_"):
        bot_id = data.split("_", 2)[2]
        await handle_start(query, bot_id, user_id, context)

    elif data.startswith("act_stop_"):
        bot_id = data.split("_", 2)[2]
        await handle_stop(query, bot_id, user_id)

    elif data.startswith("act_restart_"):
        bot_id = data.split("_", 2)[2]
        await handle_restart(query, bot_id, user_id, context)

    elif data.startswith("act_logs_"):
        bot_id = data.split("_", 2)[2]
        await handle_logs(query, bot_id, user_id)

    elif data.startswith("act_livelogs_"):
        bot_id = data.split("_", 2)[2]
        await handle_live_logs(query, bot_id, user_id, context)

    elif data.startswith("act_logdl_"):
        bot_id = data.split("_", 2)[2]
        await handle_log_download(query, bot_id, user_id, context)

    elif data.startswith("act_mode_"):
        bot_id = data.split("_", 2)[2]
        bot = db.get_bot(bot_id)
        if not can_manage_bot(bot, user_id):
            await query.answer("Service not found or access denied.", show_alert=True)
            return
        mode_help = ui.blockquote("Production = Auto-heal + Optimized\nDevelopment = Verbose + No auto-restart\nMaintenance = Offline mode")
        await query.edit_message_text(
            f"<b>⚙️ Change Mode: {bot.name}</b>\n\n"
            f"Current: <code>{bot.mode.upper()}</code>\n\n"
            f"{mode_help}",
            parse_mode="HTML",
            reply_markup=ui.mode_selector(bot_id)
        )

    elif data.startswith("act_env_"):
        bot_id = data.split("_", 2)[2]
        bot = db.get_bot(bot_id)
        if not can_manage_bot(bot, user_id):
            await query.answer("Service not found or access denied.", show_alert=True)
            return
        context.user_data["env_bot_id"] = bot_id
        try:
            current_env = json.loads(bot.env_vars) if bot.env_vars else {}
        except (TypeError, json.JSONDecodeError):
            current_env = {}
        if not isinstance(current_env, dict):
            current_env = {}
        env_text = "\n".join([f"{k}=••••••" for k in current_env]) or "None set"

        await query.edit_message_text(
            f"<b>🔧 Edit Environment Variables</b>\n\n"
            f"Bot: <code>{escape(bot.name, quote=False)}</code>\n\n"
            f"Current keys (values hidden):\n{ui.code_block(env_text, 'env')}\n\n"
            f"Send the complete replacement as:\n<code>KEY=value\nKEY2=value2</code>\n"
            f"Secret values are hidden; re-enter any secrets you want to keep.\n\n"
            f"Or send 'clear' to remove all.\n/cancel to abort.",
            parse_mode="HTML"
        )
        return ST_ENV_EDIT

    elif data.startswith("act_pull_"):
        bot_id = data.split("_", 2)[2]
        await handle_git_pull(query, bot_id, user_id, context)

    elif data.startswith("act_backup_"):
        bot_id = data.split("_", 2)[2]
        await handle_backup(query, bot_id, user_id, context)

    elif data.startswith("act_delete_"):
        bot_id = data.split("_", 2)[2]
        bot = db.get_bot(bot_id)
        if not can_manage_bot(bot, user_id):
            await query.answer("Service not found or access denied.", show_alert=True)
            return
        await query.edit_message_text(
            f"⚠️ <b>Delete {bot.name}?</b>\n\n"
            f"This will permanently remove the bot and all its data.\n\n"
            f"{ui.blockquote('This action cannot be undone!')}",
            parse_mode="HTML",
            reply_markup=ui.confirm_delete(bot_id)
        )

    elif data.startswith("del_confirm_"):
        bot_id = data.split("_", 2)[2]
        await handle_delete(query, bot_id, user_id)

    elif data.startswith("mode_prod_"):
        await set_mode(query, data.split("_", 2)[2], "production", user_id)
    elif data.startswith("mode_dev_"):
        await set_mode(query, data.split("_", 2)[2], "development", user_id)
    elif data.startswith("mode_maint_"):
        await set_mode(query, data.split("_", 2)[2], "maintenance", user_id)

    elif data == "admin_broadcast":
        await query.edit_message_text(
            "<b>📢 Broadcast Message</b>\n\nSend the message to broadcast to all users:\n/cancel to abort.",
            parse_mode="HTML"
        )
        return ST_BROADCAST

    elif data == "admin_cleanup":
        await handle_cleanup(query, user_id)

    elif data == "admin_restart_all":
        await handle_restart_all(query, user_id)

    elif data == "admin_sysinfo":
        await show_sysinfo(query)

    return ST_IDLE

# ═══════════════════════════════════════════════════════════════════
# ACTION HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def show_bot_list(query, user_id: int):
    bots = db.get_user_bots(user_id)
    if not bots:
        await query.edit_message_text(
            "📭 You have no hosted bots.\n\nClick Deploy to get started!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Deploy Bot", callback_data="nav_deploy")],
                [InlineKeyboardButton("🔙 Back", callback_data="nav_main")]
            ])
        )
        return

    text = "<b>📋 YOUR HOSTED BOTS</b>\n\n"
    for bot in bots:
        text += ui.bot_card(bot) + "\n"

    buttons = [[InlineKeyboardButton(f"🔧 Manage {b.name}", callback_data=f"nav_manage_{b.bot_id}")] 
               for b in bots]
    buttons.append([InlineKeyboardButton("🔙 Main Menu", callback_data="nav_main")])

    await query.edit_message_text(text, parse_mode="HTML", 
                                  reply_markup=InlineKeyboardMarkup(buttons))

async def show_bot_manage(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await query.answer("⛔ Access denied!", show_alert=True)
        return

    text = (
        f"<b>🔧 MANAGE: {escape(bot.name, quote=False)}</b>\n\n"
        f"{ui.bot_card(bot)}\n"
        f"GitHub: <a href='{escape(bot.github_url, quote=True)}'>Open Repo</a>\n"
        f"Last Error: <code>{escape(bot.last_error or 'None', quote=False)}</code>\n\n"
        f"{ui.blockquote('Select an action below:')}"
    )

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=ui.bot_actions(bot_id, bot.status)
    )

async def handle_start(query, bot_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await query.answer("Denied!", show_alert=True)
        return

    await query.edit_message_text(f"🟡 <b>Starting {bot.name}...</b>", parse_mode="HTML")

    success, msg = await process_mgr.deploy(bot, "")
    if success:
        db.update_bot(
            bot_id,
            status=BotStatus.RUNNING.value,
            pid=bot.pid,
            log_file=bot.log_file,
            last_error=None,
        )
        await query.edit_message_text(
            f"✅ <b>{bot.name} is LIVE!</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Manage", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    else:
        still_running = process_mgr.is_alive(bot_id)
        db.update_bot(
            bot_id,
            status=BotStatus.RUNNING.value if still_running else BotStatus.ERROR.value,
            pid=bot.pid if still_running else None,
            last_error=msg[:200],
        )
        if not still_running:
            await notify_admins_about_bot_failure(context, bot, "Hosted bot failed to start", msg)
        await query.edit_message_text(
            f"❌ <b>Failed to start {escape(bot.name, quote=False)}</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )

async def handle_stop(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        return

    await query.edit_message_text(f"🟡 <b>Stopping {bot.name}...</b>", parse_mode="HTML")
    success = await process_mgr.stop(bot_id)

    if success:
        db.update_bot(bot_id, status=BotStatus.STOPPED.value, pid=None)
        await query.edit_message_text(
            f"⏹ <b>{bot.name} stopped.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Manage", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    else:
        await query.answer("Failed to stop!", show_alert=True)

async def handle_restart(query, bot_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        return

    await query.edit_message_text(f"🔄 <b>Restarting {bot.name}...</b>", parse_mode="HTML")
    db.update_bot(bot_id, status=BotStatus.RESTARTING.value)

    success, msg = await process_mgr.restart(bot)
    if success:
        db.update_bot(
            bot_id,
            status=BotStatus.RUNNING.value,
            pid=bot.pid,
            log_file=bot.log_file,
            restart_count=bot.restart_count + 1,
            last_error=None,
        )
        await query.edit_message_text(
            f"✅ <b>{escape(bot.name, quote=False)} restarted!</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Manage", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    else:
        still_running = process_mgr.is_alive(bot_id)
        db.update_bot(
            bot_id,
            status=BotStatus.RUNNING.value if still_running else BotStatus.ERROR.value,
            pid=bot.pid if still_running else None,
            last_error=msg[:200],
        )
        if not still_running:
            await notify_admins_about_bot_failure(context, bot, "Hosted bot restart failed", msg)
        await query.edit_message_text(
            f"❌ <b>Restart failed</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )

async def handle_logs(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await query.answer("Denied!", show_alert=True)
        return

    logs = await log_streamer.tail(bot_id, Config.LOG_TAIL_LINES)
    text = log_streamer.format_log_message(bot_id, logs, live=False)

    try:
        await query.edit_message_text(text, parse_mode="HTML", 
                                      reply_markup=ui.log_actions(bot_id))
    except Exception:
        await query.answer("Logs unchanged.")

async def handle_live_logs(query, bot_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        return

    logs = await log_streamer.tail(bot_id, 20)
    text = log_streamer.format_log_message(bot_id, logs, live=True)

    msg = await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=ui.log_actions(bot_id, live=True)
    )

    task = asyncio.create_task(
        live_log_stream(context, bot_id, msg.chat_id, msg.message_id, user_id)
    )
    context.chat_data[f"live_{bot_id}"] = task

async def live_log_stream(context: ContextTypes.DEFAULT_TYPE, bot_id: str, 
                          chat_id: int, message_id: int, user_id: int):
    try:
        while True:
            await asyncio.sleep(5)
            bot = db.get_bot(bot_id)
            if not can_manage_bot(bot, user_id):
                break

            logs = await log_streamer.tail(bot_id, 25)
            text = log_streamer.format_log_message(bot_id, logs, live=True)

            try:
                await context.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=ui.log_actions(bot_id, live=True)
                )
            except Exception:
                break
    except asyncio.CancelledError:
        pass

async def handle_log_download(query, bot_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await query.answer("Service not found or access denied.", show_alert=True)
        return
    if not bot.log_file or not re.fullmatch(r"bot_[A-Za-z0-9_-]{3,80}", bot_id):
        await query.answer("No log file!", show_alert=True)
        return
    expected_log = (Config.LOGS_DIR / f"{bot_id}.log").resolve()
    log_path = Path(bot.log_file)
    try:
        if log_path.is_symlink() or log_path.resolve() != expected_log or not log_path.is_file():
            await query.answer("The log file is unavailable or unsafe.", show_alert=True)
            return
        if log_path.stat().st_size > 20 * 1024 * 1024:
            await query.answer("The log file exceeds the 20 MB Telegram upload limit.", show_alert=True)
            return
    except OSError:
        await query.answer("The log file is unavailable.", show_alert=True)
        return

    await query.answer("Sending file...")
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(str(log_path)),
        filename=f"{bot_id}_logs.txt",
        caption=f"📥 Full logs for <b>{escape(bot.name, quote=False)}</b>",
        parse_mode="HTML"
    )

async def set_mode(query, bot_id: str, mode: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        return

    if mode == "maintenance":
        if not await process_mgr.stop(bot_id):
            await query.answer("Could not stop the process safely.", show_alert=True)
            return
        db.update_bot(bot_id, mode=mode, status=BotStatus.MAINTENANCE.value, pid=None)
    else:
        db.update_bot(bot_id, mode=mode)

    await query.answer(f"Mode set to {mode.upper()}!")
    await show_bot_manage(query, bot_id, user_id)

async def handle_git_pull(query, bot_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await query.answer("Service not found or access denied.", show_alert=True)
        return
    try:
        bot.github_url = validate_github_repo_url(bot.github_url)
    except ValueError:
        await query.answer("Stored repository URL is invalid; refusing to update it.", show_alert=True)
        return

    bot_dir = Config.REPOS_DIR / bot_id
    if bot_dir.is_symlink() or not bot_dir.is_dir():
        await query.answer("Repository checkout not found or unsafe.", show_alert=True)
        return

    await query.edit_message_text(f"🔄 <b>Updating {escape(bot.name, quote=False)}...</b>", parse_mode="HTML")
    db.update_bot(bot_id, github_url=bot.github_url)
    was_running = bot.status == BotStatus.RUNNING.value or process_mgr.is_alive(bot_id)

    if was_running:
        success, output = await process_mgr.restart(bot)
        if success:
            db.update_bot(
                bot_id,
                status=BotStatus.RUNNING.value,
                pid=bot.pid,
                log_file=bot.log_file,
                restart_count=bot.restart_count + 1,
                last_error=None,
            )
            result = f"✅ Update and redeploy complete.\n{output}"
        else:
            still_running = process_mgr.is_alive(bot_id)
            db.update_bot(
                bot_id,
                status=BotStatus.RUNNING.value if still_running else BotStatus.ERROR.value,
                pid=bot.pid if still_running else None,
                last_error=output[:200],
            )
            if not still_running:
                await notify_admins_about_bot_failure(context, bot, "Hosted bot update failed", output)
            result = f"❌ Update failed.\n{output}"
    else:
        success, output = await process_mgr.refresh_checkout(bot)
        if success:
            db.update_bot(bot_id, github_url=bot.github_url, last_error=None)
            result = f"✅ Repository updated. The service remains stopped.\n{output}"
        else:
            db.update_bot(bot_id, last_error=output[:200])
            await notify_admins_about_bot_failure(context, bot, "Hosted bot checkout update failed", output)
            result = f"❌ Update failed.\n{output}"

    await query.edit_message_text(
        ui.code_block(result),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
        ]]),
    )

def _prune_bot_backups(bot_id: str) -> None:
    candidates = []
    for path in Config.BACKUPS_DIR.glob(f"{bot_id}_*.zip"):
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_file():
                candidates.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            logger.warning("Could not inspect old backup %s", path, exc_info=True)

    keep_count = max(1, MAX_BACKUPS_PER_BOT)
    candidates.sort()
    for _, _, path in candidates[:-keep_count]:
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not prune old backup %s", path, exc_info=True)


def create_bot_backup(bot_id: str, bot_dir: Path) -> Path:
    if not re.fullmatch(r"bot_[A-Za-z0-9_-]{3,80}", bot_id):
        raise ValueError("Invalid service ID")
    expected_dir = Config.REPOS_DIR / bot_id
    if bot_dir != expected_dir or bot_dir.is_symlink() or not bot_dir.is_dir():
        raise FileNotFoundError("Hosted repository is missing or unsafe")

    root = bot_dir.resolve(strict=True)
    expected_root = expected_dir.resolve(strict=True)
    if root != expected_root:
        raise ValueError("Hosted repository path does not match the service ID")

    backup_root = Config.BACKUPS_DIR
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_root.is_symlink():
        raise ValueError("Backup directory must not be a symbolic link")
    backup_root.chmod(0o700)

    excluded_directories = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    files = []
    planned_size = 0

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, names in os.walk(root, topdown=True, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        resolved_current = current_path.resolve(strict=True)
        if not resolved_current.is_relative_to(root):
            raise ValueError("Repository traversal escaped its root")

        directories[:] = [
            name for name in directories
            if name not in excluded_directories and not (current_path / name).is_symlink()
        ]
        for name in names:
            file_path = current_path / name
            if file_path.is_symlink():
                continue
            resolved_file = file_path.resolve(strict=True)
            if not resolved_file.is_relative_to(root):
                raise ValueError("Repository file resolves outside its root")
            file_info = os.stat(resolved_file, follow_symlinks=False)
            if not stat.S_ISREG(file_info.st_mode):
                continue
            if file_info.st_nlink > 1:
                raise ValueError("Backup refuses hard-linked files for safety")
            planned_size += file_info.st_size
            if planned_size > MAX_BACKUP_INPUT_BYTES:
                raise ValueError(f"Backup exceeds the {MAX_BACKUP_INPUT_BYTES // (1024 * 1024)} MiB uncompressed size limit")
            if len(files) >= MAX_BACKUP_FILES:
                raise ValueError(f"Backup contains more than {MAX_BACKUP_FILES} files")
            files.append((resolved_file, file_path.relative_to(root).as_posix(), file_info))

    timestamp = time.time_ns()
    backup_path = backup_root / f"{bot_id}_{timestamp}_{uuid.uuid4().hex[:8]}.zip"
    temporary_path = backup_root / f".{backup_path.name}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary_path, flags, 0o600)
    total_bytes = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for file_path, arcname, scanned_info in files:
                    file_descriptor = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(file_descriptor, "rb") as source:
                        opened_info = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened_info.st_mode) or (opened_info.st_dev, opened_info.st_ino) != (scanned_info.st_dev, scanned_info.st_ino):
                            raise ValueError("Repository changed while the backup was being created")
                        zip_info = zipfile.ZipInfo(arcname)
                        zip_info.compress_type = zipfile.ZIP_DEFLATED
                        zip_info.external_attr = (stat.S_IMODE(opened_info.st_mode) & 0xFFFF) << 16
                        with archive.open(zip_info, "w") as destination:
                            while True:
                                chunk = source.read(64 * 1024)
                                if not chunk:
                                    break
                                total_bytes += len(chunk)
                                if total_bytes > MAX_BACKUP_INPUT_BYTES:
                                    raise ValueError(f"Backup exceeds the {MAX_BACKUP_INPUT_BYTES // (1024 * 1024)} MiB uncompressed size limit")
                                destination.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, backup_path)
        try:
            backup_path.chmod(0o600)
        except OSError:
            logger.warning("Could not set private permissions on backup %s", backup_path, exc_info=True)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove incomplete backup %s", temporary_path, exc_info=True)
        raise

    _prune_bot_backups(bot_id)
    return backup_path


async def handle_backup(query, bot_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        await query.answer("Service not found or access denied.", show_alert=True)
        return

    bot_dir = Config.REPOS_DIR / bot_id
    try:
        backup_path = await asyncio.to_thread(create_bot_backup, bot_id, bot_dir)
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("Backup creation failed for %s", bot_id)
        await query.answer("Backup failed. Check the worker logs for details.", show_alert=True)
        return

    archive_size = backup_path.stat().st_size
    delivery = "Retained in the worker volume; it exceeds Telegram's 20 MiB upload limit."
    await query.answer("Backup created!")
    if archive_size <= MAX_BACKUP_TELEGRAM_BYTES:
        try:
            await context.bot.send_document(
                chat_id=user_id,
                document=InputFile(str(backup_path)),
                filename=f"{bot_id}_backup.zip",
                caption=f"📦 Backup for <b>{escape(bot.name, quote=False)}</b>",
                parse_mode="HTML",
            )
            delivery = "The ZIP archive was sent to your Telegram DM."
        except Exception:
            logger.warning("Could not deliver backup %s to user %s", backup_path.name, user_id, exc_info=True)
            delivery = "Saved in the worker volume, but Telegram delivery failed."

    await query.edit_message_text(
        f"✅ <b>Backup Created: {escape(bot.name, quote=False)}</b>\n\n"
        f"Size: <code>{archive_size / 1024:.1f} KB</code>\n"
        f"Archive: <code>{escape(backup_path.name, quote=False)}</code>\n"
        f"{escape(delivery, quote=False)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
        ]])
    )


async def handle_delete(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not can_manage_bot(bot, user_id):
        return

    if not await process_mgr.stop(bot_id):
        await query.answer("The process could not be stopped safely; nothing was deleted.", show_alert=True)
        return

    repo_dir = Config.REPOS_DIR / bot_id
    bot_home = Config.BASE_DIR / "bot_homes" / bot_id
    base_log = Config.LOGS_DIR / f"{bot_id}.log"
    log_files = [base_log, *[Path(f"{base_log}.{index}") for index in range(1, PROCESS_LOG_BACKUPS + 1)]]
    backups = list(Config.BACKUPS_DIR.glob(f"{bot_id}_*.zip"))
    try:
        for directory in (repo_dir, bot_home):
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
        for file_path in [*log_files, *backups]:
            if file_path.is_symlink() or file_path.is_file():
                file_path.unlink()
            elif file_path.is_dir():
                shutil.rmtree(file_path)
    except OSError as exc:
        logger.exception("Could not fully remove service data for %s", bot_id)
        await query.answer(f"Could not remove all service data: {str(exc)[:120]}", show_alert=True)
        return

    process_mgr.auto_heal_attempts.pop(bot_id, None)
    db.delete_bot(bot_id)
    db._audit(bot_id, user_id, "DELETE", f"Deleted bot {bot.name}")

    await query.edit_message_text(
        f"🗑 <b>{escape(bot.name, quote=False)} deleted permanently.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 My Bots", callback_data="nav_list")
        ]])
    )

async def show_status(query):
    stats = db.get_stats()
    try:
        uptime = subprocess.run(["uptime", "-p"], capture_output=True, text=True).stdout.strip()
    except:
        uptime = "N/A"
    try:
        disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.strip().split('\n')[1]
    except:
        disk = "N/A"

    text = (
        f"<b>📊 SYSTEM STATUS</b>\n\n"
        f"<b>Bot Statistics:</b>\n"
        f"├ Total Hosted: <code>{stats['total']}</code>\n"
        f"├ 🟢 Running: <code>{stats['running']}</code>\n"
        f"├ ⏹ Stopped: <code>{stats['stopped']}</code>\n"
        f"├ 🔴 Errors: <code>{stats['error']}</code>\n"
        f"└ Users: <code>{stats['users']}</code>\n\n"
        f"<b>Server:</b>\n"
        f"├ Uptime: <code>{uptime}</code>\n"
        f"└ Disk: <code>{disk}</code>\n\n"
        f"{ui.blockquote('Auto-heal monitor is active. Checking every 15 seconds.')}"
    )

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="nav_status"),
            InlineKeyboardButton("🔙 Back", callback_data="nav_main")
        ]])
    )

async def handle_cleanup(query, user_id: int):
    if user_id not in Config.ADMIN_IDS:
        return
    count = 0
    cutoff = time.time() - (7 * 86400)
    for log_file in Config.LOGS_DIR.glob("*.log"):
        if log_file.stat().st_mtime < cutoff:
            log_file.unlink()
            count += 1

    await query.answer(f"Cleaned {count} old logs!")
    await query.edit_message_text(
        f"🧹 <b>Cleanup complete</b>\n\nRemoved <code>{count}</code> expired log file(s).",
        parse_mode="HTML",
        reply_markup=ui.admin_panel(),
    )

async def handle_restart_all(query, user_id: int):
    if user_id not in Config.ADMIN_IDS:
        return

    await query.edit_message_text("🔄 <b>Restarting all production bots...</b>", parse_mode="HTML")
    bots = db.get_all_bots()
    restarted = 0

    for bot in bots:
        if bot.mode == "production" and bot.status in ["running", "error"]:
            success, _ = await process_mgr.restart(bot)
            if success:
                restarted += 1
                db.update_bot(bot.bot_id, status="running")

    await query.edit_message_text(
        f"✅ <b>Mass Restart Complete</b>\n\nRestarted: <code>{restarted}/{len(bots)}</code> bots.",
        parse_mode="HTML",
        reply_markup=ui.admin_panel()
    )

async def show_sysinfo(query):
    if query.from_user.id not in Config.ADMIN_IDS:
        return

    try:
        mem = subprocess.run(["free", "-h"], capture_output=True, text=True).stdout
    except:
        mem = "N/A"
    try:
        cpu = subprocess.run(["top", "-bn1"], capture_output=True, text=True).stdout.split('\n')[2:5]
        cpu_info = '\n'.join(cpu)
    except:
        cpu_info = "N/A"

    text = (
        f"<b>🖥 SYSTEM INFO</b>\n\n"
        f"{ui.code_block(mem, 'bash')}\n\n"
        f"{ui.code_block(cpu_info, 'bash')}"
    )

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=ui.admin_panel()
    )

# ═══════════════════════════════════════════════════════════════════
# CONVERSATION: DEPLOY WIZARD
# ═══════════════════════════════════════════════════════════════════

async def deploy_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_url = update.message.text.strip()

    try:
        url = validate_github_repo_url(raw_url)
    except ValueError:
        await update.message.reply_text("❌ Use a public HTTPS GitHub URL like https://github.com/owner/repository. Try again or /cancel:")
        return ST_DEPLOY_URL

    context.user_data["deploy_url"] = url
    await update.message.reply_html(
        "<b>✅ URL Accepted</b>\n\n"
        "Step 2/4: Send a <b>name</b> for your bot (2-30 chars):"
    )
    return ST_DEPLOY_NAME

async def deploy_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 30 or not name.replace("_", "").isalnum():
        await update.message.reply_text("❌ Invalid name. Use 2-30 alphanumeric chars. Try again:")
        return ST_DEPLOY_NAME

    user_bots = db.get_user_bots(update.effective_user.id)
    if len(user_bots) >= Config.MAX_BOTS_PER_USER:
        await update.message.reply_text(
            f"❌ Max bots limit reached ({Config.MAX_BOTS_PER_USER}).\nDelete one first."
        )
        return ConversationHandler.END

    context.user_data["deploy_name"] = name
    await update.message.reply_html(
        "<b>🔐 Security</b>\n\n"
        "Step 3/4: Send <b>password/secret</b> for your bot's .env\n"
        "Only continue in a private chat. Secrets are stored on the worker and values are hidden when editing.\n"
        "Or send <code>skip</code> if not needed:"
    )
    return ST_DEPLOY_PASS

async def deploy_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    if pwd.lower() != "skip" and (len(pwd.encode("utf-8")) > 8192 or any(char in pwd for char in ("\x00", "\n", "\r"))):
        await update.message.reply_text("Secret must be a single-line value no longer than 8 KB. Try again or send skip.")
        return ST_DEPLOY_PASS
    context.user_data["deploy_password"] = "" if pwd.lower() == "skip" else pwd

    await update.message.reply_html(
        "<b>🌍 Environment</b>\n\n"
        "Step 4/4: Send env vars as:\n"
        "<code>BOT_TOKEN=123\nAPI_KEY=xyz</code>\n\n"
        "Or <code>skip</code>:"
    )
    return ST_DEPLOY_ENV

async def deploy_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    env_vars = {}

    if text.lower() != "skip":
        for line in text.split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                env_vars[k.strip()] = v.strip()

    try:
        _validate_deployment_env(json.dumps(env_vars))
    except ValueError as exc:
        await update.message.reply_text(f"Invalid environment variables: {exc}")
        return ST_DEPLOY_ENV

    context.user_data["deploy_env"] = env_vars

    summary = (
        f"<b>📦 DEPLOY SUMMARY</b>\n\n"
        f"Name: <b>{context.user_data['deploy_name']}</b>\n"
        f"Repo: <code>{context.user_data['deploy_url']}</code>\n"
        f"Password: <code>{'*' * len(context.user_data['deploy_password']) or 'None'}</code>\n"
        f"Env Vars: <code>{len(env_vars)} items</code>\n\n"
        f"Confirm deployment?"
    )

    await update.message.reply_html(
        summary,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ DEPLOY NOW", callback_data="deploy_confirm")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="deploy_cancel")]
        ])
    )
    return ST_DEPLOY_CONFIRM

async def deploy_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "deploy_cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled.", reply_markup=ui.main_menu(user_id))
        return ConversationHandler.END

    bot_id = f"bot_{user_id}_{uuid.uuid4().hex[:12]}"
    deployment_env = dict(context.user_data.get("deploy_env", {}))
    deployment_password = context.user_data.get("deploy_password", "")
    if deployment_password:
        deployment_env["DEPLOY_PASSWORD"] = deployment_password
    new_bot = HostedBot(
        bot_id=bot_id,
        name=context.user_data["deploy_name"],
        owner_id=user_id,
        github_url=context.user_data["deploy_url"],
        status=BotStatus.DEPLOYING.value,
        mode=BotMode.PRODUCTION.value,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        env_vars=json.dumps(deployment_env)
    )

    await query.edit_message_text(
        f"🚀 <b>Deploying {new_bot.name}...</b>\n\n"
        f"{ui.blockquote('Cloning repo → Installing deps → Launching process...')}\n\n"
        f"<i>This takes 30-120 seconds...</i>",
        parse_mode="HTML"
    )

    success, msg = await process_mgr.deploy(new_bot)

    if success:
        try:
            db.add_bot(new_bot)
        except Exception:
            logger.exception("Could not persist successful deployment %s", bot_id)
            await process_mgr.stop(bot_id)
            db.delete_bot(bot_id)
            success = False
            msg = "The service started, but its database record could not be saved; the process was stopped."
    if success:
        await query.edit_message_text(
            f"✅ <b>{new_bot.name} IS LIVE!</b>\n\n"
            f"{ui.code_block(msg)}\n\n"
            f"{ui.blockquote('Your bot is now running in Production mode with auto-heal enabled.')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 View Logs", callback_data=f"act_logs_{bot_id}")],
                [InlineKeyboardButton("🔧 Manage Bot", callback_data=f"nav_manage_{bot_id}")],
                [InlineKeyboardButton("📋 My Bots", callback_data="nav_list")]
            ])
        )
    else:
        new_bot.status = BotStatus.ERROR.value
        new_bot.pid = None
        new_bot.last_error = msg[:200]
        new_bot.log_file = new_bot.log_file or str(Config.LOGS_DIR / f"{bot_id}.log")
        try:
            if db.get_bot(bot_id) is None:
                db.add_bot(new_bot)
            else:
                db.update_bot(bot_id, status=BotStatus.ERROR.value, pid=None, last_error=new_bot.last_error)
        except Exception:
            logger.exception("Could not persist failed deployment %s", bot_id)
        await notify_admins_about_bot_failure(
            context,
            new_bot,
            "Hosted bot deployment failed",
            msg,
        )
        await query.edit_message_text(
            f"❌ <b>Deployment Failed</b>\n\n"
            f"{ui.code_block(msg[:2000])}\n\n"
            f"The attempt is saved when possible; inspect its logs and correct the repository or configuration.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 View Logs", callback_data=f"act_logs_{bot_id}")],
                [InlineKeyboardButton("🔧 Manage Attempt", callback_data=f"nav_manage_{bot_id}")],
                [InlineKeyboardButton("📋 My Bots", callback_data="nav_list")],
            ]),
        )

    context.user_data.clear()
    return ConversationHandler.END

async def env_edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.user_data.get("env_bot_id")
    if not bot_id:
        return ST_IDLE

    bot = db.get_bot(bot_id)
    user_id = update.effective_user.id
    if not can_manage_bot(bot, user_id):
        await update.message.reply_text("❌ Session expired or access denied.")
        return ConversationHandler.END

    text = update.message.text.strip()
    env_vars: dict[str, str] = {}
    if text.lower() != "clear":
        for line in text.splitlines():
            if not line.strip():
                continue
            if "=" not in line:
                await update.message.reply_text("Use KEY=value on each line, or send clear to remove all variables.")
                return ST_ENV_EDIT
            key, value = line.split("=", 1)
            env_vars[key.strip()] = value.strip()

    try:
        validated_env = _validate_deployment_env(json.dumps(env_vars))
    except ValueError as exc:
        await update.message.reply_text(f"Invalid environment variables: {exc}")
        return ST_ENV_EDIT

    serialized_env = json.dumps(validated_env)
    was_running = bot.status == BotStatus.RUNNING.value
    old_pid = bot.pid
    db.update_bot(
        bot_id,
        env_vars=serialized_env,
        status=BotStatus.RESTARTING.value if was_running else bot.status,
    )
    restart_message = ""
    if was_running:
        bot.env_vars = serialized_env
        success, message = await process_mgr.restart(bot)
        if success:
            db.update_bot(
                bot_id,
                status=BotStatus.RUNNING.value,
                pid=bot.pid,
                log_file=bot.log_file,
                restart_count=bot.restart_count + 1,
                last_error=None,
            )
            restart_message = "The service restarted with the new environment."
        elif message.startswith("The existing process could not be stopped safely"):
            db.update_bot(bot_id, status=BotStatus.RUNNING.value, pid=old_pid, last_error=message[:200])
            restart_message = "Environment values were saved, but the old process could not be stopped safely."
        else:
            db.update_bot(bot_id, status=BotStatus.ERROR.value, pid=None, last_error=message[:200])
            await notify_admins_about_bot_failure(context, bot, "Hosted bot failed after an environment update", message)
            restart_message = "Environment values were saved, but the service failed to restart. Check its logs."
    else:
        restart_message = "The service will use these values the next time it starts."

    context.user_data.pop("env_bot_id", None)
    await update.message.reply_html(
        f"✅ <b>Environment updated for {escape(bot.name, quote=False)}</b>\n\n"
        f"{escape(restart_message, quote=False)}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
        ]])
    )
    return ConversationHandler.END

async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        return ConversationHandler.END

    message = update.message.text
    cursor = sqlite3.connect(str(Config.DB_PATH)).cursor()
    cursor.execute("SELECT DISTINCT owner_id FROM bots")
    users = [r[0] for r in cursor.fetchall()]

    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, f"📢 <b>Announcement</b>\n\n{message}", parse_mode="HTML")
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"📢 Broadcast sent to {sent} users.")
    return ConversationHandler.END

# ═══════════════════════════════════════════════════════════════════
# AUTO-HEAL BACKGROUND TASK
# ═══════════════════════════════════════════════════════════════════

async def auto_heal_task(context: ContextTypes.DEFAULT_TYPE):
    """One watchdog pass; JobQueue owns cadence and shutdown lifecycle."""
    try:
        bots = db.get_all_bots()
        for bot in bots:
            if bot.mode != BotMode.PRODUCTION.value:
                continue

            is_alive = process_mgr.is_alive(bot.bot_id)
            if bot.status == "running" and not is_alive:
                allowed, attempt_count = process_mgr.begin_auto_heal(bot.bot_id)
                if not allowed:
                    message = (
                        f"Auto-heal paused after {attempt_count} restarts in "
                        f"{Config.AUTO_HEAL_WINDOW_SECONDS}s; inspect logs and restart manually."
                    )
                    db.update_bot(bot.bot_id, status="error", pid=None, last_error=message[:200])
                    logger.error("Auto-heal circuit breaker opened for %s", bot.bot_id)
                    await notify_admins_about_bot_failure(context, bot, "Hosted bot auto-heal circuit breaker opened", message)
                    continue

                logger.warning("Auto-heal attempt %s/%s for %s", attempt_count, Config.AUTO_HEAL_MAX_RESTARTS, bot.bot_id)
                success, msg = await process_mgr.restart(bot)
                if success:
                    process = process_mgr.processes.get(bot.bot_id)
                    db.update_bot(
                        bot.bot_id,
                        status="running",
                        pid=process.pid if process else None,
                        log_file=bot.log_file,
                        restart_count=bot.restart_count + 1,
                        last_error=None,
                    )
                    logger.info("Auto-healed %s", bot.bot_id)
                else:
                    still_running = process_mgr.is_alive(bot.bot_id)
                    db.update_bot(
                        bot.bot_id,
                        status="running" if still_running else "error",
                        pid=bot.pid if still_running else None,
                        last_error=msg[:200],
                    )
                    logger.error("Auto-heal failed for %s", bot.bot_id)
                    if not still_running:
                        await notify_admins_about_bot_failure(context, bot, "Hosted bot auto-heal failed", msg)

            elif is_alive and bot.pid:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(bot.pid), "-o", "%cpu,%mem",
                        stdout=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    lines = stdout.decode().strip().split("\n")
                    if len(lines) > 1:
                        cpu, mem = lines[1].strip().split()
                        db.update_bot(bot.bot_id, resource_usage=json.dumps({
                            "cpu": f"{cpu}%", "mem": f"{mem}%", "checked": datetime.now().isoformat()
                        }))
                except Exception:
                    logger.debug("Could not collect resource usage for %s", bot.bot_id, exc_info=True)
    except Exception as e:
        logger.error("Auto-heal check error: %s", e)

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    if not Config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. Set it in the environment before starting the worker.")

    health_server = WorkerHealthServer(Config.HEALTH_HOST, Config.HEALTH_PORT)

    async def post_init(application: Application):
        manager: PluginManager = application.bot_data["plugin_manager"]
        await application.bot.set_my_commands([
            BotCommand("start", "🚀 Open main menu"),
            BotCommand("logs", "📜 View bot logs (/logs <id> [lines])"),
            BotCommand("cancel", "❌ Cancel operation"),
            *manager.bot_commands(),
        ])
        await manager.startup()
        health_server.set_ready(True)
        logger.info("Telegram application is ready")

    async def post_stop(application: Application):
        health_server.set_ready(False)
        manager: PluginManager | None = application.bot_data.get("plugin_manager")
        if manager is not None:
            await manager.shutdown()
        await process_mgr.stop_all()
        logger.info("Telegram plugins and hosted processes shut down")

    async def post_shutdown(_application: Application):
        db.close()
        logger.info("Telegram application shutdown complete")

    application = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )
    plugin_context = SimpleNamespace(
        config=Config,
        db=db,
        process_mgr=process_mgr,
        log_streamer=log_streamer,
        ui=ui,
    )
    plugin_manager = PluginManager(application, plugin_context)
    application.bot_data["plugin_context"] = plugin_context
    application.bot_data["plugin_manager"] = plugin_manager
    plugin_manager.discover()

    deploy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router, pattern="^nav_deploy$")],
        states={
            ST_DEPLOY_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_url)],
            ST_DEPLOY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_name)],
            ST_DEPLOY_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_password)],
            ST_DEPLOY_ENV: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_env)],
            ST_DEPLOY_CONFIRM: [CallbackQueryHandler(deploy_confirm_callback, pattern="^deploy_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        map_to_parent={ConversationHandler.END: ST_IDLE},
    )

    env_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router, pattern="^act_env_")],
        states={ST_ENV_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, env_edit_handler)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router, pattern="^admin_broadcast$")],
        states={ST_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_handler)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("logs", cmd_logs))
    application.add_handler(deploy_conv)
    application.add_handler(env_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(CallbackQueryHandler(callback_router))

    application.job_queue.run_repeating(
        auto_heal_task,
        interval=Config.AUTO_HEAL_INTERVAL,
        first=5,
        name="process-auto-heal-watchdog",
    )

    logger.info(
        "Starting Telegram worker with %s plugins",
        len([plugin for plugin in plugin_manager.loaded if plugin.enabled]),
    )
    health_server.start()
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        health_server.set_ready(False)
        health_server.stop()

if __name__ == "__main__":
    main()
