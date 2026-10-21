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
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import CrewhuClient, CrewhuError, Window

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
Offset = Annotated[
    int | None,
    Field(description="Record position to start from (0-based); pass next_offset from the previous result.", ge=0),
]
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


def start_offset(offset: int | None, step: int | None, limit: int) -> int:
    """``offset`` wins; otherwise page ``step`` of ``limit`` records."""
    return offset if offset is not None else ((step or 1) - 1) * limit


def window_result(
    key: str,
    window: Window,
    offset: int,
    limit: int,
    step: int | None = None,
    filtered: bool = False,
    **extra: Any,
) -> dict:
    """List result read with ``CrewhuClient.read_from``.

    ``next_offset`` continues right after the last record examined; when the result is
    cut to fit the size limit it continues right after the last record returned.
    ``next_step`` is kept for callers paging by step (unfiltered, step-aligned reads).
    """
    aligned = not filtered and offset % limit == 0 and window.next_offset in (None, offset + limit)
    result: dict[str, Any] = {
        key: window.items,
        "count": len(window.items),
        "offset": offset,
        "has_more": window.next_offset is not None,
        "next_offset": window.next_offset,
    }
    if filtered:
        # Crewhu ignores equality filters, so they are matched here; its total is unfiltered.
        result["total_unfiltered"] = window.total
        result["scanned"] = window.scanned
        if offset == 0 and window.complete:
            result["matched_total"] = len(window.items)
        elif window.next_offset is not None and len(window.items) < limit:
            result["scan_incomplete"] = True
            result["note"] = (
                f"Searched {window.scanned} records and found {len(window.items)} matches; "
                "call again with offset=next_offset to search further."
            )
    else:
        result["total"] = window.total
    if aligned:
        result["step"] = offset // limit + 1
        result["next_step"] = offset // limit + 2 if window.next_offset is not None else None
    result.update(extra)
    result["_resume"] = {"key": "next_offset", "positions": window.positions}
    return compact(result)


def refers_to(value: Any, wanted: str) -> bool:
    """True if a reference field (an id, an object with _id, or a list of them) points to ``wanted``."""
    if isinstance(value, list):
        return any(refers_to(v, wanted) for v in value)
    if isinstance(value, dict):
        value = value.get("_id")
    return value is not None and str(value) == wanted


def same_text(value: Any, wanted: str) -> bool:
    return value is not None and str(value).strip().lower() == wanted.strip().lower()


def object_id_time(record_id: str) -> str | None:
    """Creation time encoded in a 24-hex-digit Crewhu (MongoDB ObjectId) id, ISO UTC."""
    text = record_id.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{24}", text):
        return None
    created = datetime.fromtimestamp(int(text[:8], 16), UTC) - timedelta(minutes=1)
    return created.strftime("%Y-%m-%dT%H:%M:%S.000Z")


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
