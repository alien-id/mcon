"""Alien Agent ID authentication for mCon's FastAPI surface.

Verification is delegated to `alien_sso_agent_id`. This module just adapts
that to FastAPI dependencies and toggles the SSO chain check via env var.
"""

from __future__ import annotations

import os
from typing import Optional

from alien_sso_agent_id import (
    AgentIdentity,
    VerifyFailure,
    VerifyOptions,
    VerifyOwnerOptions,
    VerifyOwnerSuccess,
    VerifySuccess,
    fetch_alien_jwks,
    verify_agent_token,
    verify_agent_token_with_owner,
)
from fastapi import Header, HTTPException

# Deep verification (SSO chain) is on by default. Set MCON_SSO_VERIFY=off to
# skip it — useful only for local development with hand-rolled tokens.
_SSO_VERIFY = os.environ.get("MCON_SSO_VERIFY", "on").lower() not in ("0", "off", "false", "no")


def _load_jwks_lazy():
    """Defer the network call until first auth attempt; cache forever."""
    if not _load_jwks_lazy._cached:  # type: ignore[attr-defined]
        _load_jwks_lazy._cached = fetch_alien_jwks() if _SSO_VERIFY else None  # type: ignore[attr-defined]
    return _load_jwks_lazy._cached  # type: ignore[attr-defined]


_load_jwks_lazy._cached = None  # type: ignore[attr-defined]


def _verify(token: str) -> AgentIdentity:
    if _SSO_VERIFY:
        result = verify_agent_token_with_owner(
            token, VerifyOwnerOptions(jwks=_load_jwks_lazy())
        )
    else:
        result = verify_agent_token(token, VerifyOptions())

    if isinstance(result, VerifyFailure):
        raise HTTPException(401, result.error)

    if isinstance(result, VerifyOwnerSuccess):
        return AgentIdentity(
            fingerprint=result.fingerprint,
            owner=result.owner,
            public_key_pem=result.public_key_pem,
            owner_verified=True,
        )
    assert isinstance(result, VerifySuccess)
    return AgentIdentity(
        fingerprint=result.fingerprint,
        owner=result.owner,
        public_key_pem=result.public_key_pem,
        owner_verified=result.owner_verified,
    )


def require_agent(authorization: Optional[str] = Header(default=None)) -> AgentIdentity:
    """FastAPI dependency: extract and verify an AgentID token."""
    if not authorization or not authorization.startswith("AgentID "):
        raise HTTPException(
            401,
            "missing Authorization: AgentID <token> header",
            headers={"WWW-Authenticate": "AgentID"},
        )
    return _verify(authorization[len("AgentID "):].strip())


def require_owned_agent(
    authorization: Optional[str] = Header(default=None),
) -> AgentIdentity:
    """Same as `require_agent`, but rejects agents without a verified owner."""
    ident = require_agent(authorization)
    if not ident.owner:
        raise HTTPException(
            403,
            "agent must be owner-bound (verify your Alien Agent ID with a human owner)",
        )
    return ident
