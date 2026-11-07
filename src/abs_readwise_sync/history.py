"""Hold old, newly matched sources until their exact preview is approved."""

import hashlib
import json
from datetime import datetime, timezone

from .config import SyncError
from .legacy_matching import same_work as legacy_same_work


def snapshot_hash(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True, allow_nan=False).encode()).hexdigest()


def boundary(con, item_id, book, metadata, now):
    bid = str(book["id"])
    saved = con.execute("select 1 from books where work=?", (item_id,)).fetchone()
    previously_unmatched = con.execute(
        "select 1 from source_issues where item_id=? and status in "
        "('unmatched','ambiguous','matching_review','history_needs_review') limit 1",
        (item_id,),
    ).fetchone()
    if not saved and (previously_unmatched or not legacy_same_work(metadata, book)):
        with con:
            con.execute(
                "insert or ignore into history_boundaries values(?,?,?)",
                (item_id, bid, now.timestamp()),
            )
    row = con.execute(
        "select cutoff from history_boundaries where item_id=? and book_id=?", (item_id, bid)
    ).fetchone()
    return row[0] if row else None


def held(con, item_id, bid, source, cutoff):
    if cutoff is None or float(source["startedAt"]) / 1000 >= cutoff:
        return False
    if con.execute(
        "select 1 from exports e left join checkpoints c on c.export_id=e.source_id "
        "where e.source_id=? or c.source_id=? limit 1",
        (source["id"], source["id"]),
    ).fetchone():
        return False
    return not con.execute(
        "select 1 from history_approvals where item_id=? and book_id=? and source_id=? and snapshot_hash=?",
        (item_id, str(bid), source["id"], snapshot_hash(source)),
    ).fetchone()


def preview(con, item_id, bid, sources):
    entries = sorted((s["id"], snapshot_hash(s)) for s in sources)
    encoded = json.dumps(entries)
    token = snapshot_hash({"item_id": item_id, "book_id": str(bid), "entries": entries})[:24]
    with con:
        con.execute(
            "insert or ignore into history_previews values(?,?,?,?)",
            (token, item_id, str(bid), encoded),
        )
    return {
        "token": token,
        "sessions": len(entries),
        "listening_seconds": round(sum(float(s["timeListening"]) for s in sources)),
        "oldest_started_at": datetime.fromtimestamp(
            min(float(s["startedAt"]) for s in sources) / 1000, timezone.utc
        ).isoformat(),
        "approve_command": f"--approve-history {item_id} {token}",
        "reason": "Older sessions became eligible after resolving this match. Review their destination and ranges before approving.",
    }


def approve(con, settings, item_id, token):
    row = con.execute(
        "select book_id,entries_json from history_previews where item_id=? and token=?",
        (item_id, token),
    ).fetchone()
    if not row:
        raise SyncError("History preview not found. Run a fresh preview and use its review token.")
    bid, encoded = row
    saved = con.execute("select book_id from books where work=?", (item_id,)).fetchone()
    mapped = settings.mappings().get(item_id)
    if (saved and saved[0] != bid) or (mapped and mapped != bid):
        raise SyncError(
            "The destination changed after preview. Run a fresh preview; history was not approved."
        )
    entries = json.loads(encoded)
    for sid, digest in entries:
        cached = con.execute("select data_json from source_sessions where id=?", (sid,)).fetchone()
        if not cached or snapshot_hash(json.loads(cached[0])) != digest:
            raise SyncError(
                "Source sessions changed after preview. Run a fresh preview; history was not approved."
            )
    with con:
        con.executemany(
            "insert or ignore into history_approvals values(?,?,?,?)",
            [(item_id, bid, sid, digest) for sid, digest in entries],
        )
    return {
        "abs_item_id": item_id,
        "readwise_book_id": bid,
        "approved_sessions": len(entries),
        "remote_writes": 0,
    }
