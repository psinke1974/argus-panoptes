import asyncio
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import config, main
from app.refs import Ref, parse_refs
from app.release_notes import build_release_notes


def test_parse_refs():
    assert parse_refs("Fixes OP#12 en OP#7") == [Ref(12, True), Ref(7, False)]
    assert parse_refs("geen refs") == []
    assert parse_refs("OP#5 ... closes OP#5") == [Ref(5, True)]


class FakeOP:
    def __init__(self):
        self.calls = []

    async def comment(self, i, t):
        self.calls.append(("comment", i))

    async def set_status(self, i, s):
        self.calls.append(("status", i, s))
        return True

    async def get_wp(self, i):
        return {"subject": f"Taak {i}", "_links": {"type": {"title": "Bug" if i % 2 else "Feature"}}}


def signed(payload):
    body = json.dumps(payload).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    return body, {"X-Gitea-Event": "push", "X-Gitea-Signature": sig, "Content-Type": "application/json"}


def test_push_closes_on_default_branch(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "s3cret")
    fake = FakeOP()
    monkeypatch.setattr(main, "op", fake)
    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "a/b", "default_branch": "main"},
        "commits": [{"message": "Fixes OP#3\n", "url": "http://x", "author": {"name": "P"}}],
    }
    body, headers = signed(payload)
    c = TestClient(main.app)
    assert c.post("/hooks/gitea", content=body, headers=headers).json() == {"handled": 1}
    assert ("status", 3, config.STATUS_CLOSED) in fake.calls
    assert c.post("/hooks/gitea", content=body, headers={**headers, "X-Gitea-Signature": "x"}).status_code == 401


def test_release_notes():
    md = asyncio.run(build_release_notes(FakeOP(), ["fix OP#1", "OP#2 feat"], "v1 → v2"))
    assert "## Bug" in md and "## Feature" in md and "Taak 1" in md


def test_changed_docs_and_urls():
    from app.docs import changed_docs, doc_url, is_doc

    c = {"added": ["docs/a b.md", "src/x.py"], "modified": ["README.md", "notes/z.md"]}
    assert changed_docs(c) == ["README.md", "docs/a b.md"]
    assert is_doc("docs/x/y.md") and not is_doc("src/x.md")
    assert doc_url("a/b", "docs/a b.md").endswith("/docs/a/b/docs/a%20b.md")


def test_rewrite_links():
    from app.docs import rewrite_links

    md = "[a](architectuur/database.md#x) ![i](../_bijlagen/p.png) [e](https://e.nl) [h](#top)"
    out = rewrite_links(md, "o/r", "docs/home/x.md")
    assert "(/docs/o/r/docs/home/architectuur/database.md#x)" in out
    assert "(/docs/o/r/_raw/docs/_bijlagen/p.png)" in out
    assert "(https://e.nl)" in out and "(#top)" in out
