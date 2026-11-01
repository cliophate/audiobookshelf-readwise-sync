import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import httpx

from abs_readwise_sync import cli
from abs_readwise_sync.config import SyncError
from abs_readwise_sync.login import login, save_session


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "readwise-session"
        self.messages = []
        self.requests = []
        self.start = {
            "device_code": "private-device-code",
            "link_code": "phone-code",
            "qr_url": "https://bookwise.com/device/phone-code",
        }

    def client(self, replies):
        def respond(request):
            self.requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.host, "readwise.io")
            self.assertTrue(request.url.path.startswith("/bookwise/api/device_auth/"))
            for header in ("authorization", "mobilesession", "cookie"):
                self.assertNotIn(header, request.headers)
            if request.url.path.endswith("/start"):
                body = json.loads(request.content)
                self.assertEqual(body["device_name"], "Audiobookshelf Readwise sync")
                self.assertTrue(body["device_id"])
                return httpx.Response(200, json=self.start)
            self.assertEqual(request.url.path, "/bookwise/api/device_auth/poll")
            self.assertEqual(json.loads(request.content), {"device_code": "private-device-code"})
            reply = replies.pop(0)
            return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def test_approval_saves_private_credential_without_touching_ledger(self):
        ledger = self.path.with_name("sync.sqlite")
        ledger.write_bytes(b"existing ledger")
        async with self.client(
            [{"status": "pending"}, {"status": "approved", "sessionid": "private-app-secret"}]
        ) as http:
            await login(http, self.path, interval=0, output=self.messages.append)
        self.assertEqual(json.loads(self.path.read_text()), {"sessionid": "private-app-secret"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(ledger.read_bytes(), b"existing ledger")
        output = "\n".join(self.messages)
        self.assertIn("bookwise://device/phone-code", output)
        self.assertNotIn("private-app-secret", output)
        self.assertNotIn("private-device-code", output)

    async def test_existing_file_requires_explicit_replacement_before_http(self):
        self.path.write_text("old-secret")
        async with self.client([]) as http:
            with self.assertRaisesRegex(SyncError, "--replace-session"):
                await login(http, self.path)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.path.read_text(), "old-secret")

    async def test_failed_approval_keeps_previous_credential(self):
        for status in ("expired", "denied", "unusable", "failed"):
            with self.subTest(status=status):
                self.path.write_text("old-secret")
                async with self.client([{"status": status}]) as http:
                    with self.assertRaisesRegex(SyncError, "expired or was denied"):
                        await login(http, self.path, replace=True, output=self.messages.append)
                self.assertEqual(self.path.read_text(), "old-secret")

    async def test_approved_replacement_is_private_and_complete(self):
        self.path.write_text("old-secret")
        self.path.chmod(0o644)
        async with self.client([{"sessionid": "new-secret"}]) as http:
            await login(http, self.path, replace=True, output=self.messages.append)
        self.assertEqual(json.loads(self.path.read_text()), {"sessionid": "new-secret"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".readwise-login-*")), [])

    async def test_unsafe_or_malformed_links_are_never_shown_or_opened(self):
        for url in (
            "https://evil.test/device/code",
            "http://bookwise.com/device/code",
            "https://bookwise.com/device/code?secret=bad",
            "https://bookwise.com/device/code#bad",
            "https://readwise.io/accounts/login",
            "https://user:password@bookwise.com/device/code",
            "https://[invalid/device/code",
            "https://bookwise.com/device/code\nInjected",
            None,
        ):
            with self.subTest(url=url):
                self.start["qr_url"] = url
                self.messages.clear()
                async with self.client([]) as http:
                    with self.assertRaises(SyncError):
                        await login(http, self.path, output=self.messages.append)
                self.assertEqual(self.messages, [])
                self.assertFalse(self.path.exists())

    async def test_invalid_session_or_unknown_status_is_not_saved(self):
        for reply in (
            {"sessionid": 123},
            {"sessionid": ""},
            {"sessionid": "secret\r\nHeader: value"},
            {"sessionid": "x" * 4097},
            {"sessionid": "non-ascii-\u00e9"},
            {"status": "approved"},
            {"status": "new-server-status"},
        ):
            with self.subTest(reply=reply):
                async with self.client([reply]) as http:
                    with self.assertRaises(SyncError):
                        await login(http, self.path, output=self.messages.append)
                self.assertFalse(self.path.exists())

    async def test_timeout_does_not_write_a_credential(self):
        now = 0

        async def sleep(seconds):
            nonlocal now
            now += seconds

        async with self.client([{"status": "pending"}]) as http:
            with self.assertRaisesRegex(SyncError, "timed out"):
                await login(
                    http,
                    self.path,
                    timeout=3,
                    sleep=sleep,
                    clock=lambda: now,
                    output=self.messages.append,
                )
        self.assertFalse(self.path.exists())

    async def test_http_failures_do_not_follow_redirects_or_print_private_bodies(self):
        for response in (
            httpx.Response(401, text="private-response"),
            httpx.Response(302, headers={"Location": "https://evil.test/private-response"}),
            httpx.Response(200, text="private-response"),
            httpx.Response(200, json=[]),
        ):
            async with self.client([response]) as http:
                with self.assertRaises(SyncError) as error:
                    await login(http, self.path, output=self.messages.append)
            self.assertNotIn("private-response", str(error.exception))
            self.assertFalse(self.path.exists())

    async def test_network_failure_is_sanitized(self):
        def fail(request):
            raise httpx.ConnectError("private-detail", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            with self.assertRaisesRegex(SyncError, "Device login request failed") as error:
                await login(http, self.path)
        self.assertNotIn("private-detail", str(error.exception))

    async def test_login_works_without_sync_tokens_or_a_valid_old_session(self):
        self.path.write_text("old-invalid-session")
        args = argparse.Namespace(login=True, replace_session=True)
        with (
            patch.dict(os.environ, {"READWISE_APP_SESSION_FILE": str(self.path)}, clear=True),
            patch.object(cli.Settings, "load", side_effect=AssertionError("sync config read")),
            patch.object(cli, "login") as mocked_login,
        ):
            self.assertEqual(await cli.run(args), 0)
        self.assertEqual(mocked_login.await_args.args[1], str(self.path))
        self.assertEqual(mocked_login.await_args.kwargs, {"replace": True})

    async def test_login_refuses_conflicting_inline_credential(self):
        with patch.dict(os.environ, {"READWISE_APP_SESSION": "private-secret"}, clear=True):
            with self.assertRaisesRegex(SyncError, "Unset READWISE_APP_SESSION") as error:
                await cli.run(argparse.Namespace(login=True))
        self.assertNotIn("private-secret", str(error.exception))


class CredentialFileTests(unittest.TestCase):
    def test_symlink_is_rejected_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("untouched")
            link = Path(directory) / "session"
            link.symlink_to(target)
            with self.assertRaisesRegex(SyncError, "symlink"):
                save_session(link, "secret", replace=True)
            self.assertEqual(target.read_text(), "untouched")

    def test_failed_atomic_replacement_keeps_old_file_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session"
            path.write_text("old-secret")
            with patch("abs_readwise_sync.login.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    save_session(path, "new-secret", replace=True)
            self.assertEqual(path.read_text(), "old-secret")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_login_flags_cannot_enable_sync_writes(self):
        for flags in (
            ["--login", "--apply"],
            ["--login", "--watch"],
            ["--login", "--json"],
            ["--login", "--issues"],
            ["--login", "--list-books"],
            ["--login", "--accept-token-change"],
            ["--replace-session"],
        ):
            with (
                self.subTest(flags=flags),
                patch("sys.argv", ["abs-readwise-sync", *flags]),
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(asyncio, "run", side_effect=AssertionError("operation started")),
            ):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)
