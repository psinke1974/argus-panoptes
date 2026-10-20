from pathlib import Path

from werkplaats_import import confluence, jira
from werkplaats_import.jira_markup import jira_to_md

FX = Path(__file__).parent / "fixtures"


def test_jira_markup():
    md = jira_to_md("h2. Titel\nDit is *vet* en _schuin_ met {{code}}\n* een\n** twee\n# nr\n[x|http://a.b]\n{code:java}\nint x;\n{code}")
    assert "## Titel" in md and "**vet**" in md and "*schuin*" in md and "`code`" in md
    assert "- een" in md and "  - twee" in md and "1. nr" in md and "[x](http://a.b)" in md
    assert "```java\nint x;\n```" in md


def test_jira_table():
    assert "| Naam | Waarde |\n|---|---|\n| a | b |" in jira_to_md("||Naam||Waarde||\n|a|b|")


def test_read_csv():
    issues = jira.read_csv((FX / "jira.csv").read_text())
    by = {i.key: i for i in issues}
    assert by["DEMO-2"].labels == ["backend", "urgent"] and by["DEMO-2"].parent == "DEMO-1"
    assert by["DEMO-1"].links == [("blocks", "DEMO-4")]
    assert [i.key for i in jira.order_parents_first(issues)][0] == "DEMO-1"
    assert by["DEMO-2"].comments[0].startswith("21/Oct/26")


def test_pick_fallback():
    actual = {"Taak": "/t", "Fout": "/f"}
    assert jira.pick("Bug", jira.TYPE_MAP, actual) == ("/f", True)
    assert jira.pick("Spike", jira.TYPE_MAP, actual, "Taak") == ("/t", False)


def test_confluence(tmp_path):
    written = confluence.convert_space(FX / "confluence", tmp_path, "TEAM")
    assert written == ["docs/home.md", "docs/home/architectuur.md", "docs/home/architectuur/database.md", "docs/home/beheer.md"]
    home = (tmp_path / "docs/home.md").read_text()
    assert "[architectuur](home/architectuur.md)" in home and "Waarschuwing" in home and "https://example.com" in home
    arch = (tmp_path / "docs/home/architectuur.md").read_text()
    assert "```sql\nSELECT 1;\n```" in arch and "(architectuur/database.md)" in arch
    assert "](../_bijlagen/attachments/123/456.png)" in arch and (tmp_path / "docs/_bijlagen/attachments/123/456.png").exists()
    assert "| Naam | Type |" in (tmp_path / "docs/home/architectuur/database.md").read_text()
