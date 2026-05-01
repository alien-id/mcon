"""SQLite storage for mCon — multi-tenant data layer.

Schema:
- agents:     one per Alien Agent ID (fingerprint PK, owner, pubkey)
- dashboards: belong to an agent (id PK, owner via agent)
- projects:   belong to a dashboard
- steps:      belong to a project

Quotas (enforced here):
- 2 agents per human owner (`MAX_AGENTS_PER_OWNER`)
- 5 dashboards per agent   (`MAX_DASHBOARDS_PER_AGENT`)
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_AGENTS_PER_OWNER = 2
MAX_DASHBOARDS_PER_AGENT = 5

_STEP_TYPES = ("awaiting_human", "awaiting_external")


class QuotaExceeded(Exception):
    pass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Forbidden(Exception):
    pass


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return secrets.token_urlsafe(12)


def _validate_iso(v: Optional[str], field: str) -> Optional[str]:
    if v is None or v == "":
        return None
    try:
        datetime.fromisoformat(v)
        return v
    except ValueError as e:
        raise ValueError(f"{field} must be ISO 8601 (got {v!r})") from e


def _norm_scope(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = s.strip().lower()
    return s or None


def _norm_text(d: Optional[str]) -> Optional[str]:
    if d is None:
        return None
    d = d.strip()
    return d or None


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    fingerprint     TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    public_key_pem  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_owner ON agents(owner);

CREATE TABLE IF NOT EXISTS dashboards (
    id                TEXT PRIMARY KEY,
    agent_fingerprint TEXT NOT NULL REFERENCES agents(fingerprint) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    last_updated      TEXT
);
CREATE INDEX IF NOT EXISTS idx_dashboards_agent ON dashboards(agent_fingerprint);

CREATE TABLE IF NOT EXISTS projects (
    dashboard_id  TEXT NOT NULL REFERENCES dashboards(id) ON DELETE CASCADE,
    id            TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    scope         TEXT,
    created_at    TEXT NOT NULL,
    position      INTEGER NOT NULL,
    PRIMARY KEY (dashboard_id, id)
);

CREATE TABLE IF NOT EXISTS steps (
    dashboard_id  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    id            TEXT NOT NULL,
    text          TEXT NOT NULL,
    done          INTEGER NOT NULL DEFAULT 0,
    completed_at  TEXT,
    created_at    TEXT NOT NULL,
    type          TEXT,
    details       TEXT,
    position      INTEGER NOT NULL,
    PRIMARY KEY (dashboard_id, project_id, id),
    FOREIGN KEY (dashboard_id, project_id)
        REFERENCES projects(dashboard_id, id) ON DELETE CASCADE
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
        self.version = 0

    # --------------------------- agents ---------------------------

    def upsert_agent(
        self,
        fingerprint: str,
        owner: str,
        public_key_pem: str,
    ) -> dict:
        """Register the agent on first sight; enforce per-owner agent quota."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row:
                if row["owner"] != owner:
                    raise Forbidden(
                        "agent fingerprint is bound to a different owner; "
                        "the binding is immutable"
                    )
                return _agent_row_to_dict(row)
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE owner = ?", (owner,)
            ).fetchone()["n"]
            if count >= MAX_AGENTS_PER_OWNER:
                raise QuotaExceeded(
                    f"owner already has {count} agents "
                    f"(max {MAX_AGENTS_PER_OWNER})"
                )
            self._conn.execute(
                "INSERT INTO agents(fingerprint, owner, public_key_pem, created_at) "
                "VALUES (?, ?, ?, ?)",
                (fingerprint, owner, public_key_pem, _now_iso()),
            )
            self.version += 1
            return self.get_agent(fingerprint)

    def get_agent(self, fingerprint: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        if not row:
            raise NotFound("agent not registered")
        return _agent_row_to_dict(row)

    def delete_agent(self, fingerprint: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM agents WHERE fingerprint = ?", (fingerprint,)
            )
            if cur.rowcount == 0:
                raise NotFound("agent not registered")
            self.version += 1

    def list_agents_for_owner(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE owner = ? ORDER BY created_at",
                (owner,),
            ).fetchall()
        return [_agent_row_to_dict(r) for r in rows]

    # --------------------------- dashboards ---------------------------

    def list_dashboards(self, fingerprint: Optional[str] = None) -> list[dict]:
        with self._lock:
            if fingerprint is None:
                rows = self._conn.execute(
                    "SELECT d.*, a.owner AS owner "
                    "FROM dashboards d JOIN agents a "
                    "ON a.fingerprint = d.agent_fingerprint "
                    "ORDER BY d.created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT d.*, a.owner AS owner "
                    "FROM dashboards d JOIN agents a "
                    "ON a.fingerprint = d.agent_fingerprint "
                    "WHERE d.agent_fingerprint = ? ORDER BY d.created_at",
                    (fingerprint,),
                ).fetchall()
        return [_dashboard_row_to_dict(r) for r in rows]

    def create_dashboard(
        self,
        agent_fingerprint: str,
        *,
        title: str,
        description: Optional[str],
    ) -> dict:
        if not title.strip():
            raise ValueError("title is required")
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dashboards WHERE agent_fingerprint = ?",
                (agent_fingerprint,),
            ).fetchone()["n"]
            if n >= MAX_DASHBOARDS_PER_AGENT:
                raise QuotaExceeded(
                    f"agent already has {n} dashboards "
                    f"(max {MAX_DASHBOARDS_PER_AGENT})"
                )
            did = _new_id()
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO dashboards"
                "(id, agent_fingerprint, title, description, created_at, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (did, agent_fingerprint, title, description or "", now, now),
            )
            self.version += 1
            return self.get_dashboard(did)

    def get_dashboard(self, did: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT d.*, a.owner AS owner "
                "FROM dashboards d JOIN agents a "
                "ON a.fingerprint = d.agent_fingerprint "
                "WHERE d.id = ?",
                (did,),
            ).fetchone()
        if not row:
            raise NotFound(f"dashboard {did!r} not found")
        return _dashboard_row_to_dict(row)

    def patch_dashboard(
        self,
        did: str,
        agent_fingerprint: str,
        fields: dict,
    ) -> dict:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            sets, vals = [], []
            if "title" in fields:
                if not (fields["title"] or "").strip():
                    raise ValueError("title cannot be empty")
                sets.append("title = ?")
                vals.append(fields["title"])
            if "description" in fields:
                sets.append("description = ?")
                vals.append(fields["description"] or "")
            if sets:
                vals.append(did)
                self._conn.execute(
                    f"UPDATE dashboards SET {', '.join(sets)} WHERE id = ?", vals
                )
            self._touch_dashboard(did)
            return self.get_dashboard(did)

    def delete_dashboard(self, did: str, agent_fingerprint: str) -> None:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            self._conn.execute("DELETE FROM dashboards WHERE id = ?", (did,))
            self.version += 1

    def _assert_dashboard_owner(self, did: str, agent_fingerprint: str) -> None:
        row = self._conn.execute(
            "SELECT agent_fingerprint FROM dashboards WHERE id = ?", (did,)
        ).fetchone()
        if not row:
            raise NotFound(f"dashboard {did!r} not found")
        if row["agent_fingerprint"] != agent_fingerprint:
            raise Forbidden("dashboard belongs to another agent")

    def _touch_dashboard(self, did: str) -> None:
        self._conn.execute(
            "UPDATE dashboards SET last_updated = ? WHERE id = ?",
            (_now_iso(), did),
        )
        self.version += 1

    # --------------------------- projects ---------------------------

    def list_projects(self, did: str) -> list[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM dashboards WHERE id = ?", (did,)
            ).fetchone()
            if not row:
                raise NotFound(f"dashboard {did!r} not found")
            prows = self._conn.execute(
                "SELECT * FROM projects WHERE dashboard_id = ? "
                "ORDER BY position, created_at",
                (did,),
            ).fetchall()
            srows = self._conn.execute(
                "SELECT * FROM steps WHERE dashboard_id = ? "
                "ORDER BY position, created_at",
                (did,),
            ).fetchall()
        steps_by_project: dict[str, list[dict]] = {}
        for sr in srows:
            steps_by_project.setdefault(sr["project_id"], []).append(
                _step_row_to_dict(sr)
            )
        return [_project_row_to_dict(p, steps_by_project.get(p["id"], [])) for p in prows]

    def get_project(self, did: str, pid: str) -> dict:
        with self._lock:
            prow = self._conn.execute(
                "SELECT * FROM projects WHERE dashboard_id = ? AND id = ?",
                (did, pid),
            ).fetchone()
            if not prow:
                raise NotFound(f"project {pid!r} not found")
            srows = self._conn.execute(
                "SELECT * FROM steps WHERE dashboard_id = ? AND project_id = ? "
                "ORDER BY position, created_at",
                (did, pid),
            ).fetchall()
        return _project_row_to_dict(prow, [_step_row_to_dict(s) for s in srows])

    def create_project(
        self,
        did: str,
        agent_fingerprint: str,
        *,
        pid: Optional[str],
        title: str,
        description: Optional[str],
        scope: Optional[str],
        created_at: Optional[str],
        steps: list[dict],
    ) -> dict:
        if not title.strip():
            raise ValueError("title is required")
        created_at = _validate_iso(created_at, "created_at")
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            pid = pid or _new_id()
            existing = self._conn.execute(
                "SELECT 1 FROM projects WHERE dashboard_id = ? AND id = ?",
                (did, pid),
            ).fetchone()
            if existing:
                raise Conflict(f"project id {pid!r} already exists in this dashboard")
            position = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p "
                "FROM projects WHERE dashboard_id = ?",
                (did,),
            ).fetchone()["p"]
            self._conn.execute(
                "INSERT INTO projects"
                "(dashboard_id, id, title, description, scope, created_at, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    did,
                    pid,
                    title,
                    description or "",
                    _norm_scope(scope),
                    created_at or _now_iso(),
                    position,
                ),
            )
            for i, s in enumerate(steps):
                self._insert_step(did, pid, s, position=i)
            self._touch_dashboard(did)
            return self.get_project(did, pid)

    def upsert_project(
        self,
        did: str,
        agent_fingerprint: str,
        pid: str,
        *,
        title: str,
        description: Optional[str],
        scope: Optional[str],
        created_at: Optional[str],
        steps: list[dict],
    ) -> dict:
        if not title.strip():
            raise ValueError("title is required")
        created_at = _validate_iso(created_at, "created_at")
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            existing = self._conn.execute(
                "SELECT created_at, position FROM projects "
                "WHERE dashboard_id = ? AND id = ?",
                (did, pid),
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE projects SET title = ?, description = ?, scope = ?, "
                    "created_at = ? WHERE dashboard_id = ? AND id = ?",
                    (
                        title,
                        description or "",
                        _norm_scope(scope),
                        created_at or existing["created_at"],
                        did,
                        pid,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM steps WHERE dashboard_id = ? AND project_id = ?",
                    (did, pid),
                )
            else:
                position = self._conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS p "
                    "FROM projects WHERE dashboard_id = ?",
                    (did,),
                ).fetchone()["p"]
                self._conn.execute(
                    "INSERT INTO projects"
                    "(dashboard_id, id, title, description, scope, created_at, "
                    " position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        did,
                        pid,
                        title,
                        description or "",
                        _norm_scope(scope),
                        created_at or _now_iso(),
                        position,
                    ),
                )
            for i, s in enumerate(steps):
                self._insert_step(did, pid, s, position=i)
            self._touch_dashboard(did)
            return self.get_project(did, pid)

    def patch_project(
        self,
        did: str,
        agent_fingerprint: str,
        pid: str,
        fields: dict,
    ) -> dict:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            existing = self._conn.execute(
                "SELECT 1 FROM projects WHERE dashboard_id = ? AND id = ?",
                (did, pid),
            ).fetchone()
            if not existing:
                raise NotFound(f"project {pid!r} not found")
            sets, vals = [], []
            if "title" in fields:
                if not (fields["title"] or "").strip():
                    raise ValueError("title cannot be empty")
                sets.append("title = ?")
                vals.append(fields["title"])
            if "description" in fields:
                sets.append("description = ?")
                vals.append(fields["description"] or "")
            if "scope" in fields:
                sets.append("scope = ?")
                vals.append(_norm_scope(fields["scope"]))
            if "created_at" in fields:
                sets.append("created_at = ?")
                vals.append(_validate_iso(fields["created_at"], "created_at") or _now_iso())
            if sets:
                vals.extend([did, pid])
                self._conn.execute(
                    f"UPDATE projects SET {', '.join(sets)} "
                    "WHERE dashboard_id = ? AND id = ?",
                    vals,
                )
            self._touch_dashboard(did)
            return self.get_project(did, pid)

    def delete_project(self, did: str, agent_fingerprint: str, pid: str) -> None:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            cur = self._conn.execute(
                "DELETE FROM projects WHERE dashboard_id = ? AND id = ?",
                (did, pid),
            )
            if cur.rowcount == 0:
                raise NotFound(f"project {pid!r} not found")
            self._touch_dashboard(did)

    # --------------------------- steps ---------------------------

    def add_step(
        self,
        did: str,
        agent_fingerprint: str,
        pid: str,
        s: dict,
    ) -> dict:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            existing = self._conn.execute(
                "SELECT 1 FROM projects WHERE dashboard_id = ? AND id = ?",
                (did, pid),
            ).fetchone()
            if not existing:
                raise NotFound(f"project {pid!r} not found")
            position = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p "
                "FROM steps WHERE dashboard_id = ? AND project_id = ?",
                (did, pid),
            ).fetchone()["p"]
            sid = self._insert_step(did, pid, s, position=position)
            self._touch_dashboard(did)
            row = self._conn.execute(
                "SELECT * FROM steps WHERE dashboard_id = ? AND project_id = ? "
                "AND id = ?",
                (did, pid, sid),
            ).fetchone()
        return _step_row_to_dict(row)

    def patch_step(
        self,
        did: str,
        agent_fingerprint: str,
        pid: str,
        sid: str,
        fields: dict,
    ) -> dict:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            row = self._conn.execute(
                "SELECT * FROM steps WHERE dashboard_id = ? AND project_id = ? "
                "AND id = ?",
                (did, pid, sid),
            ).fetchone()
            if not row:
                raise NotFound(f"step {sid!r} not found")

            sets: list[str] = []
            vals: list = []
            if "text" in fields:
                if not (fields["text"] or "").strip():
                    raise ValueError("text cannot be empty")
                sets.append("text = ?")
                vals.append(fields["text"])
            new_done = bool(fields["done"]) if "done" in fields else bool(row["done"])
            if "done" in fields:
                sets.append("done = ?")
                vals.append(1 if new_done else 0)
            if "completed_at" in fields:
                sets.append("completed_at = ?")
                vals.append(_validate_iso(fields["completed_at"], "completed_at"))
            elif "done" in fields:
                if new_done and not row["completed_at"]:
                    sets.append("completed_at = ?")
                    vals.append(_now_iso())
                elif not new_done:
                    sets.append("completed_at = NULL")
            if "type" in fields:
                t = fields["type"]
                if t is not None and t not in _STEP_TYPES:
                    raise ValueError(
                        f"type must be one of: {', '.join(_STEP_TYPES)}, or null"
                    )
                sets.append("type = ?")
                vals.append(t or None)
            if "details" in fields:
                sets.append("details = ?")
                vals.append(_norm_text(fields["details"]))
            if "created_at" in fields:
                sets.append("created_at = ?")
                vals.append(_validate_iso(fields["created_at"], "created_at") or _now_iso())

            if sets:
                vals.extend([did, pid, sid])
                self._conn.execute(
                    f"UPDATE steps SET {', '.join(sets)} "
                    "WHERE dashboard_id = ? AND project_id = ? AND id = ?",
                    vals,
                )
            self._touch_dashboard(did)
            row = self._conn.execute(
                "SELECT * FROM steps WHERE dashboard_id = ? AND project_id = ? "
                "AND id = ?",
                (did, pid, sid),
            ).fetchone()
        return _step_row_to_dict(row)

    def delete_step(
        self,
        did: str,
        agent_fingerprint: str,
        pid: str,
        sid: str,
    ) -> None:
        with self._lock:
            self._assert_dashboard_owner(did, agent_fingerprint)
            cur = self._conn.execute(
                "DELETE FROM steps WHERE dashboard_id = ? AND project_id = ? "
                "AND id = ?",
                (did, pid, sid),
            )
            if cur.rowcount == 0:
                raise NotFound(f"step {sid!r} not found")
            self._touch_dashboard(did)

    def _insert_step(
        self,
        did: str,
        pid: str,
        s: dict,
        *,
        position: int,
    ) -> str:
        text = (s.get("text") or "").strip()
        if not text:
            raise ValueError("step text is required")
        t = s.get("type")
        if t is not None and t != "" and t not in _STEP_TYPES:
            raise ValueError(
                f"step type must be one of: {', '.join(_STEP_TYPES)}, or null"
            )
        done = bool(s.get("done"))
        completed_at = _validate_iso(s.get("completed_at"), "completed_at")
        if done and not completed_at:
            completed_at = _now_iso()
        if not done:
            completed_at = None
        sid = _new_id()
        self._conn.execute(
            "INSERT INTO steps"
            "(dashboard_id, project_id, id, text, done, completed_at, "
            " created_at, type, details, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                did,
                pid,
                sid,
                text,
                1 if done else 0,
                completed_at,
                _validate_iso(s.get("created_at"), "created_at") or _now_iso(),
                t or None,
                _norm_text(s.get("details")),
                position,
            ),
        )
        return sid

    # --------------------------- aggregations ---------------------------

    def aggregate_stats(self) -> dict:
        """Privacy-safe global counters for the landing page."""
        with self._lock:
            owners = self._conn.execute(
                "SELECT COUNT(DISTINCT owner) AS n FROM agents"
            ).fetchone()["n"]
            agents = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agents"
            ).fetchone()["n"]
            dashboards = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dashboards"
            ).fetchone()["n"]
            projects = self._conn.execute(
                "SELECT COUNT(*) AS n FROM projects"
            ).fetchone()["n"]
            steps = self._conn.execute(
                "SELECT COUNT(*) AS n FROM steps"
            ).fetchone()["n"]
            steps_done = self._conn.execute(
                "SELECT COUNT(*) AS n FROM steps WHERE done = 1"
            ).fetchone()["n"]
            last = self._conn.execute(
                "SELECT MAX(last_updated) AS t FROM dashboards"
            ).fetchone()["t"]
        return {
            "owners": owners,
            "agents": agents,
            "dashboards": dashboards,
            "projects": projects,
            "steps": steps,
            "steps_done": steps_done,
            "last_updated": last,
        }

    def step_stats_for_dashboard(
        self,
        did: str,
        today_iso: str,
        week_iso: str,
    ) -> tuple[int, int]:
        """Return (today_done, week_done) for a dashboard."""
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "  SUM(CASE WHEN completed_at >= ? THEN 1 ELSE 0 END) AS week_n, "
                "  SUM(CASE WHEN completed_at >= ? THEN 1 ELSE 0 END) AS today_n "
                "FROM steps WHERE dashboard_id = ? AND done = 1 "
                "AND completed_at IS NOT NULL",
                (week_iso, today_iso, did),
            ).fetchone()
        return int(row["today_n"] or 0), int(row["week_n"] or 0)


# --------------------------- row → dict helpers ---------------------------


def _agent_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "fingerprint": r["fingerprint"],
        "owner": r["owner"],
        "public_key_pem": r["public_key_pem"],
        "created_at": r["created_at"],
    }


def _dashboard_row_to_dict(r: sqlite3.Row) -> dict:
    keys = r.keys()
    return {
        "id": r["id"],
        "agent_fingerprint": r["agent_fingerprint"],
        "owner": r["owner"] if "owner" in keys else None,
        "title": r["title"],
        "description": r["description"] or "",
        "created_at": r["created_at"],
        "last_updated": r["last_updated"],
    }


def _project_row_to_dict(r: sqlite3.Row, steps: list[dict]) -> dict:
    return {
        "id": r["id"],
        "dashboard_id": r["dashboard_id"],
        "title": r["title"],
        "description": r["description"] or "",
        "scope": r["scope"],
        "created_at": r["created_at"],
        "steps": steps,
    }


def _step_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "text": r["text"],
        "done": bool(r["done"]),
        "completed_at": r["completed_at"],
        "created_at": r["created_at"],
        "type": r["type"],
        "details": r["details"],
    }
