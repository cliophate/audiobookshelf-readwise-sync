"""Preview and verify optional shelf changes through the documented Books API."""

from collections import Counter
import json
import math

from .config import SyncError

PROTECTED = ("rating", "review", "started_reading_at", "finished_reading_at")


def desired_shelf(progress, item_id):
    if progress is None:
        return "want_to_read"
    if not isinstance(progress, dict):
        raise ValueError("ABS progress is not a record.")
    if str(progress.get("libraryItemId")) != item_id or progress.get("episodeId"):
        raise ValueError("ABS progress belongs to a different item or a podcast episode.")
    if type(progress.get("isFinished")) is not bool:
        raise ValueError("ABS progress has no valid completion flag.")
    if progress["isFinished"]:
        return "finished"
    if not all(key in progress for key in ("currentTime", "progress")):
        raise ValueError("ABS progress has no complete position information.")
    values = []
    for key in ("currentTime", "progress"):
        value = progress.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("ABS progress has an invalid position.")
        if not math.isfinite(value) or value < 0 or (key == "progress" and value > 1):
            raise ValueError("ABS progress has an invalid position.")
        values.append(value)
    return "currently_reading" if any(values) else "want_to_read"


def annotate(con, plans, bundle, books):
    by_id = {str(b["id"]): b for b in books}
    destinations = Counter(p["readwise_book_id"] for p in plans if p["readwise_book_id"])
    for plan in plans:
        bid = plan["readwise_book_id"]
        book = by_id.get(bid)
        shelf = {"current": (book or {}).get("status"), "desired": None, "status": "needs_review"}
        plan["shelf"] = shelf
        if plan["status"] in (
            "ambiguous",
            "mapping_changed",
            "book_unavailable",
            "setup_needs_review",
            "missing_abs_item",
        ):
            shelf["reason"] = "Resolve the book match or setup problem before changing its shelf."
            continue
        if bid and destinations[bid] > 1:
            shelf["reason"] = (
                "Multiple ABS items resolve to this Readwise book. Review their mappings."
            )
            continue
        try:
            if plan["abs_item_id"] not in bundle.get("progress", {}):
                raise ValueError("No fresh ABS progress was fetched for this item.")
            shelf["desired"] = desired_shelf(
                bundle["progress"][plan["abs_item_id"]], plan["abs_item_id"]
            )
        except ValueError as exc:
            shelf["reason"] = str(exc)
            continue
        pending = con.execute(
            "select 1 from shelf_attempts where item_id=? and status!='confirmed'",
            (plan["abs_item_id"],),
        ).fetchone()
        if pending:
            shelf["reason"] = "A previous shelf write needs review. It will not be replayed."
        elif not book:
            shelf["status"] = "needs_book"
            shelf["reason"] = "Add and map the book, or enable optional automatic book setup."
        else:
            shelf["status"] = "unchanged" if shelf["current"] == shelf["desired"] else "ready"


async def apply(con, api, plans):
    for plan in plans:
        shelf = plan.get("shelf", {})
        if shelf.get("status") != "ready":
            continue
        bid = plan["readwise_book_id"]
        fresh = await api.list("/books", ids=bid)
        if len(fresh) != 1 or str(fresh[0]["id"]) != bid:
            shelf.update(status="needs_review", reason="Cannot read the selected Readwise book.")
            continue
        before = fresh[0]
        if before.get("status") == shelf["desired"]:
            shelf["status"] = "unchanged"
            continue
        if before.get("status") != shelf["current"]:
            shelf.update(status="needs_review", reason="The Readwise shelf changed after preview.")
            continue
        with con:
            attempt = con.execute(
                "insert into shelf_attempts(item_id,book_id,previous_json,desired,status) "
                "values(?,?,?,?, 'pending')",
                (plan["abs_item_id"], bid, json.dumps(before), shelf["desired"]),
            ).lastrowid
            con.execute(
                "insert or ignore into books values(?,?,?)", (plan["abs_item_id"], bid, None)
            )
        try:
            result = await api.request(
                "PATCH", "/books", json={"items": [{"id": int(bid), "status": shelf["desired"]}]}
            )
            if len(result["results"]) != 1:
                raise SyncError("Shelf update returned an incomplete result.")
            row = result["results"][0]
            if (
                not isinstance(row, dict)
                or str(row.get("id", row.get("book_id"))) != bid
                or row.get("status") != "updated"
                or row.get("error")
            ):
                raise SyncError("Shelf update returned an error. Review it in Readwise Books.")
            after = await api.list("/books", ids=bid)
            if (
                len(after) != 1
                or str(after[0]["id"]) != bid
                or after[0].get("status") != shelf["desired"]
                or any(after[0].get(key) != before.get(key) for key in PROTECTED)
            ):
                raise SyncError(
                    "The shelf or preserved rating, review and dates could not be verified."
                )
        except SyncError as exc:
            shelf.update(status="needs_review", reason=str(exc))
            continue
        with con:
            con.execute("update shelf_attempts set status='confirmed' where id=?", (attempt,))
        shelf["status"] = "confirmed"
