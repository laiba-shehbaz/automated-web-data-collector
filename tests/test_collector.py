"""
Offline tests for collector.py - no internet needed.
Run:  python -m unittest -v        (from the project folder)
"""
import contextlib
import csv
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import collector  # noqa: E402
from fakes import FakeResponse, FakeSession, build_demo_docs  # noqa: E402


def setUpModule():
    logging.disable(logging.CRITICAL)      # keep test output clean


def ok(docs, total=None):
    return FakeResponse(200, {"numFound": len(docs) if total is None else total, "docs": docs})


def make_settings(**changes):
    values = {"query": "python", "limit": 10, "retries": 2, "output": "unused.json"}
    values.update(changes)
    return collector.validate(collector.Settings(**values))


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(collector, "_sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


# ───────────────────────────── parsing ──────────────────────────
class ParseTests(unittest.TestCase):
    STAMP = "2026-01-01T00:00:00Z"

    def test_complete_document(self):
        book = collector.parse_book({
            "key": "/works/OL1W", "title": "A Title", "author_name": ["Ann", "Bob"],
            "first_publish_year": 1999, "edition_count": 4, "language": ["eng", "fre"],
            "subject": ["Python", "Code"], "ratings_average": 4.256}, self.STAMP)
        self.assertEqual(book["authors"], "Ann; Bob")
        self.assertEqual(book["languages"], "eng|fre")
        self.assertEqual(book["rating_average"], 4.26)
        self.assertEqual(book["url"], "https://openlibrary.org/works/OL1W")
        self.assertEqual(set(book), set(collector.CSV_COLUMNS))

    def test_missing_optional_fields_become_empty(self):
        book = collector.parse_book({"key": "/works/OL2W", "title": "Only Title"}, self.STAMP)
        self.assertEqual(book["authors"], "")
        self.assertIsNone(book["first_publish_year"])
        self.assertIsNone(book["edition_count"])
        self.assertIsNone(book["rating_average"])

    def test_unusable_documents_return_none(self):
        self.assertIsNone(collector.parse_book({"key": "/works/OL3W"}, self.STAMP))          # no title
        self.assertIsNone(collector.parse_book({"title": "No key"}, self.STAMP))              # no key
        self.assertIsNone(collector.parse_book({"key": "/w/1", "title": "   "}, self.STAMP))  # blank title
        self.assertIsNone(collector.parse_book("not a dict", self.STAMP))
        self.assertIsNone(collector.parse_book(None, self.STAMP))

    def test_invalid_values_are_dropped_not_crashed(self):
        book = collector.parse_book({"key": "/w/4", "title": "T", "first_publish_year": "abc",
                                     "edition_count": -4, "ratings_average": 9}, self.STAMP)
        self.assertIsNone(book["first_publish_year"])
        self.assertIsNone(book["edition_count"])
        self.assertIsNone(book["rating_average"])
        for bad_year in (3025, True, None, [], "1999x"):
            book = collector.parse_book({"key": "/w/5", "title": "T", "first_publish_year": bad_year}, self.STAMP)
            self.assertIsNone(book["first_publish_year"], bad_year)

    def test_text_is_cleaned(self):
        book = collector.parse_book({"key": "/w/6", "title": "  Spaced \n  Title  ",
                                     "author_name": ["Ann", "Ann", " "]}, self.STAMP)
        self.assertEqual(book["title"], "Spaced Title")
        self.assertEqual(book["authors"], "Ann")


# ───────────────────────────── HTTP errors ──────────────────────
class FetchTests(BaseTest):
    def fetch(self, script, **settings):
        session, stats = FakeSession(script), collector.Stats()
        s = make_settings(**settings)
        return collector.fetch_page(session, {"q": "x"}, s, stats), stats

    def test_timeout_then_success(self):
        data, stats = self.fetch([requests.Timeout(), ok([])])
        self.assertEqual(data["docs"], [])
        self.assertEqual((stats.requests, stats.retries), (2, 1))
        self.assertEqual(self.sleeps, [1])

    def test_connection_error_then_success(self):
        _, stats = self.fetch([requests.ConnectionError(), ok([])])
        self.assertEqual(stats.retries, 1)

    def test_gives_up_after_all_retries_with_growing_wait(self):
        with self.assertRaisesRegex(collector.FetchError, "gave up after 3 attempts.*timed out"):
            self.fetch([requests.Timeout()] * 3)
        self.assertEqual(self.sleeps, [1, 2])

    def test_rate_limit_respects_retry_after(self):
        _, stats = self.fetch([FakeResponse(429, headers={"Retry-After": "5"}), ok([])])
        self.assertEqual(self.sleeps, [5.0])

    def test_server_error_is_retried(self):
        _, stats = self.fetch([FakeResponse(503), ok([])])
        self.assertEqual(stats.requests, 2)

    def test_client_error_is_not_retried(self):
        with self.assertRaisesRegex(collector.FetchError, "HTTP 404"):
            self.fetch([FakeResponse(404), ok([])])
        self.assertEqual(self.sleeps, [])

    def test_invalid_json_is_retried(self):
        _, stats = self.fetch([FakeResponse(200, bad_json=True), ok([])])
        self.assertEqual(stats.retries, 1)

    def test_unexpected_structure_is_rejected_immediately(self):
        with self.assertRaisesRegex(collector.FetchError, "unexpected structure"):
            self.fetch([FakeResponse(200, {"nothing": 1})])
        with self.assertRaisesRegex(collector.FetchError, "unexpected structure"):
            self.fetch([FakeResponse(200, ["list", "not", "dict"])])

    def test_retries_can_be_disabled(self):
        with self.assertRaises(collector.FetchError):
            self.fetch([requests.Timeout()], retries=0)
        self.assertEqual(self.sleeps, [])


# ───────────────────────────── collecting ───────────────────────
class CollectTests(BaseTest):
    def test_limit_is_respected(self):
        records, stats = collector.collect(make_settings(limit=10), FakeSession(docs=build_demo_docs()), set())
        self.assertEqual(len(records), 10)
        # page 1 holds one unusable record, so a second request is needed to reach 10 valid ones
        self.assertEqual(stats.pages, 2)
        self.assertEqual(stats.invalid, 1)

    def test_duplicates_and_invalid_records_are_skipped(self):
        records, stats = collector.collect(make_settings(limit=30), FakeSession(docs=build_demo_docs()), set())
        keys = [r["work_key"] for r in records]
        self.assertEqual(len(records), 30)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual((stats.dup_run, stats.invalid, stats.pages), (1, 1, 2))
        self.assertEqual(self.sleeps, [1.0])           # waited once between the two requests

    def test_known_keys_from_earlier_runs_are_skipped(self):
        first, _ = collector.collect(make_settings(limit=10), FakeSession(docs=build_demo_docs()), set())
        known = {r["work_key"] for r in first}
        second, stats = collector.collect(make_settings(limit=10), FakeSession(docs=build_demo_docs()), known)
        self.assertFalse(known & {r["work_key"] for r in second})
        self.assertGreaterEqual(stats.dup_existing, 10)   # all 10 earlier books were recognised

    def test_stops_when_api_has_no_results(self):
        records, stats = collector.collect(make_settings(), FakeSession(docs=[]), set())
        self.assertEqual((records, stats.errors), ([], []))

    def test_stops_after_last_result(self):
        session = FakeSession(docs=build_demo_docs()[:5])
        records, stats = collector.collect(make_settings(limit=50), session, set())
        self.assertEqual(len(records), 5)
        self.assertEqual(len(session.calls), 1)

    def test_error_in_later_page_keeps_earlier_records(self):
        docs = build_demo_docs()
        session = FakeSession(script=[ok(docs[:10], total=43)] + [requests.Timeout()] * 3, docs=docs)
        records, stats = collector.collect(make_settings(limit=30), session, set())
        self.assertGreater(len(records), 0)
        self.assertEqual(len(stats.errors), 1)

    def test_page_limit_is_a_hard_stop(self):
        endless = [{"key": f"/w/{i}", "title": f"T{i}"} for i in range(1000)]
        session = FakeSession(docs=endless)
        _, stats = collector.collect(make_settings(limit=1), session, {f"/w/{i}" for i in range(1000)})
        self.assertEqual(stats.pages, collector.MAX_PAGES)

    def test_search_term_reaches_the_api(self):
        session = FakeSession(docs=[])
        collector.collect(make_settings(query="web scraping", subject="data science"), session, set())
        self.assertEqual(session.calls[0]["q"], 'web scraping subject:"data science"')
        self.assertEqual(session.calls[0]["fields"], collector.API_FIELDS)


# ───────────────────────────── storage ──────────────────────────
class StorageTests(BaseTest):
    def records(self, n=3):
        return [collector.parse_book(d, "t") for d in build_demo_docs()[:n]]

    def test_json_round_trip(self):
        path = self.dir / "books.json"
        collector.save_records(path, self.records(), "q")
        self.assertEqual(len(collector.load_existing(path)), 3)
        self.assertEqual(json.loads(path.read_text())["count"], 3)
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_csv_round_trip(self):
        path = self.dir / "books.csv"
        collector.save_records(path, self.records(), "q")
        with path.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(next(csv.reader(handle)), collector.CSV_COLUMNS)
        self.assertEqual(len(collector.load_existing(path)), 3)

    def test_plain_json_list_is_accepted(self):
        path = self.dir / "list.json"
        path.write_text(json.dumps(self.records()), encoding="utf-8")
        self.assertEqual(len(collector.load_existing(path)), 3)

    def test_damaged_json_is_moved_aside(self):
        path = self.dir / "books.json"
        path.write_text("{broken json", encoding="utf-8")
        self.assertEqual(collector.load_existing(path), [])
        self.assertFalse(path.exists())
        self.assertEqual(len(list(self.dir.glob("books.json.corrupt-*"))), 1)

    def test_wrong_json_structure_is_moved_aside(self):
        path = self.dir / "books.json"
        path.write_text('{"records": "nope"}', encoding="utf-8")
        self.assertEqual(collector.load_existing(path), [])
        self.assertEqual(len(list(self.dir.glob("*.corrupt-*"))), 1)

    def test_csv_without_header_is_moved_aside(self):
        path = self.dir / "books.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        self.assertEqual(collector.load_existing(path), [])
        self.assertEqual(len(list(self.dir.glob("*.corrupt-*"))), 1)

    def test_missing_file_gives_empty_list(self):
        self.assertEqual(collector.load_existing(self.dir / "none.json"), [])


# ───────────────────────────── full runs ────────────────────────
class RunTests(BaseTest):
    def run_collector(self, session, **changes):
        out = self.dir / changes.pop("name", "books.json")
        s = make_settings(output=str(out), **changes)
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = collector.run(s, session)
        return code, out, buffer.getvalue()

    def test_repeated_runs_never_create_duplicates(self):
        docs = build_demo_docs()
        code1, out, _ = self.run_collector(FakeSession(docs=docs))
        code2, _, _ = self.run_collector(FakeSession(docs=docs))
        keys = [r["work_key"] for r in collector.load_existing(out)]
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(len(keys), 20)
        self.assertEqual(len(keys), len(set(keys)))

    def test_nothing_new_leaves_file_unchanged(self):
        docs = build_demo_docs()[:5]
        self.run_collector(FakeSession(docs=docs))
        code, out, text = self.run_collector(FakeSession(docs=docs))
        self.assertEqual(code, 0)
        self.assertIn("Nothing new", text)
        self.assertEqual(len(collector.load_existing(out)), 5)

    def test_network_down_exits_with_1_and_creates_no_file(self):
        code, out, text = self.run_collector(FakeSession(script=[requests.ConnectionError()] * 9))
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())
        self.assertIn("stopped early", text)

    def test_partial_run_saves_data_and_exits_with_3(self):
        docs = build_demo_docs()
        session = FakeSession(script=[ok(docs[:10], total=43)] + [requests.Timeout()] * 3, docs=docs)
        code, out, _ = self.run_collector(session, limit=30)
        self.assertEqual(code, 3)
        self.assertGreater(len(collector.load_existing(out)), 0)

    def test_csv_output(self):
        code, out, _ = self.run_collector(FakeSession(docs=build_demo_docs()), name="books.csv")
        self.assertEqual(code, 0)
        self.assertEqual(len(collector.load_existing(out)), 10)

    def test_damaged_output_file_does_not_stop_the_run(self):
        out = self.dir / "books.json"
        out.write_text("%%% not json", encoding="utf-8")
        code, _, _ = self.run_collector(FakeSession(docs=build_demo_docs()))
        self.assertEqual(code, 0)
        self.assertEqual(len(collector.load_existing(out)), 10)


# ───────────────────────────── settings ─────────────────────────
class SettingsTests(BaseTest):
    def test_limit_above_cap_is_rejected(self):
        with self.assertRaisesRegex(collector.ConfigError, "limit"):
            make_settings(limit=collector.MAX_RECORDS + 1)
        with self.assertRaises(collector.ConfigError):
            make_settings(limit=0)

    def test_fast_delay_is_raised_to_minimum(self):
        s = make_settings(delay=0.1)
        self.assertEqual(s.delay, collector.MIN_DELAY)
        self.assertTrue(any("delay raised" in n for n in s.notes))

    def test_bad_output_extension_is_rejected(self):
        with self.assertRaisesRegex(collector.ConfigError, "output"):
            make_settings(output="books.txt")

    def test_default_query_when_nothing_given(self):
        self.assertEqual(collector.validate(collector.Settings()).query, "python programming")

    def test_non_numeric_values_are_rejected(self):
        with self.assertRaises(collector.ConfigError):
            make_settings(limit="many")

    def test_config_file_with_command_line_override(self):
        cfg = self.dir / "cfg.json"
        cfg.write_text(json.dumps({"subject": "history", "limit": 5, "bogus": 1}), encoding="utf-8")
        s = collector.build_settings(collector.parse_args(["--config", str(cfg), "--limit", "7"]))
        self.assertEqual((s.subject, s.limit), ("history", 7))
        self.assertTrue(any("bogus" in n for n in s.notes))

    def test_missing_or_broken_config_file(self):
        with self.assertRaisesRegex(collector.ConfigError, "not found"):
            collector.build_settings(collector.parse_args(["--config", str(self.dir / "none.json")]))
        bad = self.dir / "bad.json"
        bad.write_text("{oops", encoding="utf-8")
        with self.assertRaisesRegex(collector.ConfigError, "JSON"):
            collector.build_settings(collector.parse_args(["--config", str(bad)]))

    def test_main_returns_2_for_bad_settings(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(["--limit", "9999"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
