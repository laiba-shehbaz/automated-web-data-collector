"""
Fake HTTP pieces used by the tests and by offline_demo.py.

FakeSession behaves like requests.Session().get(): it first plays back a script of
responses / exceptions (to simulate timeouts, HTTP errors...), then serves pages from
a pool of documents using the same `limit` and `offset` parameters as the real API.

IMPORTANT: the demo documents are SYNTHETIC. Every title starts with "[DEMO]".
"""
from __future__ import annotations


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    def __init__(self, script=None, docs=None):
        self.script = list(script or [])
        self.docs = docs or []
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 10))
        return FakeResponse(200, {"numFound": len(self.docs), "start": offset,
                                  "docs": self.docs[offset:offset + limit]})

    def close(self):
        pass


TOPICS = ["Python Basics", "Data Structures", "Web Scraping", "Machine Learning", "Flask Web Apps",
          "Automation Scripts", "Testing with Python", "Data Analysis", "Algorithms", "Networking"]
LANGS = [["eng"], ["eng", "urd"], ["eng", "fre"], ["eng"]]


def build_demo_docs() -> list[dict]:
    """42 synthetic API documents that include realistic problems."""
    docs = []
    for i in range(1, 43):
        doc = {
            "key": f"/works/OL{1000 + i}W",
            "title": f"[DEMO] {TOPICS[i % 10]} Vol. {i:02d}",
            "author_name": [f"Demo Author {chr(64 + i % 12 + 1)}"] + ([f"Co-Author {i}"] if i % 5 == 0 else []),
            "first_publish_year": 1995 + (i * 7) % 30,
            "edition_count": (i * 3) % 25 + 1,
            "language": LANGS[i % 4],
            "subject": [TOPICS[i % 10], "Programming", "Computer science", "Demo data"],
            "ratings_average": round(3.2 + ((i * 37) % 18) / 10, 2),
        }
        if i % 7 == 0:
            del doc["first_publish_year"]            # missing year
        if i % 4 == 0:
            del doc["ratings_average"]               # not rated yet
        docs.append(doc)
    docs[20]["first_publish_year"] = "abc"            # invalid year
    del docs[24]["author_name"]                       # missing author
    del docs[5]["title"]                              # unusable record (no title)
    docs.insert(12, dict(docs[3]))                    # exact duplicate of an earlier record
    return docs
