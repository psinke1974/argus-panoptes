#!/usr/bin/env python3
"""Manage Panoptes tenants: one isolated stack (OpenProject + Gitea + glue) per customer behind a shared Caddy proxy.

  tenantctl.py create acme [--base-domain panoptes.example.com] [--admin-email admin@acme.com]
  tenantctl.py list
  tenantctl.py destroy acme --yes        (deletes containers AND data volumes)

Local trial: the default base domain is 'localhost' (acme.localhost, git.acme.localhost, docs.acme.localhost, local TLS).
Production / self-hosting: use a real base domain with wildcard DNS (*.base) pointing at the server; Caddy gets Let's Encrypt certificates.
"""
import argparse
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = ROOT / "deploy" / "proxy"
TENANT_COMPOSE = ROOT / "deploy" / "tenant" / "docker-compose.yml"
TENANTS = ROOT / "tenants"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

OP_BOOTSTRAP = """
u = User.admin.first
u.login = 'admin'
u.password = ENV['NEWPW']; u.password_confirmation = ENV['NEWPW']; u.force_password_change = false
u.mail = ENV['ADMIN_MAIL'] unless ENV['ADMIN_MAIL'].to_s.empty?
u.save!(validate: false)
Token::API.where(user: u).destroy_all
puts 'APIKEY=' + Token::API.create!(user: u).plain_value
"""


def sh(*cmd, check=True, env=None, input=None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, input=input, cwd=ROOT)
    if check and r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r.stdout.strip()


def read_env(path: Path) -> dict:
    return dict(l.split("=", 1) for l in path.read_text().splitlines() if "=" in l and not l.startswith("#"))


def set_env(path: Path, key: str, value: str) -> None:
    lines = [l for l in path.read_text().splitlines() if not l.startswith(key + "=")]
    path.write_text("\n".join(lines + [f"{key}={value}"]) + "\n")


def compose(tenant: str, *args, **kw) -> str:
    return sh("docker", "compose", "-f", str(TENANT_COMPOSE), "--env-file", str(TENANTS / tenant / ".env"), *args, **kw)


def wait_ready(url: str, host_hint: str, timeout: int = 420) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # local CA / not-yet-issued certificates
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError, ssl.SSLError):
            pass
        time.sleep(5)
    sys.exit(f"{host_hint} did not become ready within {timeout}s (check: docker compose logs)")


def caddy_snippet(tenant: str, host: str, docs_hash: str) -> str:
    local = host.endswith(".localhost") or host == "localhost"
    tls = "\ttls internal\n" if local else ""
    return f"""# Panoptes tenant {tenant}
{host} {{
{tls}\treverse_proxy {tenant}-op:80
}}

git.{host} {{
{tls}\treverse_proxy {tenant}-git:3000
}}

docs.{host} {{
{tls}\t# webhooks are internal only (Gitea -> glue over the tenant network)
\t@hooks path /hooks/*
\trespond @hooks 404
\t# the docs portal reads every repo with the service token, so it must not be public.
\t# Interim protection until SSO is in place.
\tbasic_auth {{
\t\tdocs {docs_hash}
\t}}
\treverse_proxy {tenant}-glue:8090
}}
"""


def reload_proxy() -> None:
    sh("docker", "compose", "-f", str(PROXY_DIR / "docker-compose.yml"), "up", "-d")
    sh("docker", "compose", "-f", str(PROXY_DIR / "docker-compose.yml"), "exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", check=False)


def create(a) -> None:
    t = a.name
    if not NAME_RE.match(t):
        sys.exit("tenant name: lowercase letters, digits and '-', 2-31 chars, starting with a letter")
    tdir = TENANTS / t
    if tdir.exists():
        sys.exit(f"tenant {t} already exists ({tdir})")
    host = f"{t}.{a.base_domain}"
    op_pw, docs_pw = secrets.token_hex(12), secrets.token_urlsafe(12)
    gitea_pw = secrets.token_hex(12)
    tdir.mkdir(parents=True)
    (tdir / "import-state").mkdir()
    env = tdir / ".env"
    env.write_text("\n".join([
        f"TENANT={t}", f"TENANT_DIR={tdir}", f"OP_HOST={host}", f"LANGUAGE={a.language}",
        f"OP_SECRET_KEY_BASE={secrets.token_hex(64)}", f"WEBHOOK_SECRET={secrets.token_hex(24)}",
        f"STATUS_CLOSED={'Closed' if a.language == 'en' else 'Gesloten'}",
        f"STATUS_IN_PROGRESS={'In progress' if a.language == 'en' else 'In behandeling'}",
        f"GITEA_ADMIN_PASSWORD={gitea_pw}", f"OP_ADMIN_PASSWORD={op_pw}", f"DOCS_PASSWORD={docs_pw}",
        f"OP_PUBLIC_URL_LOCAL=https://{host}", f"GITEA_URL_LOCAL=https://git.{host}", "TLS_VERIFY=" + ("false" if a.base_domain.endswith("localhost") else "true"), ""]))
    env.chmod(0o600)

    print("• shared proxy network + Caddy")
    sh("docker", "network", "create", "panoptes-proxy", check=False)
    docs_hash = sh("docker", "run", "--rm", "caddy:2", "caddy", "hash-password", "--plaintext", docs_pw)
    (PROXY_DIR / "tenants" / f"{t}.caddy").write_text(caddy_snippet(t, host, docs_hash))
    reload_proxy()

    print("• starting the tenant stack (first start pulls images and migrates the database, ~2-5 min)")
    compose(t, "up", "-d", "--build")
    wait_ready(f"https://{host}/health_checks/default", f"OpenProject of {t}")

    print("• bootstrapping Gitea and OpenProject")
    sh("docker", "compose", "-f", str(TENANT_COMPOSE), "--env-file", str(env), "exec", "-T", "-u", "git", "gitea", "gitea", "admin", "user", "create",
       "--admin", "--username", "panoptes", "--password", gitea_pw, "--email", a.admin_email, "--must-change-password=false")
    token = compose(t, "exec", "-T", "-u", "git", "gitea", "gitea", "admin", "user", "generate-access-token", "--username", "panoptes", "--token-name", "glue", "--scopes", "all", "--raw").splitlines()[-1]
    set_env(env, "GITEA_TOKEN", token)
    # pipe the script through a temp file inside the container (rails runner does not read stdin)
    local_rb = tdir / "bootstrap.rb"
    local_rb.write_text(OP_BOOTSTRAP)
    compose(t, "cp", str(local_rb), "openproject:/tmp/bootstrap.rb")
    local_rb.unlink()
    out = compose(t, "exec", "-T", "-e", f"NEWPW={op_pw}", "-e", f"ADMIN_MAIL={a.admin_email}", "openproject", "bash", "-c",
                  "cd /app && RAILS_ENV=production bundle exec rails runner /tmp/bootstrap.rb && rm /tmp/bootstrap.rb")
    key = re.search(r"APIKEY=(\S+)", out)
    if not key:
        sys.exit("could not create the OpenProject API key:\n" + out[-500:])
    set_env(env, "OP_API_KEY", key.group(1))
    compose(t, "up", "-d", "--force-recreate", "glue")

    print(f"""
Tenant '{t}' is ready.
  OpenProject   https://{host}                (login: admin)
  Git           https://git.{host}            (login: panoptes)
  Docs portal   https://docs.{host}           (login: docs)
  Credentials   {env}   (chmod 600; change the admin passwords on first login)
""")


def list_tenants(_) -> None:
    if not TENANTS.exists() or not any(TENANTS.iterdir()):
        print("no tenants")
        return
    for d in sorted(TENANTS.iterdir()):
        if (d / ".env").exists():
            e = read_env(d / ".env")
            running = sh("docker", "compose", "-f", str(TENANT_COMPOSE), "--env-file", str(d / ".env"), "ps", "-q", check=False)
            print(f"{d.name:20} https://{e['OP_HOST']:40} {'running' if running else 'stopped'}")


def destroy(a) -> None:
    t = a.name
    if not a.yes:
        sys.exit("destroy removes the containers AND all data volumes of this tenant; repeat with --yes")
    if not (TENANTS / t / ".env").exists():
        sys.exit(f"unknown tenant {t}")
    compose(t, "down", "-v")
    (PROXY_DIR / "tenants" / f"{t}.caddy").unlink(missing_ok=True)
    reload_proxy()
    shutil.rmtree(TENANTS / t)
    print(f"tenant {t} removed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--base-domain", default="localhost")
    c.add_argument("--admin-email", default="admin@example.com")
    c.add_argument("--language", default="en", choices=["en", "nl"])
    c.set_defaults(fn=create)
    sub.add_parser("list").set_defaults(fn=list_tenants)
    d = sub.add_parser("destroy")
    d.add_argument("name")
    d.add_argument("--yes", action="store_true")
    d.set_defaults(fn=destroy)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
