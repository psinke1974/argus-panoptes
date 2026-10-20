import hashlib
import hmac
import logging

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from . import config
from .docs import changed_docs, doc_url, router as docs_router
from .openproject import OpenProject
from .refs import parse_refs
from .release_notes import build_release_notes

log = logging.getLogger("werkplaats")
app = FastAPI(title="Werkplaats glue")
op = OpenProject()
app.include_router(docs_router)


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
            for ref in parse_refs(c["message"]):
                first = c["message"].splitlines()[0]
                docs = "".join(f"\n- Documentatie: [{d}]({doc_url(repo, d)})" for d in changed_docs(c))
                await op.comment(ref.wp_id, f"Commit in **{repo}**: [{first}]({c['url']}) door {c['author']['name']}{docs}")
                if ref.closes and on_default:
                    await op.set_status(ref.wp_id, config.STATUS_CLOSED)
                handled += 1

    elif x_gitea_event == "pull_request":
        pr = p["pull_request"]
        action = p.get("action")
        for ref in parse_refs(f"{pr['title']}\n{pr.get('body') or ''}"):
            if action == "opened":
                await op.comment(ref.wp_id, f"Pull request geopend in **{repo}**: [{pr['title']}]({pr['html_url']})")
                await op.set_status(ref.wp_id, config.STATUS_IN_PROGRESS)
            elif action == "closed" and pr.get("merged"):
                await op.comment(ref.wp_id, f"Pull request gemerged: [{pr['title']}]({pr['html_url']})")
                if ref.closes:
                    await op.set_status(ref.wp_id, config.STATUS_CLOSED)
            else:
                continue
            handled += 1

    return {"handled": handled}


@app.get("/release-notes")
async def release_notes(repo: str, base: str, head: str):
    """Markdown release notes uit commits tussen twee tags/branches, gegroepeerd per type werkpakket."""
    async with httpx.AsyncClient(base_url=config.GITEA_URL, headers={"Authorization": f"token {config.GITEA_TOKEN}"}) as g:
        r = await g.get(f"/api/v1/repos/{repo}/compare/{base}...{head}")
        r.raise_for_status()
        messages = [c["commit"]["message"] for c in r.json().get("commits", [])]
    return {"markdown": await build_release_notes(op, messages, f"{base} → {head}")}
