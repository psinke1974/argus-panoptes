import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, digest, hooks, legacy
from .docs import router as docs_router
from .openproject import OpenProject
from .release_notes import build_release_notes
from .stale import stale_markdown

log = logging.getLogger("panoptes")
@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(digest.scheduler(op)) if digest.configured() else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Panoptes glue", lifespan=lifespan)
op = OpenProject()
app.include_router(docs_router)
app.include_router(auth.router)
if config.OIDC_ISSUER:
    app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="panoptes_session", https_only=True, same_site="lax", max_age=8 * 3600)


@app.exception_handler(auth.NeedLogin)
async def need_login(request: Request, _exc):
    """Browsers go to the login page; API clients (JSON) get a 401."""
    wants_json = request.query_params.get("format") == "json" or "application/json" in request.headers.get("accept", "")
    if wants_json:
        return JSONResponse({"detail": "sign in required"}, status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/auth/login?next={quote(target, safe='')}")


@app.get("/health")
async def health():
    return {"ok": True}


async def _github_like(request: Request, event: str, signature: str | None) -> dict:
    body = await request.body()
    if not hooks.signature_ok(body, signature):
        raise HTTPException(401, "bad signature")
    return {"handled": await hooks.handle_github_like(op, event, await request.json(), doc_links=True)}


@app.post("/hooks/gitea")
async def gitea_hook(request: Request, x_gitea_event: str = Header(default=""), x_gitea_signature: str | None = Header(default=None)):
    return await _github_like(request, x_gitea_event, x_gitea_signature)


@app.post("/hooks/forgejo")
async def forgejo_hook(request: Request, x_forgejo_event: str = Header(default=""), x_forgejo_signature: str | None = Header(default=None),
                       x_gitea_event: str = Header(default=""), x_gitea_signature: str | None = Header(default=None)):
    """Forgejo sends X-Forgejo-* headers and, for compatibility, the X-Gitea-* ones; either works."""
    return await _github_like(request, x_forgejo_event or x_gitea_event, x_forgejo_signature or x_gitea_signature)


@app.post("/hooks/github")
async def github_hook(request: Request, x_github_event: str = Header(default=""), x_hub_signature_256: str | None = Header(default=None)):
    """GitHub (set the webhook secret to WEBHOOK_SECRET, content type application/json, events: push + pull requests)."""
    body = await request.body()
    if not hooks.signature_ok(body, x_hub_signature_256):
        raise HTTPException(401, "bad signature")
    return {"handled": await hooks.handle_github_like(op, x_github_event, await request.json(), doc_links=False)}


@app.post("/hooks/gitlab")
async def gitlab_hook(request: Request, x_gitlab_event: str = Header(default=""), x_gitlab_token: str | None = Header(default=None)):
    """GitLab (secret token = WEBHOOK_SECRET; events: push + merge requests)."""
    if not hooks.token_ok(x_gitlab_token):
        raise HTTPException(401, "bad token")
    return {"handled": await hooks.handle_gitlab(op, x_gitlab_event, await request.json())}


@app.get("/release-notes")
async def release_notes(request: Request, repo: str, base: str, head: str):
    """Markdown release notes from the commits between two tags/branches, grouped by work package type."""
    user = await auth.require_login(request)
    if user:
        await auth.ensure_repo_access(user["username"], repo)
    async with httpx.AsyncClient(base_url=config.GITEA_URL, headers={"Authorization": f"token {config.GITEA_TOKEN}"}) as g:
        r = await g.get(f"/api/v1/repos/{repo}/compare/{base}...{head}")
        r.raise_for_status()
        messages = [c["commit"]["message"] for c in r.json().get("commits", [])]
    return {"markdown": await build_release_notes(op, messages, f"{base} → {head}")}


@app.get("/stale", dependencies=[Depends(auth.require_admin)])
async def stale(project: str, days: int = 30, format: str = "markdown"):
    """Open work packages in an OpenProject project (identifier) that nobody has touched for `days` days."""
    items = await op.stale(project, days)
    if format == "json":
        return {"project": project, "days": days, "items": items}
    return {"markdown": stale_markdown(items, project, days)}


@app.get("/digest/preview", dependencies=[Depends(auth.require_admin)])
async def digest_preview():
    """The text the weekly digest would contain right now (nothing is sent)."""
    text, total = await digest.build_digest(op)
    return {"stale": total, "configured": digest.configured(), "markdown": text}


@app.post("/digest/send", dependencies=[Depends(auth.require_admin)])
async def digest_send():
    """Send the digest now to the configured channels."""
    if not digest.configured():
        raise HTTPException(409, "digest not configured: set DIGEST_PROJECTS and DIGEST_WEBHOOK_URL or DIGEST_SMTP_HOST + DIGEST_MAIL_TO")
    return await digest.send_digest(op)
