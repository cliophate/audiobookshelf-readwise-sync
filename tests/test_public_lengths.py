import sqlite3
import unittest

from abs_readwise_sync.public_lengths import DurationSetupError, ensure_length


class API:
    def __init__(self):
        self.book = dict(
            id=42,
            status="finished",
            manual_total_seconds=None,
            started_reading_at="2026-01-01T00:00:00Z",
            finished_reading_at="2026-01-02T00:00:00Z",
            rating=4,
            review="Keep my review",
            manual_total_pages=300,
        )
        self.calls = []
        self.rejection = False
        self.after_write = None
        self.response = None

    async def list(self, path, **params):
        assert path == "/books" and params == {"ids": "42"}
        self.calls.append(("GET", path))
        return [dict(self.book)]

    async def request(self, method, path, **params):
        assert method == "PATCH" and path == "/books"
        self.calls.append((method, params["json"]))
        if self.rejection:
            return {
                "results": [
                    {
                        "id": 42,
                        "status": "error",
                        "error": {"code": "invalid_value", "message": "PRIVATE RESPONSE"},
                    }
                ]
            }
        self.book.update(params["json"]["items"][0])
        if self.after_write:
            self.after_write(self.book)
        return self.response or {"results": [{"id": 42, "status": "updated", "error": None}]}


class PublicLengthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.api = API()

    async def test_public_update_changes_only_duration_and_reads_it_back(self):
        book = dict(self.api.book)
        original = dict(book)
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "updated")
        self.assertEqual(
            self.api.calls,
            [
                ("GET", "/books"),
                ("PATCH", {"items": [{"id": 42, "manual_total_seconds": 1000}]}),
                ("GET", "/books"),
            ],
        )
        self.assertEqual(book, {**original, "manual_total_seconds": 1000})
        self.assertEqual(
            self.con.execute("select status,session_id from length_attempts").fetchone(),
            ("confirmed", None),
        )
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "existing")
        self.assertEqual(len(self.api.calls), 3)

    async def test_all_invalid_saved_values_are_repaired(self):
        for old in (None, 0, -1, False, "1000", 0.5):
            with self.subTest(old=old):
                con = sqlite3.connect(":memory:")
                try:
                    api = API()
                    api.book["manual_total_seconds"] = old
                    self.assertEqual(await ensure_length(con, api, dict(api.book), 1000), "updated")
                    self.assertEqual(api.book["manual_total_seconds"], 1000)
                finally:
                    con.close()

    async def test_valid_manual_length_is_preserved(self):
        book = {**self.api.book, "manual_total_seconds": 2000}
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "existing")
        self.assertEqual(self.api.calls, [])

    async def test_rejection_can_retry_after_active_session_is_closed(self):
        self.api.rejection = True
        book = dict(self.api.book)
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "rejected")
        self.assertEqual(
            self.con.execute("select status from length_attempts").fetchone()[0], "api_rejected"
        )
        self.assertIsNone(book["manual_total_seconds"])
        self.api.rejection = False
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "updated")

    async def test_failed_readback_is_not_retried_or_treated_as_success(self):
        self.api.after_write = lambda book: book.update(manual_total_seconds=None)
        book = dict(self.api.book)
        with self.assertRaisesRegex(DurationSetupError, "did not save"):
            await ensure_length(self.con, self.api, book, 1000)
        calls = len(self.api.calls)
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "needs_review")
        self.assertEqual(len(self.api.calls), calls)

    async def test_editorial_changes_are_not_overwritten_by_restoration(self):
        self.api.after_write = lambda book: book.update(review="Changed elsewhere")
        with self.assertRaisesRegex(DurationSetupError, "details changed"):
            await ensure_length(self.con, self.api, dict(self.api.book), 1000)
        self.assertEqual(self.api.book["review"], "Changed elsewhere")
        self.assertEqual(sum(c[0] == "PATCH" for c in self.api.calls), 1)

    async def test_new_manual_duration_between_reads_is_preserved(self):
        old = dict(self.api.book)
        self.api.book["manual_total_seconds"] = 1500
        self.assertEqual(await ensure_length(self.con, self.api, old, 1000), "existing")
        self.assertEqual(old["manual_total_seconds"], 1500)
        self.assertEqual(self.api.calls, [("GET", "/books")])

    async def test_book_changes_before_write_hold_without_patch(self):
        old = dict(self.api.book)
        self.api.book["status"] = "currently_reading"
        self.assertEqual(await ensure_length(self.con, self.api, old, 1000), "needs_review")
        self.assertEqual(self.api.calls, [("GET", "/books")])

    async def test_legacy_incomplete_attempt_blocks_even_with_saved_duration(self):
        self.con.execute(
            "create table length_attempts(book_id primary key,length,status,session_id,previous_json)"
        )
        self.con.execute("insert into length_attempts values('42',1000,'cancel_pending','99','{}')")
        self.con.commit()
        book = {**self.api.book, "manual_total_seconds": 1000}
        self.assertEqual(await ensure_length(self.con, self.api, book, 1000), "needs_review")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(
            self.con.execute("select status,session_id from length_attempts").fetchone(),
            ("cancel_pending", "99"),
        )

    async def test_invalid_result_stays_pending_for_review(self):
        for response in (
            {"results": []},
            {"results": [{"id": 99, "status": "updated"}]},
            {
                "results": [
                    {"id": 42, "status": "updated", "error": {"message": "PRIVATE RESPONSE"}}
                ]
            },
        ):
            con = sqlite3.connect(":memory:")
            try:
                api = API()
                api.response = response
                with self.assertRaises(DurationSetupError) as error:
                    await ensure_length(con, api, dict(api.book), 1000)
                self.assertNotIn("PRIVATE RESPONSE", str(error.exception))
                self.assertEqual(
                    con.execute("select status from length_attempts").fetchone()[0], "api_pending"
                )
            finally:
                con.close()

    async def test_invalid_source_lengths_never_reach_the_api(self):
        for value in (0, False, None, "1000", 0.5, float("nan"), 360001):
            with self.assertRaises(DurationSetupError):
                await ensure_length(self.con, self.api, dict(self.api.book), value)
        self.assertEqual(self.api.calls, [])
