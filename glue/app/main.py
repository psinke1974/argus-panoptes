import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, digest, legacy
from .docs import changed_docs, doc_url, router as docs_router
from .openproject import OpenProject
from .refs import parse_refs
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


def verify(body: bytes, signature: str | None) -> bool:
    if not config.WEBHOOK_SECRET:
        return False
    expected = hmac.new(config.WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/hooks/gitea")
async def gitea_hook(
    request: Request,
    x_gitea_event: str = Header(default=""),
    x_gitea_signature: str | None = Header(default=None),
):
    body = await request.body()
    if not verify(body, x_gitea_signature):
        raise HTTPException(401, "bad signature")
    p = await request.json()
    repo = p.get("repository", {}).get("full_name", "?")
    handled = 0

    if x_gitea_event == "push":
        default = p["repository"].get("default_branch", "main")
        on_default = p.get("ref") == f"refs/heads/{default}"
        for c in p.get("commits", []):
            for ref in parse_refs(c["message"], legacy.load()):
                first = c["message"].splitlines()[0]
                docs = "".join(f"\n- Documentation: [{d}]({doc_url(repo, d)})" for d in changed_docs(c))
                await op.comment(ref.wp_id, f"Commit in **{repo}**: [{first}]({c['url']}) by {c['author']['name']}{docs}")
                if ref.closes and on_default:
                    await op.set_status(ref.wp_id, config.STATUS_CLOSED)
                handled += 1

    elif x_gitea_event == "pull_request":
        pr = p["pull_request"]
        action = p.get("action")
        for ref in parse_refs(f"{pr['title']}\n{pr.get('body') or ''}", legacy.load()):
            if action == "opened":
                await op.comment(ref.wp_id, f"Pull request opened in **{repo}**: [{pr['title']}]({pr['html_url']})")
                await op.set_status(ref.wp_id, config.STATUS_IN_PROGRESS)
            elif action == "closed" and pr.get("merged"):
                await op.comment(ref.wp_id, f"Pull request merged: [{pr['title']}]({pr['html_url']})")
                if ref.closes:
                    await op.set_status(ref.wp_id, config.STATUS_CLOSED)
            else:
                continue
            handled += 1

    return {"handled": handled}


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
