"""Optional OpenID Connect single sign-on for the docs portal and the reports.

Active when OIDC_ISSUER is set. Repository access is checked *as the signed-in user* by asking Gitea
(Sudo header, service token must be an admin token), so users only see repositories they may read in Gitea.
Without OIDC_ISSUER nothing here is enforced (single-stack development, or the tenant's basic-auth fallback)."""
import time
from urllib.parse import quote

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config

router = APIRouter(prefix="/auth")
_oauth: OAuth | None = None
_access_cache: dict[tuple[str, str], tuple[float, bool]] = {}
ACCESS_TTL = 30  # seconds


class NeedLogin(Exception):
    pass


def client():
    global _oauth
    if _oauth is None:
        _oauth = OAuth()
        _oauth.register(
            name="sso",
            server_metadata_url=f"{config.OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration",
            client_id=config.OIDC_CLIENT_ID,
            client_secret=config.OIDC_CLIENT_SECRET,
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth.sso


def safe_next(value: str | None) -> str:
    """Only allow local paths as post-login redirect (no open redirect)."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


@router.get("/login")
async def login(request: Request, next: str = "/"):
    request.session["next"] = safe_next(next)
    return await client().authorize_redirect(request, f"{config.PUBLIC_URL}/auth/callback")


@router.get("/callback")
async def callback(request: Request):
    token = await client().authorize_access_token(request)
    info = token.get("userinfo") or {}
    request.session["user"] = {
        "username": info.get("preferred_username") or info.get("email", "").split("@")[0],
        "email": info.get("email", ""),
        "name": info.get("name", ""),
        "roles": info.get("roles", []),
    }
    return RedirectResponse(safe_next(request.session.pop("next", "/")))


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    end = f"{config.OIDC_ISSUER.rstrip('/')}/protocol/openid-connect/logout"
    return RedirectResponse(f"{end}?client_id={quote(config.OIDC_CLIENT_ID)}&post_logout_redirect_uri={quote(config.PUBLIC_URL + '/auth/signed-out')}")


@router.get("/signed-out", response_class=HTMLResponse)
async def signed_out():
    return '<!doctype html><meta charset=utf-8><title>Signed out</title><p>You have been signed out. <a href="/auth/login">Sign in again</a></p>'


async def require_login(request: Request) -> dict | None:
    """Dependency: the signed-in user, or None when SSO is not enabled. Raises NeedLogin otherwise."""
    if not config.OIDC_ISSUER:
        return None
    user = request.session.get("user")
    if not user:
        raise NeedLogin
    return user


async def can_read_repo(http: httpx.AsyncClient, username: str, repo: str) -> bool:
    """Ask Gitea as `username` (Sudo) whether the repository is readable. Unknown users have no access."""
    r = await http.get(f"/api/v1/repos/{repo}", headers={"Sudo": username})
    return r.status_code == 200


async def ensure_repo_access(username: str, repo: str) -> None:
    now = time.monotonic()
    hit = _access_cache.get((username, repo))
    if hit and now - hit[0] < ACCESS_TTL:
        allowed = hit[1]
    else:
        async with httpx.AsyncClient(base_url=config.GITEA_URL, headers={"Authorization": f"token {config.GITEA_TOKEN}"}, timeout=20) as g:
            allowed = await can_read_repo(g, username, repo)
        _access_cache[(username, repo)] = (now, allowed)
    if not allowed:  # do not reveal whether the repository exists
        raise HTTPException(404, "repo not found, or no access. Sign in to the Git service once so your account exists there.")


async def repo_access(request: Request, owner: str, name: str) -> None:
    """Route dependency for /docs/{owner}/{name}/..."""
    user = await require_login(request)
    if user:
        await ensure_repo_access(user["username"], f"{owner}/{name}")


async def require_admin(request: Request) -> None:
    user = await require_login(request)
    if user and config.OIDC_ADMIN_ROLE not in user.get("roles", []):
        raise HTTPException(403, "administrators only")
