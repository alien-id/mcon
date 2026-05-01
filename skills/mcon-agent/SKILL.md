---
name: mcon-agent
description: Manage personal mCon dashboards on behalf of your human owner. Create accounts, dashboards, projects, and steps via the mCon REST API, authenticated with your Alien Agent ID.
license: MIT
compatibility: Any AI agent with shell access, Node.js 18+, and an Alien Agent ID
metadata:
  author: mCon
  version: "1.0.0"
allowed-tools: Bash(node:*) Bash(curl:*) Bash(jq:*)
---

# mCon — Agent-Managed Dashboards for Your Human Owner

You are an AI agent. mCon is a multi-tenant dashboard service where every
project card and checklist item is created and updated **by an agent** on
behalf of its **human owner**. Humans only read; you write.

This skill teaches you how to:

1. Authenticate to mCon with your Alien Agent ID
2. Register your account and dashboards (created on first authenticated call)
3. Create, update, and delete projects and steps
4. Respect the per-agent and per-owner quotas

## 1) Prerequisites

You must have an **Alien Agent ID** already bound to a human owner.
If you don't, run the `alien-agent-id` skill first
(<https://alien.org/agent-id>). The minimum requirement is:

```bash
node /path/to/alien-agent-id/cli.mjs status
# → "bound": true, "owner": "0000000301..."
```

If `bound` is `false`, stop here and complete the Alien Agent ID setup.
mCon rejects agents without a verified human owner with `403`.

You also need:

- **Node.js 18+** (for the Alien Agent ID CLI)
- **`curl` and `jq`** (or any HTTP client of your choice)
- The mCon **base URL** — ask the user, or default to
  `http://localhost:8765` for a local install.

## 2) The auth header

Every write request must include a fresh signed token from the Alien Agent
ID CLI. Tokens are valid for **5 minutes** — generate one per request, or
cache for the session and re-issue when needed.

```bash
# Resolve once
ALIEN_CLI="node /absolute/path/to/alien-agent-id/cli.mjs"
MCON="http://localhost:8765"  # or your deployed mcon URL

# One-shot helper
auth() { $ALIEN_CLI auth-header --raw; }
# Usage in curl:
curl -H "$(auth)" "$MCON/api/me"
```

The header looks like `Authorization: AgentID <base64url-json>`. mCon
verifies the Ed25519 signature, the timestamp, the
`fingerprint ↔ public-key` binding, and — for any token that claims an
`owner` — the embedded `ownerBinding` and `idToken` against Alien SSO's
JWKS. The Alien CLI's `auth-header` already includes the `ownerBinding`
and `idToken` automatically, so you don't need to do anything extra.

**Read endpoints (`GET`) are public — no auth required.**
**Write endpoints (`POST`, `PATCH`, `PUT`, `DELETE`) require auth.**

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
curl -H "$(auth)" "$MCON/api/me" | jq
# → {
#     "fingerprint": "f5d9fac4...",
#     "owner": "00000003...",
#     "created_at": "...",
#     "dashboards": [ ... ],
#     "limits": {"dashboards_per_agent": 5, "agents_per_owner": 2}
#   }

# Delete your account (cascades to all your dashboards, projects, steps).
# Use this to free a slot for the per-owner agent quota.
curl -X DELETE -H "$(auth)" "$MCON/api/me"
```

## 5) Dashboards

There is **no public list** of every dashboard — discovery is by URL only.
Dashboard ids are assigned by the server (≈96 bits of entropy, URL-safe) so
that the URL itself is the read capability. **You cannot pick the id.**
List your own dashboards via `GET /api/me`.

```bash
# Create — title required, description optional. The server returns the id.
curl -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards" -d '{"title": "Work", "description": "Day job projects"}'
# → 409 if you already have 5 dashboards
# → 422 if you try to send `id` (or any unknown field)

# Capture the id from the response
DID=$(curl -sX POST -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards" -d '{"title": "Side projects"}' | jq -r .id)

# Get one (anyone with the id; no auth)
curl -s "$MCON/api/dashboards/$DID" | jq

# Update title or description
curl -X PATCH -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID" -d '{"title": "Work — 2026"}'

# Delete (cascades to projects + steps)
curl -X DELETE -H "$(auth)" "$MCON/api/dashboards/$DID"
```

After creating a dashboard, send your human owner the URL
`<mcon-url>/d/<dashboard-id>` — that is how they read it. Treat the URL as a
capability: anyone who has it can read the dashboard. Don't paste it into
shared chats, public issues, or anywhere it might be indexed.

## 6) Projects

Projects belong to a dashboard. Same shape as before, but now nested:
`/api/dashboards/{did}/projects/{pid}`.

```bash
DID=$YOUR_DASHBOARD_ID  # the id returned by POST /api/dashboards

# List (public)
curl -s "$MCON/api/dashboards/$DID/projects" | jq

# Create
curl -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects" -d '{
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
curl -X PATCH -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/site-rebuild" \
  -d '{"scope": "infra"}'
# Clear the scope by sending null:
curl -X PATCH -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/site-rebuild" \
  -d '{"scope": null}'

# Upsert — replaces title, description, scope, AND all steps
curl -X PUT -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/site-rebuild" \
  -d '{"title": "Rebuild site", "steps": [{"text": "Done"}]}'

# Delete
curl -X DELETE -H "$(auth)" \
  "$MCON/api/dashboards/$DID/projects/site-rebuild"
```

## 7) Steps

A step is one checklist item. `done: false` is a todo, `done: true` is a
completed line that carries `completed_at`. Optional `type` can flag a
step that's blocked on the human or an external party — the dashboard
shows a different bullet icon and surfaces `details` as a hover tooltip.

```bash
DID=$YOUR_DASHBOARD_ID; PID=site-rebuild

# Append a normal todo
curl -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps" \
  -d '{"text": "Set up build pipeline"}'

# Block on the human
curl -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps" -d '{
    "text": "Pick CMS",
    "type": "awaiting_human",
    "details": "Astro vs Eleventy — Astro has better DX, Eleventy ships zero JS."
  }'

# Block on someone outside
curl -X POST -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps" -d '{
    "text": "Hosting quote from Vercel",
    "type": "awaiting_external",
    "details": "Sales replied 2026-04-28 — numbers by Friday."
  }'

# Mark a step done now (server stamps completed_at)
SID=abc12345
curl -X PATCH -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps/$SID" \
  -d '{"done": true}'

# Mark done at a historical time
curl -X PATCH -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps/$SID" \
  -d '{"done": true, "completed_at": "2026-04-29T11:00:00"}'

# Unblock + clear context
curl -X PATCH -H "$(auth)" -H "Content-Type: application/json" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps/$SID" \
  -d '{"type": null, "details": null}'

# Delete a step
curl -X DELETE -H "$(auth)" \
  "$MCON/api/dashboards/$DID/projects/$PID/steps/$SID"
```

`type` must be one of `null`, `"awaiting_human"`, or `"awaiting_external"`
— other values return `422`.

## 8) Errors you should expect

| Code | Cause |
| --- | --- |
| `401` | Missing or invalid `Authorization: AgentID …` token (likely expired — regenerate). |
| `403` | Token has no `owner` (your Alien Agent ID isn't bound to a human), or the dashboard belongs to another agent. |
| `404` | Unknown dashboard, project, or step id. |
| `409` | project id collision on create, or you hit a quota (5 dashboards / 2 agents per owner). |
| `422` | Validation failure — bad ISO timestamp, empty title, unknown `type`, etc. The body lists the offending field paths. |

## 9) Recipes

**Discover state across all your dashboards:**

```bash
curl -s -H "$(auth)" "$MCON/api/me" | jq '.dashboards[] | {id, title}'
```

**Find every step blocked on the human:**

```bash
DID=$YOUR_DASHBOARD_ID  # the id returned by POST /api/dashboards
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

## 10) After you're done

When you finish a session and there is nothing pending for the human,
double-check that any `awaiting_human` steps have a clear, specific
question in `details`. Vague prompts (e.g. "what do you think?") cost the
human time. Be concrete.

If you're handing the work off to a new instance of yourself, that
instance can pick up by listing your dashboards (`GET /api/me`) and
inspecting the steps. No state to migrate.
