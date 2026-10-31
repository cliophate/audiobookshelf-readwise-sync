# Audiobookshelf to Readwise Books

Import your Audiobookshelf listening sessions into Readwise Books, formerly Bookwise. It imports older listening history and keeps checking for new sessions afterward.

Run it alongside Audiobookshelf in Docker. Your credentials and listening history stay on your server, with requests going to your Audiobookshelf server and Readwise. No audio files are uploaded.

The default setup uses your regular Readwise API token. An extra app session credential is optional and enables automatic book setup.

This is an early standalone release extracted from a personal reading stats app. The original integration runs daily; this extracted version has automated HTTP tests and a Docker build check. It has not yet had a separate live-account trial. Start with the preview and check a small import before enabling the background worker.

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

Add the books you want to sync to your Readwise Books library. For each one, set the correct audiobook length in the app's manual audiobook tracking form. If saving the length starts a timer, cancel that temporary session afterward.

This step matters. The sync waits for a saved audiobook length before importing sessions. Setting a length afterward does not repair percentages on sessions already imported.

It automatically matches a book when exactly one library entry has the same title and author. Capitalization, punctuation and common edition labels are ignored. Different subtitles stay distinct. Use a manual mapping when titles or author credits differ.

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
| `needs_length` | Set the audiobook length in Readwise Books. |
| `recent` | Wait for the quiet period after listening. |
| `existing` | This source session has already been imported. |
| `remote_overlap` | An existing or planned audiobook session overlaps. Review before importing. |
| `position_exceeds_length` | Check the edition and audiobook length. |
| `before_import_since` | Excluded by your history cutoff. |
| `mapping_changed` | A previously imported item now points to another book. Review its ledger. |
| `book_unavailable` | A mapped book is missing or deleted in Readwise Books. |
| `needs_review` | The source is inconsistent, or a previous submission remains unresolved. |
| `setup_needs_review` | An optional app setup was interrupted. Check its local journal and the app. |

To map a book yourself, list your Readwise library IDs:

```sh
docker compose run --rm sync --list-books
```

Create `data/mappings.json` with the ABS item ID from the preview and the Readwise **library book ID** from this list:

```json
{
  "your-abs-library-item-id": 123456
}
```

Use the library ID, not the catalog/product ID in a store link. A manual mapping overrides title matching, so check it carefully. Once an item has imported sessions, the sync refuses to move its history to a different book.

### 4. Import once, then run in the background

Check older sessions too. They are included unless you set `IMPORT_SINCE` in `.env`. For a small first import, set a recent cutoff such as `2026-10-01T00:00:00Z`; clearing it later makes older history eligible again.

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

## Optional automatic book setup

If you already have a valid session credential for your own Readwise Books app login, save it in `data/readwise-session`. It can be the bare session value or JSON containing `sessionid`. Set file permissions to 600, then add this to `.env`:

```dotenv
READWISE_APP_SESSION_FILE=/data/readwise-session
```

This is a session for the same Readwise account. It does not require a second account or an administrator login. The normal Readwise API token is still required for session imports. This project does not include a login flow for obtaining the optional credential.

With the credential present, `--apply` can search the app catalog, add a book when there is one exact title-and-author match, and save a missing audiobook length. Catalog results with multiple matching editions remain for manual setup. The preview performs no app writes or automatic setup.

Saving a length uses the app's manual-session workflow: start a temporary audiobook session, cancel it immediately, verify the saved length, and restore any affected shelf or dates. It leaves existing lengths alone and does not interrupt an active manual session. A failed or uncertain setup blocks that book for review, including when the length was saved but cancellation failed.

The optional paths use `/bookwise/api/`, which is an internal app API and can change independently of the official API. Remove the setting to return to token-only operation. Credentials that expire need replacing. Never send a session credential to someone helping you set this up.

## History, duplicates and pauses

The first import considers all session history ABS still returns for your user. It cannot recover sessions ABS has deleted, or listening that never reached your ABS server. Zero listening time, invalid timestamps, rewinds and sessions without a forward audio span stay out of the import.

The ledger is committed before an upload. If a response is lost, later apply runs look for one exact remote session before marking it confirmed. They never blindly retry an uncertain upload. This can leave a session waiting for review even when a failed request never arrived. That is preferable to silently recording it twice.

Existing audiobook sessions with overlapping timestamps are held for review. This helps with manually tracked history, but is not a general duplicate detector for entries whose dates were changed.

When ABS extends a previously imported session, only its added listening time and forward position span are sent. ABS does not provide an exact resume timestamp here, so the new interval starts at the previous exported end. Any idle gap becomes paused time.

Deleting an imported session in Readwise Books does not make the sync recreate it. Its ledger entry remains. New listening can still import afterward.

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

For `needs_review` or `setup_needs_review`, stop the worker and inspect the local ledger alongside the corresponding Readwise record. The `exports`, `checkpoints`, `setup_attempts` and `length_attempts` tables retain the submitted details. Do not clear an uncertain attempt just to make it retry. Cancel any leftover temporary session in the app and verify its shelf and dates before repairing a setup journal.

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
