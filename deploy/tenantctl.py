#!/usr/bin/env python3
"""Manage Panoptes tenants: one isolated stack (OpenProject + Gitea + glue) per customer behind a shared Caddy proxy.

  tenantctl.py create acme [--base-domain panoptes.example.com] [--admin-email admin@acme.com]
  tenantctl.py enable-sso acme [--admin-email admin@acme.com]   (bundled Keycloak: sign in once for OpenProject, Git and docs)
  tenantctl.py digest acme --projects proj1,proj2 --webhook-url https://hooks.slack.com/...   (weekly stale-ticket digest)
  tenantctl.py list
  tenantctl.py backup acme [--out backups]      (consistent: the stack is stopped for ~1 min)
  tenantctl.py restore acme backups/acme-<timestamp>.tar --yes
  tenantctl.py destroy acme --yes        (deletes containers AND data volumes)

Local trial: the default base domain is 'localhost' (acme.localhost, git.acme.localhost, docs.acme.localhost, local TLS).
Production / self-hosting: use a real base domain with wildcard DNS (*.base) pointing at the server; Caddy gets Let's Encrypt certificates.
"""
import argparse
import json
import re
import secrets
import shutil
import tarfile
import tempfile
import datetime
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


def tenant_env(tenant: str) -> dict:
    return read_env(TENANTS / tenant / ".env")


def compose(tenant: str, *args, **kw) -> str:
    profile = ["--profile", "sso"] if tenant_env(tenant).get("SSO_ENABLED") == "1" else []
    return sh("docker", "compose", "-f", str(TENANT_COMPOSE), "--env-file", str(TENANTS / tenant / ".env"), *profile, *args, **kw)


def ensure_ca_bundle(tdir: Path, local: bool) -> None:
    """CA bundle mounted into the services: system roots, plus the proxy's local CA for the *.localhost trial."""
    bundle = sh("docker", "run", "--rm", "caddy:2", "cat", "/etc/ssl/certs/ca-certificates.crt")
    if local:
        for _ in range(30):  # Caddy creates its local CA when it first serves a `tls internal` site
            root = sh("docker", "compose", "-f", str(PROXY_DIR / "docker-compose.yml"), "exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt", check=False)
            if "BEGIN CERTIFICATE" in root:
                bundle += "\n" + root
                break
            time.sleep(1)
        else:
            sys.exit("Caddy's local CA was not created; did the proxy start?")
    (tdir / "ca-bundle.crt").write_text(bundle + "\n")


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


def caddy_snippet(tenant: str, host: str, docs_hash: str, sso: bool = False) -> str:
    local = host.endswith(".localhost") or host == "localhost"
    tls = "\ttls internal\n" if local else ""
    docs_auth = "\t# SSO: the glue itself requires a Keycloak sign-in and checks repository access per user\n" if sso else f"""\t# the docs portal reads every repo with the service token, so it must not be public.
\t# Interim protection until SSO is enabled (tenantctl enable-sso).
\tbasic_auth {{
\t\tdocs {docs_hash}
\t}}
"""
    auth_site = f"""
auth.{host} {{
{tls}\treverse_proxy {tenant}-kc:8080
}}
""" if sso else ""
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
{docs_auth}\treverse_proxy {tenant}-glue:8090
}}
{auth_site}"""


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
    (tdir / "keycloak").mkdir()
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
    ensure_ca_bundle(tdir, a.base_domain.endswith("localhost"))

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


def realm_json(host: str, secrets_: dict, admin_email: str, admin_pw: str) -> dict:
    """Keycloak realm 'panoptes' with one client per app and a first (temporary-password) administrator."""
    def client(cid, secret, redirects, extra=None):
        return {"clientId": cid, "secret": secret, "enabled": True, "protocol": "openid-connect", "publicClient": False,
                "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "redirectUris": redirects,
                "webOrigins": ["+"], "attributes": {"post.logout.redirect.uris": "+"}, **(extra or {})}
    roles_mapper = {"name": "realm roles", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-realm-role-mapper",
                    "config": {"claim.name": "roles", "multivalued": "true", "jsonType.label": "String",
                               "id.token.claim": "true", "access.token.claim": "false", "userinfo.token.claim": "true"}}
    return {
        "realm": "panoptes", "enabled": True, "displayName": "Panoptes", "sslRequired": "external",
        "registrationAllowed": False, "loginWithEmailAllowed": True, "duplicateEmailsAllowed": False, "resetPasswordAllowed": True,
        "roles": {"realm": [{"name": "admin", "description": "Panoptes administrators (may use the stale-ticket report)"}]},
        "clients": [
            client("panoptes-op", secrets_["op"], [f"https://{host}/*"]),
            client("panoptes-gitea", secrets_["gitea"], [f"https://git.{host}/*"]),
            client("panoptes-docs", secrets_["docs"], [f"https://docs.{host}/*"], {"protocolMappers": [roles_mapper]}),
        ],
        "users": [{"username": "admin", "email": admin_email, "emailVerified": True, "enabled": True, "firstName": "Panoptes", "lastName": "Admin",
                   "realmRoles": ["admin"], "credentials": [{"type": "password", "value": admin_pw, "temporary": True}]}],
    }


OP_SSO = """
iss = ENV['ISSUER']
cfg = {
  display_name: 'Panoptes SSO', oidc_provider: 'custom', limit_self_registration: false,
  issuer: iss, client_id: 'panoptes-op', client_secret: ENV['OP_SECRET'],
  authorization_endpoint: iss + '/protocol/openid-connect/auth',
  token_endpoint: iss + '/protocol/openid-connect/token',
  userinfo_endpoint: iss + '/protocol/openid-connect/userinfo',
  end_session_endpoint: iss + '/protocol/openid-connect/logout',
  jwks_uri: iss + '/protocol/openid-connect/certs',
  scope: 'openid email profile',
  mapping_login: 'preferred_username', mapping_email: 'email', mapping_first_name: 'given_name', mapping_last_name: 'family_name'
}
call = OpenIDConnect::SyncService.new('keycloak', cfg.merge(name: 'keycloak')).call
OpenIDConnect::Provider.find_by(slug: 'keycloak')&.update_columns(available: true)
puts call.success ? 'OPSSO=ok' : 'OPSSO=failed ' + call.message.to_s
"""


def enable_sso(a) -> None:
    """Add a bundled Keycloak and wire OpenProject, Gitea and the docs portal to it."""
    t = a.name
    tdir = TENANTS / t
    env = tdir / ".env"
    if not env.exists():
        sys.exit(f"unknown tenant {t}")
    e = read_env(env)
    host = e["OP_HOST"]
    local = host.endswith("localhost")
    iss = f"https://auth.{host}/realms/panoptes"
    admin_email = a.admin_email or e.get("ADMIN_EMAIL", "admin@example.com")
    sec = {k: secrets.token_urlsafe(24) for k in ("op", "gitea", "docs")}
    admin_pw = secrets.token_urlsafe(12)
    if e.get("SSO_ENABLED") == "1" and not a.rotate:
        sys.exit("SSO is already enabled for this tenant (use --rotate to regenerate the client secrets)")

    print("• generating the Keycloak realm and secrets")
    (tdir / "keycloak").mkdir(exist_ok=True)
    (tdir / "keycloak" / "panoptes-realm.json").write_text(json.dumps(realm_json(host, sec, admin_email, admin_pw), indent=1))
    (tdir / "keycloak" / "panoptes-realm.json").chmod(0o600)
    for k, v in {"SSO_ENABLED": "1", "OIDC_ISSUER": iss, "OIDC_CLIENT_ID": "panoptes-docs", "OIDC_CLIENT_SECRET": sec["docs"],
                 "SESSION_SECRET": secrets.token_hex(32), "KC_ADMIN_PASSWORD": secrets.token_urlsafe(16), "SSO_ADMIN_PASSWORD": admin_pw,
                 "SSO_SECRET_OP": sec["op"], "SSO_SECRET_GITEA": sec["gitea"], "ADMIN_EMAIL": admin_email}.items():
        set_env(env, k, v)

    print("• routing auth.<host> and switching the docs portal from basic auth to SSO")
    route = PROXY_DIR / "tenants" / f"{t}.caddy"
    route.write_text(caddy_snippet(t, host, "", sso=True))
    reload_proxy()
    ensure_ca_bundle(tdir, local)

    print("• starting Keycloak (first start imports the realm, ~1 min)")
    compose(t, "up", "-d", "--build")
    wait_ready(f"{iss}/.well-known/openid-configuration", f"Keycloak of {t}", timeout=300)

    print("• configuring Gitea")
    listing = compose(t, "exec", "-T", "-u", "git", "gitea", "gitea", "admin", "auth", "list")
    existing = next((l.split()[0] for l in listing.splitlines()[1:] if "keycloak" in l), None)
    base = ["exec", "-T", "-u", "git", "gitea", "gitea", "admin", "auth"]
    args = ["--name", "keycloak", "--provider", "openidConnect", "--key", "panoptes-gitea", "--secret", sec["gitea"],
            "--auto-discover-url", f"{iss}/.well-known/openid-configuration", "--scopes", "openid", "--scopes", "email", "--scopes", "profile"]
    compose(t, *base, *(["update-oauth", "--id", existing] if existing else ["add-oauth"]), *args)

    print("• configuring OpenProject")
    rb = tdir / "sso.rb"
    rb.write_text(OP_SSO)
    compose(t, "cp", str(rb), "openproject:/tmp/sso.rb")
    rb.unlink()
    out = compose(t, "exec", "-T", "-e", f"ISSUER={iss}", "-e", f"OP_SECRET={sec['op']}", "openproject", "bash", "-c",
                  "cd /app && RAILS_ENV=production bundle exec rails runner /tmp/sso.rb && rm /tmp/sso.rb")
    if "OPSSO=ok" not in out:
        sys.exit("OpenProject SSO configuration failed:\n" + out[-600:])

    print(f"""
SSO is enabled for '{t}' (Git and the docs portal).
  Identity provider  https://auth.{host}   (realm 'panoptes'; Keycloak admin console login: kcadmin, password in {env})
  First SSO user     admin / SSO_ADMIN_PASSWORD from {env} (Keycloak asks to change it at first sign-in)
  Works for          https://git.{host} (Git: account is created on first sign-in) and https://docs.{host} (docs portal)
  NOT for OpenProject: its Community Edition has no OIDC/SAML login (an Enterprise feature). OpenProject keeps its
  own logins. The OpenProject provider is already configured, so it starts working once you add an Enterprise token
  (OpenProject > Administration > Enterprise edition). The local admin accounts keep working as break-glass logins.
""")


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def digest(a) -> None:
    """Configure (or switch off) the weekly stale-ticket digest of a tenant's glue."""
    t = a.name
    env = TENANTS / t / ".env"
    if not env.exists():
        sys.exit(f"unknown tenant {t}")
    keys = ("DIGEST_PROJECTS", "DIGEST_DAYS", "DIGEST_WEEKDAY", "DIGEST_HOUR", "DIGEST_TZ", "DIGEST_WEBHOOK_URL", "DIGEST_WEBHOOK_FORMAT",
            "DIGEST_SMTP_HOST", "DIGEST_SMTP_PORT", "DIGEST_SMTP_USER", "DIGEST_SMTP_PASSWORD", "DIGEST_MAIL_FROM", "DIGEST_MAIL_TO")
    if a.off:
        for k in keys:
            set_env(env, k, "")
        compose(t, "up", "-d", "--build", "glue")
        print("weekly digest switched off")
        return
    if not a.projects or not (a.webhook_url or (a.smtp_host and a.to)):
        sys.exit("need --projects and a channel: --webhook-url, or --smtp-host with --to")
    values = {"DIGEST_PROJECTS": a.projects, "DIGEST_DAYS": str(a.days), "DIGEST_WEEKDAY": str(WEEKDAYS.index(a.weekday)), "DIGEST_HOUR": str(a.hour),
              "DIGEST_TZ": a.tz, "DIGEST_WEBHOOK_URL": a.webhook_url or "", "DIGEST_WEBHOOK_FORMAT": a.webhook_format,
              "DIGEST_SMTP_HOST": a.smtp_host or "", "DIGEST_SMTP_PORT": str(a.smtp_port), "DIGEST_SMTP_USER": a.smtp_user or "",
              "DIGEST_SMTP_PASSWORD": a.smtp_password or "", "DIGEST_MAIL_FROM": a.mail_from or "", "DIGEST_MAIL_TO": a.to or ""}
    for k, v in values.items():
        set_env(env, k, v)
    compose(t, "up", "-d", "--build", "glue")
    print(f"weekly digest configured for {t}: every {a.weekday} from {a.hour}:00 ({a.tz}), projects {a.projects}, stale after {a.days} days.")
    print("Try it now: POST https://docs.<host>/digest/send (administrators), preview with GET /digest/preview.")


def list_tenants(_) -> None:
    if not TENANTS.exists() or not any(TENANTS.iterdir()):
        print("no tenants")
        return
    for d in sorted(TENANTS.iterdir()):
        if (d / ".env").exists():
            e = read_env(d / ".env")
            running = sh("docker", "compose", "-f", str(TENANT_COMPOSE), "--env-file", str(d / ".env"), "ps", "-q", check=False)
            print(f"{d.name:20} https://{e['OP_HOST']:40} {'running' if running else 'stopped'}")


def volumes_for(t: str) -> tuple:
    return ("op_pgdata", "op_assets", "gitea_data") + (("kc_data",) if tenant_env(t).get("SSO_ENABLED") == "1" else ())


def volume(t: str, name: str) -> str:
    return f"panoptes-{t}_{name}"


def backup(a) -> None:
    """One tar with: the three data volumes, the tenant .env (secrets!), the import state and the proxy route."""
    t = a.name
    env = TENANTS / t / ".env"
    if not env.exists():
        sys.exit(f"unknown tenant {t}")
    out_dir = Path(a.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = out_dir / f"{t}-{stamp}.tar"
    print("• stopping the stack for a consistent snapshot")
    compose(t, "stop")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for v in volumes_for(t):
                print(f"• archiving volume {v}")
                sh("docker", "run", "--rm", "-v", f"{volume(t, v)}:/data:ro", "-v", f"{tmp}:/backup", "alpine", "tar", "czf", f"/backup/{v}.tgz", "-C", "/data", ".")
            with tarfile.open(archive, "w") as tar:
                for v in volumes_for(t):
                    tar.add(Path(tmp) / f"{v}.tgz", arcname=f"{v}.tgz")
                tar.add(TENANTS / t / "keycloak", arcname="keycloak")
                tar.add(env, arcname="env")
                tar.add(TENANTS / t / "import-state", arcname="import-state")
                route = PROXY_DIR / "tenants" / f"{t}.caddy"
                if route.exists():
                    tar.add(route, arcname="route.caddy")
    finally:
        compose(t, "start")
    archive.chmod(0o600)  # contains the tenant secrets
    print(f"backup written: {archive} ({archive.stat().st_size // 1024 // 1024} MB). Keep it somewhere safe: it contains the tenant secrets.")


def restore(a) -> None:
    """Restore into the same tenant name (replaces everything currently stored for it)."""
    t = a.name
    if not a.yes:
        sys.exit("restore REPLACES all data of this tenant with the backup; repeat with --yes")
    archive = Path(a.archive).resolve()
    if not archive.exists():
        sys.exit(f"{archive} not found")
    tdir = TENANTS / t
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        tmp = Path(tmp)
        tdir.mkdir(parents=True, exist_ok=True)
        if (tdir / ".env").exists():
            compose(t, "down", "-v")
        shutil.copy(tmp / "env", tdir / ".env")
        (tdir / ".env").chmod(0o600)
        set_env(tdir / ".env", "TENANT_DIR", str(tdir))  # the backup may come from another machine/path
        shutil.rmtree(tdir / "import-state", ignore_errors=True)
        shutil.copytree(tmp / "import-state", tdir / "import-state")
        shutil.rmtree(tdir / "keycloak", ignore_errors=True)
        if (tmp / "keycloak").exists():
            shutil.copytree(tmp / "keycloak", tdir / "keycloak")
        else:
            (tdir / "keycloak").mkdir()
        for v in volumes_for(t):
            print(f"• restoring volume {v}")
            sh("docker", "volume", "create", volume(t, v))
            sh("docker", "run", "--rm", "-v", f"{volume(t, v)}:/data", "-v", f"{tmp}:/backup:ro", "alpine", "sh", "-c",
               f"rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /backup/{v}.tgz -C /data")
        if (tmp / "route.caddy").exists():
            shutil.copy(tmp / "route.caddy", PROXY_DIR / "tenants" / f"{t}.caddy")
    sh("docker", "network", "create", "panoptes-proxy", check=False)
    reload_proxy()
    e = read_env(tdir / ".env")
    ensure_ca_bundle(tdir, e["OP_HOST"].endswith("localhost"))
    compose(t, "up", "-d")
    wait_ready(f"https://{e['OP_HOST']}/health_checks/default", f"OpenProject of {t}")
    print(f"tenant {t} restored from {archive.name}")


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
    g = sub.add_parser("digest")
    g.add_argument("name")
    g.add_argument("--projects", help="comma-separated OpenProject project identifiers")
    g.add_argument("--days", type=int, default=30, help="stale after this many days without any update")
    g.add_argument("--weekday", choices=WEEKDAYS, default="mon")
    g.add_argument("--hour", type=int, default=8)
    g.add_argument("--tz", default="UTC", help="IANA time zone, e.g. Europe/Amsterdam")
    g.add_argument("--webhook-url", help="Slack-compatible incoming webhook (Slack, Mattermost, Rocket.Chat)")
    g.add_argument("--webhook-format", choices=["slack", "markdown"], default="slack")
    g.add_argument("--smtp-host")
    g.add_argument("--smtp-port", type=int, default=587)
    g.add_argument("--smtp-user")
    g.add_argument("--smtp-password")
    g.add_argument("--mail-from")
    g.add_argument("--to", help="comma-separated recipients")
    g.add_argument("--off", action="store_true")
    g.set_defaults(fn=digest)
    sso = sub.add_parser("enable-sso")
    sso.add_argument("name")
    sso.add_argument("--admin-email")
    sso.add_argument("--rotate", action="store_true")
    sso.set_defaults(fn=enable_sso)
    sub.add_parser("list").set_defaults(fn=list_tenants)
    b = sub.add_parser("backup")
    b.add_argument("name")
    b.add_argument("--out", default="backups")
    b.set_defaults(fn=backup)
    r = sub.add_parser("restore")
    r.add_argument("name")
    r.add_argument("archive")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=restore)
    d = sub.add_parser("destroy")
    d.add_argument("name")
    d.add_argument("--yes", action="store_true")
    d.set_defaults(fn=destroy)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
