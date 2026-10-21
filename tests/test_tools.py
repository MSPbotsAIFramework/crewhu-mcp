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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from crewhu_mcp.api_client import CrewhuError
from crewhu_mcp.server import INSTRUCTIONS, create_mcp_server

# Tool names are a public contract: existing agents and tenant connections
# depend on them. Changing this snapshot is a breaking change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "crewhu_surveys_list": set(),
    "crewhu_surveys_get": {"id"},
    "crewhu_surveys_search": {"query"},
    "crewhu_surveys_detractors": set(),
    "crewhu_surveys_promoters": set(),
    "crewhu_users_list": set(),
    "crewhu_users_get": {"id"},
    "crewhu_users_search": {"query"},
    "crewhu_badges_list": set(),
    "crewhu_badges_get": {"id"},
    "crewhu_badges_history_list": set(),
    "crewhu_badges_user_recognition": {"user_id"},
    # POST /v1/contest/metrics params plus the legacy id/name/description/target; checked at runtime.
    "crewhu_badges_update_contest": set(),
    "crewhu_prizes_list": set(),
    "crewhu_prizes_get": {"id"},
    "crewhu_prizes_history_list": set(),
    "crewhu_prizes_user_redemptions": {"user_id"},
    "crewhu_prizes_pending_redemptions": set(),
}
WRITE_TOOLS = {"crewhu_badges_update_contest"}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is (name not in WRITE_TOOLS)


def test_write_tool_says_when_to_use_it(tools):
    assert "explicitly asks" in tools["crewhu_badges_update_contest"].description


def test_page_limits_follow_the_api_maximum(tools):
    schema = tools["crewhu_surveys_list"].inputSchema["properties"]["limit"]
    assert next(a["maximum"] for a in schema["anyOf"] if "maximum" in a) == 100


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (400, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(CrewhuError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("crewhu_badges_list", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"
