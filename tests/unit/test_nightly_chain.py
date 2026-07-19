"""Unit + golden tests for `brutex.nightly.chain` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The pure chain core: sequencer order, the multi-outcome PULL step
(``extra_outcomes``), every LinkStatus mapping, BLOCKED synthesis naming the
FIRST breaker, SKIPPED-does-not-break, exception→FAILED with error_type,
KeyboardInterrupt/SystemExit propagating raw, the exhaustive ``exit_code_for``
matrix (6 pass-through; SUSPECT→2 on all-green), the T1/T2 tripwires, and the
``to_dict`` goldens. The exit-code constants are drift-pinned against
``brutex.cli``'s (the chain core mirrors them to stay stdlib-only).
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Callable, Mapping

import pytest
from brutex.nightly.chain import (
    EVENT_LINK_BLOCKED,
    EVENT_LINK_DONE,
    EVENT_LINK_START,
    EVENT_TRIPWIRE,
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_SWEEP_TUPLE_FAILED,
    EXIT_USER_ERROR,
    LINK_ORDER,
    REPORT_SCHEMA,
    REPORT_SCHEMA_VERSION,
    ChainStep,
    LinkOutcome,
    LinkResult,
    LinkStatus,
    NightlyLink,
    NightlyReport,
    Tripwire,
    audit_reconcile_tripwire,
    exit_code_for,
    first_breaker,
    run_chain,
)


def _outcome(
    link: NightlyLink,
    status: LinkStatus,
    *,
    started: float | None = 1.0,
    ended: float | None = 2.0,
    detail: str = "",
    counts: Mapping[str, int] | None = None,
    error_type: str | None = None,
    blocked_by: NightlyLink | None = None,
    exit_hint: int | None = None,
) -> LinkOutcome:
    return LinkOutcome(
        link=link,
        status=status,
        started_utc=started,
        ended_utc=ended,
        detail=detail,
        counts=dict(counts or {}),
        error_type=error_type,
        blocked_by=blocked_by,
        exit_hint=exit_hint,
    )


class _Ticker:
    """Deterministic injected clock: 1.0, 2.0, 3.0, ..."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def _ok_steps() -> tuple[list[ChainStep], dict[NightlyLink, int]]:
    """Five green steps (PULL also declares PREBUILD) + per-link call counters."""
    calls: dict[NightlyLink, int] = dict.fromkeys(NightlyLink, 0)

    def fn(link: NightlyLink) -> LinkResult:
        calls[link] += 1
        if link is NightlyLink.PULL:
            prebuild = _outcome(NightlyLink.PREBUILD, LinkStatus.OK, detail="derived")
            return LinkResult(status=LinkStatus.OK, extra_outcomes=(prebuild,))
        return LinkResult(status=LinkStatus.OK)

    steps = [
        ChainStep(link=NightlyLink.COLLISION, fn=lambda: fn(NightlyLink.COLLISION)),
        ChainStep(link=NightlyLink.DISK, fn=lambda: fn(NightlyLink.DISK)),
        ChainStep(link=NightlyLink.BACKUP, fn=lambda: fn(NightlyLink.BACKUP)),
        ChainStep(
            link=NightlyLink.PULL,
            fn=lambda: fn(NightlyLink.PULL),
            also_declares=(NightlyLink.PREBUILD,),
        ),
        ChainStep(link=NightlyLink.SWEEP, fn=lambda: fn(NightlyLink.SWEEP)),
    ]
    return steps, calls


# ── constants + enums ────────────────────────────────────────────────────


def test_exit_codes_drift_pinned_against_cli() -> None:
    """The stdlib-only mirrors must equal the CLI's authoritative constants."""
    from brutex import cli

    assert EXIT_OK == cli.EXIT_OK
    assert EXIT_USER_ERROR == cli.EXIT_USER_ERROR
    assert EXIT_INTERNAL_ERROR == cli.EXIT_INTERNAL_ERROR
    assert EXIT_SWEEP_TUPLE_FAILED == cli.EXIT_SWEEP_TUPLE_FAILED


def test_link_order_is_the_six_step_law() -> None:
    assert LINK_ORDER == (
        NightlyLink.COLLISION,
        NightlyLink.DISK,
        NightlyLink.BACKUP,
        NightlyLink.PULL,
        NightlyLink.PREBUILD,
        NightlyLink.SWEEP,
    )
    assert [link.value for link in LINK_ORDER] == [
        "collision",
        "disk",
        "backup",
        "pull",
        "prebuild",
        "sweep",
    ]


def test_link_result_refuses_blocked_status() -> None:
    """BLOCKED is sequencer-assigned only — a callable can never return it."""
    with pytest.raises(ValueError, match="sequencer-assigned"):
        LinkResult(status=LinkStatus.BLOCKED)


@pytest.mark.parametrize(
    "status", [LinkStatus.OK, LinkStatus.SKIPPED, LinkStatus.REFUSED, LinkStatus.FAILED]
)
def test_link_result_accepts_every_returnable_status(status: LinkStatus) -> None:
    assert LinkResult(status=status).status is status


# ── happy path ───────────────────────────────────────────────────────────


def test_happy_path_all_six_links_ok_in_order() -> None:
    steps, calls = _ok_steps()
    events: list[tuple[str, Mapping[str, object]]] = []
    outcomes, tripwires = run_chain(
        nightly_id="nightly-x",
        steps=steps,
        on_event=lambda event, payload: events.append((event, payload)),
        now=_Ticker(),
    )
    assert [o.link for o in outcomes] == list(LINK_ORDER)
    assert all(o.status is LinkStatus.OK for o in outcomes)
    assert tripwires == ()
    assert all(calls[s.link] == 1 for s in steps)
    # The sequencer stamps its own timing on executed steps; the PREBUILD
    # extra keeps the timing its builder gave it.
    collision = outcomes[0]
    assert (collision.started_utc, collision.ended_utc) == (1.0, 2.0)
    # Event stream: START+DONE per executed step, DONE for the extra outcome.
    kinds = [event for event, _ in events]
    assert kinds == [
        EVENT_LINK_START,
        EVENT_LINK_DONE,  # collision
        EVENT_LINK_START,
        EVENT_LINK_DONE,  # disk
        EVENT_LINK_START,
        EVENT_LINK_DONE,  # backup
        EVENT_LINK_START,
        EVENT_LINK_DONE,  # pull
        EVENT_LINK_DONE,  # prebuild (extra outcome of the pull step)
        EVENT_LINK_START,
        EVENT_LINK_DONE,  # sweep
    ]
    # Every event payload is stamped with the one R43 nightly id.
    assert all(payload["nightly_id"] == "nightly-x" for _, payload in events)


def test_on_event_none_is_silent() -> None:
    steps, _ = _ok_steps()
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    assert len(outcomes) == 6
    assert tripwires == ()


def test_skipped_does_not_break_the_chain() -> None:
    steps, calls = _ok_steps()
    steps[2] = ChainStep(
        link=NightlyLink.BACKUP,
        fn=lambda: LinkResult(status=LinkStatus.SKIPPED, detail="operator --skip-backup"),
    )
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    assert outcomes[2].status is LinkStatus.SKIPPED
    assert outcomes[2].detail == "operator --skip-backup"
    assert [o.status for o in outcomes[3:]] == [LinkStatus.OK] * 3
    assert calls[NightlyLink.PULL] == 1
    assert calls[NightlyLink.SWEEP] == 1
    assert tripwires == ()


# ── break law ────────────────────────────────────────────────────────────


def test_refused_link_blocks_every_later_link() -> None:
    steps, calls = _ok_steps()
    steps[1] = ChainStep(
        link=NightlyLink.DISK,
        fn=lambda: LinkResult(status=LinkStatus.REFUSED, detail="floor breached"),
    )
    events: list[tuple[str, Mapping[str, object]]] = []
    outcomes, tripwires = run_chain(
        nightly_id="n",
        steps=steps,
        on_event=lambda event, payload: events.append((event, payload)),
        now=_Ticker(),
    )
    assert tripwires == ()
    assert outcomes[1].status is LinkStatus.REFUSED
    blocked = outcomes[2:]
    assert [o.link for o in blocked] == [
        NightlyLink.BACKUP,
        NightlyLink.PULL,
        NightlyLink.PREBUILD,
        NightlyLink.SWEEP,
    ]
    for o in blocked:
        assert o.status is LinkStatus.BLOCKED
        assert o.blocked_by is NightlyLink.DISK
        assert o.detail == "REFUSED-BECAUSE-disk refused"
        assert o.started_utc is None
        assert o.ended_utc is None
    # Blocked steps are NEVER invoked.
    assert calls[NightlyLink.BACKUP] == 0
    assert calls[NightlyLink.PULL] == 0
    assert calls[NightlyLink.SWEEP] == 0
    assert [event for event, _ in events].count(EVENT_LINK_BLOCKED) == 4


def test_raising_step_becomes_failed_with_error_type_and_blocks() -> None:
    steps, calls = _ok_steps()

    def boom() -> LinkResult:
        raise RuntimeError("copy exploded: " + "x" * 600)

    steps[2] = ChainStep(link=NightlyLink.BACKUP, fn=boom)
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    backup = outcomes[2]
    assert backup.status is LinkStatus.FAILED
    assert backup.error_type == "RuntimeError"
    assert len(backup.detail) == 500  # truncation bound
    assert backup.detail.startswith("copy exploded: ")
    assert [o.status for o in outcomes[3:]] == [LinkStatus.BLOCKED] * 3
    assert all(o.blocked_by is NightlyLink.BACKUP for o in outcomes[3:])
    assert calls[NightlyLink.PULL] == 0
    assert tripwires == ()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_operator_interrupts_propagate_raw(interrupt: type[BaseException]) -> None:
    steps, _ = _ok_steps()

    def boom() -> LinkResult:
        raise interrupt

    steps[0] = ChainStep(link=NightlyLink.COLLISION, fn=boom)
    with pytest.raises(interrupt):
        run_chain(nightly_id="n", steps=steps, now=_Ticker())


def test_pull_failed_blocks_prebuild_and_sweep_without_tripwire() -> None:
    """PULL FAILED without a PREBUILD extra ⇒ PREBUILD synthesized BLOCKED-by-PULL."""
    steps, _ = _ok_steps()
    steps[3] = ChainStep(
        link=NightlyLink.PULL,
        fn=lambda: LinkResult(status=LinkStatus.FAILED, detail="stopped_after=spot"),
        also_declares=(NightlyLink.PREBUILD,),
    )
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    prebuild = outcomes[4]
    assert prebuild.link is NightlyLink.PREBUILD
    assert prebuild.status is LinkStatus.BLOCKED
    assert prebuild.blocked_by is NightlyLink.PULL
    assert prebuild.detail == "REFUSED-BECAUSE-pull failed"
    sweep = outcomes[5]
    assert sweep.status is LinkStatus.BLOCKED
    assert sweep.blocked_by is NightlyLink.PULL
    assert tripwires == ()  # a failed step owing no extra is NOT incoherent


def test_pull_ran_but_prebuild_extra_failed_blocks_sweep() -> None:
    """A FAILED extra outcome is a breaker for everything after it."""
    steps, calls = _ok_steps()
    failed_prebuild = _outcome(NightlyLink.PREBUILD, LinkStatus.FAILED, detail="quarantined: INFY")
    steps[3] = ChainStep(
        link=NightlyLink.PULL,
        fn=lambda: LinkResult(status=LinkStatus.OK, extra_outcomes=(failed_prebuild,)),
        also_declares=(NightlyLink.PREBUILD,),
    )
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    assert outcomes[3].status is LinkStatus.OK  # pull itself ran green
    assert outcomes[4].status is LinkStatus.FAILED
    sweep = outcomes[5]
    assert sweep.status is LinkStatus.BLOCKED
    assert sweep.blocked_by is NightlyLink.PREBUILD
    assert calls[NightlyLink.SWEEP] == 0
    assert tripwires == ()


# ── T1 tripwires (corrupted decomposition states) ───────────────────────


def test_green_step_missing_declared_outcome_fires_link_transition() -> None:
    steps, _ = _ok_steps()
    steps[3] = ChainStep(
        link=NightlyLink.PULL,
        fn=lambda: LinkResult(status=LinkStatus.OK),  # declares PREBUILD, reports nothing
        also_declares=(NightlyLink.PREBUILD,),
    )
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    prebuild = outcomes[4]
    assert prebuild.status is LinkStatus.BLOCKED
    assert prebuild.blocked_by is NightlyLink.PULL
    # Two tripwires: the missing declared outcome, then the belt stopping the
    # chain at the next step (a BLOCKED row with no breaker = corrupt state).
    assert [t.name for t in tripwires] == ["link_transition", "link_transition"]
    assert tripwires[0].link is NightlyLink.PREBUILD
    assert "reported no outcome for declared link" in tripwires[0].detail
    assert "no breaker" in tripwires[1].detail
    # The chain STOPPED: the sweep was never attempted.
    sweep = outcomes[5]
    assert sweep.status is LinkStatus.BLOCKED
    assert sweep.started_utc is None


def test_undeclared_extra_outcome_fires_link_transition() -> None:
    steps, _ = _ok_steps()
    rogue = _outcome(NightlyLink.SWEEP, LinkStatus.OK, detail="rogue")
    steps[2] = ChainStep(
        link=NightlyLink.BACKUP,
        fn=lambda: LinkResult(status=LinkStatus.OK, extra_outcomes=(rogue,)),
    )
    outcomes, tripwires = run_chain(nightly_id="n", steps=steps, now=_Ticker())
    # Recorded (never silent) AND tripwired.
    assert any(o.detail == "rogue" for o in outcomes)
    assert any(t.name == "link_transition" and "undeclared" in t.detail for t in tripwires)


def test_blocked_extra_without_breaker_stops_chain_via_t1() -> None:
    """A BLOCKED prior outcome with no breaker = untrustworthy state ⇒ halt."""
    steps, calls = _ok_steps()
    corrupt = LinkOutcome(
        link=NightlyLink.PREBUILD,
        status=LinkStatus.BLOCKED,
        started_utc=None,
        ended_utc=None,
        detail="corrupt",
        counts={},
        error_type=None,
        blocked_by=NightlyLink.PULL,
        exit_hint=None,
    )
    steps[3] = ChainStep(
        link=NightlyLink.PULL,
        fn=lambda: LinkResult(status=LinkStatus.OK, extra_outcomes=(corrupt,)),
        also_declares=(NightlyLink.PREBUILD,),
    )
    events: list[str] = []
    outcomes, tripwires = run_chain(
        nightly_id="n",
        steps=steps,
        on_event=lambda event, _payload: events.append(event),
        now=_Ticker(),
    )
    assert any(t.name == "link_transition" and "no breaker" in t.detail for t in tripwires)
    sweep = outcomes[-1]
    assert sweep.link is NightlyLink.SWEEP
    assert sweep.status is LinkStatus.BLOCKED
    assert calls[NightlyLink.SWEEP] == 0
    assert EVENT_TRIPWIRE in events


# ── exit-code law ────────────────────────────────────────────────────────


def test_exit_code_matrix() -> None:
    ok = _outcome(NightlyLink.PULL, LinkStatus.OK)
    skipped = _outcome(NightlyLink.BACKUP, LinkStatus.SKIPPED)
    refused = _outcome(NightlyLink.DISK, LinkStatus.REFUSED)
    failed = _outcome(NightlyLink.PULL, LinkStatus.FAILED)
    failed_6 = _outcome(NightlyLink.SWEEP, LinkStatus.FAILED, exit_hint=6)
    wire = Tripwire(name="audit_reconcile", link=None, detail="x")

    assert exit_code_for([ok, skipped], []) == EXIT_OK
    assert exit_code_for([ok, refused], []) == EXIT_USER_ERROR
    assert exit_code_for([ok, failed], []) == EXIT_INTERNAL_ERROR
    assert exit_code_for([ok, failed_6], []) == EXIT_SWEEP_TUPLE_FAILED
    # First breaker wins: a REFUSED before a FAILED reads user-error.
    assert exit_code_for([refused, failed_6], []) == EXIT_USER_ERROR
    # SUSPECT upgrades and never downgrades.
    assert exit_code_for([ok], [wire]) == EXIT_INTERNAL_ERROR
    assert exit_code_for([ok, refused], [wire]) == EXIT_INTERNAL_ERROR
    assert exit_code_for([ok, failed], [wire]) == EXIT_INTERNAL_ERROR
    assert exit_code_for([ok, failed_6], [wire]) == EXIT_SWEEP_TUPLE_FAILED
    assert exit_code_for([], []) == EXIT_OK


def test_first_breaker_returns_first_only() -> None:
    a = _outcome(NightlyLink.DISK, LinkStatus.REFUSED)
    b = _outcome(NightlyLink.PULL, LinkStatus.FAILED)
    assert first_breaker([_outcome(NightlyLink.COLLISION, LinkStatus.OK), a, b]) is a
    assert first_breaker([]) is None
    assert first_breaker([_outcome(NightlyLink.COLLISION, LinkStatus.OK)]) is None


# ── T2 audit reconcile ───────────────────────────────────────────────────


def _green_run() -> tuple[list[tuple[str, str | None]], list[LinkOutcome]]:
    outcomes = [_outcome(link, LinkStatus.OK) for link in LINK_ORDER]
    events: list[tuple[str, str | None]] = [("NIGHTLY_START", None)]
    for link in LINK_ORDER:
        events.append(("LINK_START", link.value))
        events.append(("LINK_DONE", link.value))
    return events, outcomes


def test_audit_reconcile_clean_run_returns_none() -> None:
    events, outcomes = _green_run()
    assert audit_reconcile_tripwire(nightly_id="n", events=events, outcomes=outcomes) is None


def test_audit_reconcile_blocked_links_reconcile_without_start() -> None:
    """A BLOCKED link has LINK_BLOCKED but no LINK_START — still clean."""
    outcomes = [
        _outcome(NightlyLink.COLLISION, LinkStatus.REFUSED),
        _outcome(NightlyLink.DISK, LinkStatus.BLOCKED, started=None, ended=None),
    ]
    events: list[tuple[str, str | None]] = [
        ("NIGHTLY_START", None),
        ("LINK_START", "collision"),
        ("LINK_DONE", "collision"),
        ("LINK_BLOCKED", "disk"),
    ]
    assert audit_reconcile_tripwire(nightly_id="n", events=events, outcomes=outcomes) is None


_Events = list[tuple[str, "str | None"]]


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda ev: [e for e in ev if e[0] != "NIGHTLY_START"], "NIGHTLY_START x0"),
        (lambda ev: [("NIGHTLY_START", None), *ev], "NIGHTLY_START x2"),
        (lambda ev: [("LINK_START", "prebuild"), *ev], "LINK_START"),
        (lambda ev: [("LINK_START", "disk"), *ev], "LINK_START"),
        (lambda ev: [e for e in ev if e != ("LINK_DONE", "sweep")], "LINK_DONE"),
    ],
)
def test_audit_reconcile_mismatches_fire(
    mutate: Callable[[_Events], _Events],
    expect: str,
) -> None:
    events, outcomes = _green_run()
    mutated = mutate(events)
    wire = audit_reconcile_tripwire(nightly_id="n-7", events=mutated, outcomes=outcomes)
    assert wire is not None
    assert wire.name == "audit_reconcile"
    assert "n-7" in wire.detail
    assert expect in wire.detail


# ── report + goldens ─────────────────────────────────────────────────────


def _report(
    *,
    links: tuple[LinkOutcome, ...] | None = None,
    tripwires: tuple[Tripwire, ...] = (),
) -> NightlyReport:
    if links is None:
        links = tuple(_outcome(link, LinkStatus.OK) for link in LINK_ORDER)
    return NightlyReport(
        schema_version=REPORT_SCHEMA_VERSION,
        nightly_id="nightly-20260708T013000Z-abc123",
        lake_root="/lake",
        results_root="/lake/sweeps",
        backup_root="/backups",
        started_utc=10.0,
        ended_utc=20.0,
        links=links,
        tripwires=tripwires,
        counts={"pull_bars": 5, "sweep_run": None},
        snapshot_id="nightly-20260708T013000Z-abc123",
        absent_artifacts=("run_manifest", "top100"),
        honesty_notes=("R48 decay diff deferred to v1.1",),
        dry_run=False,
    )


def test_report_green_properties() -> None:
    report = _report()
    assert report.ok is True
    assert report.suspect is False
    assert report.blocker is None
    assert report.exit_code == EXIT_OK


def test_report_suspect_and_blocker_properties() -> None:
    links = (
        _outcome(NightlyLink.COLLISION, LinkStatus.OK),
        _outcome(NightlyLink.DISK, LinkStatus.REFUSED),
        _outcome(NightlyLink.BACKUP, LinkStatus.BLOCKED, started=None, ended=None),
    )
    report = _report(links=links, tripwires=(Tripwire(name="t", link=None, detail="d"),))
    assert report.ok is False
    assert report.suspect is True
    assert report.blocker is NightlyLink.DISK
    assert report.exit_code == EXIT_INTERNAL_ERROR  # tripwire upgrades the refusal


def test_report_to_dict_golden() -> None:
    """Byte-shape golden: stable keys, enum values as strings, derived fields."""
    links = (
        _outcome(NightlyLink.COLLISION, LinkStatus.OK, counts={"locks": 2}),
        _outcome(
            NightlyLink.DISK,
            LinkStatus.BLOCKED,
            started=None,
            ended=None,
            blocked_by=NightlyLink.COLLISION,
            detail="REFUSED-BECAUSE-collision refused",
        ),
    )
    report = _report(links=links, tripwires=())
    got = report.to_dict()
    assert got == {
        "schema": REPORT_SCHEMA,
        "schema_version": 1,
        "nightly_id": "nightly-20260708T013000Z-abc123",
        "lake_root": "/lake",
        "results_root": "/lake/sweeps",
        "backup_root": "/backups",
        "started_utc": 10.0,
        "ended_utc": 20.0,
        "links": [
            {
                "link": "collision",
                "status": "ok",
                "started_utc": 1.0,
                "ended_utc": 2.0,
                "detail": "",
                "counts": {"locks": 2},
                "error_type": None,
                "blocked_by": None,
                "exit_hint": None,
            },
            {
                "link": "disk",
                "status": "blocked",
                "started_utc": None,
                "ended_utc": None,
                "detail": "REFUSED-BECAUSE-collision refused",
                "counts": {},
                "error_type": None,
                "blocked_by": "collision",
                "exit_hint": None,
            },
        ],
        "tripwires": [],
        "counts": {"pull_bars": 5, "sweep_run": None},
        "snapshot_id": "nightly-20260708T013000Z-abc123",
        "absent_artifacts": ["run_manifest", "top100"],
        "honesty_notes": ["R48 decay diff deferred to v1.1"],
        "dry_run": False,
        # DEC-BOX-AUTODEPLOY-LANE-001 §5 — the report golden CONSCIOUSLY
        # re-locked for the two ADDITIVE keys (emitted always; None = absent).
        "box_update": None,
        "env_preflight": None,
        # DEC-NIGHTLY-DECAY-R48-001 + DEC-CHAMPION-CHALLENGER-001 (LANE W5) —
        # re-locked again for the two ADDITIVE adaptation-leg blocks.
        "decay": None,
        "champions": None,
        "ok": False,
        "suspect": False,
        "blocker": None,
        "exit_code": 0,
    }


def test_report_to_dict_decay_champions_non_none() -> None:
    """The LANE-W5 additive blocks serialize as plain dicts when present."""
    filled = dataclasses.replace(
        _report(),
        decay={"status": "ok", "decayed": 1},
        champions={"status": "ok", "transitions": []},
    )
    got = filled.to_dict()
    assert got["decay"] == {"status": "ok", "decayed": 1}
    assert got["champions"] == {"status": "ok", "transitions": []}


def test_tripwire_to_dict_golden() -> None:
    assert Tripwire(name="snapshot_unreadable", link=NightlyLink.BACKUP, detail="x").to_dict() == {
        "name": "snapshot_unreadable",
        "link": "backup",
        "detail": "x",
    }
    assert Tripwire(name="audit_reconcile", link=None, detail="y").to_dict() == {
        "name": "audit_reconcile",
        "link": None,
        "detail": "y",
    }


def test_every_status_pair_has_a_defined_exit_code() -> None:
    """Total-function pin: any single-breaker chain maps into {0, 1, 2, 6}."""
    statuses = [LinkStatus.OK, LinkStatus.SKIPPED, LinkStatus.REFUSED, LinkStatus.FAILED]
    for first, second in itertools.product(statuses, repeat=2):
        outcomes = [
            _outcome(NightlyLink.COLLISION, first),
            _outcome(NightlyLink.DISK, second),
        ]
        assert exit_code_for(outcomes, []) in {0, 1, 2, 6}
        assert exit_code_for(outcomes, [Tripwire(name="t", link=None, detail="")]) in {2, 6}
