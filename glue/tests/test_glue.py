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


# ---- more git hosts: Forgejo, GitHub, GitLab (issue #9) ----
def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from app import config, main

    monkeypatch.setattr(config, "WEBHOOK_SECRET", "s3cret")
    fake = FakeOP()
    monkeypatch.setattr(main, "op", fake)
    return TestClient(main.app), fake


def _sig(body: bytes) -> str:
    return hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


GH_PUSH = {"ref": "refs/heads/main", "repository": {"full_name": "acme/app", "default_branch": "main"},
           "commits": [{"message": "Fixes OP#3: handle empty input", "url": "https://github.com/acme/app/commit/abc", "author": {"name": "Jan"}, "added": [], "modified": ["docs/a.md"]}]}


def test_forgejo_headers_and_github_signature(monkeypatch):
    c, fake = _client(monkeypatch)
    body = json.dumps(GH_PUSH).encode()
    h = {"Content-Type": "application/json"}
    # Forgejo: its own headers
    r = c.post("/hooks/forgejo", content=body, headers={**h, "X-Forgejo-Event": "push", "X-Forgejo-Signature": _sig(body)})
    assert r.json() == {"handled": 1}
    # Forgejo's Gitea-compatible headers also work
    r = c.post("/hooks/forgejo", content=body, headers={**h, "X-Gitea-Event": "push", "X-Gitea-Signature": _sig(body)})
    assert r.json() == {"handled": 1}
    # GitHub: sha256=<hex> in X-Hub-Signature-256, and no Gitea doc links
    r = c.post("/hooks/github", content=body, headers={**h, "X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=" + _sig(body)})
    assert r.json() == {"handled": 1}
    assert ("status", 3, config.STATUS_CLOSED) in fake.calls
    bad = c.post("/hooks/github", content=body, headers={**h, "X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=deadbeef"})
    assert bad.status_code == 401


def test_github_pull_request_merge(monkeypatch):
    c, fake = _client(monkeypatch)
    pr = {"action": "closed", "repository": {"full_name": "acme/app"}, "pull_request": {"title": "Add thing (fixes OP#9)", "body": "", "html_url": "https://github.com/acme/app/pull/1", "merged": True}}
    body = json.dumps(pr).encode()
    r = c.post("/hooks/github", content=body, headers={"Content-Type": "application/json", "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=" + _sig(body)})
    assert r.json() == {"handled": 1} and ("status", 9, config.STATUS_CLOSED) in fake.calls


GL_PUSH = {"object_kind": "push", "ref": "refs/heads/main", "project": {"path_with_namespace": "acme/app", "default_branch": "main"},
           "commits": [{"message": "Closes OP#4\n\nlonger text", "url": "https://gitlab.com/acme/app/-/commit/abc", "author": {"name": "Jan"}, "added": [], "modified": ["docs/x.md"]},
                       {"message": "unrelated change", "url": "https://gitlab.com/acme/app/-/commit/def", "author": {"name": "Jan"}}]}


def test_gitlab_push_and_token(monkeypatch):
    c, fake = _client(monkeypatch)
    body = json.dumps(GL_PUSH).encode()
    h = {"Content-Type": "application/json", "X-Gitlab-Event": "Push Hook"}
    assert c.post("/hooks/gitlab", content=body, headers=h).status_code == 401  # no token
    assert c.post("/hooks/gitlab", content=body, headers={**h, "X-Gitlab-Token": "wrong"}).status_code == 401
    r = c.post("/hooks/gitlab", content=body, headers={**h, "X-Gitlab-Token": "s3cret"})
    assert r.json() == {"handled": 1}  # only the commit that mentions a work package
    assert ("status", 4, config.STATUS_CLOSED) in fake.calls
    assert c.post("/hooks/gitlab", content=body, headers={"Content-Type": "application/json", "X-Gitlab-Event": "Tag Push Hook", "X-Gitlab-Token": "s3cret"}).json() == {"handled": 0}


def test_gitlab_merge_request_lifecycle(monkeypatch):
    c, fake = _client(monkeypatch)
    base = {"object_kind": "merge_request", "project": {"path_with_namespace": "acme/app"}}
    h = {"Content-Type": "application/json", "X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s3cret"}
    for action, expected in (("open", 1), ("update", 0), ("merge", 1), ("close", 0)):
        body = json.dumps({**base, "object_attributes": {"action": action, "title": "Do it (fixes OP#5)", "description": "", "url": "https://gitlab.com/acme/app/-/merge_requests/2"}}).encode()
        assert c.post("/hooks/gitlab", content=body, headers=h).json() == {"handled": expected}, action
    assert ("status", 5, config.STATUS_IN_PROGRESS) in fake.calls and ("status", 5, config.STATUS_CLOSED) in fake.calls


# ---- docs portal: table of contents, breadcrumbs, diagrams (issue #8) ----
GITEA_HTML = ('<h1 id="user-content-t" dir="auto">Title</h1><h2 id="user-content-a" dir="auto">Alpha &amp; beta</h2><p>x</p>'
              '<h3 id="user-content-b" dir="auto"><a href="#b">Beta</a></h3><h2 id="user-content-c" dir="auto">Gamma</h2>'
              '<div class="code-block-container code-overflow-scroll"><pre class="code-block is-loading"><code class="chroma language-mermaid display">graph TD; A--&gt;B;\n</code></pre></div>'
              '<div class="code-block-container code-overflow-scroll"><pre class="code-block"><code class="chroma language-python display">print(1)\n</code></pre></div>')


def test_build_toc():
    from app.docs import build_toc

    toc = build_toc(GITEA_HTML)
    assert 'href="#user-content-a">Alpha &amp; beta</a>' in toc and 'class="l3"><a href="#user-content-b">Beta</a>' in toc and "Title" not in toc  # h1 is not listed
    assert build_toc('<h2 id="x">One</h2><h2 id="y">Two</h2>') == ""  # too short to need one


def test_breadcrumbs_link_folders_to_their_page():
    from app.docs import breadcrumbs

    paths = ["docs/README.md", "docs/home.md", "docs/home/architecture.md", "docs/home/architecture/database.md", "docs/misc/loose.md"]
    html_ = breadcrumbs("o/r", "docs/home/architecture/database.md", paths)
    assert '<a href="/docs/o/r">o/r</a>' in html_ and '<a href="/docs/o/r/docs/README.md">docs</a>' in html_
    assert '<a href="/docs/o/r/docs/home.md">home</a>' in html_ and '<a href="/docs/o/r/docs/home/architecture.md">architecture</a>' in html_
    assert html_.endswith("<b>database</b></div>")
    loose = breadcrumbs("o/r", "docs/misc/loose.md", paths)
    assert " › misc › " in loose and 'href="/docs/o/r/docs/misc' not in loose  # no page for the folder: plain text


def test_mermaidify_only_touches_mermaid_blocks(monkeypatch, tmp_path):
    from app import docs

    out, found = docs.mermaidify(GITEA_HTML)
    assert found and '<pre class="mermaid">graph TD; A--&gt;B;\n</pre>' in out and "language-python" in out
    assert docs.mermaidify("<p>nothing</p>") == ("<p>nothing</p>", False)
    # script source: nothing by default, our own copy when the file exists, a URL when configured
    monkeypatch.setattr(config, "MERMAID_JS_URL", "")
    monkeypatch.setattr(config, "MERMAID_JS_PATH", str(tmp_path / "missing.js"))
    assert docs.mermaid_src() == ""
    (tmp_path / "m.js").write_text("// mermaid")
    monkeypatch.setattr(config, "MERMAID_JS_PATH", str(tmp_path / "m.js"))
    assert docs.mermaid_src() == "/docs/_assets/mermaid.js"
    monkeypatch.setattr(config, "MERMAID_JS_URL", "https://cdn.example/m.js")
    assert docs.mermaid_src() == "https://cdn.example/m.js"


def test_doc_page_route_composes_breadcrumbs_toc_and_diagram(monkeypatch, tmp_path):
    import httpx
    from fastapi.testclient import TestClient

    from app import docs, main

    def gitea_handler(req: httpx.Request):
        p = req.url.path
        if p == "/api/v1/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if p.startswith("/api/v1/repos/o/r/git/trees/"):
            return httpx.Response(200, json={"tree": [{"path": x, "type": "blob"} for x in ("docs/home.md", "docs/home/page.md")]})
        if p.startswith("/api/v1/repos/o/r/raw/"):
            return httpx.Response(200, text="# Title\n")
        if p == "/api/v1/markdown":
            return httpx.Response(200, text=GITEA_HTML)
        if p == "/api/v1/repos/o/r/commits":
            return httpx.Response(200, json=[{"commit": {"message": "docs: page (OP#12)"}}])
        return httpx.Response(404)

    monkeypatch.setattr(docs, "gitea", lambda: httpx.AsyncClient(base_url="http://gitea", transport=httpx.MockTransport(gitea_handler)))
    (tmp_path / "m.js").write_text("// mermaid")
    monkeypatch.setattr(config, "MERMAID_JS_URL", "")
    monkeypatch.setattr(config, "MERMAID_JS_PATH", str(tmp_path / "m.js"))
    monkeypatch.setattr(config, "OIDC_ISSUER", "")
    c = TestClient(main.app)
    r = c.get("/docs/o/r/docs/home/page.md")
    assert r.status_code == 200
    t = r.text
    assert 'class="crumbs"' in t and 'href="/docs/o/r/docs/home.md">home</a>' in t  # breadcrumbs
    assert 'class="toc"' in t and "On this page" in t  # table of contents
    assert '<pre class="mermaid">graph TD; A--&gt;B;' in t and '<script src="/docs/_assets/mermaid.js"></script>' in t and 'securityLevel: "strict"' in t and "prefers-color-scheme" in t
    assert "OP#12" in t  # existing backlink still there
    assert c.get("/docs/_assets/mermaid.js").text == "// mermaid"
    # without a mermaid copy the diagram stays a readable code block and no script is added
    monkeypatch.setattr(config, "MERMAID_JS_PATH", str(tmp_path / "gone.js"))
    t2 = c.get("/docs/o/r/docs/home/page.md").text
    assert "language-mermaid" in t2 and "mermaid.js" not in t2 and c.get("/docs/_assets/mermaid.js").status_code == 404
