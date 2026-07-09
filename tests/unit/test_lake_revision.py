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
