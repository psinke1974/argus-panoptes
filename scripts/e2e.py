"""End-to-end test: Gitea push/PR -> glue -> OpenProject. Gebruik: python scripts/e2e.py (stack moet draaien)."""
import base64, os, subprocess, sys, tempfile, time
from pathlib import Path

import httpx


def load_env(path=".env"):
    env = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.split("   #")[0].strip()
    return env


E = load_env()
OP = httpx.Client(base_url="http://localhost:8080/api/v3", auth=("apikey", E["OP_API_KEY"]), timeout=30)
GT = httpx.Client(base_url="http://localhost:3000/api/v1", headers={"Authorization": f"token {E['GITEA_TOKEN']}"}, timeout=30)
suffix = str(int(time.time()))


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


statuses = {s["name"]: s["_links"]["self"]["href"] for s in OP.get("/statuses").json()["_embedded"]["elements"]}
print("OpenProject statussen:", list(statuses))
check(E.get("STATUS_CLOSED", "Closed") in statuses, "status 'Closed' bestaat")

r = OP.post("/projects", json={"name": f"E2E {suffix}", "identifier": f"e2e-{suffix}"})
check(r.status_code == 201, f"OpenProject-project aangemaakt ({r.status_code})")
proj = r.json()["id"]
types = OP.get(f"/projects/{proj}/types").json()["_embedded"]["elements"]
task = types[0]
r = OP.post("/work_packages", json={"subject": "E2E taak", "_links": {
    "project": {"href": f"/api/v3/projects/{proj}"}, "type": {"href": task["_links"]["self"]["href"]}}})
check(r.status_code == 201, f"werkpakket aangemaakt ({r.status_code})")
wp = r.json()["id"]

repo = f"e2e-{suffix}"
check(GT.post("/user/repos", json={"name": repo, "auto_init": True, "default_branch": "main"}).status_code == 201, "Gitea-repo aangemaakt")
r = GT.post(f"/repos/werkplaats/{repo}/hooks", json={"type": "gitea", "active": True, "events": ["push", "pull_request"],
    "config": {"url": "http://glue:8090/hooks/gitea", "content_type": "json", "secret": E["WEBHOOK_SECRET"]}})
check(r.status_code == 201, "webhook aangemaakt")


def put_file(path, content, branch, msg, new_branch=None):
    body = {"content": base64.b64encode(content.encode()).decode(), "message": msg, "branch": branch}
    if new_branch:
        body["new_branch"] = new_branch
    return GT.post(f"/repos/werkplaats/{repo}/contents/{path}", json=body)


def activities():
    r = OP.get(f"/work_packages/{wp}/activities").json()["_embedded"]["elements"]
    return [a.get("comment", {}).get("raw", "") for a in r]


def status():
    return OP.get(f"/work_packages/{wp}").json()["_links"]["status"]["title"]


def wait(fn, what, timeout=20):
    for _ in range(timeout):
        if fn():
            return True
        time.sleep(1)
    return False


# 1. PR met verwijzing -> comment + In progress
check(put_file("a.txt", "a", "main", f"feature voor OP#{wp}", new_branch="feat").status_code == 201, "commit op feature-branch")
r = GT.post(f"/repos/werkplaats/{repo}/pulls", json={"head": "feat", "base": "main", "title": f"Doe iets (fixes OP#{wp})"})
check(r.status_code == 201, "pull request geopend")
pr = r.json()["number"]
check(wait(lambda: any("Pull request geopend" in a for a in activities()), "pr-comment"), "OpenProject kreeg PR-opmerking")
check(wait(lambda: status() == E.get("STATUS_IN_PROGRESS", "In progress"), "in progress"), f"status = {status()}")

# 2. Merge -> Closed
r = GT.post(f"/repos/werkplaats/{repo}/pulls/{pr}/merge", json={"Do": "merge"})
check(r.status_code in (200, 204), f"PR gemerged ({r.status_code})")
check(wait(lambda: status() == E.get("STATUS_CLOSED", "Closed"), "closed"), f"status na merge = {status()}")

# 3. Docs-in-repo
check(put_file("docs/gids.md", "# Gids\n\nHet **geheime** woord is pinguin.\n", "main", f"docs: gids (OP#{wp})").status_code == 201, "docs/gids.md gepusht")
check(wait(lambda: any("Documentatie:" in a and "docs/gids.md" in a for a in activities()), "doc-link"), "OpenProject-opmerking bevat doc-link")
base_url = f"http://localhost:8090/docs/werkplaats/{repo}"
check("docs/gids.md" in httpx.get(base_url, timeout=30).text, "docs-index toont pagina")
v = httpx.get(f"{base_url}/docs/gids.md", timeout=30).text
check("<strong>geheime</strong>" in v and f"OP#{wp}" in v, "pagina gerenderd met backlink naar werkpakket")
check("pinguin" in httpx.get(f"{base_url}/search", params={"q": "pinguin"}, timeout=30).text, "zoeken vindt pagina")

# 4. Release notes
GT.post(f"/repos/werkplaats/{repo}/tags", json={"tag_name": "v0", "target": "main"})
base = GT.get(f"/repos/werkplaats/{repo}/commits", params={"limit": 50}).json()[-1]["sha"]
rn = httpx.get("http://localhost:8090/release-notes", params={"repo": f"werkplaats/{repo}", "base": base, "head": "main"}, timeout=30)
check(rn.status_code == 200 and "E2E taak" in rn.json()["markdown"], "release notes bevatten het werkpakket")
print(rn.json()["markdown"])
print("\nALLES GESLAAGD")
