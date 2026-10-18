import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HOSTING_DATA_DIR", tempfile.mkdtemp(prefix="hosting-bot-worker-tests-"))

import ultimate_hosting_bot_fixed as worker


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
        raw = "API_SECRET=super-secret-value" + chr(10) + "Authorization: Bearer abcdefghijklmnop"
        redacted = worker.redact_sensitive_log_text(raw)
        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn("super-secret-value", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)

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


if __name__ == "__main__":
    unittest.main()
