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
"""Prize catalog and prize redemptions."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import CrewhuClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    Offset,
    RecordId,
    Since,
    Step,
    limit_field,
    refers_to,
    run_tool,
    same_text,
    start_offset,
    updated_since,
    window_result,
)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_prizes_list(
        limit: limit_field(50) = None,
        step: Step = None,
        available_only: Annotated[bool | None, Field(description="Only active prizes (default true).")] = None,
        offset: Offset = None,
    ) -> str:
        """List the prize catalog: description (the prize title), points cost, taxable flag and link."""

        async def action(client: CrewhuClient) -> dict:
            query = None if available_only is False else {"inactive": False}
            size = limit or 50
            start = start_offset(offset, step, size)
            return window_result("prizes", await client.read_from("prize", query, start, size), start, size)

        return await run_tool("crewhu_prizes_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_prizes_get(id: RecordId) -> str:
        """Get one prize by id."""

        async def action(client: CrewhuClient) -> Any:
            return await client.get("prize", id)

        return await run_tool("crewhu_prizes_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_prizes_history_list(
        limit: limit_field(20) = None,
        step: Step = None,
        user_id: Annotated[str | None, Field(description="Only redemptions by this user id.")] = None,
        prize_id: Annotated[str | None, Field(description="Only redemptions of this prize id.")] = None,
        status: Annotated[str | None, Field(description="Exact redemption status (values are account-specific).")] = None,
        since: Since = None,
        offset: Offset = None,
    ) -> str:
        """List prize redemptions: prize, user, points, quantity, status, redeem and cancel dates.

        user_id, prize_id and status are matched here (Crewhu filters only by date), reading up to
        2,000 redemptions per call; continue with offset=next_offset.
        """

        async def action(client: CrewhuClient) -> dict:
            wanted = {k: v.strip() for k, v in (("user", user_id), ("prize", prize_id)) if v and v.strip()}
            state = (status or "").strip()

            def keep(r: Any) -> bool:
                return (
                    isinstance(r, dict)
                    and all(refers_to(r.get(k), v) for k, v in wanted.items())
                    and (not state or same_text(r.get("status"), state))
                )

            filtered = bool(wanted or state)
            size = limit or 20
            start = start_offset(offset, step, size)
            query = updated_since(since) or None
            window = await client.read_from("prizehistory", query, start, size, keep if filtered else None)
            return window_result("redemptions", window, start, size, filtered=filtered)

        return await run_tool("crewhu_prizes_history_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_prizes_user_redemptions(
        user_id: Annotated[str, Field(description="User id.", min_length=1)],
        limit: limit_field(20, "Redemptions to return") = None,
        offset: Offset = None,
    ) -> str:
        """Get the prizes one user has redeemed."""

        async def action(client: CrewhuClient) -> dict:
            wanted = user_id.strip()
            size, start = limit or 20, offset or 0
            window = await client.read_from(
                "prizehistory", None, start, size, lambda r: isinstance(r, dict) and refers_to(r.get("user"), wanted)
            )
            return window_result("redemptions", window, start, size, filtered=True, user_id=wanted)

        return await run_tool("crewhu_prizes_user_redemptions", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_prizes_pending_redemptions(
        limit: limit_field(50, "Redemptions to return") = None,
    ) -> str:
        """Get prize redemptions still waiting to be processed (status contains "pend", not cancelled)."""

        async def action(client: CrewhuClient) -> dict:
            page = await client.list_many("prizehistory", None, 1000)
            pending = [
                r
                for r in page.items
                if isinstance(r, dict) and "pend" in str(r.get("status") or "").lower() and not r.get("date_cancel")
            ]
            shown = pending[: limit or 50]
            return {
                "redemptions": shown,
                "count": len(shown),
                "pending_found": len(pending),
                "scanned": len(page.items),
                "scan_incomplete": page.has_more or None,
            }

        return await run_tool("crewhu_prizes_pending_redemptions", client_factory, action)
