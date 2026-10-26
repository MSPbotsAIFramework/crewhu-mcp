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

import time
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import CrewhuClient
from ..config import MAX_PAGE_SIZE
from ._common import (
    READ_ONLY,
    ClientFactory,
    Offset,
    RecordId,
    Since,
    Step,
    days_ago,
    iso_utc,
    limit_field,
    matches,
    object_id_time,
    run_tool,
    start_offset,
    updated_since,
    window_result,
)

Sentiment = Literal["positive", "neutral", "negative"]
# How far detractor / promoter reports read back through the survey history.
_SENTIMENT_SCAN_LIMIT = 1000
_SEARCH_FIELDS = ("comment", "summary", "ticket_num", "customer_data.name", "customer_data.contact_name", "customer_data.email")
# Search reads at most this many pages of 100, newest first, within this many seconds.
_SEARCH_MAX_PAGES = 20
_SEARCH_BUDGET_SECONDS = 60.0


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


async def _search_newest_first(client: CrewhuClient, needle: str, limit: int, since: str | None) -> dict:
    """Match surveys locally, reading pages from the last one back (Crewhu lists oldest first)."""
    query = {"inactive": False} | updated_since(since)
    total = (await client.list("survey", query, 1, 1)).total or 0
    step = max(1, -(-total // MAX_PAGE_SIZE))
    last = step
    found: list = []
    scanned = 0
    created: list[str] = []
    started = time.monotonic()
    while step >= 1 and len(found) < limit:
        if last - step >= _SEARCH_MAX_PAGES or time.monotonic() - started > _SEARCH_BUDGET_SECONDS:
            break
        page = await client.list("survey", query, MAX_PAGE_SIZE, step)
        for survey in reversed(page.items):
            scanned += 1
            if isinstance(survey, dict):
                if survey.get("_created_at"):
                    created.append(str(survey["_created_at"]))
                if matches(survey, needle, _SEARCH_FIELDS):
                    found.append(survey)
                    if len(found) >= limit:
                        break
        step -= 1
    searched_all = scanned >= total
    return {
        "query": needle,
        "surveys": with_sentiment(found),
        "count": len(found),
        "searched": scanned,
        "total": total,
        "searched_all": searched_all,
        "order": "newest first",
        "since": iso_utc(since, "since") if since and since.strip() else None,
        "searched_range": {"newest_created_at": max(created), "oldest_created_at": min(created)} if created else None,
        "note": None
        if searched_all
        else "Older surveys were not searched; set since to search a specific period.",
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
        offset: Offset = None,
    ) -> str:
        """List CSAT survey responses: customer, rating / sentiment, comment, ticket and credited employees.

        Crewhu lists surveys oldest first: set since for recent ones. sentiment is matched here.
        """

        async def action(client: CrewhuClient) -> dict:
            query = updated_since(since) | ({} if include_inactive else {"inactive": False})
            size = limit or 50
            start = start_offset(offset, step, size)
            keep = (lambda s: isinstance(s, dict) and sentiment_of(s) == sentiment) if sentiment else None
            window = await client.read_from("survey", query, start, size, keep)
            window.items = with_sentiment(window.items)
            return window_result("surveys", window, start, size, filtered=bool(sentiment))

        return await run_tool("crewhu_surveys_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_get(id: RecordId) -> str:
        """Get one survey by id, including tags, review flag and credited employee ids."""

        async def action(client: CrewhuClient) -> Any:
            # A survey is updated at or after its creation, which its id encodes: read from there.
            created = object_id_time(id)
            survey = await client.get_by_update_time("survey", id, created) if created else await client.get("survey", id)
            return with_sentiment([survey])[0]

        return await run_tool("crewhu_surveys_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def crewhu_surveys_search(
        query: Annotated[str, Field(description="Text in the comment, ticket summary, ticket number or customer name.", min_length=1)],
        limit: limit_field(100, "Matching surveys to return") = None,
        since: Since = None,
    ) -> str:
        """Search surveys by comment, ticket summary / number or customer, newest first.

        The API has no text search, so up to 2,000 of the most recent surveys (updated since
        `since`, if set) are read and matched here; searched_range says what was covered.
        """

        async def action(client: CrewhuClient) -> dict:
            needle = query.strip()
            if not needle:
                raise ValueError("query must not be blank.")
            return await _search_newest_first(client, needle, limit or 100, since)

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
