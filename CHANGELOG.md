# Changelog

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
