"""Tests for `brutex.dashboard.lake_revision` (DEC-HARDENING-LEFTOVERS-001 +
the sweeps-tree component of DEC-RESULTS-SURFACE-HONESTY-001).

100% line + branch on the cross-process lake freshness token:

* a lake with NO window ledger, NO pending ledger deltas AND no sweeps tree
  yields the stable composite ``"absent+absent|absent"`` (and it is stable
  across repeat probes — no oscillation);
* the ledger component ALSO folds the ``window_ledger/deltas/`` directory stat
  (DEC-LEDGER-DELTA-FLUSH-001) so a mid-pull DELTA append — which no longer
  touches ``ledger.parquet`` — still moves the cross-process token;
* a present ledger contributes the ``"<mtime_ns>:<size>"`` stat component,
  which CHANGES after a writer touches the ledger (the CLI-checkpoint signal);
* the NEW sweeps component (:func:`sweeps_revision`) changes when a results
  store lands in an EXISTING ``<sweeps>/<instrument>/<tf>/`` directory — the
  out-of-band CLI ``sweep-all`` write the #1069 ledger-only probe was blind to;
* the Greeks-backfill marker component (DEC-GREEKS-BULK-REENRICH-001 fix
  round 2) changes when a wet backfill touches
  ``.greeks_backfill/revision.json`` — the out-of-band CLI ``greeks-backfill``
  write the two-component probe was blind to (its literals are drift-pinned
  against ``brutex.data.greeks_backfill``'s constants);
* any non-missing-file :class:`OSError` (permissions, mid-scan fault) maps to
  ``None`` — "unknown, no signal" — never a raise, never a spurious
  invalidation, for ANY component;
* EXCEPT ``ENOTDIR`` (fix round 3): a stray regular FILE squatting where a
  sidecar directory belongs (the tolerated advisory-corruption state at
  ``.greeks_backfill``) reads the stable ``absent`` sentinel — the marker
  genuinely does not exist — so one stray file can never permanently mute the
  ENTIRE composite's cross-process invalidation.
"""

# The drift-pin tests read module-private constants deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import os
from pathlib import Path

import pytest
from brutex.dashboard.lake_revision import (
    ABSENT_TOKEN,
    lake_revision,
    rate_bridge_revision,
    sweep_results_revision,
    sweeps_revision,
)
from brutex.data.rate_bridge import rate_bridge_path
from brutex.data.window_ledger_store import deltas_dir, ledger_path
from structlog.testing import capture_logs


def _write_ledger_bytes(lake_root: Path, payload: bytes) -> Path:
    """Write raw *payload* at the COMMON ledger path (a stat-only stand-in)."""
    target = ledger_path(lake_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def _write_store_file(lake_root: Path, instrument: str, tf: str, name: str) -> Path:
    """Land a store file the way every repo writer does: tempfile + ``os.replace``.

    The parent-directory mtime move caused by the ``os.replace`` rename is the
    exact signal :func:`sweeps_revision` keys on.
    """
    dest_dir = lake_root / "sweeps" / instrument / tf
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / (name + ".tmp")
    tmp.write_bytes(b"parquet-stand-in")
    dest = dest_dir / name
    os.replace(tmp, dest)
    return dest


def _bump_dir_mtime(path: Path, *, ns: int) -> None:
    """Force a DISTINCT directory mtime (deterministic — no timer-granularity flake)."""
    os.utime(path, ns=(ns, ns))


class TestLedgerComponent:
    """The window-ledger half of the composite (the #1069 behaviour, preserved)."""

    def test_absent_everything_returns_stable_composite_sentinel(self, tmp_path: Path) -> None:
        """No ledger + no deltas + no sweeps tree + no marker → the stable all-absent composite."""
        all_absent = f"{ABSENT_TOKEN}+{ABSENT_TOKEN}|{ABSENT_TOKEN}|{ABSENT_TOKEN}"
        assert lake_revision(tmp_path) == all_absent
        # Stable: a second probe of the same absent lake returns the SAME token
        # (the sentinel never oscillates a generation).
        assert lake_revision(tmp_path) == all_absent

    def test_present_ledger_component_matches_stat(self, tmp_path: Path) -> None:
        """A present ledger contributes the exact ``"<mtime_ns>:<size>"`` component."""
        target = _write_ledger_bytes(tmp_path, b"v1")
        st = os.stat(target)
        assert lake_revision(tmp_path) == (
            f"{st.st_mtime_ns}:{st.st_size}+{ABSENT_TOKEN}|{ABSENT_TOKEN}|{ABSENT_TOKEN}"
        )

    def test_token_changes_after_a_ledger_write(self, tmp_path: Path) -> None:
        """A writer touching the ledger (the CLI checkpoint) moves the token."""
        _write_ledger_bytes(tmp_path, b"v1")
        first = lake_revision(tmp_path)
        # A second checkpoint with different content — size (and mtime) move.
        _write_ledger_bytes(tmp_path, b"v2-longer")
        second = lake_revision(tmp_path)
        assert first is not None and second is not None
        assert second != first
        # And the absent→present transition also moved the token.
        assert first != f"{ABSENT_TOKEN}+{ABSENT_TOKEN}|{ABSENT_TOKEN}|{ABSENT_TOKEN}"

    def test_unreadable_ledger_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError (permissions) → ``None`` ("no signal")."""
        target = _write_ledger_bytes(tmp_path, b"v1")
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path) == str(target):
                raise PermissionError(13, "denied", str(target))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        assert lake_revision(tmp_path) is None


class TestSweepsComponent:
    """The sweeps-tree half (DEC-RESULTS-SURFACE-HONESTY-001) — the CLI ``sweep-all`` signal."""

    def test_absent_sweeps_tree_returns_stable_sentinel(self, tmp_path: Path) -> None:
        """No ``<lake>/sweeps`` directory → the stable ``absent`` sentinel."""
        assert sweeps_revision(tmp_path) == ABSENT_TOKEN
        assert sweeps_revision(tmp_path) == ABSENT_TOKEN

    def test_store_write_into_existing_tf_dir_moves_the_token(self, tmp_path: Path) -> None:
        """The exact #1069 blind spot: a store landing in an EXISTING ``<tf>/`` dir.

        A root-only ``os.stat`` cannot see it (directory mtimes move only for
        DIRECT children) — the two-level dir digest must.
        """
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k1_" + "a" * 16 + ".parquet")
        tf_dir = tmp_path / "sweeps" / "NIFTY" / "5m"
        _bump_dir_mtime(tf_dir, ns=1_000_000_000)
        first = sweeps_revision(tmp_path)
        assert first is not None and first != ABSENT_TOKEN

        # The out-of-band "CLI sweep-all" writes a SECOND store into the SAME
        # tf directory; the dir mtime is pinned to a distinct value so the test
        # is deterministic on coarse filesystem timestamp granularity.
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k2_" + "b" * 16 + ".parquet")
        _bump_dir_mtime(tf_dir, ns=2_000_000_000)
        second = sweeps_revision(tmp_path)
        assert second is not None
        assert second != first

    def test_new_instrument_dir_moves_the_token(self, tmp_path: Path) -> None:
        """A first store for a NEW instrument (new depth-1 dir) moves the token."""
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k1_" + "a" * 16 + ".parquet")
        first = sweeps_revision(tmp_path)
        _write_store_file(tmp_path, "BANKNIFTY", "15m", "bear_k1_" + "c" * 16 + ".parquet")
        second = sweeps_revision(tmp_path)
        assert first is not None and second is not None
        assert second != first

    def test_non_directory_entries_are_ignored(self, tmp_path: Path) -> None:
        """A stray FILE at depth 1 (e.g. a cascade JSON) does not enter the digest."""
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k1_" + "a" * 16 + ".parquet")
        root_mtime_ns = os.stat(tmp_path / "sweeps").st_mtime_ns
        first = sweeps_revision(tmp_path)
        stray = tmp_path / "sweeps" / "stray.json"
        stray.write_text("{}")
        # Only DIRECTORY entries are digested; creating the file moved the root
        # dir's own mtime though — pin it back so the assertion isolates the
        # is_dir filter (the root mtime IS deliberately part of the digest).
        _bump_dir_mtime(tmp_path / "sweeps", ns=root_mtime_ns)
        second = sweeps_revision(tmp_path)
        assert second == first

    def test_empty_sweeps_root_yields_a_token(self, tmp_path: Path) -> None:
        """An empty ``sweeps/`` dir (no stores yet) yields a real, stable token."""
        (tmp_path / "sweeps").mkdir()
        first = sweeps_revision(tmp_path)
        assert first is not None and first != ABSENT_TOKEN
        assert sweeps_revision(tmp_path) == first

    def test_unreadable_sweeps_root_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError on the root stat → ``None`` ("no signal")."""
        (tmp_path / "sweeps").mkdir()
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path) == str(tmp_path / "sweeps"):
                raise PermissionError(13, "denied", str(path))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        assert sweeps_revision(tmp_path) is None

    def test_scan_fault_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An OSError DURING the dir scan (dir vanished mid-walk) → ``None``."""
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k1_" + "a" * 16 + ".parquet")

        def _raising_scandir(path: object) -> object:
            raise OSError(5, "I/O error", str(path))

        monkeypatch.setattr(os, "scandir", _raising_scandir)
        assert sweeps_revision(tmp_path) is None


class TestComposite:
    """The composite ``ledger|sweeps`` join semantics."""

    def test_sweep_write_moves_the_composite_without_a_ledger_touch(self, tmp_path: Path) -> None:
        """The defect under test: an out-of-band sweep write MUST bump the token."""
        _write_ledger_bytes(tmp_path, b"v1")
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k1_" + "a" * 16 + ".parquet")
        tf_dir = tmp_path / "sweeps" / "NIFTY" / "5m"
        _bump_dir_mtime(tf_dir, ns=1_000_000_000)
        first = lake_revision(tmp_path)
        # A CLI sweep-all writes a store; the ledger is NOT touched.
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k2_" + "b" * 16 + ".parquet")
        _bump_dir_mtime(tf_dir, ns=2_000_000_000)
        second = lake_revision(tmp_path)
        assert first is not None and second is not None
        assert second != first
        # The ledger component itself did not move — only the sweeps half did.
        assert first.split("|", 1)[0] == second.split("|", 1)[0]

    def test_sweeps_probe_failure_maps_the_composite_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ledger fine + sweeps probe faulting → ``None`` (no half-signal token)."""
        _write_ledger_bytes(tmp_path, b"v1")
        (tmp_path / "sweeps").mkdir()

        def _raising_scandir(path: object) -> object:
            raise OSError(5, "I/O error", str(path))

        monkeypatch.setattr(os, "scandir", _raising_scandir)
        assert lake_revision(tmp_path) is None

    def test_sweeps_dirname_pinned_to_the_results_view_constant(self) -> None:
        """The not-imported dirname copy cannot drift from the ONE definition.

        ``lake_revision`` deliberately does NOT import ``sweep_results_view``
        (that module pulls the engine/Rust-wheel import graph; the probe must
        stay stdlib-lean) — this pin is the drift guard, the
        ``trial_stats._TUPLE_DIGEST_LEN`` precedent.
        """
        pytest.importorskip("polars", reason="sweep_results_view imports the engine store")
        from brutex.dashboard import lake_revision as probe_mod
        from brutex.dashboard.sweep_results_view import SWEEPS_DIRNAME

        assert probe_mod._SWEEPS_DIRNAME == SWEEPS_DIRNAME


class TestLedgerDeltaComponent:
    """The deltas-dir fold of the ledger component (DEC-LEDGER-DELTA-FLUSH-001)."""

    def test_delta_append_moves_the_token_without_a_base_touch(self, tmp_path: Path) -> None:
        """A mid-pull DELTA append (base untouched) must move the composite.

        Regression for the freshness freeze: the flush no longer rewrites
        ``ledger.parquet``, so a base-only stat would serve pre-pull caches for
        the whole pull.
        """
        _write_ledger_bytes(tmp_path, b"v1")
        first = lake_revision(tmp_path)
        d = deltas_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / "00000000000000000001-1.parquet").write_bytes(b"delta")
        _bump_dir_mtime(d, ns=3_000_000_000)  # deterministic distinct dir mtime
        second = lake_revision(tmp_path)
        assert first is not None and second is not None
        assert second != first

    def test_delta_prune_moves_the_token_again(self, tmp_path: Path) -> None:
        """The pull-end subsume-prune (delta deleted) also moves the token."""
        _write_ledger_bytes(tmp_path, b"v1")
        d = deltas_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        delta = d / "00000000000000000001-1.parquet"
        delta.write_bytes(b"delta")
        _bump_dir_mtime(d, ns=4_000_000_000)
        with_delta = lake_revision(tmp_path)
        delta.unlink()
        _bump_dir_mtime(d, ns=5_000_000_000)
        after_prune = lake_revision(tmp_path)
        assert with_delta is not None and after_prune is not None
        assert after_prune != with_delta

    def test_unreadable_deltas_dir_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError on the deltas dir → ``None`` ("no signal")."""
        _write_ledger_bytes(tmp_path, b"v1")
        target = deltas_dir(tmp_path)
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path) == str(target):
                raise PermissionError(13, "denied", str(target))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        assert lake_revision(tmp_path) is None


class TestSweepResultsRevisionNoSignal:
    """``sweep_results_revision``'s two ``None`` ("no signal") branches.

    Standalone twins of ``TestSweepResultsRevisionToken`` in
    ``test_dashboard_results_revision_key.py`` (the discovery-gate contract:
    this module's stem-matched suite must prove them on its own).
    """

    def test_happy_composite_joins_dirs_and_chain(self, tmp_path: Path) -> None:
        """The happy path: ``<dirs digest>|<chain stat>`` — and an audit append moves it.

        The standalone twin of the token behaviour proven in
        ``test_dashboard_results_revision_key.py`` (this module's stem-matched
        suite must reach the composite return on its own — the discovery-gate
        contract; the line was previously covered only by the sibling file).
        """
        _write_store_file(tmp_path, "NIFTY", "5m", "bull_k1_" + "a" * 16 + ".parquet")
        chain = tmp_path / "sweeps" / "audit" / "sweep-all.jsonl"
        chain.parent.mkdir(parents=True, exist_ok=True)
        chain.write_bytes(b'{"action":"SWEEP_START"}\n')
        first = sweep_results_revision(tmp_path)
        assert first is not None and "|" in first
        dirs = sweeps_revision(tmp_path)
        st = os.stat(chain)
        assert first == f"{dirs}|{st.st_mtime_ns}:{st.st_size}"
        # An audit APPEND (no directory mtime moves) still moves the composite.
        with chain.open("ab") as handle:
            handle.write(b'{"action":"SWEEP_START"}\n')
        second = sweep_results_revision(tmp_path)
        assert second is not None and second != first

    def test_dirs_probe_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing OSError on the sweeps root → ``None`` — never a token."""
        (tmp_path / "sweeps").mkdir()
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path) == str(tmp_path / "sweeps"):
                raise PermissionError(13, "denied", str(path))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        assert sweep_results_revision(tmp_path) is None

    def test_chain_probe_failure_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dirs digest OK but a non-missing OSError on the audit-chain stat → ``None``."""
        (tmp_path / "sweeps").mkdir()
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path).endswith("sweep-all.jsonl"):
                raise PermissionError(13, "denied", str(path))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        assert sweep_results_revision(tmp_path) is None


class TestRateBridgeRevision:
    """The CLI rate-bridge file's stat token (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001)."""

    def test_present_bridge_file_matches_stat_with_inode(self, tmp_path: Path) -> None:
        """A present bridge file tokens as the exact ``mtime_ns:size:st_ino`` triple."""
        target = rate_bridge_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"schema":3}')
        st = os.stat(target)
        assert rate_bridge_revision(tmp_path) == f"{st.st_mtime_ns}:{st.st_size}:{st.st_ino}"

    def test_absent_bridge_file_returns_stable_sentinel(self, tmp_path: Path) -> None:
        """No bridge file (no CLI pull has run) → the stable ``absent`` sentinel."""
        assert rate_bridge_revision(tmp_path) == ABSENT_TOKEN
        assert rate_bridge_revision(tmp_path) == ABSENT_TOKEN

    def test_atomic_replace_moves_the_token_even_at_equal_size(self, tmp_path: Path) -> None:
        """The inode fold: an ``os.replace`` publish of EQUAL-SIZE content with a
        pinned-equal mtime still moves the token (the coarse-mtime-filesystem case)."""
        target = rate_bridge_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"schema":3,"n":1}')
        pinned_ns = 1_000_000_000
        os.utime(target, ns=(pinned_ns, pinned_ns))
        first = rate_bridge_revision(tmp_path)
        # An atomic publish: same byte COUNT, same pinned mtime — new inode.
        tmp = target.parent / "pull_rate.json.tmp"
        tmp.write_bytes(b'{"schema":3,"n":2}')
        os.replace(tmp, target)
        os.utime(target, ns=(pinned_ns, pinned_ns))
        second = rate_bridge_revision(tmp_path)
        assert first is not None and second is not None
        assert second != first

    def test_unreadable_bridge_stat_returns_none_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError → structlog WARN + ``None`` ("no signal")."""
        target = rate_bridge_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{"schema":3}')
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path) == str(target):
                raise PermissionError(13, "denied", str(target))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        with capture_logs() as logs:
            assert rate_bridge_revision(tmp_path) is None
        assert any(entry["event"] == "lake_revision.rate_bridge_stat_failed" for entry in logs)

    def test_default_stat_tokens_stay_ino_free_regression(self, tmp_path: Path) -> None:
        """``include_ino`` defaults False: the ledger component token format is
        byte-unchanged (two colon-separated fields, no inode) — the pre-change pin."""
        target = _write_ledger_bytes(tmp_path, b"v1")
        st = os.stat(target)
        token = lake_revision(tmp_path)
        assert token is not None
        ledger_component = token.split("+", 1)[0]
        assert ledger_component == f"{st.st_mtime_ns}:{st.st_size}"


def _write_greeks_marker(lake_root: Path, payload: bytes) -> Path:
    """Write raw *payload* at the backfill freshness-marker path (stat-only stand-in)."""
    target = lake_root / ".greeks_backfill" / "revision.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


class TestGreeksBackfillComponent:
    """The Greeks-backfill marker component (DEC-GREEKS-BULK-REENRICH-001 fix round 2).

    Refuter-confirmed: the bulk backfill rewrites option partitions WITHOUT
    touching the window ledger, so a CLI ``brutex greeks-backfill`` never moved
    the two-component token — a running dashboard's ``/movers`` kept serving
    the pre-backfill iv/delta the backfill was run to fix.
    """

    def test_marker_write_moves_the_composite_without_a_ledger_touch(self, tmp_path: Path) -> None:
        """The defect under test: a CLI backfill's marker touch MUST bump the token."""
        _write_ledger_bytes(tmp_path, b"v1")
        first = lake_revision(tmp_path)
        # A CLI greeks-backfill batch writes bars and touches the marker; the
        # ledger and the sweeps tree are NOT touched.
        _write_greeks_marker(tmp_path, b'{"backfill_id": "x", "bars_written": 3}')
        second = lake_revision(tmp_path)
        assert first is not None and second is not None
        assert second != first
        # Only the marker component moved — ledger + sweeps halves unchanged.
        assert first.rsplit("|", 1)[0] == second.rsplit("|", 1)[0]
        # A second batch's rewrite (different size) moves it again.
        _write_greeks_marker(tmp_path, b'{"backfill_id": "x", "bars_written": 3000}')
        third = lake_revision(tmp_path)
        assert third is not None and third != second

    def test_stray_file_at_the_sidecar_dir_reads_absent_never_mutes_the_composite(
        self, tmp_path: Path
    ) -> None:
        """A stray regular FILE at ``.greeks_backfill`` must NOT mute the probe.

        Refuter-confirmed (DEC-GREEKS-BULK-REENRICH-001 fix round 3): the
        marker stat raises ``NotADirectoryError`` (ENOTDIR — NOT a
        ``FileNotFoundError``), which mapped to ``None`` → ``lake_revision``
        returned ``None`` on EVERY poll → ``SummaryCache`` made no freshness
        decision — cross-process invalidation for pulls AND sweeps (which
        worked on main, where this path was never probed) silently stopped
        until manual cleanup, even though the branch's own advisory contract
        TOLERATES exactly this stray file (the checkpoint store + the marker
        touch both log + continue on it). The marker genuinely does not exist
        under a non-directory path, so the stable ``absent`` sentinel is the
        semantically-correct token and the OTHER components keep invalidating.
        """
        _write_ledger_bytes(tmp_path, b"v1")
        clean = lake_revision(tmp_path)
        assert clean is not None
        (tmp_path / ".greeks_backfill").write_text("stray", encoding="utf-8")
        with_stray = lake_revision(tmp_path)
        assert with_stray is not None  # NOT muted (returned None before the fix)
        # The greeks half reads the stable "absent" sentinel — a marker under a
        # non-directory ancestor genuinely does not exist …
        assert with_stray.rsplit("|", 1)[1] == ABSENT_TOKEN
        assert with_stray == clean  # … and never oscillates vs the clean lake.
        # The ledger component STILL invalidates: a pull's checkpoint moves the
        # composite despite the stray file (the muted-probe regression).
        _write_ledger_bytes(tmp_path, b"v2-longer")
        after_pull = lake_revision(tmp_path)
        assert after_pull is not None
        assert after_pull != with_stray

    def test_unreadable_marker_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError on the marker stat → ``None`` ("no signal")."""
        _write_ledger_bytes(tmp_path, b"v1")
        target = _write_greeks_marker(tmp_path, b"{}")
        real_stat = os.stat

        def _denying_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path) == str(target):
                raise PermissionError(13, "denied", str(target))
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]  # passthrough shim

        monkeypatch.setattr(os, "stat", _denying_stat)
        assert lake_revision(tmp_path) is None

    def test_marker_literals_pinned_to_the_backfill_constants(self) -> None:
        """The not-imported dir/file literals cannot drift from the ONE definition.

        ``lake_revision`` deliberately does NOT import ``greeks_backfill``
        (that module pulls the greeks-enrich polars/trading_core-wheel import
        graph; the probe must stay stdlib-lean) — this pin is the drift guard,
        the ``_SWEEPS_DIRNAME`` precedent. Since fix round 8 the canonical
        home is the wheel-free ``greeks_revision`` (shared with the
        DEC-CLI-GREEKS-ENRICH-001 enrich pass); ``greeks_backfill`` re-exports
        it — BOTH are pinned so neither surface can drift.
        """
        from brutex.dashboard import lake_revision as probe_mod
        from brutex.data.greeks_revision import (
            GREEKS_SIDECAR_DIR_NAME,
        )
        from brutex.data.greeks_revision import (
            REVISION_MARKER_FILE_NAME as CANONICAL_MARKER_FILE_NAME,
        )

        assert probe_mod._GREEKS_BACKFILL_DIRNAME == GREEKS_SIDECAR_DIR_NAME
        assert probe_mod._GREEKS_REVISION_FILENAME == CANONICAL_MARKER_FILE_NAME
        pytest.importorskip("trading_core", reason="greeks_backfill needs the wheel")
        pytest.importorskip("polars", reason="greeks_backfill imports the enrich driver")
        from brutex.data.greeks_backfill import CHECKPOINT_DIR_NAME, REVISION_MARKER_FILE_NAME

        assert probe_mod._GREEKS_BACKFILL_DIRNAME == CHECKPOINT_DIR_NAME
        assert probe_mod._GREEKS_REVISION_FILENAME == REVISION_MARKER_FILE_NAME


class TestCascadeReportRevision:
    """DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the dedicated report stat token."""

    def test_absent_reads_the_stable_sentinel(self, tmp_path: Path) -> None:
        from brutex.dashboard.lake_revision import ABSENT_TOKEN, cascade_report_revision

        assert cascade_report_revision(tmp_path) == ABSENT_TOKEN

    def test_token_moves_on_an_in_place_rewrite(self, tmp_path: Path) -> None:
        from brutex.dashboard.lake_revision import cascade_report_revision

        report = tmp_path / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b'{"version":1}')
        first = cascade_report_revision(tmp_path)
        assert first is not None and ":" in first
        # An atomic in-place rewrite moves NO directory mtime the sweeps
        # digest sees — the FILE stat token must move on its own.
        os.utime(report, ns=(1, 1))
        second = cascade_report_revision(tmp_path)
        assert second is not None
        assert second != first

    def test_probe_outage_reads_none_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard import lake_revision as probe_mod

        def _boom(path: object) -> object:
            raise PermissionError("wedged")

        monkeypatch.setattr(probe_mod.os, "stat", _boom)
        with capture_logs() as logs:
            assert probe_mod.cascade_report_revision(tmp_path) is None
        assert [e for e in logs if e["event"] == "lake_revision.cascade_report_stat_failed"]

    def test_relpath_pinned_to_the_wire_path_law(self, tmp_path: Path) -> None:
        """The mirrored relpath literal tracks cascade_wire.report_path —
        pinned by test, never imported (lake_revision stays stdlib-lean)."""
        from brutex.dashboard import lake_revision as probe_mod
        from brutex.engine.cascade_wire import report_path

        pinned = tmp_path.joinpath("sweeps", *probe_mod._CASCADE_REPORT_RELPATH)
        assert pinned == report_path(tmp_path / "sweeps")


class TestRegistryRevision:
    """`registry_revision` — the H1 registry-content stat token (DEC-O1-GUARANTEE-HARDEN-001)."""

    def test_absent_registry_reads_the_stable_absent_composite(self, tmp_path: Path) -> None:
        """No registry.duckdb + no .wal → the stable ``absent|absent`` token."""
        from brutex.dashboard.lake_revision import registry_revision

        token = registry_revision(tmp_path / "registry.duckdb")
        assert token == f"{ABSENT_TOKEN}|{ABSENT_TOKEN}"
        # Stable across repeat probes — never oscillates.
        assert registry_revision(tmp_path / "registry.duckdb") == token

    def test_main_file_write_moves_the_token(self, tmp_path: Path) -> None:
        """Creating / rewriting the main file moves the token exactly once each."""
        from brutex.dashboard.lake_revision import registry_revision

        path = tmp_path / "registry.duckdb"
        absent = registry_revision(path)
        path.write_bytes(b"v1")
        created = registry_revision(path)
        assert created != absent
        os.utime(path, ns=(1, 1))  # deterministic mtime move
        assert registry_revision(path) != created

    def test_wal_sibling_append_moves_the_token(self, tmp_path: Path) -> None:
        """A WAL append moves the token even while the MAIN file's stat is still."""
        from brutex.dashboard.lake_revision import registry_revision

        path = tmp_path / "registry.duckdb"
        path.write_bytes(b"v1")
        os.utime(path, ns=(1, 1))
        before = registry_revision(path)
        wal = tmp_path / "registry.duckdb.wal"
        wal.write_bytes(b"pending rows")
        after = registry_revision(path)
        assert after != before
        assert registry_revision(path) == after

    def test_main_stat_fault_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError on the MAIN stat → ``None`` ("no signal")."""
        from brutex.dashboard import lake_revision as probe_mod

        def _boom(path: object) -> object:
            raise PermissionError(13, "denied", str(path))

        monkeypatch.setattr(probe_mod.os, "stat", _boom)
        with capture_logs() as logs:
            assert probe_mod.registry_revision(tmp_path / "registry.duckdb") is None
        assert [e for e in logs if e["event"] == "lake_revision.registry_stat_failed"]

    def test_wal_stat_fault_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-missing-file OSError on only the WAL stat → ``None`` too."""
        from brutex.dashboard import lake_revision as probe_mod

        path = tmp_path / "registry.duckdb"
        path.write_bytes(b"v1")
        real_stat = os.stat

        def _wal_boom(target: object, *args: object, **kwargs: object) -> object:
            if str(target).endswith(".wal"):
                raise PermissionError(13, "denied", str(target))
            return real_stat(str(target))

        monkeypatch.setattr(probe_mod.os, "stat", _wal_boom)
        with capture_logs() as logs:
            assert probe_mod.registry_revision(path) is None
        assert [e for e in logs if e["event"] == "lake_revision.registry_stat_failed"]

    def test_wal_checkpoint_removal_moves_the_token_again(self, tmp_path: Path) -> None:
        """The checkpoint that REMOVES the wal moves the token too — correctly.

        Carried from the DEC-MEMORY-FOOTPRINT-HARDENING-001 twin at the
        merge-reconcile consolidation (the two in-flight `registry_revision`
        implementations collapsed onto this ONE probe): a held-open commit
        creates/grows the wal (visible), and the close's checkpoint that
        unlinks it must move the composite AGAIN — a write happened — back to
        the ``|absent``-shaped composite.
        """
        from brutex.dashboard.lake_revision import ABSENT_TOKEN, registry_revision

        path = tmp_path / "registry.duckdb"
        path.write_bytes(b"main-checkpointed")
        wal = tmp_path / "registry.duckdb.wal"
        wal.write_bytes(b"wal-commit-1")
        with_wal = registry_revision(path)
        assert with_wal is not None and not with_wal.endswith(f"|{ABSENT_TOKEN}")
        wal.unlink()
        after_checkpoint = registry_revision(path)
        st = os.stat(path)
        assert after_checkpoint == f"{st.st_mtime_ns}:{st.st_size}|{ABSENT_TOKEN}"
        assert after_checkpoint != with_wal

    def test_wal_beside_absent_main_still_composes(self, tmp_path: Path) -> None:
        """A wal with no main file (a mid-creation race shape) composes visibly.

        Carried from the merge-reconcile consolidation: the write in flight is
        visible — never the plain double-absent composite.
        """
        from brutex.dashboard.lake_revision import ABSENT_TOKEN, registry_revision

        (tmp_path / "registry.duckdb.wal").write_bytes(b"creation-ddl")
        token = registry_revision(tmp_path / "registry.duckdb")
        assert token is not None
        assert token.startswith(f"{ABSENT_TOKEN}|")
        assert token != f"{ABSENT_TOKEN}|{ABSENT_TOKEN}"


class TestResolveRevisionProbeTtl:
    """`resolve_revision_probe_ttl` — the fail-loud env law (H2)."""

    def test_absent_and_blank_read_the_default(self) -> None:
        from brutex.dashboard.lake_revision import (
            DEFAULT_REVISION_PROBE_TTL_S,
            resolve_revision_probe_ttl,
        )

        assert resolve_revision_probe_ttl(env={}) == DEFAULT_REVISION_PROBE_TTL_S
        assert (
            resolve_revision_probe_ttl(env={"BRUTEX_REVISION_PROBE_TTL_S": "  "})
            == DEFAULT_REVISION_PROBE_TTL_S
        )

    def test_explicit_values_parse(self) -> None:
        from brutex.dashboard.lake_revision import resolve_revision_probe_ttl

        assert resolve_revision_probe_ttl(env={"BRUTEX_REVISION_PROBE_TTL_S": "2.5"}) == 2.5
        assert resolve_revision_probe_ttl(env={"BRUTEX_REVISION_PROBE_TTL_S": "0"}) == 0.0

    @pytest.mark.parametrize("junk", ["abc", "-1", "nan", "inf", "-inf"])
    def test_junk_fails_loud(self, junk: str) -> None:
        """Junk / negative / non-finite overrides raise — never a silent fallback."""
        from brutex.dashboard.lake_revision import resolve_revision_probe_ttl

        with pytest.raises(ValueError, match="BRUTEX_REVISION_PROBE_TTL_S"):
            resolve_revision_probe_ttl(env={"BRUTEX_REVISION_PROBE_TTL_S": junk})

    def test_ambient_environ_is_read_when_env_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard.lake_revision import resolve_revision_probe_ttl

        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "3.25")
        assert resolve_revision_probe_ttl() == 3.25


class TestRevisionProbeMemo:
    """`RevisionProbeMemo` — one probe per TTL window; no permanent staleness (H2)."""

    def test_polls_within_ttl_pay_one_probe(self) -> None:
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        clock_now = [0.0]
        calls = [0]

        def _probe() -> str:
            calls[0] += 1
            return f"token-{calls[0]}"

        memo = RevisionProbeMemo(ttl_s=5.0, clock=lambda: clock_now[0])
        results = [memo.get("k", _probe) for _ in range(5)]
        assert calls[0] == 1
        assert results == ["token-1"] * 5

    def test_post_ttl_poll_always_sees_a_moved_token(self) -> None:
        """The H2-guard: a poll AFTER the TTL re-probes — no permanent staleness."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        clock_now = [0.0]
        calls = [0]

        def _probe() -> str:
            calls[0] += 1
            return f"token-{calls[0]}"

        memo = RevisionProbeMemo(ttl_s=5.0, clock=lambda: clock_now[0])
        assert memo.get("k", _probe) == "token-1"
        clock_now[0] = 4.999
        assert memo.get("k", _probe) == "token-1"  # inside the window — memoised
        clock_now[0] = 5.0
        # Exactly AT the TTL boundary (elapsed == ttl) the memo re-probes —
        # strict `<` on the freshness check, so staleness is < ttl, never <= .
        assert memo.get("k", _probe) == "token-2"
        assert calls[0] == 2

    def test_ttl_zero_is_a_pass_through(self) -> None:
        """TTL 0 (the kill switch) = byte-identical legacy: every call probes."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        calls = [0]

        def _probe() -> str:
            calls[0] += 1
            return "t"

        memo = RevisionProbeMemo(ttl_s=0.0)
        for _ in range(3):
            memo.get("k", _probe)
        assert calls[0] == 3

    def test_none_probe_result_is_never_memoised_and_evicts(self) -> None:
        """A ``None`` (outage) verdict is re-derived per call AND evicts a prior entry."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        clock_now = [0.0]
        outcomes: list[str | None] = ["good", None, None, "recovered"]
        calls = [0]

        def _probe() -> str | None:
            value = outcomes[calls[0]]
            calls[0] += 1
            return value

        memo = RevisionProbeMemo(ttl_s=1000.0, clock=lambda: clock_now[0])
        assert memo.get("k", _probe) == "good"
        memo.reset()  # drop the healthy entry so the outage arm is reached
        assert memo.get("k", _probe) is None
        # The None was NOT memoised — the next call probes again...
        assert memo.get("k", _probe) is None
        # ...and recovery lands immediately (never held behind a cached None).
        assert memo.get("k", _probe) == "recovered"
        assert calls[0] == 4

    def test_none_evicts_a_prior_healthy_entry(self) -> None:
        """An outage verdict evicts the prior token — recovery is fresh, not stale."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        clock_now = [0.0]
        outcomes: list[str | None] = ["good", None, "recovered"]
        calls = [0]

        def _probe() -> str | None:
            value = outcomes[calls[0]]
            calls[0] += 1
            return value

        memo = RevisionProbeMemo(ttl_s=5.0, clock=lambda: clock_now[0])
        assert memo.get("k", _probe) == "good"
        clock_now[0] = 5.0  # past the TTL — re-probe hits the outage
        assert memo.get("k", _probe) is None
        # The prior "good" entry was evicted: the NEXT call re-probes even
        # though the clock has not advanced (nothing cached to serve).
        assert memo.get("k", _probe) == "recovered"

    def test_custom_cacheable_predicate(self) -> None:
        """A caller predicate (the runs-probe ArtifactProbe law) gates memoisation."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        calls = [0]

        def _probe() -> tuple[str | None, bool]:
            calls[0] += 1
            return (None, False)  # an "absent" verdict — never memoised

        memo = RevisionProbeMemo(ttl_s=1000.0, clock=lambda: 0.0)
        for _ in range(3):
            assert memo.get("k", _probe, cacheable=lambda p: p[0] is not None) == (None, False)
        assert calls[0] == 3

    def test_keys_are_independent(self) -> None:
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        memo = RevisionProbeMemo(ttl_s=1000.0, clock=lambda: 0.0)
        assert memo.get("a", lambda: "ta") == "ta"
        assert memo.get("b", lambda: "tb") == "tb"
        assert memo.get("a", lambda: "never") == "ta"

    def test_reset_drops_entries(self) -> None:
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        calls = [0]

        def _probe() -> str:
            calls[0] += 1
            return "t"

        memo = RevisionProbeMemo(ttl_s=1000.0, clock=lambda: 0.0)
        memo.get("k", _probe)
        memo.reset()
        memo.get("k", _probe)
        assert calls[0] == 2

    def test_env_junk_fails_loud_at_first_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No injected TTL → the env resolves at the FIRST get; junk raises there."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "garbage")
        memo = RevisionProbeMemo()
        with pytest.raises(ValueError, match="BRUTEX_REVISION_PROBE_TTL_S"):
            memo.get("k", lambda: "t")

    def test_env_ttl_latches_at_first_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env TTL resolves ONCE per instance — a mid-life env edit never re-parses."""
        from brutex.dashboard.lake_revision import RevisionProbeMemo

        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "0")
        memo = RevisionProbeMemo()
        calls = [0]

        def _probe() -> str:
            calls[0] += 1
            return "t"

        memo.get("k", _probe)
        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "garbage")
        memo.get("k", _probe)  # latched 0 — pass-through, junk never re-parsed
        assert calls[0] == 2

    def test_resolve_ttl_double_checked_lock_loser_adopts_the_latch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review round 1 (R1 NIT-3 / R4 NIT-2) — the locked double-check arm.

        Two racing first-callers must latch exactly ONE value: the lock
        LOSER re-reads ``_resolved_ttl`` under the lock, finds the winner's
        latch, and must NOT re-resolve the env. Deterministic race: the test
        holds the memo lock, lets a worker thread block on it, latches the
        value while holding, then releases — the worker takes the
        already-latched inner arm (a re-resolve would read the junk env and
        raise).
        """
        import threading

        from brutex.dashboard.lake_revision import RevisionProbeMemo

        memo = RevisionProbeMemo()
        # Junk env: if the loser re-resolved instead of adopting the latch,
        # it would raise ValueError — the adopt arm must never read the env.
        monkeypatch.setenv("BRUTEX_REVISION_PROBE_TTL_S", "garbage")
        results: list[float | Exception] = []

        def _worker() -> None:
            try:
                results.append(memo._resolve_ttl())
            except Exception as exc:  # the spy arm — a junk-env re-resolve raises ValueError
                results.append(exc)

        with memo._lock:  # the test IS the winning first-caller
            t = threading.Thread(target=_worker)
            t.start()
            # Give the worker time to pass the outer None check and block on
            # the lock; then latch the winner's value while still holding it.
            import time as _time

            _time.sleep(0.05)
            memo._resolved_ttl = 5.0
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert results == [5.0]  # adopted the latch — never re-resolved junk

    def test_production_default_composition_absent_env_memoises_at_1s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full production default end-to-end (review round 1 NIT-2).

        Production wires ``RevisionProbeMemo()`` with NO env override — the
        conftest hermeticity belt sets the env to ``"0"`` suite-wide, so no
        other test ever exercises the composed default path: an ABSENT env
        must resolve to the 1.0 s default AND actually memoise (clock-injected
        — polls inside 1 s pay one probe, a poll at 1 s re-probes).
        """
        from brutex.dashboard.lake_revision import (
            DEFAULT_REVISION_PROBE_TTL_S,
            RevisionProbeMemo,
        )

        monkeypatch.delenv("BRUTEX_REVISION_PROBE_TTL_S", raising=False)
        clock_now = [0.0]
        calls = [0]

        def _probe() -> str:
            calls[0] += 1
            return f"token-{calls[0]}"

        memo = RevisionProbeMemo(clock=lambda: clock_now[0])  # the production shape
        assert [memo.get("k", _probe) for _ in range(4)] == ["token-1"] * 4
        assert calls[0] == 1  # the absent-env default genuinely memoises
        assert DEFAULT_REVISION_PROBE_TTL_S == 1.0
        clock_now[0] = 0.999
        assert memo.get("k", _probe) == "token-1"  # still inside the 1 s window
        clock_now[0] = 1.0
        assert memo.get("k", _probe) == "token-2"  # the default TTL re-probes
        assert calls[0] == 2


class TestConftestHermeticityBelt:
    """Review round 1 (R4 NIT-1) — the suite-wide TTL=0 belt is pinned.

    tests/conftest.py ``setdefault``s ``BRUTEX_REVISION_PROBE_TTL_S=0`` so the
    memo is a pass-through suite-wide (the #1692/#1774 wall-clock hermeticity
    precedent) — dropping that line would reintroduce a real-clock 1 s TTL
    into every /results + oneclick-runs invalidation test (timing flakes).
    The memo's OWN tests stay armed via explicit ``ttl_s=``/``clock=``
    injection, so the belt hides no wiring bug.
    """

    def test_conftest_sets_the_ttl_zero_belt(self) -> None:
        conftest = (Path(__file__).resolve().parents[1] / "conftest.py").read_text(encoding="utf-8")
        assert "from brutex.dashboard.lake_revision import REVISION_PROBE_TTL_ENV" in conftest
        assert 'os.environ.setdefault(REVISION_PROBE_TTL_ENV, "0")' in conftest
