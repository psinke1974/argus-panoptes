"""Smoke test for a tenant stack: routing, docs-portal protection, webhook isolation and the git -> ticket chain.
Usage: python scripts/tenant_smoke.py acme   (after `tenantctl.py create acme` and a Jira import for project API, see README)"""
import base64
import json
import sys
import time
from pathlib import Path

import httpx

tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
env = dict(l.split("=", 1) for l in Path(f"tenants/{tenant}/.env").read_text().splitlines() if "=" in l and not l.startswith("#"))
verify = env.get("TLS_VERIFY", "true") != "false"
host = env["OP_HOST"]
http = httpx.Client(verify=verify, timeout=30)
op = httpx.Client(base_url=f"https://{host}/api/v3", auth=("apikey", env["OP_API_KEY"]), verify=verify, timeout=30)
gt = httpx.Client(base_url=f"https://git.{host}/api/v1", headers={"Authorization": f"token {env['GITEA_TOKEN']}"}, verify=verify, timeout=30)


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


check(op.get("/").status_code == 200, "OpenProject API reachable over HTTPS with the API key")
check(gt.get("/user").json()["login"] == "panoptes", "Gitea API reachable over HTTPS")
docs = f"https://docs.{host}"
sso = env.get("SSO_ENABLED") == "1"
auth = None if sso else ("docs", env["DOCS_PASSWORD"])
if sso:  # per-user checks live in scripts/sso_smoke.py
    r = httpx.Client(verify=verify, timeout=30, follow_redirects=False).get(f"{docs}/docs/panoptes/x")
    check(r.status_code in (302, 307) and "/auth/login" in r.headers["location"], "docs portal sends anonymous users to the SSO login")
else:
    check(http.get(f"{docs}/health").status_code == 401, "docs portal refuses anonymous requests")
    check(http.get(f"{docs}/health", auth=auth).status_code == 200, "docs portal accepts the tenant password")
check(http.post(f"{docs}/hooks/gitea", auth=auth, content=b"{}").status_code == 404, "webhook endpoint is not reachable from outside")

if not sso and gt.get("/repos/panoptes/confluence-demo").status_code == 200:
    r = http.get(f"{docs}/docs/panoptes/confluence-demo", auth=auth)
    check(r.status_code == 200 and "docs/home.md" in r.text, "docs portal lists the imported Confluence pages")

if not sso and Path(f"tenants/{tenant}/import-state/jira-api-demo.json").exists():
    r = http.get(f"{docs}/stale", params={"project": "api-demo", "days": 0, "format": "json"}, auth=auth)
    check(r.status_code == 200 and "items" in r.json(), "stale-ticket report works")

# git -> ticket chain with an old Jira key from the import mapping
state = Path(f"tenants/{tenant}/import-state/jira-api-demo.json")
if state.exists():
    wp = json.loads(state.read_text())["API-2"]
    repo = f"smoke-{int(time.time())}"
    check(gt.post("/user/repos", json={"name": repo, "auto_init": True, "default_branch": "main"}).status_code == 201, "repo created")
    check(gt.post(f"/repos/panoptes/{repo}/hooks", json={"type": "gitea", "active": True, "events": ["push"],
          "config": {"url": "http://glue:8090/hooks/gitea", "content_type": "json", "secret": env["WEBHOOK_SECRET"]}}).status_code == 201, "internal webhook created")
    body = {"content": base64.b64encode(b"x").decode(), "message": "fixes API-2: old key through the tenant glue", "branch": "main"}
    check(gt.post(f"/repos/panoptes/{repo}/contents/x.txt", json=body).status_code == 201, "commit pushed")
    closed = env["STATUS_CLOSED"]
    for _ in range(30):
        if op.get(f"/work_packages/{wp}").json()["_links"]["status"]["title"] == closed:
            break
        time.sleep(1)
    check(op.get(f"/work_packages/{wp}").json()["_links"]["status"]["title"] == closed, f"API-2 closed through the old Jira key (status {closed})")
else:
    print("SKIP chain test (no Jira import for this tenant yet)")
print("\nALL PASSED")
