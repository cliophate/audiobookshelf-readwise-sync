# Changelog

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
