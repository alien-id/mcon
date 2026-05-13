"""Unit tests for mcon/auth.py — the DPoP-verifying FastAPI dependency.

The SDK's `verify_dpop_request` is itself exhaustively tested in
sso-sdk-py/packages/agent-id. What this file covers is the *adapter
glue* in mcon: the Starlette-to-dict header normalization, the
AgentIdentity construction, the WWW-Authenticate challenge shape on
failure, the failure-code log line, and the JWKS TTL cache. Network calls
are mocked end-to-end — these tests are offline and fast.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth as auth_module
from auth import (
    AgentIdentity,
    _build_req,
    _TTLJwks,
    require_agent,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _fake_request(
    method: str = "POST",
    path: str = "/api/dashboards",
    headers: dict[str, str] | None = None,
    scheme: str = "https",
    host: str = "mcon.alien.org",
) -> Request:
    """Build a Starlette Request from a synthetic ASGI scope. Cheap — no
    transport, no event loop, no I/O. Real Request semantics for headers
    and URL reconstruction."""
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": raw_headers,
        "scheme": scheme,
        "server": (host, 443 if scheme == "https" else 80),
        "http_version": "1.1",
    }
    return Request(scope)


def _fake_jwks() -> dict:
    return {"keys": []}


# ── _build_req ───────────────────────────────────────────────────────────


def test_build_req_emits_scalar_value_for_single_header() -> None:
    req = _fake_request(headers={"Authorization": "DPoP token", "DPoP": "proof"})
    out = _build_req(req)
    assert out["method"] == "POST"
    assert out["url"].startswith("https://mcon.alien.org/api/dashboards")
    # Single occurrences stay as scalar strings — matches what
    # verify_dpop_request expects in the common case.
    assert out["headers"]["authorization"] == "DPoP token"
    assert out["headers"]["dpop"] == "proof"


def test_build_req_keys_are_lowercased() -> None:
    """SDK contract: header dict is case-folded to lowercase."""
    req = _fake_request(headers={"AUTHORIZATION": "DPoP t", "Content-Type": "x"})
    out = _build_req(req)
    assert "authorization" in out["headers"]
    assert "content-type" in out["headers"]
    # No upper-case duplicates leaking through.
    assert "AUTHORIZATION" not in out["headers"]
    assert "Content-Type" not in out["headers"]


# ── require_agent: success ───────────────────────────────────────────────


def test_require_agent_success_returns_identity_with_jkt_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alien_sso_agent_id import VerifyDPoPSuccess

    monkeypatch.setattr(auth_module, "_jwks_cache", MagicMock(get=lambda: _fake_jwks()))
    success = VerifyDPoPSuccess(
        sub="0000000301abc",
        jkt="THUMBPRINT123",
        access_token_claims={"sub": "0000000301abc", "cnf": {"jkt": "THUMBPRINT123"}},
        proof_claims={},
    )
    monkeypatch.setattr(auth_module, "verify_dpop_request", lambda req, opts: success)

    ident = require_agent(_fake_request(headers={"Authorization": "DPoP t", "DPoP": "p"}))

    assert isinstance(ident, AgentIdentity)
    assert ident.jkt == "THUMBPRINT123"
    assert ident.owner == "0000000301abc"


# ── require_agent: failure ───────────────────────────────────────────────


def test_require_agent_failure_raises_401_with_dpop_www_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alien_sso_agent_id import VerifyDPoPFailure

    monkeypatch.setattr(auth_module, "_jwks_cache", MagicMock(get=lambda: _fake_jwks()))
    monkeypatch.setattr(
        auth_module,
        "verify_dpop_request",
        lambda req, opts: VerifyDPoPFailure(
            code="jkt_mismatch", error="DPoP key thumbprint does not match cnf.jkt"
        ),
    )

    with pytest.raises(HTTPException) as ei:
        require_agent(_fake_request(headers={"Authorization": "DPoP t", "DPoP": "p"}))

    assert ei.value.status_code == 401
    assert ei.value.detail == "DPoP key thumbprint does not match cnf.jkt"
    # RFC 6750 §3.1 + RFC 9449 §7.1: 401 carries a DPoP scheme challenge
    # with error="invalid_token" and the machine-readable code as
    # error_description. This is what agents parse to self-diagnose.
    challenge = ei.value.headers["WWW-Authenticate"]
    assert challenge.startswith("DPoP ")
    assert 'error="invalid_token"' in challenge
    assert 'error_description="jkt_mismatch"' in challenge


def test_require_agent_failure_logs_only_code_not_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Privacy guard: the failure log line must carry the stable code (so
    you can grep it) but not the human-readable error (which could carry
    request specifics) and certainly not any token bytes."""
    from alien_sso_agent_id import VerifyDPoPFailure

    monkeypatch.setattr(auth_module, "_jwks_cache", MagicMock(get=lambda: _fake_jwks()))
    monkeypatch.setattr(
        auth_module,
        "verify_dpop_request",
        lambda req, opts: VerifyDPoPFailure(
            code="proof_stale",
            error="proof iat 2026-05-13T11:00:00 is older than the 30s window",
        ),
    )

    with caplog.at_level(logging.INFO, logger="mcon.auth"):
        with pytest.raises(HTTPException):
            require_agent(_fake_request(headers={"Authorization": "DPoP t", "DPoP": "p"}))

    failure_records = [r for r in caplog.records if r.name == "mcon.auth"]
    assert len(failure_records) == 1
    assert "proof_stale" in failure_records[0].getMessage()
    # The verbose error string and its embedded timestamp must NOT appear.
    assert "2026-05-13T11:00:00" not in failure_records[0].getMessage()


# ── _TTLJwks ─────────────────────────────────────────────────────────────


def test_ttl_jwks_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Within the TTL, fetch_alien_jwks must be called exactly once
    regardless of how many `.get()` calls hit the cache."""
    calls = {"n": 0}

    def fake_fetch() -> dict:
        calls["n"] += 1
        return {"keys": [{"kid": f"k{calls['n']}"}]}

    monkeypatch.setattr(auth_module, "fetch_alien_jwks", fake_fetch)

    cache = _TTLJwks(ttl_sec=3600)
    a = cache.get()
    b = cache.get()
    c = cache.get()

    assert calls["n"] == 1
    assert a is b is c
    assert a["keys"][0]["kid"] == "k1"


def test_ttl_jwks_refreshes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the TTL boundary, the next `.get()` refetches."""
    calls = {"n": 0}

    def fake_fetch() -> dict:
        calls["n"] += 1
        return {"keys": [{"kid": f"k{calls['n']}"}]}

    monkeypatch.setattr(auth_module, "fetch_alien_jwks", fake_fetch)

    # Patch monotonic so we control the timeline deterministically.
    now = [1000.0]
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now[0])

    cache = _TTLJwks(ttl_sec=60)
    first = cache.get()
    now[0] += 30
    same = cache.get()
    now[0] += 31  # cross the 60s boundary
    refreshed = cache.get()

    assert first is same
    assert refreshed is not first
    assert calls["n"] == 2
    assert refreshed["keys"][0]["kid"] == "k2"


def test_ttl_jwks_serves_stale_on_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed refresh keeps the previous value rather than 503-ing
    every request."""
    state = {"first_call_done": False, "raise_now": False}

    def fake_fetch() -> dict:
        if state["raise_now"]:
            raise ConnectionError("DNS resolution failed")
        state["first_call_done"] = True
        return {"keys": [{"kid": "original"}]}

    monkeypatch.setattr(auth_module, "fetch_alien_jwks", fake_fetch)

    now = [1000.0]
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now[0])

    cache = _TTLJwks(ttl_sec=60)
    original = cache.get()
    assert state["first_call_done"]

    # Network breaks; advance past the TTL so the next get() tries to refresh.
    state["raise_now"] = True
    now[0] += 120

    with caplog.at_level(logging.WARNING, logger="mcon.auth"):
        stale_but_serving = cache.get()

    assert stale_but_serving is original
    assert any("jwks refresh failed" in r.getMessage() for r in caplog.records)


def test_ttl_jwks_propagates_initial_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we have NO previous value, a fetch failure has nothing to fall
    back to and must surface as an exception (the dependency will then
    bubble a 5xx — better than silently serving uncached auth)."""

    def fake_fetch() -> dict:
        raise ConnectionError("DNS resolution failed")

    monkeypatch.setattr(auth_module, "fetch_alien_jwks", fake_fetch)

    cache = _TTLJwks(ttl_sec=60)
    with pytest.raises(ConnectionError):
        cache.get()


def test_ttl_jwks_passes_sso_base_url_through_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sso_base_url=None` should let the SDK pick its default (no kwarg
    passed); a string should be forwarded as `sso_base_url=`. This is what
    lets a deployment override target SSO via MCON_SSO_BASE_URL without
    monkey-patching the SDK."""
    seen: list[dict] = []

    def fake_fetch(**kwargs) -> dict:
        seen.append(kwargs)
        return {"keys": []}

    monkeypatch.setattr(auth_module, "fetch_alien_jwks", fake_fetch)

    _TTLJwks(ttl_sec=60).get()
    _TTLJwks(ttl_sec=60, sso_base_url="https://sso.staging.alien-api.com").get()

    # First call: no kwargs (SDK default), second call: explicit base URL.
    assert seen == [
        {},
        {"sso_base_url": "https://sso.staging.alien-api.com"},
    ]
