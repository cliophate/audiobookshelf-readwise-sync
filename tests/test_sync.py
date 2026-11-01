import dataclasses
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from abs_readwise_sync import exporter
from abs_readwise_sync.clients import ABSClient, AppClient, BooksClient
from abs_readwise_sync.config import Settings, SyncError
from abs_readwise_sync.matching import item_metadata, same_work
from abs_readwise_sync.sync import run_once


def source(**updates):
    return {
        "id": "source-1",
        "libraryItemId": "item-1",
        "mediaType": "book",
        "startedAt": 1600000000000,
        "updatedAt": 1600000060000,
        "timeListening": 40,
        "startTime": 100,
        "currentTime": 180,
        **updates,
    }


class Server:
    """An ABS user and a Readwise library, with observable writes."""

    def __init__(self):
        self.sources = [source()]
        self.books = [
            {
                "id": 42,
                "title": "An Example Book",
                "author": "Ada Example",
                "status": "currently_reading",
                "manual_total_seconds": 1000,
            }
        ]
        self.sessions = []
        self.requests = []
        self.fail_abs = False
        self.timeout_after_post = False
        self.timeout_before_post = False
        self.verify_fail = False
        self.user_id = "user-1"
        self.results = None
        self.item_duration = 1000
        self.active = None
        self.app_start_count = 0
        self.readwise_token = "test-readwise-token"

    @property
    def writes(self):
        return [r for r in self.requests if r.method != "GET"]

    def respond(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "abs.test":
            if self.fail_abs:
                return httpx.Response(503, text="private response")
            if path == "/api/me":
                return httpx.Response(200, json={"id": self.user_id})
            if path == "/api/me/listening-sessions":
                page = int(request.url.params["page"])
                rows = self.sources[page * 100 : (page + 1) * 100]
                return httpx.Response(
                    200, json={"sessions": rows, "numPages": (len(self.sources) + 99) // 100}
                )
            if path.startswith("/api/items/"):
                return httpx.Response(
                    200,
                    json={
                        "id": path.rsplit("/", 1)[-1],
                        "media": {
                            "duration": self.item_duration,
                            "metadata": {"title": "An Example Book", "authorName": "Ada Example"},
                        },
                    },
                )
        if request.url.host == "readwise.io":
            if path.startswith("/books/api/v1/"):
                if request.headers.get("Authorization") != "Token " + self.readwise_token:
                    return httpx.Response(401)
                if "MOBILESESSION" in request.headers:
                    raise AssertionError("App credential leaked to official API")
                kind = path.rsplit("/", 1)[-1]
                if request.method == "GET":
                    rows = self.books if kind == "books" else self.sessions
                    ids = request.url.params.get("ids")
                    if ids:
                        rows = [r for r in rows if str(r["id"]) in ids.split(",")]
                    if self.verify_fail and kind == "sessions" and ids:
                        rows = [{**r, "total_time_seconds": -1} for r in rows]
                    return httpx.Response(200, json={"results": rows, "next": None})
                if request.method == "POST" and kind == "books":
                    self.books = [
                        {
                            "id": 42,
                            "title": "An Example Book",
                            "author": "Ada Example",
                            "status": "currently_reading",
                            "manual_total_seconds": None,
                        }
                    ]
                    return httpx.Response(
                        200, json={"results": [{"status": "created", "book_id": 42}]}
                    )
                if request.method == "PATCH" and kind == "books":
                    item = json.loads(request.content)["items"][0]
                    self.books[0].update(item)
                    return httpx.Response(
                        200, json={"results": [{"status": "updated", "book_id": 42}]}
                    )
                if request.method == "POST" and kind == "sessions":
                    if self.timeout_before_post:
                        raise httpx.ReadTimeout("private timeout", request=request)
                    results = []
                    for item in json.loads(request.content)["items"]:
                        sid = len(self.sessions) + 1
                        self.sessions.append(
                            {
                                **item,
                                "id": sid,
                                "origin": "api",
                                "is_deleted": False,
                                "total_time_seconds": item["seconds_at_end"]
                                - item["seconds_at_start"],
                            }
                        )
                        results.append({"status": "created", "session_id": sid})
                    if self.timeout_after_post:
                        self.timeout_after_post = False
                        raise httpx.ReadTimeout("private timeout", request=request)
                    return httpx.Response(
                        200, json={"results": results if self.results is None else self.results}
                    )
            if path.startswith("/bookwise/api/"):
                if request.headers.get("MOBILESESSION") != "test-app-session":
                    return httpx.Response(401)
                if "Authorization" in request.headers:
                    raise AssertionError("Official token leaked to app API")
                if path.endswith("search_products"):
                    return httpx.Response(
                        200,
                        json=[{"id": "123", "title": "An Example Book", "author": "Ada Example"}],
                    )
                if path.endswith("ongoing"):
                    return httpx.Response(200, json={"session": self.active})
                if path.endswith("/start"):
                    self.app_start_count += 1
                    item = json.loads(request.content)
                    self.books[0]["manual_total_seconds"] = item["total_seconds"]
                    self.books[0]["status"] = "currently_reading"
                    self.active = {"id": 99}
                    return httpx.Response(200, json={"session": self.active})
                if path.endswith("/cancel"):
                    self.active = None
                    return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")


class SyncFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        data = Path(self.directory.name)
        self.settings = Settings(
            "https://abs.test",
            "test-abs-token",
            "test-readwise-token",
            "",
            data,
            data / "mappings.json",
            15,
            45,
            "UTC",
            None,
        )
        self.server = Server()
        self.transport = httpx.MockTransport(self.server.respond)
        self.env = patch.dict(os.environ, {"PUBLISH_FROM": "", "TIMEZONE": "UTC"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    async def run_sync(self, apply=False):
        return await run_once(self.settings, apply=apply, transport=self.transport, delay=0)

    def db(self):
        con = sqlite3.connect(self.settings.data_dir / "sync.sqlite")
        self.addCleanup(con.close)
        return con


class SyncTests(SyncFixture, unittest.IsolatedAsyncioTestCase):
    async def test_token_only_preview_then_backfill_then_repeat_without_duplicates(self):
        preview = await self.run_sync()
        self.assertEqual(preview["summary"], {"ready": 1})
        self.assertEqual(self.server.writes, [])
        self.assertEqual(self.db().execute("select count(*) from exports").fetchone()[0], 0)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})

        self.assertEqual(len(self.server.sessions), 1)
        session = self.server.sessions[0]
        self.assertEqual(session["total_time_seconds"], 80)
        self.assertEqual(session["paused_seconds"], 20)
        self.assertFalse(session["publish_to_feed"])
        self.assertFalse(any("/bookwise/api/" in r.url.path for r in self.server.requests))

    async def test_missing_length_blocks_token_only_without_private_requests(self):
        self.server.books[0]["manual_total_seconds"] = None
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"needs_length": 1})
        self.assertEqual(self.server.writes, [])
        self.server.books[0]["manual_total_seconds"] = 1000
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})

    async def test_unmatched_book_stays_for_manual_setup_without_app_credential(self):
        self.server.books = []
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"unmatched": 1})
        self.assertEqual(self.server.writes, [])

    async def test_ambiguous_matches_require_explicit_mapping(self):
        self.server.books.append({**self.server.books[0], "id": 43})
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"ambiguous": 1})
        self.settings.mappings_file.write_text(json.dumps({"item-1": 43}))
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})
        self.assertEqual(self.server.sessions[0]["book_id"], 43)

    async def test_explicit_mapping_can_resolve_different_catalog_title(self):
        self.server.books[0]["title"] = "A different edition title"
        self.settings.mappings_file.write_text(json.dumps({"item-1": 42}))
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})

    async def test_changing_mapping_after_import_is_blocked(self):
        await self.run_sync(apply=True)
        self.server.books.append({**self.server.books[0], "id": 43})
        self.settings.mappings_file.write_text(json.dumps({"item-1": 43}))
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"mapping_changed": 1})
        self.assertEqual(len(self.server.sessions), 1)

    async def test_deleted_remote_book_is_not_recreated(self):
        await self.run_sync(apply=True)
        self.server.books[0]["status"] = "deleted"
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"book_unavailable": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_failed_abs_fetch_never_exports_cached_history(self):
        await self.run_sync()
        self.server.fail_abs = True
        with self.assertRaises(SyncError):
            await self.run_sync(apply=True)
        self.assertEqual(self.server.writes, [])

    async def test_resumed_source_exports_only_the_added_interval(self):
        await self.run_sync(apply=True)
        self.server.sources = [source(updatedAt=1600000180000, timeListening=70, currentTime=240)]
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})
        second = self.server.sessions[1]
        self.assertEqual(second["seconds_at_start"], 180)
        self.assertEqual(second["seconds_at_end"], 240)
        self.assertEqual(second["paused_seconds"], 90)
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})
        self.server.sources = [source(updatedAt=1600000300000, timeListening=100, currentTime=300)]
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})
        self.assertEqual(self.server.sessions[-1]["seconds_at_start"], 240)
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})

    async def test_rewinds_and_counter_resets_are_held_for_review(self):
        await self.run_sync(apply=True)
        for changes in ({"timeListening": 10}, {"currentTime": 150}, {"startedAt": 1599999990000}):
            self.server.sources = [source(updatedAt=1600000180000, **changes)]
            self.assertEqual((await self.run_sync(apply=True))["summary"], {"needs_review": 1})
        self.assertEqual(len(self.server.sessions), 1)

    async def test_timeout_after_success_reconciles_without_reposting(self):
        self.server.timeout_after_post = True
        with self.assertRaises(SyncError):
            await self.run_sync(apply=True)
        self.assertEqual(self.db().execute("select status from exports").fetchone()[0], "pending")
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_timeout_before_arrival_is_never_retried(self):
        self.server.timeout_before_post = True
        with self.assertRaises(SyncError):
            await self.run_sync(apply=True)
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"needs_review": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_ambiguous_reconciliation_is_held_for_review(self):
        self.server.timeout_after_post = True
        with self.assertRaises(SyncError):
            await self.run_sync(apply=True)
        self.server.sessions.append({**self.server.sessions[0], "id": 2})
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"needs_review": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_verification_failure_does_not_repeat_import(self):
        self.server.verify_fail = True
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"verification_failed": 1})
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"needs_review": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_existing_manual_overlap_is_not_duplicated(self):
        self.server.sessions = [{**exporter.payload(source(), 42), "id": 7, "origin": "manual"}]
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"remote_overlap": 1})
        self.assertEqual(self.server.writes, [])

    async def test_two_overlapping_abs_sessions_are_not_both_sent(self):
        self.server.sources.append(source(id="source-2"))
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ready": 1, "remote_overlap": 1})
        self.assertEqual(len(self.server.sessions), 1)

    async def test_old_history_can_be_excluded(self):
        self.settings = dataclasses.replace(
            self.settings, import_since=datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"before_import_since": 1})
        self.assertEqual(self.server.writes, [])

    async def test_active_source_is_not_exported(self):
        self.server.sources = [source(updatedAt=datetime.now(timezone.utc).timestamp() * 1000)]
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"recent": 1})
        self.assertEqual(self.server.writes, [])

    async def test_audio_position_above_configured_length_is_blocked(self):
        self.server.books[0]["manual_total_seconds"] = 150
        self.assertEqual(
            (await self.run_sync(apply=True))["summary"], {"position_exceeds_length": 1}
        )
        self.assertEqual(self.server.writes, [])

    async def test_state_cannot_be_reused_for_another_abs_user(self):
        await self.run_sync()
        self.server.user_id = "user-2"
        with self.assertRaisesRegex(SyncError, "different credentials"):
            await self.run_sync(apply=True)
        self.assertEqual(self.server.writes, [])

    async def test_same_account_token_rotation_requires_explicit_preview_acceptance(self):
        await self.run_sync(apply=True)
        self.settings = dataclasses.replace(self.settings, readwise_token="replacement-test-token")
        self.server.readwise_token = "replacement-test-token"
        with self.assertRaisesRegex(SyncError, "different credentials"):
            await self.run_sync(apply=True)
        preview = await run_once(
            self.settings, accept_token_change=True, transport=self.transport, delay=0
        )
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_reconciliation_does_not_restore_deleted_remote_session(self):
        await self.run_sync(apply=True)
        self.server.sessions[0]["is_deleted"] = True
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})
        self.assertEqual(len(self.server.writes), 1)

    async def test_wrong_abs_user_in_a_session_aborts_before_any_write(self):
        self.server.sources = [source(userId="a-different-user")]
        with self.assertRaisesRegex(SyncError, "another user"):
            await self.run_sync(apply=True)
        self.assertEqual(self.server.writes, [])

    async def test_scope_lock_prevents_concurrent_preview_or_import(self):
        from abs_readwise_sync.storage import database

        with database(self.settings):
            with self.assertRaisesRegex(SyncError, "Another sync"):
                await self.run_sync(apply=True)
        self.assertEqual(self.server.requests, [])

    async def test_app_credential_is_optional_even_with_readonly_preview(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books = []
        self.assertEqual((await self.run_sync())["summary"], {"unmatched": 1})
        self.assertEqual(self.server.writes, [])
        self.assertFalse(any("/bookwise/api/" in r.url.path for r in self.server.requests))

    async def test_opted_in_app_automation_adds_book_sets_length_then_imports(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books = []
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})
        self.assertEqual(self.server.books[0]["manual_total_seconds"], 1000)
        self.assertIsNone(self.server.active)
        self.assertEqual(self.server.app_start_count, 1)
        await self.run_sync(apply=True)
        self.assertEqual(self.server.app_start_count, 1)

    async def test_app_automation_leaves_existing_length_alone(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = 1500
        await self.run_sync(apply=True)
        self.assertEqual(self.server.books[0]["manual_total_seconds"], 1500)
        self.assertEqual(self.server.app_start_count, 0)

    async def test_app_automation_does_not_interrupt_active_manual_session(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = None
        self.server.active = {"id": 15}
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"needs_length": 1})
        self.assertEqual(self.server.writes, [])

    async def test_app_automation_restores_finished_shelf(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0].update(status="finished", manual_total_seconds=None)
        await self.run_sync(apply=True)
        self.assertEqual(self.server.books[0]["status"], "finished")

    async def test_app_automation_never_repeats_an_uncertain_length_setup(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = None
        original = self.server.respond

        def respond(request):
            if request.url.path.endswith("/manual_sessions/start"):
                self.server.requests.append(request)
                raise httpx.ReadTimeout("private timeout", request=request)
            return original(request)

        self.transport = httpx.MockTransport(respond)
        with self.assertRaises(SyncError):
            await self.run_sync(apply=True)
        count = len(self.server.writes)
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"setup_needs_review": 1})
        self.assertEqual(len(self.server.writes), count)

    async def test_cancel_timeout_blocks_import_even_when_length_was_saved(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0]["manual_total_seconds"] = None
        original = self.server.respond

        def respond(request):
            if request.url.path.endswith("/manual_sessions/cancel"):
                self.server.requests.append(request)
                raise httpx.ReadTimeout("private timeout", request=request)
            return original(request)

        self.transport = httpx.MockTransport(respond)
        with self.assertRaises(SyncError):
            await self.run_sync(apply=True)
        self.assertEqual(self.server.books[0]["manual_total_seconds"], 1000)
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"setup_needs_review": 1})
        self.assertEqual(self.server.sessions, [])

    async def test_101_sessions_use_two_abs_pages_and_batches_of_ten(self):
        self.server.sources = [
            source(
                id=f"source-{i}",
                startedAt=1600000000000 + i * 100000,
                updatedAt=1600000060000 + i * 100000,
            )
            for i in range(101)
        ]
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 101})
        self.assertEqual(len(self.server.writes), 11)
        pages = [
            r.url.params["page"]
            for r in self.server.requests
            if r.url.path == "/api/me/listening-sessions"
        ]
        self.assertEqual(pages, ["0", "1"])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_get_follows_only_same_origin_canonical_redirects(self):
        seen = []

        def respond(request):
            seen.append(request)
            if not request.url.path.endswith("/"):
                return httpx.Response(
                    301, headers={"Location": "/bookwise/api/manual_sessions/ongoing/"}
                )
            return httpx.Response(200, json={"session": None})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            result = await AppClient(http, "test", delay=0).request(
                "GET", "/manual_sessions/ongoing"
            )
        self.assertEqual(result, {"session": None})
        self.assertEqual(len(seen), 2)

    async def test_app_post_does_not_follow_or_retry_redirect(self):
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(307, headers={"Location": "/bookwise/api/manual_sessions/start/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with self.assertRaises(SyncError):
                await AppClient(http, "test", delay=0).request(
                    "POST", "/manual_sessions/start", json={}
                )
        self.assertEqual(len(seen), 1)

    async def test_pagination_retains_embedded_cursor_and_rejects_foreign_origin(self):
        seen = []

        def respond(request):
            seen.append(request)
            if "cursor" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "results": [{"id": 1}],
                        "next": "https://readwise.io/books/api/v1/books?cursor=second&limit=1000",
                    },
                )
            return httpx.Response(200, json={"results": [{"id": 2}], "next": None})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            api = BooksClient(http, "test", delay=0)
            self.assertEqual([r["id"] for r in await api.list("/books")], [1, 2])
            self.assertEqual(seen[1].url.params["cursor"], "second")
            for url in (
                "https://evil.test/books/api/v1/books",
                "https://user@readwise.io/books/api/v1/books",
                "https://readwise.io:8443/books/api/v1/books",
            ):
                with self.assertRaises(SyncError):
                    await api.request("GET", url)
            self.assertEqual(len(seen), 2)

    async def test_redirects_do_not_receive_credentials(self):
        for client_type in (BooksClient, AppClient):
            seen = []

            def respond(request):
                seen.append(request)
                return httpx.Response(302, headers={"Location": "https://evil.test/"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                with self.assertRaises(SyncError):
                    await client_type(http, "secret", delay=0).request("GET", "/books")
            self.assertEqual(len(seen), 1)

    async def test_duplicate_record_or_missing_next_is_rejected(self):
        for data in ({"results": [{"id": 1}, {"id": 1}], "next": None}, {"results": []}):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
            ) as http:
                with self.assertRaises(SyncError):
                    await BooksClient(http, "test", delay=0).list("/books")

    async def test_error_response_never_exposes_tokens_or_body(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(401, text="secret-response"))
        ) as http:
            with self.assertRaises(SyncError) as caught:
                await BooksClient(http, "secret-token", delay=0).list("/books")
        self.assertNotIn("secret", str(caught.exception))

    async def test_abs_redirect_is_rejected_without_following(self):
        seen = []

        def respond(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://evil.test/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with self.assertRaises(SyncError):
                await ABSClient(http, "https://abs.test", "secret").fetch()
        self.assertEqual(len(seen), 1)


class MatchingTests(unittest.TestCase):
    def test_separate_abs_subtitle_is_not_lost(self):
        metadata = item_metadata(
            {
                "media": {
                    "metadata": {
                        "title": "A Book",
                        "subtitle": "Part Two",
                        "authors": [{"name": "Ada Example"}],
                    }
                }
            }
        )
        self.assertEqual(metadata["title"], "A Book: Part Two")
        self.assertFalse(
            same_work(metadata, {"title": "A Book: Part One", "author": "Ada Example"})
        )

    def test_edition_suffixes_match_but_different_subtitles_do_not(self):
        a = {"title": "A Book (Full-Cast Edition)", "author": "Ada Example"}
        self.assertTrue(same_work(a, {"title": "A Book", "author": "Ada Example"}))
        self.assertFalse(same_work(a, {"title": "A Book", "author": "Another Person"}))
        self.assertFalse(
            same_work(
                {**a, "title": "A Book: Part One"},
                {"title": "A Book: Part Two", "author": "Ada Example"},
            )
        )
        self.assertFalse(same_work({**a, "author": ""}, {"title": "A Book", "author": ""}))
