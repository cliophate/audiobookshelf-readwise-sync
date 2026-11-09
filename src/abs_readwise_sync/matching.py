"""Conservative work matching, with explanations for review candidates."""

from functools import lru_cache
import json
import re
import unicodedata

NUMBER = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|[ivxlcdm]+)"
BOOK = rf"(?:\b(?:book|volume|vol)\s*\.?\s*#?\s*|(?<![\w#])#\s*)({NUMBER})\b"
PART = rf"\bpart\s*#?\s*({NUMBER})(?:\s+of\s+(\d+))?\b|\b(\d+)\s+of\s+(\d+)\b"
EDITION = r"\b(?:(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|revised|updated|expanded|updated and expanded)\s+)?edition\b"
ROLE = r"\b(?:narrator|narrated by|translator|translated by|foreword|introduction by|lecturer|production)\b"
CREDENTIAL = r"\b(?:ph\.?\s*d\.?|m\.?\s*d\.?|d\.?\s*phil\.?|mba|esq\.?)\s*(?=[,;&]|$)"


def normal(value):
    text = str(value or "").translate(str.maketrans({"’": "", "'": "", "‘": ""}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def title(value):
    # Only harmless, complete format labels can disappear here.
    text = re.sub(
        r"[\[(]\s*(?:unabridged|audiobook|surround sound)\s*[\])]", "", str(value or ""), flags=re.I
    )
    return re.sub(r"^(?:the|an|a) ", "", normal(text))


def numeric(value):
    words = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11)))
    value = str(value).lower()
    if re.fullmatch(r"(?=.)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})", value):
        values = dict(zip("ivxlcdm", (1, 5, 10, 50, 100, 500, 1000)))
        total, previous = 0, 0
        for letter in reversed(value):
            current = values[letter]
            total += -current if current < previous else current
            previous = max(previous, current)
        return str(total)
    try:
        return format(float(words.get(value, value)), "g")
    except ValueError:
        return value


def author_names(metadata):
    authors = metadata.get("authors")
    if isinstance(authors, list) and authors:
        names = [
            str(a.get("name") or "")
            for a in authors
            if isinstance(a, dict)
            and str(a.get("role") or "author").lower() in ("author", "primary author", "writer")
        ]
    else:
        text = re.sub(CREDENTIAL, "", str(metadata.get("author") or ""), flags=re.I)
        names = re.split(r"\s+and\s+|\s*[&;,]\s*", text, flags=re.I)
        if (
            text.count(",") == 1
            and not re.search(r"\s+and\s+|[&;]", text, re.I)
            and len(names) == 2
            and all(len(normal(n).split()) == 1 for n in names)
        ):
            names = [names[1] + " " + names[0]]
    result = set()
    for name in names:
        if re.search(ROLE, name, re.I):
            continue
        name = re.sub(CREDENTIAL, "", name, flags=re.I)
        if name.count(",") == 1:
            surname, given = name.split(",")
            name = given + " " + surname
        words = normal(name).split()
        if words:
            given = []
            initials = ""
            for word in words[:-1]:
                if len(word) == 1:
                    initials += word
                else:
                    if initials:
                        given.append(initials)
                        initials = ""
                    given.append(word)
            if initials:
                given.append(initials)
            result.add((words[-1], tuple(given)))
    return frozenset(result)


def effective(metadata):
    product = metadata.get("product") or {}
    result = {**product, **{k: v for k, v in metadata.items() if v is not None}}
    for key in ("title", "author", "isbn", "asin"):
        result[key] = metadata.get(key) or product.get(key)
    return result


def identifiers(metadata):
    result = {}
    for key in ("isbn", "asin"):
        value = re.sub(r"[^A-Z0-9]", "", str(metadata.get(key) or "").upper())
        if value:
            if key == "asin" and not re.fullmatch(r"[A-Z0-9]{10}", value):
                continue
            if key == "isbn":
                valid10 = (
                    bool(re.fullmatch(r"\d{9}[\dX]", value))
                    and sum((10 if n == "X" else int(n)) * (10 - i) for i, n in enumerate(value))
                    % 11
                    == 0
                )
                valid13 = (
                    bool(re.fullmatch(r"97[89]\d{10}", value))
                    and sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(value)) % 10
                    == 0
                )
                if not valid10 and not valid13:
                    continue
            if key == "isbn" and len(value) == 10 and value[:9].isdigit():
                base = "978" + value[:9]
                value = base + str(
                    (-sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(base))) % 10
                )
            result[key] = value
    return result


def features(metadata):
    return _features(json.dumps(effective(metadata), sort_keys=True, ensure_ascii=False))


def book_markers(value):
    parts = [m.span() for m in re.finditer(PART, value, re.I)]
    for match in re.finditer(BOOK, value, re.I):
        if not any(start <= match.start() and match.end() <= end for start, end in parts):
            yield match


@lru_cache(maxsize=8192)
def _features(encoded):
    metadata = json.loads(encoded)
    text = str(metadata.get("title") or "")
    subtitle = str(metadata.get("subtitle") or "")
    all_text = text + " " + subtitle
    books = set()
    number_evidence = []
    for field, value in (("title", text), ("subtitle", subtitle)):
        for match in book_markers(value):
            books.add(numeric(match.group(1)))
            number_evidence.append(
                {
                    "value": numeric(match.group(1)),
                    "kind": "volume"
                    if match.group().lower().startswith("vol")
                    else "series_installment",
                    "field": field,
                    "text": value,
                }
            )
    parts = {
        tuple(numeric(n) if n else "" for n in (a or c, b or d))
        for a, b, c, d in re.findall(PART, all_text, re.I)
    }
    series = metadata.get("series") or []
    series_names = set()
    if isinstance(series, list):
        for index, entry in enumerate(series):
            if not isinstance(entry, dict):
                continue
            if entry.get("name"):
                series_names.add(title(entry["name"]).removesuffix(" series"))
            if re.fullmatch(r"\d+(?:\.\d+)?", str(entry.get("sequence", ""))):
                books.add(numeric(entry["sequence"]))
                number_evidence.append(
                    {
                        "value": numeric(entry["sequence"]),
                        "kind": "series_installment",
                        "field": f"series[{index}].sequence",
                        "text": str(entry.get("name") or "") + " " + str(entry["sequence"]),
                    }
                )
    editions = {normal(m.group()) for m in re.finditer(EDITION, all_text, re.I)}
    adaptation = bool(re.search(r"dramatiz|full.cast|graphic.?audio|\babridged\b", all_text, re.I))
    collection = bool(
        re.search(r"\bbox\s*set\b|\bomnibus\b|\bcollection\b|\d+\s*[-–]\s*\d+", all_text, re.I)
    )
    subtitles = []

    def clean(value):
        value = re.sub(EDITION, "", value, flags=re.I)
        value = re.sub(PART, "", value, flags=re.I)
        value = re.sub(
            r"\b(?:dramatized adaptation|full.cast|graphic.?audio)\b", "", value, flags=re.I
        )
        return value

    def bracket(match):
        inner = match.group(1)
        if list(book_markers(inner)):
            name = title(re.sub(BOOK, "", inner, flags=re.I)).removesuffix(" series")
            if name:
                series_names.add(name)
        elif not re.search(
            PART + "|" + EDITION + r"|dramatiz|full.cast|unabridged|audiobook|surround", inner, re.I
        ):
            subtitles.append(inner)
        return " "

    primary, separator, embedded = text.partition(":")
    if separator:
        subtitles.append(embedded)
    primary = re.sub(r"[\[(]([^\])]+)[\])]", bracket, primary)
    primary = re.sub(BOOK, "", clean(primary), flags=re.I)
    trailing = re.search(r"\s+(\d+(?:\.\d+)?)\s*$", primary)
    if trailing:
        books.add(numeric(trailing.group(1)))
        number_evidence.append(
            {
                "value": numeric(trailing.group(1)),
                "kind": "series_installment" if series_names else "title_number",
                "field": "title",
                "text": text,
            }
        )
        primary = primary[: trailing.start()]
    embedded_subtitle = bool(subtitles)
    if subtitle:
        subtitles.append(subtitle)
    meaningful = set()
    for value in subtitles:
        if list(book_markers(value)):
            series_label = title(re.sub(BOOK, "", value, flags=re.I)).removesuffix(" series")
            if series_label in series_names:
                continue
        value = title(clean(value))
        if value:
            meaningful.add(value)
    return {
        "core": title(primary),
        "subtitles": frozenset(meaningful),
        "embedded_subtitle": embedded_subtitle and bool(meaningful),
        "authors": author_names(metadata),
        "volumes": frozenset(books),
        "number_evidence": number_evidence,
        "parts": frozenset(parts),
        "editions": frozenset(editions),
        "adaptation": adaptation,
        "collection": collection,
        "series": frozenset(series_names),
        "identifiers": identifiers(metadata),
    }


def describe_numbers(f, side):
    if not f["number_evidence"]:
        return f"{side} supplies no book or series number."
    labels = {
        "series_installment": "installment",
        "volume": "volume",
        "title_number": "title number",
    }
    entries = [
        f'{labels[e["kind"]]} {e["value"]} from {e["field"]} "{e["text"]}"'
        for e in f["number_evidence"]
    ]
    return side + " supplies " + "; ".join(entries) + "."


def compare(source, target):
    a, b = features(source), features(target)
    shared_ids = a["identifiers"].keys() & b["identifiers"].keys()
    exact_id = any(a["identifiers"][k] == b["identifiers"][k] for k in shared_ids)
    core_equal = bool(a["core"]) and a["core"] == b["core"]
    authors_equal = bool(a["authors"]) and a["authors"] == b["authors"]
    tokens_a, tokens_b = set(a["core"].split()), set(b["core"].split())
    fuzzy = (
        bool(tokens_a and tokens_b) and len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= 0.6
    )
    relevant = core_equal or exact_id or (authors_equal and fuzzy)
    reasons = []
    if not core_equal:
        reasons.append("Primary titles differ.")
    if not authors_equal:
        reasons.append("Primary authors are missing or differ.")
    # Readwise resolves edition identifiers to the canonical ebook for a work.
    # Audiobook and print identifiers can differ without identifying different works.
    identifier_evidence = {
        k: "exact" if a["identifiers"][k] == b["identifiers"][k] else "different_neutral"
        for k in sorted(shared_ids)
    }
    if a["subtitles"] and b["subtitles"] and a["subtitles"] != b["subtitles"]:
        reasons.append("Meaningful subtitles differ.")
    elif (
        not exact_id
        and bool(a["subtitles"]) != bool(b["subtitles"])
        and (a["embedded_subtitle"] or b["embedded_subtitle"])
    ):
        reasons.append("One title includes a subtitle that the other record does not supply.")
    volume_conflict = bool(a["volumes"] and b["volumes"]) and a["volumes"] != b["volumes"]
    if volume_conflict:
        reasons.append("Book or series numbers conflict.")
    elif a["volumes"] != b["volumes"]:
        reasons.append("A book or series number is supplied on only one side.")
    if a["volumes"] != b["volumes"]:
        reasons.extend((describe_numbers(a, "ABS"), describe_numbers(b, "Readwise")))
    if len(a["volumes"]) > 1 or len(b["volumes"]) > 1:
        reasons.append("Conflicting book or series numbers need review.")
    if any(not re.fullmatch(r"\d+(?:\.\d+)?", v) for v in a["volumes"] | b["volumes"]):
        reasons.append("A book or series number could not be validated.")
    if a["series"] and b["series"] and a["series"] != b["series"]:
        reasons.append("Series names differ.")
    if a["parts"] != b["parts"]:
        reasons.append("Split audiobook parts need an explicit progress policy.")
    if a["editions"] != b["editions"]:
        reasons.append("Edition information differs or is missing.")
    if a["adaptation"] != b["adaptation"]:
        reasons.append("Adaptation information differs or is missing.")
    if a["collection"] or b["collection"]:
        reasons.append("Collections and box sets require manual review.")
    blocking = [
        r
        for r in reasons
        if r not in ("Primary titles differ.", "Primary authors are missing or differ.")
    ]
    eligible = relevant and not blocking and ((core_equal and authors_equal) or exact_id)
    return {
        "eligible": eligible,
        "relevant": relevant,
        "plausible": relevant and not volume_conflict,
        "reasons": reasons if not eligible else [],
        "identifier_evidence": identifier_evidence,
        "evidence": "Exact identifier with no conflicting metadata."
        if exact_id
        else "Primary title and author agree after normalization.",
    }


def same_work(source, target):
    return compare(source, target)["eligible"]


def requires_progress_policy(source, target):
    a, b = features(source), features(target)
    return a["parts"] != b["parts"] or a["collection"] or b["collection"]


def catalog_candidates(source, books):
    candidates = []
    for book in books:
        assessment = compare(source, book)
        if not assessment["relevant"]:
            continue
        f = features(book)
        candidates.append(
            {
                "id": str(book["id"]),
                "title": effective(book).get("title"),
                "author": effective(book).get("author"),
                "volumes": sorted(f["volumes"]),
                # Keep volumes for existing consumers, but explain the actual marker kinds.
                "source_number_evidence": features(source)["number_evidence"],
                "number_evidence": f["number_evidence"],
                "identifier_evidence": assessment["identifier_evidence"],
                "parts": sorted(f["parts"]),
                "subtitles": sorted(f["subtitles"]),
                "identifiers": f["identifiers"],
                "plausible": assessment["plausible"],
                "status": "deleted"
                if book.get("status") == "deleted"
                else (
                    "ready"
                    if assessment["eligible"]
                    else "review"
                    if assessment["plausible"]
                    else "rejected"
                ),
                "reasons": assessment["reasons"] or [assessment["evidence"]],
            }
        )
    return sorted(
        candidates,
        key=lambda c: (
            {"ready": 0, "review": 1, "rejected": 2, "deleted": 3}[c["status"]],
            c["id"],
        ),
    )


def primary_text(metadata):
    return str(metadata.get("title") or "")


def primary_title(metadata):
    return features(metadata)["core"]


def number_marks(metadata):
    f = features(metadata)
    result = {}
    if len(f["volumes"]) == 1:
        result["book"] = next(iter(f["volumes"]))
    if len(f["parts"]) == 1:
        result["part"] = next(iter(f["parts"]))[0]
    return result


def item_metadata(item):
    media = item.get("media") or {}
    metadata = media.get("metadata") or {}
    authors = metadata.get("authors") or []
    return {
        "title": metadata.get("title") or "Untitled",
        "subtitle": metadata.get("subtitle"),
        "series": metadata.get("series") or [],
        "authors": authors,
        "author": metadata.get("authorName")
        or metadata.get("author")
        or ", ".join(a["name"] for a in authors if isinstance(a, dict) and a.get("name")),
        "isbn": metadata.get("isbn"),
        "asin": metadata.get("asin"),
        "duration": media.get("duration"),
    }
