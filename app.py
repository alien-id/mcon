from __future__ import annotations

import html as _html
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from nicegui import app, ui
from pydantic import BaseModel, field_validator

DATA_FILE = Path(__file__).parent / "dashboard.json"
_lock = threading.Lock()


def _load() -> dict:
    if not DATA_FILE.exists():
        return {"projects": []}
    try:
        return json.loads(DATA_FILE.read_text())
    except json.JSONDecodeError:
        return {"projects": []}


def _save(data: dict) -> None:
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(DATA_FILE)


class _Store:
    def __init__(self) -> None:
        self.data = _load()
        self.version = 0

    def bump(self) -> None:
        self.data["last_updated"] = _now_iso()
        _save(self.data)
        self.version += 1

    def find(self, pid: str) -> Optional[dict]:
        return next((p for p in self.data["projects"] if p["id"] == pid), None)


S = _Store()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _validate_iso(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return None
    try:
        datetime.fromisoformat(v)
        return v
    except ValueError as e:
        raise ValueError(f"must be ISO 8601 (got {v!r})") from e


def _norm_scope(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = s.strip().lower()
    return s or None


_STEP_TYPES = ("awaiting_human", "awaiting_external")


def _norm_details(d: Optional[str]) -> Optional[str]:
    if d is None:
        return None
    d = d.strip()
    return d or None


def _make_step(
    text: str,
    done: bool,
    completed_at: Optional[str],
    step_type: Optional[str] = None,
    details: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    if done:
        ts = completed_at or _now_iso()
    else:
        ts = None
    return {
        "id": _new_id(),
        "text": text,
        "done": done,
        "completed_at": ts,
        "type": step_type or None,
        "details": _norm_details(details),
        "created_at": created_at or _now_iso(),
    }


# ----------------------------- API models -----------------------------


class StepIn(BaseModel):
    text: str
    done: bool = False
    completed_at: Optional[str] = None
    created_at: Optional[str] = None
    type: Optional[str] = None
    details: Optional[str] = None

    @field_validator("completed_at", "created_at")
    @classmethod
    def _v(cls, v):
        return _validate_iso(v)

    @field_validator("type")
    @classmethod
    def _t(cls, v):
        if v is None or v == "":
            return None
        if v not in _STEP_TYPES:
            raise ValueError(
                f"must be one of: {', '.join(_STEP_TYPES)}, or null"
            )
        return v


class StepUpdate(BaseModel):
    text: Optional[str] = None
    done: Optional[bool] = None
    completed_at: Optional[str] = None
    created_at: Optional[str] = None
    type: Optional[str] = None
    details: Optional[str] = None

    @field_validator("completed_at", "created_at")
    @classmethod
    def _v(cls, v):
        return _validate_iso(v)

    @field_validator("type")
    @classmethod
    def _t(cls, v):
        if v is None or v == "":
            return None
        if v not in _STEP_TYPES:
            raise ValueError(
                f"must be one of: {', '.join(_STEP_TYPES)}, or null"
            )
        return v


class ProjectIn(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = None
    scope: Optional[str] = None
    created_at: Optional[str] = None
    steps: Optional[list[StepIn]] = None

    @field_validator("created_at")
    @classmethod
    def _v(cls, v):
        return _validate_iso(v)


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    scope: Optional[str] = None
    created_at: Optional[str] = None

    @field_validator("created_at")
    @classmethod
    def _v(cls, v):
        return _validate_iso(v)


# ----------------------------- API routes -----------------------------


@app.get("/api/projects")
def list_projects():
    return S.data["projects"]


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = S.find(pid)
    if not p:
        raise HTTPException(404)
    return p


@app.post("/api/projects")
def create_project(payload: ProjectIn):
    with _lock:
        pid = payload.id or _new_id()
        if S.find(pid):
            raise HTTPException(409, "project exists")
        proj = {
            "id": pid,
            "title": payload.title,
            "description": payload.description or "",
            "scope": _norm_scope(payload.scope),
            "steps": [
                _make_step(
                    s.text,
                    s.done,
                    s.completed_at,
                    s.type,
                    s.details,
                    s.created_at,
                )
                for s in (payload.steps or [])
            ],
            "created_at": payload.created_at or _now_iso(),
        }
        S.data["projects"].append(proj)
        S.bump()
        return proj


@app.put("/api/projects/{pid}")
def upsert_project(pid: str, payload: ProjectIn):
    with _lock:
        steps = [
            _make_step(s.text, s.done, s.completed_at)
            for s in (payload.steps or [])
        ]
        proj = S.find(pid)
        if proj:
            proj["title"] = payload.title
            proj["description"] = payload.description or ""
            proj["scope"] = _norm_scope(payload.scope)
            proj["steps"] = steps
            if payload.created_at is not None:
                proj["created_at"] = payload.created_at
        else:
            proj = {
                "id": pid,
                "title": payload.title,
                "description": payload.description or "",
                "scope": _norm_scope(payload.scope),
                "steps": steps,
                "created_at": payload.created_at or _now_iso(),
            }
            S.data["projects"].append(proj)
        S.bump()
        return proj


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, payload: ProjectUpdate):
    with _lock:
        proj = S.find(pid)
        if not proj:
            raise HTTPException(404)
        fields = payload.model_dump(exclude_unset=True)
        if "title" in fields:
            proj["title"] = fields["title"]
        if "description" in fields:
            proj["description"] = fields["description"] or ""
        if "scope" in fields:
            proj["scope"] = _norm_scope(fields["scope"])
        if "created_at" in fields:
            proj["created_at"] = fields["created_at"] or _now_iso()
        S.bump()
        return proj


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    with _lock:
        proj = S.find(pid)
        if not proj:
            raise HTTPException(404)
        S.data["projects"].remove(proj)
        S.bump()
        return {"ok": True}


@app.post("/api/projects/{pid}/steps")
def add_step(pid: str, payload: StepIn):
    with _lock:
        proj = S.find(pid)
        if not proj:
            raise HTTPException(404)
        step = _make_step(
            payload.text,
            payload.done,
            payload.completed_at,
            payload.type,
            payload.details,
            payload.created_at,
        )
        proj["steps"].append(step)
        S.bump()
        return step


@app.patch("/api/projects/{pid}/steps/{sid}")
def update_step(pid: str, sid: str, payload: StepUpdate):
    with _lock:
        proj = S.find(pid)
        if not proj:
            raise HTTPException(404)
        for s in proj["steps"]:
            if s["id"] != sid:
                continue
            fields = payload.model_dump(exclude_unset=True)
            if "text" in fields:
                s["text"] = fields["text"]
            if "done" in fields:
                s["done"] = bool(fields["done"])
            if "completed_at" in fields:
                s["completed_at"] = fields["completed_at"]
            if "type" in fields:
                s["type"] = fields["type"] or None
            if "details" in fields:
                s["details"] = _norm_details(fields["details"])
            if "created_at" in fields:
                s["created_at"] = fields["created_at"] or _now_iso()
            # If done was just flipped and the caller didn't set the timestamp
            # explicitly, manage it for them.
            if "done" in fields and "completed_at" not in fields:
                if s["done"] and not s.get("completed_at"):
                    s["completed_at"] = _now_iso()
                elif not s["done"]:
                    s["completed_at"] = None
            S.bump()
            return s
        raise HTTPException(404, "step not found")


@app.delete("/api/projects/{pid}/steps/{sid}")
def delete_step(pid: str, sid: str):
    with _lock:
        proj = S.find(pid)
        if not proj:
            raise HTTPException(404)
        before = len(proj["steps"])
        proj["steps"] = [s for s in proj["steps"] if s["id"] != sid]
        if len(proj["steps"]) == before:
            raise HTTPException(404, "step not found")
        S.bump()
        return {"ok": True}


# ----------------------------- UI helpers -----------------------------


def _esc(s: Optional[str]) -> str:
    return _html.escape(s or "")


def _fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except ValueError:
        return ""


def _relative(
    iso: Optional[str],
    suffix: str,
    zero: str,
    now: Optional[datetime] = None,
) -> str:
    if not iso:
        return ""
    try:
        start = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    now = now or datetime.now()
    secs = max(0, (now - start).total_seconds())
    if secs < 60:
        return zero

    def plural(n: int, unit: str) -> str:
        return f"{n} {unit}{'s' if n != 1 else ''} {suffix}"

    if secs < 3600:
        return plural(int(secs // 60), "minute")
    if secs < 86400:
        return plural(int(secs // 3600), "hour")
    if secs < 7 * 86400:
        return plural(int(secs // 86400), "day")
    if secs < 30 * 86400:
        return plural(int(secs // (7 * 86400)), "week")
    if secs < 365 * 86400:
        return plural(int(secs // (30 * 86400)), "month")
    return plural(int(secs // (365 * 86400)), "year")


def _humanize(iso: Optional[str], now: Optional[datetime] = None) -> str:
    return _relative(iso, "elapsed", "just started", now)


def _ago(iso: Optional[str], now: Optional[datetime] = None) -> str:
    return _relative(iso, "ago", "just now", now)


def _last_activity_iso() -> Optional[str]:
    last = S.data.get("last_updated")
    if last:
        return last
    candidates: list[str] = []
    for p in S.data["projects"]:
        if p.get("created_at"):
            candidates.append(p["created_at"])
        for s in p.get("steps", []):
            if s.get("completed_at"):
                candidates.append(s["completed_at"])
    return max(candidates) if candidates else None


def _step_stats(now: Optional[datetime] = None) -> tuple[int, int]:
    now = now or datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    today_n = 0
    week_n = 0
    for p in S.data["projects"]:
        for s in p.get("steps", []):
            if not s.get("done"):
                continue
            iso = s.get("completed_at")
            if not iso:
                continue
            try:
                ts = datetime.fromisoformat(iso)
            except ValueError:
                continue
            if ts >= week_start:
                week_n += 1
                if ts >= today_start:
                    today_n += 1
    return today_n, week_n


def _is_complete(p: dict) -> bool:
    steps = p.get("steps", [])
    return bool(steps) and all(s["done"] for s in steps)


_ICON_SPEECH = '<i class="material-icons-outlined" aria-hidden="true">sms</i>'
_ICON_CLOCK = '<i class="material-icons-outlined" aria-hidden="true">schedule</i>'


# ----------------------------- CSS -----------------------------


CSS = """
:root {
  --bg: #f6f3ec;
  --card: #ffffff;
  --ink: #1f2630;
  --muted: #8a8e96;
  --soft: #f0ece3;
  --accent: #0f766e;
  --done: #a8acb3;
  --border: #e7e3d8;
}
html, body { background: var(--bg) !important; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  color: var(--ink);
  font-feature-settings: "ss01", "cv11";
  -webkit-font-smoothing: antialiased;
}
.q-page, .nicegui-content {
  background: var(--bg) !important;
  padding: 0 !important;
  min-height: 0 !important;
}
.q-layout, .q-page-container { min-height: 0 !important; }

.header-bar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 18px 32px 4px 32px;
  max-width: 1400px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
  gap: 24px;
  flex-wrap: wrap;
}
.header-bar h1 {
  font-size: 1.35rem;
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: -0.02em;
  margin: 0;
}
.header-left {
  display: flex;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
}
.header-left .clock {
  color: var(--ink);
  font-weight: 500;
  font-size: 0.98rem;
  font-variant-numeric: tabular-nums;
}
.header-left .updated::before {
  content: "·";
  margin-right: 8px;
  color: var(--border);
}
@keyframes blink-colon { 50% { opacity: 0.18; } }
.clock-colon {
  animation: blink-colon 1s infinite;
  padding: 0 1px;
}
.header-meta {
  display: flex;
  gap: 18px;
  align-items: center;
  color: var(--muted);
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
  flex-wrap: wrap;
}
.header-meta .info { display: flex; gap: 14px; align-items: baseline; }

/* Toggles (Quasar q-toggle) */
.header-meta .q-toggle__label {
  font-size: 0.82rem !important;
  color: var(--muted);
  font-weight: 500;
}
.header-meta .q-toggle { padding: 0 !important; }

.tabs-row {
  display: flex;
  gap: 6px;
  padding: 2px 32px 2px 32px;
  max-width: 1400px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
  flex-wrap: wrap;
}
.tab {
  display: inline-flex;
  align-items: center;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 0.83rem;
  font-weight: 500;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease, color 0.15s ease;
  line-height: 1.4;
}
.tab:hover { background: var(--soft); color: var(--ink); }
.tab.active, .tab.active:hover {
  background: var(--ink);
  color: #ffffff;
}

.grid-wrap {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
  padding: 8px 32px 12px 32px;
  max-width: 1400px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
}
.dash-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 14px 20px 16px 20px;
  box-shadow: 0 1px 2px rgba(20, 20, 30, 0.03),
              0 12px 28px -20px rgba(20, 20, 30, 0.10);
  transition: opacity .25s ease, box-shadow .18s ease;
  display: flex;
  flex-direction: column;
}
.dash-card:hover {
  box-shadow: 0 1px 3px rgba(20, 20, 30, 0.05),
              0 18px 38px -18px rgba(20, 20, 30, 0.14);
}
.dash-card.complete {
  opacity: 0.55;
  filter: saturate(0.4);
}
.dash-card.complete:hover { opacity: 0.9; }

.dash-card .head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 1px;
}
.dash-card h2 {
  font-size: 1.02rem;
  font-weight: 600;
  line-height: 1.15;
  margin: 0;
  letter-spacing: -0.01em;
  flex: 1;
}
.dash-card .started {
  font-size: 0.74rem;
  color: var(--muted);
  margin: 0 0 9px 0;
  font-variant-numeric: tabular-nums;
}
.dash-card .desc {
  color: var(--muted);
  font-size: 0.85rem;
  margin: 0 0 10px 0;
  line-height: 1.45;
}
.progress {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 0.72rem;
  color: var(--muted);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.progress .bar {
  width: 56px; height: 4px;
  border-radius: 2px;
  background: var(--soft);
  overflow: hidden;
  position: relative;
}
.progress .bar i {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: var(--accent);
  display: block;
  border-radius: 2px;
}
.progress.done-all { color: var(--accent); }

.steps {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin-top: 4px;
}
.dash-card:has(.done-collapsed) .steps { margin-bottom: 12px; }
.step-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.9rem;
  line-height: 1.55;
  padding: 2px 6px;
  margin: 0 -6px;
  border-radius: 5px;
  transition: background 0.12s ease;
}
.step-row:hover { background: rgba(0, 0, 0, 0.035); }
.step-row .icon {
  font-size: 0.85rem;
  color: var(--muted);
  flex: 0 0 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.step-row .text {
  flex: 1 1 auto;
  word-break: break-word;
}
.step-row.todo .text { color: var(--ink); }
.step-row .icon i.material-icons,
.step-row .icon i.material-icons-outlined {
  font-size: 13px;
  line-height: 1;
}
.step-row .icon-human i.material-icons-outlined { transform: scaleX(-1); }

.step-row.done .ts {
  font-variant-numeric: tabular-nums;
  font-size: 0.74rem;
  color: var(--muted);
  flex: 0 0 76px;
  text-align: left;
}
.step-row.done .icon { color: var(--accent); }
.step-row.done .text {
  color: var(--done);
  text-decoration: line-through;
  text-decoration-thickness: 1px;
  text-decoration-color: var(--done);
}

.section-divider {
  height: 1px;
  background: var(--border);
  margin: 8px 0 6px 0;
  border: 0;
  opacity: 0.7;
}

.done-collapsed {
  font-size: 0.74rem;
  color: var(--accent);
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
  font-weight: 500;
  letter-spacing: 0.01em;
}

.footer {
  text-align: left;
  color: var(--muted);
  font-size: 0.78rem;
  font-variant-numeric: tabular-nums;
  padding: 0 32px 24px 32px;
  max-width: 1400px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
  letter-spacing: 0.01em;
}

.empty {
  grid-column: 1 / -1;
  text-align: center;
  color: var(--muted);
  padding: 100px 20px;
  font-size: 0.95rem;
  line-height: 1.6;
}
.empty code {
  background: var(--soft);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.85rem;
  color: var(--ink);
}
"""


# ----------------------------- Render -----------------------------


def _card_html(p: dict, expand_done: bool) -> str:
    steps = p.get("steps", [])
    done_steps = sorted(
        [s for s in steps if s["done"]],
        key=lambda s: s.get("completed_at") or "",
    )
    todo_steps = [s for s in steps if not s["done"]]
    done_count = len(done_steps)
    total = len(steps)
    pct = int(done_count / total * 100) if total else 0
    complete = total > 0 and done_count == total

    progress = ""
    if total:
        cls = "progress done-all" if complete else "progress"
        progress = (
            f'<span class="{cls}">{done_count}/{total}'
            f'<span class="bar"><i style="width:{pct}%"></i></span></span>'
        )

    started = ""
    if p.get("created_at"):
        started = f'<p class="started">{_humanize(p["created_at"])}</p>'

    desc = (
        f'<p class="desc">{_esc(p.get("description"))}</p>'
        if p.get("description")
        else ""
    )

    rows: list[str] = []
    for s in todo_steps:
        title_attr = (
            f' title="{_esc(s["details"])}"' if s.get("details") else ""
        )
        t = s.get("type")
        if t == "awaiting_human":
            icon = (
                '<span class="icon icon-human" '
                'aria-label="awaiting human input">'
                f"{_ICON_SPEECH}</span>"
            )
        elif t == "awaiting_external":
            icon = (
                '<span class="icon icon-external" '
                'aria-label="awaiting external input">'
                f"{_ICON_CLOCK}</span>"
            )
        else:
            icon = '<span class="icon">○</span>'
        rows.append(
            f'<div class="step-row todo"{title_attr}>'
            f"{icon}"
            f'<span class="text">{_esc(s["text"])}</span>'
            "</div>"
        )

    collapsed = ""
    if done_steps:
        if expand_done:
            if todo_steps:
                rows.append('<hr class="section-divider"/>')
            for s in done_steps:
                title_attr = (
                    f' title="{_esc(s["details"])}"' if s.get("details") else ""
                )
                rows.append(
                    f'<div class="step-row done"{title_attr}>'
                    f'<span class="ts">{_fmt_ts(s.get("completed_at"))}</span>'
                    '<span class="icon">✓</span>'
                    f'<span class="text">{_esc(s["text"])}</span>'
                    "</div>"
                )
        else:
            collapsed = f'<div class="done-collapsed">✓ {done_count} done</div>'

    if not steps:
        body = '<div class="desc">No steps yet.</div>'
    else:
        body = "".join(rows)
    card_class = "dash-card complete" if complete else "dash-card"
    return (
        f'<div class="{card_class}">'
        f'<div class="head"><h2>{_esc(p["title"])}</h2>{progress}</div>'
        f"{started}{desc}"
        f'<div class="steps">{body}</div>'
        f"{collapsed}"
        "</div>"
    )


def _grid_html(
    show_completed: bool,
    expand_done: bool,
    scope: Optional[str],
) -> str:
    all_projects = S.data["projects"]
    if not all_projects:
        return (
            '<div class="empty">'
            "No projects yet.<br/>"
            "Try <code>POST /api/projects</code> with "
            '<code>{"title": "..."}</code>.'
            "</div>"
        )
    if scope:
        projects = [p for p in all_projects if p.get("scope") == scope]
        if not projects:
            return (
                '<div class="empty">No projects in scope '
                f'&ldquo;{_esc(scope)}&rdquo;.</div>'
            )
    else:
        projects = all_projects
    active = [p for p in projects if not _is_complete(p)]
    completed = [p for p in projects if _is_complete(p)]
    ordered = active + (completed if show_completed else [])
    if not ordered:
        return (
            '<div class="empty">All projects complete. '
            'Toggle "completed" to see them.</div>'
        )
    return "".join(_card_html(p, expand_done) for p in ordered)


# ----------------------------- Page -----------------------------


@ui.page("/")
def index() -> None:
    ui.add_head_html(
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
        '<link href="https://fonts.googleapis.com/icon?family=Material+Icons"'
        ' rel="stylesheet">'
        '<link href="https://fonts.googleapis.com/icon?family=Material+Icons+Outlined"'
        ' rel="stylesheet">'
        f"<style>{CSS}</style>"
    )

    ui_state = {
        "show_completed": False,
        "expand_done": False,
        "scope": None,
    }
    cache = {
        "date": "",
        "clock": "",
        "updated": "",
        "info": "",
        "grid": "",
        "footer": "",
        "tabs_sig": None,
    }

    with ui.element("div").classes("header-bar"):
        with ui.element("div").classes("header-left"):
            ui.html("<h1>mCon</h1>")
            date_html = ui.html("").classes("date")
            clock_html = ui.html("").classes("clock")
            updated_html = ui.html("").classes("updated")
        with ui.element("div").classes("header-meta"):
            info_html = ui.html("")

            def on_done_toggle(e):
                ui_state["expand_done"] = bool(e.value)
                cache["grid"] = ""  # force re-render
                render_grid()

            def on_completed_toggle(e):
                ui_state["show_completed"] = bool(e.value)
                cache["grid"] = ""
                render_grid()

            ui.switch(
                "done",
                value=False,
                on_change=on_done_toggle,
            ).props("dense color=teal")
            ui.switch(
                "completed",
                value=False,
                on_change=on_completed_toggle,
            ).props("dense color=teal")

    tabs_row = ui.element("div").classes("tabs-row")
    grid_html = ui.html("").classes("w-full")
    footer_html = ui.html("").classes("footer")

    def render_header() -> None:
        now = datetime.now()
        clock = (
            now.strftime("%H")
            + '<span class="clock-colon">:</span>'
            + now.strftime("%M")
        )
        date = now.strftime("%a, %d.%m.%Y")
        projects = S.data["projects"]
        active = [p for p in projects if not _is_complete(p)]
        completed_n = len(projects) - len(active)
        open_n = sum(1 for p in active for s in p["steps"] if not s["done"])
        parts = [
            f"{len(active)} active",
            f"{open_n} open",
        ]
        if completed_n:
            parts.append(f"{completed_n} done")
        info = '<div class="info">' + " · ".join(parts) + "</div>"
        last_iso = _last_activity_iso()
        updated = f"updated {_ago(last_iso)}" if last_iso else ""
        if date != cache["date"]:
            cache["date"] = date
            date_html.set_content(date)
        if clock != cache["clock"]:
            cache["clock"] = clock
            clock_html.set_content(clock)
        if updated != cache["updated"]:
            cache["updated"] = updated
            updated_html.set_content(updated)
        if info != cache["info"]:
            cache["info"] = info
            info_html.set_content(info)

    def render_grid() -> None:
        html_str = (
            f'<div class="grid-wrap">'
            f'{_grid_html(ui_state["show_completed"], ui_state["expand_done"], ui_state["scope"])}'
            "</div>"
        )
        if html_str != cache["grid"]:
            cache["grid"] = html_str
            grid_html.set_content(html_str)

    def select_scope(target: Optional[str]) -> None:
        if ui_state["scope"] == target:
            return
        ui_state["scope"] = target
        cache["tabs_sig"] = None
        cache["grid"] = ""
        render_tabs()
        render_grid()

    def render_tabs() -> None:
        scopes = sorted({
            p.get("scope")
            for p in S.data["projects"]
            if not _is_complete(p) and p.get("scope")
        })
        if ui_state["scope"] is not None and ui_state["scope"] not in scopes:
            ui_state["scope"] = None
            cache["grid"] = ""
        if not scopes:
            if cache["tabs_sig"] != "empty":
                cache["tabs_sig"] = "empty"
                tabs_row.clear()
                tabs_row.style("display: none")
            return
        sig = (ui_state["scope"], tuple(scopes))
        if sig == cache["tabs_sig"]:
            return
        cache["tabs_sig"] = sig
        tabs_row.style("display: flex")
        tabs_row.clear()
        with tabs_row:
            def add_tab(label: str, target: Optional[str]) -> None:
                cls = "tab active" if ui_state["scope"] == target else "tab"
                lbl = ui.label(label).classes(cls)
                lbl.on("click", lambda e, t=target: select_scope(t))

            add_tab("All", None)
            for s in scopes:
                add_tab(s, s)

    def render_footer() -> None:
        today_n, week_n = _step_stats()
        text = f"{today_n} done today · {week_n} this week"
        if text != cache["footer"]:
            cache["footer"] = text
            footer_html.set_content(text)

    render_header()
    render_tabs()
    render_grid()
    render_footer()
    ui.timer(1.0, render_header)

    # Picks up server-side mutations and refreshes "X elapsed" labels.
    def refresh_state() -> None:
        render_tabs()
        render_grid()
        render_footer()

    ui.timer(2.0, refresh_state)


ui.run(
    title="mCon",
    favicon="📋",
    dark=False,
    port=8765,
    reload=True,
    show=False,
)
