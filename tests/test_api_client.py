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


async def test_get_returns_the_record_with_that_id_not_the_first_one():
    # Crewhu ignores an id in the path or in query and returns the collection.
    pages = {1: page([{"_id": f"b{i}"} for i in range(100)], total=150, more=True),
             2: page([{"_id": f"b{i}"} for i in range(100, 150)], total=150, step=2)}
    rec = Recorder({"badge": lambda r: httpx.Response(200, json=pages[int(r.url.params["step"])])})
    assert await _client(rec).get("badge", "b120") == {"_id": "b120"}
    assert all(r.url.path == "/api/v1/badge" and "query" not in r.url.params for r in rec.requests)


async def test_get_missing_record_is_not_found():
    rec = Recorder({"badge": page([{"_id": "b1"}, {"_id": "b2"}])})
    with pytest.raises(CrewhuError) as err:
        await _client(rec).get("badge", "000000000000000000000000")
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
    assert result["total_unfiltered"] == 3 and result["matched_total"] == 1 and result["has_more"] is False


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
    users = [{"_id": "u1", "department": "Service Desk"}, {"_id": "u2", "department": "Finance Team"},
             {"_id": "u3", "department": "service desk"}]
    rec = Recorder({"user": page(users)})
    result = await _call(monkeypatch, rec, "crewhu_users_list", {"department": "Service Desk"})
    assert query_of(rec.requests[0]) == {"inactive": False}  # Crewhu ignores a department filter
    assert [u["_id"] for u in result["users"]] == ["u1", "u3"]
    assert result["matched_total"] == 2 and result["total_unfiltered"] == 3


async def test_users_search_matches_full_name(monkeypatch):
    rec = Recorder({"user": page([{"_id": "u1", "firstname": "Jane", "lastname": "Doe"}, {"_id": "u2", "firstname": "John"}])})
    result = await _call(monkeypatch, rec, "crewhu_users_search", {"query": "jane doe"})
    assert [u["_id"] for u in result["users"]] == ["u1"]


async def test_badge_history_filters(monkeypatch):
    awards = [
        {"_id": "a1", "toUsers": ["u1", "u9"], "badge": "b1", "fromUser": "u2"},
        {"_id": "a2", "toUsers": ["u9"], "badge": "b1", "fromUser": "u2"},
        {"_id": "a3", "toUsers": [{"_id": "u1"}], "badge": {"_id": "b1"}, "fromUser": {"_id": "u2"}},
        {"_id": "a4", "toUsers": ["u1"], "badge": "b2", "fromUser": "u2"},
    ]
    rec = Recorder({"badgehistory": page(awards)})
    result = await _call(
        monkeypatch, rec, "crewhu_badges_history_list",
        {"user_id": "u1", "badge_id": "b1", "awarded_by": "u2", "since": "2026-01-01"},
    )
    assert query_of(rec.requests[0]) == {"_updated_at": {"$gte": "2026-01-01T00:00:00.000Z"}}
    assert [a["_id"] for a in result["awards"]] == ["a1", "a3"]
    assert result["matched_total"] == 2


async def test_user_recognition_totals_active_points(monkeypatch):
    awards = [
        {"toUsers": ["u1"], "badgePoints": 10},
        {"toUsers": ["u1"], "badgePoints": 5, "dateCancelled": "2026-05-01"},
        {"toUsers": ["u2"], "badgePoints": 500},
        {"toUsers": ["u2", "u1"], "badgePoints": 3},
    ]
    rec = Recorder({"badgehistory": page(awards)})
    result = await _call(monkeypatch, rec, "crewhu_badges_user_recognition", {"user_id": "u1"})
    assert (result["count"], result["active_awards"], result["active_points_in_page"]) == (3, 2, 13)


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


async def test_update_contest_legacy_params_explain_missing_endpoint(monkeypatch):
    rec = Recorder()
    result = await _call(
        monkeypatch, rec, "crewhu_badges_update_contest", {"id": "c1", "name": "Q3", "description": "x", "target": 10}
    )
    assert result["error"]["code"] == "invalid_argument"
    message = result["error"]["message"]
    assert "no endpoint to change a contest's name, description or target" in message
    assert "metric_token" in message and "timeframe" in message and "data" in message
    assert rec.requests == []


async def test_update_contest_without_arguments(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "crewhu_badges_update_contest", {})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


@pytest.mark.parametrize(
    "args,missing",
    [
        ({"metric_token": "timemetric"}, "timeframe, data"),
        ({"timeframe": "WK", "data": [{"identificator": "jdoe", "value": 1}]}, "metric_token"),
        ({"id": "c1", "metric_token": "timemetric", "timeframe": "MO"}, "data"),
    ],
)
async def test_update_contest_needs_all_metric_params(monkeypatch, args, missing):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "crewhu_badges_update_contest", args)
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["message"].endswith(f"missing: {missing}.")
    assert rec.requests == []


async def test_update_contest_ignores_legacy_params_with_metrics(monkeypatch):
    rec = Recorder({"POST contest/metrics": {"ok": True}})
    args = {"id": "c1", "metric_token": "timemetric", "timeframe": "DY", "data": [{"identificator": "jdoe", "value": 2}]}
    result = await _call(monkeypatch, rec, "crewhu_badges_update_contest", args)
    assert result["updated"] is True
    assert json.loads(rec.calls("contest/metrics")[0].content)["metricToken"] == "timemetric"


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


# ---- PRD-20987: one record by id ----------------------------------------------------------


@pytest.mark.parametrize(
    "tool,resource", [("crewhu_badges_get", "badge"), ("crewhu_prizes_get", "prize"), ("crewhu_users_get", "user")]
)
async def test_get_tools_return_the_requested_record(monkeypatch, tool, resource):
    records = [{"_id": "62b1f35ee54ade5e8a6223f6"}, {"_id": "62b1f35ee54ade5e8a6223ff", "name": "wanted"}]
    rec = Recorder({resource: page(records)})
    assert (await _call(monkeypatch, rec, tool, {"id": "62b1f35ee54ade5e8a6223ff"}))["name"] == "wanted"
    missing = await _call(monkeypatch, rec, tool, {"id": "000000000000000000000000"})
    assert missing["error"]["code"] == "not_found"


async def test_survey_get_scans_from_the_creation_time_in_its_id(monkeypatch):
    rec = Recorder({"survey": page([{"_id": "62bf4f3cd3de20549a6ec145"}, {"_id": "62d065c27441a44c87a74c32", "rating": "5"}])})
    result = await _call(monkeypatch, rec, "crewhu_surveys_get", {"id": "62d065c27441a44c87a74c32"})
    assert result["_id"] == "62d065c27441a44c87a74c32" and result["sentiment"] == "positive"
    # 0x62d065c2 = 2022-07-14T18:51:46Z, less one minute
    assert query_of(rec.requests[0]) == {"_updated_at": {"$gte": "2022-07-14T18:50:46.000Z"}}


# ---- PRD-20988: equality filters are matched locally -----------------------------------------


async def test_prize_history_filters_by_user_prize_and_status(monkeypatch):
    rows = [
        {"_id": "r1", "user": "u1", "prize": "p1", "status": "STS_PRZ_PROCESSED"},
        {"_id": "r2", "user": "u1", "prize": "p1", "status": "STS_PRZ_CANCELLED"},
        {"_id": "r3", "user": "u2", "prize": "p1", "status": "STS_PRZ_PROCESSED"},
        {"_id": "r4", "user": {"_id": "u1"}, "prize": {"_id": "p1"}, "status": "sts_prz_processed"},
    ]
    rec = Recorder({"prizehistory": page(rows)})
    result = await _call(
        monkeypatch, rec, "crewhu_prizes_history_list", {"user_id": "u1", "prize_id": "p1", "status": "STS_PRZ_PROCESSED"}
    )
    assert "query" not in rec.requests[0].url.params
    assert [r["_id"] for r in result["redemptions"]] == ["r1", "r4"]
    mine = await _call(monkeypatch, rec, "crewhu_prizes_user_redemptions", {"user_id": "u2"})
    assert [r["_id"] for r in mine["redemptions"]] == ["r3"]


async def test_filtered_scan_reads_further_pages_and_reports_where_it_stopped(monkeypatch):
    def reply(request):
        step = int(request.url.params["step"])
        rows = [{"_id": f"a{step}-{i}", "toUsers": ["u1" if i == 0 else "u9"]} for i in range(100)]
        return httpx.Response(200, json=page(rows, total=5000, step=step, more=True))

    rec = Recorder({"badgehistory": reply})
    result = await _call(monkeypatch, rec, "crewhu_badges_history_list", {"user_id": "u1", "limit": 50})
    assert len(rec.requests) == 20  # the per-call scan cap
    assert result["count"] == 20 and result["scan_incomplete"] is True
    assert result["next_offset"] == 2000 and "matched_total" not in result
    nxt = await _call(monkeypatch, rec, "crewhu_badges_history_list", {"user_id": "u1", "limit": 2, "offset": 2000})
    assert [a["_id"] for a in nxt["awards"]] == ["a21-0", "a22-0"] and nxt["next_offset"] == 2101


# ---- PRD-20989: paging survives truncation ----------------------------------------------------


def _big_surveys(count):
    rows = [{"_id": f"s{i:03d}", "rating": "5", **{f"f{k}": k * 1000 + i for k in range(40)}} for i in range(count)]

    def reply(request):
        step, size = int(request.url.params["step"]), int(request.url.params["limit"])
        chunk = rows[(step - 1) * size : step * size]
        return httpx.Response(200, json=page(chunk, total=count, step=step, limit=size, more=step * size < count))

    return reply


async def test_truncated_page_continues_right_after_the_last_record(monkeypatch):
    rec = Recorder({"survey": _big_surveys(163)})
    seen, args, calls = [], {"limit": 100}, 0
    while True:
        result = await _call(monkeypatch, rec, "crewhu_surveys_list", args)
        calls += 1
        assert "next_step" not in result or not result.get("truncated")
        seen += [s["_id"] for s in result["surveys"]]
        if result.get("next_offset") is None:
            break
        args = {"limit": 100, "offset": result["next_offset"]}
        assert calls < 20
    assert result.get("truncated") is None or calls > 2
    assert seen == [f"s{i:03d}" for i in range(163)]  # no gaps, no duplicates


async def test_step_paging_still_works_when_nothing_is_cut(monkeypatch):
    rec = Recorder({"badge": page([{"_id": f"b{i}"} for i in range(30)], total=30)})
    first = await _call(monkeypatch, rec, "crewhu_badges_list", {"limit": 10, "step": 2})
    assert [b["_id"] for b in first["badges"]] == [f"b{i}" for i in range(10, 20)]
    assert (first["step"], first["next_step"], first["next_offset"], first["total"]) == (2, 3, 20, 30)


# ---- PRD-20990: search the newest surveys first ------------------------------------------------


async def test_survey_search_reads_newest_first_and_reports_the_range(monkeypatch):
    def reply(request):
        step, size = int(request.url.params["step"]), int(request.url.params["limit"])
        rows = [
            {"_id": f"s{n}", "summary": "Laptop slow" if n == 4980 else "Printer", "_created_at": f"2026-01-01T00:00:{n % 60:02d}Z"}
            for n in range((step - 1) * size, min(step * size, 4984))
        ]
        return httpx.Response(200, json=page(rows, total=4984, step=step, limit=size, more=step * size < 4984))

    rec = Recorder({"survey": reply})
    result = await _call(monkeypatch, rec, "crewhu_surveys_search", {"query": "laptop", "limit": 5})
    assert [s["_id"] for s in result["surveys"]] == ["s4980"]
    assert [r.url.params["step"] for r in rec.requests[:3]] == ["1", "50", "49"]
    assert result["order"] == "newest first" and result["searched_all"] is False
    assert len(rec.requests) == 1 + 20 and result["searched"] == 1984
    assert set(result["searched_range"]) == {"newest_created_at", "oldest_created_at"}



# ---- PRD-21052: recognition counts match the awards returned ---------------------------------


async def test_user_recognition_counts_only_the_awards_returned_on_a_cut_page(monkeypatch):
    awards = [
        {"_id": f"a{i:03d}", "toUsers": ["u1"], "badgePoints": 50 + i, "message": "well done " * 30,
         **({"dateCancelled": "2026-05-01"} if i % 7 == 0 else {}), **{f"f{k}": k for k in range(30)}}
        for i in range(120)
    ]
    rec = Recorder({"badgehistory": lambda r: httpx.Response(200, json=page(
        awards[(int(r.url.params["step"]) - 1) * 100 : int(r.url.params["step"]) * 100],
        total=120, step=int(r.url.params["step"]), more=r.url.params["step"] == "1"))})
    seen, total_awards, total_points, args = [], 0, 0, {"user_id": "u1", "limit": 100}
    while True:
        result = await _call(monkeypatch, rec, "crewhu_badges_user_recognition", args)
        returned = result["awards"]
        active = [a for a in returned if not a.get("dateCancelled")]
        assert result["active_awards"] == len(active)
        assert result["active_points_in_page"] == sum(a["badgePoints"] for a in active)
        seen += [a["_id"] for a in returned]
        total_awards += result["active_awards"]
        total_points += result["active_points_in_page"]
        if result.get("next_offset") is None:
            break
        args = {"user_id": "u1", "limit": 100, "offset": result["next_offset"]}
    assert seen == [a["_id"] for a in awards]  # no gaps, no duplicates
    real = [a for a in awards if "dateCancelled" not in a]
    assert (total_awards, total_points) == (len(real), sum(a["badgePoints"] for a in real))


async def test_user_recognition_reports_whole_user_totals_when_all_were_read(monkeypatch):
    awards = [{"toUsers": ["u1"], "badgePoints": 10}, {"toUsers": ["u1"], "badgePoints": 5, "dateCancelled": "x"},
              {"toUsers": ["u2"], "badgePoints": 99}]
    rec = Recorder({"badgehistory": page(awards)})
    result = await _call(monkeypatch, rec, "crewhu_badges_user_recognition", {"user_id": "u1"})
    assert (result["user_total_awards"], result["user_total_points"]) == (1, 10)
