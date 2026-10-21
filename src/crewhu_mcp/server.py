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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import CrewhuClient, CrewhuCredentials
from .config import Settings

HEADER_API_TOKEN = "X-Crewhu-Api-Token"

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_BEARER = re.compile(r"^bearer(\s+|$)", re.IGNORECASE)

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[CrewhuCredentials | None] = contextvars.ContextVar(
    "crewhu_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("crewhu_request_id", default="-")

INSTRUCTIONS = (
    "Crewhu is a customer-satisfaction and employee-recognition platform used "
    "by MSP service desks: customers answer one-click CSAT surveys on closed "
    "tickets (rating 5 positive, 0 neutral, -5 negative), employees earn "
    "badges and points from surveys and peers, and redeem points for prizes.\n"
    "Usage:\n"
    "- CSAT review: crewhu_surveys_detractors (negative, for service recovery) "
    "and crewhu_surveys_promoters (positive); crewhu_surveys_list(since) for "
    "everything; crewhu_surveys_search to find a customer or ticket.\n"
    "- Employees: crewhu_users_search(query) gives user ids; then "
    "crewhu_badges_user_recognition and crewhu_prizes_user_redemptions.\n"
    "- Lists page with limit (max 100); pass next_offset as offset for the next "
    "page (step still works). Crewhu lists oldest first: use since for recent data.\n"
    "- Ids are Crewhu _id strings. Dates are ISO 8601 UTC.\n"
    "crewhu_badges_update_contest replaces a contest leaderboard's values: call "
    "it only when the user explicitly asks, with the exact metric and values."
)


def get_client_from_context() -> CrewhuClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return CrewhuClient(credentials)


def _clean(value: str | None) -> str | None:
    text = _BEARER.sub("", (value or "").strip()).strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


class GatewayCredentialsMiddleware:
    """Reads X-Crewhu-Api-Token on /mcp requests into the request context; 401 when missing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = _clean(request.headers.get(HEADER_API_TOKEN))
        if not token:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Send the Crewhu Open API token in {HEADER_API_TOKEN}",
                    "required_headers": [HEADER_API_TOKEN],
                },
                status_code=401,
            )(scope, receive, send)
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(CrewhuCredentials(token))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="crewhu-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], CrewhuClient | None] = get_client_from_context

    from .tools import badges, prizes, surveys, users

    for module in (surveys, users, badges, prizes):
        module.register(mcp, client_factory)

    return mcp
