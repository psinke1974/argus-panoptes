"""Confluence HTML-space-export (zip of map) -> Markdown-bestanden in docs/ (met boomstructuur en bijlagen)."""
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


def slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower(), flags=re.U)
    return re.sub(r"[\s_-]+", "-", s).strip("-") or "pagina"


class Conv(MarkdownConverter):
    def convert_pre(self, el, text, parent_tags=None, **kw):
        params = el.get("data-syntaxhighlighter-params", "")
        m = re.search(r"brush:\s*(\w+)", params)
        return f"\n```{m.group(1) if m else ''}\n{el.get_text().strip(chr(10))}\n```\n"


PANEL = {"confluence-information-macro-information": "Info", "confluence-information-macro-note": "Let op",
         "confluence-information-macro-warning": "Waarschuwing", "confluence-information-macro-tip": "Tip"}


def prepare(main: BeautifulSoup) -> None:
    for div in main.select("div.confluence-information-macro"):
        label = next((v for c, v in PANEL.items() if c in div.get("class", [])), "Opmerking")
        body = div.select_one(".confluence-information-macro-body")
        quote = main.new_tag("blockquote")
        quote.append(BeautifulSoup(f"<p><b>{label}</b></p>", "html.parser"))
        quote.append(BeautifulSoup(str(body) if body else "", "html.parser"))
        div.replace_with(quote)
    for sel in (".page-metadata", ".pageSection.group", "script", "style"):
        for e in main.select(sel):
            e.decompose()


def read_tree(index_html: str) -> list[tuple[str, int]]:
    """[(html-bestandsnaam, diepte)] uit de paginaboom van index.html."""
    soup = BeautifulSoup(index_html, "html.parser")
    root = soup.select_one("#content ul") or soup.find("ul")
    out: list[tuple[str, int]] = []

    def walk(ul, depth):
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", href=True, recursive=False) or li.find("a", href=True)
            if a and a["href"].endswith(".html"):
                out.append((a["href"], depth))
            sub = li.find("ul", recursive=False)
            if sub:
                walk(sub, depth + 1)

    if root:
        walk(root, 0)
    return out


def convert_space(src: Path, dest: Path, space_name: str | None = None) -> list[str]:
    """Schrijf Markdown naar dest/docs. Geeft lijst met geschreven paginapaden."""
    tree = read_tree((src / "index.html").read_text(encoding="utf-8")) if (src / "index.html").exists() else []
    if not tree:
        tree = [(p.name, 0) for p in sorted(src.glob("*.html")) if p.name != "index.html"]
    titles, outpaths, stack = {}, {}, []
    for fname, depth in tree:
        soup = BeautifulSoup((src / fname).read_text(encoding="utf-8"), "html.parser")
        h1 = soup.select_one("#title-heading") or soup.find("h1")
        title = (soup.title.string.split(" : ", 1)[-1] if soup.title and soup.title.string else fname[:-5]).strip()
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(strip=True)
        titles[fname] = title
        stack = stack[:depth] + [slug(title)]
        outpaths[fname] = "docs/" + "/".join(stack) + ".md"
    written, used = [], set()
    for fname, depth in tree:
        out = outpaths[fname]
        if out in used:  # dubbele titels
            out = out[:-3] + f"-{fname[:-5].rsplit('_', 1)[-1]}.md"
            outpaths[fname] = out
        used.add(out)
    for fname, depth in tree:
        soup = BeautifulSoup((src / fname).read_text(encoding="utf-8"), "html.parser")
        main = soup.select_one("#main-content") or soup.body or soup
        prepare(main)
        out = outpaths[fname]
        for tag, attr in (("a", "href"), ("img", "src")):
            for el in main.find_all(tag, **{attr: True}):
                target = el[attr].split("#")[0]
                if target in outpaths:
                    el[attr] = posixpath.relpath(outpaths[target], posixpath.dirname(out))
                elif target and not re.match(r"^(https?:|mailto:|#)", el[attr]) and (src / target).is_file():
                    dst = "docs/_bijlagen/" + target.replace("..", "")
                    (dest / dst).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src / target, dest / dst)
                    el[attr] = posixpath.relpath(dst, posixpath.dirname(out))
        md = Conv(heading_style="ATX", bullets="-").convert_soup(main)
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        target_file = dest / out
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(f"# {titles[fname]}\n\n{md}\n\n---\n*Geïmporteerd uit Confluence{' (' + space_name + ')' if space_name else ''}*\n", encoding="utf-8")
        written.append(out)
    lines = [f"# Documentatie{' ' + space_name if space_name else ''}", ""]
    for fname, depth in tree:
        lines.append(f"{'  ' * depth}- [{titles[fname]}]({posixpath.relpath(outpaths[fname], 'docs')})")
    (dest / "docs").mkdir(parents=True, exist_ok=True)
    (dest / "docs" / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def open_export(path: str, workdir: Path) -> Path:
    p = Path(path)
    if p.is_dir():
        return p
    with zipfile.ZipFile(p) as z:
        z.extractall(workdir)
    found = next(workdir.rglob("index.html"), None)
    return found.parent if found else workdir


def publish(dest: Path, gitea_url: str, token: str, repo: str, httpx_client) -> str:
    """Maak repo aan indien nodig en push dest als commit op main."""
    owner, name = repo.split("/")
    h = {"Authorization": f"token {token}"}
    if httpx_client.get(f"{gitea_url}/api/v1/repos/{repo}", headers=h).status_code == 404:
        r = httpx_client.post(f"{gitea_url}/api/v1/user/repos", headers=h, json={"name": name, "default_branch": "main"})
        r.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp:
        git = lambda *a: subprocess.run(["git", "-c", f"http.extraHeader=Authorization: token {token}", "-c", "user.name=Werkplaats import", "-c", "user.email=import@werkplaats.local", *a], cwd=tmp, check=True, capture_output=True, text=True)
        url = f"{gitea_url}/{repo}.git"
        try:
            git("clone", url, ".")
        except subprocess.CalledProcessError:
            git("init", "-b", "main")
            git("remote", "add", "origin", url)
        shutil.copytree(dest / "docs", Path(tmp) / "docs", dirs_exist_ok=True)
        git("add", "-A")
        if not git("status", "--porcelain").stdout.strip():
            return "geen wijzigingen"
        git("commit", "-m", "Import Confluence-space")
        git("push", "-u", "origin", "HEAD:main")
    return f"{gitea_url}/{repo}"
