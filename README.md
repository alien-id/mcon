# mCon

Personal dashboard updated by an agent over HTTP. Single Python file: NiceGUI for the UI, FastAPI for the REST API, JSON for storage.

The user does not edit cards. The agent does, by calling the API.

![](https://img.shields.io/badge/python-3.11%2B-blue) ![](https://img.shields.io/badge/license-MIT-green)

## Run

```bash
uv sync
uv run python app.py
# UI:  http://localhost:8765
# API: http://localhost:8765/api
```

State persists to `dashboard.json` next to `app.py`. Hot reload watches `*.py` only — agent writes to the data file do not trigger restarts. No authentication; bind behind a reverse proxy if exposing beyond localhost.

## Concepts

- **Project** — a card on the dashboard. Has `id`, `title`, optional `description`, optional `scope`, `created_at`, and an ordered list of steps.
- **Step** — a checklist item. `done: false` is a todo, `done: true` is completed and carries `completed_at`.
- **Scope** — a single string tag (e.g. `work`, `admin`, `research`). Normalized server-side: stripped + lowercased. Empty/whitespace stores as `null`. Drives the tab strip in the UI; only scopes from active projects appear as tabs.
- **Completed project** — every step is done. Sorts to the bottom of the grid, dimmed, hidden unless the *completed* toggle is on. Does not contribute scopes to the tab strip.
- **Timestamps** — ISO 8601 (`2026-04-20T14:38:00`). Provide explicitly to backfill history, or omit and let the server stamp `datetime.now()`. Invalid ISO returns `422`.

## Schemas

### Project

```json
{
  "id": "string",                      // 8-hex auto-generated if not supplied
  "title": "string",                   // required on create
  "description": "string",             // "" if absent
  "scope": "string | null",            // lowercased; null = unscoped
  "created_at": "2026-04-20T09:15:00", // ISO 8601
  "steps": [ /* Step */ ]
}
```

### Step

```json
{
  "id": "string",                      // 8-hex, server-generated, immutable
  "text": "string",                    // required
  "done": false,
  "completed_at": "string | null",     // ISO; null while not done
  "type": "string | null",             // null | "awaiting_human" | "awaiting_external"
  "details": "string | null"           // free-form context, shown only as hover tooltip
}
```

#### Step types

| `type` value | UI icon | Meaning |
| --- | --- | --- |
| `null` (default) | open circle | Agent can proceed on its own. |
| `"awaiting_human"` | speech bubble | Agent is blocked on input from the principal — answer a question, make a decision, provide a credential. |
| `"awaiting_external"` | clock | Agent is blocked on something outside both itself and the principal — an email reply, a scheduled event, a third party. |

`details` is for context the agent wants to keep with the step — *what* it needs from the human, *who* it's waiting on, links, deadlines. The dashboard only surfaces it as a native browser tooltip on hover; rows with details get a `cursor: help` cue. Once the step is done, type becomes irrelevant in display but `details` remains accessible by hovering.

## Endpoints

Base URL: `http://<host>:8765/api`. All bodies and responses are JSON.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/projects` | List all projects |
| `GET` | `/api/projects/{pid}` | Get one project |
| `POST` | `/api/projects` | Create project (`409` if `id` collides) |
| `PUT` | `/api/projects/{pid}` | Upsert: replaces `title`, `description`, `scope`, `steps` |
| `PATCH` | `/api/projects/{pid}` | Partial update (`title`, `description`, `scope`, `created_at`) |
| `DELETE` | `/api/projects/{pid}` | Delete the project |
| `POST` | `/api/projects/{pid}/steps` | Append a step |
| `PATCH` | `/api/projects/{pid}/steps/{sid}` | Partial update (`text`, `done`, `completed_at`) |
| `DELETE` | `/api/projects/{pid}/steps/{sid}` | Delete the step |

### `POST /api/projects`

```bash
curl -X POST http://localhost:8765/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "site-rebuild",
    "title": "Rebuild site",
    "description": "Move to Astro, drop the WordPress install.",
    "scope": "work",
    "created_at": "2026-04-20T09:15:00",
    "steps": [
      {"text": "Audit current pages", "done": true, "completed_at": "2026-04-22T14:00:00"},
      {"text": "Pick CMS"},
      {"text": "Migrate content"}
    ]
  }'
```

Returns the created project (with server-generated step ids). `409 Conflict` if `id` already exists.

### `PUT /api/projects/{pid}`

Upserts. Replaces all step ids since steps are recreated. If `created_at` is omitted on update, the existing value is preserved.

### `PATCH /api/projects/{pid}`

Partial update. Send only the fields you want to change. To clear `scope`, send `null` explicitly:

```bash
curl -X PATCH http://localhost:8765/api/projects/site-rebuild \
  -H 'Content-Type: application/json' -d '{"scope": "work"}'

# Clear the scope
curl -X PATCH http://localhost:8765/api/projects/site-rebuild \
  -H 'Content-Type: application/json' -d '{"scope": null}'
```

### `POST /api/projects/{pid}/steps`

Append. If `done: true` and `completed_at` is omitted, the server stamps now.

```bash
curl -X POST http://localhost:8765/api/projects/site-rebuild/steps \
  -H 'Content-Type: application/json' \
  -d '{"text": "Set up build pipeline"}'

# Blocked on a decision from the user
curl -X POST http://localhost:8765/api/projects/site-rebuild/steps \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Pick CMS",
    "type": "awaiting_human",
    "details": "Astro vs. Eleventy — Astro has better DX, Eleventy has zero JS by default."
  }'

# Blocked on an outside party
curl -X POST http://localhost:8765/api/projects/site-rebuild/steps \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Hosting quote from Vercel",
    "type": "awaiting_external",
    "details": "Sales replied 2026-04-28 promising numbers by Friday."
  }'
```

### `PATCH /api/projects/{pid}/steps/{sid}`

```bash
# Mark as done now
curl -X PATCH http://localhost:8765/api/projects/site-rebuild/steps/abc12345 \
  -H 'Content-Type: application/json' -d '{"done": true}'

# Mark as done at a historical time
curl -X PATCH http://localhost:8765/api/projects/site-rebuild/steps/abc12345 \
  -H 'Content-Type: application/json' \
  -d '{"done": true, "completed_at": "2026-04-29T11:00:00"}'

# Re-time an already-done step
curl -X PATCH http://localhost:8765/api/projects/site-rebuild/steps/abc12345 \
  -H 'Content-Type: application/json' \
  -d '{"completed_at": "2026-04-19T08:00:00"}'

# Edit text
curl -X PATCH http://localhost:8765/api/projects/site-rebuild/steps/abc12345 \
  -H 'Content-Type: application/json' \
  -d '{"text": "Set up Cloudflare Pages build"}'

# Block on the user (sets the type and the tooltip context)
curl -X PATCH http://localhost:8765/api/projects/site-rebuild/steps/abc12345 \
  -H 'Content-Type: application/json' \
  -d '{"type": "awaiting_human", "details": "Confirm budget cap before purchase."}'

# Unblock and clear context (null clears each field)
curl -X PATCH http://localhost:8765/api/projects/site-rebuild/steps/abc12345 \
  -H 'Content-Type: application/json' \
  -d '{"type": null, "details": null}'
```

Auto-stamping rules when `completed_at` is not provided in the payload:

- `done` flips `false → true` → `completed_at` is set to now.
- `done` flips `true → false` → `completed_at` is cleared.
- `done` unchanged → `completed_at` left alone.

When `completed_at` *is* provided, it's used verbatim (including `null` to clear).

### Errors

| Code | Cause |
| --- | --- |
| `404` | Unknown project or step id |
| `409` | `id` collision on `POST /api/projects` |
| `422` | Validation: missing required field, malformed ISO, etc. Body lists offending field paths. |

## Agent recipes

**Discover state:**

```bash
curl -s http://localhost:8765/api/projects | jq '.[] | {id, title, scope, open: ([.steps[]|select(.done==false)]|length), total: (.steps|length)}'
```

**Track a long-running task** — create the project once, then PATCH steps as work progresses. The dashboard reorders done items to the bottom and timestamps them.

**Block on user input** — when the agent can't proceed without a decision or fact from the human, mark the relevant step `type: "awaiting_human"` and write the question into `details`. A speech-bubble icon replaces the bullet; hover the row to see the question. Clear `type` (set to `null`) once they answer.

**Block on external dependency** — for waiting on email replies, calendar events, third-party SLAs, etc., use `type: "awaiting_external"` with `details` describing who/what and any deadline. A clock icon replaces the bullet. Useful for the agent's own bookkeeping too — `GET /api/projects` filtered by step type tells it what's still in flight.

**Reconstruct history** — `created_at` on the project and `completed_at` on each done step are honoured verbatim. An agent ingesting logs can backfill an accurate timeline.

**Mark a project complete** — flip every step's `done` to `true`. The card dims and moves to the bottom; the scope leaves the tab strip if no other active project shares it.

**Garbage-collect** — `DELETE /api/projects/{pid}` removes a project entirely. There is no soft-delete.

## License

[MIT](LICENSE).
