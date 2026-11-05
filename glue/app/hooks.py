"""Git host webhooks -> work package updates.

Gitea, Forgejo and GitHub send the same payload shape (Gitea's is GitHub-compatible); GitLab has its own, so its events are
normalized first. Both end in two actions: a push (commits that mention work packages) and a change request (PR/MR opened or merged)."""
import hashlib
import hmac

from . import config, legacy
from .docs import changed_docs, doc_url
from .refs import parse_refs


def signature_ok(body: bytes, provided: str | None) -> bool:
    """HMAC-SHA256 of the body with WEBHOOK_SECRET; accepts a bare hex digest (Gitea/Forgejo) or 'sha256=<hex>' (GitHub)."""
    if not config.WEBHOOK_SECRET:
        return False
    expected = hmac.new(config.WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (provided or "").removeprefix("sha256="))


def token_ok(provided: str | None) -> bool:
    """GitLab sends the shared secret as-is in X-Gitlab-Token."""
    return bool(config.WEBHOOK_SECRET) and hmac.compare_digest(config.WEBHOOK_SECRET, provided or "")


async def handle_push(op, repo: str, on_default: bool, commits: list[dict], doc_links: bool) -> int:
    """commits: {message, url, author_name, files_changed (for doc links)}"""
    handled = 0
    for c in commits:
        for ref in parse_refs(c["message"], legacy.load()):
            first = c["message"].splitlines()[0]
            docs = "".join(f"\n- Documentation: [{d}]({doc_url(repo, d)})" for d in changed_docs(c)) if doc_links else ""
            await op.comment(ref.wp_id, f"Commit in **{repo}**: [{first}]({c['url']}) by {c['author_name']}{docs}")
            if ref.closes and on_default:
                await op.set_status(ref.wp_id, config.STATUS_CLOSED)
            handled += 1
    return handled


async def handle_change_request(op, repo: str, action: str, title: str, body: str, url: str, kind: str = "Pull request") -> int:
    """action: 'opened' or 'merged' (anything else is ignored)."""
    handled = 0
    for ref in parse_refs(f"{title}\n{body or ''}", legacy.load()):
        if action == "opened":
            await op.comment(ref.wp_id, f"{kind} opened in **{repo}**: [{title}]({url})")
            await op.set_status(ref.wp_id, config.STATUS_IN_PROGRESS)
        elif action == "merged":
            await op.comment(ref.wp_id, f"{kind} merged: [{title}]({url})")
            if ref.closes:
                await op.set_status(ref.wp_id, config.STATUS_CLOSED)
        else:
            continue
        handled += 1
    return handled


async def handle_github_like(op, event: str, p: dict, doc_links: bool) -> int:
    """Gitea, Forgejo and GitHub payloads."""
    repo = p.get("repository", {}).get("full_name", "?")
    if event == "push":
        default = p["repository"].get("default_branch", "main")
        commits = [{**c, "author_name": (c.get("author") or {}).get("name", "?")} for c in p.get("commits", [])]
        return await handle_push(op, repo, p.get("ref") == f"refs/heads/{default}", commits, doc_links)
    if event == "pull_request":
        pr, action = p["pull_request"], p.get("action")
        state = "opened" if action == "opened" else "merged" if action == "closed" and pr.get("merged") else ""
        return await handle_change_request(op, repo, state, pr["title"], pr.get("body") or "", pr["html_url"])
    return 0


async def handle_gitlab(op, event: str, p: dict) -> int:
    """GitLab 'Push Hook' and 'Merge Request Hook' payloads. (The docs portal reads Gitea, so no doc links.)"""
    project = p.get("project", {})
    repo = project.get("path_with_namespace", "?")
    if event == "Push Hook":
        default = project.get("default_branch", "main")
        commits = [{"message": c["message"], "url": c["url"], "author_name": (c.get("author") or {}).get("name", "?")} for c in p.get("commits", [])]
        return await handle_push(op, repo, p.get("ref") == f"refs/heads/{default}", commits, doc_links=False)
    if event == "Merge Request Hook":
        a = p.get("object_attributes", {})
        state = {"open": "opened", "merge": "merged"}.get(a.get("action", ""), "")
        return await handle_change_request(op, repo, state, a.get("title", ""), a.get("description") or "", a.get("url", ""), kind="Merge request")
    return 0
