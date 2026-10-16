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
"""Request shapes, paging, retries and tool behaviour against a mocked Crewhu API."""

import json

import httpx
import pytest

from crewhu_mcp import api_client
from crewhu_mcp.api_client import CrewhuClient, CrewhuCredentials, CrewhuError
from crewhu_mcp.server import _credentials_var, create_mcp_server

CREDS = CrewhuCredentials("tok-123")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


def page(results, total=None, step=1, limit=100, more=False):
    links = {"base": "https://api.crewhu.com/api/", "self": f"/v1/x?step={step}"}
    if more:
        links["next"] = f"/v1/x?step={step + 1}"
    return {"_links": links, "start": 1, "limit": limit, "size": len(results), "total": total or len(results), "results": results}


class Recorder:
    """Mock transport: replies by path below /api/v1/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else page([])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/v1/{path}"]


def query_of(request: httpx.Request):
    raw = request.url.params.get("query")
    return json.loads(raw) if raw else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> CrewhuClient:
    return CrewhuClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- client ----------------------------------------------------------------


async def test_token_header_and_query_encoding():
    rec = Recorder({"survey": page([{"_id": "s1"}], total=40, more=True)})
    result = await _client(rec).list("survey", {"inactive": False}, limit=20, step=2)
    request = rec.requests[0]
    assert request.headers["X_CREWHU_APITOKEN"] == "tok-123"
    assert request.url.params["limit"] == "20" and request.url.params["step"] == "2"
    assert query_of(request) == {"inactive": False}
    assert (result.total, result.has_more, result.items) == (40, True, [{"_id": "s1"}])


async def test_get_falls_back_to_an_id_filter():
    rec = Recorder({"survey/s9": lambda r: httpx.Response(404, json={}), "survey": page([{"_id": "s9"}])})
    assert await _client(rec).get("survey", "s9") == {"_id": "s9"}
    assert query_of(rec.calls("survey")[0]) == {"_id": "s9"}


async def test_get_missing_record_is_not_found():
    rec = Recorder({"survey/s9": lambda r: httpx.Response(404, json={})})
    with pytest.raises(CrewhuError) as err:
        await _client(rec).get("survey", "s9")
    assert err.value.status_code == 404


async def test_list_many_follows_pages_until_the_end():
    pages = {1: page([{"i": 1}] * 100, total=150, more=True), 2: page([{"i": 2}] * 50, total=150, step=2)}
    rec = Recorder({"survey": lambda r: httpx.Response(200, json=pages[int(r.url.params["step"])])})
    result = await _client(rec).list_many("survey", None, 1000)
    assert len(result.items) == 150 and result.has_more is False
    assert len(rec.requests) == 2


@pytest.mark.parametrize(
    "token,meaning",
    [("API_ERR_INV_AUTH_PARAM", "not valid"), ("API_ERR_INACT_AUTH_PARAM", "inactive")],
)
async def test_token_errors_are_explained(monkeypatch, token, meaning):
    rec = Recorder({"badge": lambda r: httpx.Response(401, json={"error": token})})
    result = await _call(monkeypatch, rec, "crewhu_badges_list", {})
    assert result["error"]["code"] == "unauthorized"
    assert token in result["error"]["message"] and meaning in result["error"]["message"]
    assert "tok-123" not in json.dumps(result)


async def test_get_retries_5xx_but_post_does_not():
    replies = iter([httpx.Response(502), httpx.Response(200, json=page([]))])
    rec = Recorder({"badge": lambda r: next(replies), "POST contest/metrics": lambda r: httpx.Response(500, json={})})
    client = _client(rec)
    await client.list("badge")
    with pytest.raises(CrewhuError):
        await client.post("contest/metrics", {})
    assert len(rec.calls("badge")) == 2
    assert len(rec.calls("contest/metrics")) == 1


# ---- tools -----------------------------------------------------------------


async def test_surveys_list_filters_and_sentiment(monkeypatch):
    surveys = [{"_id": "a", "rating": "5"}, {"_id": "b", "rating": "-5"}, {"_id": "c", "rating": "0"}]
    rec = Recorder({"survey": page(surveys, total=3)})
    result = await _call(monkeypatch, rec, "crewhu_surveys_list", {"since": "2026-06-01", "sentiment": "negative"})
    assert query_of(rec.requests[0]) == {"_updated_at": {"$gte": "2026-06-01T00:00:00.000Z"}, "inactive": False}
    assert result["surveys"] == [{"_id": "b", "rating": "-5", "sentiment": "negative"}]
    assert result["total"] == 3 and result["has_more"] is False


async def test_include_inactive_drops_the_filter(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "crewhu_surveys_list", {"include_inactive": True})
    assert "query" not in rec.requests[0].url.params


async def test_bad_since_is_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "crewhu_surveys_list", {"since": "last week"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_detractors_scan_pages_and_keep_negative_only(monkeypatch):
    first = page([{"_id": f"p{i}", "rating": "5"} for i in range(100)], total=101, more=True)
    second = page([{"_id": "n1", "rating": "-5", "comment": "slow"}], total=101, step=2)
    rec = Recorder({"survey": lambda r: httpx.Response(200, json=first if r.url.params["step"] == "1" else second)})
    result = await _call(monkeypatch, rec, "crewhu_surveys_detractors", {"since": "2026-06-01T00:00:00Z"})
    assert [s["_id"] for s in result["surveys"]] == ["n1"]
    assert result["scanned"] == 101
    assert query_of(rec.requests[0]) == {"inactive": False, "_updated_at": {"$gte": "2026-06-01T00:00:00.000Z"}}


async def test_survey_search_matches_nested_customer_fields(monkeypatch):
    surveys = [
        {"_id": "a", "comment": "great", "customer_data": {"name": "Example Dental"}},
        {"_id": "b", "comment": "ok", "ticket_num": "4411"},
    ]
    rec = Recorder({"survey": page(surveys)})
    result = await _call(monkeypatch, rec, "crewhu_surveys_search", {"query": "dental"})
    assert [s["_id"] for s in result["surveys"]] == ["a"]
    assert result["searched_all"] is True


async def test_users_list_department_filter(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "crewhu_users_list", {"department": "Service Desk"})
    assert query_of(rec.requests[0]) == {"inactive": False, "department": "Service Desk"}


async def test_users_search_matches_full_name(monkeypatch):
    rec = Recorder({"user": page([{"_id": "u1", "firstname": "Jane", "lastname": "Doe"}, {"_id": "u2", "firstname": "John"}])})
    result = await _call(monkeypatch, rec, "crewhu_users_search", {"query": "jane doe"})
    assert [u["_id"] for u in result["users"]] == ["u1"]


async def test_badge_history_filters(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "crewhu_badges_history_list", {"user_id": "u1", "badge_id": "b1", "awarded_by": "u2"})
    assert query_of(rec.requests[0]) == {"toUsers": "u1", "badge": "b1", "fromUser": "u2"}
    assert rec.requests[0].url.params["limit"] == "20"


async def test_user_recognition_totals_active_points(monkeypatch):
    awards = [{"badgePoints": 10}, {"badgePoints": 5, "dateCancelled": "2026-05-01"}, {"badgePoints": 3}]
    rec = Recorder({"badgehistory": page(awards)})
    result = await _call(monkeypatch, rec, "crewhu_badges_user_recognition", {"user_id": "u1"})
    assert (result["active_awards"], result["active_points_in_page"]) == (2, 13)


async def test_update_contest_posts_documented_body(monkeypatch):
    rec = Recorder({"POST contest/metrics": {"ok": True}})
    args = {
        "metric_token": "timemetric",
        "timeframe": "WK",
        "data": [{"identificator": "jdoe", "value": 30}, {"identificator": "a@example.com", "value": 79.5}],
    }
    result = await _call(monkeypatch, rec, "crewhu_badges_update_contest", args)
    request = rec.calls("contest/metrics")[0]
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "metricToken": "timemetric",
        "timeframe": "WK",
        "data": [{"identificator": "jdoe", "value": 30}, {"identificator": "a@example.com", "value": 79.5}],
    }
    assert result["participants"] == 2 and result["updated"] is True


async def test_prizes_list_active_only_by_default(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "crewhu_prizes_list", {})
    await _call(monkeypatch, rec, "crewhu_prizes_list", {"available_only": False})
    assert query_of(rec.requests[0]) == {"inactive": False}
    assert query_of(rec.requests[1]) is None


async def test_pending_redemptions(monkeypatch):
    rows = [{"status": "Pending"}, {"status": "Approved"}, {"status": "pending approval", "date_cancel": "2026-01-01"}]
    rec = Recorder({"prizehistory": page(rows)})
    result = await _call(monkeypatch, rec, "crewhu_prizes_pending_redemptions", {})
    assert result["redemptions"] == [{"status": "Pending"}]
    assert result["scanned"] == 3
