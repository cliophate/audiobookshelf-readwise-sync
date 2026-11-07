"""Matching regressions and history gates use synthetic sources and HTTP mocks."""

import contextlib
import dataclasses
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from abs_readwise_sync import cli, exporter
from abs_readwise_sync.attention import render
from abs_readwise_sync.config import SyncError
from abs_readwise_sync.matching import (
    author_names,
    catalog_candidates,
    compare,
    features,
    item_metadata,
    same_work,
)
from abs_readwise_sync.review_commands import save_mapping
from abs_readwise_sync.storage import database
from abs_readwise_sync.sync import resolve_book
from test_sync import SyncFixture, source
from test_shelves_and_matching import ShelfFixture


def book(title, author="Ada Example", **extra):
    return {"id": 42, "title": title, "author": author, "status": "currently_reading", **extra}


class MatcherTests(unittest.TestCase):
    def test_report_examples_keep_wrong_volumes_and_split_adaptations_blocked(self):
        rows = json.loads((Path(__file__).parent / "fixtures/matching_examples.json").read_text())
        self.assertEqual(len(rows), 21)
        self.assertEqual(sum(len(r["candidates"]) for r in rows), 32)
        eligible = []
        for row in rows:
            for candidate in row["candidates"]:
                if same_work(row["source"], candidate):
                    eligible.append((row["source"]["title"], candidate["id"]))
        self.assertEqual(
            eligible,
            [
                ("48 Laws of Power", 1001),
                ("Above All Things", 1004),
                ("Barging In", 1014),
                ("Protocols", 1023),
            ],
        )
        skills = next(r for r in rows if r["source"]["title"].startswith("All the Skills 4"))
        candidates = catalog_candidates(skills["source"], skills["candidates"])
        self.assertEqual(
            sum(c["status"] == "review" and c["volumes"] == ["4"] for c in candidates), 2
        )
        self.assertTrue(
            all(
                "Book volume numbers conflict." in c["reasons"]
                for c in candidates
                if c["status"] == "rejected"
            )
        )

    def test_punctuation_accents_articles_and_initial_spacing(self):
        for a, b, author_a, author_b in [
            ("Hunter’s Way", "Hunter's Way", "Gerri Hill", "Gerri Hill"),
            ("48 Laws of Power", "The 48 Laws of Power", "Robert Greene", "Robert Greene"),
            ("Café", "CAFE", "M.J. Duncan", "MJ Duncan"),
            ("Protocols", "Protocols", "Andrew D. Huberman Ph.D.", "Andrew D. Huberman"),
            ("Example", "Example", "Hitoma Iruma", "Iruma, Hitoma"),
            ("Example", "Example", "John Townsend, Henry Cloud", "Henry Cloud and John Townsend"),
            ("Example", "Example", "John Townsend&Henry Cloud", "HENRY CLOUD AND JOHN TOWNSEND"),
        ]:
            with self.subTest(a=a, author=author_a):
                self.assertTrue(same_work(book(a, author_a), book(b, author_b)))

    def test_structured_authors_and_contributor_roles(self):
        source_book = book(
            "Example",
            authors=[
                {"name": "Iruma, Hitoma", "role": "author"},
                {"name": "A Narrator", "role": "narrator"},
            ],
        )
        self.assertTrue(same_work(source_book, book("Example", "Hitoma Iruma")))
        self.assertFalse(same_work(book("Example", "A Narrator"), source_book))
        self.assertTrue(
            same_work(book("Example", "Ada Example, Bob Reader - foreword"), book("Example"))
        )
        self.assertEqual(
            author_names({"authors": [{"name": "A Narrator", "role": "narrator"}]}), frozenset()
        )
        self.assertFalse(
            same_work(book("Example", "Mary Ann Smith"), book("Example", "Maryann Smith"))
        )
        self.assertFalse(same_work(book("Example", "Ann A Smith"), book("Example", "Anna Smith")))
        self.assertFalse(same_work(book("Example", "Jae and Cher"), book("Example", "Cher Jae")))
        self.assertTrue(same_work(book("Example", "ADA EXAMPLE"), book("Example", "Ada Example")))

    def test_subtitle_placement_and_conflicts(self):
        a = book("Barging In", subtitle="A Sapphic Age-Gap Romance")
        self.assertTrue(same_work(a, book("Barging In (A Sapphic Age Gap Romance)")))
        self.assertTrue(same_work(a, book("Barging In: A Sapphic Age Gap Romance")))
        self.assertTrue(same_work(a, book("Barging In")))
        self.assertFalse(same_work(a, book("Barging In: A History of Shipping")))
        self.assertFalse(same_work(book("Shared Title: Biology"), book("Shared Title: Astronomy")))
        self.assertFalse(
            same_work(book("Shared Title", subtitle="Biology"), book("Shared Title: Astronomy"))
        )
        self.assertFalse(
            same_work(book("Dune: Messiah", "Frank Herbert"), book("Dune", "Frank Herbert"))
        )
        self.assertFalse(
            same_work(book("Dune", "Frank Herbert"), book("Dune: Messiah", "Frank Herbert"))
        )

    def test_editions_parts_collections_adaptations_and_bare_volumes(self):
        for a, b in [
            ("Example (3rd Edition)", "Example (2nd Edition)"),
            ("Example, Updated and Expanded Edition", "Example"),
            ("Example 17", "Example 16"),
            ("Example 17", "Example"),
            ("Example (Part 1 of 2)", "Example (Part 2 of 2)"),
            ("Example (Part 1 of 2)", "Example"),
            ("Example (2 of 2)", "Example"),
            ("Example (Dramatized Adaptation)", "Example"),
            ("Example (Full Cast)", "Example"),
            ("Example Box Set", "Example"),
            ("Example 1 - 100", "Example 1"),
        ]:
            with self.subTest(a=a, b=b):
                self.assertFalse(same_work(book(a), book(b)))
        self.assertTrue(same_work(book("Example 4"), book("Example, Volume IV")))
        self.assertTrue(same_work(book("Example 17"), book("Example, Volume XVII")))
        self.assertFalse(same_work(book("Example, Volume IIV"), book("Example, Volume IIV")))
        self.assertTrue(same_work(book("Example (Unabridged)"), book("Example")))
        self.assertEqual(
            features(book("Example 4", series=[{"sequence": "5"}]))["volumes"],
            frozenset({"4", "5"}),
        )
        self.assertFalse(
            same_work(book("Example 4", series=[{"sequence": "5"}]), book("Example 4"))
        )

    def test_identifiers_validate_normalize_support_names_and_preserve_conflicts(self):
        a = book("Title A", "Author A", isbn="0-306-40615-2")
        b = book("Title B", "Author B", product={"isbn": "9780306406157"})
        self.assertTrue(same_work(a, b))
        self.assertFalse(same_work(a, {**b, "isbn": "9780141439518"}))
        self.assertFalse(
            same_work(
                book("One", "Author A", isbn="garbage"), book("Two", "Author B", isbn="garbage")
            )
        )
        self.assertFalse(
            same_work(book("One", "Author A", asin="bad"), book("Two", "Author B", asin="bad"))
        )
        self.assertFalse(
            same_work(book("Example 1", asin="B012345678"), book("Example 2", asin="B012345678"))
        )
        self.assertFalse(
            same_work(
                book("Example", isbn="9780306406157", asin="B012345678"),
                book("Example", isbn="9780306406157", asin="B087654321"),
            )
        )
        data = item_metadata(
            {
                "media": {
                    "metadata": {"title": "Example", "isbn": "9780306406157", "asin": "B012345678"}
                }
            }
        )
        self.assertEqual(data["isbn"], "9780306406157")
        self.assertEqual(data["asin"], "B012345678")

    def test_fuzzy_candidates_never_choose_a_destination(self):
        result = compare(book("A Quite Distinct Long Title"), book("A Quite Different Long Title"))
        self.assertTrue(result["relevant"])
        self.assertFalse(result["eligible"])
        self.assertIn("Primary titles differ.", result["reasons"])


class HistoryTests(SyncFixture, unittest.IsolatedAsyncioTestCase):
    def new_match(self):
        self.server.books[0]["title"] = "Example Book"

    async def test_improved_match_requires_exact_history_preview_before_any_export(self):
        self.new_match()
        preview = await self.run_sync()
        self.assertEqual(preview["summary"], {"history_needs_review": 1})
        review = preview["books"][0]["history_review"]
        self.assertEqual(review["sessions"], 1)
        self.assertEqual(preview["books"][0]["sessions"][0]["seconds_at_start"], 100)
        self.assertEqual(self.server.writes, [])
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"history_needs_review": 1})
        self.assertEqual(self.server.writes, [])
        self.approve_history(preview)
        self.assertEqual((await self.run_sync(apply=True))["result"], {"confirmed": 1})
        before = self.db().execute("select * from checkpoints").fetchall()
        self.server.sessions = []
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"existing": 1})
        self.assertEqual(self.db().execute("select * from checkpoints").fetchall(), before)
        self.assertEqual(len(self.server.writes), 1)

    async def test_newly_resolved_previously_unmatched_exact_title_is_held(self):
        self.server.books = []
        await self.run_sync()
        self.server.books = [book("An Example Book", manual_total_seconds=1000)]
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"history_needs_review": 1})
        self.assertEqual(self.server.writes, [])

    async def test_approval_does_not_cover_added_or_changed_old_sources(self):
        self.new_match()
        preview = await self.run_sync()
        self.approve_history(preview)
        self.server.sources[0] = source(updatedAt=1600000180000, timeListening=70, currentTime=240)
        self.server.sources.append(
            source(id="older-2", startedAt=1500000000000, updatedAt=1500000060000)
        )
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"history_needs_review": 2})
        self.assertEqual(self.server.sessions, [])
        self.assertNotEqual(
            result["books"][0]["history_review"]["token"],
            preview["books"][0]["history_review"]["token"],
        )

    async def test_stale_cached_source_cannot_be_approved(self):
        self.new_match()
        preview = await self.run_sync()
        self.server.sources[0] = source(updatedAt=1600000180000, timeListening=70, currentTime=240)
        await self.run_sync()
        with self.assertRaisesRegex(SyncError, "changed after preview"):
            self.approve_history(preview)
        self.assertEqual(
            self.db().execute("select count(*) from history_approvals").fetchone()[0], 0
        )

    async def test_destination_change_invalidates_approval(self):
        self.new_match()
        preview = await self.run_sync()
        self.settings.mappings_file.write_text(json.dumps({"item-1": 43}))
        with self.assertRaisesRegex(SyncError, "destination changed"):
            self.approve_history(preview)

    async def test_new_live_sessions_can_sync_without_replaying_held_history(self):
        self.new_match()
        await self.run_sync()
        future_start = 2000000000000
        self.server.sources.append(
            source(id="new-live", startedAt=future_start, updatedAt=future_start + 60000)
        )
        # Move the boundary into the past of the new source while keeping it settled.
        with database(self.settings) as con:
            con.execute("update history_boundaries set cutoff=1700000000")
            con.commit()
        # Use a realistic source newer than that boundary and older than the quiet period.
        self.server.sources[-1] = source(
            id="new-live", startedAt=1750000000000, updatedAt=1750000060000
        )
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"history_needs_review": 1, "ready": 1})
        self.assertEqual(len(self.server.sessions), 1)
        self.assertEqual(
            self.server.sessions[0]["started_at"],
            exporter.payload(self.server.sources[-1], 42)["started_at"],
        )
        again = await self.run_sync(apply=True)
        self.assertEqual(again["summary"], {"history_needs_review": 1, "existing": 1})

    async def test_saved_binding_and_explicit_mappings_survive_new_rules(self):
        await self.run_sync(apply=True)
        before = self.db().execute("select * from exports").fetchall()
        self.server.books[0]["title"] = "Completely changed title"
        self.server.books.append({**self.server.books[0], "id": 43})
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"existing": 1})
        self.assertEqual(result["books"][0]["readwise_book_id"], "42")
        self.assertEqual(self.db().execute("select * from exports").fetchall(), before)
        self.settings.mappings_file.write_text(json.dumps({"item-1": 43}))
        changed = await self.run_sync(apply=True)
        self.assertEqual(changed["summary"], {"mapping_changed": 1})
        self.assertIn("42", changed["books"][0]["matching"]["reason"])
        self.assertIn("43", changed["books"][0]["matching"]["reason"])

    async def test_possible_existing_match_blocks_optional_book_creation(self):
        self.settings = dataclasses.replace(self.settings, app_session="test-app-session")
        self.server.books[0]["title"] = "An Example Book (3rd Edition)"
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"matching_review": 1})
        self.assertEqual(self.server.writes, [])
        self.assertFalse(any("search_products" in r.url.path for r in self.server.requests))
        candidate = result["books"][0]["matching"]["candidates"][0]
        self.assertIn("Edition information differs or is missing.", candidate["reasons"])

    async def test_duration_setup_waits_for_history_approval_then_revalidates(self):
        self.new_match()
        self.server.books[0]["manual_total_seconds"] = None
        preview = await self.run_sync(apply=True)
        self.assertEqual(preview["summary"], {"history_needs_review": 1})
        self.assertEqual(self.server.writes, [])
        self.approve_history(preview)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertEqual(self.server.length_update_count, 1)

    async def test_approved_history_keeps_existing_continuation_checks(self):
        self.new_match()
        self.approve_history(await self.run_sync())
        await self.run_sync(apply=True)
        self.server.sources = [source(updatedAt=1600000180000, timeListening=70, currentTime=240)]
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertEqual(self.server.sessions[-1]["seconds_at_start"], 180)
        self.server.sources = [source(updatedAt=1600000300000, timeListening=80, currentTime=200)]
        self.assertEqual((await self.run_sync(apply=True))["summary"], {"needs_review": 1})
        self.assertEqual(len(self.server.sessions), 2)

    async def test_mapping_and_candidate_cli_only_read_remote_books(self):
        await self.run_sync()
        for args in (SimpleNamespace(candidates="item-1"), SimpleNamespace(map=["item-1", "42"])):
            output = io.StringIO()
            http = httpx.AsyncClient(transport=self.transport)
            with (
                patch.object(cli.Settings, "load", return_value=self.settings),
                patch.object(cli.httpx, "AsyncClient", return_value=http),
                contextlib.redirect_stdout(output),
            ):
                await cli.run(args)
            self.assertEqual(self.server.writes, [])
            data = json.loads(output.getvalue())
            self.assertEqual(data["abs_item_id"], "item-1")
        self.assertEqual(json.loads(self.settings.mappings_file.read_text()), {"item-1": "42"})

    async def test_deleted_target_and_split_part_mapping_are_rejected(self):
        await self.run_sync()
        with database(self.settings) as con:
            with self.assertRaisesRegex(SyncError, "deleted"):
                save_mapping(
                    con, self.settings, "item-1", {**self.server.books[0], "status": "deleted"}
                )
            item = json.loads(
                con.execute("select data_json from source_items where id='item-1'").fetchone()[0]
            )
            item["media"]["metadata"]["title"] = "An Example Book (Part 1 of 2)"
            con.execute(
                "update source_items set data_json=? where id='item-1'", (json.dumps(item),)
            )
            con.commit()
            with self.assertRaisesRegex(SyncError, "progress policy"):
                save_mapping(con, self.settings, "item-1", self.server.books[0])
            self.assertEqual(
                resolve_book(
                    con, "item-1", item_metadata(item), self.server.books, {"item-1": "42"}
                )[1],
                "matching_review",
            )
        self.assertFalse(self.settings.mappings_file.exists())

    async def test_duplicate_active_identifiers_and_partial_candidates_are_ambiguous(self):
        self.server.books.append(
            {**self.server.books[0], "id": 43, "title": "An Example Book (Revised Edition)"}
        )
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ambiguous": 1})
        self.assertEqual(self.server.writes, [])
        with database(self.settings) as con:
            self.assertEqual(
                resolve_book(
                    con,
                    "item-1",
                    book("An Example Book", isbn="9780306406157"),
                    [
                        book("An Example Book", isbn="9780306406157"),
                        book("An Example Book", id=43, isbn="9780306406157"),
                    ],
                    {},
                )[1],
                "ambiguous",
            )

    async def test_mapping_command_preserves_other_mappings_and_blocks_reassignment(self):
        await self.run_sync()
        self.settings.mappings_file.write_text(json.dumps({"unrelated": 999}))
        with database(self.settings) as con:
            result = save_mapping(con, self.settings, "item-1", self.server.books[0])
        self.assertEqual(result["remote_writes"], 0)
        self.assertEqual(
            json.loads(self.settings.mappings_file.read_text()),
            {"unrelated": "999", "item-1": "42"},
        )
        self.assertEqual(self.settings.mappings_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.server.writes, [])
        preview = await self.run_sync()
        self.assertEqual(preview["summary"], {"history_needs_review": 1})
        self.approve_history(preview)
        await self.run_sync(apply=True)
        with (
            database(self.settings) as con,
            self.assertRaisesRegex(SyncError, "history will not be reassigned"),
        ):
            save_mapping(con, self.settings, "item-1", {**self.server.books[0], "id": 43})

    async def test_approve_cli_makes_no_http_requests(self):
        self.new_match()
        preview = await self.run_sync()
        args = SimpleNamespace(
            approve_history=["item-1", preview["books"][0]["history_review"]["token"]]
        )
        output = io.StringIO()
        with (
            patch.object(cli.Settings, "load", return_value=self.settings),
            patch.object(cli.httpx, "AsyncClient", side_effect=AssertionError("No HTTP")),
            contextlib.redirect_stdout(output),
        ):
            await cli.run(args)
        self.assertEqual(json.loads(output.getvalue())["approved_sessions"], 1)

    async def test_list_books_keeps_product_identifiers(self):
        self.server.books[0]["product"] = {"isbn": "9780306406157", "asin": "B012345678"}
        http = httpx.AsyncClient(transport=self.transport)
        output = io.StringIO()
        with (
            patch.object(cli.Settings, "load", return_value=self.settings),
            patch.object(cli.httpx, "AsyncClient", return_value=http),
            contextlib.redirect_stdout(output),
        ):
            await cli.run(SimpleNamespace(issues=False, list_books=True))
        self.assertEqual(json.loads(output.getvalue())[0]["product"]["isbn"], "9780306406157")


class LogTests(SyncFixture, unittest.IsolatedAsyncioTestCase):
    async def test_default_logs_explain_matches_and_deduplicate_unchanged_issues(self):
        self.server.books[0]["author"] = "Another Author"
        result = await self.run_sync()
        first, state = render(result)
        text = "\n".join(first)
        self.assertIn("Candidate 42", text)
        self.assertIn("Primary authors are missing or differ.", text)
        self.assertNotIn("confidence", text)
        second, _ = render(result, state)
        self.assertNotIn("Candidate 42", "\n".join(second))
        self.assertIn("remain unresolved", "\n".join(second))
        explicit, _ = render(result, state, show_all=True)
        self.assertIn("Candidate 42", "\n".join(explicit))
        self.server.books[0]["title"] = "An Example Book (Revised Edition)"
        changed, _ = render(await self.run_sync(), state)
        self.assertIn("Edition information differs", "\n".join(changed))

    async def test_existing_sessions_and_unchanged_shelves_are_quiet_but_ready_is_visible(self):
        ready, _ = render(await self.run_sync())
        self.assertIn("'ready': 1", "\n".join(ready))
        await self.run_sync(apply=True)
        result = await self.run_sync()
        result["books"][0]["shelf"] = {
            "current": "currently_reading",
            "desired": "currently_reading",
            "status": "unchanged",
        }
        lines, _ = render(result)
        self.assertNotIn("An Example Book", "\n".join(lines))
        self.assertNotIn("Shelf:", "\n".join(lines))

    async def test_remote_overlap_names_the_existing_session(self):
        self.server.sessions = [{**exporter.payload(source(), 42), "id": 77}]
        result = await self.run_sync()
        self.assertIn("Readwise session 77", result["books"][0]["sessions"][0]["reason"])

    async def test_same_issue_on_another_source_is_printed_again(self):
        self.server.books[0]["author"] = "Another Author"
        first = await self.run_sync()
        _, saved = render(first)
        self.server.sources[0] = source(id="different-source")
        lines, _ = render(await self.run_sync(), saved)
        self.assertIn("different-source", "\n".join(lines))
        self.assertNotIn("unchanged attention", "\n".join(lines))


class ShelfHistoryTests(ShelfFixture, unittest.IsolatedAsyncioTestCase):
    async def test_matching_and_history_review_block_shelf_changes(self):
        self.enable()
        self.server.books[0]["title"] = "Example Book"
        result = await self.run_sync(apply=True)
        self.assertEqual(result["shelf_summary"], {"needs_review": 1})
        self.assertEqual(result["books"][0]["shelf"]["desired"], "finished")
        self.assertEqual(self.server.writes, [])
        self.approve_history(result)
        result = await self.run_sync(apply=True)
        self.assertEqual(result["result"], {"confirmed": 1})
        self.assertEqual(result["shelf_summary"], {"confirmed": 1})
