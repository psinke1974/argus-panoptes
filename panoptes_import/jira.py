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
from . import op_names
from .op_names import candidates

# Jira value (lowercase) -> the standard OpenProject item it stands for (English name). The candidate names in every
# supported OpenProject UI language come from op_names.py, so installs in nl/de/fr/es/it/pt-BR are recognised too.
_TYPES = {"bug": "Bug", "story": "User story", "epic": "Epic", "task": "Task", "sub-task": "Task", "subtask": "Task",
          "feature": "Feature", "new feature": "Feature", "improvement": "Feature"}
_STATUSES = {"to do": "New", "open": "New", "backlog": "New", "new": "New", "selected for development": "Scheduled",
             "in progress": "In progress", "in development": "In progress", "in review": "In testing", "in test": "In testing",
             "testing": "In testing", "qa": "In testing", "done": "Closed", "closed": "Closed", "resolved": "Closed",
             "complete": "Closed", "rejected": "Rejected", "won't do": "Rejected", "on hold": "On hold", "blocked": "On hold"}
_PRIORITIES = {"highest": "Immediate", "blocker": "Immediate", "critical": "Immediate", "high": "High", "medium": "Normal",
               "low": "Low", "lowest": "Low", "major": "High", "minor": "Low", "trivial": "Low"}
TYPE_MAP = {k: candidates("types", v) for k, v in _TYPES.items()}
STATUS_MAP = {k: candidates("statuses", v) for k, v in _STATUSES.items()}
PRIO_MAP = {k: candidates("priorities", v) for k, v in _PRIORITIES.items()}


def with_custom_map(custom: dict | None) -> tuple[dict, dict, dict]:
    """Add the user's own Jira-value mappings (--map file) in front of the defaults.
    Format: {"types": {"spike": "Task"}, "statuses": {"waiting for customer": "On hold"}, "priorities": {"wishlist": "Low"}}.
    A value is the English name of a standard OpenProject item (translated automatically) or the exact name of one of your own."""
    custom = custom or {}
    out = []
    for kind, table in (("types", TYPE_MAP), ("statuses", STATUS_MAP), ("priorities", PRIO_MAP)):
        merged = dict(table)
        for jira_value, target in (custom.get(kind) or {}).items():
            try:
                merged[jira_value.lower()] = candidates(kind, target)
            except ValueError:  # not a standard English name: treat it as the exact name in this OpenProject
                merged[jira_value.lower()] = [target]
        out.append(merged)
    return tuple(out)


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
    worklogs: list[dict] = field(default_factory=list)  # {date 'YYYY-MM-DD', seconds, author, email, comment}
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


class UserResolver:
    """Maps a Jira person to an OpenProject user: the --user-map first (Jira name -> OpenProject login), then an exact
    e-mail match when Jira returned the address. (OpenProject's `name` filter does not search e-mail addresses, so the
    users are indexed once instead.)"""

    def __init__(self, op, by_name: dict[str, str]):
        self.op, self.by_name, self._by_email = op, by_name, None

    def href(self, name: str, email: str = "") -> str | None:
        if name in self.by_name:
            return self.by_name[name]
        if not email:
            return None
        if self._by_email is None:
            self._by_email = {(u.get("email") or "").lower(): u["_links"]["self"]["href"] for u in self.op.all_users() if u.get("email")}
        return self._by_email.get(email.lower())


def iso_duration(seconds: int) -> str:
    """Seconds -> ISO 8601 duration as OpenProject expects ('PT1H30M'); rounded to whole minutes, at least one minute."""
    minutes = max(1, round(seconds / 60))
    return f"PT{minutes // 60}H{minutes % 60}M"


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


def run(op, csv_path: str, project_name: str | None, identifier: str | None, user_map: dict[str, str], dry_run: bool, key_prefix: bool = True, state_dir: str = ".import-state", name_map: dict | None = None, add_members: bool = True, member_role: str = "Member") -> str:
    issues = read_csv(Path(csv_path).read_text(encoding="utf-8-sig"))
    return run_issues(op, issues, project_name, identifier, user_map, dry_run, key_prefix, state_dir, name_map=name_map, add_members=add_members, member_role=member_role)


def run_issues(op, issues: list[Issue], project_name: str | None, identifier: str | None, user_map: dict[str, str], dry_run: bool, key_prefix: bool = True, state_dir: str = ".import-state", download=None, max_attachment_mb: int = 50, name_map: dict | None = None, add_members: bool = True, member_role: str = "Member") -> str:
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

    type_map, status_map, prio_map = with_custom_map(name_map)
    plan = []
    for i in issues:
        t, te = pick(i.type, type_map, types, candidates("types", "Task"))
        s, se = pick(i.status, status_map, statuses, candidates("statuses", "New"))
        p, pe = pick(i.priority, prio_map, prios, candidates("priorities", "Normal")) if i.priority else (None, True)
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


    resolver = UserResolver(op, users)
    members: set[str] = set()
    role_href: list[str | None] = []

    def user_href(name: str, email: str = "") -> str | None:
        """Resolve a Jira person to an OpenProject user and make that user a member of the new project: OpenProject only
        accepts members as assignees. With --no-add-members nobody is linked (names go into the text instead)."""
        href = resolver.href(name, email)
        if not href or not add_members:
            return None
        if href not in members:
            if not role_href:
                roles = {r["name"]: r["_links"]["self"]["href"] for r in op.elements("/roles")}
                wanted = candidates("roles", member_role) if member_role in op_names.NAMES["en"]["roles"] else [member_role]
                role_href.append(next((roles[n] for n in wanted if n in roles), None))
                if role_href[0] is None:
                    report.append(f"- Project role '{member_role}' not found: people are not linked (use --member-role with the exact role name)")
            if role_href[0] is None:
                return None
            try:
                op.req("POST", "/memberships", json={"_links": {"project": {"href": f"/api/v3/projects/{pid}"}, "principal": {"href": href}, "roles": [{"href": role_href[0]}]}})
            except RuntimeError as e:
                if "already" not in str(e).lower() and "taken" not in str(e).lower():
                    report.append(f"- Could not add {name} to the project: {str(e)[:100]}")
                    return None
            members.add(href)
        return href

    def assignee_href(i: Issue) -> str | None:
        return user_href(i.assignee, i.assignee_email)

    me_href: list[str] = []  # the importing user: owner of time entries whose author has no OpenProject account

    by_key = {i.key: (i, t, s, p) for i, t, s, p in plan}
    created = skipped_files = uploaded = logged = 0
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
        for w in i.worklogs:
            try:
                if not me_href:
                    me_href.append(op.req("GET", "/users/me")["_links"]["self"]["href"])
                author = user_href(w["author"], w.get("email", ""))
                note = w["comment"].strip()
                text = f"[Jira worklog by {w['author']}] {jira_to_md(note)}".strip() if not author else jira_to_md(note)
                op.req("POST", "/time_entries", json={"spentOn": w["date"], "hours": iso_duration(w["seconds"]), "comment": {"raw": text},
                                                      "_links": {"entity": {"href": f"/api/v3/work_packages/{wp['id']}"}, "user": {"href": author or me_href[0]}}})
                logged += 1
            except Exception as e:  # time tracking may be disabled in the project; a failed entry must not stop the import
                report.append(f"- Worklog of {i.key} ({w['date']}, {w['author']}) skipped: {str(e)[:100]}")
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
    report.append(f"\nCreated: {created}, relations: {rels}, people added as project members: {len(members)}, time entries: {logged}, attachments: {uploaded} (skipped: {skipped_files}). Jira key -> OpenProject id mapping: {state_file}")
    report.append("Not imported: history, workflows, custom fields (only labels/sprints as text).")
    return "\n".join(report)
