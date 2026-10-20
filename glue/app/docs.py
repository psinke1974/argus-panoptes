"""Docs-in-repo: Markdown in git is de bron; dit portaal toont het, doorzoekbaar en met backlinks naar werkpakketten."""
import html
import mimetypes
import posixpath
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from . import config, legacy
from .refs import parse_refs

router = APIRouter(prefix="/docs")

STYLE = """body{font:16px/1.6 system-ui;margin:0;display:flex;min-height:100vh}nav{width:260px;background:#f4f5f7;padding:1rem;box-sizing:border-box}
nav a{display:block;padding:.2rem 0;color:#0b5cad;text-decoration:none}main{flex:1;max-width:820px;padding:1rem 2rem}
aside{border-top:1px solid #ddd;margin-top:2rem;padding-top:.5rem;font-size:.9rem;color:#555}pre{background:#f4f5f7;padding:.8rem;overflow:auto}
input{width:100%;box-sizing:border-box;padding:.3rem;margin-bottom:.6rem}"""


def is_doc(path: str) -> bool:
    return path.lower().endswith(".md") and (path.startswith("docs/") or "/" not in path)


def doc_url(repo: str, path: str) -> str:
    return f"{config.PUBLIC_URL}/docs/{repo}/{quote(path)}"


def changed_docs(commit: dict) -> list[str]:
    files = (commit.get("added") or []) + (commit.get("modified") or [])
    return sorted({f for f in files if is_doc(f)})


_LINK = re.compile(r"(!?\[[^\]]*\])\((?!https?:|mailto:|#|/)([^)\s]+)\)")


def rewrite_links(md: str, repo: str, path: str) -> str:
    """Relatieve links in een doc -> portaal-URL's (.md = pagina, rest = bestand via /_raw/)."""
    def fix(m):
        target, _, frag = m.group(2).partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        kind = "" if resolved.lower().endswith(".md") else "_raw/"
        return f"{m.group(1)}(/docs/{repo}/{kind}{quote(resolved)}{'#' + frag if frag else ''})"

    return _LINK.sub(fix, md)


def page(repo: str, nav: list[str], title: str, body: str, q: str = "") -> HTMLResponse:
    links = "".join(f'<a href="/docs/{repo}/{quote(p)}">{html.escape(p)}</a>' for p in nav)
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><style>{STYLE}</style>"
        f'<nav><b>{html.escape(repo)}</b><form action="/docs/{repo}/search"><input name=q placeholder="Zoeken…" value="{html.escape(q)}"></form>{links}</nav>'
        f"<main>{body}</main>"
    )


def gitea() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.GITEA_URL, headers={"Authorization": f"token {config.GITEA_TOKEN}"}, timeout=20)


async def doc_paths(g: httpx.AsyncClient, repo: str) -> list[str]:
    info = await g.get(f"/api/v1/repos/{repo}")
    if info.status_code != 200:
        raise HTTPException(404, "repo niet gevonden")
    tree = await g.get(f"/api/v1/repos/{repo}/git/trees/{info.json()['default_branch']}", params={"recursive": "true", "per_page": 1000})
    return sorted(t["path"] for t in tree.json().get("tree", []) if t["type"] == "blob" and is_doc(t["path"]))


async def raw(g: httpx.AsyncClient, repo: str, path: str) -> str:
    r = await g.get(f"/api/v1/repos/{repo}/raw/{quote(path)}")
    if r.status_code != 200:
        raise HTTPException(404, "pagina niet gevonden")
    return r.text


@router.get("/{owner}/{name}", response_class=HTMLResponse)
async def index(owner: str, name: str):
    repo = f"{owner}/{name}"
    async with gitea() as g:
        paths = await doc_paths(g, repo)
    body = "<h1>Documentatie</h1>" + ("".join(f'<p><a href="/docs/{repo}/{quote(p)}">{html.escape(p)}</a></p>' for p in paths) or "<p>Geen .md-bestanden in <code>docs/</code> of de root.</p>")
    return page(repo, paths, f"Docs {repo}", body)


@router.get("/{owner}/{name}/search", response_class=HTMLResponse)
async def search(owner: str, name: str, q: str = ""):
    repo = f"{owner}/{name}"
    hits = []
    async with gitea() as g:
        paths = await doc_paths(g, repo)
        for p in paths:
            text = await raw(g, repo, p)
            lines = [l.strip() for l in text.splitlines() if q.lower() in l.lower()]
            if q and lines:
                hits.append((p, lines[0]))
    body = f"<h1>Zoeken: {html.escape(q)}</h1>" + ("".join(f'<p><a href="/docs/{repo}/{quote(p)}">{html.escape(p)}</a><br><small>{html.escape(l[:200])}</small></p>' for p, l in hits) or "<p>Niets gevonden.</p>")
    return page(repo, paths, "Zoeken", body, q)


@router.get("/{owner}/{name}/_raw/{path:path}")
async def raw_file(owner: str, name: str, path: str):
    async with gitea() as g:
        r = await g.get(f"/api/v1/repos/{owner}/{name}/raw/{quote(path)}")
    if r.status_code != 200 or not path.startswith("docs/"):
        raise HTTPException(404)
    return Response(r.content, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")


@router.get("/{owner}/{name}/{path:path}", response_class=HTMLResponse)
async def view(owner: str, name: str, path: str):
    repo = f"{owner}/{name}"
    if not is_doc(path):
        raise HTTPException(404)
    async with gitea() as g:
        paths = await doc_paths(g, repo)
        text = await raw(g, repo, path)
        rendered = await g.post("/api/v1/markdown", json={"Text": rewrite_links(text, repo, path), "Mode": "gfm"})
        commits = (await g.get(f"/api/v1/repos/{repo}/commits", params={"path": path, "limit": 20, "stat": "false"})).json()
    tickets: dict[int, str] = {}
    for c in commits:
        for ref in parse_refs(c["commit"]["message"], legacy.load()):
            tickets.setdefault(ref.wp_id, c["commit"]["message"].splitlines()[0])
    back = "".join(f'<li><a href="{config.OP_PUBLIC_URL}/work_packages/{i}">OP#{i}</a> – {html.escape(m)}</li>' for i, m in tickets.items())
    aside = f"<aside><b>Gerelateerde werkpakketten</b><ul>{back}</ul></aside>" if back else ""
    # Gitea herschrijft afbeeldingsadressen naar zijn eigen /media/-URL; zet ze terug naar het portaal
    body = re.sub(r'src="[^"]*/media/(docs/[^"]+?/_raw/[^"]+)"', r'src="/\1"', rendered.text)
    return page(repo, paths, path, body + aside)
