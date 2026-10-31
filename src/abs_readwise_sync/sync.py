"""Build a preview before performing any remote write."""

import json
import math
from collections import Counter
from datetime import datetime, timezone

import httpx

from . import exporter, optional
from .clients import ABSClient, AppClient, BooksClient
from .config import SyncError
from .matching import item_metadata, same_work
from .storage import bind_identity, cache_bundle, database


def positive_length(value):
    try:
        number = float(value)
        return round(number) if math.isfinite(number) and 0 < number <= 360000 else None
    except (TypeError, ValueError):
        return None


def resolve_book(con, item_id, metadata, books, mappings):
    saved = con.execute("select book_id from books where work=?", (item_id,)).fetchone()
    requested = mappings.get(item_id)
    if saved and requested and saved[0] != requested:
        return None, "mapping_changed"
    bid = requested or (saved[0] if saved else None)
    if bid:
        matches = [b for b in books if str(b["id"]) == bid and b.get("status") != "deleted"]
        return (matches[0], "matched") if len(matches) == 1 else (None, "book_unavailable")
    matches = [b for b in books if b.get("status") != "deleted" and same_work(metadata, b)]
    if len(matches) == 1:
        return matches[0], "matched"
    return None, "ambiguous" if matches else "unmatched"


def overlaps(item, sessions):
    """Avoid importing over remote activity that this ledger does not own."""
    start = datetime.fromisoformat(item["started_at"])
    end = datetime.fromisoformat(item["ended_at"])
    for session in sessions:
        if (
            str(session.get("book_id")) != str(item["book_id"])
            or session.get("is_deleted")
            or session.get("format") != "audiobook"
        ):
            continue
        try:
            a = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(session["ended_at"].replace("Z", "+00:00"))
            if start < b and a < end:
                return True
        except (KeyError, ValueError, TypeError):
            raise SyncError(
                "A remote audiobook session has invalid timing; review it before importing."
            ) from None
    return False


def reconcile(con, sessions):
    rows = con.execute("select source_id,status,session_id,payload_json from exports").fetchall()
    claimed = {str(r[2]) for r in rows if r[2] is not None}
    for source_id, status, sid, encoded in rows:
        if status not in ("pending", "verify"):
            continue
        matches = exporter.reconciliation_matches(
            json.loads(encoded), sessions, sid, claimed - {str(sid)}
        )
        if len(matches) == 1:
            with con:
                con.execute(
                    "update exports set status='confirmed',session_id=? where source_id=?",
                    (str(matches[0]["id"]), source_id),
                )
            claimed.add(str(matches[0]["id"]))


def build_plan(con, settings, bundle, books, sessions, now=None):
    now = now or datetime.now(timezone.utc)
    mappings = settings.mappings()
    by_item = {}
    for source in bundle["sessions"]:
        if source.get("mediaType", "book") == "book":
            by_item.setdefault(str(source["libraryItemId"]), []).append(source)
    plans = []
    occupied = list(sessions)
    for item_id, sources in sorted(by_item.items()):
        metadata = item_metadata(bundle["items"][item_id])
        book, state = resolve_book(con, item_id, metadata, books, mappings)
        length = positive_length((book or {}).get("manual_total_seconds"))
        source_length = positive_length(metadata["duration"])
        if book and not length:
            state = "needs_length"
        setup = con.execute(
            "select status from setup_attempts where item_id=?", (item_id,)
        ).fetchone()
        unfinished_length = (
            book
            and con.execute(
                "select 1 from length_attempts where book_id=? and status!='confirmed'",
                (str(book["id"]),),
            ).fetchone()
        )
        if (setup and setup[0] != "confirmed") or unfinished_length:
            state = "setup_needs_review"
        plan = {
            "abs_item_id": item_id,
            "title": metadata["title"],
            "author": metadata["author"],
            "readwise_book_id": str(book["id"]) if book else None,
            "status": state,
            "abs_length_seconds": source_length,
            "readwise_length_seconds": length,
            "sessions": [],
        }
        for source in sorted(sources, key=lambda s: s.get("startedAt") or 0):
            row = {"source_id": source["id"], "status": state}
            try:
                started = float(source["startedAt"]) / 1000
                updated = float(source["updatedAt"]) / 1000
                if not math.isfinite(started) or not math.isfinite(updated):
                    raise ValueError
                # Validate even an unmatched source before considering optional setup.
                exporter.payload(source, (book or {}).get("id", 1), timezone_name=settings.timezone)
                if settings.import_since and started < settings.import_since.timestamp():
                    row["status"] = "before_import_since"
                elif updated > now.timestamp() - settings.settle_minutes * 60:
                    row["status"] = "recent"
                elif state == "setup_needs_review":
                    pass
                elif book:
                    candidate = exporter.next_source(con, source, book["id"])
                    if candidate is None:
                        row["status"] = "existing"
                    else:
                        item = exporter.payload(
                            candidate,
                            book["id"],
                            publish_to_feed=exporter.publish_session(candidate, now),
                            timezone_name=settings.timezone,
                        )
                        row.update(
                            audio_seconds=item["seconds_at_end"] - item["seconds_at_start"],
                            listening_seconds=round(float(candidate["timeListening"])),
                        )
                        if not length:
                            row["status"] = "needs_length"
                        elif item["seconds_at_end"] > length:
                            row["status"] = "position_exceeds_length"
                        elif overlaps(item, occupied):
                            row["status"] = "remote_overlap"
                        else:
                            row["status"] = "ready"
                            row["_candidate"] = candidate
                            occupied.append(item)
            except (ValueError, KeyError, TypeError, OverflowError):
                row["status"] = "needs_review"
            plan["sessions"].append(row)
        plans.append(plan)
    return plans


def report(plans, mode, result=None):
    summary = Counter(s["status"] for p in plans for s in p["sessions"])
    public = [
        {
            **p,
            "sessions": [
                {k: v for k, v in s.items() if not k.startswith("_")} for s in p["sessions"]
            ],
        }
        for p in plans
    ]
    return {"mode": mode, "summary": dict(summary), "books": public, "result": result or {}}


async def run_once(settings, *, apply=False, transport=None, delay=3.1, accept_token_change=False):
    with database(settings) as con:
        async with httpx.AsyncClient(timeout=60, transport=transport, trust_env=False) as http:
            abs_client = ABSClient(http, settings.abs_url, settings.abs_token)
            api = BooksClient(http, settings.readwise_token, delay)
            app = AppClient(http, settings.app_session, delay) if settings.app_session else None
            # Do not export anything until a complete fresh ABS fetch succeeds.
            bundle = await abs_client.fetch()
            books = await api.list("/books")
            sessions = await api.list("/sessions", include_deleted="true")
            bind_identity(con, settings, bundle["user_id"], accept_token_change=accept_token_change)
            cache_bundle(con, bundle)
            if apply:
                reconcile(con, sessions)
            plans = build_plan(con, settings, bundle, books, sessions)
            if not apply:
                return report(plans, "preview")
            if app:
                for plan in plans:
                    if not any(
                        s["status"] in ("unmatched", "needs_length") for s in plan["sessions"]
                    ):
                        continue
                    metadata = item_metadata(bundle["items"][plan["abs_item_id"]])
                    book, state = resolve_book(
                        con, plan["abs_item_id"], metadata, books, settings.mappings()
                    )
                    if state == "unmatched":
                        book = await optional.add_book(con, api, app, plan["abs_item_id"], metadata)
                        if book:
                            books.append(book)
                    if book and positive_length(metadata["duration"]):
                        await optional.ensure_length(
                            con,
                            api,
                            app,
                            book,
                            positive_length(metadata["duration"]),
                            settings.timezone,
                        )
                plans = build_plan(con, settings, bundle, books, sessions)
            pending = []
            with con:
                for plan in plans:
                    for source in plan["sessions"]:
                        if source["status"] == "ready":
                            con.execute(
                                "insert or ignore into books values(?,?,?)",
                                (plan["abs_item_id"], plan["readwise_book_id"], None),
                            )
                            pending.append((source["_candidate"], plan["readwise_book_id"]))
            counts = Counter()
            for offset in range(0, len(pending), 10):
                batch = pending[offset : offset + 10]
                # The API has no idempotency key. The ledger is committed before POST.
                results = await exporter.submit_batch(
                    con, api, batch, timezone_name=settings.timezone
                )
                counts.update(r["status"] for r in results)
                if any(r["status"] != "confirmed" for r in results):
                    break
            return report(plans, "apply", dict(counts))
