import httpx

from . import config


class OpenProject:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.http = client or httpx.AsyncClient(
            base_url=config.OP_URL, auth=("apikey", config.OP_API_KEY), timeout=15,
            headers={"Host": config.OP_HOST_HEADER}
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
