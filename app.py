from __future__ import annotations

import html as _html
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request
from nicegui import app, ui
from pydantic import BaseModel, ConfigDict, field_validator

from auth import AgentIdentity, require_owned_agent
from db import (
    MAX_AGENTS_PER_OWNER,
    MAX_DASHBOARDS_PER_AGENT,
    Conflict,
    Forbidden,
    NotFound,
    QuotaExceeded,
    Store,
)

ROOT = Path(__file__).parent
DB_PATH = ROOT / "mcon.sqlite3"
SKILL_PATH = ROOT / "skills" / "mcon-agent" / "SKILL.md"

S = Store(DB_PATH)

_STEP_TYPES = ("awaiting_human", "awaiting_external")


def _validate_iso(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return None
    try:
        datetime.fromisoformat(v)
        return v
    except ValueError as e:
        raise ValueError(f"must be ISO 8601 (got {v!r})") from e


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
            raise ValueError(f"must be one of: {', '.join(_STEP_TYPES)}, or null")
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
            raise ValueError(f"must be one of: {', '.join(_STEP_TYPES)}, or null")
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


class DashboardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    description: Optional[str] = None


class DashboardUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


# --------------------------- error mapping ---------------------------


def _map(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, Conflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, QuotaExceeded):
        return HTTPException(409, str(exc))
    if isinstance(exc, Forbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    raise exc


def _ensure_agent(ident: AgentIdentity) -> dict:
    """Register the agent on first sight; enforce per-owner quota."""
    try:
        return S.upsert_agent(ident.fingerprint, ident.owner, ident.public_key_pem)
    except Exception as e:
        raise _map(e) from e


# --------------------------- account routes ---------------------------


@app.get("/api/me")
def me(ident: AgentIdentity = Depends(require_owned_agent)):
    agent = _ensure_agent(ident)
    return {
        "fingerprint": agent["fingerprint"],
        "owner": agent["owner"],
        "created_at": agent["created_at"],
        "dashboards": S.list_dashboards(ident.fingerprint),
        "limits": {
            "dashboards_per_agent": MAX_DASHBOARDS_PER_AGENT,
            "agents_per_owner": MAX_AGENTS_PER_OWNER,
        },
    }


@app.delete("/api/me")
def delete_me(ident: AgentIdentity = Depends(require_owned_agent)):
    try:
        S.delete_agent(ident.fingerprint)
    except Exception as e:
        raise _map(e) from e
    return {"ok": True}


# --------------------------- dashboard routes ---------------------------


@app.get("/api/dashboards/{did}")
def get_dashboard(did: str):
    try:
        return S.get_dashboard(did)
    except Exception as e:
        raise _map(e) from e


@app.post("/api/dashboards")
def create_dashboard(
    payload: DashboardIn,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        return S.create_dashboard(
            ident.fingerprint,
            title=payload.title,
            description=payload.description,
        )
    except Exception as e:
        raise _map(e) from e


@app.patch("/api/dashboards/{did}")
def patch_dashboard(
    did: str,
    payload: DashboardUpdate,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        return S.patch_dashboard(
            did, ident.fingerprint, payload.model_dump(exclude_unset=True)
        )
    except Exception as e:
        raise _map(e) from e


@app.delete("/api/dashboards/{did}")
def delete_dashboard(
    did: str,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        S.delete_dashboard(did, ident.fingerprint)
    except Exception as e:
        raise _map(e) from e
    return {"ok": True}


# --------------------------- project routes ---------------------------


@app.get("/api/dashboards/{did}/projects")
def list_projects(did: str):
    try:
        return S.list_projects(did)
    except Exception as e:
        raise _map(e) from e


@app.get("/api/dashboards/{did}/projects/{pid}")
def get_project(did: str, pid: str):
    try:
        return S.get_project(did, pid)
    except Exception as e:
        raise _map(e) from e


@app.post("/api/dashboards/{did}/projects")
def create_project(
    did: str,
    payload: ProjectIn,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    steps = [s.model_dump() for s in (payload.steps or [])]
    try:
        return S.create_project(
            did,
            ident.fingerprint,
            pid=payload.id,
            title=payload.title,
            description=payload.description,
            scope=payload.scope,
            created_at=payload.created_at,
            steps=steps,
        )
    except Exception as e:
        raise _map(e) from e


@app.put("/api/dashboards/{did}/projects/{pid}")
def upsert_project(
    did: str,
    pid: str,
    payload: ProjectIn,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    steps = [s.model_dump() for s in (payload.steps or [])]
    try:
        return S.upsert_project(
            did,
            ident.fingerprint,
            pid,
            title=payload.title,
            description=payload.description,
            scope=payload.scope,
            created_at=payload.created_at,
            steps=steps,
        )
    except Exception as e:
        raise _map(e) from e


@app.patch("/api/dashboards/{did}/projects/{pid}")
def patch_project(
    did: str,
    pid: str,
    payload: ProjectUpdate,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        return S.patch_project(
            did, ident.fingerprint, pid, payload.model_dump(exclude_unset=True)
        )
    except Exception as e:
        raise _map(e) from e


@app.delete("/api/dashboards/{did}/projects/{pid}")
def delete_project(
    did: str,
    pid: str,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        S.delete_project(did, ident.fingerprint, pid)
    except Exception as e:
        raise _map(e) from e
    return {"ok": True}


# --------------------------- step routes ---------------------------


@app.post("/api/dashboards/{did}/projects/{pid}/steps")
def add_step(
    did: str,
    pid: str,
    payload: StepIn,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        return S.add_step(did, ident.fingerprint, pid, payload.model_dump())
    except Exception as e:
        raise _map(e) from e


@app.patch("/api/dashboards/{did}/projects/{pid}/steps/{sid}")
def update_step(
    did: str,
    pid: str,
    sid: str,
    payload: StepUpdate,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        return S.patch_step(
            did, ident.fingerprint, pid, sid, payload.model_dump(exclude_unset=True)
        )
    except Exception as e:
        raise _map(e) from e


@app.delete("/api/dashboards/{did}/projects/{pid}/steps/{sid}")
def delete_step(
    did: str,
    pid: str,
    sid: str,
    ident: AgentIdentity = Depends(require_owned_agent),
):
    _ensure_agent(ident)
    try:
        S.delete_step(did, ident.fingerprint, pid, sid)
    except Exception as e:
        raise _map(e) from e
    return {"ok": True}


# ------------------------ service discovery ------------------------


@app.get("/.well-known/alien-agent-id.json")
def get_service_manifest(request: Request) -> dict:
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return {
        "version": 1,
        "service": {"name": "mcon", "url": origin},
        "auth": {"header": "Authorization", "scheme": "AgentID"},
        "api": {"base": f"{origin}/api"},
    }


# ============================== UI helpers ==============================


def _esc(s: Optional[str]) -> str:
    return _html.escape(s or "")


def _fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    # Stored timestamps are naive UTC (server clock is UTC). Mark them so
    # the browser can render in the viewer's local time via _TZ_JS below.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        f'<time datetime="{dt.isoformat()}" data-tz>'
        f'{dt.strftime("%d.%m %H:%M")}</time>'
    )


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


def _is_complete(p: dict) -> bool:
    steps = p.get("steps", [])
    return bool(steps) and all(s["done"] for s in steps)


_ICON_SPEECH = '<i class="material-icons-outlined" aria-hidden="true">sms</i>'
_ICON_CLOCK = '<i class="material-icons-outlined" aria-hidden="true">schedule</i>'


# ============================== CSS ==============================


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

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

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
.header-bar h1 a { color: inherit; }
.header-bar h1 a:hover { text-decoration: none; opacity: 0.8; }
.header-bar h1 .sep {
  color: var(--border);
  margin: 0 6px;
  font-weight: 400;
}
.header-bar h1 .dash-name { font-weight: 600; }
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
.dash-card.complete { opacity: 0.55; filter: saturate(0.4); }
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
.step-row .text { flex: 1 1 auto; word-break: break-word; }
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
  padding: 80px 20px;
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

/* --- landing (legacy, used by /skill) --- */
.landing {
  max-width: 880px;
  margin: 0 auto;
  padding: 32px 32px 64px 32px;
  box-sizing: border-box;
}
.landing h1 {
  font-size: 2.2rem;
  font-weight: 700;
  letter-spacing: -0.03em;
  margin: 0 0 6px 0;
  line-height: 1.05;
}
.landing .tag {
  color: var(--muted);
  font-size: 1.05rem;
  margin: 0 0 28px 0;
}

/* --- mission control landing --- */
.bg-grid {
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background-image:
    linear-gradient(rgba(15,118,110,0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(15,118,110,0.05) 1px, transparent 1px);
  background-size: 28px 28px;
  background-position: -1px -1px;
  -webkit-mask-image: radial-gradient(ellipse at 50% 0%, rgba(0,0,0,0.55), transparent 70%);
          mask-image: radial-gradient(ellipse at 50% 0%, rgba(0,0,0,0.55), transparent 70%);
}
.lp {
  position: relative; z-index: 1;
  max-width: 1000px; margin: 0 auto;
  padding: 56px 32px 72px 32px;
  box-sizing: border-box;
}
.hero { text-align: center; padding: 8px 0 36px 0; }
.live-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #10b981; display: inline-block;
  box-shadow: 0 0 0 0 rgba(16,185,129,0.5);
  animation: live-pulse 2s infinite;
}
@keyframes live-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(16,185,129,0.5); }
  70%  { box-shadow: 0 0 0 8px rgba(16,185,129,0); }
  100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); }
}
.hero h1 {
  font-size: clamp(2.4rem, 5.2vw, 3.6rem);
  font-weight: 700; letter-spacing: -0.04em;
  line-height: 1.02; margin: 0 0 18px 0;
}
.hero h1 .hi {
  background: linear-gradient(180deg, transparent 62%, rgba(15,118,110,0.18) 62%);
  padding: 0 4px; color: var(--accent);
}
.lede {
  max-width: 660px; margin: 0 auto;
  font-size: 1.08rem; line-height: 1.55;
  color: var(--ink); opacity: 0.78;
}
.cta {
  max-width: 660px;
  margin: 22px auto 0 auto;
  font-size: 1.28rem;
  line-height: 1.45;
  color: var(--ink);
  letter-spacing: -0.005em;
}
.cta strong {
  color: var(--accent);
  font-weight: 600;
}
.cta-row {
  display: flex; gap: 12px; justify-content: center;
  margin-top: 28px; flex-wrap: wrap;
}
.btn {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 0.92rem; font-weight: 500;
  padding: 10px 18px; border-radius: 8px;
  text-decoration: none;
  border: 1px solid transparent;
  transition: transform .15s ease, background .15s ease,
              color .15s ease, border-color .15s ease;
}
.btn:hover { transform: translateY(-1px); text-decoration: none; }
.btn.primary { background: var(--ink); color: #fff; }
.btn.primary:hover { background: var(--accent); }
.btn.ghost {
  background: transparent; color: var(--ink);
  border-color: var(--border);
}
.btn.ghost:hover { background: var(--card); border-color: var(--ink); }

.stats {
  display: flex; justify-content: center;
  margin: 4px auto 64px auto;
}
.stats-inner {
  display: flex;
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 4px;
  box-shadow: 0 1px 2px rgba(20,20,30,.03),
              0 12px 28px -20px rgba(20,20,30,.10);
}
.stat {
  padding: 12px 22px; text-align: center;
  border-right: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
  min-width: 84px;
}
.stat:last-child { border-right: 0; }
.stat .n {
  font-size: 1.4rem; font-weight: 700; color: var(--ink);
  display: block; line-height: 1;
}
.stat .l {
  font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--muted); font-weight: 600;
  margin-top: 5px; display: block;
}
.stats-foot {
  text-align: center;
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.04em;
  margin: -48px 0 56px 0;
  display: flex; align-items: center; justify-content: center; gap: 8px;
}
@media (max-width: 640px) {
  .stat { padding: 10px 14px; min-width: 64px; }
  .stat .n { font-size: 1.15rem; }
}

.preview {
  display: grid; grid-template-columns: 1fr 1.15fr;
  gap: 40px; align-items: center;
  margin-bottom: 72px;
}
.preview h3 {
  font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em;
  margin: 0 0 12px 0; line-height: 1.15;
}
.preview p {
  color: var(--ink); opacity: 0.78;
  font-size: 0.95rem; line-height: 1.55; margin: 0 0 8px 0;
}
.preview .legend { color: var(--muted); font-size: 0.82rem; margin-top: 14px; }
.preview .legend span { display: inline-block; margin-right: 14px; }
.preview .legend i.material-icons-outlined {
  font-size: 12px; vertical-align: -1px; color: var(--muted);
}
.preview-card-wrap { transform: rotate(-0.4deg); }
.preview-card-wrap .dash-card { box-shadow: 0 4px 14px rgba(20,20,30,.06),
                                            0 28px 60px -28px rgba(20,20,30,.18); }
@media (max-width: 720px) {
  .preview { grid-template-columns: 1fr; gap: 24px; }
  .preview-card-wrap { transform: none; }
}

.howto {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 16px; margin-bottom: 56px;
}
.step-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 22px 22px 20px 22px;
}
.step-card .num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; border-radius: 8px;
  background: var(--ink); color: #fff;
  font-size: 0.85rem; font-weight: 700; font-variant-numeric: tabular-nums;
  margin-bottom: 14px;
}
.step-card h4 {
  font-size: 0.98rem; font-weight: 600;
  margin: 0 0 6px 0; letter-spacing: -0.01em;
}
.step-card p {
  color: var(--muted); font-size: 0.88rem; line-height: 1.5; margin: 0;
}
.step-card code {
  background: var(--soft); padding: 1px 5px;
  border-radius: 4px; font-size: 0.82rem; color: var(--ink);
}
@media (max-width: 720px) { .howto { grid-template-columns: 1fr; } }

.duo {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 16px; margin-bottom: 48px;
}
.panel {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 18px 22px;
}
.panel h4 {
  margin: 0 0 6px 0; font-size: 0.95rem; font-weight: 600;
  letter-spacing: -0.01em;
}
.panel p {
  margin: 0 0 6px 0; color: var(--muted);
  font-size: 0.88rem; line-height: 1.5;
}
.panel p:last-child { margin-bottom: 0; }
.panel code {
  background: var(--soft); padding: 1px 5px;
  border-radius: 4px; font-size: 0.82rem; color: var(--ink);
}
.panel .instructions {
  margin: 8px 0 10px 0;
  padding: 0;
  list-style: none;
  counter-reset: human-step;
}
.panel .instructions li {
  position: relative;
  padding: 0 0 10px 28px;
  color: var(--muted);
  font-size: 0.88rem;
  line-height: 1.5;
  counter-increment: human-step;
}
.panel .instructions li:last-child { padding-bottom: 0; }
.panel .instructions li::before {
  content: counter(human-step);
  position: absolute;
  left: 0; top: 0;
  width: 18px; height: 18px;
  border-radius: 5px;
  background: var(--soft);
  color: var(--ink);
  font-size: 0.72rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  display: inline-flex;
  align-items: center; justify-content: center;
  line-height: 1;
}
.panel .instructions em {
  font-style: normal;
  color: var(--ink);
  font-weight: 500;
}
.panel .fineprint {
  font-size: 0.78rem;
  color: var(--muted);
  border-top: 1px dashed var(--border);
  padding-top: 8px;
  margin-top: 10px;
  line-height: 1.5;
}
.panel.muted {
  opacity: 0.6;
  transition: opacity .25s ease;
}
.panel.muted:hover { opacity: 1; }
@media (max-width: 640px) { .duo { grid-template-columns: 1fr; } }

.foot {
  text-align: center; color: var(--muted);
  font-size: 0.78rem; padding-top: 28px;
  border-top: 1px solid var(--border);
}
.foot a { color: var(--muted); margin: 0 10px; }
.foot a:hover { color: var(--accent); text-decoration: none; }
"""


# ============================== card render ==============================


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

    meta = []
    if p.get("scope"):
        meta.append(f'<span class="scope-tag">{_esc(p["scope"])}</span>')
    if p.get("created_at"):
        meta.append(_humanize(p["created_at"]))
    started = f'<p class="started">{" · ".join(meta)}</p>' if meta else ""

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
    projects: list[dict],
    show_completed: bool,
    expand_done: bool,
    scope: Optional[str],
) -> str:
    if not projects:
        return (
            '<div class="empty">'
            "No projects yet.<br/>"
            "Try <code>POST /api/dashboards/&lt;id&gt;/projects</code> with "
            '<code>{"title": "..."}</code>.'
            "</div>"
        )
    if scope:
        projects = [p for p in projects if p.get("scope") == scope]
        if not projects:
            return (
                '<div class="empty">No projects in scope '
                f'&ldquo;{_esc(scope)}&rdquo;.</div>'
            )
    active = [p for p in projects if not _is_complete(p)]
    completed = [p for p in projects if _is_complete(p)]
    ordered = active + (completed if show_completed else [])
    if not ordered:
        return (
            '<div class="empty">All projects complete. '
            'Toggle "completed" to see them.</div>'
        )
    return "".join(_card_html(p, expand_done) for p in ordered)


_TZ_JS = """
(function () {
  const pad = n => String(n).padStart(2, '0');
  const fmt = d => pad(d.getDate()) + '.' + pad(d.getMonth() + 1)
                 + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  const apply = root => {
    const els = (root.querySelectorAll
                  ? root.querySelectorAll('time[data-tz]:not([data-tz-done])')
                  : []);
    els.forEach(el => {
      const d = new Date(el.getAttribute('datetime'));
      if (!isNaN(d)) el.textContent = fmt(d);
      el.setAttribute('data-tz-done', '1');
    });
  };
  const start = () => {
    apply(document);
    new MutationObserver(muts => {
      for (const m of muts) for (const n of m.addedNodes)
        if (n.nodeType === 1) apply(n);
    }).observe(document.body, {childList: true, subtree: true});
  };
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', start);
  else start();
})();
"""


def _head_html() -> str:
    return (
        '<meta name="alien-agent-id" content="v1">'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
        '<link href="https://fonts.googleapis.com/icon?family=Material+Icons"'
        ' rel="stylesheet">'
        '<link href="https://fonts.googleapis.com/icon?family=Material+Icons+Outlined"'
        ' rel="stylesheet">'
        f"<style>{CSS}</style>"
        f"<script>{_TZ_JS}</script>"
    )


# ============================== landing page ==============================


def _sample_card_html() -> str:
    """Render the landing-page preview card via the real renderer so it
    stays pixel-identical to a live project card."""
    now = datetime.now()
    ago_2d = (now - timedelta(days=2)).isoformat(timespec="seconds")
    ago_1d = (now - timedelta(days=1)).isoformat(timespec="seconds")
    proj = {
        "id": "demo",
        "title": "Rebuild personal site",
        "description": "Move off WordPress. Astro + Cloudflare Pages.",
        "scope": "frontend",
        "created_at": ago_2d,
        "steps": [
            {"id": "s1", "text": "Audit current pages",
             "done": True, "completed_at": ago_2d, "created_at": ago_2d,
             "type": None, "details": None},
            {"id": "s2", "text": "Pick CMS — chose Astro",
             "done": True, "completed_at": ago_1d, "created_at": ago_2d,
             "type": None, "details": None},
            {"id": "s3", "text": "Confirm content workflow",
             "done": False, "completed_at": None, "created_at": ago_1d,
             "type": "awaiting_human",
             "details": "Astro vs hosted CMS — should editors use the file "
                        "system or a UI?"},
            {"id": "s4", "text": "Migrate first 10 posts",
             "done": False, "completed_at": None, "created_at": ago_1d,
             "type": None, "details": None},
        ],
    }
    return _card_html(proj, expand_done=True)


def _stats_html(s: dict) -> str:
    last = s.get("last_updated")
    last_label = _ago(last) if last else "no activity yet"

    def cell(n: int, label: str) -> str:
        return f'<div class="stat"><span class="n">{n}</span>' \
               f'<span class="l">{label}</span></div>'

    return (
        '<div class="stats"><div class="stats-inner">'
        f'{cell(s["owners"], "humans")}'
        f'{cell(s["agents"], "agents")}'
        f'{cell(s["dashboards"], "boards")}'
        f'{cell(s["projects"], "projects")}'
        f'{cell(s["steps"], "steps")}'
        '</div></div>'
        '<p class="stats-foot">'
        '<span class="live-dot"></span>'
        f'live · last update {_esc(last_label)}'
        '</p>'
    )


@ui.page("/")
def landing() -> None:
    ui.add_head_html(_head_html())
    ui.html('<div class="bg-grid"></div>')

    with ui.element("div").classes("lp"):
        ui.html(
            '<section class="hero">'
            '<h1>Watch your AI agent <span class="hi">work, live.</span></h1>'
            '<p class="lede">'
            'mCon is the live status board your AI agent builds for you. '
            'Every project, every step, every question it needs answered — '
            'on cards, in real time.'
            '</p>'
            '<p class="cta">'
            '<strong>Show this site to your AI agent to start.</strong> '
            'It will create a private dashboard for you and give you back '
            'the secret URL.'
            '</p>'
            '<div class="cta-row">'
            '<a class="btn primary" href="/skill">See what the agent will read →</a>'
            '<a class="btn ghost" href="https://alien.org/agent-id" '
            'target="_blank" rel="noopener">Get an Alien Agent ID ↗</a>'
            '</div>'
            '</section>'
        )

        stats_el = ui.html("")

        ui.html(
            '<section class="preview">'
            '<div>'
            '<h3>One card per project. One line per step.</h3>'
            '<p>Your agent writes the cards. You read them. Done items strike '
            'through and slip to the bottom. Open work stays at the top, '
            'sorted by what is blocking what.</p>'
            '<p>When the agent needs a decision from you, the bullet turns '
            'into a speech bubble — hover to see the question.</p>'
            '<div class="legend">'
            '<span><span class="icon">○</span> open</span>'
            '<span><span class="icon">✓</span> done</span>'
            f'<span>{_ICON_SPEECH} awaiting you</span>'
            f'<span>{_ICON_CLOCK} awaiting external</span>'
            '</div>'
            '</div>'
            f'<div class="preview-card-wrap">{_sample_card_html()}</div>'
            '</section>'

            '<section class="howto">'
            '<div class="step-card"><div class="num">1</div>'
            '<h4>Agent gets identity</h4>'
            '<p>One QR scan in the Alien app binds your AI agent to you. '
            'Every API call from the agent is Ed25519-signed and traceable.</p>'
            '</div>'
            '<div class="step-card"><div class="num">2</div>'
            '<h4>Agent creates a dashboard</h4>'
            '<p>The agent calls <code>POST /api/dashboards</code>, then '
            'populates projects and steps as it works. Up to '
            f'{MAX_DASHBOARDS_PER_AGENT} dashboards per agent.</p>'
            '</div>'
            '<div class="step-card"><div class="num">3</div>'
            '<h4>You read it via the URL</h4>'
            '<p>The agent gives you a private URL like '
            '<code>/d/abc12345</code>. Open it any time — the page refreshes '
            'as the agent works.</p>'
            '</div>'
            '</section>'

            '<section class="duo">'
            '<div class="panel muted">'
            '<h4>For AI agents</h4>'
            '<p>mCon is the live status page you keep for your human owner. '
            'One dashboard per area of work, one project per task, one step '
            'per checklist item.</p>'
            '<p>You drive everything via the REST API; humans only read. '
            'Mark a step <code>awaiting_human</code> (with the question in '
            '<code>details</code>) when blocked on the human, or '
            '<code>awaiting_external</code> when waiting on email or an '
            'SLA — both render as distinct bullet icons.</p>'
            '<p>The skill at <a href="/skill">/skill</a> is the canonical '
            'reference. Read it before your first call.</p>'
            f'<p class="fineprint">Quotas: {MAX_DASHBOARDS_PER_AGENT} '
            f'dashboards per agent · {MAX_AGENTS_PER_OWNER} agents per '
            'human owner. Unbound agents are rejected with '
            '<code>403</code> — get an Alien Agent ID first.</p>'
            '</div>'
            '<div class="panel">'
            '<h4>For humans</h4>'
            '<p>You don\'t sign in here. To get a dashboard:</p>'
            '<ol class="instructions">'
            '<li>Make sure your AI agent has an '
            '<a href="https://alien.org/agent-id" target="_blank" '
            'rel="noopener">Alien Agent ID</a> bound to you. mCon will not '
            'accept calls from an unbound agent.</li>'
            '<li>Show your agent the URL of <em>this</em> mCon site and ask '
            'it to <em>create a dashboard for the tasks you want it to '
            'manage</em>. It will read <a href="/skill">/skill</a> and call '
            'the API on its own.</li>'
            '<li>The agent gives you back a secret URL like '
            '<code>/d/abc12345</code>. Open it any time to see what your '
            'agent is doing — the page refreshes as it works.</li>'
            '</ol>'
            f'<p class="fineprint">Quotas: {MAX_DASHBOARDS_PER_AGENT} '
            f'dashboards per agent · {MAX_AGENTS_PER_OWNER} agents per human '
            'owner. The dashboard URL is the only key — anyone you share it '
            'with can read the dashboard, so keep it private.</p>'
            '</div>'
            '</section>'

            '<div class="foot">'
            'mCon · v1.0.0'
            ' · <a href="/skill">agent skill</a>'
            ' · <a href="https://alien.org/agent-id" target="_blank" '
            'rel="noopener">Alien Agent ID</a>'
            '</div>'
        )

    def render_stats() -> None:
        stats_el.set_content(_stats_html(S.aggregate_stats()))

    render_stats()
    ui.timer(5.0, render_stats)


# ============================== skill page ==============================


@ui.page("/skill")
def skill_page() -> None:
    ui.add_head_html(_head_html())
    if not SKILL_PATH.exists():
        ui.html(
            '<div class="landing">'
            "<h1>Skill not found</h1>"
            f"<p>Expected at <code>{_esc(str(SKILL_PATH))}</code>.</p>"
            "</div>"
        )
        return
    text = SKILL_PATH.read_text()
    with ui.element("div").classes("landing"):
        ui.html(
            '<p class="tag" style="margin-bottom:18px">'
            'Agent skill — human-readable view. '
            '<a href="/">← back</a></p>'
        )
        ui.markdown(text).classes("w-full")


# ============================== dashboard page ==============================


@ui.page("/d/{did}")
def dashboard_view(did: str) -> None:
    ui.add_head_html(_head_html())

    try:
        dboard = S.get_dashboard(did)
    except NotFound:
        ui.html(
            '<div class="landing">'
            "<h1>Dashboard not found</h1>"
            '<p><a href="/">← back to all dashboards</a></p>'
            "</div>"
        )
        return

    ui_state = {"show_completed": False, "expand_done": False, "scope": None}
    cache = {
        "date": "",
        "clock": "",
        "updated": "",
        "info": "",
        "grid": "",
        "footer": "",
        "tabs_sig": None,
        "title": "",
    }

    with ui.element("div").classes("header-bar"):
        with ui.element("div").classes("header-left"):
            title_html = ui.html("")
            date_html = ui.html("").classes("date")
            clock_html = ui.html("").classes("clock")
            updated_html = ui.html("").classes("updated")
        with ui.element("div").classes("header-meta"):
            info_html = ui.html("")

            def on_done_toggle(e):
                ui_state["expand_done"] = bool(e.value)
                cache["grid"] = ""
                render_grid()

            def on_completed_toggle(e):
                ui_state["show_completed"] = bool(e.value)
                cache["grid"] = ""
                render_grid()

            ui.switch("done", value=False, on_change=on_done_toggle).props(
                "dense color=teal"
            )
            ui.switch(
                "completed", value=False, on_change=on_completed_toggle
            ).props("dense color=teal")

    tabs_row = ui.element("div").classes("tabs-row")
    grid_html = ui.html("").classes("w-full")
    footer_html = ui.html("").classes("footer")

    def fetch() -> tuple[dict, list[dict]]:
        try:
            d = S.get_dashboard(did)
            ps = S.list_projects(did)
        except NotFound:
            return dboard, []
        return d, ps

    def render_header() -> None:
        d, projects = fetch()
        now = datetime.now()
        clock = (
            now.strftime("%H")
            + '<span class="clock-colon">:</span>'
            + now.strftime("%M")
        )
        date = now.strftime("%a, %d.%m.%Y")
        title = (
            '<h1><a href="/">mCon</a>'
            '<span class="sep">/</span>'
            f'<span class="dash-name">{_esc(d["title"])}</span></h1>'
        )
        active = [p for p in projects if not _is_complete(p)]
        completed_n = len(projects) - len(active)
        open_n = sum(1 for p in active for s in p["steps"] if not s["done"])
        parts = [f"{len(active)} active", f"{open_n} open"]
        if completed_n:
            parts.append(f"{completed_n} done")
        info = '<div class="info">' + " · ".join(parts) + "</div>"
        last_iso = d.get("last_updated") or d.get("created_at")
        updated = f"updated {_ago(last_iso)}" if last_iso else ""
        if title != cache["title"]:
            cache["title"] = title
            title_html.set_content(title)
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
        _, projects = fetch()
        html_str = (
            f'<div class="grid-wrap">'
            f'{_grid_html(projects, ui_state["show_completed"], ui_state["expand_done"], ui_state["scope"])}'
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
        _, projects = fetch()
        scopes = sorted({
            p.get("scope")
            for p in projects
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
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        today_n, week_n = S.step_stats_for_dashboard(
            did,
            today_iso=today_start.isoformat(timespec="seconds"),
            week_iso=week_start.isoformat(timespec="seconds"),
        )
        text = f"{today_n} done today · {week_n} this week"
        if text != cache["footer"]:
            cache["footer"] = text
            footer_html.set_content(text)

    render_header()
    render_tabs()
    render_grid()
    render_footer()
    ui.timer(1.0, render_header)

    def refresh_state() -> None:
        render_tabs()
        render_grid()
        render_footer()

    ui.timer(2.0, refresh_state)


# ============================== entrypoint ==============================


ui.run(
    title="mCon",
    favicon="📋",
    dark=False,
    port=8765,
    reload=True,
    show=False,
)
