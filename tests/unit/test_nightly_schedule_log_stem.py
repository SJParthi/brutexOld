"""The additive ``log_stem`` param on the nightly plist renderer/installer.

DEC-CROSSVERIFY-PRODUCER-001 (MIGRATION.md #1753): the cross-verify producer
reuses the ONE launchd render/install law with its own log stem instead of
minting a second copy — so ``render_launchd_plist`` (and the delegating
``install``) gained an ADDITIVE, DEFAULTED ``log_stem`` (+ ``next_step``)
kw-only param. These pins prove the nightly bytes are UNCHANGED at the
default (the golden law) and the custom stem is honored.
"""

from __future__ import annotations

from pathlib import Path

from brutex.nightly.schedule import install, render_launchd_plist


def _render(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "label": "com.brutex.nightly",
        "program_arguments": ["/usr/local/bin/brutex", "nightly", "run"],
        "hour": 1,
        "minute": 30,
        "env_vars": {},
        "log_dir": Path("/Users/op/.brutex/logs"),
    }
    kwargs.update(overrides)
    return render_launchd_plist(**kwargs)  # type: ignore[arg-type]


def test_default_log_stem_renders_byte_identical_nightly_plist() -> None:
    """Omitting log_stem == passing the historical 'nightly-launchd' — byte-equal."""
    assert _render() == _render(log_stem="nightly-launchd")
    rendered = _render()
    assert "<string>/Users/op/.brutex/logs/nightly-launchd.out.log</string>" in rendered
    assert "<string>/Users/op/.brutex/logs/nightly-launchd.err.log</string>" in rendered


def test_custom_log_stem_names_both_log_files() -> None:
    rendered = _render(log_stem="crossverify-launchd")
    assert "<string>/Users/op/.brutex/logs/crossverify-launchd.out.log</string>" in rendered
    assert "<string>/Users/op/.brutex/logs/crossverify-launchd.err.log</string>" in rendered
    assert "nightly-launchd" not in rendered


def test_install_threads_log_stem_and_next_step(tmp_path: Path) -> None:
    """The delegating installer rides the same additive params end-to-end."""
    calls: list[list[str]] = []

    def runner(argv: object) -> tuple[int, str, str]:
        calls.append(list(argv))  # type: ignore[call-overload]
        return 0, "", ""

    outcome = install(
        label="com.brutex.crossverify",
        plist_dir=tmp_path / "agents",
        runner=runner,
        program_arguments=["/usr/local/bin/brutex", "crossverify", "run"],
        hour=16,
        minute=15,
        env_vars={},
        log_dir=tmp_path / "logs",
        log_stem="crossverify-launchd",
        next_step="brutex crossverify run --dry-run",
    )
    assert outcome.ok
    assert "Next step: brutex crossverify run --dry-run" in outcome.detail
    payload = (tmp_path / "agents" / "com.brutex.crossverify.plist").read_text(encoding="utf-8")
    assert "crossverify-launchd.out.log" in payload
    assert "nightly-launchd" not in payload


def test_install_default_next_step_is_the_nightly_hint(tmp_path: Path) -> None:
    """Every existing nightly caller's success detail is byte-compatible."""
    outcome = install(
        label="com.brutex.nightly",
        plist_dir=tmp_path / "agents",
        runner=lambda argv: (0, "", ""),
        program_arguments=["/usr/local/bin/brutex", "nightly", "run"],
        hour=1,
        minute=30,
        env_vars={},
        log_dir=tmp_path / "logs",
    )
    assert outcome.ok
    assert "Next step: brutex nightly run --dry-run" in outcome.detail
