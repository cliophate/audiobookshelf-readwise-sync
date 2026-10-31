"""Optional automation through the Readwise Books app API."""

import json

from .config import SyncError
from .matching import same_work


async def add_book(con, api, app, item_id, source):
    if con.execute("select 1 from setup_attempts where item_id=?", (item_id,)).fetchone():
        raise SyncError("A previous book setup needs review. It will not be retried automatically.")
    products = await app.request("GET", "/search_products", params={"q": source["title"]})
    if not isinstance(products, list):
        raise SyncError("Readwise catalog search returned an invalid response.")
    matches = [p for p in products if isinstance(p, dict) and same_work(source, p)]
    if len(matches) != 1:
        return None
    pid = str(matches[0]["id"])
    with con:
        con.execute("insert into setup_attempts values(?,?)", (item_id, "pending"))
    data = await api.request(
        "POST", "/books", json={"items": [{"product": {"id": pid}, "status": "currently_reading"}]}
    )
    if len(data["results"]) != 1:
        raise SyncError("Book creation returned an invalid result. Review the setup attempt.")
    result = data["results"][0]
    if (result.get("error") or {}).get("code") == "not_found":
        result = await app.request(
            "POST",
            "/tracked_books/",
            json={"catalog_product_id": pid, "status": "currently_reading"},
        )
        bid = result.get("id")
    else:
        bid = result.get("book_id")
    if not bid:
        raise SyncError("Could not add the catalog book. Add it in the app and map it manually.")
    books = await api.list("/books", ids=str(bid))
    if len(books) != 1 or not same_work(source, books[0]) or books[0].get("status") == "deleted":
        raise SyncError("Added book could not be verified. Review the setup attempt.")
    with con:
        con.execute("update setup_attempts set status='confirmed' where item_id=?", (item_id,))
    return books[0]


async def ensure_length(con, api, app, book, length, timezone):
    """Save a missing length, cancel the temporary session, restore shelf/dates."""
    if book.get("manual_total_seconds") is not None:
        return "existing"
    con.execute("""create table if not exists length_attempts(
        book_id text primary key,length integer,status text,session_id text,previous_json text)""")
    con.commit()
    bid = str(book["id"])
    if con.execute("select 1 from length_attempts where book_id=?", (bid,)).fetchone():
        return "needs_review"
    if (await app.request("GET", "/manual_sessions/ongoing")).get("session"):
        return "session_active"
    with con:
        con.execute(
            "insert into length_attempts values(?,?,?,?,?)",
            (bid, length, "pending", None, json.dumps(book)),
        )
    data = await app.request(
        "POST",
        "/manual_sessions/start",
        json={
            "tracked_book_id": int(bid),
            "manual_format": "audiobook",
            "seconds": 0,
            "total_seconds": length,
            "timezone": timezone,
        },
    )
    session = data.get("session") or data
    sid = session.get("id") or session.get("session_id")
    if not sid:
        raise SyncError("The temporary length session needs review. Its ID was not returned.")
    with con:
        con.execute(
            "update length_attempts set status='cancel_pending',session_id=? where book_id=?",
            (str(sid), bid),
        )
    await app.request("POST", "/manual_sessions/cancel", json={"session_id": sid})
    if (await app.request("GET", "/manual_sessions/ongoing")).get("session"):
        raise SyncError(
            "A manual session is still active after length setup. Review it in the app."
        )
    fresh_books = await api.list("/books", ids=bid)
    if len(fresh_books) != 1:
        raise SyncError("Cannot verify the audiobook length.")
    fresh = fresh_books[0]
    if fresh.get("manual_total_seconds") != length:
        raise SyncError("The app did not save the expected audiobook length.")
    changes = {
        k: book.get(k)
        for k in ("status", "started_reading_at", "finished_reading_at")
        if fresh.get(k) != book.get(k)
    }
    if changes:
        result = await api.request("PATCH", "/books", json={"items": [{"id": int(bid), **changes}]})
        if len(result["results"]) != 1 or result["results"][0].get("error"):
            raise SyncError("Could not restore the original shelf and reading dates.")
        fresh_books = await api.list("/books", ids=bid)
        if len(fresh_books) != 1 or any(fresh_books[0].get(k) != v for k, v in changes.items()):
            raise SyncError("Shelf and date restoration needs review.")
        fresh = fresh_books[0]
    if any(fresh.get(k) != book.get(k) for k in ("rating", "review")):
        raise SyncError("Editorial fields changed during length setup. Review the book.")
    book.update(fresh)
    with con:
        con.execute("update length_attempts set status='confirmed' where book_id=?", (bid,))
    return "updated"
