"""Golden byte-exact snapshot of the ``doctor --live`` diagnostics catalog.

DEC-DOCTOR-LIVE-PROOF-KIT-001. The FAIL/WARN/PASS lines ARE the product — the
operator acts on their exact wording (register cross-references, fix commands,
parameter paths). This golden test freezes the catalog byte-exactly so no
refactor can silently drift a diagnosis. To change a line INTENTIONALLY,
update the fixture in the same PR (the diff then reviews the wording change).

Regenerate after an intentional edit::

    python -c "from tests.golden.test_doctor_live_diagnostics import regenerate; regenerate()"
"""

from __future__ import annotations

from pathlib import Path

from brutex.doctor_live import DIAGNOSTICS

_FIXTURE = Path(__file__).parent / "fixtures" / "doctor_live_diagnostics.txt"


def render_catalog() -> str:
    """The catalog serialised deterministically (sorted keys, escaped newlines)."""
    lines = [
        f"{key} ::= {template.replace(chr(10), '\\n')}"
        for key, template in sorted(DIAGNOSTICS.items())
    ]
    return "\n".join(lines) + "\n"


def regenerate() -> None:  # pragma: no cover — manual fixture regeneration helper
    """Rewrite the fixture from the live catalog (run only for INTENTIONAL edits)."""
    _FIXTURE.write_text(render_catalog(), encoding="utf-8")


def test_diagnostics_catalog_is_byte_exact() -> None:
    assert _FIXTURE.is_file(), "fixture missing — run the regenerate() helper and review the diff"
    assert render_catalog() == _FIXTURE.read_text(encoding="utf-8"), (
        "the doctor --live diagnostics catalog drifted from its golden snapshot; "
        "if the wording change is INTENTIONAL, regenerate the fixture in this PR "
        "(python -c 'from tests.golden.test_doctor_live_diagnostics import regenerate; "
        "regenerate()') and let the diff review the new wording"
    )


def test_every_diagnostic_is_register_cross_referenced_or_infra() -> None:
    """Every FAIL/WARN/EVIDENCE line names a register row or is kit infrastructure."""
    import re

    uncrossed = [
        key
        for key, template in DIAGNOSTICS.items()
        if template.split(" ", 1)[0] in {"FAIL", "WARN", "EVIDENCE"}
        and not re.search(r"\[(R\d|M\d+|N\d|G\d)", template)
        and not key.startswith(("kit.", "master.", "lake.", "integrity.", "resample."))
        and key
        not in {
            "totp.mint_failed",  # M3-adjacent; the primary M3 lines carry the tag
            "spot1m.parse_failed",  # the generic arm of the tagged M11/M13 family
            "spot1m.vendor_error",  # relays stage-3's tagged diagnoses
            "token.smoke_failed",  # the untagged generic arm of M1/M2/R8
            "token.mint_anyway",  # operator-invoked hazard warning, not a register row
            "bhavcopy.unreachable",  # external dependency, honestly untagged
            "bhavcopy.wiring",  # kit-INTERNAL wiring bug (mis-wired getter), not a register row
            "bhavcopy.unmapped",
            "bhavcopy.no_row",
            "bhavcopy.no_daily_bar",
            "rate.aborted",  # companion to the tagged rate.throttle_evidence
            "rate.pull_lock_held",  # operational single-flight refusal, not a register row
            "rate.lock_halt",  # infra halt (disk full/OOM) relayed from the lock, not a register row
            "depth.error",  # transport-failure arm of the tagged R6 family
        }
    ]
    assert uncrossed == []
