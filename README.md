# Audiobookshelf to Readwise Books

Import your Audiobookshelf listening sessions into Readwise Books, formerly Bookwise. It imports older listening history and keeps checking for new sessions afterward.

Run it alongside Audiobookshelf in Docker. Your credentials and listening history stay on your server, with requests going to your Audiobookshelf server and Readwise. No audio files are uploaded.

The default setup uses your regular Readwise API token, including saving audiobook lengths automatically. An extra app session credential is optional and enables catalog search and adding unmatched books.

This is an early standalone release extracted from a personal reading stats app. The original integration runs daily; this extracted version has automated HTTP tests and a Docker build check. It has not yet had a separate live-account trial. Start with the preview and check a small import before enabling the background worker.

Version 0.1.8 treats different audiobook and print ISBNs/ASINs as neutral. Candidate explanations show which ABS or Readwise fields supply installment numbers. Newly resolved older sessions wait for an approved history preview. Normal watch logs suppress unchanged details. Audiobook lengths still use the public API with your regular token.

See the [changelog](CHANGELOG.md) for version history and update instructions.

## What it does

- Imports individual listening sessions, including their original dates and audio positions.
- Uses audiobook length to support percentage progress in Readwise Books.
- Checks every 15 minutes and waits for 45 minutes without activity before exporting a session.
- Tracks successful imports in SQLite so restarts do not repeat them.
- Exports only the added portion if Audiobookshelf later extends a session.
- Keeps feed posting off unless you explicitly enable it.

The sync runs from Audiobookshelf to Readwise Books. Changes in Readwise Books do not change Audiobookshelf.

Readwise Books counts audio covered. Listening for 30 minutes at 2x speed therefore contributes 60 minutes to its totals. The sync preserves ABS's measured listening time in its local source cache, but cannot change how Readwise calculates its totals.

## Set it up with Docker

You need Docker Compose v2, an Audiobookshelf API token for the user whose listening you want, and a [Readwise personal access token](https://readwise.io/access_token). A Readwise public feed token will not work.

### 1. Download and configure

```sh
git clone https://github.com/cliophate/audiobookshelf-readwise-sync.git
cd audiobookshelf-readwise-sync
cp .env.example .env
mkdir -p data
chmod 700 data
chmod 600 .env
```

Open `.env` in your editor and set:

```dotenv
AUDIOBOOKSHELF_URL=https://your-audiobookshelf-server.example
AUDIOBOOKSHELF_TOKEN=your-abs-user-token
READWISE_TOKEN=your-readwise-personal-access-token
TIMEZONE=Europe/London
```

Use your own timezone. Leave `READWISE_APP_SESSION_FILE` empty for the default setup.

The container uses UID and GID 1000 by default. If your user has different IDs, set `PUID` and `PGID` in `.env` to the values from `id -u` and `id -g`. The container needs write access to `data/`.

Use an ABS address reachable from the sync container. `localhost` points to the sync container itself. If ABS publishes a port on the Docker host, Linux users can use `http://host.docker.internal:13378`, replacing the port with their own. An existing HTTPS address also works. For container-name access, attach the sync service to the same Docker network as ABS. The sync needs no incoming ports or reverse proxy.

### 2. Prepare the books in Readwise Books

Add the books you want to sync to your Readwise Books library. The preview shows any missing audiobook lengths. With `--apply`, the sync saves the ABS duration through the public API before importing. Your regular API token is enough. If you already set a valid length yourself, the sync leaves it alone.

The sync reads the book back to verify the saved length before importing sessions. Setting a length afterward does not repair percentages on sessions already imported.

`manual_total_seconds: null` means Readwise has no saved audiobook length for that record. Having the book in both libraries does not set it. If Readwise rejects a length update, finish or cancel any active reading session for that book in the app, then retry. The sync never closes your session. If ABS has no usable duration, set the correct length yourself in Readwise Books.

For a large ABS library, start with a few books. Set `ABS_ITEM_IDS` in `.env` to their ABS item IDs, separated by commas, or use `IMPORT_SINCE` to limit listening history by date. See [Choose which books to sync](#choose-which-books-to-sync).

It automatically matches one credible library entry after checking titles, authors and available identifiers. Harmless formatting differences are allowed. Conflicting subtitles, editions, volumes and adaptations stay for review. The preview explains its decisions.

### 3. Preview

```sh
docker compose build
docker compose run --rm sync --json
```

This reads both services and updates a local cache. It makes no changes to Readwise Books, even when the optional app credential is present. It lists each ABS item ID, its matching Readwise library ID, and session statuses.

Common statuses:

| Status | What to do |
| --- | --- |
| `ready` | Check the match. These sessions can be imported. |
| `unmatched` | Add the book to Readwise Books or supply a manual mapping. |
| `ambiguous` | More than one library book matches. Supply a mapping. |
| `matching_review` | A possible existing book has conflicting or incomplete metadata. Inspect the candidates and choose a mapping if appropriate. |
| `history_needs_review` | A newly resolved match has older sessions. Review the ranges in `--json`, then approve its saved history preview. |
| `needs_length` | Use `--apply` to save the ABS duration, or set a valid length in Readwise Books. |
| `recent` | Wait for the quiet period after listening. |
| `existing` | This source session has already been imported. |
| `remote_overlap` | An existing or planned audiobook session overlaps. Review before importing. |
| `position_exceeds_length` | Check the edition and audiobook length. |
| `missing_abs_item` | ABS no longer returns this library item. Other books can still import; the source record stays in the review journal. |
| `before_import_since` | Excluded by your history cutoff. |
| `mapping_changed` | A previously imported item now points to another book. Review its ledger. |
| `book_unavailable` | A mapped book is missing or deleted in Readwise Books. |
| `needs_review` | The source is inconsistent, or a previous submission remains unresolved. |
| `setup_needs_review` | A book or duration setup was interrupted or could not be verified. Check its local journal and Readwise. |

To inspect suggested matches and save your choice:

```sh
docker compose run --rm sync --candidates ABS_ITEM_ID
docker compose run --rm sync --map ABS_ITEM_ID READWISE_BOOK_ID
docker compose run --rm sync --json
```

These commands use the ABS metadata cached by your last preview. Candidate lookup reads the complete Readwise library and compares the old and new automatic decisions. Mapping verifies that the chosen library entry is active, preserves other mappings, and makes no Readwise changes. It refuses to replace an existing different mapping or imported destination. Split parts and collections need a progress policy before a new mapping can be saved.

You can also list your Readwise library IDs:

```sh
docker compose run --rm sync --list-books
```

Alternatively, edit `data/mappings.json` with the ABS item ID from the preview and the Readwise **library book ID** from this list:

```json
{
  "your-abs-library-item-id": 123456
}
```

Use the library ID, not the catalog/product ID in a store link. A manual mapping overrides title matching, so check it carefully. New split-part or collection mappings still need a progress policy. Once an item has imported sessions, the sync refuses to move its history to a different book.

If the preview reports `history_needs_review`, inspect the destination, dates and audio ranges in `--json`. Approve that exact preview using its printed token:

```sh
docker compose run --rm sync --approve-history ABS_ITEM_ID REVIEW_TOKEN
docker compose run --rm sync --apply
```

Approval changes only local state. It covers the listed source snapshots and destination, not every past or future session for that book. Changed sources and additional old sessions need another preview. Existing timing, duration and overlap checks still apply after approval. There is no automatic repair or replay of previously exported history.

### 4. Import once, then run in the background

Check older sessions too. Initial imports still include ordinary exact matches unless you set `IMPORT_SINCE` in `.env`. Books recognized by the improved matching rules, previously unmatched books, and new mappings saved with `--map` hold older sessions for explicit history approval. For a small first import, set a recent cutoff such as `2026-10-01T00:00:00Z`.

When the preview looks right:

```sh
docker compose run --rm sync --apply
```

Read the `Imported` result. `confirmed` means the sync read the created sessions back and verified their timings and positions. Run the preview again to check the remaining statuses.

Enable ongoing imports with the explicit apply override:

```sh
docker compose -f compose.yaml -f compose.apply.yaml up -d
docker compose logs --tail=50 sync
```

Plain `docker compose up -d` runs previews only. The worker performs a poll immediately, then waits `POLL_MINUTES` after each poll completes. Typical listening appears about 45 to 60 minutes after it stops, plus API request time.

Stop the worker with `docker compose stop sync`. Nothing is deleted from either service.

### NAS and prebuilt images

The [v0.1.8 release](https://github.com/cliophate/audiobookshelf-readwise-sync/releases/tag/v0.1.8) provides tagged Docker image archives for `linux-amd64` and `linux-arm64`. Choose the one matching your NAS. Import it in Container Manager, or load it with Docker:

```sh
docker load --input audiobookshelf-readwise-sync-v0.1.8-linux-amd64.tar.gz
```

The image is named `audiobookshelf-readwise-sync:0.1.8`. Use the normal configuration above, with the image override:

```sh
docker compose -f compose.yaml -f compose.image.yaml run --rm sync --json
docker compose -f compose.yaml -f compose.image.yaml run --rm sync --apply
docker compose -f compose.yaml -f compose.image.yaml -f compose.apply.yaml up -d --no-build
```

No registry account is needed. SHA-256 checksum files accompany both downloads. A 32-bit ARM NAS is not supported by these images.

If building from source instead, pull the updated code, run `docker compose build sync`, then recreate only the sync service using the same overrides you normally use. Reusing an old container does not load the rebuilt code. The startup banner identifies the version actually running.

### Inspect skipped records

```sh
docker compose run --rm sync --issues
```

This reads the local review journal without contacting either service. `needs_review`, `missing_abs_item` and duration mismatch records retain their original source data in `source_issues`. The command lists IDs, reasons and observation dates. If a source becomes usable again, its issue is marked resolved; its journal row is retained. Export ledgers and checkpoints are never deleted to clear an error.

Ordinary logs show actionable sessions, candidate explanations and shelf changes. They omit routine existing sessions and unchanged shelves. Watch mode prints unresolved counts on every poll and repeats full entries only when they change, including across restarts. Use `--show-all` to repeat them, or `--json` for all current session details. A one-off preview always prints its current attention entries.

An item-specific 404 can be skipped. A 404, authentication failure, network failure or invalid response on a required account/session endpoint still stops the run. Error messages identify the endpoint path without printing tokens, full URLs or response bodies.

`IMPORT_SINCE` now excludes old sessions before item metadata requests, matching and per-book output. The full paginated ABS session list is still read, since the endpoint does not offer the cutoff used by this tool. Older cached sources and review records remain available locally.

For end-position rounding, the sync clips at most one second only when ABS's book length and Readwise's saved length also agree within one second. It keeps the unmodified ABS observation in the checkpoint, so later polls do not repeat that session. Larger mismatches remain `position_exceeds_length`, with the ABS position and both lengths in the preview. It never stretches a 98% session to 100% or marks a book finished on that basis.

## Choose which books to sync

By default, the sync considers books with eligible listening sessions. It does not enumerate your whole ABS library. For a small import, set an explicit allowlist in `.env`:

```dotenv
ABS_ITEM_IDS=library-item-id-1,library-item-id-2
```

The IDs are the ABS item IDs shown in the preview or your ABS book URLs. The allowlist filters sessions before item lookups, matching, automatic book setup and shelf updates. Sessions outside it contribute one summary count instead of a long list of unmatched books. It does not remove books or sessions already imported, or erase local review records.

`IMPORT_SINCE` separately limits the original start date of sessions to import. Clearing either limit makes older or additional history eligible again, using the existing ledger to avoid repeating successful imports.

## Optional shelf sync

Shelf syncing is off by default. To let ABS manage the Readwise shelf of selected books, add this to `.env`:

```dotenv
SYNC_SHELVES=true
```

| ABS state | Readwise shelf |
| --- | --- |
| No progress, or zero progress with `isFinished: false` | Want to Read |
| Progress with `isFinished: false` | Currently Reading |
| `isFinished: true` | Finished |

Completion comes from ABS's explicit flag. Reaching 98%, 99% or even 100% without that flag does not mark a book finished.

Finished also requires a valid saved Readwise audiobook duration. A missing, zero, fractional, boolean, string or out-of-range value blocks both the Finished update and new session imports. With `--apply`, the sync saves a missing or invalid duration from ABS through the public API and re-fetches the book to verify the saved value before proceeding. No optional app login is required. This also covers selected completed books with no new listening sessions.

A rejected update, uncertain write or failed verification keeps Finished blocked. Readwise rejects duration changes while a reading session for that book is active. Close that session yourself and retry. Unfinished attempts from the old temporary-session workaround also stay blocked for review. A source position above the saved duration also blocks Finished for review. The sync checks the duration again immediately before a Finished shelf write and holds the change if it has changed since planning. Want to Read and Currently Reading do not require an audio duration. Existing Finished shelves are not automatically reversed or repaired by this update.

Run the usual preview first. It shows the current shelf, proposed shelf and any review reason. Shelf changes require `--apply`. They use the [documented Books API](https://readwise.io/books_api#update-books) and need only your regular Readwise API token for books already in your Readwise library. Adding a missing book automatically still needs the optional app login.

With no allowlist, shelf sync covers books with eligible listening history plus books already bound in the local sync ledger. This lets an imported book's shelf update after you tighten `IMPORT_SINCE`. To include a book you have never started, explicitly include its ID in `ABS_ITEM_IDS`. The sync never imports every untouched book from your library automatically.

When enabled, ABS controls these shelves. Resetting progress in ABS can move a selected Readwise book back to Want to Read. Ratings, reviews and existing reading dates are preserved; the sync does not invent completion dates. Deleted Readwise books are left for review. Conflicting mappings or invalid progress block the affected shelf change.

Shelf writes are journaled before sending and verified by reading the book back. An uncertain update stays blocked for review and appears in `--issues`; it is not replayed on restart. The `shelf_attempts` table retains the original book snapshot and desired shelf. Back up `data/` before any manual ledger repair, and check the corresponding Readwise record first. Keep the database when upgrading, disabling the feature, or restoring code.

## Book matching

Matching compares the primary title, author set, subtitle and available series, volume, edition and adaptation information. It handles leading articles, accents, punctuation, apostrophes, author ordering, initial spacing and recognized academic credentials. Contributor roles are respected when supplied; narrator or foreword credits do not become primary authors. It does not infer missing author roles or expand initials into guessed names.

A subtitle can be separate, after a colon or in parentheses. If both records supply meaningful subtitles, they must agree. Without an exact identifier, an embedded title suffix cannot be dropped to match an entry that lacks it. This keeps a title such as Dune: Messiah from matching Dune. A book or series number supplied on only one side goes to review. No number on either side is not a conflict. Explicitly different numbers are rejected. Candidate output identifies each number as a series installment, an explicit volume or an otherwise unclassified trailing title number, and shows its source field. Publication editions and audio parts remain separate checks. Split parts, collections and adaptations are never silently equated with an unsplit standard book.

Valid ISBN and ASIN values are retained from ABS and the public Readwise product metadata. An exact identifier supports a match, including differently recorded names, but cannot override conflicting volumes, subtitles, editions or adaptation metadata. Different identifiers are neutral because audiobook, print and ebook identifiers often differ for the same work. Readwise also resolves edition identifiers to the canonical ebook, as described in the [public API documentation](https://readwise.io/books_api). A mismatch alone never blocks a match. It does not establish that the editions contain identical content. Meaningful subtitle, installment, edition, adaptation, split-book and collection differences still need review. Multiple plausible entries still block automatic selection. `--list-books` now preserves product metadata for offline investigation.

The full paginated library is checked before optional catalog creation. A unique acceptable entry is selected only when there are no competing plausible entries. Fuzzy title comparisons suggest candidates for review and never choose a destination. Ordinary logs explain rejected and uncertain candidates; `--candidates ABS_ITEM_ID` shows the complete candidate list. If a plausible existing entry needs review, automatic creation is blocked.

Existing ledger bindings and explicit mappings take precedence. They are not rewritten by new matching rules, and confirmed exports are not repeated even if their remote sessions were deleted. Newly recognized old sessions need a saved preview approval. New listening after that book's review boundary can still sync while older sessions remain held. Keep the existing data directory when upgrading; the new review tables are added without replacing exports, checkpoints or source records.

## Optional automatic book setup

Use device approval to obtain the extra app credential. Update to version 0.1.2 or later first. From your project directory, run:

```sh
docker compose run --rm sync --login
```

For a prebuilt NAS image, include the image override:

```sh
docker compose -f compose.yaml -f compose.image.yaml run --rm sync --login
```

Open the printed approval link on the phone where you are signed into Readwise Books. Approve **Audiobookshelf Readwise sync** using the same account as your regular Readwise API token. If the HTTPS link opens an app-download page, try the printed `bookwise://device/...` link on that phone. This fallback worked with the Android app in the original integration; iOS approval has not been tested.

Leave the command running while you approve. It waits for up to ten minutes, then saves the credential in `data/readwise-session` with permissions 600. It never prints the credential. Keep the temporary approval links private too. If approval times out, rerun the command for a fresh link.

Then add this to `.env` and leave `READWISE_APP_SESSION` unset:

```dotenv
READWISE_APP_SESSION_FILE=/data/readwise-session
```

Check a preview with `docker compose run --rm sync --json`, including the image override if needed. Then recreate the worker with the same Compose overrides you normally use. Your regular Readwise API token is still required for session imports. Device login does not need your ABS token or Readwise API token and does not import listening sessions.

If an existing app credential returns `Readwise app returned HTTP 401`, renew it with:

```sh
docker compose run --rm sync --login --replace-session
```

Include `-f compose.yaml -f compose.image.yaml` for a prebuilt NAS image. The command keeps the old credential until approval succeeds, then replaces it. Recreate the worker afterward so it reads the replacement. Your import ledger and mappings stay in place. Session lifetime is not confirmed, and there is no automatic renewal.

If you already have a valid app credential, you can still save the bare value or JSON containing `sessionid` in `data/readwise-session` yourself, with permissions 600. The login command uses `READWISE_APP_SESSION_FILE` when set, otherwise `DATA_DIR/readwise-session`. It never reads or displays an old credential.

With the credential present, `--apply` can search the app catalog and add a book when there is one exact title-and-author match. Catalog results with multiple matching editions remain for manual setup. The preview performs no app writes or automatic setup.

Length setup uses the documented `PATCH /books` endpoint with `manual_total_seconds`, independently of the app credential. It sends only the book ID and duration, verifies the result, and never creates or cancels a listening session. If you used the app login only for duration setup, you can remove `READWISE_APP_SESSION` and `READWISE_APP_SESSION_FILE` from your configuration. Keep the data directory and any unfinished setup journals for review.

The optional paths use `/bookwise/api/`, which is an internal app API and can change independently of the official API. Remove the setting to return to token-only operation. Credentials that expire need replacing. Never send a session credential to someone helping you set this up.

## History, duplicates and pauses

The first import considers all session history ABS still returns for your user. It cannot recover sessions ABS has deleted, or listening that never reached your ABS server. Zero listening time, invalid timestamps, rewinds and sessions without a forward audio span stay out of the import.

The ledger is committed before an upload. If a response is lost, later apply runs look for one exact remote session before marking it confirmed. They never blindly retry an uncertain upload. This can leave a session waiting for review even when a failed request never arrived. That is preferable to silently recording it twice.

Existing audiobook sessions with overlapping timestamps are held for review. This helps with manually tracked history, but is not a general duplicate detector for entries whose dates were changed.

When ABS extends a previously imported session, only its added listening time and forward position span are sent. ABS does not provide an exact resume timestamp here, so the new interval starts at the previous exported end. Any idle gap becomes paused time.

Deleting an imported session in Readwise Books does not make the sync recreate it. Its ledger entry remains. New listening can still import afterward.

## Investigate an inflated listening total

Use the ABS item ID for one affected book:

```sh
docker compose run --rm sync --audit ABS_ITEM_ID
```

For a prebuilt NAS image:

```sh
docker compose -f compose.yaml -f compose.image.yaml run --rm sync --audit ABS_ITEM_ID
```

The audit makes GET requests and opens an existing ledger read-only. It never imports, deletes, edits, creates a database or clears checkpoints. It works without the optional app credential. Pause the worker for a stable comparison; the ledger snapshot and live service reads happen at different times.

The JSON report includes ABS listening seconds and audio positions, confirmed local exports and their Readwise session IDs, active Readwise totals by format, repeated audio ranges, identical session groups and ABS items sharing the same Readwise book. It also shows deleted remote sessions separately. It omits credentials, reviews, captions and raw response bodies, but contains listening dates and positions. Review it before sharing.

Readwise's [documented session API](https://readwise.io/books_api) computes audiobook time from end position minus start position. It does not use wall-clock listening time or the supplied pause duration for that total. The audit keeps this figure separate from ABS's `timeListening`. Repeated ranges may reflect re-listening, seeks or cumulative source data. Sessions without a ledger ID may be manual entries or imports completed during the audit. Neither observation alone proves a duplicate. The importer now checks whether the positions are plausible before exporting. Existing sessions remain unchanged.

## Permission and NAS storage errors

Storage errors now identify the operation, file path, container UID/GID, and the path and parent ownership and modes when available. This covers credentials, mappings, the sync lock, inaccessible or read-only SQLite storage, and device-login storage. File contents and arbitrary exception text stay out of the error.

Check that `PUID` and `PGID` match the NAS user who owns the bind-mounted `data` directory. Check NAS ACLs and whether the mount is read-only too. A mode such as `777` does not establish why an operation failed, and this tool does not reject a sync database merely for having that mode. Do not loosen permissions to work around an error. Keep credential files at `600` in a private directory; the tool never changes existing permissions or ownership to recover from an access failure.

Device login verifies that its new temporary credential file has mode `600` before writing the credential. If a filesystem forces broader permissions, login stops and leaves an existing credential untouched. Use a private folder or mount that supports owner-only permissions.

## Suspect audio ranges and existing imports

An ABS session can have a unique ID but an incorrect starting position. For example, five seconds of listening paired with positions 0 to 35,285 would add almost ten audio hours to Readwise. The sync now keeps that session as `needs_review` and continues with valid sessions.

`AUDIO_MAX_PLAYBACK_SPEED` defaults to `4`. An audio span above measured listening time multiplied by this limit, plus five seconds for rounding, needs review. Measured listening time above elapsed time plus five seconds also needs review. Set the limit to your actual maximum if you listen faster than 4x. This is a plausibility check, not proof that every accepted range is accurate. Seeks within a plausible range may still be indistinguishable from playback.

To review previously imported sessions without credentials, HTTP requests or history changes:

```sh
docker compose run --rm sync --review-imports
```

For a prebuilt NAS image:

```sh
docker compose -f compose.yaml -f compose.image.yaml run --rm sync --review-imports
```

The review uses the saved payloads and source checkpoints, including listening-time deltas for cumulative sessions. It lists suspect ranges and entries whose original observations are missing. `plausible` means only that the saved interval passes these checks. Repeated audio alone does not trigger removal, and the command never deletes or rewrites sessions, creates a database, or clears the ledger.

Pause imports while investigating affected history. Use `--audit ABS_ITEM_ID` to compare flagged entries with Readwise. A local review does not prove that the remote session is still unchanged or belongs to this import. Check the session ID, book, timings, positions and device against the current remote record before any manual correction. Preserve manual entries and the existing ledger. Deleting the ledger can reimport the same bad history.

Historical starting positions cannot be recovered from `timeListening` alone, especially with accelerated playback or seeks. This version does not guess replacement ranges or automatically delete suspect imports. It also cannot repair the listening app's source records. Keep uncertain entries for review and identify the recording client and version when reporting them.

## Settings and credentials

`.env.example` lists the available settings. `POLL_MINUTES` defaults to 15 and `SETTLE_MINUTES` to 45. `IMPORT_SINCE` filters by a source session's original start time and needs a timestamp with a timezone.

Feed posting is off by default. To publish future listening, set `PUBLISH_FROM` to the earliest start timestamp you want published. A session must also arrive within 48 hours of starting. This affects only newly created sessions; it does not republish existing imports. Historical backfills can still affect Readwise's stats, XP and leaderboards.

`AUDIOBOOKSHELF_TOKEN`, `READWISE_TOKEN` and `READWISE_APP_SESSION` each support a `_FILE` alternative. Set a value or its file path, not both. With Docker, the path must exist inside the container. The default data mount can hold those files, or you can add read-only Docker secret mounts. Use HTTPS for connections outside a trusted private network.

Each person's installation needs its own `data/` directory. It contains listening metadata and the ledger in `sync.sqlite`, plus any credentials you choose to keep there. All of these paths are ignored by Git and excluded from Docker builds.

## Backups and account changes

Back up `data/` while the worker is stopped, or use SQLite's online backup facility for `sync.sqlite`. Keep the export ledger and checkpoints together. Keep `.env` and any credential files in your private backup too.

Do not delete the database to fix a sync problem. Do not restore an old ledger after successful remote imports without reconciling them first. The remote overlap check helps, but does not replace the ledger. Rolling back code does not undo remote sessions or feed posts.

The data directory is bound to your ABS server, ABS user and a hash of your Readwise token. Changing these stops imports. If you replace a Readwise token for the **same account**, stop the worker, update the token, and explicitly accept the replacement with a preview:

```sh
docker compose run --rm sync --accept-token-change
```

Only use this when you know the account is unchanged. A different pair of accounts needs separate state. Moving the same ABS server to a new address also needs a deliberate identity migration; do not work around it by discarding the ledger.

For `needs_review` or `setup_needs_review`, stop the worker and inspect the local ledger alongside the corresponding Readwise record. The `exports`, `checkpoints`, `setup_attempts` and `length_attempts` tables retain the submitted details. Do not clear an uncertain attempt just to make it retry. For legacy attempts from before 0.1.6, cancel any leftover temporary session yourself and verify its shelf and dates before repairing a setup journal. New duration writes create no temporary sessions. A known `invalid_value` rejection can retry on a later `--apply`; uncertain writes remain blocked.

## Development

Python 3.12 or newer:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Run locally with `abs-readwise-sync --json`. Docker Compose loads `.env`; a local Python run expects the variables in its environment. The tests use mock HTTP services and temporary SQLite databases. They never need real credentials.

The official [Readwise Books API documentation](https://readwise.io/books_api) describes the public endpoints. The [Audiobookshelf API documentation](https://api.audiobookshelf.org/) describes the source endpoints. This project is independent of both services.
