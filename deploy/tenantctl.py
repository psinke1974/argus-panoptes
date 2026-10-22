#!/usr/bin/env python3
"""Manage Panoptes tenants: one isolated stack (OpenProject + Gitea + glue) per customer behind a shared Caddy proxy.

  tenantctl.py create acme [--base-domain panoptes.example.com] [--admin-email admin@acme.com]
  tenantctl.py enable-sso acme [--admin-email admin@acme.com]   (bundled Keycloak: sign in once for OpenProject, Git and docs)
  tenantctl.py enable-ldap acme [--admin-user owner] [--admin-email ...]   (one account for OpenProject, Git and docs)
  tenantctl.py ldap-user acme --username jan --email jan@acme.com [--admin]
  tenantctl.py digest acme --projects proj1,proj2 --webhook-url https://hooks.slack.com/...   (weekly stale-ticket digest)
  tenantctl.py offsite configure --repository s3:https://s3.example.eu/bucket/panoptes --generate-password   (encrypted off-site backups, restic)
  tenantctl.py offsite backup acme|--all     /  offsite list acme  /  offsite verify acme  /  offsite restore acme --yes
  tenantctl.py list
  tenantctl.py backup acme [--out backups]      (consistent: the stack is stopped for ~1 min)
  tenantctl.py restore acme backups/acme-<timestamp>.tar --yes
  tenantctl.py destroy acme --yes        (deletes containers AND data volumes)

Local trial: the default base domain is 'localhost' (acme.localhost, git.acme.localhost, docs.acme.localhost, local TLS).
Production / self-hosting: use a real base domain with wildcard DNS (*.base) pointing at the server; Caddy gets Let's Encrypt certificates.
"""
import argparse
import json
import os
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
import urllib.parse
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
    e = tenant_env(tenant)
    profile = [x for flag, name in (("SSO_ENABLED", "sso"), ("LDAP_ENABLED", "ldap")) if e.get(flag) == "1" for x in ("--profile", name)]
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


def caddy_snippet(tenant: str, host: str, docs_hash: str, sso: bool = False, ldap: bool = False) -> str:
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
    users_site = f"""
users.{host} {{
{tls}\treverse_proxy {tenant}-ldap:17170
}}
""" if ldap else ""
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
{auth_site}{users_site}"""


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
       "--admin", "--username", "panoptes", "--password", gitea_pw, "--email", plus_address(a.admin_email, "git"), "--must-change-password=false")
    token = compose(t, "exec", "-T", "-u", "git", "gitea", "gitea", "admin", "user", "generate-access-token", "--username", "panoptes", "--token-name", "glue", "--scopes", "all", "--raw").splitlines()[-1]
    set_env(env, "GITEA_TOKEN", token)
    # pipe the script through a temp file inside the container (rails runner does not read stdin)
    local_rb = tdir / "bootstrap.rb"
    local_rb.write_text(OP_BOOTSTRAP)
    compose(t, "cp", str(local_rb), "openproject:/tmp/bootstrap.rb")
    local_rb.unlink()
    out = compose(t, "exec", "-T", "-e", f"NEWPW={op_pw}", "-e", f"ADMIN_MAIL={plus_address(a.admin_email, 'op')}", "openproject", "bash", "-c",
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


LDAP_BASE_DN = "dc=panoptes,dc=local"
LDAP_PEOPLE = f"ou=people,{LDAP_BASE_DN}"
LDAP_GROUPS = f"ou=groups,{LDAP_BASE_DN}"
LDAP_BIND_USER = "panoptes-bind"

OP_LDAP = """
src = LdapAuthSource.find_or_initialize_by(name: 'Panoptes directory')
src.assign_attributes(
  host: 'lldap', port: 3890, tls_mode: 'plain_ldap',
  account: ENV['BIND_DN'], account_password: ENV['BIND_PW'], base_dn: ENV['PEOPLE_DN'],
  attr_login: 'uid', attr_firstname: 'givenName', attr_lastname: 'sn', attr_mail: 'mail',
  onthefly_register: true, filter_string: nil
)
src.save!
puts 'OPLDAP=ok'
"""

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
    route.write_text(caddy_snippet(t, host, "", sso=True, ldap=e.get("LDAP_ENABLED") == "1"))
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
        wait_ready(f"https://docs.{tenant_env(t)['OP_HOST']}/health", "the glue", timeout=90)
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
    wait_ready(f"https://docs.{tenant_env(t)['OP_HOST']}/health", "the glue", timeout=90)
    print(f"weekly digest configured for {t}: every {a.weekday} from {a.hour}:00 ({a.tz}), projects {a.projects}, stale after {a.days} days.")
    print("Try it now: POST https://docs.<host>/digest/send (administrators), preview with GET /digest/preview.")


def plus_address(email: str, tag: str) -> str:
    """info@x.nl -> info+git@x.nl: same mailbox, unique address (local break-glass accounts must not share the owner's e-mail)."""
    local, _, domain = email.partition("@")
    return f"{local.split('+')[0]}+{tag}@{domain}" if domain else email


def derive_names(username: str, email: str, first: str | None, last: str | None) -> tuple[str, str]:
    """OpenProject requires first and last name; derive them from 'jan.jansen@x' when not given."""
    if first and last:
        return first, last
    parts = [p for p in re.split(r"[._-]+", email.split("@")[0].split("+")[0]) if p]
    f = first or (parts[0].capitalize() if parts else username)
    l = last or (" ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else "User")
    return f, l


def https_json(url: str, method: str = "GET", token: str | None = None, body=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    headers = {"Authorization": f"token {token}"} if token else {}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def deconflict_admin_emails(t: str, owner_email: str) -> None:
    """The directory user owns the real address; give the local break-glass accounts (OpenProject admin, Git admin,
    Keycloak admin) a plus-address if they still use it, so no account creation or e-mail based linking collides."""
    e = tenant_env(t)
    host = e["OP_HOST"]
    rb = TENANTS / t / "emails.rb"
    rb.write_text("User.where(login: 'admin', mail: ENV['OLD']).update_all(mail: ENV['NEW'])\nputs 'EMAILS=ok'\n")
    compose(t, "cp", str(rb), "openproject:/tmp/emails.rb")
    rb.unlink()
    compose(t, "exec", "-T", "-e", f"OLD={owner_email}", "-e", f"NEW={plus_address(owner_email, 'op')}", "openproject", "bash", "-c",
            "cd /app && RAILS_ENV=production bundle exec rails runner /tmp/emails.rb && rm /tmp/emails.rb")
    gitea = f"https://git.{host}/api/v1"
    me = https_json(f"{gitea}/users/panoptes", token=e["GITEA_TOKEN"])
    if (me.get("email") or "").lower() == owner_email.lower():
        https_json(f"{gitea}/admin/users/panoptes", "PATCH", e["GITEA_TOKEN"], {"email": plus_address(owner_email, "git"), "login_name": "panoptes", "source_id": 0})
    if e.get("SSO_ENABLED") == "1":
        tok = kc_api(host, "POST", "/realms/master/protocol/openid-connect/token",
                     form={"grant_type": "password", "client_id": "admin-cli", "username": "kcadmin", "password": e["KC_ADMIN_PASSWORD"]})["access_token"]
        for u in kc_api(host, "GET", "/admin/realms/panoptes/users?username=admin&exact=true", tok):
            if (u.get("email") or "").lower() == owner_email.lower():
                kc_api(host, "PUT", f"/admin/realms/panoptes/users/{u['id']}", tok, {**u, "email": plus_address(owner_email, "kc")})


def write_route(t: str) -> None:
    """(Re)write the tenant's Caddy routes from its current settings."""
    e = tenant_env(t)
    docs_hash = sh("docker", "run", "--rm", "caddy:2", "caddy", "hash-password", "--plaintext", e["DOCS_PASSWORD"])
    (PROXY_DIR / "tenants" / f"{t}.caddy").write_text(caddy_snippet(t, e["OP_HOST"], docs_hash, sso=e.get("SSO_ENABLED") == "1", ldap=e.get("LDAP_ENABLED") == "1"))
    reload_proxy()


def lldap_exec(t: str, *args, input=None) -> str:
    return compose(t, "exec", "-T", "lldap", *args, input=input)


def lldap_token(t: str, admin_pw: str) -> str:
    out = lldap_exec(t, "curl", "-s", "-X", "POST", "http://localhost:17170/auth/simple/login", "-H", "Content-Type: application/json",
                     "--data-binary", "@-", input=json.dumps({"username": "lldapadmin", "password": admin_pw}))
    return json.loads(out)["token"]


def lldap_graphql(t: str, token: str, query: str) -> dict:
    out = lldap_exec(t, "curl", "-s", "-X", "POST", "http://localhost:17170/api/graphql", "-H", "Content-Type: application/json",
                     "-H", f"Authorization: Bearer {token}", "--data-binary", "@-", input=json.dumps({"query": query}))
    data = json.loads(out)
    if data.get("errors"):
        raise RuntimeError(data["errors"][0]["message"])
    return data["data"]


def lldap_add_user(t: str, username: str, email: str, password: str, groups: tuple = (), display: str = "", first: str | None = None, last: str | None = None) -> None:
    """Create (or update the password of) a directory user and put it in the given groups (created when missing)."""
    admin_pw = tenant_env(t)["LDAP_ADMIN_PASSWORD"]
    token = lldap_token(t, admin_pw)
    q = lambda query: lldap_graphql(t, token, query)
    if username not in [u["id"] for u in q("query { users { id } }")["users"]]:
        f, l = derive_names(username, email, first, last)
        q('mutation { createUser(user: {id: %s, email: %s, displayName: %s, firstName: %s, lastName: %s}) { id } }'
          % (json.dumps(username), json.dumps(email), json.dumps(display or f"{f} {l}"), json.dumps(f), json.dumps(l)))
    lldap_exec(t, "/app/lldap_set_password", "--base-url", "http://localhost:17170", "--admin-username", "lldapadmin", "--admin-password", admin_pw,
               "--username", username, "--password", password)
    existing = {g["displayName"]: g["id"] for g in q("query { groups { id displayName } }")["groups"]}
    for g in groups:
        if g not in existing:
            existing[g] = q("mutation { createGroup(name: %s) { id } }" % json.dumps(g))["createGroup"]["id"]
        try:
            q("mutation { addUserToGroup(userId: %s, groupId: %d) { ok } }" % (json.dumps(username), existing[g]))
        except RuntimeError as err:
            if "already" not in str(err).lower() and "duplicate" not in str(err).lower() and "unique" not in str(err).lower():
                raise


def kc_api(host: str, method: str, path: str, token: str | None = None, body=None, form=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    data, headers = None, {}
    if form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://auth.{host}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        sys.exit(f"Keycloak {method} {path} failed: {err.code} {err.read().decode()[:300]}")


def keycloak_federate_ldap(t: str) -> None:
    """Point the Keycloak realm at the directory: users sign in with their directory password; directory group 'admin' becomes realm role 'admin'."""
    e = tenant_env(t)
    host = e["OP_HOST"]
    tok = kc_api(host, "POST", "/realms/master/protocol/openid-connect/token",
                 form={"grant_type": "password", "client_id": "admin-cli", "username": "kcadmin", "password": e["KC_ADMIN_PASSWORD"]})["access_token"]
    realm_id = kc_api(host, "GET", "/admin/realms/panoptes", tok)["id"]
    for c in kc_api(host, "GET", f"/admin/realms/panoptes/components?type=org.keycloak.storage.UserStorageProvider&name=panoptes-ldap", tok):
        kc_api(host, "DELETE", f"/admin/realms/panoptes/components/{c['id']}", tok)
    one = lambda **kw: {k: [str(v)] for k, v in kw.items()}
    provider = {"name": "panoptes-ldap", "providerId": "ldap", "providerType": "org.keycloak.storage.UserStorageProvider", "parentId": realm_id,
                "config": one(vendor="other", connectionUrl="ldap://lldap:3890", authType="simple", bindDn=f"uid={LDAP_BIND_USER},{LDAP_PEOPLE}",
                              bindCredential=e["LDAP_BIND_PASSWORD"], usersDn=LDAP_PEOPLE, usernameLDAPAttribute="uid", rdnLDAPAttribute="uid",
                              uuidLDAPAttribute="uid", userObjectClasses="person", editMode="READ_ONLY", importEnabled="true", syncRegistrations="false",
                              searchScope="1", pagination="false", enabled="true", priority="1", cachePolicy="DEFAULT", fullSyncPeriod="-1",
                              changedSyncPeriod="-1", validatePasswordPolicy="false", trustEmail="true", connectionPooling="true")}
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(f"https://auth.{host}/admin/realms/panoptes/components", data=json.dumps(provider).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        comp_id = r.headers["Location"].rsplit("/", 1)[1]
    mapper = {"name": "directory-groups-to-roles", "providerId": "role-ldap-mapper", "providerType": "org.keycloak.storage.ldap.mappers.LDAPStorageMapper", "parentId": comp_id,
              "config": one(mode="READ_ONLY", **{"membership.attribute.type": "DN", "user.roles.retrieve.strategy": "LOAD_ROLES_BY_MEMBER_ATTRIBUTE",
                            "roles.dn": LDAP_GROUPS, "membership.ldap.attribute": "uniqueMember", "membership.user.ldap.attribute": "uid",
                            "role.name.ldap.attribute": "cn", "role.object.classes": "groupOfUniqueNames", "use.realm.roles.mapping": "true"})}
    kc_api(host, "POST", "/admin/realms/panoptes/components", tok, mapper)


def enable_ldap(a) -> None:
    """Add a bundled LDAP directory (lldap): one account for OpenProject, Git and the docs portal."""
    t = a.name
    tdir = TENANTS / t
    env = tdir / ".env"
    if not env.exists():
        sys.exit(f"unknown tenant {t}")
    e = read_env(env)
    host = e["OP_HOST"]
    if e.get("LDAP_ENABLED") == "1":
        sys.exit("the directory is already enabled for this tenant")
    admin_email = a.admin_email or e.get("ADMIN_EMAIL", "admin@example.com")
    first_pw = secrets.token_urlsafe(12)
    for k, v in {"LDAP_ENABLED": "1", "LDAP_JWT_SECRET": secrets.token_hex(32), "LDAP_KEY_SEED": secrets.token_hex(32),
                 "LDAP_ADMIN_PASSWORD": secrets.token_urlsafe(18), "LDAP_BIND_PASSWORD": secrets.token_urlsafe(18),
                 "LDAP_FIRST_USER": a.admin_user, "LDAP_FIRST_PASSWORD": first_pw}.items():
        if v or k not in e:
            set_env(env, k, v)
    e = read_env(env)

    print("• routing users.<host> (directory web UI)")
    write_route(t)
    print("• starting the directory")
    compose(t, "up", "-d")
    for _ in range(40):
        try:
            lldap_token(t, e["LDAP_ADMIN_PASSWORD"])
            break
        except (SystemExit, json.JSONDecodeError, KeyError):
            time.sleep(3)
    else:
        sys.exit("the directory did not start (docker compose logs lldap)")

    print("• creating the read-only service account and the first user")
    lldap_add_user(t, LDAP_BIND_USER, f"{LDAP_BIND_USER}@panoptes.local", e["LDAP_BIND_PASSWORD"], ("lldap_strict_readonly",), "Panoptes service account", "Panoptes", "Service")
    deconflict_admin_emails(t, admin_email)
    lldap_add_user(t, a.admin_user, admin_email, first_pw, ("admin",), first=a.first_name, last=a.last_name)

    print("• configuring OpenProject (LDAP login source, accounts are created on first sign-in)")
    rb = tdir / "ldap.rb"
    rb.write_text(OP_LDAP)
    compose(t, "cp", str(rb), "openproject:/tmp/ldap.rb")
    rb.unlink()
    out = compose(t, "exec", "-T", "-e", f"BIND_DN=uid={LDAP_BIND_USER},{LDAP_PEOPLE}", "-e", f"BIND_PW={e['LDAP_BIND_PASSWORD']}", "-e", f"PEOPLE_DN={LDAP_PEOPLE}",
                  "openproject", "bash", "-c", "cd /app && RAILS_ENV=production bundle exec rails runner /tmp/ldap.rb && rm /tmp/ldap.rb")
    if "OPLDAP=ok" not in out:
        sys.exit("OpenProject LDAP configuration failed:\n" + out[-600:])

    sso = e.get("SSO_ENABLED") == "1"
    if sso:
        print("• federating Keycloak with the directory (Git and docs sign-ins)")
        keycloak_federate_ldap(t)

    print(f"""
Directory enabled for '{t}'.
  User management    https://users.{host}   (log in as lldapadmin; password LDAP_ADMIN_PASSWORD in {env})
  First user         {a.admin_user} / LDAP_FIRST_PASSWORD in {env} (member of the group 'admin'; change the password in the directory UI)
  OpenProject        log in at https://{host} with a directory account; the account is created on first sign-in (not an OpenProject admin: grant that in OpenProject)
  Git and docs       {"same accounts through Keycloak; group 'admin' becomes the realm role 'admin'" if sso else "run 'tenantctl enable-sso' to give Git and the docs portal the same accounts"}
  Add people         tenantctl.py ldap-user {t} --username jan --email jan@example.com
""")


def ldap_user(a) -> None:
    t = a.name
    e = tenant_env(t)
    if e.get("LDAP_ENABLED") != "1":
        sys.exit("enable the directory first: tenantctl enable-ldap " + t)
    pw = a.password or secrets.token_urlsafe(12)
    lldap_add_user(t, a.username, a.email, pw, ("admin",) if a.admin else (), first=a.first_name, last=a.last_name)
    print(f"user {a.username} ready" + ("" if a.password else f"; initial password: {pw}") + (" (member of group 'admin')" if a.admin else ""))


# ---------------------------------------------------------------------------------------------------------------
# Encrypted off-site backups with restic (client-side encryption, deduplicated snapshots, S3/SFTP/... backends).
# The repository password lives in deploy/offsite.env and NOT in the tenant folder, so it is never part of a snapshot.
# ---------------------------------------------------------------------------------------------------------------
OFFSITE_ENV = Path(os.environ.get("PANOPTES_OFFSITE_ENV", ROOT / "deploy" / "offsite.env"))
RESTIC_IMAGE = "restic/restic:0.17.3"


def fresh_volume(name: str) -> None:
    sh("docker", "volume", "rm", "-f", name, check=False)
    sh("docker", "volume", "create", name)


def volume_cat(volume_name: str, path: str) -> str:
    return sh("docker", "run", "--rm", "-v", f"{volume_name}:/v:ro", "alpine", "cat", f"/v/{path}", check=False)


def offsite_conf() -> dict:
    if not OFFSITE_ENV.exists():
        sys.exit("off-site backups are not configured yet: tenantctl.py offsite configure --repository <repo> --generate-password")
    conf = read_env(OFFSITE_ENV)
    if not conf.get("RESTIC_REPOSITORY") or not conf.get("RESTIC_PASSWORD"):
        sys.exit(f"{OFFSITE_ENV} needs RESTIC_REPOSITORY and RESTIC_PASSWORD")
    return conf


def restic(t: str, conf: dict, args: list, mounts: tuple = (), user: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run restic in a container against the tenant's own repository (<repository>/<tenant>).
    Secrets are passed by name (-e KEY) so they never show up in the process list."""
    repo = conf["RESTIC_REPOSITORY"].rstrip("/")
    local = repo.startswith("/")
    secret_keys = [k for k in conf if k not in ("RESTIC_REPOSITORY",) and not k.startswith("OFFSITE_")]
    env = {**os.environ, **{k: conf[k] for k in secret_keys}, "RESTIC_REPOSITORY": f"/repo/{t}" if local else f"{repo}/{t}"}
    cmd = ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway", "-e", "RESTIC_REPOSITORY", *[x for k in secret_keys for x in ("-e", k)]]
    if user:
        cmd += ["--user", user]
    if local:
        Path(repo).mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{repo}:/repo"]
    cmd += [*mounts, RESTIC_IMAGE, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    if check and r.returncode != 0:
        sys.exit(f"restic {' '.join(args[:2])} failed:\n{r.stdout[-600:]}{r.stderr[-1200:]}")
    return r


def restic_ensure_repo(t: str, conf: dict) -> None:
    if restic(t, conf, ["cat", "config"], check=False).returncode != 0:
        print("• initialising the encrypted repository")
        restic(t, conf, ["init"])


def notify_failure(conf: dict, text: str) -> None:
    url = conf.get("OFFSITE_NOTIFY_WEBHOOK")
    if url:
        try:
            https_json(url, "POST", body={"text": f"Panoptes off-site backup: {text}"})
        except Exception:  # the notification is best effort
            pass


def offsite_configure(a) -> None:
    conf = read_env(OFFSITE_ENV) if OFFSITE_ENV.exists() else {}
    generated = None
    if a.password:
        conf["RESTIC_PASSWORD"] = a.password
    elif a.generate_password:
        if conf.get("RESTIC_PASSWORD") and not a.force:
            sys.exit("a repository password is already configured; changing it makes existing snapshots unreadable (use --force to overwrite)")
        generated = conf["RESTIC_PASSWORD"] = secrets.token_urlsafe(32)
    if a.repository:
        conf["RESTIC_REPOSITORY"] = a.repository
    for flag, key in (("aws_access_key_id", "AWS_ACCESS_KEY_ID"), ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"), ("notify_webhook", "OFFSITE_NOTIFY_WEBHOOK"),
                      ("keep_daily", "OFFSITE_KEEP_DAILY"), ("keep_weekly", "OFFSITE_KEEP_WEEKLY"), ("keep_monthly", "OFFSITE_KEEP_MONTHLY")):
        if getattr(a, flag) is not None:
            conf[key] = str(getattr(a, flag))
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        conf[k] = v
    conf.setdefault("OFFSITE_KEEP_DAILY", "7")
    conf.setdefault("OFFSITE_KEEP_WEEKLY", "4")
    conf.setdefault("OFFSITE_KEEP_MONTHLY", "6")
    if not conf.get("RESTIC_REPOSITORY") or not conf.get("RESTIC_PASSWORD"):
        sys.exit("need --repository and a password (--generate-password or --password)")
    OFFSITE_ENV.write_text("\n".join(f"{k}={v}" for k, v in conf.items()) + "\n")
    OFFSITE_ENV.chmod(0o600)
    print(f"saved {OFFSITE_ENV} (chmod 600)")
    if generated:
        print(f"""
IMPORTANT: the repository password was generated just now:

    {generated}

Store it in your password manager NOW. Without it the off-site backups cannot be decrypted, and it is deliberately not
part of any backup. (It is also in {OFFSITE_ENV}, which is not in git.)""")


def offsite_backup(a) -> None:
    conf = offsite_conf()
    names = sorted(d.name for d in TENANTS.iterdir() if (d / ".env").exists()) if a.all else [a.name]
    if not names or names == [None]:
        sys.exit("give a tenant name or --all")
    failed = []
    for t in names:
        try:
            offsite_backup_one(t, conf)
        except SystemExit as err:
            failed.append(t)
            notify_failure(conf, f"tenant {t} failed: {err}")
            print(f"FAILED {t}: {err}", file=sys.stderr)
    if failed:
        sys.exit(f"backup failed for: {', '.join(failed)}")


def offsite_backup_one(t: str, conf: dict) -> None:
    tdir = TENANTS / t
    if not (tdir / ".env").exists():
        sys.exit(f"unknown tenant {t}")
    host = f"panoptes-{t}"
    restic_ensure_repo(t, conf)
    print(f"• {t}: stopping the stack for a consistent snapshot")
    compose(t, "stop")
    stage = f"panoptes-{t}_backup_stage"
    try:
        # restic cannot read host bind mounts on Docker Desktop (I/O errors), so stage the tenant files in a volume first
        fresh_volume(stage)
        route = PROXY_DIR / "tenants" / f"{t}.caddy"
        sh("docker", "run", "--rm", "-v", f"{stage}:/stage", "-v", f"{tdir}:/src:ro", "-v", f"{route.parent}:/routes:ro", "alpine", "sh", "-c",
           f"mkdir -p /stage/tenant /stage/route && cp -a /src/. /stage/tenant/ && rm -f /stage/tenant/ca-bundle.crt && "
           f"{{ [ -f /routes/{t}.caddy ] && cp /routes/{t}.caddy /stage/route/route.caddy; true; }}")
        mounts = [x for v in volumes_for(t) for x in ("-v", f"{volume(t, v)}:/data/volumes/{v}:ro")] + ["-v", f"{stage}:/data/stage:ro"]
        print(f"• {t}: encrypting and uploading (only changed data is sent after the first run)")
        r = restic(t, conf, ["backup", "/data", "--host", host, "--tag", "panoptes", "--tag", f"tenant-{t}", "--json"], tuple(mounts))
        summary = next((json.loads(l) for l in r.stdout.splitlines() if '"message_type":"summary"' in l.replace(" ", "")), {})
    finally:
        sh("docker", "volume", "rm", "-f", stage, check=False)
        compose(t, "start")
    keep = [x for k, f in (("OFFSITE_KEEP_DAILY", "--keep-daily"), ("OFFSITE_KEEP_WEEKLY", "--keep-weekly"), ("OFFSITE_KEEP_MONTHLY", "--keep-monthly")) for x in (f, conf.get(k, "0"))]
    restic(t, conf, ["forget", "--host", host, "--tag", "panoptes", *keep, "--prune"])
    mb = summary.get("data_added", 0) / 1024 / 1024
    print(f"• {t}: snapshot {summary.get('snapshot_id', '?')[:8]} stored ({mb:.1f} MB new data uploaded); retention applied")


def offsite_list(a) -> None:
    conf = offsite_conf()
    print(restic(a.name, conf, ["snapshots", "--host", f"panoptes-{a.name}", "--tag", "panoptes"]).stdout)


def offsite_verify(a) -> None:
    """Restore drill, non-destructive: repository integrity, snapshot age, and a test restore of key files into a temp folder."""
    conf = offsite_conf()
    t, host = a.name, f"panoptes-{a.name}"
    try:
        snaps = json.loads(restic(t, conf, ["snapshots", "--host", host, "--tag", "panoptes", "--json", "--latest", "1"]).stdout or "[]")
        if not snaps:
            sys.exit(f"no snapshots for {t}")
        latest = snaps[-1]
        taken = datetime.datetime.fromisoformat(latest["time"][:19]).replace(tzinfo=datetime.timezone.utc)
        age_h = (datetime.datetime.now(datetime.timezone.utc) - taken).total_seconds() / 3600
        print(f"• latest snapshot {latest['short_id']} is {age_h:.1f} hours old")
        if age_h > a.max_age_hours:
            sys.exit(f"latest snapshot is older than {a.max_age_hours} hours")
        print("• checking repository integrity (structure + 5% of the data)")
        restic(t, conf, ["check", "--read-data-subset=5%"])
        print("• test-restoring key files")
        scratch = f"panoptes-{t}_verify_scratch"
        fresh_volume(scratch)
        try:
            restic(t, conf, ["restore", latest["id"], "--target", "/restore", "--include", "/data/stage/tenant/.env", "--include", "/data/volumes/op_pgdata/PG_VERSION"],
                   ("-v", f"{scratch}:/restore",))
            if f"TENANT={t}" not in volume_cat(scratch, "data/stage/tenant/.env").splitlines():
                sys.exit("restored .env does not belong to this tenant")
            if not volume_cat(scratch, "data/volumes/op_pgdata/PG_VERSION").strip():
                sys.exit("the OpenProject database files are missing from the snapshot")
        finally:
            sh("docker", "volume", "rm", "-f", scratch, check=False)
        print(f"verified: {t} can be restored (snapshot {latest['short_id']})")
    except SystemExit as err:
        notify_failure(conf, f"verification of {t} failed: {err}")
        raise


def offsite_restore(a) -> None:
    """Restore a tenant from the off-site repository (replaces everything currently stored for that tenant)."""
    if not a.yes:
        sys.exit("restore REPLACES all data of this tenant with the snapshot; repeat with --yes")
    conf = offsite_conf()
    t, host = a.name, f"panoptes-{a.name}"
    snap = a.snapshot
    tdir = TENANTS / t
    uid = f"{os.getuid()}:{os.getgid()}"
    stage = f"panoptes-{t}_restore_stage"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        print("• reading the snapshot's tenant settings")
        fresh_volume(stage)
        restic(t, conf, ["restore", snap, "--host", host, "--tag", "panoptes", "--target", "/restore", "--include", "/data/stage"], ("-v", f"{stage}:/restore/data/stage",))
        sh("docker", "run", "--rm", "-v", f"{stage}:/s:ro", "-v", f"{tmp}:/out", "alpine", "sh", "-c", f"cp -a /s/. /out/ && chown -R {uid} /out")
        sh("docker", "volume", "rm", "-f", stage, check=False)
        flags = read_env(tmp / "tenant" / ".env")
        vols = ("op_pgdata", "op_assets", "gitea_data") + (("kc_data",) if flags.get("SSO_ENABLED") == "1" else ()) + (("lldap_data",) if flags.get("LDAP_ENABLED") == "1" else ())
        if (tdir / ".env").exists():
            compose(t, "down", "-v")
        for v in vols:
            fresh_volume(volume(t, v))
        print("• restoring the data volumes")
        restic(t, conf, ["restore", snap, "--host", host, "--tag", "panoptes", "--target", "/restore", "--include", "/data/volumes"],
               tuple(x for v in vols for x in ("-v", f"{volume(t, v)}:/restore/data/volumes/{v}")))
        shutil.rmtree(tdir, ignore_errors=True)
        shutil.copytree(tmp / "tenant", tdir)
        (tdir / ".env").chmod(0o600)
        set_env(tdir / ".env", "TENANT_DIR", str(tdir))
        (PROXY_DIR / "tenants").mkdir(parents=True, exist_ok=True)
        if (tmp / "route" / "route.caddy").exists():
            shutil.copy(tmp / "route" / "route.caddy", PROXY_DIR / "tenants" / f"{t}.caddy")
    sh("docker", "network", "create", "panoptes-proxy", check=False)
    reload_proxy()
    e = read_env(tdir / ".env")
    ensure_ca_bundle(tdir, e["OP_HOST"].endswith("localhost"))
    compose(t, "up", "-d")
    wait_ready(f"https://{e['OP_HOST']}/health_checks/default", f"OpenProject of {t}")
    print(f"tenant {t} restored from the off-site snapshot {snap}")


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
    e = tenant_env(t)
    return ("op_pgdata", "op_assets", "gitea_data") + (("kc_data",) if e.get("SSO_ENABLED") == "1" else ()) + (("lldap_data",) if e.get("LDAP_ENABLED") == "1" else ())


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
    ld = sub.add_parser("enable-ldap")
    ld.add_argument("name")
    ld.add_argument("--admin-user", default="owner", help="first directory user (not 'admin' or 'panoptes': those are local break-glass accounts)")
    ld.add_argument("--admin-email")
    ld.add_argument("--first-name")
    ld.add_argument("--last-name")
    ld.set_defaults(fn=enable_ldap)
    lu = sub.add_parser("ldap-user")
    lu.add_argument("name")
    lu.add_argument("--username", required=True)
    lu.add_argument("--email", required=True)
    lu.add_argument("--first-name", help="default: derived from the e-mail address (jan.jansen@x -> Jan Jansen)")
    lu.add_argument("--last-name")
    lu.add_argument("--password", help="default: generated and printed")
    lu.add_argument("--admin", action="store_true", help="add to the directory group 'admin' (Keycloak role 'admin')")
    lu.set_defaults(fn=ldap_user)
    off = sub.add_parser("offsite", help="encrypted off-site backups (restic)")
    osub = off.add_subparsers(dest="ocmd", required=True)
    oc = osub.add_parser("configure")
    oc.add_argument("--repository", help="restic repository: s3:https://host/bucket/prefix, sftp:user@host:/path, rclone:remote:path or a local /path")
    oc.add_argument("--password", help="repository password (or use --generate-password)")
    oc.add_argument("--generate-password", action="store_true")
    oc.add_argument("--force", action="store_true")
    oc.add_argument("--aws-access-key-id")
    oc.add_argument("--aws-secret-access-key")
    oc.add_argument("--keep-daily", type=int)
    oc.add_argument("--keep-weekly", type=int)
    oc.add_argument("--keep-monthly", type=int)
    oc.add_argument("--notify-webhook", help="Slack-compatible webhook that hears about failed backups/verifications")
    oc.add_argument("--set", action="append", metavar="KEY=VALUE", help="extra backend settings (for example B2_ACCOUNT_ID=...)")
    oc.set_defaults(fn=offsite_configure)
    ob = osub.add_parser("backup")
    ob.add_argument("name", nargs="?")
    ob.add_argument("--all", action="store_true")
    ob.set_defaults(fn=offsite_backup)
    ol = osub.add_parser("list")
    ol.add_argument("name")
    ol.set_defaults(fn=offsite_list)
    ov = osub.add_parser("verify")
    ov.add_argument("name")
    ov.add_argument("--max-age-hours", type=int, default=36)
    ov.set_defaults(fn=offsite_verify)
    orr = osub.add_parser("restore")
    orr.add_argument("name")
    orr.add_argument("--snapshot", default="latest")
    orr.add_argument("--yes", action="store_true")
    orr.set_defaults(fn=offsite_restore)
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
