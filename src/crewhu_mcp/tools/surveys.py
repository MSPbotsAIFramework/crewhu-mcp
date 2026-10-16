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
"""CSAT surveys: customer ratings and comments on tickets."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import CrewhuClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    RecordId,
    Since,
    Step,
    days_ago,
    iso_utc,
    limit_field,
    matches,
    page_result,
    run_tool,
    updated_since,
)

Sentiment = Literal["positive", "neutral", "negative"]
# How far detractor / promoter reports read back through the survey history.
_SENTIMENT_SCAN_LIMIT = 1000
_SEARCH_FIELDS = ("comment", "summary", "ticket_num", "customer_data.name", "customer_data.contact_name", "customer_data.email")


def sentiment_of(survey: dict) -> str | None:
    try:
        rating = float(survey.get("rating"))
    except (TypeError, ValueError):
        return None
    return "positive" if rating > 0 else "negative" if rating < 0 else "neutral"


def with_sentiment(surveys: list) -> list:
    return [s | {"sentiment": sentiment_of(s)} if isinstance(s, dict) else s for s in surveys]


async def _by_sentiment(client: CrewhuClient, sentiment: str, limit: int | None, since: str | None) -> dict:
    start = iso_utc(since, "since") if since and since.strip() else days_ago(30)
    page = await client.list_many(
        "survey", {"inactive": False, "_updated_at": {"$gte": start}}, _SENTIMENT_SCAN_LIMIT
    )
    found = [s for s in page.items if isinstance(s, dict) and sentiment_of(s) == sentiment]
    shown = with_sentiment(found[: limit or 20])
    return {
        "surveys": shown,
        "count": len(shown),
        "matching": len(found),
        "scanned": len(page.items),
        "since": start,
        "scan_incomplete": page.has_more or None,
    }


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_list(
        limit: limit_field(50) = None,
        step: Step = None,
        sentiment: Annotated[
            Sentiment | None, Field(description="Keep only this sentiment (filtered within the fetched page).")
        ] = None,
        since: Since = None,
        include_inactive: Annotated[bool | None, Field(description="Include inactive / deleted surveys (default false).")] = None,
    ) -> str:
        """List CSAT survey responses: customer, rating / sentiment, comment, ticket and credited employees."""

        async def action(client: CrewhuClient) -> dict:
            query = updated_since(since) | ({} if include_inactive else {"inactive": False})
            page = await client.list("survey", query, limit or 50, step or 1)
            items = with_sentiment(page.items)
            if sentiment:
                items = [s for s in items if isinstance(s, dict) and s.get("sentiment") == sentiment]
            return page_result("surveys", page, items, sentiment_filtered_in_page=bool(sentiment) or None)

        return await run_tool("crewhu_surveys_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_get(id: RecordId) -> str:
        """Get one survey by id, including tags, review flag and credited employee ids."""

        async def action(client: CrewhuClient) -> Any:
            survey = await client.get("survey", id)
            return with_sentiment([survey])[0]

        return await run_tool("crewhu_surveys_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_search(
        query: Annotated[str, Field(description="Text in the comment, ticket summary, ticket number or customer name.", min_length=1)],
        limit: limit_field(100, "Surveys to read and search through") = None,
    ) -> str:
        """Search surveys by comment, ticket summary / number or customer (searches the first `limit` surveys).

        The API has no text search, so this reads up to `limit` surveys and matches them here.
        """

        async def action(client: CrewhuClient) -> dict:
            needle = query.strip()
            if not needle:
                raise ValueError("query must not be blank.")
            page = await client.list("survey", {"inactive": False}, limit or 100, 1)
            found = [s for s in page.items if isinstance(s, dict) and matches(s, needle, _SEARCH_FIELDS)]
            return {
                "query": needle,
                "surveys": with_sentiment(found),
                "count": len(found),
                "searched": len(page.items),
                "total": page.total,
                "searched_all": not page.has_more,
            }

        return await run_tool("crewhu_surveys_search", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_detractors(
        limit: limit_field(20, "Surveys to return") = None,
        since: Annotated[str | None, Field(description="ISO 8601 UTC start (default: 30 days ago).")] = None,
    ) -> str:
        """Get recent negative surveys (rating -5) for follow-up and service recovery."""

        async def action(client: CrewhuClient) -> dict:
            return await _by_sentiment(client, "negative", limit, since)

        return await run_tool("crewhu_surveys_detractors", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_promoters(
        limit: limit_field(20, "Surveys to return") = None,
        since: Annotated[str | None, Field(description="ISO 8601 UTC start (default: 30 days ago).")] = None,
    ) -> str:
        """Get recent positive surveys (rating 5), e.g. to recognise the credited employees."""

        async def action(client: CrewhuClient) -> dict:
            return await _by_sentiment(client, "positive", limit, since)

        return await run_tool("crewhu_surveys_promoters", client_factory, action)
