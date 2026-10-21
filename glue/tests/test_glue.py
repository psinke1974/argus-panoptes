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
    assert parse_refs("no refs") == []
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
        return {"subject": f"Task {i}", "_links": {"type": {"title": "Bug" if i % 2 else "Feature"}}}


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
    assert "## Bug" in md and "## Feature" in md and "Task 1" in md


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


def test_legacy_keys(tmp_path, monkeypatch):
    from app import legacy

    (tmp_path / "jira-demo.json").write_text('{"DEMO-2": 43, "DEMO-1": 41, "__relations__": []}')
    monkeypatch.setattr(config, "LEGACY_KEYS_DIR", str(tmp_path))
    assert legacy.load() == {"DEMO-2": 43, "DEMO-1": 41}
    keys = legacy.load()
    assert parse_refs("fixes DEMO-2, see also demo-1 and UTF-8 and OTHER-9", keys) == [Ref(43, True), Ref(41, False)]
    assert parse_refs("DEMO-2 and OP#43", keys) == [Ref(43, False)]
    assert parse_refs("DEMO-2", None) == []


def test_stale_markdown():
    from app.stale import stale_markdown

    items = [{"id": 5, "subject": "Old thing", "status": "New", "type": "Bug", "assignee": "unassigned", "updated": "2026-01-02"},
             {"id": 6, "subject": "Mine", "status": "In progress", "type": "Task", "assignee": "Jan", "updated": "2026-02-03"}]
    md = stale_markdown(items, "demo", 30)
    assert md.index("## Jan") < md.index("## unassigned")  # people first, unassigned last
    assert "OP#5" in md and "more than 30 days: 2" in md
    assert "Nothing is stale" in stale_markdown([], "demo", 30)


def test_safe_next():
    from app.auth import safe_next

    assert safe_next("/docs/a/b?x=1") == "/docs/a/b?x=1"
    assert safe_next("//evil.example") == "/" and safe_next("https://evil.example") == "/" and safe_next(None) == "/"
    assert safe_next("/\\evil") == "/"


def test_can_read_repo_uses_sudo():
    import httpx

    from app.auth import can_read_repo

    seen = {}

    def handler(req: httpx.Request):
        seen["sudo"] = req.headers.get("Sudo")
        return httpx.Response(200 if req.headers.get("Sudo") == "jan" else 404)

    async def run():
        async with httpx.AsyncClient(base_url="http://gitea", transport=httpx.MockTransport(handler)) as g:
            return await can_read_repo(g, "jan", "o/r"), await can_read_repo(g, "stranger", "o/r")

    assert asyncio.run(run()) == (True, False)
    assert seen["sudo"] == "stranger"


def test_sso_disabled_is_transparent(monkeypatch):
    from app import auth

    monkeypatch.setattr(config, "OIDC_ISSUER", "")
    assert asyncio.run(auth.require_login(object())) is None  # no request/session needed when SSO is off


def test_admin_role_required(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app import auth

    monkeypatch.setattr(config, "OIDC_ISSUER", "https://sso.example/realms/x")

    class Req:
        def __init__(self, user):
            self.session = {"user": user} if user else {}

    asyncio.run(auth.require_admin(Req({"username": "a", "roles": ["admin"]})))
    with pytest.raises(HTTPException):
        asyncio.run(auth.require_admin(Req({"username": "b", "roles": []})))
    with pytest.raises(auth.NeedLogin):
        asyncio.run(auth.require_admin(Req(None)))


def test_login_redirect_keeps_query(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import auth, main

    monkeypatch.setattr(config, "OIDC_ISSUER", "https://sso.example/realms/x")
    app = FastAPI()
    app.add_exception_handler(auth.NeedLogin, main.need_login)

    @app.get("/page")
    async def page(request: __import__("fastapi").Request):
        await auth.require_login(request)

    # no SessionMiddleware needed: require_login would touch request.session, so give it an empty one
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key="x")
    r = TestClient(app, follow_redirects=False).get("/page?q=a&b=2")
    assert r.status_code == 307 and r.headers["location"] == "/auth/login?next=%2Fpage%3Fq%3Da%26b%3D2"
    r = TestClient(app, follow_redirects=False).get("/page?format=json")
    assert r.status_code == 401


# ---- weekly digest ----
def test_digest_due_once_per_day():
    import datetime as dt

    from app.digest import due

    monday_9 = dt.datetime(2026, 10, 19, 9, 0)  # a Monday
    assert due(monday_9, None, 0, 8)
    assert not due(monday_9, monday_9.date(), 0, 8)  # already sent today
    assert not due(dt.datetime(2026, 10, 19, 7, 59), None, 0, 8)  # too early
    assert not due(dt.datetime(2026, 10, 20, 9, 0), None, 0, 8)  # Tuesday
    assert due(dt.datetime(2026, 10, 26, 8, 0), dt.date(2026, 10, 19), 0, 8)  # next Monday


def test_to_slack():
    from app.digest import to_slack

    md = "# Stale in demo\nOpen: **2**\n\n## Jan\n- [OP#5](http://op/5) Old thing (Bug)"
    out = to_slack(md)
    assert "*Stale in demo*" in out and "*2*" in out and "• <http://op/5|OP#5> Old thing (Bug)" in out and "##" not in out


def test_build_and_send_webhook(monkeypatch):
    import httpx

    from app import config, digest

    class FakeOP:
        async def stale(self, project, days):
            if project == "broken":
                raise RuntimeError("boom")
            return [{"id": 1, "subject": "S", "status": "New", "type": "Bug", "assignee": "unassigned", "updated": "2026-01-01"}]

    monkeypatch.setattr(config, "DIGEST_PROJECTS", ["a", "broken"])
    monkeypatch.setattr(config, "DIGEST_WEBHOOK_URL", "http://chat/hook")
    monkeypatch.setattr(config, "DIGEST_SMTP_HOST", "")
    text, total = asyncio.run(digest.build_digest(FakeOP()))
    assert total == 1 and "Could not read this project: boom" in text

    got = {}

    def handler(req: httpx.Request):
        got["body"] = json.loads(req.content)
        return httpx.Response(200)

    async def send():
        await digest.send_webhook(text, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    asyncio.run(send())
    assert "OP#1" in got["body"]["text"] and "<" in got["body"]["text"]  # slack-style link
    assert digest.configured()
    monkeypatch.setattr(config, "DIGEST_WEBHOOK_URL", "")
    assert not digest.configured()


def test_send_email(monkeypatch):
    from app import config, digest

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg):
            sent["to"], sent["subject"] = msg["To"], msg["Subject"]

    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    for k, v in {"DIGEST_SMTP_HOST": "smtp.example", "DIGEST_SMTP_PORT": 587, "DIGEST_SMTP_USER": "u", "DIGEST_SMTP_PASSWORD": "p",
                 "DIGEST_MAIL_FROM": "panoptes@example.com", "DIGEST_MAIL_TO": ["a@example.com", "b@example.com"], "DIGEST_SMTP_STARTTLS": True}.items():
        monkeypatch.setattr(config, k, v)
    digest.send_email("Subject", "body")
    assert sent == {"host": ("smtp.example", 587), "tls": True, "login": "u", "to": "a@example.com, b@example.com", "subject": "Subject"}
