"""Smoke test for the shared directory (after `tenantctl.py enable-sso` and `enable-ldap`):
one directory account signs in to OpenProject (LDAP), Git and the docs portal (Keycloak federation).
Usage: python scripts/ldap_smoke.py demo"""
import html
import re
import secrets
import subprocess
import sys
from pathlib import Path

import httpx

tenant = sys.argv[1] if len(sys.argv) > 1 else "demo"
env = dict(l.split("=", 1) for l in Path(f"tenants/{tenant}/.env").read_text().splitlines() if "=" in l and not l.startswith("#"))
verify = env.get("TLS_VERIFY", "true") != "false"
host = env["OP_HOST"]
docs, git, op_url = f"https://docs.{host}", f"https://git.{host}", f"https://{host}"


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def client(redirects=True):
    return httpx.Client(verify=verify, timeout=30, follow_redirects=redirects)


def kc_form(r):
    return re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', r.text) or re.search(r'action="([^"]+)"[^>]*id="kc-form-login"', r.text)


def keycloak_login(c, start, user, pw):
    r = c.get(start)
    m = kc_form(r)
    if not m:
        return r, False
    return c.post(html.unescape(m.group(1)), data={"username": user, "password": pw, "credentialId": ""}), True


def op_login(user, pw):
    c = client(False)
    page = c.get(f"{op_url}/login")
    form = page.text[page.text.index('data-test-selector="user-login--form"'):]  # per-form CSRF tokens: use the main form's own
    tok = re.search(r'name="authenticity_token" value="([^"]+)"', form).group(1)
    r = c.post(f"{op_url}/login", data={"authenticity_token": tok, "username": user, "password": pw})
    return c, r


owner, owner_pw = env["LDAP_FIRST_USER"], env["LDAP_FIRST_PASSWORD"]
plain, plain_pw = f"jan{secrets.token_hex(2)}", secrets.token_urlsafe(12)
out = subprocess.run(["python3", "deploy/tenantctl.py", "ldap-user", tenant, "--username", plain, "--email", f"{plain}@example.com", "--password", plain_pw], capture_output=True, text=True)
check(out.returncode == 0, f"directory user {plain} created ({out.stdout.strip()[:40]}{out.stderr.strip()[:200]})")

check(client().get(f"https://users.{host}/").status_code == 200, "directory web UI reachable at users.<host>")

# --- OpenProject: LDAP login, account created on first sign-in
opapi = httpx.Client(base_url=f"{op_url}/api/v3", auth=("apikey", env["OP_API_KEY"]), verify=verify, timeout=30)
c, r = op_login(plain, plain_pw)
check(r.status_code in (302, 303) and "/login" not in r.headers.get("location", ""), f"OpenProject accepts the directory password ({r.status_code})")
users = opapi.get("/users", params={"filters": '[{"login":{"operator":"=","values":["%s"]}}]' % plain}).json()["_embedded"]["elements"]
check(len(users) == 1 and users[0]["email"] == f"{plain}@example.com", "OpenProject created the account on first sign-in (name and e-mail from the directory)")
c2, r2 = op_login(plain, "wrong-" + plain_pw)
check(r2.status_code in (200, 422) or "/login" in r2.headers.get("location", ""), f"OpenProject rejects a wrong directory password ({r2.status_code})")

# --- Keycloak federation: Git and docs
u = client()
r, formed = keycloak_login(u, f"{git}/user/oauth2/keycloak", plain, plain_pw)
check(formed and str(r.url).startswith(git) and "kc-form-login" not in r.text, "the same account signs in to Git through Keycloak (federated directory)")
gt = httpx.Client(base_url=f"{git}/api/v1", headers={"Authorization": f"token {env['GITEA_TOKEN']}"}, verify=verify, timeout=30)
check(gt.get(f"/users/{plain}").status_code == 200, f"Gitea account {plain} created from the directory login")
bad = client()
rb = bad.get(f"{git}/user/oauth2/keycloak")
rb = bad.post(html.unescape(kc_form(rb).group(1)), data={"username": plain, "password": "nope", "credentialId": ""})
check(kc_form(rb) is not None and str(rb.url).startswith(f"https://auth.{host}"), "Keycloak rejects a wrong directory password")

keycloak_login(u, f"{docs}/auth/login?next=%2F", plain, plain_pw)
check(u.get(f"{docs}/stale", params={"project": "api-demo", "days": 0, "format": "json"}).status_code == 403, "a directory user without the group 'admin' is refused on the admin report")

# --- the first user is in the group 'admin': realm role arrives through the federation
a = client()
keycloak_login(a, f"{docs}/auth/login?next=%2F", owner, owner_pw)
r = a.get(f"{docs}/stale", params={"project": "api-demo", "days": 0, "format": "json"})
check(r.status_code == 200 and "items" in r.text, f"directory group 'admin' became the Keycloak role 'admin' ({owner} may use the admin report)")

# one directory, one password: same first user in OpenProject
c3, r3 = op_login(owner, owner_pw)
check(r3.status_code in (302, 303) and "/login" not in r3.headers.get("location", ""), f"{owner} signs in to OpenProject with the same password")
print("\nALL PASSED")
