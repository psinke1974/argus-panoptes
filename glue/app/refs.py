import re
from dataclasses import dataclass

REF = re.compile(r"(?:(?P<kw>fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed))\s+)?OP#(?P<id>\d+)", re.I)


@dataclass(frozen=True)
class Ref:
    wp_id: int
    closes: bool


def parse_refs(text: str) -> list[Ref]:
    """Vind OP#123-verwijzingen; 'fixes OP#123' sluit het werkpakket af."""
    found: dict[int, bool] = {}
    for m in REF.finditer(text or ""):
        wp = int(m.group("id"))
        found[wp] = found.get(wp, False) or bool(m.group("kw"))
    return [Ref(i, c) for i, c in found.items()]
