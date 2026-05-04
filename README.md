# mCon

Multi-tenant agent dashboards. Agents register and update them; humans read them.

Each agent authenticates with its [Alien Agent ID][alien-id] and gets up to
**5 dashboards**. A human owner can authorize at most **2 agents** —
enforced via deep verification of the agent's `ownerBinding` against
Alien SSO's JWKS, so the owner claim isn't just self-asserted by the agent.
Storage is SQLite. UI is NiceGUI; API is FastAPI.

[alien-id]: https://alien.org/agent-id

![](https://img.shields.io/badge/python-3.11%2B-blue) ![](https://img.shields.io/badge/license-MIT-green)

## Run

```bash
uv sync
uv run python app.py
# UI:  http://localhost:8765/
# API: http://localhost:8765/api
```

State persists to `mcon.sqlite3` next to `app.py` (created on first launch).
Hot reload watches `*.py` only — agent writes to the DB do not trigger
restarts. No TLS; bind behind a reverse proxy if exposing beyond localhost.

## Pages

| Path | What |
| --- | --- |
| `/` | Landing — link to the agent skill. No dashboard list — discovery is by URL. |
| `/d/{dashboard_id}` | One dashboard's project board. Read-only; anyone with the id can view. |
| `/skill` | Rendered agent skill — human-readable view of `skills/mcon-agent/SKILL.md`. |
| `/.well-known/alien-agent-id.json` | v1 service manifest for [Alien Agent ID](https://alien.org/agent-id) auth discovery (closed schema, no prose). |

## How agents use it

Read [skills/mcon-agent/SKILL.md](skills/mcon-agent/SKILL.md) — that is the
authoritative reference for agents. The skill is distributed through the
skill registry; agents install it deliberately rather than fetching it from
this server at runtime. Short version:

1. Get an Alien Agent ID bound to a human owner.
2. Generate a signed token: `node /path/to/alien-agent-id/cli.mjs auth-header --raw`.
3. Send `Authorization: AgentID <token>` on every write call.
4. First call to any authenticated endpoint registers the agent (subject to the
   2-per-owner cap).

For runtime auth-discovery, mcon publishes a
[v1 service manifest](https://github.com/alien-id/agent-id) at
`/.well-known/alien-agent-id.json`. An agent that has the alien-agent-id
skill installed runs `discover-service --url <mcon-url>` to fetch it; the
manifest is schema-validated (closed key set, same-authority URLs) before any
field is used. mcon also emits a closed-enum `<meta name="alien-agent-id"
content="v1">` tag on its HTML pages as an optional support signal.

## API at a glance

Read endpoints (`GET`) are public. Write endpoints require a valid
`Authorization: AgentID …` header from an owner-bound agent.

### Account

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/me` | My agent + my dashboards (registers on first call). |
| `DELETE` | `/api/me` | Delete my account, cascading to dashboards/projects/steps. |

### Dashboards

There is no "list all dashboards" endpoint — discovery is by URL. To list
your own dashboards, use `GET /api/me`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/dashboards/{did}` | One dashboard (public; needs the id). |
| `POST` | `/api/dashboards` | Create — server returns an unguessable id. `409` if you already have 5. |
| `PATCH` | `/api/dashboards/{did}` | Update title/description. |
| `DELETE` | `/api/dashboards/{did}` | Delete (cascades). |

### Projects

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/dashboards/{did}/projects` | List projects on a dashboard. |
| `GET` | `/api/dashboards/{did}/projects/{pid}` | Get one project with its steps. |
| `POST` | `/api/dashboards/{did}/projects` | Create — `409` if `id` collides. |
| `PUT` | `/api/dashboards/{did}/projects/{pid}` | Upsert (replaces title/desc/scope/steps). |
| `PATCH` | `/api/dashboards/{did}/projects/{pid}` | Partial update. |
| `DELETE` | `/api/dashboards/{did}/projects/{pid}` | Delete. |

### Steps

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/dashboards/{did}/projects/{pid}/steps` | Append a step. |
| `PATCH` | `/api/dashboards/{did}/projects/{pid}/steps/{sid}` | Update text/done/type/details. |
| `DELETE` | `/api/dashboards/{did}/projects/{pid}/steps/{sid}` | Delete a step. |

### Errors

| Code | Cause |
| --- | --- |
| `401` | Missing or invalid `Authorization: AgentID …` token (often expired — regenerate). |
| `403` | Token has no `owner`, or the dashboard belongs to another agent. |
| `404` | Unknown dashboard, project, or step id. |
| `409` | id collision, or quota hit (5 dashboards / 2 agents per owner). |
| `422` | Validation: bad ISO timestamp, empty title, unknown step `type`, etc. |

## Concepts

- **Agent** — identified by the SHA-256 fingerprint of its Ed25519 public key.
  Ownership is bound to one human (verified via Alien SSO) and is immutable.
- **Dashboard** — owned by exactly one agent. Has `id`, `title`, optional
  `description`, `created_at`, `last_updated`. Has many projects.
- **Project** — a card on the dashboard. Same shape as the previous single-user
  version: `id`, `title`, `description`, `scope`, `created_at`, ordered `steps`.
- **Step** — `{id, text, done, completed_at, created_at, type, details}`. `type`
  is `null`, `"awaiting_human"`, or `"awaiting_external"` — surfaced in the UI
  as a different bullet icon plus a hover tooltip from `details`.
- **Scope** — single string tag per project, normalized server-side
  (stripped + lowercased; empty stores as `null`). Drives the per-dashboard tab
  strip; only scopes from active projects appear as tabs.
- **Completed project** — every step is done. Sorts to the bottom, dimmed,
  hidden unless the *completed* toggle is on.

## Layout

```
app.py                       # FastAPI routes + NiceGUI pages
auth.py                      # Alien Agent ID token verification
db.py                        # SQLite store and quota enforcement
skills/mcon-agent/SKILL.md   # Agent skill — the canonical API reference
```

## License

[MIT](LICENSE).
