"""Ordinary logs show actions and changed issues, with stable deduplication."""

from collections import Counter
import hashlib
import json

QUIET = {"existing", "recent", "before_import_since"}


def render(result, previous=None, *, show_all=False):
    previous = previous or {}
    current = {}
    lines = [f"{result['mode']}: {json.dumps(result['summary'], sort_keys=True)}"]
    repeated = 0

    def emit(key, body):
        nonlocal repeated
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        current[key] = digest
        if not show_all and previous.get(key) == digest:
            repeated += 1
            return False
        return True

    for book in result["books"]:
        sessions = [s for s in book["sessions"] if s["status"] not in QUIET]
        shelf = book.get("shelf")
        if shelf and shelf["status"] == "unchanged":
            shelf = None
        if not sessions and not shelf:
            continue
        counts = dict(Counter(s["status"] for s in sessions))
        reasons = sorted({s["reason"] for s in sessions if s.get("reason")})
        matching = book.get("matching", {})
        body = {
            "counts": counts,
            "reasons": reasons,
            "sources": sorted((s["source_id"], s["status"], s.get("reason")) for s in sessions),
            "shelf": shelf,
            "matching": matching,
            "history": book.get("history_review"),
            "length_help": book.get("length_help"),
        }
        if not emit("book:" + book["abs_item_id"], body):
            continue
        lines.append(
            f"  {book['title']} | ABS {book['abs_item_id']} | Readwise {book['readwise_book_id'] or 'unmatched'} | {counts}"
        )
        if book["status"] in (
            "unmatched",
            "ambiguous",
            "matching_review",
            "mapping_changed",
            "book_unavailable",
        ):
            lines.append("    Match: " + matching.get("reason", book["status"]))
            for candidate in matching.get("candidates", []):
                volumes = ", ".join(candidate["volumes"]) or "unknown"
                lines.append(
                    f"    Candidate {candidate['id']}: {candidate['title']} / {candidate['author']} | volume {volumes} | {candidate['status']}. "
                    + " ".join(candidate["reasons"])
                )
            if matching.get("candidate_count", 0) > len(matching.get("candidates", [])):
                lines.append(f"    Use --candidates {book['abs_item_id']} to see all candidates.")
        for reason in reasons:
            ids = [str(s["source_id"]) for s in sessions if s.get("reason") == reason]
            label = ", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")
            lines.append(f"    Review [{label}]: " + reason)
        if book.get("length_help") and book["length_help"] not in reasons:
            lines.append("    Length: " + book["length_help"])
        if shelf:
            lines.append(
                f"    Shelf: {shelf['current']} -> {shelf['desired']} ({shelf['status']})"
                + (" " + shelf["reason"] if shelf.get("reason") else "")
            )
        if book.get("history_review"):
            history = book["history_review"]
            lines.append(
                f"    History preview: {history['sessions']} sessions, {history['listening_seconds']} listening seconds, since {history['oldest_started_at']}."
            )
            lines.append("    Review ranges with --json, then use " + history["approve_command"])
    planned = {s["source_id"] for b in result["books"] for s in b["sessions"]}
    for issue in result.get("source_issues", []):
        if issue["source_id"] in planned:
            continue
        if emit("source:" + issue["source_id"], {k: issue.get(k) for k in ("status", "reason")}):
            lines.append(f"  Review {issue['source_id']}: {issue['status']}. {issue['reason']}")
    if repeated:
        lines.append(
            f"  {repeated} unchanged attention entries remain unresolved. Use --show-all to print them again."
        )
    if result["result"]:
        lines.append("  Imported: " + json.dumps(result["result"], sort_keys=True))
    if result["mode"] == "preview":
        lines.append("Preview only. Use --apply after checking the matches and any history holds.")
    return lines, current


def print_report(settings, result, *, show_all=False):
    from .storage import database

    with database(settings) as con:
        lines, current = render(
            result, dict(con.execute("select key,fingerprint from attention")), show_all=show_all
        )
        for line in lines:
            print(line, flush=True)
        with con:
            con.execute("delete from attention")
            con.executemany("insert into attention values(?,?)", current.items())
