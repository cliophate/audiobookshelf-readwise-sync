import contextlib
import io
import json
import os
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from abs_readwise_sync import cli, exporter
from abs_readwise_sync.audio_safety import SuspectAudioRange, check_audio_range
from abs_readwise_sync.import_review import review_database, review_records
from test_sync import SyncFixture, source


class AudioSafetyTests(unittest.TestCase):
    def test_five_seconds_cannot_export_ten_hours(self):
        with self.assertRaises(SuspectAudioRange):
            exporter.payload(source(startTime=0, currentTime=35285, timeListening=5), 42)

    def test_high_speed_rounding_and_legitimate_relistening_remain_representable(self):
        for span, listened in ((160, 40), (165, 40), (80, 40), (1, 1)):
            with self.subTest(span=span):
                check_audio_range(source(startTime=0, currentTime=span, timeListening=listened))
        with patch.dict(os.environ, {"AUDIO_MAX_PLAYBACK_SPEED": "6"}):
            check_audio_range(source(startTime=0, currentTime=240))

    def test_excess_listening_nonfinite_and_invalid_configuration_fail_closed(self):
        with self.assertRaises(SuspectAudioRange):
            check_audio_range(source(timeListening=600))
        for value in ("nan", "inf", "0", "100", "nonsense"):
            with patch.dict(os.environ, {"AUDIO_MAX_PLAYBACK_SPEED": value}):
                with self.assertRaises(ValueError):
                    check_audio_range(source())


def record(export_id="one", baseline=None, payload=None, **changes):
    baseline = source() if baseline is None else baseline
    return dict(
        export_id=export_id,
        book_id="42",
        source_id="source-1",
        status="confirmed",
        session_id="7",
        baseline=baseline,
        payload=payload
        or dict(
            started_at="2020-09-13T12:26:40+00:00",
            ended_at="2020-09-13T12:27:40+00:00",
            seconds_at_start=100,
            seconds_at_end=180,
        ),
        **changes,
    )


class ImportReviewTests(unittest.TestCase):
    def test_distinct_ids_with_zero_starts_are_flagged(self):
        bad = source(startTime=0, currentTime=35285, timeListening=5)
        row = record(
            baseline=bad,
            payload={**record()["payload"], "seconds_at_start": 0, "seconds_at_end": 35285},
        )
        other = {
            **row,
            "export_id": "two",
            "source_id": "different-source",
            "baseline": {**bad, "id": "different-source"},
        }
        report = review_records([row, other])
        self.assertEqual(report["summary"], {"suspect_range": 2})
        self.assertFalse(report["sessions"][0]["automatic_repair"])
        self.assertEqual(report["sessions"][0]["listening_seconds"], 5)

    def test_cumulative_checkpoints_use_listening_deltas(self):
        initial = record()
        tail = record(
            export_id="tail",
            baseline=source(updatedAt=1600000120000, currentTime=280, timeListening=41),
            payload=dict(
                started_at="2020-09-13T12:27:40+00:00",
                ended_at="2020-09-13T12:28:40+00:00",
                seconds_at_start=180,
                seconds_at_end=280,
            ),
        )
        report = review_records([tail, initial])
        self.assertEqual(report["summary"], {"plausible": 1, "suspect_range": 1})
        self.assertEqual(report["sessions"][0]["listening_seconds"], 1)
        tail["baseline"]["timeListening"] = 90
        self.assertEqual(review_records([initial, tail])["summary"], {"plausible": 2})

    def test_legitimate_relisten_is_not_deduplicated(self):
        self.assertEqual(
            review_records([record(), record(export_id="two")])["summary"], {"plausible": 2}
        )

    def test_missing_baseline_is_reviewed_and_private_payload_fields_are_omitted(self):
        row = record(baseline={})
        row["payload"].update(caption="PRIVATE", device={"secret": "PRIVATE"})
        result = review_records([row])
        self.assertEqual(result["summary"], {"unverifiable_source": 1})
        self.assertNotIn("PRIVATE", json.dumps(result))


class ImportSafetyIntegrationTests(SyncFixture, unittest.IsolatedAsyncioTestCase):
    async def test_suspect_source_is_retained_and_other_sessions_continue(self):
        self.server.sources.append(source(id="bad", startTime=0, currentTime=900, timeListening=1))
        result = await self.run_sync(apply=True)
        self.assertEqual(result["summary"], {"ready": 1, "needs_review": 1})
        self.assertEqual(len(self.server.sessions), 1)
        issue = (
            self.db()
            .execute("select reason,data_json from source_issues where source_id='bad'")
            .fetchone()
        )
        self.assertIn("Suspect audio range", issue[0])
        self.assertEqual(json.loads(issue[1])["timeListening"], 1)

    async def test_existing_import_review_is_read_only_and_needs_no_credentials(self):
        await self.run_sync(apply=True)
        path = self.settings.data_dir / "sync.sqlite"
        before = path.read_bytes()
        writes = len(self.server.writes)
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"DATA_DIR": str(self.settings.data_dir)}, clear=True),
            patch.object(cli.httpx, "AsyncClient", side_effect=AssertionError("No HTTP")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(await cli.run(SimpleNamespace(review_imports=True)), 0)
        self.assertEqual(json.loads(output.getvalue())["summary"], {"plausible": 1})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.server.writes), writes)

    async def test_review_does_not_create_a_missing_database(self):
        path = self.settings.data_dir / "sync.sqlite"
        self.assertEqual(review_database(path)["scanned_exports"], 0)
        self.assertFalse(path.exists())

    async def test_review_supports_legacy_ledgers_without_checkpoint_table(self):
        path = self.settings.data_dir / "old.sqlite"
        with sqlite3.connect(path) as con:
            con.execute("create table exports(source_id,book_id,payload_json,status,session_id)")
            con.execute(
                "insert into exports values(?,?,?,?,?)",
                ("one", "42", json.dumps(record()["payload"]), "confirmed", "7"),
            )
        self.assertEqual(review_database(path)["summary"], {"unverifiable_source": 1})

    def test_review_cannot_be_combined_with_remote_writes(self):
        for flag in ("--apply", "--watch", "--login", "--audit", "--issues"):
            args = ["sync", "--review-imports", flag] + (["item-1"] if flag == "--audit" else [])
            with patch("sys.argv", args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
            self.assertEqual(error.exception.code, 2)
