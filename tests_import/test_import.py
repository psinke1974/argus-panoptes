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
