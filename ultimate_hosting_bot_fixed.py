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
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
from enum import Enum
from collections import defaultdict

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
    BOT_TOKEN: str = "YOUR_BOT_TOKEN_HERE"      # ← CHANGE THIS
    ADMIN_IDS: List[int] = [123456789]           # ← YOUR TELEGRAM ID

    # Auto-detect base dir: uses ./hosting_data in same folder as script
    BASE_DIR: Path = Path(__file__).parent / "hosting_data"
    LOGS_DIR: Path = BASE_DIR / "logs"
    REPOS_DIR: Path = BASE_DIR / "repos"
    BACKUPS_DIR: Path = BASE_DIR / "backups"
    DB_PATH: Path = BASE_DIR / "hosting.db"

    MAX_BOTS_PER_USER: int = 5
    AUTO_HEAL_INTERVAL: int = 15
    LOG_TAIL_LINES: int = 50
    DEPLOY_TIMEOUT: int = 120

    @classmethod
    def init_dirs(cls):
        for d in [cls.BASE_DIR, cls.LOGS_DIR, cls.REPOS_DIR, cls.BACKUPS_DIR]:
            d.mkdir(parents=True, exist_ok=True)

# Create dirs FIRST before logging tries to write there
Config.init_dirs()

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
        self.conn = sqlite3.connect(str(Config.DB_PATH), check_same_thread=False)
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
        self._audit(bot.bot_id, bot.owner_id, "CREATE", f"Deployed {bot.name}")

    def update_bot(self, bot_id: str, **kwargs):
        if not kwargs:
            return
        sets = ", ".join([f"{k}=?" for k in kwargs])
        values = list(kwargs.values()) + [bot_id]
        cursor = self.conn.cursor()
        cursor.execute(f"UPDATE bots SET {sets}, updated_at=? WHERE bot_id=?", 
                      [datetime.now().isoformat()] + values)
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

    def _audit(self, bot_id: str, user_id: int, action: str, details: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO audit_log (bot_id, user_id, action, timestamp, details)
            VALUES (?, ?, ?, ?, ?)
        """, (bot_id, user_id, action, datetime.now().isoformat(), details))
        self.conn.commit()

db = Database()

# ═══════════════════════════════════════════════════════════════════
# PROCESS MANAGER
# ═══════════════════════════════════════════════════════════════════

class ProcessManager:
    def __init__(self):
        self.processes: Dict[str, subprocess.Popen] = {}
        self._lock = asyncio.Lock()

    async def deploy(self, bot: HostedBot, password: str = "") -> tuple[bool, str]:
        bot_dir = Config.REPOS_DIR / bot.bot_id
        log_file = Config.LOGS_DIR / f"{bot.bot_id}.log"

        try:
            if bot_dir.exists():
                shutil.rmtree(bot_dir)

            proc = await asyncio.create_subprocess_exec(
                "git", "clone", bot.github_url, str(bot_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if proc.returncode != 0:
                return False, f"Git clone failed:\n{stderr.decode()[:500]}"

            env_dict = json.loads(bot.env_vars) if bot.env_vars else {}
            if password:
                env_dict["DEPLOY_PASSWORD"] = password

            env_path = bot_dir / ".env"
            with open(env_path, "w") as f:
                for k, v in env_dict.items():
                    f.write(f"{k}={v}\n")

            venv_dir = bot_dir / ".venv"
            venv_python = venv_dir / "bin" / "python"

            setup_cmds = [
                [sys.executable, "-m", "venv", str(venv_dir)],
                [str(venv_dir / "bin" / "pip"), "install", "--upgrade", "pip"],
            ]

            req_file = bot_dir / "requirements.txt"
            if req_file.exists():
                setup_cmds.append([str(venv_dir / "bin" / "pip"), "install", "-r", str(req_file)])

            for cmd in setup_cmds:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=str(bot_dir)
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=Config.DEPLOY_TIMEOUT)
                if proc.returncode != 0:
                    return False, f"Setup failed ({cmd[0]}):\n{stderr.decode()[:500]}"

            entry_point = bot_dir / "main.py"
            if not entry_point.exists():
                py_files = list(bot_dir.glob("*.py"))
                if not py_files:
                    return False, "No Python entry point found in repo!"
                entry_point = py_files[0]

            log_handle = open(log_file, "a")
            log_handle.write(f"\n\n=== DEPLOY {datetime.now()} ===\n")
            log_handle.flush()

            process = subprocess.Popen(
                [str(venv_python), str(entry_point)],
                cwd=str(bot_dir),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env={**os.environ, **env_dict},
                preexec_fn=os.setsid
            )

            await asyncio.sleep(3)
            if process.poll() is not None:
                return False, "Bot crashed immediately after start. Check logs."

            async with self._lock:
                self.processes[bot.bot_id] = process

            bot.pid = process.pid
            bot.log_file = str(log_file)
            bot.status = BotStatus.RUNNING.value
            bot.last_error = None

            return True, f"Deployed!\nPID: {process.pid}\nEntry: {entry_point.name}"

        except asyncio.TimeoutError:
            return False, "Deployment timed out (>120s)."
        except Exception as e:
            return False, f"Deployment error: {str(e)}"

    async def stop(self, bot_id: str) -> bool:
        async with self._lock:
            process = self.processes.get(bot_id)

        if not process:
            bot = db.get_bot(bot_id)
            if bot and bot.pid:
                try:
                    os.kill(bot.pid, 15)
                    await asyncio.sleep(2)
                    os.kill(bot.pid, 9)
                except (ProcessLookupError, OSError):
                    pass
            return True

        try:
            os.killpg(os.getpgid(process.pid), 15)
            await asyncio.sleep(2)
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), 9)
            return True
        except Exception as e:
            logger.error(f"Stop error for {bot_id}: {e}")
            return False
        finally:
            async with self._lock:
                self.processes.pop(bot_id, None)

    async def restart(self, bot: HostedBot, password: str = "") -> tuple[bool, str]:
        await self.stop(bot.bot_id)
        await asyncio.sleep(1)
        return await self.deploy(bot, password)

    def is_alive(self, bot_id: str) -> bool:
        process = self.processes.get(bot_id)
        if process:
            return process.poll() is None
        bot = db.get_bot(bot_id)
        if bot and bot.pid:
            try:
                os.kill(bot.pid, 0)
                return True
            except OSError:
                return False
        return False

process_mgr = ProcessManager()

# ═══════════════════════════════════════════════════════════════════
# LOG STREAMER
# ═══════════════════════════════════════════════════════════════════

class LogStreamer:
    def __init__(self):
        self._watchers: Dict[str, asyncio.Task] = {}

    async def tail(self, bot_id: str, lines: int = 50) -> str:
        bot = db.get_bot(bot_id)
        if not bot or not bot.log_file:
            return "📭 No logs available."

        log_path = Path(bot.log_file)
        if not log_path.exists():
            return "📭 Log file not found."

        try:
            proc = await asyncio.create_subprocess_exec(
                "tail", "-n", str(lines), str(log_path),
                stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            return stdout.decode("utf-8", errors="replace")[-3800:]
        except Exception as e:
            return f"❌ Log read error: {e}"

    def format_log_message(self, bot_id: str, content: str, live: bool = False) -> str:
        bot = db.get_bot(bot_id)
        status_emoji = "🟢" if bot and bot.status == "running" else "🔴"
        mode_str = bot.mode.upper() if bot else "UNKNOWN"

        header = (
            f"{status_emoji} <b>📜 LIVE LOGS: {bot.name if bot else bot_id}</b>\n"
            f"<i>Mode: <code>{mode_str}</code> | "
            f"{'🔴 LIVE STREAM' if live else '📦 Snapshot'} | "
            f"Lines: {Config.LOG_TAIL_LINES}</i>\n\n"
        )

        max_content = 3800
        if len(content) > max_content:
            content = "...[truncated]...\n" + content[-max_content:]

        return header + f"<pre><code class='language-bash'>{content}</code></pre>"

log_streamer = LogStreamer()

# ═══════════════════════════════════════════════════════════════════
# UI FACTORY
# ═══════════════════════════════════════════════════════════════════

class UI:
    @staticmethod
    def blockquote(text: str, expandable: bool = True) -> str:
        tag = "blockquote expandable" if expandable else "blockquote"
        return f"<{tag}>{text}</{tag.split()[0]}>"

    @staticmethod
    def code_block(text: str, lang: str = "bash") -> str:
        return f"<pre><code class='language-{lang}'>{text}</code></pre>"

    @staticmethod
    def main_menu(user_id: int) -> InlineKeyboardMarkup:
        is_admin = user_id in Config.ADMIN_IDS
        buttons = [
            [InlineKeyboardButton("🚀 DEPLOY NEW BOT", callback_data="nav_deploy")],
            [InlineKeyboardButton("📋 MY BOTS", callback_data="nav_list"),
             InlineKeyboardButton("📊 STATUS", callback_data="nav_status")],
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
            f"{e} <b>{bot.name}</b> <code>[{bot.mode.upper()}]</code>\n"
            f"├ ID: <code>{bot.bot_id}</code>\n"
            f"├ Status: <b>{bot.status.upper()}</b>\n"
            f"├ PID: <code>{bot.pid or 'None'}</code>\n"
            f"├ Restarts: <code>{bot.restart_count}</code>\n"
            f"├ CPU: {cpu} | MEM: {mem}\n"
            f"└ Created: <i>{bot.created_at[:19]}</i>\n"
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
        f"{ui.blockquote('Deploy and manage unlimited Telegram bots from GitHub with live logs, auto-healing, and full process isolation.')}\n\n"
        f"<b>🚀 Quick Start:</b>\n"
        f"1. Click <b>DEPLOY NEW BOT</b>\n"
        f"2. Paste your GitHub URL\n"
        f"3. Watch it go live instantly\n\n"
        f"<b>⚡ Ultra Features:</b>\n"
        f"• Live log streaming with auto-refresh\n"
        f"• Auto-heal crash recovery\n"
        f"• 3 runtime modes (Prod/Dev/Maint)\n"
        f"• Full inline button control\n"
        f"• Resource monitoring\n"
        f"• One-click backup/restore"
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
    lines = int(args[1]) if len(args) > 1 and args[1].isdigit() else Config.LOG_TAIL_LINES

    bot = db.get_bot(bot_id)
    if not bot or (bot.owner_id != user_id and user_id not in Config.ADMIN_IDS):
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
        await query.edit_message_text(
            f"<b>🚀 Deploy New Bot</b>\n\n"
            f"Step 1/4: Send me the <b>GitHub Repository URL</b>\n\n"
            f"{ui.blockquote('Example: https://github.com/username/mybot\n\nThe repo must contain a main.py or bot.py and optionally requirements.txt')}\n\n"
            f"Type /cancel to abort.",
            parse_mode="HTML"
        )
        return ST_DEPLOY_URL

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
        await handle_start(query, bot_id, user_id)

    elif data.startswith("act_stop_"):
        bot_id = data.split("_", 2)[2]
        await handle_stop(query, bot_id, user_id)

    elif data.startswith("act_restart_"):
        bot_id = data.split("_", 2)[2]
        await handle_restart(query, bot_id, user_id)

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
        if not bot:
            await query.answer("Bot not found!", show_alert=True)
            return
        await query.edit_message_text(
            f"<b>⚙️ Change Mode: {bot.name}</b>\n\n"
            f"Current: <code>{bot.mode.upper()}</code>\n\n"
            f"{ui.blockquote('Production = Auto-heal + Optimized\nDevelopment = Verbose + No auto-restart\nMaintenance = Offline mode')}",
            parse_mode="HTML",
            reply_markup=ui.mode_selector(bot_id)
        )

    elif data.startswith("act_env_"):
        bot_id = data.split("_", 2)[2]
        context.user_data["env_bot_id"] = bot_id
        bot = db.get_bot(bot_id)
        current_env = json.loads(bot.env_vars) if bot.env_vars else {}
        env_text = "\n".join([f"{k}={v}" for k, v in current_env.items()]) or "None set"

        await query.edit_message_text(
            f"<b>🔧 Edit Environment Variables</b>\n\n"
            f"Bot: <code>{bot.name}</code>\n\n"
            f"Current:\n{ui.code_block(env_text, 'env')}\n\n"
            f"Send new vars in format:\n<code>KEY=value\nKEY2=value2</code>\n\nOr send 'clear' to remove all.\n/cancel to abort.",
            parse_mode="HTML"
        )
        return ST_ENV_EDIT

    elif data.startswith("act_pull_"):
        bot_id = data.split("_", 2)[2]
        await handle_git_pull(query, bot_id, user_id)

    elif data.startswith("act_backup_"):
        bot_id = data.split("_", 2)[2]
        await handle_backup(query, bot_id, user_id)

    elif data.startswith("act_delete_"):
        bot_id = data.split("_", 2)[2]
        bot = db.get_bot(bot_id)
        if not bot:
            await query.answer("Not found!", show_alert=True)
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
    if not bot or (bot.owner_id != user_id and user_id not in Config.ADMIN_IDS):
        await query.answer("⛔ Access denied!", show_alert=True)
        return

    text = (
        f"<b>🔧 MANAGE: {bot.name}</b>\n\n"
        f"{ui.bot_card(bot)}\n"
        f"GitHub: <a href='{bot.github_url}'>Open Repo</a>\n"
        f"Last Error: <code>{bot.last_error or 'None'}</code>\n\n"
        f"{ui.blockquote('Select an action below:')}"
    )

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=ui.bot_actions(bot_id, bot.status)
    )

async def handle_start(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
        await query.answer("Denied!", show_alert=True)
        return

    await query.edit_message_text(f"🟡 <b>Starting {bot.name}...</b>", parse_mode="HTML")

    success, msg = await process_mgr.deploy(bot, "")
    if success:
        db.update_bot(bot_id, status=BotStatus.RUNNING.value, pid=bot.pid, 
                     log_file=bot.log_file, updated_at=datetime.now().isoformat())
        await query.edit_message_text(
            f"✅ <b>{bot.name} is LIVE!</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Manage", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    else:
        db.update_bot(bot_id, status=BotStatus.ERROR.value, last_error=msg[:200])
        await query.edit_message_text(
            f"❌ <b>Failed to start {bot.name}</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )

async def handle_stop(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
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

async def handle_restart(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
        return

    await query.edit_message_text(f"🔄 <b>Restarting {bot.name}...</b>", parse_mode="HTML")
    db.update_bot(bot_id, status=BotStatus.RESTARTING.value)

    success, msg = await process_mgr.restart(bot)
    if success:
        db.update_bot(bot_id, status=BotStatus.RUNNING.value, pid=bot.pid,
                     restart_count=bot.restart_count + 1)
        await query.edit_message_text(
            f"✅ <b>{bot.name} restarted!</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Manage", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    else:
        db.update_bot(bot_id, status=BotStatus.ERROR.value, last_error=msg[:200])
        await query.edit_message_text(
            f"❌ <b>Restart failed</b>\n\n{ui.code_block(msg)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )

async def handle_logs(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or (bot.owner_id != user_id and user_id not in Config.ADMIN_IDS):
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
    if not bot or bot.owner_id != user_id:
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
            if not bot or bot.owner_id != user_id:
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
    if not bot or not bot.log_file or not Path(bot.log_file).exists():
        await query.answer("No log file!", show_alert=True)
        return

    await query.answer("Sending file...")
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(bot.log_file),
        filename=f"{bot_id}_logs.txt",
        caption=f"📥 Full logs for <b>{bot.name}</b>",
        parse_mode="HTML"
    )

async def set_mode(query, bot_id: str, mode: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
        return

    db.update_bot(bot_id, mode=mode)

    if mode == "maintenance":
        await process_mgr.stop(bot_id)
        db.update_bot(bot_id, status=BotStatus.MAINTENANCE.value, pid=None)

    await query.answer(f"Mode set to {mode.upper()}!")
    query.data = f"nav_manage_{bot_id}"
    await callback_router(query, None)

async def handle_git_pull(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
        return

    bot_dir = Config.REPOS_DIR / bot_id
    if not bot_dir.exists():
        await query.answer("Repo not found!", show_alert=True)
        return

    await query.edit_message_text(f"🔄 <b>Pulling updates for {bot.name}...</b>", parse_mode="HTML")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(bot_dir), "pull",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode() or stderr.decode()

        was_running = bot.status == "running"
        if was_running:
            await process_mgr.restart(bot)

        await query.edit_message_text(
            f"✅ <b>Git Pull Complete: {bot.name}</b>\n\n"
            f"{ui.code_block(output[:1500], 'bash')}\n\n"
            f"{'🔄 Auto-restarted.' if was_running else '⏹ Was stopped, not restarted.'}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ <b>Git Pull Failed</b>\n\n{ui.code_block(str(e))}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )

async def handle_backup(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
        return

    bot_dir = Config.REPOS_DIR / bot_id
    backup_path = Config.BACKUPS_DIR / f"{bot_id}_{int(time.time())}.zip"
    Config.BACKUPS_DIR.mkdir(exist_ok=True)

    try:
        shutil.make_archive(str(backup_path).replace(".zip", ""), 'zip', bot_dir)
        await query.answer("Backup created!")
        await query.edit_message_text(
            f"✅ <b>Backup Created: {bot.name}</b>\n\n"
            f"Size: <code>{backup_path.stat().st_size / 1024:.1f} KB</code>\n"
            f"Path: <code>{backup_path.name}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data=f"nav_manage_{bot_id}")
            ]])
        )
    except Exception as e:
        await query.answer("Backup failed!", show_alert=True)

async def handle_delete(query, bot_id: str, user_id: int):
    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != user_id:
        return

    await process_mgr.stop(bot_id)
    bot_dir = Config.REPOS_DIR / bot_id
    if bot_dir.exists():
        shutil.rmtree(bot_dir)

    log_file = Config.LOGS_DIR / f"{bot_id}.log"
    if log_file.exists():
        log_file.unlink()

    db.delete_bot(bot_id)
    db._audit(bot_id, user_id, "DELETE", f"Deleted bot {bot.name}")

    await query.edit_message_text(
        f"🗑 <b>{bot.name} deleted permanently.</b>",
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
    query.data = "nav_admin"
    await callback_router(query, None)

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
    url = update.message.text.strip()
    user_id = update.effective_user.id

    if not url.startswith(("https://github.com/", "http://github.com/")):
        await update.message.reply_text("❌ Invalid GitHub URL. Try again or /cancel:")
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
        "Or send <code>skip</code> if not needed:"
    )
    return ST_DEPLOY_PASS

async def deploy_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
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

    bot_id = f"bot_{user_id}_{int(time.time())}"
    new_bot = HostedBot(
        bot_id=bot_id,
        name=context.user_data["deploy_name"],
        owner_id=user_id,
        github_url=context.user_data["deploy_url"],
        status=BotStatus.DEPLOYING.value,
        mode=BotMode.PRODUCTION.value,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        env_vars=json.dumps(context.user_data.get("deploy_env", {}))
    )

    await query.edit_message_text(
        f"🚀 <b>Deploying {new_bot.name}...</b>\n\n"
        f"{ui.blockquote('Cloning repo → Installing deps → Launching process...')}\n\n"
        f"<i>This takes 30-120 seconds...</i>",
        parse_mode="HTML"
    )

    success, msg = await process_mgr.deploy(new_bot, context.user_data.get("deploy_password", ""))

    if success:
        db.add_bot(new_bot)
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
        await query.edit_message_text(
            f"❌ <b>Deployment Failed</b>\n\n"
            f"{ui.code_block(msg[:2000])}\n\n"
            f"Check the URL and try again.",
            parse_mode="HTML",
            reply_markup=ui.main_menu(user_id)
        )

    context.user_data.clear()
    return ConversationHandler.END

async def env_edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.user_data.get("env_bot_id")
    if not bot_id:
        return ST_IDLE

    bot = db.get_bot(bot_id)
    if not bot or bot.owner_id != update.effective_user.id:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END

    text = update.message.text.strip()
    env_vars = {} if text.lower() == "clear" else {}

    if text.lower() != "clear":
        for line in text.split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                env_vars[k.strip()] = v.strip()

    db.update_bot(bot_id, env_vars=json.dumps(env_vars))

    if bot.status == "running":
        bot.env_vars = json.dumps(env_vars)
        await process_mgr.restart(bot)

    await update.message.reply_html(
        f"✅ <b>Environment updated for {bot.name}</b>\n\n"
        f"{'🔄 Bot restarted to apply changes.' if bot.status == 'running' else ''}",
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
    while True:
        try:
            bots = db.get_all_bots()
            for bot in bots:
                if bot.mode != BotMode.PRODUCTION.value:
                    continue

                is_alive = process_mgr.is_alive(bot.bot_id)

                if bot.status == "running" and not is_alive:
                    logger.warning(f"💀 Auto-heal triggered for {bot.bot_id}")
                    success, msg = await process_mgr.restart(bot)
                    if success:
                        db.update_bot(bot.bot_id, status="running", 
                                    restart_count=bot.restart_count + 1,
                                    last_error=f"Auto-healed at {datetime.now()}")
                        logger.info(f"✅ Auto-healed {bot.bot_id}")
                    else:
                        db.update_bot(bot.bot_id, status="error", last_error=msg[:200])
                        logger.error(f"❌ Auto-heal failed for {bot.bot_id}")

                elif is_alive and bot.pid:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "ps", "-p", str(bot.pid), "-o", "%cpu,%mem", 
                            stdout=asyncio.subprocess.PIPE
                        )
                        stdout, _ = await proc.communicate()
                        lines = stdout.decode().strip().split('\n')
                        if len(lines) > 1:
                            cpu, mem = lines[1].strip().split()
                            db.update_bot(bot.bot_id, resource_usage=json.dumps({
                                "cpu": f"{cpu}%", "mem": f"{mem}%", "checked": datetime.now().isoformat()
                            }))
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Auto-heal error: {e}")

        await asyncio.sleep(Config.AUTO_HEAL_INTERVAL)

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    application = Application.builder().token(Config.BOT_TOKEN).build()

    application.bot.set_my_commands([
        BotCommand("start", "🚀 Open main menu"),
        BotCommand("logs", "📜 View bot logs (/logs <id> [lines])"),
        BotCommand("cancel", "❌ Cancel operation")
    ])

    deploy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router, pattern="^nav_deploy$")],
        states={
            ST_DEPLOY_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_url)],
            ST_DEPLOY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_name)],
            ST_DEPLOY_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_password)],
            ST_DEPLOY_ENV: [MessageHandler(filters.TEXT & ~filters.COMMAND, deploy_env)],
            ST_DEPLOY_CONFIRM: [CallbackQueryHandler(deploy_confirm_callback, pattern="^deploy_")]
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        map_to_parent={ConversationHandler.END: ST_IDLE}
    )

    env_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router, pattern="^act_env_")],
        states={ST_ENV_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, env_edit_handler)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_router, pattern="^admin_broadcast$")],
        states={ST_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_handler)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("logs", cmd_logs))
    application.add_handler(deploy_conv)
    application.add_handler(env_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(CallbackQueryHandler(callback_router))

    application.job_queue.run_once(
        lambda ctx: asyncio.create_task(auto_heal_task(ctx)), 
        when=5
    )

    logger.info("🤖 ULTIMATE HOSTING BOT v3.1 STARTED")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
