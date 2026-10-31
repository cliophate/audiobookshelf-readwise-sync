"""The source cache and submission ledger belong to one pair of accounts."""

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager

from .config import SyncError
from .exporter import ensure_schema


@contextmanager
def database(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError(
                "Another sync or preview is running. Try again after it finishes."
            ) from None
        con = sqlite3.connect(settings.data_dir / "sync.sqlite")
        try:
            con.execute("pragma foreign_keys=on")
            ensure_schema(con)
            con.executescript("""
                create table if not exists identity(key text primary key,value text not null);
                create table if not exists source_sessions(id text primary key,data_json text not null);
                create table if not exists source_items(id text primary key,data_json text not null);
                create table if not exists setup_attempts(item_id text primary key,status text not null);
                create table if not exists length_attempts(
                    book_id text primary key,length integer,status text,session_id text,previous_json text);
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
        con.execute("delete from source_sessions")
        con.executemany(
            "insert into source_sessions values(?,?)",
            [(s["id"], json.dumps(s)) for s in bundle["sessions"]],
        )
        con.execute("delete from source_items")
        con.executemany(
            "insert into source_items values(?,?)",
            [(key, json.dumps(value)) for key, value in bundle["items"].items()],
        )
