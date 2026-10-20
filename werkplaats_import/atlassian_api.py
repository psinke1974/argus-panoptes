"""Directe import uit Jira (Cloud/Server) en Confluence Cloud via hun REST-API's."""
import html
import re
from pathlib import Path

import httpx

from .confluence import slug
from .confluence_storage import storage_to_html
from .jira import Issue


class Client:
    def __init__(self, base_url: str, email: str | None, token: str, transport=None):
        auth = (email, token) if email else None  # zonder e-mail: Bearer (Jira Server/DC Personal Access Token)
        headers = {} if email else {"Authorization": f"Bearer {token}"}
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, auth=auth, headers=headers, timeout=60, follow_redirects=True, transport=transport)

    def get(self, path: str, **params) -> dict:
        r = self.http.get(path, params=params)
        if r.status_code in (401, 403):
            raise SystemExit(f"Geen toegang ({r.status_code}) tot {self.base}{path}: controleer e-mail/token en rechten")
        r.raise_for_status()
        return r.json()

    def download(self, url: str) -> bytes:
        r = self.http.get(url)
        r.raise_for_status()
        return r.content


SPRINT_NAME = re.compile(r"name=([^,\]]+)")


def to_issue(raw: dict, sprint_field: str | None) -> Issue:
    f = raw["fields"]
    name = lambda k: (f.get(k) or {}).get("name", "")
    assignee = f.get("assignee") or {}
    sprints = []
    for sp in (f.get(sprint_field) or []) if sprint_field else []:
        sprints.append(sp.get("name", "") if isinstance(sp, dict) else (SPRINT_NAME.search(sp) or [None, sp])[1])
    links = []
    for l in f.get("issuelinks") or []:
        if "outwardIssue" in l:
            links.append((l["type"]["name"].lower(), l["outwardIssue"]["key"]))
    comments = [(c.get("created", "")[:16].replace("T", " "), (c.get("author") or {}).get("displayName", ""), c.get("body") or "")
                for c in (f.get("comment") or {}).get("comments", [])]
    parent = (f.get("parent") or {}).get("key")
    return Issue(
        key=raw["key"], id=str(raw.get("id", "")), summary=f.get("summary", ""), type=name("issuetype"), status=name("status"),
        priority=name("priority"), assignee=assignee.get("displayName", ""), reporter=(f.get("reporter") or {}).get("displayName", ""),
        created=(f.get("created") or "")[:16].replace("T", " "), description=f.get("description") or "", parent=parent,
        labels=f.get("labels") or [], sprints=[s for s in sprints if s], comments=comments, links=links,
        assignee_email=assignee.get("emailAddress", ""),
        attachments=[(a["filename"], a["content"]) for a in f.get("attachment") or []],
    )


def fetch_jira_issues(c: Client, project_key: str, server: bool = False) -> list[Issue]:
    fields = c.get("/rest/api/2/field")
    sprint_field = next((x["id"] for x in fields if x.get("name") == "Sprint"), None)
    jql = f'project = "{project_key}" ORDER BY key ASC'
    raws, token, start = [], None, 0
    while True:
        if server:  # Jira Server/DC: klassieke zoekopdracht met startAt
            page = c.get("/rest/api/2/search", jql=jql, fields="*all", startAt=start, maxResults=100)
            raws += page["issues"]
            start += len(page["issues"])
            if not page["issues"] or start >= page["total"]:
                break
        else:  # Cloud: /search/jql met nextPageToken
            params = {"jql": jql, "fields": "*all", "maxResults": 100}
            if token:
                params["nextPageToken"] = token
            page = c.get("/rest/api/2/search/jql", **params)
            raws += page["issues"]
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                break
    issues = [to_issue(r, sprint_field) for r in raws]
    for raw, issue in zip(raws, issues):  # comments: zoekresultaat kan afgekapt zijn
        cm = raw["fields"].get("comment") or {}
        if cm.get("total", 0) > len(cm.get("comments", [])):
            more = c.get(f"/rest/api/2/issue/{issue.key}/comment", maxResults=1000)["comments"]
            issue.comments = [(x.get("created", "")[:16].replace("T", " "), (x.get("author") or {}).get("displayName", ""), x.get("body") or "") for x in more]
    return issues


def export_confluence_space(c: Client, space_key: str, dest: Path) -> list[str]:
    """Haal een space op via de v2-API en schrijf een export in dezelfde vorm als de HTML-export. Geeft waarschuwingen terug."""
    spaces = c.get("/wiki/api/v2/spaces", keys=space_key)["results"]
    if not spaces:
        raise SystemExit(f"Space {space_key} niet gevonden")
    sid = spaces[0]["id"]
    pages, path, params = [], f"/wiki/api/v2/spaces/{sid}/pages", {"body-format": "storage", "limit": 100}
    while path:
        data = c.get(path, **params)
        pages += data["results"]
        path, params = data.get("_links", {}).get("next"), {}
    files = {p["title"]: f"{slug(p['title'])}_{p['id']}.html" for p in pages}
    warnings: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for p in pages:
        needed: set[str] = set()
        body = storage_to_html(p["body"]["storage"]["value"], p["id"], files, warnings, needed)
        if needed:
            atts = c.get(f"/wiki/api/v2/pages/{p['id']}/attachments", limit=250)["results"]
            for a in atts:
                if a["title"] in needed:
                    out = dest / "attachments" / p["id"] / a["title"]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    link = a["downloadLink"]
                    out.write_bytes(c.download(link if link.startswith("/wiki") else "/wiki" + link))
        (dest / files[p["title"]]).write_text(
            f'<html><head><title>{space_key} : {html.escape(p["title"])}</title></head><body><div id="main-content">{body}</div></body></html>', encoding="utf-8")
    by_parent: dict[str | None, list[dict]] = {}
    ids = {p["id"] for p in pages}
    for p in pages:
        by_parent.setdefault(p.get("parentId") if p.get("parentId") in ids else None, []).append(p)

    def ul(parent):
        items = "".join(f'<li><a href="{files[p["title"]]}">{html.escape(p["title"])}</a>{ul(p["id"])}</li>' for p in by_parent.get(parent, []))
        return f"<ul>{items}</ul>" if items else ""

    (dest / "index.html").write_text(f'<html><body><div id="content">{ul(None)}</div></body></html>', encoding="utf-8")
    return warnings
