from collections import defaultdict

from . import legacy
from .refs import parse_refs


async def build_release_notes(op, messages: list[str], title: str) -> str:
    ids = sorted({r.wp_id for m in messages for r in parse_refs(m, legacy.load())})
    groups: dict[str, list[str]] = defaultdict(list)
    for wp_id in ids:
        try:
            wp = await op.get_wp(wp_id)
            kind = wp["_links"]["type"]["title"]
            groups[kind].append(f"- {wp['subject']} (OP#{wp_id})")
        except Exception:
            groups["Other"].append(f"- OP#{wp_id}")
    lines = [f"# Release notes {title}", ""]
    for kind in sorted(groups):
        lines += [f"## {kind}", *groups[kind], ""]
    if not ids:
        lines.append("_No linked work packages found._")
    return "\n".join(lines)
