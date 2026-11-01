"""Source validation, retained review records, and audio endpoint rounding."""

import hashlib
import json
import math
from datetime import datetime, timezone


def validate_source(source):
    if not isinstance(source, dict):
        return "Session is not an object."
    if not isinstance(source.get("id"), str) or not source["id"]:
        return "Session has no usable source ID."
    if not isinstance(source.get("libraryItemId"), str) or not source["libraryItemId"]:
        return "Session has no usable library item ID."
    try:
        values = {
            k: float(source[k])
            for k in ("startedAt", "updatedAt", "timeListening", "startTime", "currentTime")
        }
        if not all(math.isfinite(v) for v in values.values()):
            return "Session contains non-finite timing or positions."
        if values["updatedAt"] <= values["startedAt"] or values["timeListening"] <= 0:
            return "Session has no positive listening interval."
        if not 0 <= values["startTime"] < values["currentTime"] <= 360001:
            return "Session has no valid forward audio span."
        datetime.fromtimestamp(values["startedAt"] / 1000, timezone.utc)
        datetime.fromtimestamp(values["updatedAt"] / 1000, timezone.utc)
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        return "Session timing or audio positions are invalid."
    return None


def source_key(source):
    if isinstance(source, dict) and isinstance(source.get("id"), str) and source["id"]:
        return source["id"]
    encoded = json.dumps(source, sort_keys=True, ensure_ascii=False)
    return "invalid:" + hashlib.sha256(encoded.encode()).hexdigest()[:24]


def source_issue(source, status, reason):
    return {
        "source_id": source_key(source),
        "item_id": str(source.get("libraryItemId") or "") if isinstance(source, dict) else "",
        "status": status,
        "reason": reason,
        "source": source,
    }


def ensure_issues(con, table="source_issues"):
    if table not in ("source_issues", "abs_source_issues"):
        raise ValueError("Invalid source issue table")
    con.execute(f"""create table if not exists {table}(
        source_id text primary key,item_id text,status text,reason text,data_json text,
        first_seen_at text,last_seen_at text,resolved_at text)""")


def save_issue(con, issue, table="source_issues"):
    ensure_issues(con, table)
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        f"""insert into {table} values(?,?,?,?,?,?,?,null)
        on conflict(source_id) do update set item_id=excluded.item_id,status=excluded.status,
        reason=excluded.reason,data_json=excluded.data_json,last_seen_at=excluded.last_seen_at,
        resolved_at=null""",
        (
            issue["source_id"],
            issue["item_id"],
            issue["status"],
            issue["reason"],
            json.dumps(issue["source"], ensure_ascii=False),
            now,
            now,
        ),
    )


def resolve_issue(con, source_id, table="source_issues"):
    ensure_issues(con, table)
    con.execute(
        f"update {table} set resolved_at=? where source_id=? and resolved_at is null",
        (datetime.now(timezone.utc).isoformat(), source_id),
    )


def fit_audio_endpoint(source, source_length, target_length):
    """Clip at most one second at the end when the two book lengths agree.

    Keep the actual cumulative source observation in the checkpoint. Never
    stretch a short session to completion or conceal a different edition.
    """
    target = float(target_length)
    end = float(source["currentTime"])
    if round(end) <= target:
        return source, False
    try:
        duration = float(source_length)
    except (ValueError, TypeError):
        raise ValueError("Audio position exceeds the saved length.") from None
    if (
        not all(math.isfinite(v) for v in (target, end, duration))
        or not 0 < target <= 360000
        or abs(duration - target) > 1
        or not 0 < end - target <= 1
        or abs(end - duration) > 1
        or round(float(source["startTime"])) >= target
    ):
        raise ValueError("Audio position exceeds the saved length; check the edition and duration.")
    return {
        **source,
        "currentTime": target,
        "_original_source": source.get("_original_source", source),
    }, True
