#!/usr/bin/env python3
"""
Offline demo - shows the collector working WITHOUT internet.

It replaces the real HTTP session with a fake API (tests/fakes.py) that serves 43
SYNTHETIC "[DEMO]" book documents, including a duplicate, a record without a title,
missing fields and an invalid year. A timeout is simulated on the first request.

The output files in sample_output/ were produced by this script, NOT by the real
Open Library. For real data run:  python collector.py --subject "data science"

Run:  python offline_demo.py
"""
import logging
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import collector  # noqa: E402
from fakes import FakeSession, build_demo_docs  # noqa: E402

OUT = Path("sample_output")          # relative, so the printed paths stay short


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def main() -> None:
    os.chdir(ROOT)
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="  [%(levelname)s] %(message)s")
    collector._sleep = lambda seconds: None          # skip real waiting in the demo
    docs = build_demo_docs()
    for path in OUT.glob("books_sample.*"):
        path.unlink()

    print("OFFLINE DEMO - simulated API responses, not the real Open Library.")
    print("Waiting times are skipped here; the real tool waits >= 1 second between requests.")

    banner("RUN 1 - first run, with one simulated timeout (watch the automatic retry)")
    settings = collector.Settings(query="python programming", limit=30, output=str(OUT / "books_sample.json"),
                                  user_agent="BookCollector/1.0 (demo@example.com)")
    collector.run(collector.validate(settings),
                  FakeSession(script=[requests.Timeout("simulated")], docs=docs))

    banner("RUN 2 - the SAME command again (duplicates must not be saved twice)")
    collector.run(collector.validate(settings), FakeSession(docs=docs))

    banner("RUN 3 - same search saved as CSV instead")
    csv_settings = collector.Settings(query="python programming", limit=15, output=str(OUT / "books_sample.csv"),
                                      user_agent="BookCollector/1.0 (demo@example.com)")
    collector.run(collector.validate(csv_settings), FakeSession(docs=docs))

    banner("RUN 4 - internet is down (graceful failure, no crash, no file created)")
    down = collector.Settings(query="python programming", limit=10, retries=2,
                              output=str(OUT / "network_down_test.json"),
                              user_agent="BookCollector/1.0 (demo@example.com)")
    code = collector.run(collector.validate(down), FakeSession(script=[requests.ConnectionError()] * 5))
    print(f"\nExit code: {code}   |   output file created: {(OUT / 'network_down_test.json').exists()}")


if __name__ == "__main__":
    main()
