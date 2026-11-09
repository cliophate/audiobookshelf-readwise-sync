# Changelog

## 0.1.9 - 2026-11-09

### Fixed

- Series annotations such as `#1`, `#13` and `Series, #8` now supply installment numbers. Explicitly different installments are rejected rather than remaining plausible and causing false ambiguity. Audio parts remain separate from book numbers, and meaningful subtitles and competing candidates keep their checks.
- `--map` can save a split-book or collection choice. It reports `needs_policy`; progress imports, duration setup and shelf changes remain blocked for that new mapping. No history is approved. Reassignment of an existing destination remains blocked.
- Preview and apply logs include the author and available subtitle. Candidate titles stay on one line. Shelf-only entries show the book status and `no session actions` instead of `{}`; an unset shelf is labelled explicitly. Identity-label changes are visible even when the underlying issue is unchanged.

### Safety and tests

- Exact ISBN/ASIN matches still do not approve previously held history. Existing mappings, exports and checkpoints remain intact. Added regressions cover the reported hash-number conflicts, duplicate candidates, split and collection mappings, blocked shelf and duration writes, history holds and clearer logs.

## 0.1.8 - 2026-11-07

### Fixed

- Different ISBNs or ASINs no longer block a work match. Audiobook and print or ebook editions commonly use different identifiers. Exact valid identifiers remain strong evidence, subject to the existing content and structure checks.
- Candidate inspection identifies series installments, explicit volumes and unclassified title numbers, with the source field on each side. Missing-number and conflict explanations show the actual ABS and Readwise evidence. No number on either side is not a conflict.
- Ambiguous entries, conflicting installments, meaningful subtitles, changed content editions, split books, adaptations and collections retain their review checks. Manual mapping still does not approve history. Existing destinations, exports and checkpoints remain unchanged.

### Tests

- Added cross-format regressions from the reported examples, identifier-independent content checks, Hunter's Way metadata explanations, number types and a history hold for a previously blocked ISBN match.

## 0.1.7 - 2026-11-07

### Fixed

- Matching preserves meaningful subtitles, numbered editions, book volumes, audio parts and adaptations. Conflicting or incomplete metadata stays for review instead of sending progress to a guessed destination.
- Title and author normalization handles leading articles, apostrophes, accents, author order, initial spacing and recognized academic credentials. Subtitles can be stored separately or embedded in titles. Available ISBN/ASIN fields are retained and checked, with ISBN-10/13 normalization and format validation.
- A plausible existing library entry blocks automatic creation of another book. Multiple possible entries stay ambiguous even when one passes the automatic checks.

### Added

- `--candidates ABS_ITEM_ID` explains library candidates. `--map ABS_ITEM_ID READWISE_BOOK_ID` verifies the target and saves a mapping while preserving other mappings and saved destinations. Neither command writes to Readwise.
- Newly recognized or previously unmatched old sessions require a saved history preview and explicit `--approve-history ABS_ITEM_ID REVIEW_TOKEN` before import. Approval covers exact source snapshots and one destination. Changed observations and additional old sources remain held. Existing exports, checkpoints and account bindings are preserved; no historical records are repaired or replayed.
- Normal logs show actionable entries, matching explanations and overlap IDs. Watch logs retain unresolved counts but deduplicate unchanged explanations across polls and restarts. `--show-all` repeats them; `--json` retains full detail.
- Regression fixtures cover all 32 reported candidate pairs, plus unsafe subtitle/edition matches, identifiers, contributor roles, historical approvals, saved destinations and log deduplication.

## 0.1.6 - 2026-11-03

### Changed

- Missing or invalid audiobook lengths are saved through the documented `PATCH /books` field `manual_total_seconds`, using the regular Readwise API token. The temporary-session start/cancel workaround has been removed. `--apply` still controls remote writes.
- A fresh read checks the book before setting its length, and another verifies the saved length and preserved shelf, dates, rating, review and page count. Valid existing lengths are left alone. Verification failures block imports and Finished updates.
- Readwise can reject a length update while a reading session is open for that book. An explicit `invalid_value` rejection stays blocked and can retry after the session is closed. An uncertain write or unfinished attempt from the old workaround stays in the journal for review and is never replayed automatically.
- The optional app login is now used only for catalog search and adding unmatched books. It is no longer required for automatic length setup. Keep the existing data directory and journals when upgrading.

### Tests

- Added coverage for token-only duration setup, read-back verification, rejected and uncertain writes, preservation of manually chosen lengths, concurrent edits and legacy cancellation failures.

## 0.1.5 - 2026-11-03

### Fixed

- Implausible ABS audio ranges are held as `needs_review`. The sync compares the audio span with measured listening time, allows playback up to `AUDIO_MAX_PLAYBACK_SPEED` by default 4x plus five seconds, and checks listening time against elapsed time with the same five-second allowance. Distinct source IDs with incorrect starting positions can no longer bypass this check. Valid sessions continue.
- Finished shelf changes are blocked when source timing needs review. Original source records and export checkpoints remain intact. The sync never estimates a missing starting position or removes genuine re-listening.

### Added

- `--review-imports` checks existing exports and their saved source observations locally. It needs no credentials or network connection and changes no files or history. It distinguishes suspect timing from missing observations, accounts for cumulative-session listening deltas, and reports the affected Readwise session IDs for review.
- Per-book audits include suspect source ranges and the existing-import review. These reports are previews, not deletion lists. Missing positions cannot be reconstructed from listening time alone, and current remote ownership and edits must be checked before any manual correction.
- Regression tests for the reported five-second session covering almost ten audio hours, separate source IDs, legitimate re-listening, fast playback, continuation deltas, and read-only history review.

## 0.1.4 - 2026-11-02

### Fixed

- Finished shelf changes require a valid saved audiobook duration. Optional setup can repair missing or invalid values and verifies the saved duration before imports and completion. Selected completed books without new sessions also get this check. Failed or uncertain setup stays blocked for review.
- Finished checks the fresh duration before writing, and blocks changes if the duration changed or a source position exceeds it. Other shelves remain independent of audio duration.
- Filesystem access errors identify the operation, path, UID/GID, ownership and modes without reading file contents or loosening permissions. SQLite access failures and read-only mounts have storage diagnostics.
- Device login verifies mode `600` before writing a new credential. A filesystem that forces broader permissions cannot receive the credential; replacement leaves the previous file intact on failure.

### Added

- `--audit ABS_ITEM_ID` compares one book's live ABS history, local export ledger and Readwise sessions. It is read-only and reports listening time, audio covered, totals by format, repeated ranges, identical session groups and shared mappings. No history is deleted or repaired automatically.
- Regression coverage for a seven-hour cumulative source across repeated updates. It exports seven audio hours, rather than the sum of full cumulative snapshots. The reported 29-hour case still needs the affected book's audit output; no accounting change is claimed for it.

## 0.1.3 - 2026-11-02

### Added

- Optional `SYNC_SHELVES=true` maps ABS progress to Want to Read, Currently Reading and Finished. It previews changes, requires `--apply`, preserves ratings, reviews and dates, and uses ABS's completion flag rather than rounding a percentage to finished.
- `ABS_ITEM_IDS` limits metadata requests, matching, imports and shelf changes to an explicit list of books. Untouched books require explicit selection and shelf sync; the tool does not enumerate or import the whole ABS library.
- A durable shelf-write journal and read-back verification. Failed or uncertain writes stay available through `--issues` and are not replayed. Keep the existing data directory on upgrade.
- Missing-duration guidance explains `manual_total_seconds: null`, manual setup, optional automatic setup and active manual sessions that prevent setup.

### Fixed

- ABS subtitles remain separate from the primary title during matching. Title and author drive matching, supporting metadata retains series information, and conflicting book or part numbers still prevent a match.
- Multiple matching library books or catalog editions are held for review. Existing mappings and imported book identities remain fixed. Deleted Readwise books are not restored automatically.

## 0.1.2 - 2026-11-01

### Added

- `--login` starts Readwise Books device approval. Open the printed link on your signed-in phone and approve the device to save the optional app credential locally with permissions 600. No passwords or existing API tokens are needed for this command.
- `--login --replace-session` renews an expired or rejected app credential. A failed approval leaves the previous file intact. The credential never appears in terminal output, and the command leaves import history and mappings alone.
- Instructions for source builds and prebuilt NAS images, including the direct app link that worked on Android in the original integration. The login endpoints are internal, session lifetime is unconfirmed, and iOS approval remains untested.

### Fixed

- Optional app HTTP 401 errors now point to the renewal command. The earlier setup instructions assumed an existing app credential without explaining how to obtain one.

## 0.1.1 - 2026-11-01

### Fixed

- A missing Audiobookshelf item no longer stops the whole sync. Its sessions are skipped as `missing_abs_item`, and other books continue. Failures on required account or session endpoints still stop the sync.
- Invalid source records are skipped as `needs_review`. The local database keeps their original data and review history, including when a later poll omits them.
- `IMPORT_SINCE` filters old sessions before book lookups and matching. Audiobookshelf session history still needs to be fetched, but older sessions no longer trigger unnecessary item requests or unmatched-book output.
- Audio positions can be clipped by at most one second at the end of a book when the Audiobookshelf and Readwise lengths agree within one second. Larger differences remain `position_exceeds_length` errors. The original source position stays in the checkpoint to prevent repeat imports.

### Added

- `--issues` lists unresolved source problems from the local database without contacting either service.
- Errors identify the failing endpoint without including credentials or response bodies. Length errors include the source position and both book lengths in the preview.
- Startup logs show the version and source revision so an outdated Docker image is easier to spot.
- Prebuilt Docker image archives for Linux amd64 and arm64, with checksums and a Compose override for loading them on a NAS.
- Regression coverage for missing items, retained review records, import cutoffs, and audio length rounding. All 57 tests pass in the release images for both architectures.

## 0.1.0 - 2026-10-31

Initial source publication, extracted from the personal Bookstats integration.

- Imports historical and new Audiobookshelf listening sessions through the documented Readwise Books API.
- Uses a regular Readwise API token by default. Optional app session credentials enable automatic book setup.
- Supports explicit book mappings and conservative title-and-author matching.
- Previews imports before writing. SQLite checkpoints track submitted sessions and later additions to existing sessions.
- Keeps feed posting off by default, with date limits for users who enable it.
- Includes Docker Compose setup, background polling, and credential-file support.

## Updating

Keep the existing `data` directory when updating. It contains the import checkpoints, mappings, and review records.

For a source build, rebuild the image and recreate the container so Docker uses the updated code:

```sh
docker compose build --pull --no-cache
docker compose up -d
```

For a prebuilt image, follow the image-loading instructions in the [README](README.md). Check the startup log for the expected version after either update method.
