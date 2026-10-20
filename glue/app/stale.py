from collections import defaultdict

from . import config


def stale_markdown(items: list[dict], project: str, days: int) -> str:
    """Group stale work packages by assignee so each person sees their own list."""
    lines = [f"# Stale work packages in {project}", f"Open, and untouched for more than {days} days: {len(items)}", ""]
    by_person: dict[str, list[dict]] = defaultdict(list)
    for i in items:
        by_person[i["assignee"]].append(i)
    for person in sorted(by_person, key=lambda p: (p == "unassigned", p)):
        lines.append(f"## {person}")
        lines += [f"- [OP#{i['id']}]({config.OP_PUBLIC_URL}/work_packages/{i['id']}) {i['subject']} ({i['type']}, {i['status']}, last update {i['updated']})" for i in by_person[person]]
        lines.append("")
    if not items:
        lines.append("_Nothing is stale. Nicely kept._")
    return "\n".join(lines)
