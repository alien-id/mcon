"""Integration tests for /.well-known/alien-agent-id.json discovery and the
closed-enum HTML support-signal meta tag.

These tests exercise the publisher side: that mcon emits a v1 manifest
shaped exactly the way the agent-id consumer expects, with a same-authority
api.base, the AgentID auth scheme, and no free-text fields. They also
confirm the legacy markdown routes (/ALIEN-SKILL.md, /api/skill) are gone.

Run: pytest tests/test_well_known.py
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    # NiceGUI mounts on a FastAPI `app` instance; importing the module wires
    # all routes (including /.well-known/alien-agent-id.json from app.py).
    import app as mcon_app  # noqa: F401  -- side effect: route registration

    from nicegui import app as fastapi_app

    return TestClient(fastapi_app)


# ── manifest happy path ────────────────────────────────────────────────────


def test_manifest_returns_200_with_json_content_type(client: TestClient) -> None:
    r = client.get("/.well-known/alien-agent-id.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_manifest_v1_required_fields(client: TestClient) -> None:
    body = client.get("/.well-known/alien-agent-id.json").json()
    assert body["version"] == 1
    assert body["auth"]["header"] == "Authorization"
    assert body["auth"]["scheme"] == "DPoP", (
        "scheme must be DPoP — agents present 'Authorization: DPoP <access_token>' "
        "(RFC 9449 §7.1) paired with a DPoP proof header; Bearer or the legacy "
        "AgentID envelope would produce headers the verifier rejects"
    )
    assert isinstance(body["api"]["base"], str) and body["api"]["base"]
    assert body["service"]["name"] == "mcon"


def test_manifest_api_base_under_same_authority_as_service_url(client: TestClient) -> None:
    body = client.get("/.well-known/alien-agent-id.json").json()
    api_host = urlparse(body["api"]["base"]).netloc
    svc_host = urlparse(body["service"]["url"]).netloc
    assert api_host == svc_host or api_host.endswith("." + svc_host), (
        f"api.base host '{api_host}' must share authority with service.url host '{svc_host}'"
    )


def test_manifest_api_base_origin_matches_request(client: TestClient) -> None:
    """Origin reflection: agents probe the same host they were given."""
    r = client.get("/.well-known/alien-agent-id.json")
    body = r.json()
    # TestClient default origin is http://testserver
    assert body["api"]["base"].startswith("http://testserver"), body["api"]["base"]
    assert body["api"]["base"].endswith("/api")
    assert body["service"]["url"] == "http://testserver"


# ── trust-boundary invariants ──────────────────────────────────────────────


def test_manifest_has_no_free_text_fields(client: TestClient) -> None:
    """The schema explicitly rejects 'instructions', 'notes', 'description',
    'prompt' — the publisher must not include them either, or agents using
    a permissive parser would inadvertently expose a prompt-injection surface.
    """
    body = client.get("/.well-known/alien-agent-id.json").json()
    forbidden = ("instructions", "notes", "description", "prompt", "skill", "skillUrl")
    for block_name in ("", "auth", "api", "service"):
        block = body if block_name == "" else body.get(block_name, {})
        for f in forbidden:
            assert f not in block, f"forbidden field '{f}' found in manifest{':'+block_name if block_name else ''}"


def test_manifest_top_level_keys_are_closed_set(client: TestClient) -> None:
    body = client.get("/.well-known/alien-agent-id.json").json()
    allowed = {"version", "service", "auth", "api"}
    extra = set(body.keys()) - allowed
    assert not extra, f"unexpected top-level keys in manifest: {extra}"


# ── legacy routes removed ──────────────────────────────────────────────────
#
# These check the route table directly rather than driving HTTP through the
# NiceGUI page handler — `ui.run()` is module-guarded for testability and
# NiceGUI's page-resolution machinery isn't fully wired up without it.


def _registered_paths() -> set[str]:
    import app as _app  # noqa: F401  -- side effect: route registration
    from nicegui import app as fastapi_app

    return {getattr(r, "path", "") for r in fastapi_app.routes}


def test_legacy_alien_skill_md_route_is_gone() -> None:
    assert "/ALIEN-SKILL.md" not in _registered_paths()


def test_legacy_api_skill_route_is_gone() -> None:
    assert "/api/skill" not in _registered_paths()


def test_well_known_route_is_registered() -> None:
    assert "/.well-known/alien-agent-id.json" in _registered_paths()


# ── support-signal meta tag ────────────────────────────────────────────────
#
# `_head_html()` is the helper that emits `<head>` content for every NiceGUI
# page in mcon. Calling it directly avoids needing a running NiceGUI server.


def test_head_html_emits_closed_enum_support_meta_tag() -> None:
    import app as _app

    head = _app._head_html()
    assert 'name="alien-agent-id"' in head, "support-signal meta tag missing"
    assert 'content="v1"' in head, "meta tag content must be the closed-enum 'v1'"


def test_head_html_meta_tag_carries_no_prose_or_url() -> None:
    """The pre-W011-fix meta tag carried prose like 'FOR AI AGENTS: read the
    skill at /ALIEN-SKILL.md'. The closed-enum form must not carry any URL
    or instruction text.
    """
    import re

    import app as _app

    head = _app._head_html()
    matches = re.findall(
        r'<meta\b[^>]*name=["\']alien-agent-id["\'][^>]*>',
        head,
        flags=re.IGNORECASE,
    )
    assert matches, "no alien-agent-id meta tag found in _head_html()"
    for tag in matches:
        m = re.search(r'content=["\']([^"\']*)["\']', tag, flags=re.IGNORECASE)
        assert m, f"meta tag has no content attribute: {tag}"
        content = m.group(1)
        assert content == "v1", (
            f"meta content must be the closed-enum 'v1', got: {content!r}"
        )
        # Specifically forbid the legacy prose pattern.
        assert "/" not in content, "meta content must not carry a URL"
        assert "FOR AI AGENTS" not in content.upper(), (
            "legacy prose meta-tag pattern leaked back in"
        )
