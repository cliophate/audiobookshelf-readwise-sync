"""Report filesystem access failures without exposing file contents."""

import json
import errno
import os
from pathlib import Path
import stat


def access_message(operation, path):
    path = Path(path)

    def describe(candidate):
        name = json.dumps(str(candidate))
        try:
            info = candidate.stat()
        except OSError:
            return name + " metadata unavailable"
        return (
            f"{name} owner UID {info.st_uid}:GID {info.st_gid}, "
            f"mode {stat.S_IMODE(info.st_mode):03o}"
        )

    return (
        f"Cannot access local storage while {operation}. "
        f"Running as UID {os.getuid()}:GID {os.getgid()}. "
        f"Path {describe(path)}. Parent {describe(path.parent)}. "
        "Check Docker PUID/PGID, bind-mount ownership, NAS ACLs and read-only mounts. "
        "Keep credential files at 600; do not solve this with chmod 777. "
        "No permissions were changed."
    )


def local_call(operation, path, callback):
    # Import here so configuration can use access_message without a circular import.
    from .config import SyncError

    try:
        return callback()
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
            raise SyncError(access_message(operation, path)) from None
        raise
