"""Smoke test for the weekly digest on a tenant: configures a webhook pointing at a local receiver, triggers the digest
as an administrator and checks what arrives. Usage: python scripts/digest_smoke.py acme   (project api-demo must exist)"""
import html
import json
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
env_path = Path(f"tenants/{tenant}/.env")
env = dict(l.split("=", 1) for l in env_path.read_text().splitlines() if "=" in l and not l.startswith("#"))
verify = env.get("TLS_VERIFY", "true") != "false"
host = env["OP_HOST"]
docs = f"https://docs.{host}"
sso = env.get("SSO_ENABLED") == "1"
received = []


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


server = HTTPServer(("0.0.0.0", 9099), Receiver)
threading.Thread(target=server.serve_forever, daemon=True).start()
tenantctl = ["python3", "deploy/tenantctl.py", "digest", tenant]
kc_user = None
kc = httpx.Client(verify=verify, timeout=30, follow_redirects=True)
try:
    subprocess.run(tenantctl + ["--projects", "api-demo", "--days", "0", "--webhook-url", "http://host.docker.internal:9099/hook"], check=True, capture_output=True)
    import time
    time.sleep(4)
    c = httpx.Client(verify=verify, timeout=30, follow_redirects=True)
    auth = None
    if sso:  # sign in as a temporary Keycloak administrator
        tok = kc.post(f"https://auth.{host}/realms/master/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": "admin-cli", "username": "kcadmin", "password": env["KC_ADMIN_PASSWORD"]}).json()["access_token"]
        H = {"Authorization": f"Bearer {tok}"}
        name, pw = f"digest-admin-{secrets.token_hex(3)}", secrets.token_urlsafe(14)
        r = kc.post(f"https://auth.{host}/admin/realms/panoptes/users", headers=H, json={"username": name, "email": f"{name}@example.com", "emailVerified": True,
                    "enabled": True, "firstName": "D", "lastName": "Admin", "credentials": [{"type": "password", "value": pw, "temporary": False}]})
        kc_user = r.headers["location"].rsplit("/", 1)[1]
        role = kc.get(f"https://auth.{host}/admin/realms/panoptes/roles/admin", headers=H).json()
        kc.post(f"https://auth.{host}/admin/realms/panoptes/users/{kc_user}/role-mappings/realm", headers=H, json=[role])
        r = c.get(f"{docs}/auth/login?next=%2F")
        form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', r.text) or re.search(r'action="([^"]+)"[^>]*id="kc-form-login"', r.text)
        c.post(html.unescape(form.group(1)), data={"username": name, "password": pw, "credentialId": ""})
    else:
        auth = ("docs", env["DOCS_PASSWORD"])

    r = c.get(f"{docs}/digest/preview", auth=auth)
    check(r.status_code == 200 and r.json()["configured"] and "Stale work packages in api-demo" in r.json()["markdown"], "preview shows the stale report and the digest is configured")
    r = c.post(f"{docs}/digest/send", auth=auth)
    check(r.status_code == 200 and r.json()["sent_via"] == ["webhook"], f"digest sent via the webhook ({r.text[:80]})")
    check(len(received) == 1 and "<https://" in received[0]["text"] and "/work_packages/" in received[0]["text"], "the chat receiver got a Slack-formatted digest with links")
    print("\nALL PASSED")
finally:
    subprocess.run(tenantctl + ["--off"], capture_output=True)
    if kc_user:
        kc.delete(f"https://auth.{host}/admin/realms/panoptes/users/{kc_user}", headers=H)
    server.shutdown()
