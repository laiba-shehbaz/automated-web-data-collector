#!/usr/bin/env python3
"""
📚 Open Library Book Collector
==============================
Collects structured book records from the free, public Open Library
Search API (https://openlibrary.org/developers/api) and saves them to
JSON or CSV for later analysis.

What it does
    * Uses a public API (no scraping, no API key)
    * Collects 10 fields per record, converted to clean structured data
    * Handles timeouts, network errors, HTTP errors, invalid JSON, missing fields
    * Polite access: identifying User-Agent, >= 1 second between requests,
      hard cap on records and pages, no bulk downloading
    * Never saves the same book twice (also across repeated runs)
    * Search term / category chosen with --query / --subject or a config file

Examples
    python collector.py --subject "data science" --limit 30
    python collector.py --query "web scraping" --output data/scraping.csv
    python collector.py --config config.example.json

Exit codes:  0 = success   1 = failed, nothing collected
             2 = bad settings   3 = partly collected (an error stopped the run)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://openlibrary.org/search.json"
SITE_URL = "https://openlibrary.org"
API_FIELDS = "key,title,author_name,first_publish_year,edition_count,language,subject,ratings_average"
PLACEHOLDER_AGENT = "BookCollectorStudentProject/1.0 (contact: your-email@example.com)"

MAX_RECORDS = 200      # polite cap: this tool is not for bulk downloads
MAX_PAGE_SIZE = 50     # records requested per API call
MAX_PAGES = 10         # hard stop on API calls per run
MIN_DELAY = 1.0        # seconds between requests (Open Library: 1 request/second)
MAX_BACKOFF = 30       # longest single wait after an error, in seconds

CSV_COLUMNS = ["work_key", "title", "authors", "first_publish_year", "edition_count",
               "languages", "subjects", "rating_average", "url", "collected_at"]

log = logging.getLogger("collector")


def _sleep(seconds: float) -> None:
    """All waiting goes through here so tests and demos can skip it."""
    time.sleep(seconds)


class ConfigError(ValueError):
    """Invalid settings."""


class FetchError(RuntimeError):
    """A page could not be downloaded after all retries."""


# ──────────────────────────── settings ───────────────────────────
@dataclass
class Settings:
    query: str = ""
    subject: str = ""
    limit: int = 30
    output: str = "data/books.json"
    delay: float = MIN_DELAY
    timeout: float = 10.0
    retries: int = 3
    user_agent: str = PLACEHOLDER_AGENT
    notes: list = field(default_factory=list, repr=False)


def load_config(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except (OSError, ValueError) as err:
        raise ConfigError(f"Config file could not be read as JSON: {err}")
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a JSON object")
    return data


def build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    valid = {f.name for f in fields(Settings)} - {"notes"}
    if args.config:
        for key, value in load_config(args.config).items():
            if key in valid:
                setattr(settings, key, value)
            else:
                settings.notes.append(f"Ignored unknown config key '{key}'")
    for key in valid:
        value = getattr(args, key, None)
        if value is not None:
            setattr(settings, key, value)
    return validate(settings)


def validate(s: Settings) -> Settings:
    try:
        s.limit = int(s.limit)
        s.retries = int(s.retries)
        s.delay = float(s.delay)
        s.timeout = float(s.timeout)
    except (TypeError, ValueError):
        raise ConfigError("limit, retries, delay and timeout must be numbers")
    s.query, s.subject = str(s.query or "").strip(), str(s.subject or "").strip()
    if not s.query and not s.subject:
        s.query = "python programming"
        s.notes.append("No search given - using the default query 'python programming'")
    if not 1 <= s.limit <= MAX_RECORDS:
        raise ConfigError(f"limit must be between 1 and {MAX_RECORDS} (this tool is not for bulk downloads)")
    if not 0 <= s.retries <= 5:
        raise ConfigError("retries must be between 0 and 5")
    if s.timeout <= 0:
        raise ConfigError("timeout must be greater than 0")
    if s.delay < MIN_DELAY:
        s.notes.append(f"delay raised from {s.delay} to {MIN_DELAY} second(s) to respect the API rate limit")
        s.delay = MIN_DELAY
    if Path(s.output).suffix.lower() not in (".json", ".csv"):
        raise ConfigError("output file must end with .json or .csv")
    if "your-email@example.com" in s.user_agent:
        s.notes.append("Tip: set your own contact with --user-agent 'AppName/1.0 (you@email.com)'. "
                       "Open Library asks apps to identify themselves.")
    return s


def build_query(s: Settings) -> str:
    parts = [s.query] if s.query else []
    if s.subject:
        parts.append(f'subject:"{s.subject}"')
    return " ".join(parts)


# ──────────────────────────── downloading ────────────────────────
@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    pages: int = 0
    fetched: int = 0
    invalid: int = 0
    dup_existing: int = 0
    dup_run: int = 0
    new: int = 0
    errors: list = field(default_factory=list)


def _retry_after(response) -> float | None:
    value = getattr(response, "headers", {}).get("Retry-After")
    try:
        return min(float(value), MAX_BACKOFF) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_page(session, params: dict, s: Settings, stats: Stats) -> dict:
    """Download one page. Retries timeouts, connection errors, HTTP 429/5xx and bad JSON."""
    headers = {"User-Agent": s.user_agent, "Accept": "application/json"}
    reason = "unknown error"
    for attempt in range(s.retries + 1):
        wait = None
        stats.requests += 1
        try:
            response = session.get(API_URL, params=params, headers=headers, timeout=s.timeout)
        except requests.Timeout:
            reason = f"request timed out after {s.timeout:g}s"
        except requests.ConnectionError:
            reason = "network connection failed"
        except requests.RequestException as err:
            raise FetchError(f"request failed: {err}")
        else:
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    reason = "server returned invalid JSON"
                else:
                    if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
                        raise FetchError("response has an unexpected structure (no 'docs' list)")
                    return data
            elif status == 429 or 500 <= status < 600:
                reason = f"server answered HTTP {status}"
                wait = _retry_after(response)
            else:
                raise FetchError(f"request rejected with HTTP {status}")
        if attempt < s.retries:
            stats.retries += 1
            pause = wait if wait is not None else min(2 ** attempt, MAX_BACKOFF)
            log.warning("%s - retry %d/%d in %gs", reason, attempt + 1, s.retries, pause)
            _sleep(pause)
    raise FetchError(f"gave up after {s.retries + 1} attempts ({reason})")


# ──────────────────────────── parsing ────────────────────────────
def clean_text(value) -> str:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return " ".join(str(value).split())
    return ""


def as_list(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    seen, result = set(), []
    for item in items:
        text = clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def to_int(value, low: int, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if low <= number <= high else None


def parse_book(doc, collected_at: str) -> dict | None:
    """Turn one raw API document into a clean record, or None if it is unusable."""
    if not isinstance(doc, dict):
        return None
    key, title = clean_text(doc.get("key")), clean_text(doc.get("title"))
    if not key or not title:
        return None                       # without an ID and a title the record is useless
    try:
        rating = round(float(doc["ratings_average"]), 2)
        rating = rating if 0 <= rating <= 5 else None
    except (KeyError, TypeError, ValueError):
        rating = None
    path = key if key.startswith("/") else "/" + key
    return {
        "work_key": key,
        "title": title[:200],
        "authors": "; ".join(as_list(doc.get("author_name"))[:5]),
        "first_publish_year": to_int(doc.get("first_publish_year"), 1000, datetime.now().year + 1),
        "edition_count": to_int(doc.get("edition_count"), 0, 100000),
        "languages": "|".join(as_list(doc.get("language"))[:8]),
        "subjects": "|".join(t[:60] for t in as_list(doc.get("subject"))[:5]),
        "rating_average": rating,
        "url": SITE_URL + path,
        "collected_at": collected_at,
    }


# ──────────────────────────── collecting ─────────────────────────
def collect(s: Settings, session, known_keys: set[str]) -> tuple[list[dict], Stats]:
    """Download pages until `limit` NEW records are found or the API has no more."""
    stats, records = Stats(), []
    run_keys: set[str] = set()
    query = build_query(s)
    page_size = min(s.limit, MAX_PAGE_SIZE)
    offset = 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    while len(records) < s.limit and stats.pages < MAX_PAGES:
        if stats.pages:
            _sleep(s.delay)                       # be polite between requests
        params = {"q": query, "limit": page_size, "offset": offset, "fields": API_FIELDS}
        try:
            data = fetch_page(session, params, s, stats)
        except FetchError as err:
            stats.errors.append(str(err))
            log.error("%s", err)
            break
        stats.pages += 1
        docs = data["docs"]
        if not docs:
            log.info("No more results from the API.")
            break
        offset += len(docs)

        new = dups = bad = 0
        for doc in docs:
            stats.fetched += 1
            book = parse_book(doc, stamp)
            if book is None:
                stats.invalid += 1
                bad += 1
            elif book["work_key"] in known_keys:
                stats.dup_existing += 1
                dups += 1
            elif book["work_key"] in run_keys:
                stats.dup_run += 1
                dups += 1
            else:
                run_keys.add(book["work_key"])
                records.append(book)
                stats.new += 1
                new += 1
                if len(records) >= s.limit:
                    break
        log.info("Page %d: %d results -> %d new, %d duplicate, %d invalid", stats.pages, len(docs), new, dups, bad)

        total = data.get("numFound")
        if isinstance(total, int) and offset >= total:
            log.info("Reached the last result for this search.")
            break
    else:
        if len(records) < s.limit:
            log.info("Stopped at the safety limit of %d pages.", MAX_PAGES)
    return records, stats


# ──────────────────────────── storage ────────────────────────────
def load_existing(path: Path) -> list[dict]:
    """Read earlier results. A damaged file is moved aside instead of crashing the run."""
    if not path.exists():
        return []
    try:
        if path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "work_key" not in reader.fieldnames:
                    raise ValueError("CSV header is missing 'work_key'")
                rows = [dict(row) for row in reader]
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("records") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError("JSON does not contain a list of records")
        rows = [r for r in rows if isinstance(r, dict) and r.get("work_key")]
        return rows
    except (OSError, ValueError, UnicodeDecodeError, csv.Error) as err:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            path.rename(backup)
            where = f"moved to {backup.name}"
        except OSError:
            where = "could not be backed up"
        log.warning("Existing output file is damaged (%s); original %s. Starting a new file.", err, where)
        return []


def save_records(path: Path, records: list[dict], query: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if path.suffix.lower() == ".csv":
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
    else:
        payload = {"source": "Open Library Search API", "last_query": query,
                   "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(records), "records": records}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)          # atomic: never leaves a half-written file


# ──────────────────────────── display ────────────────────────────
def table(headers, rows, aligns=None) -> str:
    aligns = aligns or ["<"] * len(headers)
    rows = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = lambda cells: "| " + " | ".join(f"{c:{a}{w}}" for c, a, w in zip(cells, aligns, widths)) + " |"
    return "\n".join([line, fmt(headers), line] + [fmt(r) for r in rows] + [line])


def short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def print_summary(s: Settings, stats: Stats, records: list[dict], total: int) -> None:
    print("\nSUMMARY")
    print(table(["Measure", "Value"], [
        ["API requests sent", stats.requests], ["Retries after errors", stats.retries],
        ["Results received", stats.fetched], ["New records saved", stats.new],
        ["Duplicates skipped", stats.dup_existing + stats.dup_run],
        ["Invalid records skipped", stats.invalid], ["Total records in file", total],
    ], ["<", ">"]))
    if records:
        print("\nFIELD COMPLETENESS (new records)")
        rows = []
        for name in CSV_COLUMNS[1:]:
            filled = sum(1 for r in records if r.get(name) not in (None, ""))
            rows.append([name, f"{filled}/{len(records)}", f"{filled / len(records) * 100:.0f}%"])
        print(table(["Field", "Filled", "Share"], rows, ["<", ">", ">"]))
        print("\nPREVIEW")
        print(table(["#", "Title", "Authors", "Year", "Editions"],
                    [[i, short(r["title"], 38), short(r["authors"] or "-", 22),
                      r["first_publish_year"] or "-", r["edition_count"] if r["edition_count"] is not None else "-"]
                     for i, r in enumerate(records[:8], 1)], [">", "<", "<", ">", ">"]))


# ──────────────────────────── main ───────────────────────────────
def run(s: Settings, session=None) -> int:
    """Collect, merge with earlier results, save, print a summary. Returns an exit code."""
    out = Path(s.output)
    print(f"\n📚 Open Library Book Collector\n   Search : {build_query(s)}\n   Target : up to {s.limit} new records"
          f"\n   Output : {out}")
    for note in s.notes:
        log.info(note)
    existing = load_existing(out)
    known = {str(r["work_key"]) for r in existing}
    if existing:
        log.info("Loaded %d earlier record(s) from %s", len(existing), out.name)

    own_session = session is None
    session = session or requests.Session()
    try:
        records, stats = collect(s, session, known)
    finally:
        if own_session:
            session.close()

    total = len(existing) + len(records)
    if records:
        try:
            save_records(out, existing + records, build_query(s))
        except OSError as err:
            log.error("Could not save %s: %s", out, err)
            return 1
        print(f"\n✓ Saved {len(records)} new record(s) to {out}")
    elif not stats.errors:
        print("\n✓ Nothing new to save - every result was already in the file.")
    print_summary(s, stats, records, total)
    if stats.errors:
        print(f"\n! The run stopped early: {stats.errors[-1]}")
        return 3 if records else 1
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect book data from the Open Library API.")
    p.add_argument("-q", "--query", help="free-text search, e.g. 'web scraping'")
    p.add_argument("-s", "--subject", help="category / subject, e.g. 'data science'")
    p.add_argument("-n", "--limit", type=int, help=f"new records to collect (1-{MAX_RECORDS}, default 30)")
    p.add_argument("-o", "--output", help="output file, .json or .csv (default data/books.json)")
    p.add_argument("--delay", type=float, help=f"seconds between requests (minimum {MIN_DELAY:g})")
    p.add_argument("--timeout", type=float, help="seconds to wait for each response (default 10)")
    p.add_argument("--retries", type=int, help="retries after a temporary error (0-5, default 3)")
    p.add_argument("--user-agent", dest="user_agent", help="identify your app: 'Name/1.0 (you@email.com)'")
    p.add_argument("--config", help="JSON file with the same settings; command-line options override it")
    return p.parse_args(argv)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="  [%(levelname)s] %(message)s")
    try:
        settings = build_settings(parse_args(argv))
    except ConfigError as err:
        print(f"Settings error: {err}")
        return 2
    try:
        return run(settings)
    except KeyboardInterrupt:
        print("\nStopped by user. Records saved so far are kept.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
