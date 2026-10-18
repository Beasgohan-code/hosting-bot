import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("HOSTING_DATA_DIR", tempfile.mkdtemp(prefix="hosting-bot-worker-tests-"))

import ultimate_hosting_bot_fixed as worker
from bot_plugins.builtin.admin import AdminPlugin
from bot_plugins.core import BotPlugin, LoadedPlugin, PluginManager, PluginSpec


class WorkerInputValidationTests(unittest.TestCase):
    def test_accepts_only_canonical_public_github_https_urls(self):
        self.assertEqual(
            worker.validate_github_repo_url("https://github.com/Example/worker"),
            "https://github.com/Example/worker.git",
        )
        for url in (
            "http://github.com/example/worker",
            "https://github.com.evil.example/example/worker",
            "https://github.com@127.0.0.1/example/worker",
            "https://github.com/example/worker/tree/main",
            "https://github.com/example/worker?token=secret",
            "ssh://git@github.com/example/worker.git",
            "https://github.com:8443/example/worker",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    worker.validate_github_repo_url(url)

    def test_rejects_invalid_or_multiline_environment_values(self):
        for values in (
            {"BAD KEY": "value"},
            {"API_KEY": "line one\nPATH=/tmp"},
            {"HOME": "/tmp"},
            {"PATH": "/tmp"},
            {"BOT_ID": "other"},
            {"API_KEY": "x" * 9000},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    worker._validate_deployment_env(__import__("json").dumps(values))

    def test_child_environment_does_not_inherit_worker_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            env = worker._minimal_child_environment(Path(directory) / "home")
        self.assertNotIn("BOT_TOKEN", env)
        self.assertNotIn("ADMIN_IDS", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_html_output_escapes_untrusted_log_text(self):
        text = worker.ui.code_block("<script>alert(1)</script> & value")
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_failure_log_redactor_catches_common_secret_formats(self):
        api_token = "987654:abcdefghijklmnopqrstuvwxyzABCDE"
        raw = (
            "API_SECRET=super-secret-value" + chr(10)
            + "Authorization: Bearer abcdefghijklmnop" + chr(10)
            + "HTTP Request: POST https://api.telegram.org/bot" + api_token + "/getMe"
        )
        redacted = worker.redact_sensitive_log_text(raw)
        self.assertIn("[REDACTED]", redacted)
        self.assertIn("[REDACTED_TELEGRAM_TOKEN]", redacted)
        self.assertNotIn("super-secret-value", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)
        self.assertNotIn(api_token, redacted)

    def test_httpx_info_logs_are_suppressed_to_avoid_bot_api_token_urls(self):
        self.assertEqual(worker.logging.getLogger("httpx").level, worker.logging.WARNING)

    def test_logging_filter_redacts_tokens_from_messages_and_exceptions(self):
        api_token = "987654:abcdefghijklmnopqrstuvwxyzABCDE"
        url = "https://api.telegram.org/bot" + api_token + "/getMe"
        record = worker.logging.LogRecord("test", worker.logging.INFO, "", 0, "request %s", (url,), None)
        token_filter = worker.TelegramTokenLogFilter()
        token_filter.filter(record)
        self.assertNotIn(api_token, record.getMessage())

        try:
            raise RuntimeError("Request failed for " + url)
        except RuntimeError:
            error_record = worker.logging.LogRecord(
                "test", worker.logging.ERROR, "", 0, "request failed", (),
                __import__("sys").exc_info(),
            )
        token_filter.filter(error_record)
        formatted = worker.logging.Formatter().format(error_record)
        self.assertNotIn(api_token, formatted)

    def test_log_rotation_keeps_bounded_number_of_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "service.log"
            for index in range(worker.PROCESS_LOG_BACKUPS + 2):
                log.write_text(f"log-{index}", encoding="utf-8")
                worker.ProcessManager._rotate_process_log(log)
            self.assertTrue((Path(f"{log}.{worker.PROCESS_LOG_BACKUPS}")).exists())
            self.assertTrue((Path(f"{log}.1")).exists())
            self.assertFalse(Path(f"{log}.{worker.PROCESS_LOG_BACKUPS + 1}").exists())


class BackupHardeningTests(unittest.IsolatedAsyncioTestCase):
    def _configure_backup_paths(self, root, bot_id):
        repos = Path(root) / "repos"
        backup_dir = Path(root) / "backups"
        bot_dir = repos / bot_id
        bot_dir.mkdir(parents=True)
        return repos, backup_dir, bot_dir

    def test_backup_archives_regular_files_without_following_links_or_caches(self):
        bot_id = "bot_123_abcd1234abcd"
        with tempfile.TemporaryDirectory() as directory:
            repos, backups, bot_dir = self._configure_backup_paths(directory, bot_id)
            (bot_dir / "main.py").write_text("print('hello')", encoding="utf-8")
            (bot_dir / ".git").mkdir()
            (bot_dir / ".git" / "config").write_text("not included", encoding="utf-8")
            (bot_dir / "node_modules").mkdir()
            (bot_dir / "node_modules" / "large.js").write_text("not included", encoding="utf-8")
            outside = Path(directory) / "outside.txt"
            outside.write_text("private data", encoding="utf-8")
            try:
                (bot_dir / "linked.txt").symlink_to(outside)
            except (NotImplementedError, OSError):
                pass

            with (
                patch.object(worker.Config, "REPOS_DIR", repos),
                patch.object(worker.Config, "BACKUPS_DIR", backups),
            ):
                archive_path = worker.create_bot_backup(bot_id, bot_dir)

            with worker.zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist(), ["main.py"])
                self.assertEqual(archive.read("main.py"), b"print('hello')")
            self.assertEqual(archive_path.stat().st_mode & 0o777, 0o600)

    def test_backup_size_limit_leaves_no_partial_archive(self):
        bot_id = "bot_123_abcd1234abcd"
        with tempfile.TemporaryDirectory() as directory:
            repos, backups, bot_dir = self._configure_backup_paths(directory, bot_id)
            (bot_dir / "large.txt").write_text("12345", encoding="utf-8")
            with (
                patch.object(worker.Config, "REPOS_DIR", repos),
                patch.object(worker.Config, "BACKUPS_DIR", backups),
                patch.object(worker, "MAX_BACKUP_INPUT_BYTES", 4),
            ):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    worker.create_bot_backup(bot_id, bot_dir)
            self.assertEqual(list(backups.iterdir()), [])

    async def test_backup_handler_delivers_small_archives_to_requesting_user(self):
        bot_id = "bot_123_abcd1234abcd"
        bot = worker.HostedBot(
            bot_id=bot_id,
            name="Test Worker",
            owner_id=123,
            github_url="https://github.com/example/worker.git",
            status="stopped",
            mode="production",
            created_at="2026-10-18T00:00:00",
            updated_at="2026-10-18T00:00:00",
        )
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        send_document = AsyncMock()
        context = SimpleNamespace(bot=SimpleNamespace(send_document=send_document))
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "backup.zip"
            archive_path.write_bytes(b"zip-data")
            with (
                patch.object(worker.db, "get_bot", return_value=bot),
                patch.object(worker, "can_manage_bot", return_value=True),
                patch.object(worker, "create_bot_backup", return_value=archive_path),
            ):
                await worker.handle_backup(query, bot_id, 123, context)

        self.assertEqual(send_document.await_args.kwargs["chat_id"], 123)
        self.assertIn("backup.zip", send_document.await_args.kwargs["filename"])
        self.assertIn("sent to your Telegram DM", query.edit_message_text.await_args.args[0])

    def test_backup_retention_keeps_only_the_newest_archives(self):
        bot_id = "bot_123_abcd1234abcd"
        with tempfile.TemporaryDirectory() as directory:
            repos, backups, bot_dir = self._configure_backup_paths(directory, bot_id)
            file_path = bot_dir / "state.txt"
            with (
                patch.object(worker.Config, "REPOS_DIR", repos),
                patch.object(worker.Config, "BACKUPS_DIR", backups),
                patch.object(worker, "MAX_BACKUPS_PER_BOT", 2),
            ):
                for index in range(3):
                    file_path.write_text(str(index), encoding="utf-8")
                    worker.create_bot_backup(bot_id, bot_dir)
                retained = list(backups.glob(f"{bot_id}_*.zip"))
            self.assertEqual(len(retained), 2)
            retained_contents = []
            for path in retained:
                with worker.zipfile.ZipFile(path) as archive:
                    retained_contents.append(archive.read("state.txt").decode("utf-8"))
            self.assertNotIn("0", retained_contents)


class AdminFailureNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_redacted_failure_logs_to_each_admin_dm(self):
        bot = worker.HostedBot(
            bot_id="bot_123_abcd1234abcd",
            name="Test Worker",
            owner_id=123,
            github_url="https://github.com/example/worker.git",
            status="error",
            mode="production",
            created_at="2026-10-18T00:00:00",
            updated_at="2026-10-18T00:00:00",
        )
        send_message = AsyncMock()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        raw_logs = "<b>bad startup</b>" + chr(10) + "BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyzABCDE"

        with (
            patch.object(worker.Config, "ADMIN_IDS", [111, 222]),
            patch.object(worker.log_streamer, "tail", new=AsyncMock(return_value=raw_logs)),
        ):
            await worker.notify_admins_about_bot_failure(
                context, bot, "Hosted bot failed to start", "<script>process exited</script>"
            )

        self.assertEqual(send_message.await_count, 2)
        self.assertEqual([call.kwargs["chat_id"] for call in send_message.await_args_list], [111, 222])
        text = send_message.await_args_list[0].kwargs["text"]
        self.assertIn("Test Worker", text)
        self.assertIn("&lt;b&gt;bad startup&lt;/b&gt;", text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("&lt;script&gt;process exited&lt;/script&gt;", text)
        self.assertIn(chr(10), text)
        self.assertNotIn(chr(92) + "n", text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyzABCDE", text)


class ProfessionalBotFeatureTests(unittest.IsolatedAsyncioTestCase):
    def _hosted_bot(self, *, owner_id=123, name="User service", status="running"):
        return worker.HostedBot(
            bot_id="bot_123_abcd1234abcd",
            name=name,
            owner_id=owner_id,
            github_url="https://github.com/example/worker.git",
            status=status,
            mode="production",
            created_at="2026-10-18T00:00:00",
            updated_at="2026-10-18T00:00:00",
        )

    def test_default_command_menu_excludes_admin_only_plugins(self):
        class PublicPlugin(BotPlugin):
            spec = PluginSpec(
                "public", "1.0", "Public",
                (("health", "Health"), ("stats", "Statistics")),
                admin_commands=("stats",),
            )

        class AdminPlugin(BotPlugin):
            spec = PluginSpec("admin", "1.0", "Admin", (("restartall", "Restart"),), admin_only=True)

        manager = PluginManager.__new__(PluginManager)
        manager.loaded = [
            LoadedPlugin(PublicPlugin(), "public"),
            LoadedPlugin(AdminPlugin(), "admin"),
            LoadedPlugin(AdminPlugin(), "disabled", enabled=False),
        ]
        self.assertEqual([command.command for command in manager.bot_commands(include_admin=False)], ["health"])
        self.assertEqual(
            [command.command for command in manager.bot_commands(include_admin=True)],
            ["health", "stats", "restartall"],
        )
        public_catalog = manager.catalog(include_admin=False)
        self.assertEqual([plugin["name"] for plugin in public_catalog], ["public"])
        self.assertEqual(public_catalog[0]["commands"], ["health"])

    def test_help_is_dynamic_and_hides_admin_plugin_commands_from_regular_users(self):
        manager = SimpleNamespace(loaded=[
            SimpleNamespace(
                enabled=True,
                plugin=SimpleNamespace(spec=SimpleNamespace(
                    name="Diagnostics <tools>",
                    admin_only=False,
                    commands=(("health", "Check <worker> health"), ("stats", "Global statistics")),
                    admin_commands=("stats",),
                )),
            ),
            SimpleNamespace(
                enabled=True,
                plugin=SimpleNamespace(spec=SimpleNamespace(
                    name="Admin",
                    admin_only=True,
                    commands=(("restartall", "Restart everything"),),
                    admin_commands=(),
                )),
            ),
        ])
        with patch.object(worker.Config, "ADMIN_IDS", [999]):
            public_text = worker.build_help_text(123, manager)
            admin_text = worker.build_help_text(999, manager)

        self.assertIn("<code>/health</code>", public_text)
        self.assertIn("Diagnostics &lt;tools&gt;", public_text)
        self.assertIn("Check &lt;worker&gt; health", public_text)
        self.assertNotIn("/stats", public_text)
        self.assertNotIn("/restartall", public_text)
        self.assertIn("/stats", admin_text)
        self.assertIn("/restartall", admin_text)

    def test_regular_user_status_is_scoped_to_their_real_records(self):
        bot = self._hosted_bot()
        with (
            patch.object(worker.Config, "ADMIN_IDS", [999]),
            patch.object(worker.db, "get_user_bots", return_value=[bot]) as get_user_bots,
            patch.object(worker.db, "get_all_bots") as get_all_bots,
            patch.object(worker.process_mgr, "is_alive", return_value=True),
            patch.object(worker.db, "get_keepalives", return_value=[{"monitor_id": "real-monitor"}]),
        ):
            report = worker.build_status_text(123)

        get_user_bots.assert_called_once_with(123)
        get_all_bots.assert_not_called()
        self.assertIn("Services in scope: <code>1</code>", report)
        self.assertIn("🟢 Running: <code>1</code>", report)
        self.assertIn("Keep-alive monitors: <code>1</code>", report)
        self.assertNotIn("Unique Users", report)
        self.assertNotIn("Hosting data storage", report)

    def test_admin_status_uses_database_counts_and_real_worker_metrics(self):
        bots = [self._hosted_bot(owner_id=123), self._hosted_bot(owner_id=456, status="error")]
        with (
            patch.object(worker.Config, "ADMIN_IDS", [999]),
            patch.object(worker.db, "get_all_bots", return_value=bots) as get_all_bots,
            patch.object(worker.db, "get_keepalives", return_value=[]),
            patch.object(worker.process_mgr, "is_alive", return_value=True),
            patch.object(worker.Config, "AUTO_HEAL_INTERVAL", 37),
            patch.object(worker.shutil, "disk_usage", return_value=SimpleNamespace(free=1024, total=4096)),
        ):
            report = worker.build_status_text(999)

        get_all_bots.assert_called_once_with()
        self.assertIn("Services in scope: <code>2</code>", report)
        self.assertIn("🔴 Needs attention: <code>1</code>", report)
        self.assertIn("Auto-heal cadence: <code>37s</code>", report)
        self.assertIn("1.0 KiB free of 4.0 KiB", report)

    async def test_restart_all_preserves_stopped_services_and_records_failures(self):
        running = self._hosted_bot(name="Running service", status="running")
        running.bot_id = "bot_run_123456"
        stopped = self._hosted_bot(name="Stopped service", status="stopped")
        stopped.bot_id = "bot_stop_123456"
        failed = self._hosted_bot(name="Failed service", status="error")
        failed.bot_id = "bot_fail_123456"

        db = SimpleNamespace(get_all_bots=lambda: [running, stopped, failed], update_bot=Mock())
        process_mgr = SimpleNamespace(
            restart=AsyncMock(side_effect=[(True, "started"), (False, "startup failed")]),
            is_alive=Mock(return_value=False),
        )
        failure_notifier = AsyncMock()
        plugin = AdminPlugin()
        plugin.context = SimpleNamespace(
            config=SimpleNamespace(ADMIN_IDS=[999]),
            db=db,
            process_mgr=process_mgr,
            failure_notifier=failure_notifier,
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_message=SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock()),
        )
        context = SimpleNamespace()

        await plugin.restart_all_command(update, context)

        self.assertEqual(process_mgr.restart.await_count, 2)
        updated = [call.args[0] for call in db.update_bot.call_args_list]
        self.assertIn(running.bot_id, updated)
        self.assertIn(failed.bot_id, updated)
        self.assertNotIn(stopped.bot_id, updated)
        self.assertEqual(db.update_bot.call_args_list[0].kwargs["restart_count"], 1)
        self.assertEqual(db.update_bot.call_args_list[1].kwargs["status"], "error")
        failure_notifier.assert_awaited_once()
        report = update.effective_message.reply_html.await_args.args[0]
        self.assertIn("Succeeded: <code>1</code> · Failed: <code>1</code>", report)

    async def test_admin_broadcast_escapes_html_and_reports_delivery_counts(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_message=SimpleNamespace(text="<script>alert(1)</script> & hello", reply_text=AsyncMock()),
        )
        send_message = AsyncMock()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        with (
            patch.object(worker.Config, "ADMIN_IDS", [999]),
            patch.object(worker.db, "get_user_ids", return_value=[123, 456]),
            patch.object(worker.asyncio, "sleep", new=AsyncMock()),
        ):
            result = await worker.broadcast_handler(update, context)

        self.assertEqual(result, worker.ConversationHandler.END)
        self.assertEqual(send_message.await_count, 2)
        broadcast = send_message.await_args.args[1]
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; hello", broadcast)
        self.assertNotIn("<script>", broadcast)
        self.assertEqual(send_message.await_args.kwargs["parse_mode"], "HTML")
        self.assertIn("delivered to 2; failed for 0", update.effective_message.reply_text.await_args.args[0])

    async def test_callback_query_is_acknowledged_at_most_once(self):
        query = SimpleNamespace(id="callback-123", answer=AsyncMock())
        self.assertTrue(await worker.answer_callback_once(query))
        self.assertFalse(await worker.answer_callback_once(query, "duplicate", show_alert=True))
        query.answer.assert_awaited_once_with()

    async def test_global_error_handler_redacts_and_dms_admins(self):
        token = "987654:abcdefghijklmnopqrstuvwxyzABCDE"
        error = RuntimeError("Request failed at https://api.telegram.org/bot" + token + "/getMe")
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            update_id=3456,
            effective_user=SimpleNamespace(id=123),
            effective_message=message,
        )
        send_message = AsyncMock()
        context = SimpleNamespace(error=error, bot=SimpleNamespace(send_message=send_message))
        worker._recent_telegram_error_alerts.clear()
        with patch.object(worker.Config, "ADMIN_IDS", [999, 1000]):
            await worker.telegram_error_handler(update, context)

        self.assertEqual(send_message.await_count, 2)
        for call in send_message.await_args_list:
            self.assertIn("[REDACTED_TELEGRAM_TOKEN]", call.kwargs["text"])
            self.assertNotIn(token, call.kwargs["text"])
        message.reply_text.assert_awaited_once()



if __name__ == "__main__":
    unittest.main()
