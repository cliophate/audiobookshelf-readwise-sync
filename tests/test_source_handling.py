import dataclasses
import json
import unittest
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone

import httpx

from test_sync import SyncFixture, source
from abs_readwise_sync.config import SyncError
from abs_readwise_sync import cli


class SourceHandlingTests(SyncFixture, unittest.IsolatedAsyncioTestCase):
    async def test_issues_command_reads_journal_without_network_or_source_payload(self):
        self.server.sources.append(source(id="bad", currentTime="invalid"))
        await self.run_sync()
        output = io.StringIO()
        with (
            patch.object(cli.Settings, "load", return_value=self.settings),
            patch.object(
                cli.httpx, "AsyncClient", side_effect=AssertionError("No network allowed")
            ),
            contextlib.redirect_stdout(output),
        ):
            result = await cli.run(SimpleNamespace(issues=True))
        self.assertEqual(result, 0)
        rows = json.loads(output.getvalue())
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertNotIn("source", rows[0])

    async def test_missing_item_is_retained_while_other_book_imports(self):
        missing = source(id="missing-session", libraryItemId="missing-item")
        self.server.sources.append(missing)
        original = self.server.respond

        def respond(request):
            if request.url.path == "/api/items/missing-item":
                return httpx.Response(404)
            return original(request)

        self.transport = httpx.MockTransport(respond)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ready": 1, "missing_abs_item": 1})
        self.assertEqual(result["result"], {"confirmed": 1})
        con = self.db()
        issue = con.execute(
            "select status,data_json from source_issues where source_id=?", (missing["id"],)
        ).fetchone()
        self.assertEqual(issue[0], "missing_abs_item")
        self.assertEqual(json.loads(issue[1]), missing)
        self.server.sources = [source()]
        await self.run_sync()
        self.assertEqual(con.execute("select count(*) from source_sessions").fetchone()[0], 2)
        self.assertEqual(
            con.execute("select count(*) from source_issues where resolved_at is null").fetchone()[
                0
            ],
            1,
        )

    async def test_missing_main_endpoint_still_stops_with_path_in_error(self):
        original = self.server.respond

        def respond(request):
            if request.url.path == "/api/me/listening-sessions":
                return httpx.Response(404, text="private response body")
            return original(request)

        self.transport = httpx.MockTransport(respond)
        with self.assertRaisesRegex(
            SyncError, "GET /api/me/listening-sessions returned HTTP 404"
        ) as caught:
            await self.run_sync(apply=True)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.server.writes, [])

    async def test_cutoff_avoids_old_item_requests_and_old_unmatched_groups(self):
        self.server.sources.append(
            source(
                id="older",
                libraryItemId="old-item",
                startedAt=1500000000000,
                updatedAt=1500000060000,
            )
        )
        self.settings = dataclasses.replace(
            self.settings, import_since=datetime.fromtimestamp(1550000000, timezone.utc)
        )
        result = await self.run_sync()
        paths = [r.url.path for r in self.server.requests]
        self.assertNotIn("/api/items/old-item", paths)
        self.assertEqual(result["summary"], {"ready": 1, "before_import_since": 1})
        self.assertEqual(len(result["books"]), 1)
        self.assertEqual(self.db().execute("select count(*) from source_sessions").fetchone()[0], 2)

    async def test_bad_source_is_reviewed_without_blocking_valid_history(self):
        bad = source(id="bad-session", currentTime="not-a-number")
        self.server.sources.append(bad)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ready": 1, "needs_review": 1})
        self.assertEqual(result["result"], {"confirmed": 1})
        con = self.db()
        self.assertEqual(
            json.loads(
                con.execute(
                    "select data_json from source_issues where source_id='bad-session'"
                ).fetchone()[0]
            ),
            bad,
        )
        self.server.sources = []
        await self.run_sync()
        self.assertEqual(
            con.execute(
                "select count(*) from source_issues where source_id='bad-session'"
            ).fetchone()[0],
            1,
        )

    async def test_duplicate_source_ids_are_quarantined_without_blocking_other_sources(self):
        duplicate = source(id="duplicate", libraryItemId="duplicate-item")
        self.server.sources += [duplicate, duplicate]
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ready": 1, "needs_review": 1})
        self.assertEqual(len(self.server.sessions), 1)

    async def test_missing_item_recovery_resolves_journal_without_replaying_history(self):
        self.server.sources = [source(libraryItemId="temporarily-missing")]
        original = self.server.respond
        self.transport = httpx.MockTransport(
            lambda request: httpx.Response(404)
            if request.url.path == "/api/items/temporarily-missing"
            else original(request)
        )
        await self.run_sync(apply=True)
        self.transport = httpx.MockTransport(original)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertIsNotNone(
            self.db().execute("select resolved_at from source_issues").fetchone()[0]
        )
        await self.run_sync(apply=True)
        self.assertEqual(len(self.server.sessions), 1)

    async def test_one_second_terminal_rounding_preserves_source_checkpoint(self):
        self.server.sources = [source(startTime=900, currentTime=1000.8)]
        self.server.item_duration = 1000.8
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertTrue(result["books"][0]["sessions"][0]["endpoint_adjusted"])
        self.assertEqual(self.server.sessions[0]["seconds_at_end"], 1000)
        checkpoint = json.loads(
            self.db().execute("select snapshot_json from checkpoints").fetchone()[0]
        )
        self.assertEqual(checkpoint["currentTime"], 1000.8)
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})

    async def test_different_editions_and_larger_overshoots_are_not_clipped(self):
        for duration, end in [(2000, 1000.8), (1000.8, 1002.1)]:
            self.server.sources = [source(startTime=900, currentTime=end)]
            self.server.item_duration = duration
            result = await self.run_sync(apply=True)
            self.assertEqual(result["summary"], {"position_exceeds_length": 1})
            row = result["books"][0]["sessions"][0]
            self.assertEqual(row["abs_position_seconds"], end)
            self.assertEqual(row["readwise_length_seconds"], 1000)
            self.assertEqual(self.server.writes, [])

    async def test_98_percent_is_not_stretched_to_completion(self):
        self.server.sources = [source(startTime=900, currentTime=980)]
        await self.run_sync(apply=True)
        self.assertEqual(self.server.sessions[0]["seconds_at_end"], 980)
