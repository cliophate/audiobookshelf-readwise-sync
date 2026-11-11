# Audiobookshelf to Readwise Books

This repository is public, and other people run it against their own
libraries. Never put credentials, session values, personal library data or
users' log contents in Git, issues, examples or tests. Use synthetic fixtures.

## Releases

Every user-visible change gets a version bump in `pyproject.toml`, a
`CHANGELOG.md` entry and a `v*` tag. Update the version summary in README.md
when it describes the new behavior. CI runs ruff, the unit tests and a Docker
build. Run `ruff check .`, `ruff format --check .` and
`python3 -m unittest discover -s tests -v` before pushing.

Never weaken the safety checks to make a reported case pass: held history,
blocked reassignment, and no writes to existing Readwise sessions.

## Users and replies

User reports arrive by email through the user. When asked for a reply, write
a short one in the user's voice. It must not read as AI-written. Fix the
reported bug. List feature suggestions for the user to decide on instead of
building them.

The original integration runs on hebe in `/home/clio/Projects/readwisestats`.
When fixing a bug here, check whether it also affects that integration, and
tell the user.
