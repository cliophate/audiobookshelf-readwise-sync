"""Preview by default. Book setup and session imports require --apply."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

import httpx

from . import __version__
from . import history
from .attention import print_report
from .audit import audit_book
from .clients import BooksClient
from .config import Settings, SyncError
from .login import login
from .import_review import review_database
from .permissions import access_message, local_call
from .sync import resolve_match, run_once
from .storage import database
from .matching import catalog_candidates
from .review_commands import cached_metadata, check_identity, save_mapping


async def run(args):
    if getattr(args, "review_imports", False):
        path = Path(os.getenv("DATA_DIR", "./data")) / "sync.sqlite"
        try:
            result = review_database(path)
        except PermissionError:
            raise SyncError(access_message("reviewing existing imports", path)) from None
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise SyncError(
                "Cannot review the export ledger. Keep it and check its integrity and configuration."
            ) from None
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    if getattr(args, "login", False):
        if os.getenv("READWISE_APP_SESSION", "").strip():
            raise SyncError("Unset READWISE_APP_SESSION before using file-based device login.")
        path = os.getenv("READWISE_APP_SESSION_FILE", "").strip()
        if not path:
            path = str(Path(os.getenv("DATA_DIR", "./data")) / "readwise-session")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
            try:
                await login(http, path, replace=args.replace_session)
            except PermissionError:
                raise SyncError(
                    access_message("saving the optional app credential", path)
                ) from None
            except OSError as exc:
                raise SyncError(
                    access_message(
                        f"saving the optional app credential, OS error {exc.errno}", path
                    )
                ) from None
        return 0
    if getattr(args, "audit", None):
        settings = Settings.load(include_app=False)
        print(json.dumps(await audit_book(settings, args.audit), indent=2, allow_nan=False))
        return 0
    review_action = next(
        (name for name in ("candidates", "map", "approve_history") if getattr(args, name, None)),
        None,
    )
    settings = Settings.load(include_app=False) if review_action else Settings.load()
    if review_action:
        with database(settings) as con:
            check_identity(con, settings)
            if review_action == "approve_history":
                result = history.approve(con, settings, *args.approve_history)
            else:
                item_id = args.candidates if review_action == "candidates" else args.map[0]
                metadata = cached_metadata(con, item_id)
                async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
                    api = BooksClient(http, settings.readwise_token)
                    if review_action == "candidates":
                        books = await api.list("/books")
                        candidates = catalog_candidates(metadata, books)
                        selected, state, matching = resolve_match(
                            con, item_id, metadata, books, settings.mappings()
                        )
                        result = {
                            "abs_item_id": item_id,
                            "title": metadata["title"],
                            "candidates": candidates,
                            "matching_status": state,
                            "selected_readwise_book_id": str(selected["id"]) if selected else None,
                            "previous_status": matching.get("previous_status"),
                            "previous_readwise_book_ids": matching.get(
                                "previous_readwise_book_ids", []
                            ),
                            "reason": "No related library entries found."
                            if not candidates
                            else "Choose an active library book ID with --map ABS_ITEM_ID READWISE_BOOK_ID.",
                        }
                    else:
                        bid = args.map[1]
                        if not bid.isdigit() or int(bid) < 1:
                            raise SyncError(
                                "Readwise library book ID must be a positive whole number."
                            )
                        books = await api.list("/books", ids=bid)
                        if len(books) != 1 or str(books[0]["id"]) != bid:
                            raise SyncError("Selected Readwise library book was not found.")
                        result = local_call(
                            "saving book mappings",
                            settings.mappings_file,
                            lambda: save_mapping(con, settings, item_id, books[0]),
                        )
        print(json.dumps(result, indent=2))
        return 0
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
                        for key in (
                            "id",
                            "title",
                            "author",
                            "status",
                            "manual_total_seconds",
                            "product",
                            "isbn",
                            "asin",
                            "subtitle",
                            "series",
                        )
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
                print_report(
                    settings, result, show_all=getattr(args, "show_all", False) or not args.watch
                )
        except (SyncError, ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            # Only SyncError messages are approved for display. HTTP bodies and URLs
            # can contain private data; do not print arbitrary exception messages.
            if isinstance(exc, SyncError):
                message = str(exc)
            elif isinstance(exc, PermissionError):
                message = access_message("accessing sync files", settings.data_dir)
            else:
                message = type(exc).__name__ + ". Check configuration and state."
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
        "--show-all", action="store_true", help="repeat unchanged attention entries in watch logs"
    )
    parser.add_argument(
        "--candidates",
        metavar="ABS_ITEM_ID",
        help="show all related library entries and matching explanations",
    )
    parser.add_argument(
        "--map",
        nargs=2,
        metavar=("ABS_ITEM_ID", "READWISE_BOOK_ID"),
        help="save a manual mapping without importing or changing Readwise",
    )
    parser.add_argument(
        "--approve-history",
        nargs=2,
        metavar=("ABS_ITEM_ID", "REVIEW_TOKEN"),
        help="approve exactly the historical source snapshots in a saved preview; no remote writes",
    )
    parser.add_argument(
        "--review-imports",
        action="store_true",
        help="review existing imports locally without credentials, HTTP requests or history changes",
    )
    parser.add_argument(
        "--audit",
        metavar="ABS_ITEM_ID",
        help="read-only comparison of one book's ABS, ledger and Readwise sessions",
    )
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
    review_actions = (args.candidates, args.map, args.approve_history)
    if any(review_actions) and (
        sum(bool(a) for a in review_actions) > 1
        or any(
            (
                args.apply,
                args.watch,
                args.audit,
                args.login,
                args.replace_session,
                args.issues,
                args.list_books,
                args.accept_token_change,
                args.review_imports,
            )
        )
    ):
        parser.error("Run --candidates, --map or --approve-history separately from other commands")
    if args.review_imports and any(
        (
            args.apply,
            args.watch,
            args.audit,
            args.login,
            args.replace_session,
            args.issues,
            args.list_books,
            args.accept_token_change,
        )
    ):
        parser.error(
            "Run --review-imports separately from login, sync and other inspection commands"
        )
    if args.audit and any(
        (
            args.apply,
            args.watch,
            args.login,
            args.replace_session,
            args.issues,
            args.list_books,
            args.accept_token_change,
        )
    ):
        parser.error("Run --audit separately from login, imports and other inspection commands")
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
