"""Alien Agent ID token verification.

The agent calls our API with `Authorization: AgentID <base64url-json>`. The
token is self-contained — it carries the agent's Ed25519 public key so we can
verify the signature with no prior key registration. Owner-bound tokens
ALSO carry the SSO `ownerBinding` and `idToken`, which we verify against
Alien SSO's JWKS to prove the human owner actually authorized this agent.

References:
- https://github.com/alien-id/agent-id/blob/main/docs/INTEGRATION.md
- skills/alien-agent-id/lib.mjs (canonical implementation)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Header, HTTPException

MAX_AGE_MS = 5 * 60 * 1000

# Fields covered by the Ed25519 signature (lib.mjs `createAgentToken`).
# `ownerBinding` and `idToken` are appended after signing.
_SIGNED_FIELDS = ("v", "fingerprint", "publicKeyPem", "owner", "timestamp", "nonce")

# Deep verification (SSO chain) is on by default. Set MCON_SSO_VERIFY=off
# to skip it — useful only for local development with hand-rolled tokens.
_SSO_VERIFY = os.environ.get("MCON_SSO_VERIFY", "on").lower() not in ("0", "off", "false", "no")

_DISCOVERY_TTL = 24 * 3600
_JWKS_TTL = 24 * 3600
_HTTP_TIMEOUT = 5.0


@dataclass(frozen=True)
class AgentIdentity:
    fingerprint: str
    owner: Optional[str]
    public_key_pem: str


class _BadToken(Exception):
    pass


# --------------------------- low-level helpers ---------------------------


def _b64url_decode(s: str) -> bytes:
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _canonical_json(obj) -> bytes:
    """JSON.stringify(sortValue(obj)) — keys sorted recursively, no whitespace,
    UTF-8 (no \\uXXXX escaping). Mirrors lib.mjs `canonicalJSONString`."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# --------------------------- JWKS cache ---------------------------


_disc_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.RLock()


def _http_get_json(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status >= 400:
                raise _BadToken(f"HTTP {resp.status} from {url}")
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        raise _BadToken(f"cannot reach {url}: {e}") from e


def _get_discovery(issuer: str) -> dict:
    issuer = issuer.rstrip("/")
    with _cache_lock:
        cached = _disc_cache.get(issuer)
        if cached and time.time() - cached[0] < _DISCOVERY_TTL:
            return cached[1]
    doc = _http_get_json(f"{issuer}/.well-known/openid-configuration")
    with _cache_lock:
        _disc_cache[issuer] = (time.time(), doc)
    return doc


def _get_jwks(jwks_uri: str) -> dict:
    with _cache_lock:
        cached = _jwks_cache.get(jwks_uri)
        if cached and time.time() - cached[0] < _JWKS_TTL:
            return cached[1]
    doc = _http_get_json(jwks_uri)
    with _cache_lock:
        _jwks_cache[jwks_uri] = (time.time(), doc)
    return doc


def _resolve_jwk(issuer: str, kid: Optional[str]) -> dict:
    disc = _get_discovery(issuer)
    if disc.get("issuer") and disc["issuer"].rstrip("/") != issuer.rstrip("/"):
        raise _BadToken(
            f"OIDC discovery issuer mismatch: expected {issuer}, got {disc['issuer']}"
        )
    jwks_uri = disc.get("jwks_uri")
    if not jwks_uri:
        raise _BadToken("OIDC discovery missing jwks_uri")
    jwks = _get_jwks(jwks_uri)
    for k in jwks.get("keys") or []:
        if k.get("kid") == kid and k.get("kty") == "RSA":
            return k
    raise _BadToken(f"no RSA JWK with kid={kid!r} at {jwks_uri}")


def _jwk_to_rsa(jwk: dict) -> rsa.RSAPublicKey:
    if jwk.get("kty") != "RSA":
        raise _BadToken("JWK is not RSA")
    n_bytes = _b64url_decode(jwk["n"])
    e_bytes = _b64url_decode(jwk["e"])
    n = int.from_bytes(n_bytes, "big")
    e = int.from_bytes(e_bytes, "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


# --------------------------- token verification ---------------------------


def _verify_owner_binding(
    token: dict,
    *,
    fingerprint: str,
    owner: str,
    public_key_pem: str,
) -> None:
    """Deep verification: prove that Alien SSO actually attested the
    fingerprint↔owner binding. Raises `_BadToken` on any failure.

    Steps mirror lib.mjs `verifyOwnerBindingRecord` + `verifyIdTokenSignatureOnly`:

    1. ownerBinding.payload.agentInstance binds this exact pubkey.
    2. ownerBinding.payload.ownerSessionSub matches the token's owner claim.
    3. ownerBinding.signature (Ed25519) is valid over canonical(payload),
       signed by the agent's own key.
    4. sha256(idToken_string) matches ownerBinding.payload.idTokenHash.
    5. idToken JWT signature (RS256) verifies against the JWKS published by
       the issuer in ownerBinding.payload.issuer.
    6. idToken.iss / sub / aud match the binding.

    The id_token's `exp` is intentionally NOT checked — id_tokens are
    one-shot SSO attestations that remain valid proof of the original
    authorization. Replay protection comes from the outer agent token's
    own 5-minute freshness window.
    """
    binding = token.get("ownerBinding")
    id_token = token.get("idToken")
    if not isinstance(binding, dict):
        raise _BadToken("owner-bound token is missing ownerBinding")
    if not isinstance(id_token, str):
        raise _BadToken("owner-bound token is missing idToken")

    payload = binding.get("payload")
    sig_b64 = binding.get("signature")
    if not isinstance(payload, dict) or not isinstance(sig_b64, str):
        raise _BadToken("ownerBinding malformed")

    # 1. agentInstance pins the fingerprint+pubkey to this binding
    instance = payload.get("agentInstance")
    if not isinstance(instance, dict):
        raise _BadToken("ownerBinding payload missing agentInstance")
    if instance.get("publicKeyFingerprint") != fingerprint:
        raise _BadToken("ownerBinding agentInstance fingerprint mismatch")
    if instance.get("publicKeyPem") != public_key_pem:
        raise _BadToken("ownerBinding agentInstance publicKeyPem mismatch")

    # 2. owner sub matches the token's owner claim
    if payload.get("ownerSessionSub") != owner:
        raise _BadToken(
            f"ownerBinding ownerSessionSub does not match token owner {owner!r}"
        )

    # 3. Ed25519 signature on the binding payload, verified with agent key
    try:
        agent_pub = serialization.load_pem_public_key(public_key_pem.encode())
    except Exception as e:
        raise _BadToken(f"agent publicKeyPem invalid: {e}") from e
    if not isinstance(agent_pub, Ed25519PublicKey):
        raise _BadToken("agent public key is not Ed25519")
    try:
        agent_pub.verify(_b64url_decode(sig_b64), _canonical_json(payload))
    except InvalidSignature as e:
        raise _BadToken("ownerBinding signature invalid") from e

    # 4. idToken hash binding: prevents swapping id_tokens between bindings
    expected_hash = hashlib.sha256(id_token.encode("utf-8")).hexdigest()
    if payload.get("idTokenHash") != expected_hash:
        raise _BadToken("idToken hash does not match ownerBinding")

    # 5/6. JWT signature (RS256) against issuer's JWKS, plus claim checks
    issuer = payload.get("issuer")
    provider = payload.get("providerAddress")
    if not isinstance(issuer, str) or not issuer:
        raise _BadToken("ownerBinding missing issuer")

    parts = id_token.split(".")
    if len(parts) != 3:
        raise _BadToken("idToken is not a valid JWT")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        jwt_payload = json.loads(_b64url_decode(parts[1]))
        jwt_sig = _b64url_decode(parts[2])
    except Exception as e:
        raise _BadToken(f"idToken decode failed: {e}") from e

    alg = header.get("alg")
    if alg != "RS256":
        raise _BadToken(f"unsupported idToken alg: {alg!r} (only RS256)")
    if jwt_payload.get("iss") != issuer:
        raise _BadToken(
            f"idToken iss mismatch: expected {issuer!r}, got {jwt_payload.get('iss')!r}"
        )
    if jwt_payload.get("sub") != owner:
        raise _BadToken("idToken sub does not match owner")
    aud = jwt_payload.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if provider and provider not in aud_list:
        raise _BadToken("idToken aud does not include providerAddress")

    jwk = _resolve_jwk(issuer, header.get("kid"))
    rsa_pub = _jwk_to_rsa(jwk)
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    try:
        rsa_pub.verify(jwt_sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as e:
        raise _BadToken("idToken RS256 signature verification failed") from e


def verify_token(token_b64: str, *, max_age_ms: int = MAX_AGE_MS) -> AgentIdentity:
    """Verify an `AgentID` bearer token. Raises `_BadToken` on failure.

    For owner-bound tokens, also performs deep SSO verification of the
    ownerBinding + idToken — required so the `owner` field is trustworthy
    enough to enforce the per-owner agent quota.
    """
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
    if parsed.get("fingerprint") != fp_computed:
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

    # Deep verification: an owner claim is only trusted if the SSO chain
    # validates. Without this the per-owner quota can be bypassed by
    # minting tokens with arbitrary owner strings (Sybil).
    if owner and _SSO_VERIFY:
        _verify_owner_binding(
            parsed,
            fingerprint=fp_computed,
            owner=owner,
            public_key_pem=pem,
        )

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
