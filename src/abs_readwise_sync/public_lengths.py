"""Set missing audiobook lengths with the documented Books API."""

import json

from .durations import saved_audio_length

PROTECTED = (
    "status",
    "started_reading_at",
    "finished_reading_at",
    "rating",
    "review",
    "manual_total_pages",
)


class DurationSetupError(ValueError):
    """A duration error whose message contains no private response data."""


async def ensure_length(con, api, book, length, *, table="length_attempts"):
    if table not in ("length_attempts", "abs_bookwise_lengths"):
        raise ValueError("Unknown duration journal.")
    length = saved_audio_length(length)
    if length is None:
        raise DurationSetupError("ABS has no valid whole-number audiobook length.")
    bid = str(book["id"])
    if not bid.isdigit() or int(bid) < 1:
        raise DurationSetupError("The selected Readwise book has no valid ID.")
    con.execute(f"""create table if not exists {table}(
        book_id text primary key,length integer,status text,session_id text,previous_json text)""")
    con.commit()
    attempt = con.execute(f"select status from {table} where book_id=?", (bid,)).fetchone()
    # Legacy attempts may have left a temporary session running. Never bypass
    # them merely because the length was saved before cancellation failed.
    if attempt and attempt[0] not in ("confirmed", "api_rejected"):
        return "needs_review"
    if saved_audio_length(book.get("manual_total_seconds")):
        return "existing"
    if attempt and attempt[0] == "confirmed":
        return "needs_review"
    fresh_books = await api.list("/books", ids=bid)
    if len(fresh_books) != 1 or str(fresh_books[0].get("id")) != bid:
        raise DurationSetupError("Cannot read the selected book before setting its length.")
    before = fresh_books[0]
    if before.get("status") == "deleted" or any(before.get(k) != book.get(k) for k in PROTECTED):
        return "needs_review"
    if saved_audio_length(before.get("manual_total_seconds")):
        book.update(before)
        return "existing"
    with con:
        con.execute(
            f"insert into {table} values(?,?,?,?,?) "
            "on conflict(book_id) do update set length=excluded.length,status=excluded.status,"
            "session_id=null,previous_json=excluded.previous_json",
            (bid, length, "api_pending", None, json.dumps(before)),
        )
    result = await api.request(
        "PATCH", "/books", json={"items": [{"id": int(bid), "manual_total_seconds": length}]}
    )
    rows = result.get("results")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise DurationSetupError(
            "Duration update returned an incomplete result. Keep the journal for review."
        )
    row = rows[0]
    if str(row.get("id", row.get("book_id"))) != bid:
        raise DurationSetupError(
            "Duration update returned a different book ID. Review it before importing."
        )
    error = row.get("error")
    if (
        row.get("status") == "error"
        and isinstance(error, dict)
        and error.get("code") == "invalid_value"
    ):
        # The server rejected this item, so no write is uncertain. An active
        # reading session is one documented cause. A later apply can retry.
        with con:
            con.execute(f"update {table} set status='api_rejected' where book_id=?", (bid,))
        return "rejected"
    if row.get("status") != "updated" or error:
        raise DurationSetupError("Readwise rejected the duration update. Review the saved attempt.")
    fresh_books = await api.list("/books", ids=bid)
    if len(fresh_books) != 1 or str(fresh_books[0].get("id")) != bid:
        raise DurationSetupError("Cannot verify the saved audiobook length.")
    fresh = fresh_books[0]
    if saved_audio_length(fresh.get("manual_total_seconds")) != length:
        raise DurationSetupError(
            "Readwise did not save the expected audiobook length. Imports remain blocked."
        )
    if any(fresh.get(k) != before.get(k) for k in PROTECTED):
        raise DurationSetupError(
            "Book details changed during duration setup. Review them before importing."
        )
    with con:
        con.execute(f"update {table} set status='confirmed' where book_id=?", (bid,))
    book.update(fresh)
    return "updated"
