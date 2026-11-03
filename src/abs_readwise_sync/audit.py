"""Read-only comparison of one ABS item's history, local exports and Readwise sessions."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
from urllib.parse import quote

import httpx

from .clients import ABSClient, BooksClient
from .config import SyncError
from .matching import item_metadata, same_work
from .permissions import access_message, local_call
from .audio_safety import metrics
from .import_review import review_records


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def clean_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    return value


def spans(rows, start, end):
    intervals = []
    for row in rows:
        a, b = number(row.get(start)), number(row.get(end))
        if a is not None and b is not None and b > a:
            intervals.append((a, b))
    covered = 0
    last = 0
    for a, b in sorted(intervals):
        covered += max(0, b - max(a, last))
        last = max(last, b)
    total = sum(b - a for a, b in intervals)
    return {
        "audio_seconds": round(total, 3),
        "unique_audio_seconds": round(covered, 3),
        "repeated_audio_seconds": round(total - covered, 3),
    }


def snapshot(settings, item_id, mapped_id):
    result = {
        "binding": None,
        "identity": {},
        "exports": [],
        "cached_sources": [],
        "linked_abs_items": [],
    }
    path = settings.data_dir / "sync.sqlite"
    try:
        local_call("checking the audit database", path, path.stat)
    except FileNotFoundError:
        return result
    try:
        con = sqlite3.connect(
            "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro", uri=True
        )
        try:
            con.execute("pragma query_only=on")
            con.execute("begin")
            tables = {
                r[0] for r in con.execute("select name from sqlite_master where type='table'")
            }
            if "identity" in tables:
                result["identity"] = dict(con.execute("select key,value from identity"))
            if "books" in tables:
                row = con.execute("select book_id from books where work=?", (item_id,)).fetchone()
                result["binding"] = row[0] if row else None
            bid = mapped_id or result["binding"]
            if "source_sessions" in tables:
                result["cached_sources"] = [
                    json.loads(row[0])
                    for row in con.execute(
                        "select data_json from source_sessions where "
                        "case when json_valid(data_json) then json_extract(data_json,'$.libraryItemId') end=?",
                        (item_id,),
                    )
                ]
            if "exports" in tables and "checkpoints" in tables:
                for row in con.execute(
                    "select e.source_id,e.book_id,e.payload_json,e.status,e.session_id,c.source_id,c.snapshot_json "
                    "from exports e left join checkpoints c on c.export_id=e.source_id where "
                    "e.book_id=? or case when json_valid(c.snapshot_json) "
                    "then json_extract(c.snapshot_json,'$.libraryItemId') end=?",
                    (bid, item_id),
                ):
                    export_id, book_id, payload, status, sid, source_id, baseline = row
                    decoded = json.loads(payload)
                    original = json.loads(baseline) if baseline else {}
                    if not isinstance(decoded, dict) or not isinstance(original, dict):
                        raise ValueError("Invalid local ledger record")
                    result["exports"].append(
                        {
                            "export_id": export_id,
                            "book_id": book_id,
                            "payload": decoded,
                            "status": status,
                            "session_id": sid,
                            "source_id": source_id,
                            "baseline": original,
                        }
                    )
            if bid and "books" in tables:
                result["linked_abs_items"] = [
                    r[0] for r in con.execute("select work from books where book_id=?", (bid,))
                ]
        finally:
            con.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        if isinstance(exc, PermissionError) or code in (
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_READONLY,
            sqlite3.SQLITE_PERM,
        ):
            raise SyncError(
                access_message("reading the audit database; keep the existing ledger", path)
            ) from None
        raise SyncError(
            "Cannot read the local audit ledger. Keep the existing database and check its integrity and schema."
        ) from None
    return result


def abs_history(rows):
    latest = {}
    counts = Counter()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        sid = row["id"]
        counts[sid] += 1
        if sid not in latest or (number(row.get("updatedAt")) or 0) > (
            number(latest[sid].get("updatedAt")) or 0
        ):
            latest[sid] = row
    return {
        "source_count": len(latest),
        "duplicate_source_ids": [k for k, v in counts.items() if v > 1],
        "listening_seconds": round(
            sum(number(r.get("timeListening")) or 0 for r in latest.values()), 3
        ),
        **spans(latest.values(), "startTime", "currentTime"),
    }


async def audit_book(settings, item_id, *, transport=None, delay=3.1):
    if not item_id or any(c.isspace() for c in item_id) or "/" in item_id:
        raise SyncError("--audit needs one Audiobookshelf item ID.")
    mappings = settings.mappings()
    local = snapshot(settings, item_id, mappings.get(item_id))
    async with httpx.AsyncClient(timeout=60, transport=transport, trust_env=False) as http:
        bundle = await ABSClient(http, settings.abs_url, settings.abs_token).fetch(
            item_ids=(item_id,), include_items=(item_id,)
        )
        expected = {
            "abs_url": settings.abs_url,
            "abs_user": bundle["user_id"],
            "readwise_token_hash": hashlib.sha256(settings.readwise_token.encode()).hexdigest(),
        }
        if local["identity"] and local["identity"] != expected:
            raise SyncError(
                "The audit ledger belongs to different credentials or an ABS user/server. Keep it and check the account settings."
            )
        if local["exports"] and not local["identity"]:
            raise SyncError(
                "Cannot verify which accounts own this ledger. Keep it and run a preview first."
            )
        api = BooksClient(http, settings.readwise_token, delay)
        books = await api.list("/books")
        detail = bundle["items"].get(item_id)
        metadata = item_metadata(detail) if detail else {}
        bid = mappings.get(item_id) or local["binding"]
        if local["binding"] and mappings.get(item_id) and local["binding"] != mappings[item_id]:
            raise SyncError(
                "The explicit mapping differs from the imported book. Review it before auditing."
            )
        matches = (
            [b for b in books if str(b["id"]) == bid]
            if bid
            else [b for b in books if b.get("status") != "deleted" and same_work(metadata, b)]
        )
        if len(matches) != 1:
            raise SyncError(
                "The audit needs one Readwise book match. Add an explicit mapping and retry."
            )
        book = matches[0]
        bid = str(book["id"])
        remote = [
            s
            for s in await api.list("/sessions", book_ids=bid, include_deleted="true")
            if str(s.get("book_id")) == bid
        ]
    sources = [
        r
        for r in bundle["source_records"]
        if isinstance(r, dict) and r.get("libraryItemId") == item_id
    ]
    history = abs_history(sources)
    exports = [r for r in local["exports"] if str(r["book_id"]) == bid]
    ledger_by_id = {str(r["session_id"]): r for r in exports if r["session_id"] is not None}
    active = [r for r in remote if not r.get("is_deleted")]
    audio = [r for r in active if r.get("format") == "audiobook"]
    duplicates = defaultdict(list)
    for row in active:
        key = json.dumps(
            [
                row.get(k)
                for k in (
                    "format",
                    "started_at",
                    "ended_at",
                    "paused_seconds",
                    "seconds_at_start",
                    "seconds_at_end",
                    "percent_at_start",
                    "percent_at_end",
                    "page_at_start",
                    "page_at_end",
                )
            ]
        )
        duplicates[key].append(str(row["id"]))
    confirmed = [r for r in exports if r["status"] == "confirmed"]
    source_review = []
    for source in sources:
        try:
            values = metrics(source)
            if not values["plausible"]:
                source_review.append({"source_id": source["id"], **values})
        except (KeyError, ValueError, TypeError, OverflowError):
            continue  # Basic invalid records are already listed in source_issues.
    warnings = []
    remote_spans = spans(audio, "seconds_at_start", "seconds_at_end")
    if history["repeated_audio_seconds"] or remote_spans["repeated_audio_seconds"]:
        warnings.append(
            "Repeated audio ranges are present. Re-listening, seeks or cumulative source ranges can cause this; it is not proof of duplicate imports."
        )
    if any(len(ids) > 1 for ids in duplicates.values()):
        warnings.append(
            "Some active Readwise sessions have identical timings and positions. Review them before making changes."
        )
    linked = sorted(
        set(local["linked_abs_items"])
        | {
            r["baseline"].get("libraryItemId")
            for r in exports
            if r["baseline"].get("libraryItemId")
        }
    )
    if len(linked) > 1:
        warnings.append(
            "More than one ABS item is bound to this Readwise book. Check the editions and mappings."
        )
    ledger_audio = spans([r["payload"] for r in confirmed], "seconds_at_start", "seconds_at_end")
    if ledger_audio["audio_seconds"] > history["audio_seconds"] + 1:
        warnings.append(
            "Confirmed ledger audio exceeds the ABS history currently returned. ABS may have dropped history, or the records need review."
        )
    totals = Counter()
    for row in active:
        totals[str(row.get("format") or "unknown")] += number(row.get("total_time_seconds")) or 0
    return clean_json(
        {
            "mode": "audit",
            "read_only": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "abs_item_id": item_id,
            "abs_item_available": bool(detail),
            "readwise_book_id": bid,
            "title": book.get("title"),
            "abs_length_seconds": metadata.get("duration"),
            "readwise_length_seconds": book.get("manual_total_seconds"),
            "abs_history": history,
            "cached_abs_history": abs_history(local["cached_sources"]),
            "abs_totals_use_latest_record_per_source_id": True,
            "readwise_totals_by_format": dict(totals),
            "readwise_audio_ranges": remote_spans,
            "confirmed_ledger_audio_ranges": ledger_audio,
            "ledger_statuses": dict(Counter(r["status"] for r in exports)),
            "linked_abs_item_ids": linked,
            "readwise_sessions_without_ledger_id": [
                str(s["id"]) for s in active if str(s["id"]) not in ledger_by_id
            ],
            "identical_readwise_session_groups": [
                ids for ids in duplicates.values() if len(ids) > 1
            ],
            "warnings": warnings,
            "comparison_note": "The ledger snapshot and live service reads happen at different times. Pause the worker for a stable comparison. Sessions without a ledger ID can be manual entries or imports completed during the audit.",
            "source_issues": [
                {key: issue[key] for key in ("source_id", "status", "reason")}
                for issue in bundle["issues"]
                if issue["item_id"] == item_id
            ],
            "suspect_abs_ranges": source_review,
            "import_review": review_records(exports),
            "abs_sessions": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "startedAt",
                        "updatedAt",
                        "startTime",
                        "currentTime",
                        "timeListening",
                    )
                }
                for r in sources
            ],
            "ledger": [
                {
                    "export_id": r["export_id"],
                    "abs_source_id": r["source_id"],
                    "abs_item_id": r["baseline"].get("libraryItemId"),
                    "status": r["status"],
                    "readwise_session_id": r["session_id"],
                    **{
                        k: r["payload"].get(k)
                        for k in ("started_at", "ended_at", "seconds_at_start", "seconds_at_end")
                    },
                }
                for r in exports
            ],
            "readwise_sessions": [
                {
                    **{
                        k: r.get(k)
                        for k in (
                            "id",
                            "origin",
                            "format",
                            "started_at",
                            "ended_at",
                            "seconds_at_start",
                            "seconds_at_end",
                            "paused_seconds",
                            "total_time_seconds",
                            "is_deleted",
                        )
                    },
                    "ledger_export_id": ledger_by_id.get(str(r["id"]), {}).get("export_id"),
                }
                for r in remote
            ],
            "time_definition": "Readwise audiobook totals count audio position covered. ABS timeListening measures listening time. Playback speed, seeks and re-listening can make these differ.",
        }
    )
