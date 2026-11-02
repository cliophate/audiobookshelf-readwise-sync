import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from abs_readwise_sync.config import Settings, SyncError, credential
from abs_readwise_sync.login import save_session
from abs_readwise_sync.permissions import access_message
from abs_readwise_sync.storage import database


class PermissionTests(unittest.TestCase):
    def test_diagnostics_do_not_include_file_contents_or_change_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("private-token-must-not-appear")
            path.chmod(0o600)
            message = access_message("reading READWISE_TOKEN_FILE", path)
            self.assertIn(str(path), message)
            self.assertIn("UID", message)
            self.assertIn("mode 600", message)
            self.assertIn("NAS ACLs", message)
            self.assertNotIn("private-token-must-not-appear", message)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses ordinary file permissions")
    def test_unreadable_credential_reports_its_setting_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("private-value")
            path.chmod(0o000)
            try:
                with patch.dict(os.environ, {"READWISE_TOKEN_FILE": str(path)}, clear=True):
                    with self.assertRaises(SyncError) as error:
                        credential("READWISE_TOKEN")
                self.assertIn("READWISE_TOKEN_FILE", str(error.exception))
                self.assertIn(str(path), str(error.exception))
                self.assertIn("mode 000", str(error.exception))
                self.assertNotIn("private-value", str(error.exception))
            finally:
                path.chmod(0o600)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses ordinary file permissions")
    def test_unwritable_data_directory_identifies_the_sync_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            path.chmod(0o500)
            try:
                with self.assertRaises(SyncError) as error:
                    with database(SimpleNamespace(data_dir=path)):
                        self.fail("Database should not open")
                self.assertIn(".sync.lock", str(error.exception))
                self.assertIn("mode 500", str(error.exception))
                self.assertEqual(list(path.iterdir()), [])
            finally:
                path.chmod(0o700)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses ordinary file permissions")
    def test_inaccessible_mappings_are_not_treated_as_an_absent_mapping_file(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir()
            path = parent / "mappings.json"
            path.write_text('{"item-1":"42"}')
            parent.chmod(0o000)
            try:
                with self.assertRaises(SyncError) as error:
                    Settings.mappings(SimpleNamespace(mappings_file=path))
                self.assertIn("mappings.json", str(error.exception))
                self.assertIn("mode 000", str(error.exception))
            finally:
                parent.chmod(0o700)

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses ordinary file permissions")
    def test_readonly_sqlite_database_has_storage_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(data_dir=Path(directory))
            with database(settings):
                pass
            path = settings.data_dir / "sync.sqlite"
            path.chmod(0o400)
            try:
                with self.assertRaises(SyncError) as error:
                    with database(settings) as con:
                        con.execute("insert into identity values('test','value')")
                self.assertIn("sync.sqlite", str(error.exception))
                self.assertIn("mode 400", str(error.exception))
            finally:
                path.chmod(0o600)

    def test_filesystem_forcing_777_cannot_receive_a_new_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("old-private-credential")
            with patch(
                "abs_readwise_sync.login.os.fstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o777),
            ):
                with self.assertRaisesRegex(SyncError, "did not honor mode 600") as error:
                    save_session(path, "new-private-credential", replace=True)
            self.assertEqual(path.read_text(), "old-private-credential")
            self.assertNotIn("new-private-credential", str(error.exception))
            self.assertEqual(list(path.parent.iterdir()), [path])
