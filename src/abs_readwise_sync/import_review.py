"""Review existing exports without changing the ledger or contacting either service."""

from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .audio_safety import metrics, playback_limit


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A ledger timestamp has no timezone.")
    return parsed.timestamp() * 1000


def source_interval(record, previous):
    """Use saved observations only when they establish this export's boundaries."""
    baseline, payload = record["baseline"], record["payload"]
    start = timestamp(payload["started_at"])
    end = timestamp(payload["ended_at"])
    a, b = payload["seconds_at_start"], payload["seconds_at_end"]
    if abs(end - baseline["updatedAt"]) > 1000 or abs(b - baseline["currentTime"]) > 1:
        raise ValueError("Export endpoints differ from the saved source observation.")
    if abs(start - baseline["startedAt"]) <= 1000 and abs(a - baseline["startTime"]) <= 1:
        listened = baseline["timeListening"]
    else:
        matches = [
            old
            for old in previous
            if old.get("id") == baseline.get("id")
            and abs(old["startedAt"] - baseline["startedAt"]) <= 1000
            and abs(old["startTime"] - baseline["startTime"]) <= 1
            and abs(start - old["updatedAt"]) <= 1000
            and abs(a - old["currentTime"]) <= 1
            and old["updatedAt"] < baseline["updatedAt"]
        ]
        if len(matches) != 1:
            raise ValueError("A continuation has no unique saved starting observation.")
        listened = baseline["timeListening"] - matches[0]["timeListening"]
    return dict(startTime=a, currentTime=b, timeListening=listened, startedAt=start, updatedAt=end)


def review_records(records):
    limit = playback_limit()
    previous = defaultdict(list)
    issues = []
    totals = Counter()

    def order(row):
        try:
            return timestamp(row["payload"]["ended_at"])
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return float("inf")

    ordered = sorted(records, key=order)
    for row in ordered:
        status = "plausible"
        values = {}
        source_id = row.get("source_id") or row["export_id"]
        try:
            candidate = source_interval(row, previous[source_id])
            values = metrics(candidate, max_speed=limit)
            if not values["plausible"]:
                status = "suspect_range"
                reason = "Audio span or listening counter exceeds the configured timing limits."
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            status = "unverifiable_source"
            reason = "Saved source observations do not establish this export's listening interval."
        baseline = row.get("baseline")
        if isinstance(baseline, dict) and baseline and baseline not in previous[source_id]:
            previous[source_id].append(baseline)
        totals[status] += 1
        if status != "plausible":
            issues.append(
                {
                    "export_id": row["export_id"],
                    "abs_source_id": source_id,
                    "readwise_book_id": row["book_id"],
                    "readwise_session_id": row.get("session_id"),
                    "ledger_status": row["status"],
                    "status": status,
                    "reason": reason,
                    **values,
                    "automatic_repair": False,
                    "next_action": "Review the original listening app and source history. Keep this ledger entry to prevent reimport. No replacement positions can be inferred safely from this record alone.",
                }
            )
    return {
        "mode": "import_review",
        "read_only": True,
        "max_playback_speed": limit,
        "scanned_exports": len(records),
        "summary": dict(totals),
        "sessions": issues,
        "note": "This is a local preview. It does not verify current remote ownership or edits and cannot authorize deletion. Repeated audio alone is not an error. Plausible ranges can still contain seeks or incorrect positions.",
    }


def review_database(path, *, prefix=""):
    if prefix not in ("", "abs_bookwise_"):
        raise ValueError("Unknown export ledger schema.")
    path = Path(path)
    try:
        path.stat()
    except FileNotFoundError:
        return review_records([])
    con = sqlite3.connect("file:" + quote(str(path.absolute()), safe="/") + "?mode=ro", uri=True)
    try:
        con.execute("pragma query_only=on")
        con.execute("begin")
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        exports, checkpoints = prefix + "exports", prefix + "checkpoints"
        if exports not in tables:
            return review_records([])
        checkpoint_fields = "c.source_id,c.snapshot_json" if checkpoints in tables else "null,null"
        join = (
            f"left join {checkpoints} c on c.export_id=e.source_id" if checkpoints in tables else ""
        )
        rows = con.execute(
            "select e.source_id,e.book_id,e.payload_json,e.status,e.session_id,"
            + checkpoint_fields
            + f" from {exports} e {join}"
        )
        records = []
        for export_id, book_id, encoded, status, sid, source_id, snapshot in rows:
            # Do not emit raw payloads, captions, device fields or account credentials.
            records.append(
                dict(
                    export_id=export_id,
                    book_id=book_id,
                    payload=json.loads(encoded),
                    status=status,
                    session_id=sid,
                    source_id=source_id,
                    baseline=json.loads(snapshot) if snapshot else {},
                )
            )
        return review_records(records)
    finally:
        con.close()
