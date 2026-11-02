"""Preview by default. Book setup and session imports require --apply."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

import httpx

from . import __version__
from .clients import BooksClient
from .config import Settings, SyncError
from .login import login
from .sync import run_once
from .storage import database


async def run(args):
    if getattr(args, "login", False):
        if os.getenv("READWISE_APP_SESSION", "").strip():
            raise SyncError("Unset READWISE_APP_SESSION before using file-based device login.")
        path = os.getenv("READWISE_APP_SESSION_FILE", "").strip()
        if not path:
            path = str(Path(os.getenv("DATA_DIR", "./data")) / "readwise-session")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
            try:
                await login(http, path, replace=args.replace_session)
            except OSError:
                raise SyncError(
                    "Cannot save the app credential. Check data-directory ownership and permissions."
                ) from None
        return 0
    settings = Settings.load()
    if args.issues:
        with database(settings) as con:
            keys = ("source_id", "abs_item_id", "status", "reason", "first_seen_at", "last_seen_at")
            rows = con.execute(
                "select source_id,item_id,status,reason,first_seen_at,last_seen_at "
                "from source_issues where resolved_at is null order by last_seen_at desc"
            ).fetchall()
            shelf_rows = con.execute(
                "select item_id,book_id,desired from shelf_attempts where status!='confirmed'"
            ).fetchall()
        issues = [dict(zip(keys, row)) for row in rows]
        issues.extend(
            {
                "type": "shelf",
                "abs_item_id": item_id,
                "readwise_book_id": book_id,
                "desired_shelf": desired,
                "status": "needs_review",
                "reason": "A previous shelf write needs review. It will not be replayed.",
            }
            for item_id, book_id, desired in shelf_rows
        )
        print(json.dumps(issues, indent=2))
        return 0
    if args.list_books:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
            books = await BooksClient(http, settings.readwise_token).list("/books")
        print(
            json.dumps(
                [
                    {
                        key: b.get(key)
                        for key in ("id", "title", "author", "status", "manual_total_seconds")
                    }
                    for b in books
                    if b.get("status") != "deleted"
                ],
                indent=2,
            )
        )
        return 0
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def stop():
        stopped.set()
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    while not stopped.is_set():
        try:
            result = await run_once(
                settings, apply=args.apply, accept_token_change=args.accept_token_change
            )
            if args.json:
                print(json.dumps(result, indent=2), flush=True)
            else:
                print(
                    f"{result['mode']}: {json.dumps(result['summary'], sort_keys=True)}", flush=True
                )
                for book in result["books"]:
                    counts = {}
                    for session in book["sessions"]:
                        counts[session["status"]] = counts.get(session["status"], 0) + 1
                    print(
                        f"  {book['title']} | ABS {book['abs_item_id']} | "
                        f"Readwise {book['readwise_book_id'] or 'unmatched'} | {counts}",
                        flush=True,
                    )
                    if book.get("length_help"):
                        print("    Length: " + book["length_help"], flush=True)
                    if book.get("shelf"):
                        shelf = book["shelf"]
                        print(
                            f"    Shelf: {shelf['current']} -> {shelf['desired']} "
                            f"({shelf['status']})"
                            + (" " + shelf["reason"] if shelf.get("reason") else ""),
                            flush=True,
                        )
                if result["result"]:
                    print("  Imported: " + json.dumps(result["result"], sort_keys=True), flush=True)
                diagnostics = [
                    {
                        "source_id": row["source_id"],
                        "reason": row["reason"],
                        **{
                            key: row[key]
                            for key in (
                                "abs_position_seconds",
                                "abs_length_seconds",
                                "readwise_length_seconds",
                            )
                            if key in row
                        },
                    }
                    for book in result["books"]
                    for row in book["sessions"]
                    if row.get("reason")
                ]
                diagnostics.extend(result.get("source_issues", []))
                for issue in diagnostics[:20]:
                    print("  Review: " + json.dumps(issue, sort_keys=True), flush=True)
                if len(diagnostics) > 20:
                    print(
                        "  More records are retained locally. Use --issues to list them.",
                        flush=True,
                    )
                if not args.apply:
                    print("Preview only. Use --apply after checking the matches.", flush=True)
        except (SyncError, ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            # Only SyncError messages are approved for display. HTTP bodies and URLs
            # can contain private data; do not print arbitrary exception messages.
            message = (
                str(exc)
                if isinstance(exc, SyncError)
                else type(exc).__name__ + ". Check configuration and state."
            )
            print("Sync stopped: " + message, file=sys.stderr, flush=True)
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        try:
            await asyncio.wait_for(stopped.wait(), settings.poll_minutes * 60)
        except TimeoutError:
            pass
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Sync Audiobookshelf to Readwise Books. Default: read-only preview."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--apply", action="store_true", help="allow book setup and session imports")
    parser.add_argument("--watch", action="store_true", help="repeat at POLL_MINUTES intervals")
    parser.add_argument("--json", action="store_true", help="include session details as JSON")
    parser.add_argument(
        "--login", action="store_true", help="approve an optional app login on your phone"
    )
    parser.add_argument(
        "--replace-session",
        action="store_true",
        help="replace an existing app credential with --login",
    )
    parser.add_argument(
        "--issues",
        action="store_true",
        help="list retained unresolved source and shelf issues without HTTP requests",
    )
    parser.add_argument(
        "--list-books", action="store_true", help="list Readwise library book IDs without importing"
    )
    parser.add_argument(
        "--accept-token-change",
        action="store_true",
        help="confirm a replacement Readwise token belongs to the same account; preview only",
    )
    args = parser.parse_args()
    if args.replace_session and not args.login:
        parser.error("--replace-session requires --login")
    if args.login and any(
        (args.apply, args.watch, args.json, args.list_books, args.accept_token_change, args.issues)
    ):
        parser.error("Run --login separately from sync, preview and inspection commands")
    if (args.list_books or args.accept_token_change or args.issues) and (args.apply or args.watch):
        parser.error(
            "--list-books, --issues and --accept-token-change cannot be combined with --apply or --watch"
        )
    if sum((args.list_books, args.accept_token_change, args.issues)) > 1:
        parser.error("Choose only one of --list-books, --issues or --accept-token-change")
    print(
        f"audiobookshelf-readwise-sync {__version__} "
        f"(revision {os.getenv('SYNC_BUILD_REVISION', 'source')})",
        file=sys.stderr,
        flush=True,
    )
    try:
        return asyncio.run(run(args))
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
