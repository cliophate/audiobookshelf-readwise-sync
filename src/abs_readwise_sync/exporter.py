"""Export representable Audiobookshelf sessions with a durable submission ledger.

An uncertain POST is never retried automatically. Source IDs stay in the local
ledger; exact session details reconcile successful but interrupted requests.
"""

import json
import hashlib
import os
from datetime import datetime, timedelta, timezone


def publish_session(source, now=None):
    """Publish new listening only; historical and delayed backfills stay quiet."""
    configured = os.getenv("PUBLISH_FROM", "").strip()
    if not configured:
        return False
    cutoff = datetime.fromisoformat(configured.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("PUBLISH_FROM must include a timezone")
    original = source.get("_original_source", source)
    started = datetime.fromtimestamp(float(original["startedAt"]) / 1000, timezone.utc)
    now = now or datetime.now(timezone.utc)
    return started >= cutoff and timedelta(0) <= now - started <= timedelta(hours=48)


def payload(source, book_id, *, publish_to_feed=False, timezone_name=None):
    seconds = round(float(source["timeListening"]))
    start = round(float(source["startedAt"]) / 1000)
    end = round(float(source["updatedAt"]) / 1000)
    a, b = round(float(source["startTime"])), round(float(source["currentTime"]))
    if seconds <= 0 or end <= start or not 0 <= a < b <= 360000:
        raise ValueError("Source session has no forward audio span or valid timestamps")

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat()

    return dict(
        book_id=int(book_id),
        started_at=iso(start),
        ended_at=iso(end),
        paused_seconds=max(0, end - start - seconds),
        format="audiobook",
        seconds_at_start=a,
        seconds_at_end=b,
        timezone=timezone_name or os.getenv("TIMEZONE", "UTC"),
        publish_to_feed=publish_to_feed,
        device={"model": "Audiobookshelf", "vendor": "Audiobookshelf", "type": "Handset"},
    )


def ensure_schema(con):
    con.executescript("""
      create table if not exists books(work text primary key,book_id text not null,product_id text);
      create table if not exists exports(source_id text primary key,book_id text,payload_json text,status text,session_id text,error text);
      create table if not exists checkpoints(export_id text primary key,source_id text not null,snapshot_json text not null);
    """)


def next_source(con, source, book_id):
    """Return only the unexported tail of a cumulative ABS session.

    The previous observation supplies the interval boundary. ABS does not
    identify the exact resume time; the idle portion remains paused time.
    """
    rows = con.execute(
        """select e.source_id,e.status,e.payload_json,c.snapshot_json
        from exports e left join checkpoints c on c.export_id=e.source_id
        where e.source_id=? or c.source_id=?""",
        (source["id"], source["id"]),
    ).fetchall()
    if not rows:
        return source
    if any(r[1] != "confirmed" for r in rows):
        raise ValueError("Unconfirmed export requires reconciliation")
    checkpoints = []
    for _, _, encoded, snapshot in rows:
        item = json.loads(encoded)
        if str(item["book_id"]) != str(book_id):
            raise ValueError("Book identity changed")
        if snapshot:
            checkpoints.append(json.loads(snapshot))
        else:
            raise ValueError("Export has no source baseline")
    previous = max(checkpoints, key=lambda s: float(s["updatedAt"]))
    if previous.get("libraryItemId") != source.get("libraryItemId"):
        raise ValueError("Source library item changed")
    if (
        abs(float(source["startedAt"]) - float(previous["startedAt"])) > 1000
        or abs(float(source["startTime"]) - float(previous["startTime"])) > 1
    ):
        raise ValueError("Original session identity changed")
    added = float(source["timeListening"]) - float(previous["timeListening"])
    advance = float(source["currentTime"]) - float(previous["currentTime"])
    if abs(added) < 1 and abs(advance) < 1:
        return None
    elapsed = (float(source["updatedAt"]) - float(previous["updatedAt"])) / 1000
    if added < 1 or advance < 1 or elapsed <= 0 or added > elapsed + 1:
        raise ValueError("Changed session has no consistent forward continuation")
    checkpoint = {
        k: source[k]
        for k in (
            "id",
            "libraryItemId",
            "startedAt",
            "updatedAt",
            "timeListening",
            "startTime",
            "currentTime",
        )
    }
    suffix = hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest()[:20]
    return {
        **source,
        "id": source["id"] + ":" + suffix,
        "_original_source": checkpoint,
        "startedAt": previous["updatedAt"],
        "timeListening": added,
        "startTime": previous["currentTime"],
    }


def verify(item, fresh):
    if fresh.get("is_deleted") or str(fresh.get("book_id")) != str(item["book_id"]):
        return False
    fields = ("format", "seconds_at_start", "seconds_at_end", "paused_seconds")
    if any(fresh.get(k) != item[k] for k in fields):
        return False
    for k in ("started_at", "ended_at"):
        if datetime.fromisoformat(fresh[k].replace("Z", "+00:00")) != datetime.fromisoformat(
            item[k]
        ):
            return False
    return fresh.get("total_time_seconds") == item["seconds_at_end"] - item["seconds_at_start"]


def reconciliation_matches(item, sessions, session_id=None, claimed_ids=()):
    """Require one exact, unclaimed remote session before confirming a write.

    Old submissions retain their original ABS device label in the ledger.
    New submissions use a readable label and match their full timing/position
    details. Known server IDs take precedence. Ambiguity never triggers a retry.
    """
    matches = []
    for session in sessions:
        sid = str(session.get("id"))
        if sid in claimed_ids or (session_id is not None and sid != str(session_id)):
            continue
        if session.get("origin") not in (None, "api"):
            continue
        device = session.get("device") or {}
        if any(device.get(k) != v for k, v in item["device"].items()):
            continue
        try:
            if verify(item, session):
                matches.append(session)
        except (KeyError, TypeError, ValueError):
            continue
    return matches


async def submit_batch(con, api, sources, *, now=None, timezone_name=None):
    """Commit intent before POST; every uncertain result requires reconciliation."""
    ensure_schema(con)
    items = []
    for source, book_id in sources:
        if con.execute("select 1 from exports where source_id=?", (source["id"],)).fetchone():
            raise RuntimeError("Previously attempted audiobook export cannot be replayed")
        items.append(
            payload(
                source,
                book_id,
                publish_to_feed=publish_session(source, now),
                timezone_name=timezone_name,
            )
        )
    if not 1 <= len(items) <= 10:
        raise ValueError("Invalid audiobook batch size")
    with con:
        con.executemany(
            "insert into exports values(?,?,?,?,?,?)",
            [
                (s["id"], str(b), json.dumps(p), "pending", None, None)
                for (s, b), p in zip(sources, items)
            ],
        )
        for source, _ in sources:
            original = source.get("_original_source", source)
            snapshot = {
                k: original[k]
                for k in (
                    "id",
                    "startedAt",
                    "updatedAt",
                    "timeListening",
                    "startTime",
                    "currentTime",
                    "libraryItemId",
                )
            }
            con.execute(
                "insert into checkpoints values(?,?,?)",
                (source["id"], original["id"], json.dumps(snapshot)),
            )
    data = await api.request("POST", "/sessions", json={"items": items})
    if len(data["results"]) != len(items):
        raise ValueError("Invalid audiobook response length")
    ids = []
    with con:
        for (source, _), result in zip(sources, data["results"]):
            sid = str(result["session_id"]) if result.get("session_id") else None
            status = "verify" if result.get("status") == "created" and sid else "rejected"
            con.execute(
                "update exports set status=?,session_id=?,error=? where source_id=?",
                (status, sid, json.dumps(result.get("error")), source["id"]),
            )
            if status == "verify":
                ids.append(sid)
    fresh = {str(s["id"]): s for s in await api.list("/sessions", ids=",".join(ids))} if ids else {}
    results = []
    with con:
        for (source, _), item, result in zip(sources, items, data["results"]):
            sid = str(result.get("session_id") or "")
            if sid in fresh:
                status = "confirmed" if verify(item, fresh[sid]) else "verification_failed"
                con.execute("update exports set status=? where source_id=?", (status, source["id"]))
            else:
                status = "rejected" if result.get("status") != "created" else "verify"
            results.append({"status": status, "session_id": sid})
    return results
