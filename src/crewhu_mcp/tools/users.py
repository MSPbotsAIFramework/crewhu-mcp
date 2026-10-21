# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Users: the MSP's employees in Crewhu."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import CrewhuClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    Offset,
    RecordId,
    Step,
    limit_field,
    matches,
    run_tool,
    same_text,
    start_offset,
    window_result,
)

_SEARCH_FIELDS = ("firstname", "lastname", "employee_id", "department", "position", "location")


def _search_text(user: dict) -> dict:
    full = " ".join(str(user.get(k) or "") for k in ("firstname", "lastname")).strip()
    return user | {"fullname": full}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_users_list(
        limit: limit_field(50) = None,
        step: Step = None,
        department: Annotated[str | None, Field(description="Exact department name (case-insensitive).")] = None,
        include_inactive: Annotated[bool | None, Field(description="Include inactive users (default false).")] = None,
        offset: Offset = None,
    ) -> str:
        """List employees with name, employee id, position, department and location."""

        async def action(client: CrewhuClient) -> dict:
            query: dict[str, Any] = {} if include_inactive else {"inactive": False}
            size, wanted = limit or 50, (department or "").strip()
            start = start_offset(offset, step, size)
            keep = (lambda u: isinstance(u, dict) and same_text(u.get("department"), wanted)) if wanted else None
            window = await client.read_from("user", query, start, size, keep)
            return window_result("users", window, start, size, filtered=bool(wanted))

        return await run_tool("crewhu_users_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_users_get(id: RecordId) -> str:
        """Get one employee by id, including hire date and the departments / locations they manage."""

        async def action(client: CrewhuClient) -> Any:
            return await client.get("user", id)

        return await run_tool("crewhu_users_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_users_search(
        query: Annotated[str, Field(description="Name, employee id, department, position or location text.", min_length=1)],
        limit: limit_field(100, "Users to read and search through") = None,
    ) -> str:
        """Find employees by name, employee id, department, position or location (in the first `limit` users)."""

        async def action(client: CrewhuClient) -> dict:
            needle = query.strip()
            if not needle:
                raise ValueError("query must not be blank.")
            page = await client.list("user", {"inactive": False}, limit or 100, 1)
            found = [
                u for u in page.items if isinstance(u, dict) and matches(_search_text(u), needle, _SEARCH_FIELDS + ("fullname",))
            ]
            return {"query": needle, "users": found, "count": len(found), "searched": len(page.items), "searched_all": not page.has_more}

        return await run_tool("crewhu_users_search", client_factory, action)
