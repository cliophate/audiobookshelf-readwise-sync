"""Preview by default. All remote writes require --apply."""

import argparse
import asyncio
import json
import os
import signal
import sys

import httpx

from . import __version__
from .clients import BooksClient
from .config import Settings, SyncError
from .sync import run_once


async def run(args):
    settings = Settings.load()
    if args.list_books:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
            books = await BooksClient(http, settings.readwise_token).list("/books")
        print(
            json.dumps(
                [
                    {key: b.get(key) for key in ("id", "title", "author", "manual_total_seconds")}
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
                if result["result"]:
                    print("  Imported: " + json.dumps(result["result"], sort_keys=True), flush=True)
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
        "--list-books", action="store_true", help="list Readwise library book IDs without importing"
    )
    parser.add_argument(
        "--accept-token-change",
        action="store_true",
        help="confirm a replacement Readwise token belongs to the same account; preview only",
    )
    args = parser.parse_args()
    if (args.list_books or args.accept_token_change) and (args.apply or args.watch):
        parser.error(
            "--list-books and --accept-token-change cannot be combined with --apply or --watch"
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
