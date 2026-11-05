"""Direct import from Jira (Cloud/Server) and Confluence Cloud through their REST APIs."""
import html
import re
from pathlib import Path

import httpx

from .confluence import slug
from .confluence_storage import storage_to_html
from .jira import Issue


class Client:
    def __init__(self, base_url: str, email: str | None, token: str, transport=None):
        auth = (email, token) if email else None  # without e-mail: Bearer (Jira Server/DC Personal Access Token)
        headers = {} if email else {"Authorization": f"Bearer {token}"}
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, auth=auth, headers=headers, timeout=60, follow_redirects=True, transport=transport)

    def get(self, path: str, **params) -> dict:
        # params=None (not {}) when there are none: httpx replaces the URL's own query string (?cursor=...) with an empty dict
        r = self.http.get(path, params=params or None)
        if r.status_code in (401, 403):
            raise SystemExit(f"No access ({r.status_code}) to {self.base}{path}: check e-mail/token and permissions")
        r.raise_for_status()
        return r.json()

    def download(self, url: str) -> bytes:
        r = self.http.get(url)
        r.raise_for_status()
        return r.content


SPRINT_NAME = re.compile(r"name=([^,\]]+)")


def to_worklog(w: dict) -> dict:
    """Jira worklog -> {date, seconds, author, email, comment}. 'started' looks like 2026-10-20T09:00:00.000+0200."""
    author = w.get("author") or {}
    return {"date": (w.get("started") or "")[:10], "seconds": int(w.get("timeSpentSeconds") or 0), "author": author.get("displayName", ""),
            "email": author.get("emailAddress", ""), "comment": w.get("comment") if isinstance(w.get("comment"), str) else ""}


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
    worklogs = [to_worklog(w) for w in (f.get("worklog") or {}).get("worklogs", [])]
    return Issue(
        key=raw["key"], id=str(raw.get("id", "")), summary=f.get("summary", ""), type=name("issuetype"), status=name("status"),
        priority=name("priority"), assignee=assignee.get("displayName", ""), reporter=(f.get("reporter") or {}).get("displayName", ""),
        created=(f.get("created") or "")[:16].replace("T", " "), description=f.get("description") or "", parent=parent,
        labels=f.get("labels") or [], sprints=[s for s in sprints if s], comments=comments, links=links,
        assignee_email=assignee.get("emailAddress", ""),
        attachments=[(a["filename"], a["content"]) for a in f.get("attachment") or []],
        worklogs=worklogs,
    )


def fetch_jira_issues(c: Client, project_key: str, server: bool = False) -> list[Issue]:
    fields = c.get("/rest/api/2/field")
    sprint_field = next((x["id"] for x in fields if x.get("name") == "Sprint"), None)
    jql = f'project = "{project_key}" ORDER BY key ASC'
    raws, token, start = [], None, 0
    while True:
        if server:  # Jira Server/DC: classic search with startAt
            page = c.get("/rest/api/2/search", jql=jql, fields="*all", startAt=start, maxResults=100)
            raws += page["issues"]
            start += len(page["issues"])
            if not page["issues"] or start >= page["total"]:
                break
        else:  # Cloud: /search/jql with nextPageToken
            params = {"jql": jql, "fields": "*all", "maxResults": 100}
            if token:
                params["nextPageToken"] = token
            page = c.get("/rest/api/2/search/jql", **params)
            raws += page["issues"]
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                break
    issues = [to_issue(r, sprint_field) for r in raws]
    for raw, issue in zip(raws, issues):  # comments and worklogs: the search result may be truncated
        wl = raw["fields"].get("worklog") or {}
        if wl.get("total", 0) > len(wl.get("worklogs", [])):
            more, start = [], 0
            while True:
                page = c.get(f"/rest/api/2/issue/{issue.key}/worklog", startAt=start, maxResults=1000)
                more += page["worklogs"]
                start += len(page["worklogs"])
                if not page["worklogs"] or start >= page["total"]:
                    break
            issue.worklogs = [to_worklog(w) for w in more]
        cm = raw["fields"].get("comment") or {}
        if cm.get("total", 0) > len(cm.get("comments", [])):
            more = c.get(f"/rest/api/2/issue/{issue.key}/comment", maxResults=1000)["comments"]
            issue.comments = [(x.get("created", "")[:16].replace("T", " "), (x.get("author") or {}).get("displayName", ""), x.get("body") or "") for x in more]
    return issues


def paged(c: Client, path: str, **params) -> list[dict]:
    """All results of a v2 collection (follows _links.next)."""
    out, seen = [], set()
    while path:
        if path in seen:  # a server that keeps returning the same next link must not hang the import
            raise SystemExit(f"pagination loop at {path}")
        seen.add(path)
        data = c.get(path, **params)
        out += data["results"]
        path, params = data.get("_links", {}).get("next"), {}
    return out


class Authors:
    """accountId -> display name (v2 comments only carry the id); looked up once per person, falls back to the id."""

    def __init__(self, c: Client):
        self.c, self.cache = c, {}

    def name(self, account_id: str | None) -> str:
        if not account_id:
            return "unknown"
        if account_id not in self.cache:
            try:
                self.cache[account_id] = self.c.get("/wiki/rest/api/user", accountId=account_id).get("displayName") or account_id
            except (Exception, SystemExit):  # no permission to look people up (the client signals 403 with SystemExit): show the id rather than failing the export
                self.cache[account_id] = account_id
        return self.cache[account_id]


def page_extras_html(c: Client, page: dict, authors: Authors, files: dict, warnings: list[str]) -> tuple[str, str]:
    """(labels paragraph for the top of the page, comments section for the bottom), as HTML. Both can be empty."""
    labels = [l["name"] for l in paged(c, f"/wiki/api/v2/pages/{page['id']}/labels", limit=250)]
    top = f"<p><em>Labels: {html.escape(', '.join(labels))}</em></p>" if labels else ""
    items = []
    for kind, path in (("", "footer-comments"), ("Inline comment, ", "inline-comments")):
        for cm in paged(c, f"/wiki/api/v2/pages/{page['id']}/{path}", **{"body-format": "storage", "limit": 100}):
            ver = cm.get("version") or {}
            body = storage_to_html((cm.get("body") or {}).get("storage", {}).get("value", ""), page["id"], files, warnings, set())
            items.append(f"<blockquote><p><strong>{html.escape(authors.name(ver.get('authorId')))}</strong> "
                         f"({html.escape(kind)}{html.escape((ver.get('createdAt') or '')[:10])})</p>{body}</blockquote>")
    return top, (f"<h2>Comments</h2>{''.join(items)}" if items else "")


def export_confluence_space(c: Client, space_key: str, dest: Path, with_extras: bool = True) -> list[str]:
    """Fetch a space through the v2 API and write an export shaped like the HTML export. Returns warnings."""
    spaces = c.get("/wiki/api/v2/spaces", keys=space_key)["results"]
    if not spaces:
        raise SystemExit(f"Space {space_key} not found")
    sid = spaces[0]["id"]
    pages = paged(c, f"/wiki/api/v2/spaces/{sid}/pages", **{"body-format": "storage", "limit": 100})
    files = {p["title"]: f"{slug(p['title'])}_{p['id']}.html" for p in pages}
    warnings: list[str] = []
    authors = Authors(c)
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
        top, bottom = (page_extras_html(c, p, authors, files, warnings) if with_extras else ("", ""))
        (dest / files[p["title"]]).write_text(
            f'<html><head><title>{space_key} : {html.escape(p["title"])}</title></head><body><div id="main-content">{top}{body}{bottom}</div></body></html>', encoding="utf-8")
    by_parent: dict[str | None, list[dict]] = {}
    ids = {p["id"] for p in pages}
    for p in pages:
        by_parent.setdefault(p.get("parentId") if p.get("parentId") in ids else None, []).append(p)

    def ul(parent):
        items = "".join(f'<li><a href="{files[p["title"]]}">{html.escape(p["title"])}</a>{ul(p["id"])}</li>' for p in by_parent.get(parent, []))
        return f"<ul>{items}</ul>" if items else ""

    (dest / "index.html").write_text(f'<html><body><div id="content">{ul(None)}</div></body></html>', encoding="utf-8")
    return warnings
