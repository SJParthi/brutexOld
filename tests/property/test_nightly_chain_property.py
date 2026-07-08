"""Property gates for the nightly chain break law (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The R42 invariant, mechanically pinned over EVERY failure position × failure
mode (spec §2.3): for a break at declared-link position N, links 1..N-1 have
exactly-once call counts, links N+1.. have ZERO calls and BLOCKED outcomes
whose ``blocked_by`` names links[N]; the exit code is a total function into
{0, 1, 2, 6}; and exactly one breaker exists iff the verdict is not OK.
"""

from __future__ import annotations

import enum

from brutex.nightly.chain import (
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_SWEEP_TUPLE_FAILED,
    EXIT_USER_ERROR,
    LINK_ORDER,
    ChainStep,
    LinkOutcome,
    LinkResult,
    LinkStatus,
    NightlyLink,
    exit_code_for,
    first_breaker,
    run_chain,
)
from hypothesis import given
from hypothesis import strategies as st


class _FailureMode(enum.Enum):
    FAILED = "failed"
    REFUSED = "refused"
    RAISE = "raise"


def _build_steps(
    fail_link: NightlyLink | None,
    mode: _FailureMode,
    calls: dict[NightlyLink, int],
    exit_hint: int | None,
) -> list[ChainStep]:
    """The production chain shape: 5 steps, PULL also declares PREBUILD."""

    def make_fn(link: NightlyLink) -> ChainStep:
        def fn() -> LinkResult:
            calls[link] += 1
            if link is fail_link:
                if mode is _FailureMode.RAISE:
                    raise RuntimeError(f"{link.value} exploded")
                if mode is _FailureMode.REFUSED:
                    return LinkResult(status=LinkStatus.REFUSED, detail="refused")
                return LinkResult(status=LinkStatus.FAILED, detail="failed", exit_hint=exit_hint)
            if link is NightlyLink.PULL:
                # Green PULL reports the PREBUILD derivation; a failing
                # PREBUILD is modelled as a FAILED extra outcome.
                if fail_link is NightlyLink.PREBUILD and mode is not _FailureMode.RAISE:
                    calls[NightlyLink.PREBUILD] += 1
                    status = (
                        LinkStatus.REFUSED if mode is _FailureMode.REFUSED else LinkStatus.FAILED
                    )
                    extra = LinkOutcome(
                        link=NightlyLink.PREBUILD,
                        status=status,
                        started_utc=1.0,
                        ended_utc=2.0,
                        detail="prebuild broke",
                        counts={},
                        error_type=None,
                        blocked_by=None,
                        exit_hint=None,
                    )
                    return LinkResult(status=LinkStatus.OK, extra_outcomes=(extra,))
                calls[NightlyLink.PREBUILD] += 1
                extra = LinkOutcome(
                    link=NightlyLink.PREBUILD,
                    status=LinkStatus.OK,
                    started_utc=1.0,
                    ended_utc=2.0,
                    detail="",
                    counts={},
                    error_type=None,
                    blocked_by=None,
                    exit_hint=None,
                )
                return LinkResult(status=LinkStatus.OK, extra_outcomes=(extra,))
            return LinkResult(status=LinkStatus.OK)

        also = (NightlyLink.PREBUILD,) if link is NightlyLink.PULL else ()
        return ChainStep(link=link, fn=fn, also_declares=also)

    return [
        make_fn(link)
        for link in LINK_ORDER
        if link is not NightlyLink.PREBUILD  # PREBUILD rides the PULL step
    ]


@given(
    fail_index=st.integers(min_value=0, max_value=len(LINK_ORDER) - 1),
    mode=st.sampled_from(list(_FailureMode)),
    hint_six=st.booleans(),
)
def test_break_law_every_position_and_mode(
    fail_index: int, mode: _FailureMode, hint_six: bool
) -> None:
    fail_link = LINK_ORDER[fail_index]
    if fail_link is NightlyLink.PREBUILD and mode is _FailureMode.RAISE:
        # A raising PREBUILD derivation surfaces as the PULL step raising —
        # remap to the executed step that owns it.
        fail_link = NightlyLink.PULL
        fail_index = LINK_ORDER.index(NightlyLink.PULL)
    exit_hint = EXIT_SWEEP_TUPLE_FAILED if hint_six else None

    calls: dict[NightlyLink, int] = dict.fromkeys(NightlyLink, 0)
    steps = _build_steps(fail_link, mode, calls, exit_hint)
    outcomes, tripwires = run_chain(nightly_id="prop", steps=steps, now=lambda: 0.0)

    assert tripwires == ()  # every modelled failure is a CLEAN break, never corruption
    assert [o.link for o in outcomes] == list(LINK_ORDER)

    # Links strictly BEFORE the breaker: exactly-once calls, green outcomes.
    for before in LINK_ORDER[:fail_index]:
        if before is NightlyLink.PREBUILD:
            continue  # rides PULL; counted with it below
        assert calls[before] == 1, before
    for outcome in outcomes[:fail_index]:
        assert outcome.status is LinkStatus.OK

    # The breaker itself.
    breaker = outcomes[fail_index]
    assert breaker.link is fail_link
    if mode is _FailureMode.RAISE:
        assert breaker.status is LinkStatus.FAILED
        assert breaker.error_type == "RuntimeError"
    elif mode is _FailureMode.REFUSED:
        assert breaker.status is LinkStatus.REFUSED
    else:
        assert breaker.status is LinkStatus.FAILED

    # Links strictly AFTER: zero calls, BLOCKED naming the breaker.
    for after_outcome in outcomes[fail_index + 1 :]:
        assert after_outcome.status is LinkStatus.BLOCKED
        assert after_outcome.blocked_by is fail_link
        assert after_outcome.started_utc is None
        if after_outcome.link is not NightlyLink.PREBUILD:
            assert calls[after_outcome.link] == 0

    # Exit code total-function + verdict laws.
    code = exit_code_for(outcomes, tripwires)
    assert code in {EXIT_OK, EXIT_USER_ERROR, EXIT_INTERNAL_ERROR, EXIT_SWEEP_TUPLE_FAILED}
    if mode is _FailureMode.REFUSED:
        assert code == EXIT_USER_ERROR
    elif mode is _FailureMode.RAISE:
        assert code == EXIT_INTERNAL_ERROR
    elif fail_link is NightlyLink.PREBUILD:
        # The PREBUILD extra outcome never carries an exit hint.
        assert code == EXIT_INTERNAL_ERROR
    else:
        assert code == (EXIT_SWEEP_TUPLE_FAILED if hint_six else EXIT_INTERNAL_ERROR)

    # Exactly ONE breaker exists iff the verdict is not OK.
    breakers = [o for o in outcomes if o.status in (LinkStatus.REFUSED, LinkStatus.FAILED)]
    assert len(breakers) == 1
    assert first_breaker(outcomes) is breakers[0]


@given(data=st.data())
def test_green_chain_has_no_breaker_and_exits_ok(data: st.DataObject) -> None:
    """The all-green arm: zero breakers ⇔ exit 0 (with SKIPPED sprinkled in)."""
    skip_backup = data.draw(st.booleans())
    calls: dict[NightlyLink, int] = dict.fromkeys(NightlyLink, 0)
    steps = _build_steps(None, _FailureMode.FAILED, calls, None)
    if skip_backup:
        steps[2] = ChainStep(
            link=NightlyLink.BACKUP,
            fn=lambda: LinkResult(status=LinkStatus.SKIPPED, detail="operator"),
        )
    outcomes, tripwires = run_chain(nightly_id="prop", steps=steps, now=lambda: 0.0)
    assert tripwires == ()
    assert first_breaker(outcomes) is None
    assert exit_code_for(outcomes, tripwires) == EXIT_OK
    statuses = {o.status for o in outcomes}
    assert statuses <= {LinkStatus.OK, LinkStatus.SKIPPED}
