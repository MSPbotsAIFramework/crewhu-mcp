# Crewhu MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Crewhu](https://www.crewhu.com) Open
API, the customer-satisfaction and employee-recognition platform used by MSP service desks. It
lets an AI agent read CSAT survey responses (including detractors and promoters), employees,
badges and the recognition feed, the prize catalog and prize redemptions, and, on explicit
request, post External Metric results to contest leaderboards.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the token arrives on every request as an HTTP header and is
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Crewhu-Api-Token` | Yes | string | none | - | Crewhu Open API token (a leading `Bearer ` is accepted and ignored) | `your-crewhu-api-token` |

Where to get it:

- In the Crewhu web app open **Settings -> Integrations -> API**, click **+Add**, give the token
  a description, **Confirm**, then **Copy Token**.
- `crewhu_badges_update_contest` additionally needs **Contests update via API** enabled under
  **Settings -> Contests**, and a contest that uses an External Metric.

How the token is sent to Crewhu: every call goes to `https://api.crewhu.com/api/v1/...` with
the token in the `X_CREWHU_APITOKEN` header (the name Crewhu expects, with underscores).

A request to `/mcp` without the header gets HTTP 401 naming it.

## Tools

All tools return a JSON string. List tools take `limit` (max 100, Crewhu's limit) and
`offset` (record position from 0) or the older `step` (page number from 1), and return the
records plus `{count, total, offset, has_more, next_offset}`; pass `next_offset` as `offset` for
the next page (`step` / `next_step` are kept for step paging). Crewhu lists records oldest first,
so use `since` for recent ones.
Ids are Crewhu `_id` strings; dates are ISO 8601 UTC. Survey records get a computed
`sentiment` (`positive` for rating 5, `neutral` for 0, `negative` for -5).

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `crewhu_surveys_list` | List CSAT survey responses: customer, rating / sentiment, comment, ticket, credited employees | limit, step, offset, sentiment (`positive`, `neutral`, `negative`), since, include_inactive |
| `crewhu_surveys_get` | One survey, including tags, review flag and credited employee ids | **id** |
| `crewhu_surveys_search` | Search surveys by comment, ticket summary / number or customer, newest first | **query**, limit, since |
| `crewhu_surveys_detractors` | Recent negative surveys (default last 30 days) for service recovery | limit, since |
| `crewhu_surveys_promoters` | Recent positive surveys (default last 30 days) | limit, since |
| `crewhu_users_list` | List employees: name, employee id, position, department, location | limit, step, offset, department, include_inactive |
| `crewhu_users_get` | One employee, including hire date and managed departments / locations | **id** |
| `crewhu_users_search` | Find employees by name, employee id, department, position or location | **query**, limit |
| `crewhu_badges_list` | List the badges that can be awarded | limit, step, offset |
| `crewhu_badges_get` | One badge | **id** |
| `crewhu_badges_history_list` | Badge awards (recognition feed): giver, recipients, message, points, cancellation | limit, step, offset, user_id, badge_id, awarded_by, since |
| `crewhu_badges_user_recognition` | Badges one user received, with the points of awards not cancelled | **user_id**, limit, offset |
| `crewhu_badges_update_contest` | Post External Metric results to contest leaderboards (**write**) | metric_token, timeframe (`DY`, `WK`, `MO`, `QT`, `YR`), data (`[{identificator, value}]`) - all three needed at runtime; legacy id, name, description, target (accepted, not supported) |
| `crewhu_prizes_list` | Prize catalog: description (the title), points cost, taxable flag, link | limit, step, offset, available_only |
| `crewhu_prizes_get` | One prize | **id** |
| `crewhu_prizes_history_list` | Prize redemptions: prize, user, points, quantity, status, dates | limit, step, offset, user_id, prize_id, status, since |
| `crewhu_prizes_user_redemptions` | Prizes one user redeemed | **user_id**, limit, offset |
| `crewhu_prizes_pending_redemptions` | Redemptions still waiting to be processed | limit |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Crewhu returned HTTP 401 for survey: API_ERR_INV_AUTH_PARAM (the API token is not valid)", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`; Crewhu's error tokens (`API_ERR_*`) are kept and explained.
Reads are retried with backoff on 429, 5xx and network errors; the one write is retried only
on 429.

## Run locally

```bash
uv sync
uv run python -m crewhu_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t crewhu-mcp:dev .
docker run --rm -p 8080:8080 crewhu-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). The token is never read from
the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Crewhu-Api-Token: your-crewhu-api-token" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# negative surveys since June
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Crewhu-Api-Token: your-crewhu-api-token" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"crewhu_surveys_detractors","arguments":{"since":"2026-06-01T00:00:00Z"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (18).** Kept as is because the tool names are an existing contract.
- **`crewhu_badges_update_contest` parameters.** Older versions took `id`, `target`, `name` and
  `description`, which match no documented Crewhu endpoint. The tool sends the documented
  `POST /v1/contest/metrics` body: `metric_token`, `timeframe` and `data`
  (`[{identificator, value}]`, where `identificator` is an employee id or email). Values
  replace the leaderboard's current ones; it is the only write tool. For compatibility no
  parameter is required in the schema: a call with only the old parameters returns
  `invalid_argument` explaining that the Crewhu Open API cannot change a contest's name,
  description or target and that metrics are sent with `metric_token` / `timeframe` / `data`;
  a call with some but not all of those three returns `invalid_argument` naming the missing ones.
- **Contract test.** `tests/contract_baseline.json` holds the deployed and upstream tool schemas;
  `tests/test_contract.py` checks every tool still exists, accepts every parameter of both and
  requires nothing the deployed version did not.
- **Client-side filtering.** The Open API filters only by update date (`_updated_at`) and the
  active flag (`inactive`); it ignores any other field in `query`, and has no text search or
  sort. Department, user, badge, giver, prize, status, sentiment, search and "pending" status
  are matched here. Filtered lists read up to 2,000 records per call (20 pages of 100) and
  return `total_unfiltered` (Crewhu's count), `scanned`, `matched_total` when the whole
  collection was read, or `scan_incomplete` with `next_offset` to continue. Detractors /
  promoters read up to 1,000 surveys in the window, pending redemptions up to 1,000
  redemptions. `crewhu_surveys_search` reads up to 2,000 of the most recent surveys, newest
  first, and returns the `searched_range`; `crewhu_users_search` reads the first `limit` users.
- **Paging.** Crewhu pages by `step` only, so pages of 100 are read and sliced to `offset` /
  `limit`. When a result is cut to fit the size limit, `next_offset` points right after the
  last record returned, so following it never skips records.
- **JSON output.** Results are structured JSON (with a computed survey `sentiment`) instead of
  formatted text.
- **Single records.** The Open API has no item route and ignores an `_id` filter, so `*_get`
  tools read the collection and return the record whose `_id` matches (`not_found` otherwise).
  Survey ids encode their creation time, so `crewhu_surveys_get` only reads surveys updated
  since then.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
