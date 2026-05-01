"""Alien Agent ID token verification.

The agent calls our API with `Authorization: AgentID <base64url-json>`. The
token is self-contained — it carries the agent's Ed25519 public key, so we can
verify the signature with no prior key registration. See:
https://github.com/alien-id/agent-id/blob/main/docs/INTEGRATION.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Header, HTTPException

MAX_AGE_MS = 5 * 60 * 1000

# Fields covered by the Ed25519 signature (lib.mjs `createAgentToken`).
# `ownerBinding` and `idToken` may be appended to the token after signing —
# they are NOT part of the signed payload and must be excluded here.
_SIGNED_FIELDS = ("v", "fingerprint", "publicKeyPem", "owner", "timestamp", "nonce")


@dataclass(frozen=True)
class AgentIdentity:
    fingerprint: str
    owner: Optional[str]
    public_key_pem: str


class _BadToken(Exception):
    pass


def _b64url_decode(s: str) -> bytes:
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def verify_token(token_b64: str, *, max_age_ms: int = MAX_AGE_MS) -> AgentIdentity:
    """Verify an `AgentID` bearer token. Raises `_BadToken` on failure."""
    try:
        raw = _b64url_decode(token_b64)
    except Exception as e:
        raise _BadToken(f"invalid token encoding: {e}") from e
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _BadToken(f"invalid token JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise _BadToken("token payload must be an object")

    if parsed.get("v") != 1:
        raise _BadToken(f"unsupported token version: {parsed.get('v')!r}")

    ts = parsed.get("timestamp")
    if not isinstance(ts, int):
        raise _BadToken("missing/invalid timestamp")
    age = int(time.time() * 1000) - ts
    if age < 0 or age > max_age_ms:
        raise _BadToken(f"token expired (age: {age // 1000}s)")

    pem = parsed.get("publicKeyPem")
    if not isinstance(pem, str):
        raise _BadToken("missing publicKeyPem")
    try:
        pubkey = serialization.load_pem_public_key(pem.encode())
    except Exception as e:
        raise _BadToken(f"invalid public key: {e}") from e
    if not isinstance(pubkey, Ed25519PublicKey):
        raise _BadToken("public key is not Ed25519")

    der = pubkey.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fp_computed = hashlib.sha256(der).hexdigest()
    fp_claimed = parsed.get("fingerprint")
    if fp_claimed != fp_computed:
        raise _BadToken("fingerprint does not match public key")

    sig_b64 = parsed.get("sig")
    if not isinstance(sig_b64, str):
        raise _BadToken("missing signature")
    try:
        sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise _BadToken(f"invalid signature encoding: {e}") from e

    payload = {k: parsed[k] for k in _SIGNED_FIELDS if k in parsed}
    try:
        pubkey.verify(sig, _canonical_json(payload))
    except InvalidSignature as e:
        raise _BadToken("signature verification failed") from e

    owner = parsed.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise _BadToken("owner must be a string or null")

    return AgentIdentity(
        fingerprint=fp_computed,
        owner=owner or None,
        public_key_pem=pem,
    )


def require_agent(authorization: Optional[str] = Header(default=None)) -> AgentIdentity:
    """FastAPI dependency: extract and verify an AgentID token."""
    if not authorization or not authorization.startswith("AgentID "):
        raise HTTPException(
            401,
            "missing Authorization: AgentID <token> header",
            headers={"WWW-Authenticate": "AgentID"},
        )
    token = authorization[len("AgentID "):].strip()
    try:
        return verify_token(token)
    except _BadToken as e:
        raise HTTPException(401, str(e)) from e


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
