import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abs_readwise_sync.config import Settings, SyncError, credential


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "AUDIOBOOKSHELF_URL": "https://abs.test",
                "AUDIOBOOKSHELF_TOKEN": "test-abs",
                "READWISE_TOKEN": "test-readwise",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_no_app_session_is_required(self):
        self.assertEqual(Settings.load().app_session, "")
        self.assertFalse(Settings.load().sync_shelves)
        self.assertEqual(Settings.load().abs_item_ids, ())

    def test_shelf_sync_and_item_selection_require_valid_explicit_settings(self):
        with patch.dict(os.environ, {"SYNC_SHELVES": "true", "ABS_ITEM_IDS": "a, b,a"}):
            settings = Settings.load()
            self.assertTrue(settings.sync_shelves)
            self.assertEqual(settings.abs_item_ids, ("a", "b"))
        for change in (
            {"SYNC_SHELVES": "maybe"},
            {"ABS_ITEM_IDS": "a,,b"},
            {"ABS_ITEM_IDS": "a/b"},
            {"ABS_ITEM_IDS": "an item"},
        ):
            with self.subTest(change=change), patch.dict(os.environ, change):
                with self.assertRaises(SyncError):
                    Settings.load()

    def test_secret_file_and_json_session_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "session"
            file.write_text(json.dumps({"sessionid": "test-session"}))
            with patch.dict(os.environ, {"READWISE_APP_SESSION_FILE": str(file)}):
                self.assertEqual(Settings.load().app_session, "test-session")

    def test_token_file_missing_or_conflicting_value_fails_without_printing_secret(self):
        with patch.dict(os.environ, {"READWISE_TOKEN_FILE": "/does/not/exist"}):
            with self.assertRaisesRegex(SyncError, "either"):
                Settings.load()
        os.environ.pop("READWISE_TOKEN")
        with patch.dict(os.environ, {"READWISE_TOKEN_FILE": "/does/not/exist"}):
            with self.assertRaisesRegex(SyncError, "Cannot read"):
                Settings.load()

    def test_bad_dates_urls_and_intervals_fail_early(self):
        for change in (
            {"IMPORT_SINCE": "2026-10-01"},
            {"PUBLISH_FROM": "yesterday"},
            {"TIMEZONE": "not-a-timezone"},
            {"POLL_MINUTES": "0"},
            {"SETTLE_MINUTES": "no"},
            {"AUDIOBOOKSHELF_URL": "https://user:password@abs.test"},
            {"AUDIOBOOKSHELF_URL": "https://abs.test?key=private"},
        ):
            with self.subTest(change=change), patch.dict(os.environ, change):
                with self.assertRaises(SyncError):
                    Settings.load()

    def test_invalid_app_json_is_rejected(self):
        for value in ('{"other": "value"}', '{"sessionid": 3}', '{"sessionid": "has whitespace"}'):
            with patch.dict(os.environ, {"READWISE_APP_SESSION": value}):
                with self.assertRaises(SyncError):
                    credential("READWISE_APP_SESSION", required=False)

    def test_mapping_file_is_optional_but_cannot_silently_contain_bad_ids(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"DATA_DIR": directory}),
        ):
            settings = Settings.load()
            self.assertEqual(settings.mappings(), {})
            for data in ([42], {"item": True}, {"item": 0}, {"item": "not-id"}):
                settings.mappings_file.write_text(json.dumps(data))
                with self.assertRaises(SyncError):
                    settings.mappings()


if __name__ == "__main__":
    unittest.main()
