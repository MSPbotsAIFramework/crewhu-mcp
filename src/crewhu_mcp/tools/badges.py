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
"""Badges, the badge award history (recognition feed) and contest leaderboard updates."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..api_client import CrewhuClient
from ._common import (
    READ_ONLY,
    REPLACES_VALUES,
    ClientFactory,
    RecordId,
    Since,
    Step,
    limit_field,
    page_result,
    run_tool,
    updated_since,
)


class MetricValue(BaseModel):
    identificator: str = Field(description="Participant's Crewhu employee id or email address.", min_length=1)
    value: float = Field(description="The participant's total for the contest timeframe.")


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_badges_list(limit: limit_field(50) = None, step: Step = None) -> str:
        """List the badges employees can award and earn (name and description)."""

        async def action(client: CrewhuClient) -> dict:
            return page_result("badges", await client.list("badge", None, limit or 50, step or 1))

        return await run_tool("crewhu_badges_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_badges_get(id: RecordId) -> str:
        """Get one badge by id."""

        async def action(client: CrewhuClient) -> Any:
            return await client.get("badge", id)

        return await run_tool("crewhu_badges_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_badges_history_list(
        limit: limit_field(20) = None,
        step: Step = None,
        user_id: Annotated[str | None, Field(description="Only awards received by this user id.")] = None,
        badge_id: Annotated[str | None, Field(description="Only awards of this badge id.")] = None,
        awarded_by: Annotated[str | None, Field(description="Only awards given by this user id.")] = None,
        since: Since = None,
    ) -> str:
        """List badge awards (recognition feed): badge, giver, recipients, message, points, date, cancellation."""

        async def action(client: CrewhuClient) -> dict:
            query: dict[str, Any] = updated_since(since)
            for field, value in (("toUsers", user_id), ("badge", badge_id), ("fromUser", awarded_by)):
                if value and value.strip():
                    query[field] = value.strip()
            return page_result("awards", await client.list("badgehistory", query or None, limit or 20, step or 1))

        return await run_tool("crewhu_badges_history_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_badges_user_recognition(
        user_id: Annotated[str, Field(description="Recipient user id.", min_length=1)],
        limit: limit_field(20, "Awards to return") = None,
    ) -> str:
        """Get the badges a user has received, with total points from awards that were not cancelled."""

        async def action(client: CrewhuClient) -> dict:
            page = await client.list("badgehistory", {"toUsers": user_id.strip()}, limit or 20, 1)
            active = [a for a in page.items if isinstance(a, dict) and not a.get("dateCancelled")]
            points = sum(a.get("badgePoints") or 0 for a in active if isinstance(a.get("badgePoints"), (int, float)))
            return page_result(
                "awards",
                page,
                user_id=user_id.strip(),
                active_awards=len(active),
                active_points_in_page=points,
            )

        return await run_tool("crewhu_badges_user_recognition", client_factory, action)

    @mcp.tool(annotations=REPLACES_VALUES)
    async def crewhu_badges_update_contest(
        metric_token: Annotated[str, Field(description="Code of the External Metric the contest uses.", min_length=1)],
        timeframe: Annotated[
            Literal["DY", "WK", "MO", "QT", "YR"], Field(description="DY daily, WK weekly, MO monthly, QT quarterly, YR yearly.")
        ],
        data: Annotated[list[MetricValue], Field(description="One entry per participant.", min_length=1, max_length=1000)],
    ) -> str:
        """Post External Metric results to contest leaderboards; values replace the current ones.

        Only when the user explicitly asks, with the exact metric, timeframe and values.
        Needs "Contests update via API" enabled in Crewhu (Settings > Contests).
        """

        async def action(client: CrewhuClient) -> dict:
            body = {
                "metricToken": metric_token.strip(),
                "timeframe": timeframe,
                "data": [{"identificator": d.identificator.strip(), "value": d.value} for d in data],
            }
            result = await client.post("contest/metrics", body)
            return {
                "updated": True,
                "metric_token": body["metricToken"],
                "timeframe": timeframe,
                "participants": len(body["data"]),
                "response": result,
                "note": "Check Settings > Contests > View Updates Log in Crewhu for per-contest results.",
            }

        return await run_tool("crewhu_badges_update_contest", client_factory, action)
