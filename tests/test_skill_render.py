"""Unit tests for the frontmatter stripping used by the /skill render path.

The function is a tiny string utility; we test it directly rather than via
the FastAPI/NiceGUI route stack. The route itself is exercised in dev by
loading https://mcon.alien.org/skill — if the regression here passes but
the rendered page looks off, the issue is in `ui.markdown`, not us.
"""

from __future__ import annotations

from app import _strip_frontmatter


def test_strips_frontmatter_when_present() -> None:
    md = (
        "---\n"
        "name: example\n"
        "version: 1.0\n"
        "allowed-tools: Bash(curl:*) Bash(jq:*)\n"
        "---\n"
        "\n"
        "# Title\n"
        "\n"
        "Body text.\n"
    )
    out = _strip_frontmatter(md)
    assert out == "# Title\n\nBody text.\n"


def test_passes_through_when_no_frontmatter() -> None:
    md = "# Title\n\nNo frontmatter here.\n"
    assert _strip_frontmatter(md) == md


def test_passes_through_on_unclosed_frontmatter() -> None:
    """Better to render the source verbatim than to swallow it if the
    opening fence never closes."""
    md = "---\nname: example\nversion: 1.0\n\n# Title\n"
    assert _strip_frontmatter(md) == md


def test_does_not_swallow_triple_dash_horizontal_rule() -> None:
    """A `---` inside the body (e.g. a markdown horizontal rule) is not a
    frontmatter close and must survive."""
    md = "# Title\n\nSome text.\n\n---\n\nMore text.\n"
    assert _strip_frontmatter(md) == md


def test_strips_only_first_frontmatter_block() -> None:
    """If a stray `---` appears later, the second one should be treated as
    a horizontal rule in the body, not a second frontmatter."""
    md = (
        "---\n"
        "name: example\n"
        "---\n"
        "\n"
        "# Title\n"
        "\n"
        "---\n"
        "\n"
        "Body after a horizontal rule.\n"
    )
    out = _strip_frontmatter(md)
    assert out == "# Title\n\n---\n\nBody after a horizontal rule.\n"


def test_real_mcon_skill_has_no_yaml_leakage() -> None:
    """Smoke test against the real skill file shipped with the repo."""
    from pathlib import Path

    skill = Path(__file__).resolve().parent.parent / "skills" / "mcon-agent" / "SKILL.md"
    rendered = _strip_frontmatter(skill.read_text())
    # The first non-empty line of the rendered output should be the
    # Markdown H1, not a frontmatter key.
    first_real_line = next(line for line in rendered.splitlines() if line.strip())
    assert first_real_line.startswith("# "), (
        f"expected H1, got {first_real_line!r} — frontmatter likely leaked through"
    )
    # And none of the frontmatter keys should appear at the start of any line.
    for forbidden in ("name:", "license:", "allowed-tools:", "compatibility:", "metadata:"):
        for line in rendered.splitlines():
            assert not line.lstrip().startswith(forbidden), (
                f"frontmatter key {forbidden!r} leaked into rendered output: {line!r}"
            )
