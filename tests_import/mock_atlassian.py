"""Nep-Atlassian (Jira + Confluence Cloud) met de vorm van de echte REST-API's; voor tests en lokale proefimports.
Gebruik: python tests_import/mock_atlassian.py  (luistert op :8099)"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

USER = {"displayName": "Jan Jansen", "emailAddress": "jan@example.com"}


def issue(i, key, summary, itype, status, prio, desc, parent=None, comments=(), attachments=(), links=(), assignee=True):
    f = {"summary": summary, "issuetype": {"name": itype}, "status": {"name": status}, "priority": {"name": prio},
         "assignee": USER if assignee else None, "reporter": {"displayName": "Piet Pietersen"}, "created": "2026-10-20T09:00:00.000+0200",
         "description": desc, "labels": ["backend"], "customfield_10020": [{"name": "Sprint 4"}],
         "comment": {"total": len(comments), "comments": [{"created": "2026-10-21T10:00:00.000+0200", "author": {"displayName": "Jan Jansen"}, "body": b} for b in comments]},
         "attachment": [{"filename": n, "content": f"http://{{host}}/secure/attachment/{i}/{n}"} for n in attachments],
         "issuelinks": [{"type": {"name": t}, "outwardIssue": {"key": k}} for t, k in links]}
    if parent:
        f["parent"] = {"key": parent}
    return {"id": str(i), "key": key, "fields": f}


ISSUES = [
    issue(10001, "API-1", "Nieuwe portal", "Epic", "To Do", "Medium", "Grote epic.", links=[("Blocks", "API-4")]),
    issue(10002, "API-2", "Inloggen werkt niet", "Bug", "In Progress", "High", "h2. Probleem\n*Niet* inloggen.\n{code:python}\nprint(1)\n{code}",
          parent="API-1", comments=["Ik kijk ernaar, zie {{login.py}}"], attachments=["spec.txt"]),
    issue(10003, "API-3", "Test schrijven", "Sub-task", "Done", "Low", "", parent="API-2", assignee=False),
    issue(10004, "API-4", "Onbekend type", "Spike", "Wacht op klant", "Wishlist", "x", assignee=False),
]

PAGES = [
    {"id": "1", "title": "Home", "parentId": None, "storage": '<p>Welkom, zie <ac:link><ri:page ri:content-title="Architectuur" /></ac:link>.</p><ac:structured-macro ac:name="toc" />'
     '<ac:structured-macro ac:name="warning"><ac:rich-text-body><p>Niet in productie draaien.</p></ac:rich-text-body></ac:structured-macro>'},
    {"id": "2", "title": "Architectuur", "parentId": "1", "storage": '<h2>Overzicht</h2><p>Het <strong>systeem</strong>.</p>'
     '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sql</ac:parameter><ac:plain-text-body><![CDATA[SELECT * FROM t WHERE a < 2;]]></ac:plain-text-body></ac:structured-macro>'
     '<p><ac:image><ri:attachment ri:filename="diagram.png" /></ac:image></p><ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">API-1</ac:parameter></ac:structured-macro>'},
    {"id": "3", "title": "Database & opslag", "parentId": "2", "storage": "<table><tbody><tr><th>Naam</th><th>Type</th></tr><tr><td>id</td><td>int</td></tr></tbody></table>"},
]


def handle(path: str, query: dict, host: str):
    """-> (status, bytes, content-type)"""
    js = lambda o, st=200: (st, json.dumps(o).replace("{host}", host).encode(), "application/json")
    if path == "/rest/api/2/field":
        return js([{"id": "customfield_10020", "name": "Sprint"}, {"id": "summary", "name": "Summary"}])
    if path in ("/rest/api/2/search/jql", "/rest/api/2/search"):
        if "project" not in query["jql"][0]:
            return js({"errorMessages": ["bad jql"]}, 400)
        if path.endswith("/jql"):  # Cloud: 2 pagina's via nextPageToken
            if "nextPageToken" in query:
                return js({"issues": ISSUES[2:], "isLast": True})
            return js({"issues": ISSUES[:2], "nextPageToken": "tok2", "isLast": False})
        start = int(query.get("startAt", ["0"])[0])  # Server: startAt/total
        return js({"issues": ISSUES[start:start + 2], "total": len(ISSUES)})
    if path.startswith("/secure/attachment/"):
        return 200, b"bijlage-inhoud", "text/plain"
    if path == "/wiki/api/v2/spaces":
        return js({"results": [{"id": "99", "key": "TEAM"}] if query["keys"][0] == "TEAM" else []})
    if path == "/wiki/api/v2/spaces/99/pages":
        return js({"results": [{"id": p["id"], "title": p["title"], "parentId": p["parentId"], "body": {"storage": {"value": p["storage"]}}} for p in PAGES], "_links": {}})
    m = re.fullmatch(r"/wiki/api/v2/pages/(\d+)/attachments", path)
    if m:
        return js({"results": [{"title": "diagram.png", "downloadLink": "/download/attachments/2/diagram.png"}] if m.group(1) == "2" else []})
    if path == "/wiki/download/attachments/2/diagram.png":
        return 200, b"PNGDATA", "image/png"
    return js({"message": "niet gevonden"}, 404)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        status, body, ctype = handle(u.path, parse_qs(u.query), self.headers.get("Host", "localhost"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"Nep-Atlassian op http://localhost:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
