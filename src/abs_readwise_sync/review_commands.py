"""Explicit local mapping choices and historical-import approvals."""

from datetime import datetime, timezone
import hashlib
import json
import os
import tempfile

from .config import SyncError
from .matching import compare, item_metadata, requires_progress_policy


def check_identity(con, settings):
    identity = dict(con.execute("select key,value from identity"))
    if not identity:
        raise SyncError(
            "Run a sync preview first so this data directory has a verified account identity."
        )
    if (
        identity.get("abs_url") != settings.abs_url
        or identity.get("readwise_token_hash")
        != hashlib.sha256(settings.readwise_token.encode()).hexdigest()
    ):
        raise SyncError(
            "These credentials do not match the saved account. Run a preview to resolve the account change first."
        )


def cached_metadata(con, item_id):
    row = con.execute("select data_json from source_items where id=?", (item_id,)).fetchone()
    if not row:
        raise SyncError("ABS item is not cached. Include it in a sync preview first.")
    return item_metadata(json.loads(row[0]))


def save_mapping(con, settings, item_id, book):
    metadata = cached_metadata(con, item_id)
    bid = str(book["id"])
    if book.get("status") == "deleted":
        raise SyncError("The selected Readwise book is deleted. Choose an active library entry.")
    saved = con.execute("select book_id from books where work=?", (item_id,)).fetchone()
    mappings = settings.mappings()
    if (saved and saved[0] != bid) or (item_id in mappings and mappings[item_id] != bid):
        raise SyncError(
            "An existing mapping or saved binding points elsewhere. Its history will not be reassigned."
        )
    if requires_progress_policy(metadata, book):
        raise SyncError(
            "Split audiobooks and collections need a progress policy before a new mapping can be saved."
        )
    # Keep this boundary even if a file write fails. It only holds history, never sends it.
    if not saved:
        with con:
            con.execute(
                "insert or ignore into history_boundaries values(?,?,?)",
                (item_id, bid, datetime.now(timezone.utc).timestamp()),
            )
    mappings[item_id] = bid
    path = settings.mappings_file
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".mappings-", delete=False
        ) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            json.dump(mappings, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return {
        "abs_item_id": item_id,
        "readwise_book_id": bid,
        "remote_writes": 0,
        "reason": "Mapping saved. Run a preview to review any older sessions before --apply.",
        "matching_notes": compare(metadata, book)["reasons"],
    }
