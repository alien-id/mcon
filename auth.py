"""Alien Agent ID authentication for mCon's FastAPI surface.

Verification is delegated to `alien_sso_agent_id` (RFC 9449 DPoP). This
module just adapts the SDK's verifier to a FastAPI dependency, caches the
SSO JWKS with a TTL so key rotations are picked up without a restart, and
logs failure codes for prod debugging.

There is no bypass switch. Earlier mCon revisions exposed `MCON_SSO_VERIFY`
that, when set to `off`, skipped the SSO owner-chain check while still
verifying the agent's envelope signature. Under DPoP the access-token
signature requires the SSO JWKS and the proof binding requires the access
token, so a partial mode no longer makes sense — and a flag that disables
the remaining check would silently turn a 401 surface into "anyone gets in".
Tests that need to drive the dependency without a live SSO should mock
`verify_dpop_request` (see tests/test_auth.py for the pattern).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from alien_sso_agent_id import (
    JWKS,
    VerifyDPoPFailure,
    VerifyDPoPOptions,
    VerifyDPoPSuccess,
    fetch_alien_jwks,
    verify_dpop_request,
)
from fastapi import HTTPException, Request

log = logging.getLogger("mcon.auth")

# JWKS cache TTL. Alien SSO rotates keys on the order of weeks; the SDK
# docstring suggests 24 h, which is what we use by default. Override with
# `MCON_JWKS_TTL_SECONDS` for tests (set to a few seconds to exercise
# rotation paths) or local dev against an SSO whose keys you're actively
# regenerating.
_DEFAULT_JWKS_TTL_SEC = 24 * 3600

# SSO base URL. Defaults to the SDK's bundled production endpoint
# (`https://sso.alien-api.com`). Override with `MCON_SSO_BASE_URL` to point
# at staging / testnt / develop / a locally-running SSO for non-prod
# deploys. The same URL is used both for fetching the JWKS and as the
# expected access-token `iss` claim — if those diverge the verifier will
# (correctly) reject every token with `iss_mismatch`.
_SSO_BASE_URL: Optional[str] = os.environ.get("MCON_SSO_BASE_URL") or None


@dataclass(frozen=True)
class AgentIdentity:
    """mCon's internal view of an authenticated agent.

    `jkt` is the RFC 7638 JWK thumbprint of the agent's DPoP key — the
    stable identifier for the agent's keypair. Under DPoP the resource
    server never handles the agent's public key directly; the SDK does the
    proof/access-token cryptography against the JWK embedded in each
    request's proof header, and the access token's `cnf.jkt` claim binds
    that key to the token. `jkt` replaces the legacy `fingerprint` field
    (which was a different hash — SHA-256 of the serialized Ed25519 key, not
    the canonical JWK).

    `owner` is the human owner (access-token `sub` claim, RFC 9068 §2.2).
    It is always present and cryptographically attested; there is no
    "unowned agent" state under DPoP.
    """

    jkt: str
    owner: str


class _TTLJwks:
    """JWKS fetch with a TTL and a single in-flight fetch.

    `fetch_alien_jwks()` is a network call; we cache its result for
    `ttl_sec` seconds. The lock prevents a thundering herd of parallel
    fetches when the cache expires under concurrent load — the first
    request after expiry refreshes, the rest block briefly and reuse the
    result. A fetch failure leaves the previous value in place (we'd
    rather authenticate against a slightly stale JWKS than 503 everyone)
    and will be retried on the next request.

    `sso_base_url` is the SSO origin to fetch from. `None` lets the SDK
    pick its default (Alien SSO prod). Override at construction time for
    non-prod deployments — the URL is not re-read at `get()` time so the
    cache and the verifier's `expected_issuer` cannot disagree.
    """

    def __init__(self, ttl_sec: int, sso_base_url: Optional[str] = None) -> None:
        self._ttl = ttl_sec
        self._sso_base_url = sso_base_url
        self._lock = threading.Lock()
        self._jwks: Optional[JWKS] = None
        self._fetched_at: float = 0.0

    def _fetch(self) -> JWKS:
        if self._sso_base_url is None:
            return fetch_alien_jwks()
        return fetch_alien_jwks(sso_base_url=self._sso_base_url)

    def get(self) -> JWKS:
        now = time.monotonic()
        if self._jwks is not None and now - self._fetched_at < self._ttl:
            return self._jwks
        with self._lock:
            now = time.monotonic()
            if self._jwks is not None and now - self._fetched_at < self._ttl:
                return self._jwks
            try:
                self._jwks = self._fetch()
                self._fetched_at = now
            except Exception:
                if self._jwks is None:
                    raise  # no previous value — propagate
                log.warning(
                    "jwks refresh failed; serving cached jwks until next attempt",
                    exc_info=True,
                )
            return self._jwks  # type: ignore[return-value]


_jwks_cache = _TTLJwks(
    ttl_sec=int(os.environ.get("MCON_JWKS_TTL_SECONDS", _DEFAULT_JWKS_TTL_SEC)),
    sso_base_url=_SSO_BASE_URL,
)


def _build_req(request: Request) -> dict:
    """Adapt a Starlette `Request` to the dict shape `verify_dpop_request`
    expects (case-folded header dict; scalar or list of values per name).
    `str(request.url)` carries the query string, but the SDK's `htu` check
    normalizes to origin + pathname per WHATWG URL — see the agent-id PR
    body for the full normalization rules."""
    headers: dict[str, object] = {}
    for k, v in request.headers.items():
        existing = headers.get(k.lower())
        if existing is None:
            headers[k.lower()] = v
        elif isinstance(existing, list):
            existing.append(v)
        else:
            headers[k.lower()] = [existing, v]
    return {
        "method": request.method,
        "url": str(request.url),
        "headers": headers,
    }


def require_agent(request: Request) -> AgentIdentity:
    """FastAPI dependency: verify an RFC 9449 DPoP request and return identity.

    On failure, raises 401 with an RFC 6750 §3.1 / RFC 9449 §7.1
    `WWW-Authenticate: DPoP error="invalid_token"` challenge. The
    failure-code label (e.g. `jkt_mismatch`, `htu_mismatch`, `proof_stale`)
    is both logged and surfaced as the `error_description` parameter on the
    challenge so the calling agent can self-diagnose.
    """
    result = verify_dpop_request(
        _build_req(request),
        VerifyDPoPOptions(
            jwks=_jwks_cache.get(),
            # Lock the verifier's expected `iss` to whatever the JWKS was
            # fetched from. The SDK's default would pick prod; if mcon is
            # pointed at staging/develop, accepting a prod-issued token
            # against staging keys would be a confusion attack waiting to
            # happen. Letting `None` fall through to the SDK's default
            # keeps prod-vs-prod the SDK's responsibility.
            expected_issuer=_SSO_BASE_URL,
        ),
    )
    if isinstance(result, VerifyDPoPFailure):
        # Log the machine-readable code only — no token bytes, no header
        # values, nothing PII-bearing. The SDK's `code` set is stable
        # across releases (see VerifyDPoPFailure docstring).
        log.info("dpop verify failed: %s", result.code)
        raise HTTPException(
            401,
            result.error,
            headers={
                "WWW-Authenticate": (
                    f'DPoP error="invalid_token", error_description="{result.code}"'
                )
            },
        )
    # Static narrowing: the SDK guarantees Union[Success, Failure].
    if not isinstance(result, VerifyDPoPSuccess):  # pragma: no cover - defensive
        log.error("verify_dpop_request returned unexpected type %r", type(result))
        raise HTTPException(500, "internal auth error")
    return AgentIdentity(jkt=result.jkt, owner=result.sub)


# Legacy alias: pre-DPoP mCon distinguished `require_agent` (any verified
# agent) from `require_owned_agent` (agent bound to a human owner). Under
# DPoP every access token carries an attested `sub`, so the distinction is
# vacuous and the two collapse into one. The alias is preserved so existing
# `app.py` imports and any downstream code keep working without churn.
require_owned_agent = require_agent
