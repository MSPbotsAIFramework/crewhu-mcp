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
"""Shared argument types, query / page helpers and the error-handling wrapper."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import CrewhuClient, CrewhuError, Page

logger = logging.getLogger("crewhu_mcp.tools")

ClientFactory = Callable[[], CrewhuClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
# Replaces the leaderboard values of a contest with the ones sent.
REPLACES_VALUES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured", "Crewhu credentials are missing for this request (X-Crewhu-Api-Token).", False
)

RecordId = Annotated[str, Field(description="Crewhu record id (_id).", min_length=1)]
Step = Annotated[int | None, Field(description="Page number, from 1 (default 1).", ge=1)]
Since = Annotated[str | None, Field(description="ISO 8601 UTC timestamp, e.g. 2026-06-01T00:00:00Z.")]


def limit_field(default: int, what: str = "Results per page") -> Any:
    return Annotated[int | None, Field(description=f"{what} (default {default}, max 100).", ge=1, le=100)]


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def iso_utc(value: str, field: str) -> str:
    """Normalise an ISO 8601 date/time to the UTC form Crewhu filters expect."""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f'{field} must be an ISO 8601 date or timestamp, e.g. "2026-06-01T00:00:00Z" (got "{value}").') from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def updated_since(since: str | None, field: str = "since") -> dict[str, Any]:
    return {"_updated_at": {"$gte": iso_utc(since, field)}} if since and since.strip() else {}


def page_result(key: str, page: Page, items: list | None = None, **extra: Any) -> dict:
    shown = page.items if items is None else items
    return compact(
        {
            key: shown,
            "count": len(shown),
            "total": page.total,
            "step": page.step,
            "has_more": page.has_more,
            "next_step": page.step + 1 if page.has_more else None,
            **extra,
        }
    )


def matches(record: dict, needle: str, fields: tuple[str, ...]) -> bool:
    text = needle.lower()
    for field in fields:
        value: Any = record
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None and text in str(value).lower():
            return True
    return False


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[CrewhuClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except CrewhuError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Crewhu returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
