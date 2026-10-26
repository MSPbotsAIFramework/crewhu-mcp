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
"""Async client for the Crewhu Open API (v1)."""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from ._json import error_envelope
from .config import API_BASE_URL, MAX_PAGE_SIZE, UPSTREAM_TOKEN_HEADER

logger = logging.getLogger("crewhu_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0
# Scans that read page after page stop starting new pages after this long, or after
# SCAN_MAX_PAGES pages of 100, and say where to continue.
_SCAN_BUDGET_SECONDS = 60.0
SCAN_MAX_PAGES = 20

# One connection pool for the process. It never holds credentials: the token
# header is built per request from the caller's own token, so sharing the
# pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    # A plain CookieJar: httpx uses it as is (an httpx.Cookies would be copied into a default jar).
    return CookieJar(policy=_RejectAllCookies())


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class CrewhuError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Crewhu API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class CrewhuCredentials:
    api_token: str

    def __repr__(self) -> str:  # never leak the token into logs or tracebacks
        return "CrewhuCredentials(api_token=<redacted>)"


@dataclass
class Page:
    items: list
    total: int | None
    step: int
    limit: int
    has_more: bool


@dataclass
class Window:
    """Records read from one position of a collection, optionally matched locally."""

    items: list
    total: int | None  # server total of the collection (Crewhu applies only _updated_at / inactive)
    next_offset: int | None  # where to continue; None once the collection end was reached
    positions: list[int] = field(default_factory=list)  # position right after each returned record
    scanned: int = 0

    @property
    def complete(self) -> bool:
        return self.next_offset is None


class CrewhuClient:
    """Thin wrapper over the Crewhu Open API for one caller's token."""

    def __init__(self, credentials: CrewhuCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.base_url = API_BASE_URL

    async def list(self, resource: str, query: dict[str, Any] | None = None, limit: int = 50, step: int = 1) -> Page:
        """One page of a collection. Filters travel as a JSON string in ``query``."""
        params: dict[str, Any] = {"limit": min(max(limit, 1), MAX_PAGE_SIZE), "step": max(step, 1)}
        if query:
            params["query"] = json.dumps(query, separators=(",", ":"))
        payload = await self.request("GET", resource, params=params) or {}
        if isinstance(payload, list):
            return Page(payload, None, params["step"], params["limit"], False)
        items = payload.get("results") if isinstance(payload, dict) else None
        links = payload.get("_links") if isinstance(payload, dict) else None
        total = payload.get("total") if isinstance(payload, dict) else None
        return Page(
            items if isinstance(items, list) else [],
            total if isinstance(total, int) else None,
            params["step"],
            params["limit"],
            bool(isinstance(links, dict) and links.get("next")),
        )

    async def list_many(self, resource: str, query: dict[str, Any] | None, max_records: int) -> Page:
        """Follow pages of 100 until max_records are read or the collection ends."""
        items: list = []
        step, total, more = 1, None, True
        while more and len(items) < max_records:
            page = await self.list(resource, query, MAX_PAGE_SIZE, step)
            items.extend(page.items)
            total, more = page.total, page.has_more and bool(page.items)
            step += 1
        return Page(items[:max_records], total, 1, len(items), more or len(items) > max_records)

    async def read_from(
        self,
        resource: str,
        query: dict[str, Any] | None,
        offset: int,
        limit: int,
        keep: Callable[[Any], bool] | None = None,
        max_pages: int = SCAN_MAX_PAGES,
    ) -> Window:
        """Up to ``limit`` records from position ``offset`` (0-based) of the collection.

        Crewhu pages by step only, so pages of 100 are read and sliced here. ``keep``
        matches records locally (Crewhu ignores equality filters); then pages are read
        until ``limit`` matches, ``max_pages`` pages or the time budget are reached.
        """
        offset = max(int(offset), 0)
        step, skip = offset // MAX_PAGE_SIZE + 1, offset % MAX_PAGE_SIZE
        items: list = []
        positions: list[int] = []
        total: int | None = None
        position, scanned, pages = offset, 0, 0
        started = time.monotonic()
        while True:
            page = await self.list(resource, query, MAX_PAGE_SIZE, step)
            pages += 1
            total = page.total
            base = (step - 1) * MAX_PAGE_SIZE
            for index in range(skip, len(page.items)):
                record = page.items[index]
                position, scanned = base + index + 1, scanned + 1
                if keep is None or keep(record):
                    items.append(record)
                    positions.append(position)
                    if len(items) >= limit:
                        break
            ended = not (page.has_more and page.items)
            if len(items) >= limit:
                rest_of_page = position < base + len(page.items)
                return Window(items, total, position if rest_of_page or not ended else None, positions, scanned)
            if ended:
                return Window(items, total, None, positions, scanned)
            position = base + len(page.items)
            if pages >= max_pages or time.monotonic() - started > _SCAN_BUDGET_SECONDS:
                return Window(items, total, position, positions, scanned)
            step, skip = step + 1, 0

    async def get(self, resource: str, record_id: str, query: dict[str, Any] | None = None) -> Any:
        """One record by _id, found by scanning the collection (``query`` narrows the scan).

        The Open API has no item route and ignores an _id filter, so the record whose
        _id equals ``record_id`` is looked up page by page.
        """
        wanted = str(record_id).strip()
        if not wanted:
            raise ValueError("id must not be blank.")
        window = await self.read_from(
            resource, query, 0, 1, lambda r: isinstance(r, dict) and str(r.get("_id")) == wanted, max_pages=50
        )
        if window.items:
            return window.items[0]
        if window.complete:
            raise CrewhuError(404, f"No {resource} with id {wanted}")
        raise CrewhuError(
            404, f"No {resource} with id {wanted} in the first {window.scanned} records searched ({window.total} in total)"
        )

    async def get_by_update_time(self, resource: str, record_id: str, created: str) -> Any:
        """One record by _id, read from where it sits in the collection's update-time order.

        An unfiltered collection comes back oldest first by _updated_at (checked live on about
        11,700 surveys), and a record is updated at or after its creation, which its id encodes.
        The records updated since then are counted, so the scan starts about where the record
        is instead of at the start: a record years old is found in a page or two.
        """
        wanted = str(record_id).strip()
        everything = (await self.list(resource, None, 1, 1)).total
        later = (await self.list(resource, {"_updated_at": {"$gte": created}}, 1, 1)).total
        if not isinstance(everything, int) or not isinstance(later, int):
            return await self.get(resource, wanted, {"_updated_at": {"$gte": created}})
        # One page of slack: records updated meanwhile, or at the same moment, shift positions.
        start = max(everything - later - MAX_PAGE_SIZE, 0)
        # Usually found in the first page or two; a record updated long after its creation sits
        # further on, so read on to the end of the collection (within the time budget).
        pages = -(-(everything - start) // MAX_PAGE_SIZE) + 1
        window = await self.read_from(
            resource, None, start, 1, lambda r: isinstance(r, dict) and str(r.get("_id")) == wanted, max_pages=pages
        )
        if window.items:
            return window.items[0]
        if window.complete:
            raise CrewhuError(404, f"No {resource} with id {wanted}")
        raise CrewhuError(
            404,
            f"No {resource} with id {wanted} in the {window.scanned} records searched from position {start} "
            f"(where records updated since its creation start, of {everything} in total)",
        )

    async def post(self, path: str, body: Any) -> Any:
        return await self.request("POST", path, body=body)

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        http = self._http or _get_shared_http_client()
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {UPSTREAM_TOKEN_HEADER: self._credentials.api_token, "Accept": "application/json"}
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: CrewhuError | None = None
            try:
                resp = await http.request(method, url, params=params, json=body, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, CrewhuError(0, f"Timed out calling Crewhu ({method} {path})")
            except httpx.RequestError as e:
                status = 0
                error = CrewhuError(0, f"Network error calling Crewhu ({method} {path}): {type(e).__name__}")

            logger.info(
                "crewhu_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # 429 is refused before processing, so any method may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code >= 400:
                raise CrewhuError(resp.status_code, _error_message(resp, path))
            return _parse_body(resp)


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


_TOKEN_ERRORS = {
    "API_ERR_NO_AUTH_PARAM": "no API token was sent",
    "API_ERR_INV_AUTH_PARAM": "the API token is not valid",
    "API_ERR_INACT_AUTH_PARAM": "the API token is inactive",
    "API_ERR_INVALID_QUERY": "the query filter is not valid",
    "API_ERR_OUT_OF_BOUNDS": "limit or step is out of range",
}


def _error_message(resp: httpx.Response, path: str) -> str:
    fallback = f"Crewhu returned HTTP {resp.status_code} for {path}"
    text = resp.text.strip()
    for token, meaning in _TOKEN_ERRORS.items():
        if token in text:
            return f"{fallback}: {token} ({meaning})"
    try:
        payload = resp.json()
    except ValueError:
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        for key in ("message", "error", "errors", "Message"):
            value = payload.get(key)
            if value:
                return f"{fallback}: {str(value)[:500]}"
    return fallback
