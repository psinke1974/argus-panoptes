"""Old Jira keys (PROJ-12) -> OpenProject id, read from the importer's state files."""
import json
from pathlib import Path

from . import config

_cache: tuple[tuple, dict[str, int]] = ((), {})


def load() -> dict[str, int]:
    """All jira-*.json from LEGACY_KEYS_DIR; reloads when the files change."""
    global _cache
    files = sorted(Path(config.LEGACY_KEYS_DIR).glob("jira-*.json")) if config.LEGACY_KEYS_DIR else []
    sig = tuple((f.name, f.stat().st_mtime_ns) for f in files)
    if sig != _cache[0]:
        keys: dict[str, int] = {}
        for f in files:
            try:
                keys.update({k.upper(): int(v) for k, v in json.loads(f.read_text()).items() if not k.startswith("__")})
            except (ValueError, TypeError):
                continue
        _cache = (sig, keys)
    return _cache[1]
