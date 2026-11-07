"""Frozen pre-upgrade predicate used only to identify newly eligible history.

Do not use this matcher to select export destinations. Keeping the old rules
here lets the upgrade hold sessions that the previous release would reject.
"""

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
    other = {**product, **{k: v for k, v in target.items() if v is not None}}
    for key in ("title", "author"):
        other[key] = target.get(key) or product.get(key)
    if not author or author != normal(other.get("author")):
        return False
    a, b = primary_title(source), primary_title(other)
    target_title = str(other.get("title") or "")
    titles_agree = a == b or (
        ":" not in primary_text(source)
        and ":" in target_title
        and a == title(target_title.split(":", 1)[0])
    )
    if not a or not titles_agree:
        return False
    source_numbers, target_numbers = number_marks(source), number_marks(other)
    return not any(
        source_numbers[key] != target_numbers[key]
        for key in source_numbers.keys() & target_numbers.keys()
    )


def primary_title(metadata):
    return title(primary_text(metadata))


def primary_text(metadata):
    text = str(metadata.get("title") or "")
    subtitle = metadata.get("subtitle")
    # Remove only a known separate subtitle duplicated in the title itself.
    if subtitle and ":" in text and title(text.rsplit(":", 1)[1]) == title(subtitle):
        return text.rsplit(":", 1)[0]
    return text


def number_marks(metadata):
    words = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11)))
    roman = dict(zip("i ii iii iv v vi vii viii ix x".split(), range(1, 11)))
    result = {}
    text = str(metadata.get("title") or "") + " " + str(metadata.get("subtitle") or "")
    for label, number in re.findall(
        r"\b(book|volume|vol|part)\s*\.?\s*#?\s*(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|[ivx]+)\b",
        text,
        flags=re.I,
    ):
        number = number.lower()
        value = words.get(number, roman.get(number, number))
        result["part" if label.lower() == "part" else "book"] = (
            format(float(value), "g") if re.fullmatch(r"\d+(?:\.\d+)?", str(value)) else str(value)
        )
    series = metadata.get("series") or []
    if isinstance(series, list):
        numbers = {
            format(float(s["sequence"]), "g")
            for s in series
            if isinstance(s, dict) and re.fullmatch(r"\d+(?:\.\d+)?", str(s.get("sequence", "")))
        }
        if len(numbers) == 1:
            result.setdefault("book", numbers.pop())
    return result


def item_metadata(item):
    media = item.get("media") or {}
    metadata = media.get("metadata") or {}
    authors = metadata.get("authors") or []
    name = metadata.get("title") or "Untitled"
    return {
        "title": name,
        "subtitle": metadata.get("subtitle"),
        "series": metadata.get("series") or [],
        "author": metadata.get("authorName")
        or metadata.get("author")
        or ", ".join(a["name"] for a in authors if isinstance(a, dict) and a.get("name")),
        "duration": media.get("duration"),
    }
