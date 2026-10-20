"""Jira CSV export -> OpenProject work packages (incl. hierarchy, relations, comments)."""
import csv
import io
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .jira_markup import jira_to_md

# Jira value (lowercase) -> candidate names in OpenProject (English first, then Dutch for NL installs)
TYPE_MAP = {"bug": ["Bug", "Fout"], "story": ["User story", "Gebruikersverhaal"], "epic": ["Epic", "Episch"],
            "task": ["Task", "Taak"], "sub-task": ["Task", "Taak"], "subtask": ["Task", "Taak"],
            "feature": ["Feature", "Functie"], "new feature": ["Feature", "Functie"], "improvement": ["Feature", "Functie"]}
STATUS_MAP = {"to do": ["New", "Nieuw"], "open": ["New", "Nieuw"], "backlog": ["New", "Nieuw"], "new": ["New", "Nieuw"],
              "selected for development": ["Scheduled", "Gepland"], "in progress": ["In progress", "In behandeling"],
              "in development": ["In progress", "In behandeling"], "in review": ["In testing", "In test"],
              "in test": ["In testing", "In test"], "testing": ["In testing", "In test"], "qa": ["In testing", "In test"],
              "done": ["Closed", "Gesloten"], "closed": ["Closed", "Gesloten"], "resolved": ["Closed", "Gesloten"],
              "complete": ["Closed", "Gesloten"], "rejected": ["Rejected", "Afgewezen"], "won't do": ["Rejected", "Afgewezen"],
              "on hold": ["On hold", "In de wacht"], "blocked": ["On hold", "In de wacht"]}
PRIO_MAP = {"highest": ["Immediate", "Onmiddellijk"], "blocker": ["Immediate", "Onmiddellijk"], "critical": ["Immediate", "Onmiddellijk"],
            "high": ["High", "Hoog"], "medium": ["Normal", "Normaal"], "low": ["Low", "Laag"], "lowest": ["Low", "Laag"],
            "major": ["High", "Hoog"], "minor": ["Low", "Laag"], "trivial": ["Low", "Laag"]}
REL_MAP = {"blocks": "blocks", "duplicate": "duplicates", "duplicates": "duplicates", "cloners": "relates", "relates": "relates"}
KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


@dataclass
class Issue:
    key: str
    id: str
    summary: str
    type: str
    status: str
    priority: str
    assignee: str
    reporter: str
    created: str
    description: str
    parent: str | None
    labels: list[str]
    sprints: list[str]
    comments: list[tuple[str, str, str]]  # (date, author, text)
    links: list[tuple[str, str]]  # (Jira link type, target key)
    assignee_email: str = ""
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (file name, download URL)
    extra: dict = field(default_factory=dict)


def read_csv(text: str) -> list[Issue]:
    rows = list(csv.reader(io.StringIO(text)))
    header, data = rows[0], rows[1:]
    out, by_id = [], {}
    for r in data:
        cols = defaultdict(list)
        for h, v in zip(header, r):
            if v != "":
                cols[h].append(v)
        one = lambda h: (cols[h][0] if cols.get(h) else "")
        links = [(re.search(r"\((.*)\)", h).group(1).lower(), v) for h in header if h.startswith("Outward issue link") for v in cols.get(h, [])]
        parent = one("Parent") or one("Custom field (Epic Link)") or None
        issue = Issue(one("Issue key"), one("Issue id"), one("Summary"), one("Issue Type"), one("Status"), one("Priority"),
                      one("Assignee"), one("Reporter"), one("Created"), one("Description"), parent,
                      cols.get("Labels", []), cols.get("Sprint", []), [parse_comment(c) for c in cols.get("Comment", [])], links)
        if one("Parent id"):
            issue.extra["parent_id"] = one("Parent id")
        by_id[issue.id] = issue.key
        out.append(issue)
    for i in out:  # 'Parent id' -> key
        if i.parent is None and i.extra.get("parent_id") in by_id:
            i.parent = by_id[i.extra["parent_id"]]
    return [i for i in out if i.key]


def parse_comment(raw: str) -> tuple[str, str, str]:
    """'20/Oct/26 3:04 PM;accountid;text' -> (date, author id, text)"""
    parts = raw.split(";", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else ("", "", raw)


def order_parents_first(issues: list[Issue]) -> list[Issue]:
    keys = {i.key: i for i in issues}
    depth = {}

    def d(k, seen=()):
        if k in depth:
            return depth[k]
        p = keys[k].parent
        depth[k] = 0 if (not p or p not in keys or p in seen) else d(p, seen + (k,)) + 1
        return depth[k]

    return sorted(issues, key=lambda i: d(i.key))


def pick(name: str, table: dict, actual: dict[str, str], fallback: list[str] | None = None) -> tuple[str | None, bool]:
    """Return (href, exact) for Jira value `name`; exact=False if a fallback was used."""
    low = {k.lower(): v for k, v in actual.items()}
    for cand in table.get(name.lower(), []):
        if cand.lower() in low:
            return low[cand.lower()], True
    if name.lower() in low:
        return low[name.lower()], True
    return next((low[f.lower()] for f in fallback or [] if f.lower() in low), None), False


def ensure_types(op, identifier: str, pid: int, types: dict) -> None:
    """New projects only have a few types enabled; the API cannot change that, so use the admin script."""
    enabled = lambda: {t["name"] for t in op.elements(f"/projects/{pid}/types")}
    if set(types) <= enabled():
        return
    script = Path(__file__).resolve().parent.parent / "scripts" / "op-enable-types.sh"
    subprocess.run([str(script), identifier], check=False, capture_output=True)
    if not set(types) <= enabled():
        raise SystemExit(f"Work package types are not enabled in project {identifier}. Run: scripts/op-enable-types.sh {identifier}")


def footer(i: Issue, op_user: str | None) -> str:
    bits = [f"Imported from Jira: **{i.key}**"]
    if i.reporter:
        bits.append(f"reporter: {i.reporter}")
    if i.created:
        bits.append(f"created: {i.created}")
    if i.assignee and not op_user:
        bits.append(f"assignee (Jira): {i.assignee}")
    if i.labels:
        bits.append("labels: " + ", ".join(i.labels))
    if i.sprints:
        bits.append("sprints: " + ", ".join(i.sprints))
    return "\n\n---\n*" + " · ".join(bits) + "*"


def run(op, csv_path: str, project_name: str | None, identifier: str | None, user_map: dict[str, str], dry_run: bool, key_prefix: bool = True, state_dir: str = ".import-state") -> str:
    issues = read_csv(Path(csv_path).read_text(encoding="utf-8-sig"))
    return run_issues(op, issues, project_name, identifier, user_map, dry_run, key_prefix, state_dir)


def run_issues(op, issues: list[Issue], project_name: str | None, identifier: str | None, user_map: dict[str, str], dry_run: bool, key_prefix: bool = True, state_dir: str = ".import-state", download=None, max_attachment_mb: int = 50) -> str:
    """Shared import for CSV and API. `download(url) -> bytes` enables attachment import."""
    if not issues:
        raise SystemExit("no issues found")
    pkey = issues[0].key.rsplit("-", 1)[0]
    identifier = identifier or pkey.lower().replace("_", "-")
    state_file = Path(state_dir) / f"jira-{identifier}.json"
    state: dict = json.loads(state_file.read_text()) if state_file.exists() else {}
    done_rels: list[str] = state.pop("__relations__", [])
    report = [f"# Jira import {pkey} -> {identifier}", f"{len(issues)} issues, {len(state)} imported before", ""]
    save = lambda: (state_file.parent.mkdir(exist_ok=True), state_file.write_text(json.dumps({**state, "__relations__": done_rels}, indent=1)))
    warn: dict[str, set] = defaultdict(set)

    types = {t["name"]: t["_links"]["self"]["href"] for t in op.elements("/types")}
    statuses = {t["name"]: t["_links"]["self"]["href"] for t in op.elements("/statuses")}
    prios = {t["name"]: t["_links"]["self"]["href"] for t in op.elements("/priorities")}

    plan = []
    for i in issues:
        t, te = pick(i.type, TYPE_MAP, types, ["Task", "Taak"])
        s, se = pick(i.status, STATUS_MAP, statuses, ["New", "Nieuw"])
        p, pe = pick(i.priority, PRIO_MAP, prios, ["Normal", "Normaal"]) if i.priority else (None, True)
        for ok, kind, val in ((te, "type", i.type), (se, "status", i.status), (pe, "priority", i.priority)):
            if not ok:
                warn[kind].add(val)
        plan.append((i, t, s, p))
    for kind, vals in warn.items():
        report.append(f"- No exact mapping for {kind}: {', '.join(sorted(vals))} (fallback used)")
    if dry_run:
        report.append(f"\nDry run: {len(issues) - len(state)} work packages would be created.")
        return "\n".join(report)

    try:
        project = op.req("GET", f"/projects/{identifier}")
    except RuntimeError:
        project = op.req("POST", "/projects", json={"name": project_name or pkey, "identifier": identifier})
    pid = project["id"]
    ensure_types(op, identifier, pid, types)

    users = {}
    for jira_name, login in user_map.items():
        found = op.elements(f"/users?filters={json.dumps([{'login': {'operator': '=', 'values': [login]}}])}")
        if found:
            users[jira_name] = found[0]["_links"]["self"]["href"]

    email_cache: dict[str, str | None] = {}

    def assignee_href(i: Issue) -> str | None:
        if i.assignee in users:
            return users[i.assignee]
        if i.assignee_email:  # the API sometimes returns the e-mail address: match users on e-mail automatically
            if i.assignee_email not in email_cache:
                found = op.elements(f"/users?filters={json.dumps([{'name': {'operator': '~', 'values': [i.assignee_email]}}])}")
                match = [u for u in found if (u.get("email") or "").lower() == i.assignee_email.lower()]
                email_cache[i.assignee_email] = match[0]["_links"]["self"]["href"] if match else None
            return email_cache[i.assignee_email]
        return None

    by_key = {i.key: (i, t, s, p) for i, t, s, p in plan}
    created = skipped_files = uploaded = 0
    for i in order_parents_first([x[0] for x in plan]):
        if i.key in state:
            continue
        _, t, s, p = by_key[i.key]
        links = {"type": {"href": t}, "status": {"href": s}}
        if p:
            links["priority"] = {"href": p}
        a_href = assignee_href(i)
        if a_href:
            links["assignee"] = {"href": a_href}
        if i.parent in state:
            links["parent"] = {"href": f"/api/v3/work_packages/{state[i.parent]}"}
        subject = f"[{i.key}] {i.summary}" if key_prefix else i.summary
        body = jira_to_md(i.description) + footer(i, a_href)
        wp = op.req("POST", f"/projects/{pid}/work_packages", json={"subject": subject, "description": {"raw": body}, "_links": links})
        state[i.key] = wp["id"]
        created += 1
        for date, author, text in i.comments:
            who = f" by {author}" if author else ""
            op.req("POST", f"/work_packages/{wp['id']}/activities", json={"comment": {"raw": f"*Jira comment{who} ({date}):*\n\n{jira_to_md(text)}"}})
        for fname, url in (i.attachments if download else []):
            try:
                data = download(url)
                if len(data) > max_attachment_mb * 1024 * 1024:
                    raise RuntimeError(f"larger than {max_attachment_mb} MB")
                op.upload_attachment(wp["id"], fname, data)
                uploaded += 1
            except Exception as e:  # a failed attachment must not stop the import
                skipped_files += 1
                report.append(f"- Attachment {fname} of {i.key} skipped: {str(e)[:100]}")
        save()

    rels = 0
    for i in issues:
        for ltype, target in i.links:
            tag = f"{i.key}|{ltype}|{target}"
            if i.key in state and target in state and tag not in done_rels:
                try:
                    op.req("POST", f"/work_packages/{state[i.key]}/relations", json={"type": REL_MAP.get(ltype, "relates"), "_links": {"to": {"href": f"/api/v3/work_packages/{state[target]}"}}})
                    rels += 1
                    done_rels.append(tag)
                except RuntimeError as e:
                    report.append(f"- Relation {i.key} {ltype} {target} skipped: {str(e)[:100]}")
    save()
    report.append(f"\nCreated: {created}, relations: {rels}, attachments: {uploaded} (skipped: {skipped_files}). Jira key -> OpenProject id mapping: {state_file}")
    report.append("Not imported: worklogs, history, workflows, custom fields (only labels/sprints as text).")
    return "\n".join(report)
