"""Configuration, without reading credentials until an operation needs them."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .permissions import access_message, local_call
from .audio_safety import playback_limit


class SyncError(Exception):
    """An error whose message can be shown without exposing credentials."""


def credential(name, *, required=True):
    value = os.getenv(name, "").strip()
    file = os.getenv(name + "_FILE", "").strip()
    if value and file:
        raise SyncError(f"Set either {name} or {name}_FILE, not both.")
    if file:
        try:
            value = Path(file).read_text().strip()
        except PermissionError:
            raise SyncError(access_message(f"reading {name}_FILE", file)) from None
        except OSError:
            raise SyncError(f"Cannot read {name}_FILE.") from None
    if name == "READWISE_APP_SESSION" and value.startswith("{"):
        try:
            value = json.loads(value)["sessionid"]
        except (ValueError, KeyError, TypeError):
            raise SyncError("App session JSON must contain sessionid.") from None
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise SyncError(f"{name} must be a token without whitespace.")
    if not value and required:
        raise SyncError(f"Set {name} or {name}_FILE.")
    return value


def minutes(name, default):
    try:
        value = int(os.getenv(name, str(default)))
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        raise SyncError(f"{name} must be a positive whole number.") from None


def boolean(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ("true", "false"):
        raise SyncError(f"{name} must be true or false.")
    return value == "true"


def item_ids():
    value = os.getenv("ABS_ITEM_IDS", "").strip()
    if not value:
        return ()
    ids = tuple(dict.fromkeys(x.strip() for x in value.split(",")))
    if any(not x or any(c.isspace() for c in x) or "/" in x for x in ids):
        raise SyncError("ABS_ITEM_IDS must be comma-separated Audiobookshelf item IDs.")
    return ids


@dataclass(frozen=True)
class Settings:
    abs_url: str
    abs_token: str
    readwise_token: str
    app_session: str
    data_dir: Path
    mappings_file: Path
    poll_minutes: int
    settle_minutes: int
    timezone: str
    import_since: datetime | None
    sync_shelves: bool = False
    abs_item_ids: tuple[str, ...] = ()

    @classmethod
    def load(cls, *, include_app=True):
        try:
            playback_limit()
        except ValueError as exc:
            raise SyncError(str(exc)) from None
        url = os.getenv("AUDIOBOOKSHELF_URL", "").rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SyncError("Set AUDIOBOOKSHELF_URL to your server's HTTP or HTTPS address.")
        timezone = os.getenv("TIMEZONE", "UTC")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise SyncError("TIMEZONE must be an IANA timezone, such as Europe/London.") from None
        since = os.getenv("IMPORT_SINCE", "").strip()
        publish = os.getenv("PUBLISH_FROM", "").strip()
        try:
            dates = [
                datetime.fromisoformat(d.replace("Z", "+00:00")) if d else None
                for d in (since, publish)
            ]
            if any(d is not None and d.tzinfo is None for d in dates):
                raise ValueError
        except ValueError:
            raise SyncError(
                "IMPORT_SINCE and PUBLISH_FROM need ISO dates with a timezone."
            ) from None
        data = Path(os.getenv("DATA_DIR", "./data"))
        return cls(
            url,
            credential("AUDIOBOOKSHELF_TOKEN"),
            credential("READWISE_TOKEN"),
            credential("READWISE_APP_SESSION", required=False) if include_app else "",
            data,
            Path(os.getenv("MAPPINGS_FILE", str(data / "mappings.json"))),
            minutes("POLL_MINUTES", 15),
            minutes("SETTLE_MINUTES", 45),
            timezone,
            dates[0],
            boolean("SYNC_SHELVES"),
            item_ids(),
        )

    def mappings(self):
        try:
            local_call("checking book mappings", self.mappings_file, self.mappings_file.stat)
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(self.mappings_file.read_text())
            if not isinstance(data, dict):
                raise ValueError
            if any(
                not isinstance(k, str)
                or not k
                or isinstance(v, bool)
                or not str(v).isdigit()
                or int(v) < 1
                for k, v in data.items()
            ):
                raise ValueError
            return {k: str(v) for k, v in data.items()}
        except PermissionError:
            raise SyncError(access_message("reading book mappings", self.mappings_file)) from None
        except (OSError, ValueError, TypeError):
            raise SyncError(
                "Mappings must be JSON mapping ABS item IDs to Readwise library book IDs."
            ) from None
