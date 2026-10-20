import os
from pathlib import Path

import httpx


def load_env(path: str = ".env") -> dict:
    env = dict(os.environ)
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.split("   #")[0].strip())
    return env


class OP:
    def __init__(self, base: str, api_key: str, host: str | None = None):
        headers = {"Host": host} if host else {}
        self.http = httpx.Client(base_url=base.rstrip("/") + "/api/v3", auth=("apikey", api_key), headers=headers, timeout=60)

    @classmethod
    def from_env(cls, env: dict):
        return cls(env.get("OP_PUBLIC_URL_LOCAL", "http://localhost:8080"), env["OP_API_KEY"], env.get("OP_HOST_HEADER"))

    def req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def elements(self, path: str) -> list[dict]:
        return self.req("GET", path)["_embedded"]["elements"]
