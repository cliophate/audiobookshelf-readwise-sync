"""Optional automation through the Readwise Books app API."""

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
        return None, "ambiguous" if matches else "unmatched"
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
    return books[0], "matched"
