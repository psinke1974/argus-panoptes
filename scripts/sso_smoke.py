"""Smoke test for SSO on a tenant (after `tenantctl.py enable-sso <tenant>`): drives real sign-ins through Keycloak.
Usage: python scripts/sso_smoke.py acme

Checks: discovery, redirect to Keycloak, sign-in to the docs portal and to Git, per-user repository access
(a private repo stays hidden from other users), the admin-only stale report, and sign-out."""
import html
import re
import secrets
import sys
import time
from pathlib import Path

import httpx

tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
env = dict(l.split("=", 1) for l in Path(f"tenants/{tenant}/.env").read_text().splitlines() if "=" in l and not l.startswith("#"))
verify = env.get("TLS_VERIFY", "true") != "false"
host = env["OP_HOST"]
iss = env["OIDC_ISSUER"]
docs, git = f"https://docs.{host}", f"https://git.{host}"
suffix = secrets.token_hex(3)


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def new_client():
    return httpx.Client(verify=verify, timeout=30, follow_redirects=True)


# ---- Keycloak admin API: create two test users with permanent passwords (one admin, one plain)
kc = new_client()
tok = kc.post(f"https://auth.{host}/realms/master/protocol/openid-connect/token",
              data={"grant_type": "password", "client_id": "admin-cli", "username": "kcadmin", "password": env["KC_ADMIN_PASSWORD"]}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
users = {}
for name, roles in ((f"smoke-admin-{suffix}", ["admin"]), (f"smoke-user-{suffix}", [])):
    pw = secrets.token_urlsafe(14)
    r = kc.post(f"https://auth.{host}/admin/realms/panoptes/users", headers=H, json={
        "username": name, "email": f"{name}@example.com", "emailVerified": True, "enabled": True, "firstName": "Smoke", "lastName": name,
        "credentials": [{"type": "password", "value": pw, "temporary": False}]})
    check(r.status_code == 201, f"Keycloak test user {name} created")
    uid = r.headers["location"].rsplit("/", 1)[1]
    for role in roles:
        rep = kc.get(f"https://auth.{host}/admin/realms/panoptes/roles/{role}", headers=H).json()
        kc.post(f"https://auth.{host}/admin/realms/panoptes/users/{uid}/role-mappings/realm", headers=H, json=[rep])
    users[name] = (pw, uid)
admin_name, plain_name = list(users)


def keycloak_login(c: httpx.Client, start: str, username: str, password: str, expect_form: bool = True) -> httpx.Response:
    """Follow redirects to the Keycloak form, submit credentials, follow back to the app.
    If the client already has a Keycloak session the redirect chain completes without a form (single sign-on)."""
    r = c.get(start)
    m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', r.text) or re.search(r'action="([^"]+)"[^>]*id="kc-form-login"', r.text)
    if not m:
        check(not expect_form, f"signed in silently through the existing Keycloak session at {start.split('?')[0]}")
        return r
    if not expect_form:
        sys.exit(f"FAIL unexpected Keycloak form at {start.split('?')[0]}")
    print(f"OK   reached the Keycloak sign-in form from {start.split('?')[0]}")
    return c.post(html.unescape(m.group(1)), data={"username": username, "password": password, "credentialId": ""})


try:
    check(kc.get(f"{iss}/.well-known/openid-configuration").json()["issuer"] == iss, "Keycloak discovery reachable with the public issuer")

    # anonymous access is redirected to Keycloak
    anon = httpx.Client(verify=verify, timeout=30, follow_redirects=False)
    r = anon.get(f"{docs}/docs/panoptes/x")
    check(r.status_code in (302, 307) and "/auth/login" in r.headers["location"], "docs portal redirects anonymous users to its login")
    r = anon.get(f"{docs}/stale?project=x&format=json")
    check(r.status_code == 401, "JSON API answers 401 instead of redirecting")
    check(anon.post(f"{docs}/hooks/gitea", content=b"{}").status_code == 404, "webhook endpoint still not reachable from outside")

    # a private repo and a public repo owned by the service user
    gt = httpx.Client(base_url=f"{git}/api/v1", headers={"Authorization": f"token {env['GITEA_TOKEN']}"}, verify=verify, timeout=30)
    for repo, private in ((f"sso-public-{suffix}", False), (f"sso-private-{suffix}", True)):
        check(gt.post("/user/repos", json={"name": repo, "private": private, "auto_init": True, "readme": "Default", "default_branch": "main"}).status_code == 201, f"repo {repo} created")
        import base64
        gt.post(f"/repos/panoptes/{repo}/contents/docs/hello.md", json={"content": base64.b64encode(b"# Hello\n\nsso docs\n").decode(), "message": "docs", "branch": "main"})
    pub, priv = f"sso-public-{suffix}", f"sso-private-{suffix}"

    # --- plain user: sign in to Git first (creates the Gitea account), then the docs portal
    u = new_client()
    r = keycloak_login(u, f"{git}/user/oauth2/keycloak", plain_name, users[plain_name][0])
    check(r.status_code == 200 and "/user/login" not in str(r.url) and (f"/{plain_name}" in r.text or "Dashboard" in r.text or "Explore" in r.text or str(r.url).startswith(git)), "plain user signed in to Git through Keycloak (account auto-created)")
    check(gt.get(f"/users/{plain_name}").status_code == 200, f"Gitea account {plain_name} exists")

    r = keycloak_login(u, f"{docs}/docs/panoptes/{pub}/docs/hello.md", plain_name, users[plain_name][0], expect_form=False)
    check(r.status_code == 200 and "sso docs" in r.text, "plain user reads the PUBLIC repo's docs after signing in")
    r = u.get(f"{docs}/docs/panoptes/{priv}/docs/hello.md")
    check(r.status_code == 404, "plain user cannot read the PRIVATE repo's docs (checked per user through Gitea)")
    r = u.get(f"{docs}/stale?project=x&format=json")
    check(r.status_code == 403, "plain user is refused on the admin-only stale report")

    # --- admin: same flows, plus stale
    a = new_client()
    keycloak_login(a, f"{git}/user/oauth2/keycloak", admin_name, users[admin_name][0])
    keycloak_login(a, f"{docs}/auth/login?next=%2F", admin_name, users[admin_name][0], expect_form=False)
    r = a.get(f"{docs}/stale", params={"project": "api-demo", "days": 0, "format": "json"})
    check(r.status_code == 200 and "items" in r.text, "admin (realm role) can use the stale report through SSO")

    # a search query must survive the login redirect (the next= parameter is URL-encoded)
    fresh = httpx.Client(verify=verify, timeout=30, follow_redirects=False)
    loc = fresh.get(f"{docs}/docs/panoptes/{pub}/search", params={"q": "sso", "x": "1"}).headers["location"]
    check("next=%2Fdocs%2Fpanoptes%2F" in loc and "%3Fq%3Dsso%26x%3D1" in loc, "login redirect keeps the whole query string (encoded)")

    # --- single sign-on: the second app did not ask for credentials again
    s = new_client()
    keycloak_login(s, f"{docs}/auth/login?next=%2F", plain_name, users[plain_name][0])
    keycloak_login(s, f"{git}/user/oauth2/keycloak", plain_name, users[plain_name][0], expect_form=False)

    # --- sign out ends the docs session
    u.get(f"{docs}/auth/logout")
    r = httpx.Client(verify=verify, timeout=30, follow_redirects=False, cookies=u.cookies).get(f"{docs}/docs/panoptes/{pub}")
    check(r.status_code in (302, 307), "after sign-out the docs portal asks for a login again")
    print("\nALL PASSED")
finally:  # clean up the Keycloak test users
    for name, (_, uid) in users.items():
        kc.delete(f"https://auth.{host}/admin/realms/panoptes/users/{uid}", headers=H)
