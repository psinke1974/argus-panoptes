import re
from dataclasses import dataclass

_KW = r"(?:(?P<kw>fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed))\s+)?"
REF = re.compile(_KW + r"OP#(?P<id>\d+)", re.I)
LEGACY = re.compile(_KW + r"(?<![\w-])(?P<key>[A-Z][A-Z0-9_]+-\d+)(?![\w-])", re.I)


@dataclass(frozen=True)
class Ref:
    wp_id: int
    closes: bool


def parse_refs(text: str, legacy: dict[str, int] | None = None) -> list[Ref]:
    """Find OP#123 references and (given `legacy`) old Jira keys such as PROJ-12.
    'fixes OP#123' / 'fixes PROJ-12' closes the work package."""
    found: dict[int, bool] = {}

    def add(wp: int, kw) -> None:
        found[wp] = found.get(wp, False) or bool(kw)

    for m in REF.finditer(text or ""):
        add(int(m.group("id")), m.group("kw"))
    if legacy:
        for m in LEGACY.finditer(text or ""):
            wp = legacy.get(m.group("key").upper())
            if wp:
                add(wp, m.group("kw"))
    return [Ref(i, c) for i, c in found.items()]
