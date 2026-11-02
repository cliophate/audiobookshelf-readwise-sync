"""The source cache and submission ledger belong to one pair of accounts."""

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager

from .config import SyncError
from .exporter import ensure_schema
from .audio_sources import ensure_issues, save_issue, source_key
from .permissions import access_message, local_call


@contextmanager
def database(settings):
    try:
        with _database(settings) as con:
            yield con
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 255 in (
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_READONLY,
        ):
            raise SyncError(
                access_message(
                    "opening or writing the sync database", settings.data_dir / "sync.sqlite"
                )
            ) from None
        raise


@contextmanager
def _database(settings):
    local_call(
        "creating the sync data directory",
        settings.data_dir,
        lambda: settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700),
    )
    lock_path = settings.data_dir / ".sync.lock"
    with local_call("opening the sync lock", lock_path, lambda: lock_path.open("a")) as lock:
        try:
            local_call(
                "locking the sync data directory",
                lock_path,
                lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB),
            )
        except BlockingIOError:
            raise SyncError(
                "Another sync or preview is running. Try again after it finishes."
            ) from None
        con = sqlite3.connect(settings.data_dir / "sync.sqlite")
        try:
            con.execute("pragma foreign_keys=on")
            ensure_schema(con)
            ensure_issues(con)
            con.executescript("""
                create table if not exists identity(key text primary key,value text not null);
                create table if not exists source_sessions(id text primary key,data_json text not null);
                create table if not exists source_items(id text primary key,data_json text not null);
                create table if not exists setup_attempts(item_id text primary key,status text not null);
                create table if not exists length_attempts(
                    book_id text primary key,length integer,status text,session_id text,previous_json text);
                create table if not exists shelf_attempts(
                    id integer primary key,item_id text not null,book_id text not null,
                    previous_json text not null,desired text not null,status text not null);
                create index if not exists shelf_attempts_item_status
                    on shelf_attempts(item_id,status);
            """)
            yield con
        finally:
            con.close()


def bind_identity(con, settings, user_id, *, accept_token_change=False):
    current = {
        "abs_url": settings.abs_url,
        "abs_user": user_id,
        "readwise_token_hash": hashlib.sha256(settings.readwise_token.encode()).hexdigest(),
    }
    previous = dict(con.execute("select key,value from identity"))
    if (
        previous
        and accept_token_change
        and previous.get("abs_url") == current["abs_url"]
        and previous.get("abs_user") == current["abs_user"]
    ):
        with con:
            con.execute(
                "update identity set value=? where key='readwise_token_hash'",
                (current["readwise_token_hash"],),
            )
        previous = current
    if previous and previous != current:
        raise SyncError(
            "This data directory belongs to different credentials or an ABS user/server. "
            "Keep its ledger. See the account and token changes section in the README."
        )
    with con:
        con.executemany("insert or ignore into identity values(?,?)", current.items())


def cache_bundle(con, bundle):
    with con:
        con.executemany(
            "insert into source_sessions values(?,?) on conflict(id) do update set data_json=excluded.data_json",
            [
                (source_key(s), json.dumps(s))
                for s in bundle.get("source_records", bundle["sessions"])
            ],
        )
        con.executemany(
            "insert into source_items values(?,?) on conflict(id) do update set data_json=excluded.data_json",
            [(key, json.dumps(value)) for key, value in bundle["items"].items()],
        )
        for issue in bundle.get("issues", []):
            save_issue(con, issue)
