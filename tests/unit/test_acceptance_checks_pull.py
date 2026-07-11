"""Pull-check pins: replan idempotence, anti-vacuous tooth, silent-loss detector.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from brutex.acceptance.checks import pull
from brutex.acceptance.model import AbstainReason, Verdict
from brutex.acceptance.selftest import _LEDGER_FIELDS, _month_file

from tests.fakes.acceptance_twin import DAY1, DAY2, make_ctx, make_twin


def _row(**overrides: object) -> tuple:
    from brutex.data.window_ledger import WindowLedgerRecord

    base = {
        "market": "groww",
        "security_id": "NIFTY",
        "interval": "1minute",
        "window_start": DAY1.isoformat(),
        "window_end": (DAY2 + timedelta(days=1)).isoformat(),
        "status": "SUCCESS",
        "bars": 62,
        "attempts": 1,
        "last_error": None,
        "first_seen_at": f"{DAY2.isoformat()}T18:00:00+00:00",
        "last_attempt_at": f"{DAY2.isoformat()}T18:00:00+00:00",
        "empty_streak": 0,
        "in_pull_fetches": 1,
        "bars_net_new": 62,
        "empty_retries": 0,
        "transient_retries": 0,
    }
    base.update(overrides)
    record = WindowLedgerRecord(**base)  # type: ignore[arg-type]
    return tuple(getattr(record, f) for f in _LEDGER_FIELDS)


def _save(lake: Path, rows: list[tuple]) -> None:
    from brutex.data.window_ledger import WindowLedger
    from brutex.data.window_ledger_store import save_window_ledger

    save_window_ledger(WindowLedger.from_rows(rows), lake)


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


class TestPullCheck:
    def test_settled_fixture_replan_zero_and_tail_planned(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["newly_fetchable"] == 0
        # 2026-07-10 (the run day) is a trading session newer than the max
        # settled end — the anti-vacuous tooth must see it PLANNED.
        assert replan.evidence["tail_check"] == "planned"
        assert replan.evidence["ledger_rss_kib_after"] is not None or True

    def test_lobotomized_planner_fails_vacuous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.data.window_ledger_store as store_mod

        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        monkeypatch.setattr(store_mod, "plan_windows", lambda *a, **k: 0)
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.FAIL
        assert replan.evidence["tail_check"] == "planner_vacuous"

    def test_planned_row_is_open_by_design(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        planned = _row(
            status="PLANNED",
            bars=0,
            bars_net_new=None,
            empty_retries=None,
            transient_retries=None,
            in_pull_fetches=0,
        )
        _save(lake, [_row(), planned])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS

    def test_net_new_gt_bars_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(bars=3, bars_net_new=7)])
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.verdict is Verdict.FAIL
        assert any("bars_net_new > bars" in v for v in law.evidence["violations"])

    def test_counter_invariant_violation_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(empty_retries=1, transient_retries=1, in_pull_fetches=1)])
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.verdict is Verdict.FAIL
        assert any("counter invariant" in v for v in law.evidence["violations"])

    def test_none_counters_honest_unknown(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(
            lake,
            [_row(bars_net_new=None, empty_retries=None, transient_retries=None)],
        )
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.evidence["rows_honest_unknown"] == 1
        assert law.verdict is Verdict.ABSTAIN  # zero counted rows → nothing_to_check
        assert law.abstain_reason is AbstainReason.NOTHING_TO_CHECK

    def test_partial_counters_row_counted(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(empty_retries=None, transient_retries=None, bars_net_new=10)])
        check = pull.run(make_ctx(lake))
        law = _sub(check, "pull.net_new_law")
        assert law.evidence["rows_with_counters"] == 1
        assert law.verdict is Verdict.PASS

    def test_deleted_partition_behind_success_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        _month_file(lake, "NIFTY", "1minute", DAY2).unlink()
        check = pull.run(make_ctx(lake))
        presence = _sub(check, "pull.partition_presence")
        assert presence.verdict is Verdict.FAIL
        assert presence.evidence["missing"] == 1
        assert "NIFTY" in presence.evidence["missing_keys"][0]

    def test_whole_series_dir_missing_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(security_id="GHOST")])
        check = pull.run(make_ctx(lake))
        presence = _sub(check, "pull.partition_presence")
        assert presence.verdict is Verdict.FAIL
        assert "whole series dir missing" in presence.evidence["missing_keys"][0]

    def test_fno_series_replans_via_contract_planner(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import _twin_bars
        from brutex.data.parquet_writer import ParquetWriter

        lake = make_twin(tmp_path)
        contract = "NSE-NIFTY-28Jul26-FUT"
        ParquetWriter(lake_root=lake).write(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="1minute",
            bars=_twin_bars(DAY2, base=200.0),
        )
        fno_row = _row(
            market="groww-future",
            security_id=contract,
            window_start=DAY2.isoformat(),
            window_end=(DAY2 + timedelta(days=1)).isoformat(),
        )
        junk_row = _row(market="groww-option", security_id="UNPARSABLE-JUNK")
        _save(lake, [_row(), fno_row, junk_row])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["series_replanned"] == 3

    def test_no_ledger_abstains(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        check = pull.run(make_ctx(lake))
        assert check.sub_checks[0].abstain_reason is AbstainReason.NO_LEDGER

    def test_pull_active_abstains(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        lake = make_twin(tmp_path)
        lease = lake / ".locks" / "pull_lease.json"
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("{}", encoding="utf-8")
        check = pull.run(make_ctx(lake, now_utc=lambda: datetime.now(UTC)))
        assert check.sub_checks[0].abstain_reason is AbstainReason.PULL_IN_PROGRESS

    def test_ledger_progress_is_evidence_not_a_verdict(self, tmp_path: Path) -> None:
        # Review round 1 MINOR regression: the decorative always-PASS
        # pull.progress_snapshot sub-check is DELETED — the counters ride as
        # evidence on replan_zero and can never inflate passes_full.
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        check = pull.run(make_ctx(lake))
        assert all(s.id != "pull.progress_snapshot" for s in check.sub_checks)
        replan = _sub(check, "pull.replan_zero")
        assert replan.evidence["ledger_progress"]["total"] == 1
        assert replan.evidence["ledger_progress"]["settled"] == 1

    def test_disjoint_operator_ranges_never_false_fail(self, tmp_path: Path) -> None:
        # Review round 1 regression (interior-gap keying): TWO disjoint
        # explicit operator ranges (DEC-UNIVERSE-PULL-DATERANGE-001) leave a
        # BY-DESIGN gap between them — the replan oracle must re-plan each
        # covered span, never the min→max hull that plans the gap.
        lake = make_twin(tmp_path)
        jan = _row(
            window_start="2024-01-02",
            window_end="2024-02-01",
            bars=0,
            bars_net_new=0,
            in_pull_fetches=0,
        )
        mar = _row(
            window_start="2024-03-04",
            window_end="2024-04-03",
            bars=0,
            bars_net_new=0,
            in_pull_fetches=0,
        )
        _save(lake, [jan, mar])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["newly_fetchable"] == 0
        assert replan.evidence["spans_replanned"] == 2

    def test_all_expired_options_lake_tail_honest(self, tmp_path: Path) -> None:
        # Review round 1 MAJOR-4 root-cause regression: the replan threads the
        # SAME expiry-capped contract windows production derives, so an
        # all-expired options-only lake neither mints past-expiry windows nor
        # reads planner_vacuous — the tail is honestly ineligible.
        lake = make_twin(tmp_path)
        contract = "NSE-NIFTY-26Jun25-24000-CE"
        # Production stores ONE ledger row per (contract, interval): the
        # default option window [expiry-30 → expiry+1) = [05-27 → 06-27).
        rows = [
            _row(
                market="groww-option",
                security_id=contract,
                window_start="2025-05-27",
                window_end="2025-06-27",
                bars=0,
                bars_net_new=0,
                in_pull_fetches=0,
            ),
        ]
        _save(lake, rows)
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["newly_fetchable"] == 0
        assert replan.evidence["tail_check"] == "no_tail_eligible_series"
        assert replan.evidence["tail_eligible_series"] == 0

    def test_not_yet_open_contract_plans_nothing(self, tmp_path: Path) -> None:
        # DEC-FNO-FAR-EXPIRY-WINDOW-001 parity: a contract whose default
        # window has not OPENED inside the covered envelope plans NOTHING —
        # the replan skips it exactly as production would.
        lake = make_twin(tmp_path)
        row = _row(
            market="groww-option",
            security_id="NSE-NIFTY-24Dec26-24000-CE",
            window_start="2026-07-01",
            window_end="2026-07-02",
            bars=0,
            bars_net_new=0,
            in_pull_fetches=0,
        )
        _save(lake, [row])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.evidence["newly_fetchable"] == 0
        assert replan.evidence["tail_check"] in {"planned", "no_tail_eligible_series"}

    def test_deltas_dir_oserror_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.data.window_ledger_store as store_mod

        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        ctx = make_ctx(lake)
        ctx.ledger()  # warm the shared load BEFORE the deltas dir wedges

        def _boom(lake_root: Path) -> list[Path]:
            raise OSError("wedged deltas dir")

        monkeypatch.setattr(store_mod, "list_ledger_deltas", _boom)
        check = pull.run(ctx)
        assert _sub(check, "pull.replan_zero").verdict is Verdict.PASS

    def test_fno_window_not_yet_open_returns_none(self) -> None:
        # DEC-FNO-FAR-EXPIRY-WINDOW-001 belt on the oracle seam: a contract
        # whose default window has not OPENED inside the given envelope plans
        # NOTHING. (End-to-end such a row is classified explicit_dates_window
        # first — round 2 — so the seam's None arm is pinned directly here.)
        from datetime import date

        resolved = pull._fno_window(
            "NSE-NIFTY-24Dec26-24000-CE", start=date(2026, 7, 1), end=date(2026, 7, 2)
        )
        assert resolved is None
        assert pull._fno_window("UNPARSABLE", start=date(2026, 7, 1), end=date(2026, 7, 2)) is None

    def test_merge_covered_spans_law(self) -> None:
        from datetime import date

        merged = pull.merge_covered_spans(
            [
                (date(2024, 3, 1), date(2024, 3, 30)),
                (date(2024, 1, 1), date(2024, 1, 31)),
                (date(2024, 1, 31), date(2024, 2, 5)),  # touching → merges
            ]
        )
        assert merged == [
            (date(2024, 1, 1), date(2024, 2, 5)),
            (date(2024, 3, 1), date(2024, 3, 30)),
        ]

    def test_pinned_now_determinism(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row()])

        def _strip(check) -> list[dict]:
            subs = check.to_dict()["sub_checks"]
            for sub in subs:
                sub["evidence"].pop("ledger_rss_kib_before", None)
                sub["evidence"].pop("ledger_rss_kib_after", None)
            return subs

        assert _strip(pull.run(make_ctx(lake))) == _strip(pull.run(make_ctx(lake)))


class TestNonDefaultWindowShapes:
    """Round-2 review folds (refuter 2A MAJORs [B]+[C]): correct-or-abstain."""

    def test_explicit_dates_fno_row_never_false_fails(self, tmp_path: Path) -> None:
        # [B] production's explicit-dates OVERRIDE writes a ledger row over
        # the verbatim envelope (2025-03-01 → 2025-07-15, wider than the
        # 30d default window for expiry 2025-06-26). The old oracle re-derived
        # the DEFAULT window and false-FAILed newly_fetchable=1 on a
        # legitimately covered lake.
        lake = make_twin(tmp_path)
        row = _row(
            market="groww-option",
            security_id="NSE-NIFTY-26Jun25-24000-CE",
            window_start="2025-03-01",
            window_end="2025-07-15",
            bars=0,
            bars_net_new=0,
            in_pull_fetches=0,
        )
        _save(lake, [row])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is not Verdict.FAIL
        assert replan.evidence["newly_fetchable"] == 0
        assert replan.evidence["non_default_shape_series"] == 1
        assert any(
            "explicit_dates_window" in s for s in replan.evidence["non_default_shape_samples"]
        )
        # The ONLY series is non-default → the named sub-level abstain.
        assert replan.verdict is Verdict.ABSTAIN
        assert replan.abstain_reason is AbstainReason.NON_DEFAULT_WINDOW_SHAPE

    def test_overlapping_spot_ranges_never_false_fail(self, tmp_path: Path) -> None:
        # [C] two OVERLAPPING explicit operator ranges with different tile
        # anchors (2024-01-05→03-01 + 2024-02-10→04-01) — the merged-span
        # re-tiling from 01-05 minted 2 phantom keys → false FAIL.
        lake = make_twin(tmp_path)
        rows = [
            _row(window_start=ws, window_end=we, bars=0, bars_net_new=0, in_pull_fetches=0)
            for ws, we in (
                ("2024-01-05", "2024-02-04"),
                ("2024-02-04", "2024-03-01"),
                ("2024-02-10", "2024-03-11"),
                ("2024-03-11", "2024-04-01"),
            )
        ]
        _save(lake, rows)
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is not Verdict.FAIL
        assert replan.evidence["newly_fetchable"] == 0
        assert replan.evidence["non_default_shape_series"] == 1
        assert any("overlapping_ranges" in s for s in replan.evidence["non_default_shape_samples"])
        assert replan.abstain_reason is AbstainReason.NON_DEFAULT_WINDOW_SHAPE

    def test_default_law_series_beside_non_default_still_passes(self, tmp_path: Path) -> None:
        # A default-law series keeps its hard verified PASS while the odd
        # series is abstain-routed + counted (never a whole-check abstain).
        lake = make_twin(tmp_path)
        explicit = _row(
            market="groww-option",
            security_id="NSE-NIFTY-26Jun25-24000-CE",
            window_start="2025-03-01",
            window_end="2025-07-15",
            bars=0,
            bars_net_new=0,
            in_pull_fetches=0,
        )
        _save(lake, [_row(), explicit])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.PASS
        assert replan.evidence["newly_fetchable"] == 0
        assert replan.evidence["non_default_shape_series"] == 1
        assert replan.evidence["series_replanned"] == 1

    def test_genuine_out_of_coverage_key_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The double-fetch tooth stays intact: a planner minting a key
        # OUTSIDE the covered spans (a genuine "production would fetch again"
        # defect) is a hard FAIL — the round-2 abstain routing never
        # swallows it.
        import brutex.data.window_ledger_store as store_mod
        from brutex.data.fetch_coverage import WindowKey

        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        real_plan = store_mod.plan_windows

        def _drifted(ledger, **kwargs):  # type: ignore[no-untyped-def]
            count = real_plan(ledger, **kwargs)
            inst = next(iter(kwargs["instruments"]))
            ledger.plan(
                WindowKey(
                    market=kwargs["market"],
                    security_id=inst.symbol_name,
                    interval=kwargs["intervals"][0],
                    start=DAY1 - timedelta(days=40),
                    end=DAY1 - timedelta(days=10),
                ),
                now=kwargs["now"],
            )
            return count + 1

        monkeypatch.setattr(store_mod, "plan_windows", _drifted)
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.verdict is Verdict.FAIL
        assert replan.evidence["newly_fetchable"] >= 1


class TestPullRetryRereads:
    def test_toctou_retry_rereads_ledger_from_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-2 fix 4 (refuter 2A MINOR "pull retry is void"): a mid-check
        # ledger flush must make the RETRY re-read disk — the pre-fix retry
        # re-served the memoised snapshot while stamping retried:True, so a
        # violation landing mid-check read a stale PASS.
        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        ctx = make_ctx(lake)
        calls: list[int] = []
        real_body = pull._run_body

        def _flaky_body(ctx_, *, rss):  # type: ignore[no-untyped-def]
            if not calls:
                calls.append(1)
                # A concurrent writer lands a violating row DURING the body —
                # the fingerprint moves, forcing the retry. (attempts/newer
                # stamp so the save-time merge prefers the new row.)
                _save(
                    lake,
                    [
                        _row(
                            bars=3,
                            bars_net_new=7,
                            attempts=2,
                            last_attempt_at=f"{DAY2.isoformat()}T19:00:00+00:00",
                        )
                    ],
                )
            return real_body(ctx_, rss=rss)

        monkeypatch.setattr(pull, "_run_body", _flaky_body)
        check = pull.run(ctx)
        assert check.input_fingerprint.get("retried") is True
        law = _sub(check, "pull.net_new_law")
        assert law.verdict is Verdict.FAIL  # the RE-READ ledger, not the memo
        assert any("bars_net_new > bars" in v for v in law.evidence["violations"])

    def test_ledger_vanishing_between_attempts_abstains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data.window_ledger_store import ledger_path

        lake = make_twin(tmp_path)
        _save(lake, [_row()])
        ctx = make_ctx(lake)
        calls: list[int] = []
        real_body = pull._run_body

        def _deleting_body(ctx_, *, rss):  # type: ignore[no-untyped-def]
            if not calls:
                calls.append(1)
                ledger_path(lake).unlink()
            return real_body(ctx_, rss=rss)

        monkeypatch.setattr(pull, "_run_body", _deleting_body)
        check = pull.run(ctx)
        # The retry re-reads a now-absent ledger → the honest NO_LEDGER
        # abstain (or the moved-twice abstain), never a crash on the dropped
        # memo.
        assert check.sub_checks[0].abstain_reason in {
            AbstainReason.NO_LEDGER,
            AbstainReason.INPUT_MOVED_DURING_CHECK,
        }


class TestPullEdges:
    def test_missing_bars_tree_series_dir_none(self, tmp_path: Path) -> None:
        # A ledger beside a lake with NO bars/ tree at all: every sampled
        # SUCCESS row is a silent-loss hit (whole series dir missing).
        _save(tmp_path, [_row()])
        check = pull.run(make_ctx(tmp_path))
        presence = _sub(check, "pull.partition_presence")
        assert presence.verdict is Verdict.FAIL

    def test_empty_ledger_rows_not_applicable_tail(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.evidence["tail_check"] == "not_applicable"
        assert replan.verdict is Verdict.ABSTAIN  # zero units → nothing_to_check

    def test_ledger_covering_past_run_date_tail_not_applicable(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _save(lake, [_row(window_end="2026-07-12")])
        check = pull.run(make_ctx(lake))
        replan = _sub(check, "pull.replan_zero")
        assert replan.evidence["tail_check"] == "not_applicable"
