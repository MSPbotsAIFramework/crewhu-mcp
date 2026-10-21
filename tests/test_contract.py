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
"""Tool-contract compatibility with the deployed (int_baseline) and upstream tool schemas.

Every tool of either contract must exist, accept at least the same parameters, and must not
require anything the deployed contract did not require.
"""

import asyncio
import json
from pathlib import Path

import pytest

from crewhu_mcp.server import create_mcp_server

BASELINE = json.loads((Path(__file__).parent / "contract_baseline.json").read_text(encoding="utf-8"))
CONTRACT_TOOLS = sorted(set(BASELINE["int_baseline"]) | set(BASELINE["upstream"]))


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def _contracts(name):
    return [BASELINE[key][name] for key in ("int_baseline", "upstream") if name in BASELINE[key]]


@pytest.mark.parametrize("name", CONTRACT_TOOLS)
def test_tool_exists(tools, name):
    assert name in tools


@pytest.mark.parametrize("name", CONTRACT_TOOLS)
def test_params_are_superset(tools, name):
    ours = set(tools[name].inputSchema.get("properties", {}))
    for contract in _contracts(name):
        assert set(contract["params"]) <= ours, f"{name} misses {set(contract['params']) - ours}"


@pytest.mark.parametrize("name", CONTRACT_TOOLS)
def test_required_subset_of_int_baseline(tools, name):
    ours = set(tools[name].inputSchema.get("required", []))
    allowed = set(BASELINE["int_baseline"].get(name, {}).get("required", []))
    assert ours <= allowed, f"{name} newly requires {ours - allowed}"
