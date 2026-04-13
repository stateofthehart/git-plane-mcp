"""Plane API operations.

Thin wrapper around Plane's REST API for issue management.
Uses httpx for HTTP calls. No MCP dependency — pure API client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass
class PlaneConfig:
    """Plane connection configuration from environment."""
    api_key: str
    base_url: str
    workspace_slug: str

    @classmethod
    def from_env(cls) -> PlaneConfig:
        api_key = os.environ.get("PLANE_API_KEY", "")
        base_url = os.environ.get("PLANE_BASE_URL", "http://localhost:8585")
        workspace = os.environ.get("PLANE_WORKSPACE_SLUG", "homelab")
        return cls(api_key=api_key, base_url=base_url.rstrip("/"),
                   workspace_slug=workspace)


class PlaneClient:
    """Synchronous Plane API client."""

    def __init__(self, config: PlaneConfig | None = None):
        self.config = config or PlaneConfig.from_env()
        self._client = httpx.Client(
            base_url=f"{self.config.base_url}/api/v1/workspaces/{self.config.workspace_slug}",
            headers={
                "X-API-Key": self.config.api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self) -> list[dict]:
        """List all projects in the workspace."""
        resp = self._client.get("/projects/")
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", data) if isinstance(data, dict) else data

    def get_project_by_identifier(self, identifier: str) -> dict | None:
        """Find a project by its short identifier (e.g., 'QFM')."""
        projects = self.list_projects()
        for p in projects:
            if p.get("identifier", "").upper() == identifier.upper():
                return p
        return None

    # ------------------------------------------------------------------
    # States
    # ------------------------------------------------------------------

    def list_states(self, project_id: str) -> list[dict]:
        """List workflow states for a project."""
        resp = self._client.get(f"/projects/{project_id}/states/")
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", data) if isinstance(data, dict) else data

    def get_state_map(self, project_id: str) -> dict[str, str]:
        """Get {state_name: state_id} mapping for a project."""
        states = self.list_states(project_id)
        return {s["name"]: s["id"] for s in states}

    # ------------------------------------------------------------------
    # Work Items
    # ------------------------------------------------------------------

    def get_work_item_by_identifier(
        self, project_id: str, sequence_id: int
    ) -> dict | None:
        """Get a work item by its sequence number (e.g., 15 for QFP-15)."""
        resp = self._client.get(
            f"/projects/{project_id}/work-items/",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        for item in items:
            if item.get("sequence_id") == sequence_id:
                return item
        return None

    def update_work_item(
        self, project_id: str, work_item_id: str, **fields
    ) -> dict:
        """Update a work item. Pass any fields to update as kwargs."""
        resp = self._client.patch(
            f"/projects/{project_id}/work-items/{work_item_id}/",
            json=fields,
        )
        resp.raise_for_status()
        return resp.json()

    def add_comment(
        self, project_id: str, work_item_id: str, html: str
    ) -> dict:
        """Add a comment to a work item."""
        resp = self._client.post(
            f"/projects/{project_id}/work-items/{work_item_id}/comments/",
            json={"comment_html": html},
        )
        resp.raise_for_status()
        return resp.json()

    def add_link(
        self, project_id: str, work_item_id: str, url: str
    ) -> dict:
        """Attach a URL link to a work item."""
        resp = self._client.post(
            f"/projects/{project_id}/work-items/{work_item_id}/links/",
            json={"url": url},
        )
        resp.raise_for_status()
        return resp.json()

    def list_work_items(
        self, project_id: str, state_group: str | None = None,
        per_page: int = 50, order_by: str = "-created_at"
    ) -> list[dict]:
        """List work items, optionally filtered by state group."""
        params: dict = {"per_page": per_page, "order_by": order_by}
        resp = self._client.get(
            f"/projects/{project_id}/work-items/",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("results", data) if isinstance(data, dict) else data

        if state_group:
            # Filter client-side since the list endpoint may not support state_group filter
            # We need to know which state IDs belong to which group
            states = self.list_states(project_id)
            group_state_ids = {
                s["id"] for s in states if s.get("group") == state_group
            }
            items = [i for i in items if i.get("state") in group_state_ids]

        return items

    def create_work_item(
        self, project_id: str, name: str, **fields
    ) -> dict:
        """Create a new work item."""
        payload = {"name": name, **fields}
        resp = self._client.post(
            f"/projects/{project_id}/work-items/",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()
