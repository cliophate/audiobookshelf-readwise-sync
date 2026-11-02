import contextlib
import hashlib
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from abs_readwise_sync import cli
from abs_readwise_sync.audit import audit_book
from abs_readwise_sync.config import SyncError, Settings
from test_sync import SyncFixture, source


class AuditTests(SyncFixture, unittest.IsolatedAsyncioTestCase):
    async def audit(self):
        return await audit_book(self.settings, "item-1", transport=self.transport, delay=0)

    async def test_audit_without_a_database_creates_no_files_or_remote_writes(self):
        result = await self.audit()
        self.assertEqual(result["mode"], "audit")
        self.assertEqual(result["abs_history"]["listening_seconds"], 40)
        self.assertEqual(result["abs_history"]["audio_seconds"], 80)
        self.assertEqual(result["ledger"], [])
        self.assertEqual(self.server.writes, [])
        self.assertEqual(list(self.settings.data_dir.iterdir()), [])

    async def test_continuations_reconcile_without_changing_state_or_double_counting(self):
        await self.run_sync(apply=True)
        self.server.sources = [source(updatedAt=1600000180000, timeListening=70, currentTime=240)]
        await self.run_sync(apply=True)
        path = self.settings.data_dir / "sync.sqlite"
        before = path.read_bytes()
        writes = len(self.server.writes)
        result = await self.audit()
        self.assertEqual(result["abs_history"]["audio_seconds"], 140)
        self.assertEqual(result["abs_history"]["listening_seconds"], 70)
        self.assertEqual(result["confirmed_ledger_audio_ranges"]["audio_seconds"], 140)
        self.assertEqual(result["readwise_totals_by_format"], {"audiobook": 140})
        self.assertEqual(result["readwise_audio_ranges"]["repeated_audio_seconds"], 0)
        self.assertEqual(result["readwise_sessions_without_ledger_id"], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.server.writes), writes)

    async def test_repeated_audio_and_identical_remote_rows_are_flagged_without_deletion(self):
        await self.run_sync(apply=True)
        self.server.sessions.append({**self.server.sessions[0], "id": 99, "origin": "manual"})
        before = list(self.server.sessions)
        writes = len(self.server.writes)
        result = await self.audit()
        self.assertEqual(result["readwise_totals_by_format"]["audiobook"], 160)
        self.assertEqual(result["readwise_audio_ranges"]["unique_audio_seconds"], 80)
        self.assertEqual(result["readwise_audio_ranges"]["repeated_audio_seconds"], 80)
        self.assertEqual(result["identical_readwise_session_groups"], [["1", "99"]])
        self.assertEqual(result["readwise_sessions_without_ledger_id"], ["99"])
        self.assertEqual(self.server.sessions, before)
        self.assertEqual(len(self.server.writes), writes)

    async def test_seven_hour_cumulative_source_is_counted_as_seven_hours_not_twenty_eight(self):
        self.server.item_duration = 25200
        self.server.books[0]["manual_total_seconds"] = 25200
        for hour in range(1, 8):
            self.server.sources = [
                source(
                    startTime=0,
                    currentTime=hour * 3600,
                    timeListening=hour * 1800,
                    updatedAt=1600000000000 + hour * 1800000,
                )
            ]
            await self.run_sync(apply=True)
        self.assertEqual(sum(s["total_time_seconds"] for s in self.server.sessions), 25200)
        result = await self.audit()
        self.assertEqual(result["abs_history"]["listening_seconds"], 12600)
        self.assertEqual(result["abs_history"]["audio_seconds"], 25200)
        self.assertEqual(result["confirmed_ledger_audio_ranges"]["audio_seconds"], 25200)
        self.assertEqual(result["readwise_totals_by_format"]["audiobook"], 25200)
        self.assertEqual(result["readwise_audio_ranges"]["repeated_audio_seconds"], 0)

    async def test_deleted_remote_sessions_do_not_add_to_totals(self):
        await self.run_sync(apply=True)
        self.server.sessions[0]["is_deleted"] = True
        result = await self.audit()
        self.assertEqual(result["readwise_totals_by_format"], {})
        self.assertEqual(result["confirmed_ledger_audio_ranges"]["audio_seconds"], 80)
        self.assertTrue(result["readwise_sessions"][0]["is_deleted"])

    async def test_other_reading_formats_are_separated_and_private_fields_are_omitted(self):
        await self.run_sync(apply=True)
        self.server.sessions.append(
            {
                "id": 99,
                "book_id": 42,
                "format": "paper",
                "total_time_seconds": 500,
                "caption": "private-caption",
            }
        )
        self.server.books[0]["review"] = "private-review"
        result = await self.audit()
        encoded = json.dumps(result)
        self.assertEqual(result["readwise_totals_by_format"], {"audiobook": 80, "paper": 500})
        for private in (
            "private-caption",
            "private-review",
            self.settings.abs_token,
            self.settings.readwise_token,
            hashlib.sha256(self.settings.readwise_token.encode()).hexdigest(),
        ):
            self.assertNotIn(private, encoded)

    async def test_ledger_from_a_different_user_is_rejected_without_changes(self):
        await self.run_sync(apply=True)
        before = (self.settings.data_dir / "sync.sqlite").read_bytes()
        self.server.user_id = "different-user"
        self.server.sources = []
        with self.assertRaisesRegex(SyncError, "different credentials or an ABS user"):
            await self.audit()
        self.assertEqual((self.settings.data_dir / "sync.sqlite").read_bytes(), before)

    async def test_shared_readwise_mapping_is_visible_in_the_audit(self):
        await self.run_sync(apply=True)
        con = self.db()
        con.execute("insert into books values('item-2','42',null)")
        con.commit()
        result = await self.audit()
        self.assertEqual(result["linked_abs_item_ids"], ["item-1", "item-2"])
        self.assertTrue(any("More than one ABS item" in s for s in result["warnings"]))

    async def test_missing_optional_app_credential_file_does_not_block_audit_settings(self):
        with patch.dict(
            os.environ,
            {
                "AUDIOBOOKSHELF_URL": "https://abs.test",
                "AUDIOBOOKSHELF_TOKEN": "test-abs",
                "READWISE_TOKEN": "test-readwise",
                "READWISE_APP_SESSION_FILE": "/does/not/exist",
            },
            clear=True,
        ):
            settings = Settings.load(include_app=False)
        self.assertEqual(settings.app_session, "")

    async def test_cli_audit_does_not_enter_the_sync_worker(self):
        output = io.StringIO()
        with (
            patch.object(cli.Settings, "load", return_value=self.settings),
            patch.object(cli, "audit_book", return_value={"read_only": True}) as audit,
            patch.object(cli, "run_once", side_effect=AssertionError("Sync started")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(await cli.run(SimpleNamespace(audit="item-1", login=False)), 0)
        audit.assert_awaited_once_with(self.settings, "item-1")
        self.assertEqual(json.loads(output.getvalue()), {"read_only": True})

    def test_audit_cannot_be_combined_with_writes_or_login(self):
        for other in (
            "--apply",
            "--watch",
            "--login",
            "--issues",
            "--list-books",
            "--accept-token-change",
        ):
            with (
                self.subTest(other=other),
                patch("sys.argv", ["abs-readwise-sync", "--audit", "item-1", other]),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)
