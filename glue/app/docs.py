"""Docs-in-repo: Markdown in git is the source; this portal shows it, searchable and with backlinks to work packages."""
import html
import mimetypes
from pathlib import Path
import posixpath
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from . import auth, config, legacy
from .refs import parse_refs

router = APIRouter(prefix="/docs")

STYLE = """:root{color-scheme:light dark;--bg:#fff;--fg:#1d2733;--muted:#556070;--panel:#f4f5f7;--link:#0b5cad;--line:#d8dde3}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--fg:#e6e9ee;--muted:#a3adba;--panel:#1d232d;--link:#7db4ff;--line:#2d3644}}
body{font:16px/1.6 system-ui;margin:0;display:flex;min-height:100vh;background:var(--bg);color:var(--fg)}
nav{width:260px;flex:none;background:var(--panel);padding:1rem;box-sizing:border-box;overflow-wrap:anywhere}
a{color:var(--link)}nav a{display:block;padding:.2rem 0;text-decoration:none}main{flex:1;min-width:0;max-width:820px;padding:1rem 2rem}
aside{border-top:1px solid var(--line);margin-top:2rem;padding-top:.5rem;font-size:.9rem;color:var(--muted)}
pre{background:var(--panel);padding:.8rem;overflow:auto;border-radius:4px}code{font-size:.92em}img{max-width:100%}
table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:.3rem .6rem}blockquote{margin-left:0;padding-left:1rem;border-left:3px solid var(--line);color:var(--muted)}
input{width:100%;box-sizing:border-box;padding:.3rem;margin-bottom:.6rem;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px}
.crumbs{font-size:.9rem;color:var(--muted);margin-bottom:1rem}.crumbs a{text-decoration:none}
.toc{background:var(--panel);border-radius:6px;padding:.6rem 1rem;margin:0 0 1.2rem;font-size:.92rem}.toc ul{margin:.3rem 0;padding-left:1.1rem;list-style:none}
.toc .l3{margin-left:1rem}.toc .l4{margin-left:2rem}pre.mermaid{background:none;text-align:center}
@media (max-width:800px){body{flex-direction:column}nav{width:auto}main{padding:1rem}}"""


def is_doc(path: str) -> bool:
    return path.lower().endswith(".md") and (path.startswith("docs/") or "/" not in path)


def doc_url(repo: str, path: str) -> str:
    return f"{config.PUBLIC_URL}/docs/{repo}/{quote(path)}"


def changed_docs(commit: dict) -> list[str]:
    files = (commit.get("added") or []) + (commit.get("modified") or [])
    return sorted({f for f in files if is_doc(f)})


_LINK = re.compile(r"(!?\[[^\]]*\])\((?!https?:|mailto:|#|/)([^)\s]+)\)")


def rewrite_links(md: str, repo: str, path: str) -> str:
    """Relative links in a doc -> portal URLs (.md = page, anything else = file via /_raw/)."""
    def fix(m):
        target, _, frag = m.group(2).partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        kind = "" if resolved.lower().endswith(".md") else "_raw/"
        return f"{m.group(1)}(/docs/{repo}/{kind}{quote(resolved)}{'#' + frag if frag else ''})"

    return _LINK.sub(fix, md)


_HEADING = re.compile(r'<h([2-4]) id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
_MERMAID = re.compile(r'<div class="code-block-container[^"]*"><pre class="code-block[^"]*"><code class="chroma language-mermaid[^"]*">(.*?)</code></pre></div>', re.S)


def build_toc(body_html: str, minimum: int = 3) -> str:
    """'On this page' list from the h2-h4 headings (Gitea gives them ids); empty for short pages."""
    items = [(int(m.group(1)), m.group(2), html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()) for m in _HEADING.finditer(body_html)]
    if len(items) < minimum:
        return ""
    lis = "".join(f'<li class="l{lvl}"><a href="#{html.escape(hid)}">{html.escape(text)}</a></li>' for lvl, hid, text in items)
    return f'<nav class="toc"><b>On this page</b><ul>{lis}</ul></nav>'


def breadcrumbs(repo: str, path: str, paths: list[str]) -> str:
    """repo > docs > home > architecture > database: folders link to the page that explains them when there is one
    (home.md for the folder home/, README.md or index.md inside it), so imported Confluence trees stay navigable."""
    parts = path.split("/")
    crumbs = [f'<a href="/docs/{repo}">{html.escape(repo)}</a>']
    known = set(paths)
    for i, seg in enumerate(parts[:-1]):
        folder = "/".join(parts[: i + 1])
        target = next((c for c in (f"{folder}.md", f"{folder}/README.md", f"{folder}/index.md") if c in known), None)
        crumbs.append(f'<a href="/docs/{repo}/{quote(target)}">{html.escape(seg)}</a>' if target else html.escape(seg))
    crumbs.append(f"<b>{html.escape(parts[-1].removesuffix('.md'))}</b>")
    return '<div class="crumbs">' + " › ".join(crumbs) + "</div>"


def mermaid_src() -> str:
    """Where the browser gets mermaid.js: a configured URL, else our own copy when present, else nothing (diagrams stay code)."""
    if config.MERMAID_JS_URL:
        return config.MERMAID_JS_URL
    return "/docs/_assets/mermaid.js" if Path(config.MERMAID_JS_PATH).is_file() else ""


def mermaidify(body_html: str) -> tuple[str, bool]:
    """Turn Gitea's inert mermaid code blocks into <pre class="mermaid"> (the source stays HTML-escaped text)."""
    out, n = _MERMAID.subn(lambda m: f'<pre class="mermaid">{m.group(1)}</pre>', body_html)
    return out, n > 0


def page(repo: str, nav: list[str], title: str, body: str, q: str = "", head: str = "") -> HTMLResponse:
    links = "".join(f'<a href="/docs/{repo}/{quote(p)}">{html.escape(p)}</a>' for p in nav)
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><style>{STYLE}</style>"
        f'<nav><b>{html.escape(repo)}</b><form action="/docs/{repo}/search"><input name=q placeholder="Search…" value="{html.escape(q)}"></form>{links}</nav>'
        f"<main>{body}</main>{head}"
    )


def gitea() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.GITEA_URL, headers={"Authorization": f"token {config.GITEA_TOKEN}"}, timeout=20)


async def doc_paths(g: httpx.AsyncClient, repo: str) -> list[str]:
    info = await g.get(f"/api/v1/repos/{repo}")
    if info.status_code != 200:
        raise HTTPException(404, "repo not found")
    tree = await g.get(f"/api/v1/repos/{repo}/git/trees/{info.json()['default_branch']}", params={"recursive": "true", "per_page": 1000})
    return sorted(t["path"] for t in tree.json().get("tree", []) if t["type"] == "blob" and is_doc(t["path"]))


async def raw(g: httpx.AsyncClient, repo: str, path: str) -> str:
    r = await g.get(f"/api/v1/repos/{repo}/raw/{quote(path)}")
    if r.status_code != 200:
        raise HTTPException(404, "page not found")
    return r.text


@router.get("/_assets/mermaid.js")
async def mermaid_js():
    """Our own copy of mermaid.js (see scripts/fetch-mermaid.sh); nothing is fetched from a CDN."""
    f = Path(config.MERMAID_JS_PATH)
    if not f.is_file():
        raise HTTPException(404)
    return Response(f.read_bytes(), media_type="text/javascript", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{owner}/{name}", response_class=HTMLResponse, dependencies=[Depends(auth.repo_access)])
async def index(owner: str, name: str):
    repo = f"{owner}/{name}"
    async with gitea() as g:
        paths = await doc_paths(g, repo)
    body = "<h1>Documentation</h1>" + ("".join(f'<p><a href="/docs/{repo}/{quote(p)}">{html.escape(p)}</a></p>' for p in paths) or "<p>No .md files in <code>docs/</code> or the repository root.</p>")
    return page(repo, paths, f"Docs {repo}", body)


@router.get("/{owner}/{name}/search", response_class=HTMLResponse, dependencies=[Depends(auth.repo_access)])
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
    body = f"<h1>Search: {html.escape(q)}</h1>" + ("".join(f'<p><a href="/docs/{repo}/{quote(p)}">{html.escape(p)}</a><br><small>{html.escape(l[:200])}</small></p>' for p, l in hits) or "<p>Nothing found.</p>")
    return page(repo, paths, "Search", body, q)


@router.get("/{owner}/{name}/_raw/{path:path}", dependencies=[Depends(auth.repo_access)])
async def raw_file(owner: str, name: str, path: str):
    async with gitea() as g:
        r = await g.get(f"/api/v1/repos/{owner}/{name}/raw/{quote(path)}")
    if r.status_code != 200 or not path.startswith("docs/"):
        raise HTTPException(404)
    return Response(r.content, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")


@router.get("/{owner}/{name}/{path:path}", response_class=HTMLResponse, dependencies=[Depends(auth.repo_access)])
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
    aside = f"<aside><b>Related work packages</b><ul>{back}</ul></aside>" if back else ""
    # Gitea rewrites image URLs to its own /media/ URL; point them back at the portal
    body = re.sub(r'src="[^"]*/media/(docs/[^"]+?/_raw/[^"]+)"', r'src="/\1"', rendered.text)
    head = ""
    if src := mermaid_src():
        body, has_diagrams = mermaidify(body)
        if has_diagrams:  # strict: diagram labels cannot inject HTML or run click handlers
            head = f'<script src="{html.escape(src)}"></script><script>mermaid.initialize({{startOnLoad: true, securityLevel: "strict", theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default"}});</script>'
    return page(repo, paths, path, breadcrumbs(repo, path, paths) + build_toc(body) + body + aside, head=head)
