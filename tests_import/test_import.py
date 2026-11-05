from pathlib import Path

from panoptes_import import confluence, jira
from panoptes_import.jira_markup import jira_to_md

FX = Path(__file__).parent / "fixtures"


def test_jira_markup():
    md = jira_to_md("h2. Title\nThis is *bold* and _italic_ with {{code}}\n* one\n** two\n# num\n[x|http://a.b]\n{code:java}\nint x;\n{code}")
    assert "## Title" in md and "**bold**" in md and "*italic*" in md and "`code`" in md
    assert "- one" in md and "  - two" in md and "1. num" in md and "[x](http://a.b)" in md
    assert "```java\nint x;\n```" in md


def test_jira_table():
    assert "| Name | Value |\n|---|---|\n| a | b |" in jira_to_md("||Name||Value||\n|a|b|")


def test_read_csv():
    issues = jira.read_csv((FX / "jira.csv").read_text())
    by = {i.key: i for i in issues}
    assert by["DEMO-2"].labels == ["backend", "urgent"] and by["DEMO-2"].parent == "DEMO-1"
    assert by["DEMO-1"].links == [("blocks", "DEMO-4")]
    assert [i.key for i in jira.order_parents_first(issues)][0] == "DEMO-1"
    assert by["DEMO-2"].comments[0][0].startswith("21/Oct/26") and by["DEMO-2"].comments[0][1] == "abc123"


def test_pick_fallback():
    actual = {"Task": "/t", "Bug": "/f"}
    assert jira.pick("Bug", jira.TYPE_MAP, actual) == ("/f", True)
    assert jira.pick("Spike", jira.TYPE_MAP, actual, ["Task"]) == ("/t", False)


def test_confluence(tmp_path):
    written = confluence.convert_space(FX / "confluence", tmp_path, "TEAM")
    assert written == ["docs/home.md", "docs/home/architecture.md", "docs/home/architecture/database.md", "docs/home/operations.md"]
    home = (tmp_path / "docs/home.md").read_text()
    assert "[architecture](home/architecture.md)" in home and "Warning" in home and "https://example.com" in home
    arch = (tmp_path / "docs/home/architecture.md").read_text()
    assert "```sql\nSELECT 1;\n```" in arch and "(architecture/database.md)" in arch
    assert "](../_attachments/attachments/123/456.png)" in arch and (tmp_path / "docs/_attachments/attachments/123/456.png").exists()
    assert "| Name | Type |" in (tmp_path / "docs/home/architecture/database.md").read_text()


# ---- API importers against the fake Atlassian ----
import httpx

from panoptes_import import atlassian_api
from panoptes_import.confluence_storage import storage_to_html
from tests_import import mock_atlassian as mock


def mock_client():
    def respond(req: httpx.Request):
        status, body, ctype = mock.handle(req.url.path, {k: [v] for k, v in req.url.params.items()}, "mock")
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    return atlassian_api.Client("http://mock", "a@b.nl", "tok", transport=httpx.MockTransport(respond))


def test_jira_api_cloud_pagination():
    issues = atlassian_api.fetch_jira_issues(mock_client(), "API")
    assert [i.key for i in issues] == ["API-1", "API-2", "API-3", "API-4"]
    i2 = issues[1]
    assert i2.parent == "API-1" and i2.sprints == ["Sprint 4"] and i2.assignee_email == "jan@example.com"
    assert i2.comments == [("2026-10-21 10:00", "Jan Jansen", "I will look into it, see {{login.py}}")]
    assert i2.attachments == [("spec.txt", "http://mock/secure/attachment/10002/spec.txt")]
    assert issues[0].links == [("blocks", "API-4")]


def test_jira_api_server_paging():
    issues = atlassian_api.fetch_jira_issues(mock_client(), "API", server=True)
    assert len(issues) == 4


def test_storage_to_html():
    warnings, needed = [], set()
    out = storage_to_html(mock.PAGES[1]["storage"], "2", {"Architecture": "a_2.html"}, warnings, needed)
    assert 'brush: sql' in out and "SELECT * FROM t WHERE a &lt; 2;" in out
    assert needed == {"diagram.png"} and 'src="attachments/2/diagram.png"' in out
    assert any("jira" in w for w in warnings)
    home = storage_to_html(mock.PAGES[0]["storage"], "1", {"Architecture": "a_2.html"}, warnings, set())
    assert '<a href="a_2.html">Architecture</a>' in home and "toc" not in home and "confluence-information-macro-warning" in home


def test_confluence_api_roundtrip(tmp_path):
    warnings = atlassian_api.export_confluence_space(mock_client(), "TEAM", tmp_path / "src")
    pages = confluence.convert_space(tmp_path / "src", tmp_path / "out", "TEAM")
    assert pages == ["docs/home.md", "docs/home/architecture.md", "docs/home/architecture/database-storage.md"]
    arch = (tmp_path / "out/docs/home/architecture.md").read_text()
    assert "```sql" in arch and "_attachments/attachments/2/diagram.png" in arch
    assert (tmp_path / "out/docs/_attachments/attachments/2/diagram.png").read_bytes() == b"PNGDATA"
    assert "[Architecture](home/architecture.md)" in (tmp_path / "out/docs/home.md").read_text()
    assert warnings


# ---- name maps for OpenProject installs in other languages (issue #6) ----
import pytest

from panoptes_import import op_names


@pytest.mark.parametrize("lang", op_names.LANGUAGES)
def test_every_language_resolves_the_standard_items(lang):
    """Simulate an OpenProject install in `lang`: its own seed names must be found for common Jira values, without fallback."""
    n = op_names.NAMES[lang]
    types = {name: f"/types/{i}" for i, name in enumerate(n["types"])}
    statuses = {name: f"/statuses/{i}" for i, name in enumerate(n["statuses"])}
    prios = {name: f"/priorities/{i}" for i, name in enumerate(n["priorities"])}
    for jira_value, english in (("bug", "Bug"), ("epic", "Epic"), ("story", "User story"), ("sub-task", "Task")):
        href, exact = jira.pick(jira_value, jira.TYPE_MAP, types)
        assert exact and href == types[n["types"][op_names.NAMES["en"]["types"].index(english)]], (lang, jira_value)
    for jira_value, english in (("to do", "New"), ("in progress", "In progress"), ("done", "Closed"), ("blocked", "On hold"), ("rejected", "Rejected")):
        href, exact = jira.pick(jira_value, jira.STATUS_MAP, statuses)
        assert exact and href == statuses[n["statuses"][op_names.NAMES["en"]["statuses"].index(english)]], (lang, jira_value)
    for jira_value, english in (("highest", "Immediate"), ("medium", "Normal"), ("low", "Low")):
        href, exact = jira.pick(jira_value, jira.PRIO_MAP, prios)
        assert exact and href == prios[n["priorities"][op_names.NAMES["en"]["priorities"].index(english)]], (lang, jira_value)


def test_fallbacks_exist_in_every_language():
    for lang in op_names.LANGUAGES:
        for kind, english in (("types", "Task"), ("statuses", "New"), ("priorities", "Normal")):
            assert op_names.NAMES[lang][kind][op_names.NAMES["en"][kind].index(english)] in op_names.candidates(kind, english)


def test_custom_map():
    tm, sm, pm = jira.with_custom_map({"types": {"Spike": "Task"}, "statuses": {"Waiting for customer": "On hold", "Awaiting": "Mijn eigen status"}, "priorities": {"Wishlist": "Low"}})
    assert "Task" in tm["spike"] and "Aufgabe" in tm["spike"]  # a standard English name is translated automatically
    assert "Gestoppt" in sm["waiting for customer"]
    assert sm["awaiting"] == ["Mijn eigen status"]  # anything else is taken as the exact name in your OpenProject
    assert "Niedrig" in pm["wishlist"] and jira.TYPE_MAP.get("spike") is None  # defaults are not modified
    assert jira.pick("Spike", tm, {"Aufgabe": "/t"}) == ("/t", True)


# ---- worklogs (issue #3) ----
def test_iso_duration():
    assert jira.iso_duration(5400) == "PT1H30M" and jira.iso_duration(60) == "PT0H1M" and jira.iso_duration(10) == "PT0H1M"
    assert jira.iso_duration(3600 * 8) == "PT8H0M" and jira.iso_duration(3600 * 26 + 120) == "PT26H2M"


def test_worklogs_from_api_including_paging():
    issues = atlassian_api.fetch_jira_issues(mock_client(), "API")
    logs = {i.key: i.worklogs for i in issues}["API-2"]
    assert [(w["date"], w["seconds"], w["author"]) for w in logs] == [("2026-10-20", 5400, "Jan Jansen"), ("2026-10-21", 1800, "Piet Pietersen"), ("2026-10-22", 600, "Jan Jansen")]
    assert logs[0]["email"] == "jan@example.com" and logs[0]["comment"] == "Investigated the login bug"
    assert all(not i.worklogs for i in issues if i.key != "API-2")


def test_user_resolver_matches_on_exact_email_and_user_map():
    class FakeOP:
        calls = 0

        def all_users(self):
            FakeOP.calls += 1
            return [{"email": "Jan@Example.com", "_links": {"self": {"href": "/api/v3/users/7"}}},
                    {"email": "", "_links": {"self": {"href": "/api/v3/users/8"}}},
                    {"email": "piet@example.com", "_links": {"self": {"href": "/api/v3/users/9"}}}]

    r = jira.UserResolver(FakeOP(), {"Kees": "/api/v3/users/1"})
    assert r.href("Kees") == "/api/v3/users/1"  # --user-map wins and needs no lookup
    assert r.href("Jan Jansen", "jan@example.com") == "/api/v3/users/7"  # case-insensitive exact match
    assert r.href("Piet", "piet@example.com") == "/api/v3/users/9"
    assert r.href("Nobody", "nobody@example.com") is None and r.href("No email") is None
    assert FakeOP.calls == 1  # the user list is fetched once


# ---- the whole run_issues pipeline against a fake OpenProject (no Docker needed) ----
class FakeOpenProject:
    """Records what the importer does; answers like the real API where it matters."""

    def __init__(self, users=()):
        self.calls, self._users, self._next = [], list(users), 100
        self.projects = {}

    def elements(self, path):
        names = {"/types": ["Task", "Bug", "Epic"], "/statuses": ["New", "In progress", "Closed"], "/priorities": ["Low", "Normal", "High"],
                 "/roles": ["Member", "Reader"]}[path.split("?")[0]]
        return [{"name": n, "_links": {"self": {"href": f"{path}/{n}"}}} for n in names]

    def all_users(self):
        return self._users

    def req(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if method == "GET" and path.startswith("/projects/"):
            raise RuntimeError("404")  # project does not exist yet
        if method == "GET" and path == "/users/me":
            return {"_links": {"self": {"href": "/api/v3/users/1"}}}
        if method == "GET" and path.endswith("/types"):
            return {"_embedded": {"elements": [{"name": n} for n in ("Task", "Bug", "Epic")]}}
        self._next += 1
        return {"id": self._next, "_embedded": {"elements": [{"name": n} for n in ("Task", "Bug", "Epic")]}}

    def upload_attachment(self, *a):
        self.calls.append(("UPLOAD", a[1], None))


def _issues():
    return atlassian_api.fetch_jira_issues(mock_client(), "API")


def test_run_issues_links_people_adds_members_and_logs_time(tmp_path, monkeypatch):
    monkeypatch.setattr(jira, "ensure_types", lambda *a, **k: None)
    op = FakeOpenProject(users=[{"email": "jan@example.com", "_links": {"self": {"href": "/api/v3/users/7"}}}])
    report = jira.run_issues(op, _issues(), "API", "api", {}, False, state_dir=str(tmp_path), download=lambda url: b"x")
    posts = [(p, j) for m, p, j in op.calls if m == "POST"]
    # Jan (matched on e-mail) is added to the project as a Member exactly once, before being used
    memberships = [j for p, j in posts if p == "/memberships"]
    assert len(memberships) == 1 and memberships[0]["_links"]["principal"]["href"] == "/api/v3/users/7" and memberships[0]["_links"]["roles"][0]["href"] == "/roles/Member"
    # he is the assignee of API-1/API-2, and 3 time entries exist: Jan's own two under his account, Piet's one under the importer
    wps = {j["subject"].split("]")[0].lstrip("["): j for p, j in posts if p.endswith("/work_packages")}
    assert wps["API-1"]["_links"]["assignee"]["href"] == "/api/v3/users/7" and wps["API-2"]["_links"]["assignee"]["href"] == "/api/v3/users/7"
    assert "assignee" not in wps["API-3"]["_links"] and "assignee (Jira):" not in wps["API-2"]["description"]["raw"]
    entries = [j for p, j in posts if p == "/time_entries"]
    assert [(e["spentOn"], e["hours"], e["_links"]["user"]["href"]) for e in entries] == [("2026-10-20", "PT1H30M", "/api/v3/users/7"), ("2026-10-21", "PT0H30M", "/api/v3/users/1"), ("2026-10-22", "PT0H10M", "/api/v3/users/7")]
    assert entries[1]["comment"]["raw"].startswith("[Jira worklog by Piet Pietersen]")
    assert "time entries: 3" in report and "people added as project members: 1" in report
    # re-running creates nothing twice
    n = len(op.calls)
    again = jira.run_issues(op, _issues(), "API", "api", {}, False, state_dir=str(tmp_path))
    assert "Created: 0" in again and [c for c in op.calls[n:] if c[0] == "POST" and c[1].endswith("/work_packages")] == []


def test_run_issues_without_adding_members(tmp_path, monkeypatch):
    monkeypatch.setattr(jira, "ensure_types", lambda *a, **k: None)
    op = FakeOpenProject(users=[{"email": "jan@example.com", "_links": {"self": {"href": "/api/v3/users/7"}}}])
    jira.run_issues(op, _issues(), "API", "api", {}, False, state_dir=str(tmp_path), add_members=False)
    posts = [(p, j) for m, p, j in op.calls if m == "POST"]
    assert not [1 for p, _ in posts if p == "/memberships"]
    assert all("assignee" not in j["_links"] for p, j in posts if p.endswith("/work_packages"))
    assert all(e["_links"]["user"]["href"] == "/api/v3/users/1" for p, e in posts if p == "/time_entries")


def test_unknown_member_role_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(jira, "ensure_types", lambda *a, **k: None)
    op = FakeOpenProject(users=[{"email": "jan@example.com", "_links": {"self": {"href": "/api/v3/users/7"}}}])
    report = jira.run_issues(op, _issues(), "API", "api", {}, False, state_dir=str(tmp_path), member_role="Does not exist")
    assert "Project role 'Does not exist' not found" in report


# ---- Confluence comments and labels (issue #4) ----
def test_confluence_comments_and_labels(tmp_path):
    warnings = atlassian_api.export_confluence_space(mock_client(), "TEAM", tmp_path / "src")
    pages = confluence.convert_space(tmp_path / "src", tmp_path / "out", "TEAM")
    arch = (tmp_path / "out/docs/home/architecture.md").read_text()
    assert "*Labels: architecture, draft*" in arch
    # both comment pages, in order, authors resolved; an author lookup that is refused falls back to the account id
    assert "## Comments" in arch
    i1, i2, i3 = arch.index("Please add a diagram."), arch.index("Agreed, see **section 2**."), arch.index("Typo in this sentence")
    assert i1 < i2 < i3
    assert "**Jan Jansen** (2026-10-21)" in arch and "**Piet Pietersen** (2026-10-22)" in arch and "**acc-denied** (Inline comment, 2026-10-23)" in arch
    # pages without comments/labels get neither section
    home = (tmp_path / "out/docs/home.md").read_text()
    assert "Comments" not in home and "Labels:" not in home
    # the option to skip them
    atlassian_api.export_confluence_space(mock_client(), "TEAM", tmp_path / "src2", with_extras=False)
    plain = confluence.convert_space(tmp_path / "src2", tmp_path / "out2", "TEAM")
    assert "Comments" not in (tmp_path / "out2/docs/home/architecture.md").read_text()


def test_pagination_follows_next_links_and_detects_loops():
    # the fake API returns the 3 pages of the space in two chunks; all of them must arrive (this once looped forever)
    got = atlassian_api.paged(mock_client(), "/wiki/api/v2/spaces/99/pages", **{"body-format": "storage", "limit": 100})
    assert [p["id"] for p in got] == ["1", "2", "3"]

    def looping(req: httpx.Request):
        return httpx.Response(200, json={"results": [{"id": "x"}], "_links": {"next": "/loop?cursor=1"}})

    c = atlassian_api.Client("http://mock", "a@b.nl", "tok", transport=httpx.MockTransport(looping))
    with pytest.raises(SystemExit, match="pagination loop"):
        atlassian_api.paged(c, "/loop")
