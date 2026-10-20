import json

import httpx

from . import config


class OpenProject:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.http = client or httpx.AsyncClient(
            base_url=config.OP_URL, auth=("apikey", config.OP_API_KEY), timeout=15,
            headers={"Host": config.OP_HOST_HEADER, **({"X-Forwarded-Proto": config.OP_FORWARDED_PROTO} if config.OP_FORWARDED_PROTO else {})}
        )

    async def get_wp(self, wp_id: int) -> dict:
        r = await self.http.get(f"/api/v3/work_packages/{wp_id}")
        r.raise_for_status()
        return r.json()

    async def comment(self, wp_id: int, text: str) -> None:
        r = await self.http.post(
            f"/api/v3/work_packages/{wp_id}/activities", json={"comment": {"raw": text}}
        )
        r.raise_for_status()

    async def status_href(self, name: str) -> str | None:
        r = await self.http.get("/api/v3/statuses")
        r.raise_for_status()
        for s in r.json()["_embedded"]["elements"]:
            if s["name"].lower() == name.lower():
                return s["_links"]["self"]["href"]
        return None

    async def set_status(self, wp_id: int, status_name: str) -> bool:
        href = await self.status_href(status_name)
        if not href:
            return False
        wp = await self.get_wp(wp_id)
        if wp["_links"]["status"]["href"] == href:
            return True
        r = await self.http.patch(
            f"/api/v3/work_packages/{wp_id}",
            json={"lockVersion": wp["lockVersion"], "_links": {"status": {"href": href}}},
        )
        r.raise_for_status()
        return True

    async def stale(self, project: str, days: int) -> list[dict]:
        """Open work packages in `project` (identifier) without any update for more than `days` days.
        Comments and the glue's commit/PR notes count as updates, so git activity keeps tickets fresh."""
        filters = [{"status": {"operator": "o", "values": None}}, {"updatedAt": {"operator": "<t-", "values": [str(days)]}}]
        r = await self.http.get(f"/api/v3/projects/{project}/work_packages",
                                params={"filters": json.dumps(filters), "pageSize": 200, "sortBy": json.dumps([["updatedAt", "asc"]])})
        r.raise_for_status()
        return [{"id": w["id"], "subject": w["subject"], "status": w["_links"]["status"]["title"],
                 "type": w["_links"]["type"]["title"], "assignee": (w["_links"].get("assignee") or {}).get("title") or "unassigned",
                 "updated": w["updatedAt"][:10]} for w in r.json()["_embedded"]["elements"]]
