"""Conservative matching. Subtitles and author names must agree."""

import re
import unicodedata


def normal(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def title(value):
    value = re.sub(
        r"\([^)]*(?:full.cast|unabridged|audiobook|edition|surround)[^)]*\)",
        "",
        str(value or ""),
        flags=re.I,
    )
    return normal(value)


def same_work(source, target):
    product = target.get("product") or {}
    author = normal(source.get("author"))
    return (
        bool(author)
        and bool(title(source.get("title")))
        and title(source["title"]) == title(target.get("title") or product.get("title"))
        and author == normal(target.get("author") or product.get("author"))
    )


def item_metadata(item):
    media = item.get("media") or {}
    metadata = media.get("metadata") or {}
    authors = metadata.get("authors") or []
    name = metadata.get("title") or "Untitled"
    subtitle = metadata.get("subtitle")
    if subtitle and normal(subtitle) not in normal(name):
        name += ": " + subtitle
    return {
        "title": name,
        "author": metadata.get("authorName")
        or metadata.get("author")
        or ", ".join(a["name"] for a in authors if isinstance(a, dict) and a.get("name")),
        "duration": media.get("duration"),
    }
