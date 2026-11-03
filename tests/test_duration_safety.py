import unittest

import httpx

from abs_readwise_sync.config import SyncError
from abs_readwise_sync.durations import saved_audio_length
from test_shelves_and_matching import ShelfFixture
from test_sync import source


class DurationTests(unittest.TestCase):
    def test_saved_length_requires_a_positive_whole_number(self):
        for value in (None, 0, -1, True, False, "1000", 0.5, float("inf"), float("nan"), 360001):
            with self.subTest(value=value):
                self.assertIsNone(saved_audio_length(value))
        for value in (1, 1000, 1000.0, 360000):
            self.assertEqual(saved_audio_length(value), int(value))


class FinishSafetyTests(ShelfFixture, unittest.IsolatedAsyncioTestCase):
    async def test_suspect_source_blocks_finished_without_changing_history(self):
        self.enable()
        self.server.sources = [source(startTime=0, currentTime=900, timeListening=1)]
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"needs_review": 1})
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.server.writes, [])
        self.assertIn("Suspect audio range", result["books"][0]["sessions"][0]["reason"])

    async def test_invalid_lengths_block_finished_and_session_posts(self):
        self.enable()
        for value in (None, 0, -1, True, "1000", 1000.5):
            with self.subTest(value=value):
                self.server.books[0]["manual_total_seconds"] = value
                preview = await self.run_sync()
                self.assertEqual(preview["books"][0]["shelf"]["status"], "needs_length")
                result = await self.run_sync(apply=True)
                self.assertEqual(result["shelf_summary"], {"needs_length": 1})
                self.assertEqual(self.server.writes, [])
                self.assertEqual(self.server.books[0]["status"], "currently_reading")

    async def test_invalid_existing_length_is_repaired_verified_then_imported_and_finished(self):
        self.enable(app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = 0
        result = await self.run_sync(apply=True)
        self.assertEqual(self.server.books[0]["manual_total_seconds"], 1000)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertEqual(result["shelf_summary"], {"confirmed": 1})
        requests = self.server.requests
        cancel = next(
            i for i, r in enumerate(requests) if r.url.path.endswith("/manual_sessions/cancel")
        )
        checked = next(
            i
            for i, r in enumerate(requests)
            if i > cancel
            and r.method == "GET"
            and r.url.path.endswith("/books")
            and r.url.params.get("ids")
        )
        imported = next(
            i
            for i, r in enumerate(requests)
            if r.method == "POST" and r.url.path == "/books/api/v1/sessions"
        )
        finished = next(i for i, r in enumerate(requests) if r.method == "PATCH")
        self.assertLess(cancel, checked)
        self.assertLess(checked, imported)
        self.assertLess(imported, finished)

    async def test_finished_book_without_new_sessions_still_gets_duration_setup(self):
        self.enable(abs_item_ids=("item-1",), app_session="test-app-session")
        self.server.sources = []
        self.server.books[0]["manual_total_seconds"] = None
        result = await self.run_sync(apply=True)
        self.assertEqual(self.server.app_start_count, 1)
        self.assertEqual(self.server.books[0]["manual_total_seconds"], 1000)
        self.assertEqual(result["shelf_summary"], {"confirmed": 1})
        self.assertEqual(self.server.sessions, [])

    async def test_active_manual_timer_blocks_finished_even_without_new_sessions(self):
        self.enable(abs_item_ids=("item-1",), app_session="test-app-session")
        self.server.sources = []
        self.server.books[0]["manual_total_seconds"] = None
        self.server.active = {"id": 99}
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_length": 1})
        self.assertIn("active manual session", result["books"][0]["length_help"])
        self.assertEqual(self.server.writes, [])

    async def test_failed_duration_readback_blocks_both_operations_and_retains_review(self):
        self.enable(app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = None
        original = self.respond

        def respond(request):
            response = original(request)
            if (
                request.method == "GET"
                and request.url.path == "/books/api/v1/books"
                and request.url.params.get("ids")
                and self.server.app_start_count
            ):
                data = response.json()
                data["results"][0]["manual_total_seconds"] = None
                return httpx.Response(200, json=data)
            return response

        self.transport = httpx.MockTransport(respond)
        with self.assertRaisesRegex(SyncError, "did not save"):
            await self.run_sync(apply=True)
        self.assertEqual(self.server.sessions, [])
        self.assertEqual(self.shelf_writes(), [])
        self.transport = httpx.MockTransport(original)
        again = await self.run_sync(apply=True)
        self.assertEqual(again["books"][0]["status"], "setup_needs_review")
        self.assertEqual(again["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.server.app_start_count, 1)

    async def test_duration_changed_after_planning_blocks_finished(self):
        self.enable()
        original = self.respond

        def respond(request):
            if (
                request.method == "GET"
                and request.url.path == "/books/api/v1/books"
                and request.url.params.get("ids")
            ):
                self.server.books[0]["manual_total_seconds"] = 900
            return original(request)

        self.transport = httpx.MockTransport(respond)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.shelf_writes(), [])

    async def test_position_exceeding_saved_length_blocks_finished_too(self):
        self.enable()
        self.server.books[0]["manual_total_seconds"] = 150
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"position_exceeds_length": 1})
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.server.writes, [])

    async def test_currently_reading_still_does_not_need_a_saved_duration(self):
        self.enable()
        self.progress["item-1"]["isFinished"] = False
        self.server.books[0].update(status="want_to_read", manual_total_seconds=None)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"confirmed": 1})
        self.assertEqual(self.server.sessions, [])
