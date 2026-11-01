"""HTTP clients with fixed Readwise origins and no automatic write retries."""

import asyncio
from collections import Counter
from urllib.parse import quote, urlsplit

import httpx
from . import __version__

from .config import SyncError
from .audio_sources import source_issue, validate_source

READWISE_BASE = "https://readwise.io/books/api/v1"
APP_BASE = "https://readwise.io/bookwise/api"


class BooksClient:
    def __init__(self, http, token, delay=3.1):
        self.http, self.token, self.delay = http, token, delay
        self.last_request = None

    async def pace(self):
        now = asyncio.get_running_loop().time()
        if self.last_request is not None:
            await asyncio.sleep(max(0, self.delay - (now - self.last_request)))
        self.last_request = asyncio.get_running_loop().time()

    async def request(self, method, path, **kwargs):
        url = path if path.startswith("https:") else READWISE_BASE + path
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "readwise.io"
            or not parsed.path.startswith("/books/api/v1/")
        ):
            raise SyncError("Refusing to send the Readwise token outside the Books API.")
        await self.pace()
        try:
            response = await self.http.request(
                method,
                url,
                headers={
                    "Authorization": "Token " + self.token,
                    "User-Agent": "audiobookshelf-readwise-sync/" + __version__,
                },
                follow_redirects=False,
                **kwargs,
            )
        except httpx.HTTPError:
            raise SyncError(
                "Readwise request failed. Any pending write will need reconciliation."
            ) from None
        if response.status_code != 200:
            raise SyncError(
                f"Readwise {method} returned HTTP {response.status_code}. No write was retried."
            )
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError
            return data
        except ValueError:
            raise SyncError("Readwise returned an invalid response.") from None

    async def list(self, path, **params):
        result, ids, urls = [], set(), set()
        url, query = path, {"limit": 1000, **params}
        for _ in range(1000):
            if url in urls:
                raise SyncError("Readwise repeated a pagination cursor.")
            urls.add(url)
            data = await self.request(
                "GET", url, **({"params": query} if query is not None else {})
            )
            for row in data["results"]:
                if (
                    not isinstance(row, dict)
                    or not str(row.get("id", "")).isdigit()
                    or str(row["id"]) in ids
                ):
                    raise SyncError("Readwise returned invalid or duplicate record IDs.")
                ids.add(str(row["id"]))
                result.append(row)
            if data.get("next") is None:
                if "next" not in data:
                    raise SyncError("Readwise pagination response is incomplete.")
                return result
            url, query = data["next"], None
            if not isinstance(url, str) or not url:
                raise SyncError("Readwise returned an invalid pagination URL.")
        raise SyncError("Readwise pagination exceeded its page limit.")


class AppClient:
    def __init__(self, http, session, delay=3.1):
        self.http, self.session, self.delay = http, session, delay
        self.last_request = None

    async def request(self, method, path, **kwargs):
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("?")[0]:
            raise SyncError("Invalid app API path.")
        url = APP_BASE + path
        for _ in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "readwise.io"
                or not parsed.path.startswith("/bookwise/api/")
            ):
                raise SyncError("App redirect left the API. The credential was not forwarded.")
            now = asyncio.get_running_loop().time()
            if self.last_request is not None:
                await asyncio.sleep(max(0, self.delay - (now - self.last_request)))
            self.last_request = asyncio.get_running_loop().time()
            try:
                response = await self.http.request(
                    method,
                    url,
                    headers={"MOBILESESSION": self.session},
                    follow_redirects=False,
                    **kwargs,
                )
            except httpx.HTTPError:
                raise SyncError("Readwise app request failed. No write was retried.") from None
            if method == "GET" and response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise SyncError("Readwise app returned an invalid redirect.")
                url = str(response.url.join(location))
                # The redirected URL already contains any query string.
                kwargs.pop("params", None)
                continue
            if response.status_code not in (200, 201, 204):
                raise SyncError(
                    f"Readwise app returned HTTP {response.status_code}. Check the optional session credential."
                )
            if response.status_code == 204:
                return {}
            try:
                data = response.json()
                if not isinstance(data, (dict, list)):
                    raise ValueError
                return data
            except ValueError:
                raise SyncError("Readwise app returned an invalid response.") from None
        raise SyncError("Readwise app redirected too many times.")


class ABSClient:
    def __init__(self, http, url, token):
        self.http, self.url, self.token = http, url, token

    async def get(self, path, *, allow_missing=False, **params):
        try:
            response = await self.http.get(
                self.url + path,
                headers={"Authorization": "Bearer " + self.token},
                params=params,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise SyncError(
                f"Cannot reach Audiobookshelf GET {path}. Nothing was exported."
            ) from None
        if response.status_code == 404 and allow_missing:
            return None
        if response.status_code != 200:
            raise SyncError(
                f"Audiobookshelf GET {path} returned HTTP {response.status_code}. Nothing was exported."
            )
        try:
            return response.json()
        except ValueError:
            raise SyncError("Audiobookshelf returned an invalid response.") from None

    async def fetch(self, import_since=None):
        me = await self.get("/api/me")
        if not isinstance(me, dict) or not me.get("id"):
            raise SyncError("Audiobookshelf did not identify the current user.")
        all_sessions, page = [], 0
        while True:
            data = await self.get("/api/me/listening-sessions", itemsPerPage=100, page=page)
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("sessions"), list)
                or type(data.get("numPages")) is not int
                or not 0 <= data["numPages"] <= 10000
            ):
                raise SyncError("Audiobookshelf returned incomplete session pagination.")
            for session in data["sessions"]:
                if (
                    isinstance(session, dict)
                    and session.get("userId") is not None
                    and str(session["userId"]) != str(me["id"])
                ):
                    raise SyncError("Audiobookshelf returned a session for another user.")
                all_sessions.append(session)
            page += 1
            if page >= data["numPages"]:
                break
        counts = Counter(
            s.get("id")
            for s in all_sessions
            if isinstance(s, dict) and isinstance(s.get("id"), str)
        )
        sessions, issues, excluded = [], [], 0
        for source in all_sessions:
            if isinstance(source, dict) and source.get("mediaType", "book") != "book":
                continue
            reason = validate_source(source)
            if not reason and counts[source["id"]] > 1:
                reason = "Audiobookshelf returned a duplicate source ID."
            if reason:
                issues.append(source_issue(source, "needs_review", reason))
            elif import_since and float(source["startedAt"]) / 1000 < import_since.timestamp():
                excluded += 1
            else:
                sessions.append(source)
        items, missing = {}, []
        for item_id in sorted(
            {str(s["libraryItemId"]) for s in sessions if s.get("mediaType", "book") == "book"}
        ):
            path = "/api/items/" + quote(item_id, safe="")
            detail = await self.get(path, allow_missing=True, expanded=1)
            if detail is None:
                missing.append(item_id)
                issues.extend(
                    source_issue(
                        s, "missing_abs_item", f"Audiobookshelf GET {path} returned HTTP 404."
                    )
                    for s in sessions
                    if str(s["libraryItemId"]) == item_id
                )
                continue
            if not isinstance(detail, dict) or str(detail.get("id")) != item_id:
                raise SyncError("Audiobookshelf returned the wrong library item.")
            items[item_id] = detail
        return {
            "user_id": str(me["id"]),
            "sessions": sessions,
            "items": items,
            "source_records": all_sessions,
            "issues": issues,
            "missing_items": missing,
            "excluded_by_cutoff": excluded,
        }
