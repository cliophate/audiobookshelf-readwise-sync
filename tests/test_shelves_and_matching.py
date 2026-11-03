import dataclasses
import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from abs_readwise_sync.config import SyncError
from abs_readwise_sync import cli
from abs_readwise_sync.matching import item_metadata, same_work
from test_sync import SyncFixture, source


class MatchingTests(unittest.TestCase):
    def test_subtitle_and_series_are_retained_without_preventing_primary_title_match(self):
        metadata = item_metadata(
            {
                "media": {
                    "metadata": {
                        "title": "The Example",
                        "subtitle": "A Long Description",
                        "authorName": "Ada Example",
                        "series": [{"name": "A Series", "sequence": "1"}],
                    }
                }
            }
        )
        self.assertEqual(metadata["title"], "The Example")
        self.assertEqual(metadata["subtitle"], "A Long Description")
        self.assertEqual(metadata["series"], [{"name": "A Series", "sequence": "1"}])
        for title in ("The Example", "The Example: A Different Description"):
            self.assertTrue(same_work(metadata, {"title": title, "author": "Ada Example"}))

    def test_author_and_meaningful_title_numbers_still_have_to_agree(self):
        book = {"title": "The Example 2", "author": "Ada Example"}
        for other in (
            {"title": "The Example 1", "author": "Ada Example"},
            {"title": "The Example 2", "author": "Someone Else"},
            {"title": "The Example 2", "author": ""},
        ):
            self.assertFalse(same_work(book, other))
        self.assertFalse(
            same_work(
                {"title": "The Example", "subtitle": "Part Two", "author": "Ada Example"},
                {"title": "The Example: Part One", "author": "Ada Example"},
            )
        )

    def test_distinct_colon_titles_are_not_collapsed_to_the_same_prefix(self):
        self.assertFalse(
            same_work(
                {"title": "Dune: Messiah", "author": "Frank Herbert"},
                {"title": "Dune: Children of Dune", "author": "Frank Herbert"},
            )
        )

    def test_conflicting_known_series_numbers_do_not_match(self):
        a = {"title": "Example", "author": "Ada", "series": [{"sequence": "1"}]}
        b = {"title": "Example", "author": "Ada", "series": [{"sequence": "2"}]}
        self.assertFalse(same_work(a, b))

    def test_product_title_and_author_are_available_when_top_level_fields_are_empty(self):
        a = {"title": "Example", "author": "Ada"}
        self.assertTrue(same_work(a, {"title": "", "author": "", "product": a}))


class ShelfFixture(SyncFixture):
    def setUp(self):
        super().setUp()
        self.progress = {
            "item-1": {
                "libraryItemId": "item-1",
                "isFinished": True,
                "progress": 1,
                "currentTime": 1000,
            }
        }
        self.metadata = {}
        self.shelf_failure = None
        self.shelf_verify_failure = False
        self.transport = httpx.MockTransport(self.respond)

    def respond(self, request):
        path = request.url.path
        if request.url.host == "abs.test" and path.startswith("/api/me/progress/"):
            self.server.requests.append(request)
            item_id = path.rsplit("/", 1)[-1]
            if item_id not in self.progress:
                return httpx.Response(404)
            return httpx.Response(200, json=self.progress[item_id])
        if request.url.host == "abs.test" and path.startswith("/api/items/"):
            response = self.server.respond(request)
            data = response.json()
            data["media"]["metadata"].update(self.metadata.get(data["id"], {}))
            return httpx.Response(200, json=data)
        if request.method == "PATCH" and path == "/books/api/v1/books":
            item = json.loads(request.content)["items"][0]
            if "manual_total_seconds" in item:
                return self.server.respond(request)
            self.server.requests.append(request)
            if self.shelf_failure == "before":
                raise httpx.ReadTimeout("private-detail", request=request)
            book = next(b for b in self.server.books if b["id"] == item["id"])
            if not self.shelf_verify_failure:
                book.update(item)
            if self.shelf_failure == "after":
                raise httpx.ReadTimeout("private-detail", request=request)
            return httpx.Response(200, json={"results": [{"id": item["id"], "status": "updated"}]})
        return self.server.respond(request)

    def enable(self, **settings):
        self.settings = dataclasses.replace(self.settings, sync_shelves=True, **settings)

    def shelf_writes(self):
        return [
            r
            for r in self.server.writes
            if r.method == "PATCH" and "status" in json.loads(r.content)["items"][0]
        ]


class ShelfTests(ShelfFixture, unittest.IsolatedAsyncioTestCase):
    async def test_shelves_default_off_without_progress_requests_or_shelf_writes(self):
        await self.run_sync(apply=True)
        self.assertEqual(self.shelf_writes(), [])
        self.assertFalse(any("/progress/" in r.url.path for r in self.server.requests))

    async def test_preview_reports_finished_without_writes_then_apply_preserves_editorial_fields(
        self,
    ):
        self.enable()
        protected = {
            "rating": 4.5,
            "review": "Keep my review",
            "started_reading_at": "2020-01-01T00:00:00Z",
            "finished_reading_at": None,
        }
        self.server.books[0].update(protected)
        preview = await self.run_sync()
        self.assertEqual(
            preview["books"][0]["shelf"],
            {
                "current": "currently_reading",
                "desired": "finished",
                "status": "ready",
            },
        )
        self.assertEqual(self.server.writes, [])
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"confirmed": 1})
        self.assertEqual(
            json.loads(self.shelf_writes()[0].content),
            {
                "items": [{"id": 42, "status": "finished"}],
            },
        )
        self.assertEqual({k: self.server.books[0][k] for k in protected}, protected)
        self.assertEqual((await self.run_sync(apply=True))["shelf_summary"], {"unchanged": 1})
        self.assertEqual(len(self.shelf_writes()), 1)

    async def test_one_hundred_percent_without_completion_flag_remains_currently_reading(self):
        self.enable()
        self.progress["item-1"]["isFinished"] = False
        self.server.books[0]["status"] = "want_to_read"
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"][0]["shelf"]["desired"], "currently_reading")
        self.assertEqual(self.server.books[0]["status"], "currently_reading")

    async def test_not_started_requires_explicit_selection_and_never_fetches_whole_library(self):
        self.enable(abs_item_ids=("item-1",))
        self.server.sources = []
        self.progress = {}
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"][0]["shelf"]["desired"], "want_to_read")
        self.assertEqual(self.server.books[0]["status"], "want_to_read")
        self.assertFalse(any("/libraries" in r.url.path for r in self.server.requests))
        self.assertFalse(any(r.method == "POST" for r in self.server.requests))
        self.assertEqual(self.db().execute("select count(*) from exports").fetchone()[0], 0)

    async def test_no_sessions_and_no_selection_do_not_discover_unstarted_books(self):
        self.enable()
        self.server.sources = []
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"], [])
        self.assertEqual(self.server.writes, [])
        self.assertFalse(any("/api/items/" in r.url.path for r in self.server.requests))

    async def test_explicit_item_selection_filters_before_metadata_and_matching(self):
        self.settings = dataclasses.replace(self.settings, abs_item_ids=("item-1",))
        self.server.sources.extend(
            source(id=f"s-{i}", libraryItemId=f"other-{i}") for i in range(200)
        )
        result = await self.run_sync(apply=True)
        self.assertEqual(result["excluded_by_scope"], 200)
        self.assertEqual(len(result["books"]), 1)
        self.assertFalse(any("/api/items/other-" in r.url.path for r in self.server.requests))
        self.assertEqual(len(self.server.sessions), 1)

    async def test_managed_book_shelves_refresh_even_if_its_history_is_before_new_cutoff(self):
        self.enable()
        await self.run_sync(apply=True)
        self.settings = dataclasses.replace(
            self.settings, import_since=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        self.progress["item-1"].update(isFinished=False, currentTime=10, progress=0.01)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"][0]["shelf"]["desired"], "currently_reading")
        self.assertEqual(len(self.server.sessions), 1)
        self.assertEqual(self.server.books[0]["status"], "currently_reading")

    async def test_cutoff_still_excludes_unmanaged_old_items_from_shelf_sync(self):
        self.enable(import_since=datetime(2026, 1, 1, tzinfo=timezone.utc))
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"], [])
        self.assertEqual(self.server.writes, [])

    async def test_progress_failure_stops_before_any_writes(self):
        self.enable()
        original = self.respond

        def fail(request):
            if "/api/me/progress/" in request.url.path:
                return httpx.Response(503, text="private-detail")
            return original(request)

        self.transport = httpx.MockTransport(fail)
        with self.assertRaisesRegex(SyncError, "HTTP 503"):
            await self.run_sync(apply=True)
        self.assertEqual(self.server.writes, [])

    async def test_invalid_or_wrong_item_progress_blocks_only_shelf_change(self):
        self.enable()
        for progress in (
            {"libraryItemId": "someone-else", "isFinished": True},
            {"libraryItemId": "item-1", "isFinished": "true"},
            {"libraryItemId": "item-1", "isFinished": False},
            {"libraryItemId": "item-1", "isFinished": False, "progress": 2.0},
        ):
            self.progress["item-1"] = progress
            result = await self.run_sync(apply=True)
            self.assertEqual(result["shelf_summary"], {"needs_review": 1})
            self.assertEqual(self.shelf_writes(), [])

    async def test_uncertain_shelf_write_is_journaled_and_not_replayed(self):
        self.enable()
        self.shelf_failure = "after"
        first = await self.run_sync(apply=True)
        self.assertEqual(first["shelf_summary"], {"needs_review": 1})
        self.assertEqual(
            self.db().execute("select status from shelf_attempts").fetchone()[0], "pending"
        )
        self.shelf_failure = None
        second = await self.run_sync(apply=True)
        self.assertEqual(second["shelf_summary"], {"needs_review": 1})
        self.assertEqual(len(self.shelf_writes()), 1)
        self.assertEqual(len(self.server.sessions), 1)

        output = io.StringIO()
        with (
            patch.object(cli.Settings, "load", return_value=self.settings),
            patch.object(cli.httpx, "AsyncClient", side_effect=AssertionError("No HTTP")),
            contextlib.redirect_stdout(output),
        ):
            await cli.run(SimpleNamespace(issues=True))
        records = json.loads(output.getvalue())
        self.assertTrue(any(r.get("type") == "shelf" for r in records))
        self.assertNotIn("previous_json", output.getvalue())

    async def test_failed_readback_is_not_reported_as_a_success(self):
        self.enable()
        self.shelf_verify_failure = True
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(
            self.db().execute("select status from shelf_attempts").fetchone()[0], "pending"
        )

    async def test_primary_title_matches_when_abs_has_a_separate_subtitle(self):
        self.metadata["item-1"] = {"subtitle": "A Very Long Description"}
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertEqual(result["books"][0]["subtitle"], "A Very Long Description")

    async def test_same_author_and_primary_title_with_multiple_editions_stays_ambiguous(self):
        self.enable()
        self.server.books.append(
            {**self.server.books[0], "id": 43, "title": "An Example Book: Other Edition"}
        )
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ambiguous": 1})
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.server.writes, [])

    async def test_multiple_catalog_editions_are_flagged_without_adding_a_book(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books = []
        original = self.respond

        def respond(request):
            if request.url.path.endswith("/search_products"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": 1, "title": "An Example Book", "author": "Ada Example"},
                        {"id": 2, "title": "An Example Book: A Subtitle", "author": "Ada Example"},
                    ],
                )
            return original(request)

        self.transport = httpx.MockTransport(respond)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ambiguous": 1})
        self.assertEqual(self.server.writes, [])

    async def test_two_abs_items_cannot_fight_over_one_readwise_shelf(self):
        self.enable()
        self.server.sources.append(source(id="other-source", libraryItemId="item-2"))
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_review": 2})
        self.assertEqual(self.shelf_writes(), [])

    async def test_readwise_deleted_book_is_not_revived(self):
        self.enable(app_session="test-app-session")
        self.server.books[0]["status"] = "deleted"
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.server.writes, [])

    async def test_concurrent_readwise_shelf_edit_is_held_for_review(self):
        self.enable()
        original = self.respond

        def respond(request):
            if (
                request.method == "GET"
                and request.url.path == "/books/api/v1/books"
                and request.url.params.get("ids")
            ):
                self.server.books[0]["status"] = "did_not_finish"
            return original(request)

        self.transport = httpx.MockTransport(respond)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(self.shelf_writes(), [])
        self.assertEqual(self.server.books[0]["status"], "did_not_finish")

    async def test_abs_completed_flag_wins_over_a_rounded_or_incomplete_position(self):
        self.enable()
        self.progress["item-1"].update(isFinished=True, progress=0.99, currentTime=990)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"][0]["shelf"]["desired"], "finished")

    async def test_changed_mapping_cannot_reassign_a_book_with_a_shelf_journal(self):
        self.enable(abs_item_ids=("item-1",))
        self.server.sources = []
        await self.run_sync(apply=True)
        self.settings.mappings_file.write_text(json.dumps({"item-1": "43"}))
        self.server.books.append({**self.server.books[0], "id": 43})
        result = await self.run_sync(apply=True)
        self.assertEqual(result["books"][0]["status"], "mapping_changed")
        self.assertEqual(len(self.shelf_writes()), 1)

    async def test_null_length_explains_manual_and_automatic_setup_options(self):
        self.server.books[0]["manual_total_seconds"] = None
        result = await self.run_sync()
        help_text = result["books"][0]["length_help"]
        self.assertIn("manual_total_seconds", help_text)
        self.assertIn("public API", help_text)
        self.assertIn("--apply", help_text)
        self.assertEqual(self.server.writes, [])

    async def test_active_manual_session_explains_why_automatic_duration_setup_waits(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = None
        self.server.active = {"id": 99}
        result = await self.run_sync(apply=True)
        self.assertIn("active reading session", result["books"][0]["length_help"])
        self.assertEqual(self.server.sessions, [])
        self.assertEqual(self.server.active, {"id": 99})

    async def test_finished_and_imports_wait_for_a_saved_manual_audio_length(self):
        self.enable()
        self.server.books[0]["manual_total_seconds"] = None
        self.server.item_duration = None
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"needs_length": 1})
        self.assertEqual(result["shelf_summary"], {"needs_length": 1})
        self.assertEqual(self.shelf_writes(), [])
        self.assertEqual(self.server.sessions, [])
        self.assertFalse(any("/bookwise/api/" in r.url.path for r in self.server.requests))

    async def test_explicit_unstarted_item_can_be_added_with_optional_setup_without_fake_sessions(
        self,
    ):
        self.enable(abs_item_ids=("item-1",), app_session="test-app-session")
        self.server.sources = []
        self.server.books = []
        self.progress = {}
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"confirmed": 1})
        self.assertEqual(self.server.books[0]["status"], "want_to_read")
        self.assertEqual(self.server.sessions, [])
        self.assertEqual(self.server.app_start_count, 0)
