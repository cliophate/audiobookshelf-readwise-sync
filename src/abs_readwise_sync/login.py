"""Optional app login through Readwise Books device approval."""

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import httpx

from .clients import APP_BASE
from .config import SyncError
from .permissions import access_message


def check_destination(path, replace):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise SyncError("The session destination must be a regular file, never a symlink.")
    if not replace:
        raise SyncError("A session file already exists. Use --replace-session to replace it.")


def save_session(path, session, replace):
    """Publish a complete mode-600 file without reading the previous credential."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    check_destination(path, replace)
    fd, temporary = tempfile.mkstemp(prefix=".readwise-login-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            require_private_mode(stream.fileno(), path)
            stream.write(json.dumps({"sessionid": session}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # A competing login must not overwrite a file created since the check.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_private_mode(fd, path):
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
        raise SyncError(
            "The filesystem did not honor mode 600 for the credential file. "
            "Use a private NAS folder or mount that enforces owner-only permissions. "
            + access_message("checking private credential storage", path)
        )


async def request(http, path, body):
    try:
        response = await http.post(
            APP_BASE + "/device_auth/" + path,
            json=body,
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise SyncError("Device login request failed. Run --login again.") from None
    if response.status_code != 200:
        raise SyncError(
            f"Device login {path} returned HTTP {response.status_code}. Run --login again."
        )
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise SyncError("Device login returned an invalid response.") from None


async def login(
    http,
    path,
    *,
    replace=False,
    timeout=600,
    interval=3,
    output=print,
    sleep=asyncio.sleep,
    clock=time.monotonic,
):
    path = Path(path)
    check_destination(path, replace)
    # Check directory access before asking someone to approve a login.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryFile(dir=path.parent) as probe:
        require_private_mode(probe.fileno(), path)
    start = await request(
        http,
        "start",
        {"device_name": "Audiobookshelf Readwise sync", "device_id": str(uuid.uuid4())},
    )
    code = start.get("device_code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", code):
        raise SyncError("Device login returned an invalid device code.")
    url = start.get("qr_url")
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise SyncError("Device login returned an invalid approval link.")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise SyncError("Device login returned an invalid approval link.") from None
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ("bookwise.com", "readwise.io")
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/device/[A-Za-z0-9_-]{1,256}", parsed.path)
    ):
        raise SyncError("Device login returned an unexpected approval link.")
    link_code = parsed.path.rsplit("/", 1)[1]
    output("Open this link on the phone signed into your Readwise Books account:")
    output(url)
    output("If it opens the download page, try this direct app link on that phone:")
    output("bookwise://device/" + link_code)
    output("Approve the device named 'Audiobookshelf Readwise sync'.")
    output("Keep these temporary approval links private. Waiting up to 10 minutes...")
    deadline = clock() + timeout
    while clock() < deadline:
        data = await request(http, "poll", {"device_code": code})
        status = data.get("status")
        if status in ("expired", "denied", "unusable", "failed"):
            raise SyncError("Device login expired or was denied. Run --login again.")
        session = data.get("sessionid")
        if session is not None:
            if (
                not isinstance(session, str)
                or not session
                or len(session) > 4096
                or not session.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in session)
            ):
                raise SyncError("Device login returned an invalid session credential.")
            save_session(path, session, replace)
            output(f"Saved the app credential to {path} with permissions 600.")
            output("The credential was not printed. Keep using your regular API token for imports.")
            return
        if status != "pending":
            raise SyncError("Device login returned an unexpected approval status.")
        await sleep(min(interval, max(0, deadline - clock())))
    raise SyncError("Device approval timed out. Run --login again for a fresh link.")
