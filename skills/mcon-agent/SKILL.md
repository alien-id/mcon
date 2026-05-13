---
name: mcon-agent
description: Manage personal mCon dashboards on behalf of your human owner. Create accounts, dashboards, projects, and steps via the mCon REST API, authenticated with your Alien Agent ID over RFC 9449 DPoP.
license: MIT
compatibility: Any AI agent with shell access, Node.js 18+, and an Alien Agent ID v3+ (DPoP)
metadata:
  author: mCon
  version: "2.0.0"
allowed-tools: Bash(alien-agent-id:*) Bash(node:*) Bash(curl:*) Bash(jq:*)
---

# mCon — Agent-Managed Dashboards for Your Human Owner

You are an AI agent. mCon is a multi-tenant dashboard service where every
project card and checklist item is created and updated **by an agent** on
behalf of its **human owner**. Humans only read; you write.

This skill teaches you how to:

1. Authenticate to mCon with your Alien Agent ID using RFC 9449 DPoP
2. Register your account and dashboards (created on first authenticated call)
3. Create, update, and delete projects and steps
4. Respect the per-agent and per-owner quotas

## 1) Prerequisites

You must have an **Alien Agent ID** (v3 or later — DPoP-enabled) already
bound to a human owner. If you don't, run the [`alien-agent-id`](https://alien.org/agent-id)
skill first. The minimum requirement is:

```bash
alien-agent-id status
# → "bound": true, "owner": "0000000301..."
```

If `bound` is `false`, stop here and complete the Alien Agent ID setup.
mCon rejects requests without a valid DPoP-bound access token with `401`.

You also need:

- **Node.js 18+** (for the `alien-agent-id` CLI)
- **`curl` and `jq`** (or any HTTP client of your choice)
- The mCon **base URL** — the same origin you fetched this skill from.
  All API paths below are relative to it. (For a local dev install with
  no public origin, default to `http://localhost:8765`.)

### Resolve the CLI path

If `alien-agent-id` is installed globally (`npm install -g alien-agent-id`)
the bare command works. Otherwise resolve the absolute path to `cli.mjs`
shipped with the skill:

```bash
# Either:
ALIEN_CLI="alien-agent-id"                                # globally installed
# OR:
ALIEN_CLI="node /absolute/path/to/alien-agent-id/cli.mjs" # local checkout
```

All examples below assume `$ALIEN_CLI` is set.

## 2) The auth headers (RFC 9449 DPoP)

mCon authenticates every write call using the **two-header DPoP form** from
RFC 9449. Each request carries:

```
Authorization: DPoP <access_token>
DPoP:          <proof JWT>
```

- The **access token** is an Alien-SSO-issued `at+jwt` (RFC 9068) that names
  your owner (`sub`), pins your agent key (`cnf.jkt`), and has a short
  lifetime (~5 minutes). It's reusable across many requests until it expires.
- The **DPoP proof** is a fresh per-request JWT signed by your agent key,
  binding *that specific request* to *that specific access token* via the
  request method (`htm`), URL (`htu`), and access-token hash (`ath`).

Because the proof is bound to the target URL, you must regenerate it for
every API call. The CLI does both halves in one shot:

```bash
ALIEN_CLI="<see above>"
MCON="<the origin you fetched this skill from>"     # e.g. https://mcon.alien.org

# Mint a DPoP-bound header pair for a specific request. Captures method + URL
# into the proof; the access_token is reused from the cached SSO session.
auth() {
  local method="$1" url="$2"
  local pair
  pair=$($ALIEN_CLI auth-header --method "$method" --url "$url") || return 1
  AUTH_HEADER=$(printf '%s' "$pair" | jq -r .authorization)
  DPOP_HEADER=$(printf '%s' "$pair" | jq -r .dpop)
}

# Use it:
auth GET "$MCON/api/me"
curl -s -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" "$MCON/api/me"
```

> The CLI also accepts `--raw` to print two `Header: value` lines instead
> of JSON. Prefer the JSON form above — it's robust to whitespace, doesn't
> need `eval`, and gives you the two header values as separate variables.

**Read endpoints (`GET`) are public — no auth required.**
**Write endpoints (`POST`, `PATCH`, `PUT`, `DELETE`) require both headers.**

> **Don't precompute headers and reuse them.** Each proof's `jti` is
> single-use; mCon's verifier rejects replays. Re-run `auth` for every
> request, even back-to-back ones against the same URL.

## 3) Quotas

mCon enforces these limits server-side:

| Limit | Cap | Returned status when exceeded |
| --- | --- | --- |
| Dashboards per agent | **5** | `409 Conflict` |
| Agents per human owner | **2** | `409 Conflict` (on first call from the third agent) |

You don't pre-register. The first authenticated call you make implicitly
registers you (subject to the per-owner cap). If your owner already has
two agents, **all** of your write calls will fail until the human deletes
one of the older agents via that agent's `DELETE /api/me` call.

## 4) Account routes

```bash
# Inspect yourself — registers you on the first call
auth GET "$MCON/api/me"
curl -s -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" "$MCON/api/me" | jq
# → {
#     "jkt": "f5d9fac4...",            // RFC 7638 thumbprint of your DPoP key
#     "owner": "00000003...",          // your bound human owner
#     "created_at": "...",
#     "dashboards": [ ... ],
#     "limits": {"dashboards_per_agent": 5, "agents_per_owner": 2}
#   }

# Delete your account (cascades to all your dashboards, projects, steps).
# Use this to free a slot for the per-owner agent quota.
auth DELETE "$MCON/api/me"
curl -X DELETE -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" "$MCON/api/me"
```

## 5) Dashboards

There is **no public list** of every dashboard — discovery is by URL only.
Dashboard ids are assigned by the server (≈96 bits of entropy, URL-safe) so
that the URL itself is the read capability. **You cannot pick the id.**
List your own dashboards via `GET /api/me`.

```bash
# Create — title required, description optional. The server returns the id.
auth POST "$MCON/api/dashboards"
curl -X POST \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$MCON/api/dashboards" \
  -d '{"title": "Work", "description": "Day job projects"}'
# → 409 if you already have 5 dashboards
# → 422 if you try to send `id` (or any unknown field)

# Capture the id from the response — note we re-auth for the new request
auth POST "$MCON/api/dashboards"
DID=$(curl -sX POST \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$MCON/api/dashboards" -d '{"title": "Side projects"}' | jq -r .id)

# Get one (anyone with the id; no auth, no headers needed)
curl -s "$MCON/api/dashboards/$DID" | jq

# Update title or description
auth PATCH "$MCON/api/dashboards/$DID"
curl -X PATCH \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID" -d '{"title": "Work — 2026"}'

# Delete (cascades to projects + steps)
auth DELETE "$MCON/api/dashboards/$DID"
curl -X DELETE -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  "$MCON/api/dashboards/$DID"
```

After creating a dashboard, send your human owner the URL
`<mcon-url>/d/<dashboard-id>` — that is how they read it. Treat the URL as a
capability: anyone who has it can read the dashboard. Don't paste it into
shared chats, public issues, or anywhere it might be indexed.

## 6) Projects

Projects belong to a dashboard. Path shape:
`/api/dashboards/{did}/projects/{pid}`.

```bash
DID=$YOUR_DASHBOARD_ID  # the id returned by POST /api/dashboards

# List (public)
curl -s "$MCON/api/dashboards/$DID/projects" | jq

# Create
URL="$MCON/api/dashboards/$DID/projects"
auth POST "$URL"
curl -X POST \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{
    "id": "site-rebuild",
    "title": "Rebuild site",
    "description": "Move to Astro, drop the WordPress install.",
    "scope": "frontend",
    "steps": [
      {"text": "Audit current pages", "done": true,
       "completed_at": "2026-04-22T14:00:00"},
      {"text": "Pick CMS"},
      {"text": "Migrate content"}
    ]
  }'

# Patch (any subset of title, description, scope, created_at)
URL="$MCON/api/dashboards/$DID/projects/site-rebuild"
auth PATCH "$URL"
curl -X PATCH \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{"scope": "infra"}'

# Clear the scope by sending null:
auth PATCH "$URL"
curl -X PATCH \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{"scope": null}'

# Upsert — replaces title, description, scope, AND all steps
auth PUT "$URL"
curl -X PUT \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{"title": "Rebuild site", "steps": [{"text": "Done"}]}'

# Delete
auth DELETE "$URL"
curl -X DELETE -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" "$URL"
```

## 7) Steps

A step is one checklist item. `done: false` is a todo, `done: true` is a
completed line that carries `completed_at`. Optional `type` can flag a
step that's blocked on the human or an external party — the dashboard
shows a different bullet icon and surfaces `details` as a hover tooltip.

```bash
DID=$YOUR_DASHBOARD_ID; PID=site-rebuild
STEPS="$MCON/api/dashboards/$DID/projects/$PID/steps"

# Append a normal todo
auth POST "$STEPS"
curl -X POST \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$STEPS" -d '{"text": "Set up build pipeline"}'

# Block on the human
auth POST "$STEPS"
curl -X POST \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$STEPS" -d '{
    "text": "Pick CMS",
    "type": "awaiting_human",
    "details": "Astro vs Eleventy — Astro has better DX, Eleventy ships zero JS."
  }'

# Block on someone outside
auth POST "$STEPS"
curl -X POST \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$STEPS" -d '{
    "text": "Hosting quote from Vercel",
    "type": "awaiting_external",
    "details": "Sales replied 2026-04-28 — numbers by Friday."
  }'

# Mark a step done now (server stamps completed_at)
SID=abc12345
URL="$STEPS/$SID"
auth PATCH "$URL"
curl -X PATCH \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{"done": true}'

# Mark done at a historical time
auth PATCH "$URL"
curl -X PATCH \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{"done": true, "completed_at": "2026-04-29T11:00:00"}'

# Unblock + clear context
auth PATCH "$URL"
curl -X PATCH \
  -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  -H "Content-Type: application/json" \
  "$URL" -d '{"type": null, "details": null}'

# Delete a step
auth DELETE "$URL"
curl -X DELETE -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" "$URL"
```

`type` must be one of `null`, `"awaiting_human"`, or `"awaiting_external"`
— other values return `422`.

## 8) Errors you should expect

mCon's 401 responses carry a `WWW-Authenticate: DPoP error="invalid_token",
error_description="<code>"` header (RFC 6750 §3.1 + RFC 9449 §7.1). The
`<code>` is a stable machine-readable label — read it from the header to
self-diagnose:

| Status | `error_description` (excerpts) | Likely cause |
| --- | --- | --- |
| `401` | `missing_authorization`, `invalid_scheme` | No `Authorization` header, or it didn't start with `DPoP `. |
| `401` | `missing_dpop_header` | `Authorization` present, `DPoP` header missing. Re-run `auth`. |
| `401` | `bad_proof_signature`, `bad_proof_typ`, `htm_mismatch`, `htu_mismatch` | Proof is malformed or doesn't bind to the request you actually sent. Common cause: `--method` or `--url` passed to `auth-header` didn't match the curl call. |
| `401` | `proof_stale`, `jti_replay` | Proof older than 30 s, or `jti` already seen. Re-run `auth` and try once more. |
| `401` | `jkt_mismatch`, `ath_mismatch`, `missing_cnf_jkt` | Access token and proof key don't agree. Your SSO session may be stale — run `$ALIEN_CLI refresh` then re-auth. |
| `401` | `bad_at_signature`, `iss_mismatch`, `aud_mismatch`, `at_expired` | Access token is invalid or expired for this service. Run `$ALIEN_CLI refresh`. |
| `403` | `dashboard belongs to another agent` | The path's dashboard isn't owned by your agent key. |
| `404` | — | Unknown dashboard, project, or step id. |
| `409` | — | Project id collision on create, or you hit a quota (5 dashboards / 2 agents per owner). |
| `422` | — | Validation failure — bad ISO timestamp, empty title, unknown `type`, etc. The body lists the offending field paths. |

The full code set is documented at
<https://github.com/alien-id/sso-sdk-py/blob/main/packages/agent-id/src/alien_sso_agent_id/types.py>;
they map 1:1 to RFC 9449 §4.3 and RFC 9068 §4 rejection branches.

## 9) Recipes

**Discover state across all your dashboards:**

```bash
auth GET "$MCON/api/me"
curl -s -H "Authorization: $AUTH_HEADER" -H "DPoP: $DPOP_HEADER" \
  "$MCON/api/me" | jq '.dashboards[] | {id, title}'
```

**Find every step blocked on the human** (read-only — no auth needed):

```bash
DID=$YOUR_DASHBOARD_ID
curl -s "$MCON/api/dashboards/$DID/projects" \
  | jq '.[] | .steps[] | select(.type == "awaiting_human") | {project: .project_id, text, details}'
```

**Track a long-running task** — create the project once, then PATCH steps as
work progresses. The dashboard reorders done items to the bottom and
timestamps them. Backfill history by passing `created_at` and
`completed_at` explicitly — both accept ISO 8601 (`2026-04-29T11:00:00`).

**Mark a project complete** — flip every step's `done` to `true`. The card
dims and moves to the bottom; the scope leaves the tab strip if no other
active project shares it.

**Refresh credentials mid-session** — if you start getting `at_expired` or
`bad_at_signature`, run:

```bash
$ALIEN_CLI refresh
```

This rotates the cached access token without re-prompting the human. The
next `auth` call will use the fresh token automatically.

## 10) After you're done

When you finish a session and there is nothing pending for the human,
double-check that any `awaiting_human` steps have a clear, specific
question in `details`. Vague prompts (e.g. "what do you think?") cost the
human time. Be concrete.

If you're handing the work off to a new instance of yourself, that
instance can pick up by listing your dashboards (`GET /api/me`) and
inspecting the steps. No state to migrate.
