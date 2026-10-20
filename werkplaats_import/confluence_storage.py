"""Confluence storage-format (XHTML + ac:/ri:-macro's) -> HTML in de vorm van een HTML-export, zodat convert_space het kan verwerken."""
import html as htmllib
import re

from bs4 import BeautifulSoup

PANELS = {"info": "information", "note": "note", "warning": "warning", "tip": "tip"}
DROP = {"toc", "children", "pagetree", "recently-updated", "contentbylabel", "livesearch", "pagetreesearch"}


def storage_to_html(storage: str, page_id: str, title_to_file: dict[str, str], warnings: list[str], needed_files: set[str]) -> str:
    storage = re.sub(r"<!\[CDATA\[(.*?)\]\]>", lambda m: htmllib.escape(m.group(1)), storage, flags=re.S)
    soup = BeautifulSoup(storage, "html.parser")

    def new(name, **attrs):
        return soup.new_tag(name, **attrs)

    for m in soup.find_all("ac:structured-macro"):
        name = m.get("ac:name", "")
        params = {p.get("ac:name"): p.get_text() for p in m.find_all("ac:parameter")}
        if name == "code":
            pre = new("pre", **{"class": "syntaxhighlighter-pre", "data-syntaxhighlighter-params": f"brush: {params.get('language', '')}"})
            body = m.find("ac:plain-text-body")
            pre.string = body.get_text() if body else ""
            m.replace_with(pre)
        elif name in PANELS:
            outer = new("div", **{"class": f"confluence-information-macro confluence-information-macro-{PANELS[name]}"})
            inner = new("div", **{"class": "confluence-information-macro-body"})
            body = m.find("ac:rich-text-body")
            for c in list(body.children) if body else []:
                inner.append(c.extract())
            outer.append(inner)
            m.replace_with(outer)
        elif name in DROP:
            m.decompose()
        else:
            body = m.find("ac:rich-text-body")
            if body:
                warnings.append(f"macro '{name}' op pagina {page_id}: alleen de inhoud overgenomen")
                m.replace_with(*list(body.children))
            else:
                warnings.append(f"macro '{name}' op pagina {page_id} niet geconverteerd")
                note = new("p")
                note.string = f"[Confluence-macro '{name}' niet geconverteerd]"
                m.replace_with(note)

    for img in soup.find_all("ac:image"):
        att, url = img.find("ri:attachment"), img.find("ri:url")
        out = new("img")
        if att:
            fname = att["ri:filename"]
            needed_files.add(fname)
            out["src"] = f"attachments/{page_id}/{fname}"
        elif url:
            out["src"] = url["ri:value"]
        img.replace_with(out)

    for link in soup.find_all("ac:link"):
        body = link.find("ac:link-body") or link.find("ac:plain-text-link-body")
        text = body.get_text() if body else None
        page, att = link.find("ri:page"), link.find("ri:attachment")
        a = new("a")
        if page:
            target = page.get("ri:content-title", "")
            a["href"] = title_to_file.get(target, "")
            text = text or target
        elif att:
            needed_files.add(att["ri:filename"])
            a["href"] = f"attachments/{page_id}/{att['ri:filename']}"
            text = text or att["ri:filename"]
        a.string = text or "link"
        if not a.get("href"):
            link.replace_with(a.string)  # doelpagina onbekend: alleen tekst
        else:
            link.replace_with(a)

    for t in soup.find_all(re.compile(r"^(ac|ri):")):  # overige ac:-tags (task-list, placeholder, ...)
        t.unwrap()
    for t in soup.find_all("time"):
        t.replace_with(t.get("datetime", t.get_text()))
    return str(soup)
