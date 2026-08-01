"""Tests for the run-level Rust degrade sentinel.

DEC-RUST-DEGRADE-SENTINEL-001.

The load-bearing test in this file is
:class:`TestRealFacadeDegrade` — it drives the PRODUCTION render seam into a
genuine fall-back and asserts the sentinel notices. Everything else pins the
mechanism; that one pins that the mechanism works on real code.
"""

from __future__ import annotations

import io
from pathlib import Path

import polars as pl
import pytest
import structlog
from brutex.observability.rust_sentinel import (
    _ENGAGED_LITERAL_RE,
    CLEAN,
    DEGRADE_STEMS,
    DEGRADED,
    NOT_A_RUST_DEGRADE,
    Degrade,
    RustDegradedError,
    RustSentinel,
    _literal_namespaces,
    classify,
    rust_namespaces,
    sentinel_scope,
)


@pytest.fixture(autouse=True)
def _clear_namespace_cache() -> None:
    """Drop the process-lifetime namespace cache between tests."""
    from brutex.observability import rust_sentinel

    rust_sentinel._NAMESPACES.clear()


#: Every event a Rust-facade namespace emits that is NOT a degrade.
#:
#: This is the OTHER half of the exhaustive classification pin below, and
#: it is the forcing function that makes the sentinel un-rottable: a new
#: event on a facade namespace is either matched by
#: :data:`~brutex.observability.rust_sentinel.DEGRADE_STEMS` or it must be
#: listed here, and a reviewer sees which choice the author made in the
#: diff. An unclassified event fails the suite.
#:
#: Each entry is benign for a stated reason, not by omission:
#:
#: * ``engaged`` / ``recency_engaged`` — the crate answered. The opposite
#:   of a degrade.
#: * ``burn_in_complete`` — the bit-compare budget finished CLEAN, so the
#:   facade is now shipping crate values.
#: * ``wide_kernel_promoted`` — a promotion from the u64 to the u128 Rust
#:   kernel (DEC-ENUM-KERNEL-U128-001). More Rust, not less.
#: * ``enum_cells_invalid`` / ``work_cap_clamped`` — an env KNOB held junk
#:   or an out-of-range value and was clamped to the default. The kernel
#:   stays engaged; only the work-cell budget moved.
#: * ``enum_shadow_floor_unmet`` — the shadow-verification budget ran out
#:   of ranges before masks. Its own docstring is explicit: "the kernel
#:   STAYS engaged: every shadowed range was verified."
#: * ``rust_batch_engaged`` — the ``greeks_enrich`` batch facade's
#:   first-answer marker. Exactly the ``recency_engaged`` shape: a prefixed
#:   ``engaged``, and the opposite of a degrade.
BENIGN_FACADE_EVENTS: frozenset[str] = frozenset(
    {
        "engaged",
        "recency_engaged",
        "rust_batch_engaged",
        "burn_in_complete",
        "wide_kernel_promoted",
        "enum_cells_invalid",
        "work_cap_clamped",
        "enum_shadow_floor_unmet",
    }
)

#: Events on an EMBEDDED facade's namespace that belong to the module's
#: DOMAIN logic rather than to the facade at all.
#:
#: Keyed on the FULL event, deliberately unlike :data:`BENIGN_FACADE_EVENTS`
#: above, which is keyed on the suffix. The distinction is the point. A
#: suffix entry blesses that wording on EVERY facade namespace forever — fine
#: for ``engaged``, which means the same thing everywhere, and wrong for
#: ``target_errored``, which here means "one option contract failed to
#: enrich" and elsewhere could mean a Rust kernel refused. Suffix-keying
#: these would silently pre-approve a future ``lake_rust_render
#: .target_errored``.
#:
#: They are here because DEC-RUST-CENSUS-DISCOVERY-MARKER-001 enrolled
#: ``brutex/data/greeks_enrich.py`` — a facade EMBEDDED in a domain module,
#: so its namespace carries both. That enrolment is what made five real Rust
#: degrades visible to the sentinel for the first time; these ten events are
#: the accounting cost of the win, not a weakening of the pin.
EMBEDDED_DOMAIN_EVENTS: frozenset[str] = frozenset(
    {
        # Per-bar input screens — a bar was skipped, or a spot price failed
        # validation. Data quality, not language.
        "greeks_enrich.bar_skipped_nonfinite",
        "greeks_enrich.spot_invalid",
        "greeks_enrich.spot_key_recovered",
        # Per-target enrichment outcomes over the option chain.
        "greeks_enrich.target_enriched",
        "greeks_enrich.target_undecodable",
        "greeks_enrich.target_errored",
        # Worker-pool lifecycle. ``workers_invalid`` clamps a junk worker
        # count; ``pool_broken`` is a crashed process pool. Concurrency
        # events — the Rust batch facade is unaffected by either.
        "greeks_enrich.workers_invalid",
        "greeks_enrich.parallel_start",
        "greeks_enrich.pool_broken",
    }
)


def _facade_events() -> dict[str, str]:
    """Every ``"<facade-namespace>.<suffix>"`` literal under ``brutex/``.

    Maps the event to the file emitting it, so a failure names where to look.
    """
    import re

    root = Path(__file__).resolve().parents[2] / "brutex"
    namespaces = rust_namespaces()
    event_re = re.compile(r"""["']([a-z_0-9.]+)\.([a-z_0-9]+)["']""")
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        for namespace, suffix in event_re.findall(path.read_text(encoding="utf-8")):
            if namespace in namespaces:
                found[f"{namespace}.{suffix}"] = str(path.relative_to(root.parent))
    return found


class TestExhaustiveClassificationPin:
    """Every facade event must be classified — degrade or explicitly benign.

    This is what lets :data:`DEGRADE_STEMS` be a small heuristic rather than
    an 89-name table: the heuristic is allowed to be incomplete only if the
    incompleteness is impossible to ship unnoticed.
    """

    def test_no_facade_event_is_unclassified(self) -> None:
        """A new event wording fails HERE until someone decides what it means."""
        namespaces = rust_namespaces()
        unclassified = {
            event: source
            for event, source in _facade_events().items()
            if classify(event, namespaces) is None
            and event.rpartition(".")[2] not in BENIGN_FACADE_EVENTS
            and event not in EMBEDDED_DOMAIN_EVENTS
            and event not in NOT_A_RUST_DEGRADE
        }
        assert not unclassified, (
            "unclassified facade events — add a DEGRADE_STEMS entry, list the "
            "suffix in BENIGN_FACADE_EVENTS, or (embedded facades only) name "
            "the full event in EMBEDDED_DOMAIN_EVENTS / NOT_A_RUST_DEGRADE: "
            f"{unclassified}"
        )

    def test_the_pin_actually_sees_the_whole_estate(self) -> None:
        """Anti-vacuity: a pin over an empty derivation would pass trivially."""
        events = _facade_events()
        assert len(events) > 100, len(events)
        namespaces = rust_namespaces()
        degrades = {e for e in events if classify(e, namespaces) is not None}
        assert len(degrades) > 80, sorted(degrades)

    @pytest.mark.parametrize(
        "event",
        [
            # The twelve the first cut of this module would have MISSED.
            "sweep_rust_enum.kernel_autoreverted",
            "sweep_rust_metrics.kernel_autoreverted",
            "rust_rank_select.kernel_autoreverted",
            "rust_stage1a.kernel_autoreverted",
            "metrics_rust_fold.kernel_autoreverted",
            "rust_stats.kernel_autoreverted",
            "nightly_rust.kernel_autoreverted",
            "cvar_gate.kernel_autoreverted",
            "lake_rust_locks.locks_autoreverted",
            "lake_rust_merge.merge_autoreverted",
            "rust_trade_sim.call_fallback",
            "cvar_gate.kernel_result_refused",
        ],
    )
    def test_the_prefixed_degrade_family_is_caught(self, event: str) -> None:
        """Regression pin for the measured exact-suffix defect."""
        assert classify(event, rust_namespaces()) is not None

    def test_no_benign_entry_is_dead(self) -> None:
        """Every benign suffix listed is one a facade actually emits."""
        emitted = {event.rpartition(".")[2] for event in _facade_events()}
        assert emitted >= BENIGN_FACADE_EVENTS, BENIGN_FACADE_EVENTS - emitted

    @pytest.mark.parametrize(
        "event",
        [
            # cvar_gate — the whole facade lives in brutex/ranking/cvar_gate.py,
            # a module the census's name pre-filter never looked at.
            "cvar_gate.fallback",
            "cvar_gate.kernel_autoreverted",
            "cvar_gate.kernel_result_refused",
            # greeks_enrich — the batch facade embedded in the domain module.
            "greeks_enrich.rust_batch_autoreverted",
            "greeks_enrich.rust_batch_flag_unrecognised",
        ],
    )
    def test_the_embedded_facades_degrades_are_visible(self, event: str) -> None:
        """The win: five real fall-backs the sentinel could not see before.

        Every one of these is a genuine "Rust stopped answering" event on a
        facade the census could not discover until
        DEC-RUST-CENSUS-DISCOVERY-MARKER-001. Before that fix the namespace
        was not in :func:`rust_namespaces`, so :func:`classify` returned
        ``None`` for all five and a strict sweep would have run to completion
        on the Python oracle reporting CLEAN.

        This test fails if a future change re-hides either facade.
        """
        assert classify(event, rust_namespaces()) is not None, event

    def test_the_one_exclusion_is_not_a_rust_degrade(self) -> None:
        """``parallel_disabled`` carries a stem and must still not fire.

        The Greeks process pool falls back to sequential when the worker
        context will not pickle. That is a CONCURRENCY degrade — the Rust
        batch facade runs identically either way — so classifying it would
        abort a strict sweep for a reason with nothing to do with Rust.

        Asserted against a namespace set that DOES contain ``greeks_enrich``,
        so this proves the exclusion is doing the work and not the (now
        absent) discovery hole.
        """
        namespaces = rust_namespaces()
        assert "greeks_enrich" in namespaces
        assert any(stem in "parallel_disabled" for stem in DEGRADE_STEMS)
        assert classify("greeks_enrich.parallel_disabled", namespaces) is None

    def test_every_exclusion_would_otherwise_have_fired(self) -> None:
        """Anti-rot: an exclusion that changes nothing is dead weight.

        Each :data:`NOT_A_RUST_DEGRADE` entry must be a real suppression —
        its namespace known and its suffix stem-matched — so the set can
        never quietly accumulate entries that were never needed.
        """
        namespaces = rust_namespaces()
        for event in NOT_A_RUST_DEGRADE:
            namespace, _, suffix = event.rpartition(".")
            assert namespace in namespaces, event
            assert any(stem in suffix for stem in DEGRADE_STEMS), event

    def test_embedded_domain_events_are_real_and_stem_free(self) -> None:
        """Anti-rot for the other list: it may not smuggle a degrade past.

        An entry that carries a degrade stem belongs in
        :data:`NOT_A_RUST_DEGRADE`, where the decision is visible, not here
        where it would read as ordinary domain noise.
        """
        emitted = set(_facade_events())
        for event in EMBEDDED_DOMAIN_EVENTS:
            assert event in emitted, f"{event} is listed but never emitted"
            suffix = event.rpartition(".")[2]
            offending = [stem for stem in DEGRADE_STEMS if stem in suffix]
            assert not offending, (event, offending)

    def test_benign_and_degrade_do_not_overlap(self) -> None:
        """A suffix cannot be both; that would make the classification a coin toss."""
        namespaces = rust_namespaces()
        both = {
            event
            for event in _facade_events()
            if classify(event, namespaces) is not None
            and event.rpartition(".")[2] in BENIGN_FACADE_EVENTS
        }
        assert not both, both


class TestStemSafety:
    """The stems are a substring rule, so a bad entry is quietly catastrophic."""

    def test_no_stem_is_empty(self) -> None:
        """``"" in suffix`` is always True — one empty stem flags EVERY event."""
        assert all(DEGRADE_STEMS), DEGRADE_STEMS

    def test_no_stem_matches_a_benign_suffix(self) -> None:
        """A stem broad enough to hit ``engaged`` would make the sentinel useless."""
        offenders = {
            benign: stem
            for benign in BENIGN_FACADE_EVENTS
            for stem in DEGRADE_STEMS
            if stem in benign
        }
        assert not offenders, offenders


class TestNonRustBurnInSeamsAreExcluded:
    """The two seams that share the shape but are not Rust."""

    @pytest.mark.parametrize("namespace", ["prebuild.frame_write", "power.keepawake"])
    def test_a_python_burn_in_seam_is_not_a_rust_namespace(self, namespace: str) -> None:
        """Their degrades are real, but calling them Rust degrades is false.

        ``prebuild.frame_write`` is a Python fast-lane versus the verbatim
        legacy lane (DEC-PREBUILD-FRAME-WRITE-001) and ``power.keepawake``
        is a macOS process guard. Both emit ``.engaged`` and both autorevert.
        """
        assert namespace not in rust_namespaces()

    def test_but_the_shared_rust_core_is_kept(self) -> None:
        """``rust_parity`` names no crate of its own — the name arm keeps it."""
        assert "rust_parity" in rust_namespaces()


class TestLiteralNamespaces:
    def test_finds_the_shared_core_the_census_cannot_see(self) -> None:
        """``rust_parity`` has no facade module of its own; the scan finds it."""
        assert "rust_parity" in _literal_namespaces()

    def test_finds_a_facade_the_census_name_prefilter_skips(self) -> None:
        """``cvar_gate`` carries no "rust"/"seam" in its leaf name."""
        assert "cvar_gate" in _literal_namespaces()

    def test_an_unreadable_source_is_skipped_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One unreadable file must not blind the whole derivation.

        The read is failed by monkeypatch rather than a chmod because CI and
        the dev container both run as root, where the mode bit is not
        enforced — a permission-based test would SKIP there and leave the
        arm unproven exactly where it matters.
        """
        (tmp_path / "good.py").write_text('_LOG.info("good_rust.engaged")\n', encoding="utf-8")
        (tmp_path / "bad.py").write_text('_LOG.info("bad_rust.engaged")\n', encoding="utf-8")

        real_read = Path.read_text

        def _read(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "bad.py":
                raise PermissionError(13, "Permission denied", str(self))
            return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _read)
        assert _literal_namespaces(tmp_path) == frozenset({"good_rust"})

    def test_a_source_with_no_engaged_literal_is_skipped_early(self, tmp_path: Path) -> None:
        """The cheap pre-check: most files emit nothing and pay no regex work."""
        (tmp_path / "quiet.py").write_text("x = 1\n", encoding="utf-8")
        assert _literal_namespaces(tmp_path) == frozenset()

    def test_a_non_rust_burn_in_seam_is_filtered_out(self, tmp_path: Path) -> None:
        """Emitting ``.engaged`` is not enough — the source must be Rust."""
        (tmp_path / "seam.py").write_text(
            '_LOG.info("prebuild.frame_write.engaged")\n', encoding="utf-8"
        )
        assert _literal_namespaces(tmp_path) == frozenset()

    def test_a_crate_import_qualifies_a_source(self, tmp_path: Path) -> None:
        """The crate arm of the discriminator, not just the env-var arm."""
        (tmp_path / "seam.py").write_text(
            'importlib.import_module("brutex_lake")\n_LOG.info("some.seam.engaged")\n',
            encoding="utf-8",
        )
        assert _literal_namespaces(tmp_path) == frozenset({"some.seam"})

    def test_the_regex_accepts_dotted_namespaces(self) -> None:
        """``prebuild.frame_write.engaged`` is a real, dotted namespace."""
        assert _ENGAGED_LITERAL_RE.findall('"prebuild.frame_write.engaged"') == [
            "prebuild.frame_write"
        ]


class TestRustNamespaces:
    def test_is_the_union_of_both_derivations(self) -> None:
        """Neither derivation alone is complete; the union is what ships."""
        from brutex.observability.rust_census import census

        leaves = {row.module.rsplit(".", 1)[-1] for row in census()}
        literals = _literal_namespaces()
        resolved = rust_namespaces()
        assert leaves <= resolved
        assert literals <= resolved
        # Each half genuinely contributes something the other misses --
        # otherwise one of them is dead weight and this test says so.
        assert leaves - literals, "the census half contributes nothing"
        assert literals - leaves, "the literal half contributes nothing"

    def test_the_config_trio_arrives_via_the_census_half(self) -> None:
        """Their events are f-string rendered, so no literal exists."""
        literals = _literal_namespaces()
        for name in ("paths_rust", "dotenv_rust", "backup_env_rust"):
            assert name not in literals
            assert name in rust_namespaces()

    def test_result_is_cached(self) -> None:
        """The derivation reads the source tree; do it once per process."""
        first = rust_namespaces()
        assert rust_namespaces() is first


class TestClassify:
    @pytest.mark.parametrize(
        "event",
        [
            "lake_rust_render.fallback",
            "optcost_rust.autoreverted",
            "lake_rust_stats.render_autoreverted",
            "meta_rust.flag_unrecognised",
            "money_rust.rust_path_disabled",
            "rust_parity.fallback",
        ],
    )
    def test_a_real_facade_degrade_classifies(self, event: str) -> None:
        assert classify(event, rust_namespaces()) == event.rpartition(".")[2]

    @pytest.mark.parametrize(
        "event",
        [
            # The measured trap: a watched SUFFIX on an ordinary namespace.
            "auto_resume.disabled",
            "backup.drill.mismatch",
            "integrity.mismatch",
            "bhavcopy.mismatch",
            "universe.autopull.disabled",
            "server.heartbeat.disabled",
            "sweep_diffcheck.mismatch",
            "window_ledger_page.fallback",
            "lake.stats_manifest.fallback",
        ],
    )
    def test_a_watched_suffix_on_a_non_facade_namespace_does_not_fire(self, event: str) -> None:
        """Nine real events that would have made a suffix-only sentinel useless."""
        assert classify(event, rust_namespaces()) is None

    def test_a_facade_success_event_does_not_fire(self) -> None:
        assert classify("lake_rust_render.engaged", rust_namespaces()) is None

    def test_an_event_with_no_namespace_does_not_fire(self) -> None:
        assert classify("startup", rust_namespaces()) is None


class TestSentinelProcessor:
    def test_a_clean_run_stays_clean(self) -> None:
        sentinel = RustSentinel(namespaces=frozenset({"a_rust"}))
        sentinel(None, "info", {"event": "a_rust.engaged"})
        assert sentinel.verdict == CLEAN
        assert sentinel.report() == f"{CLEAN}: no enrolled Rust facade fell back to Python."

    def test_a_degrade_latches_and_is_reported(self) -> None:
        sentinel = RustSentinel(namespaces=frozenset({"a_rust"}))
        sentinel(None, "warning", {"event": "a_rust.fallback", "reason": "wheel_missing"})
        assert sentinel.verdict == DEGRADED
        assert sentinel.degrades == [
            Degrade(
                event="a_rust.fallback",
                suffix="fallback",
                detail={"event": "a_rust.fallback", "reason": "wheel_missing"},
            )
        ]
        assert "wheel_missing" in sentinel.report()

    def test_a_degrade_without_a_reason_still_reports(self) -> None:
        sentinel = RustSentinel(namespaces=frozenset({"a_rust"}))
        sentinel(None, "warning", {"event": "a_rust.fallback"})
        assert "reason=-" in sentinel.report()

    def test_a_non_string_event_is_ignored(self) -> None:
        """Defensive: a processor upstream may not have set ``event`` to a str."""
        sentinel = RustSentinel(namespaces=frozenset({"a_rust"}))
        sentinel(None, "info", {"event": 17})
        assert sentinel.verdict == CLEAN

    def test_the_event_dict_is_returned_unmodified(self) -> None:
        """The processor contract: observe, never mutate."""
        sentinel = RustSentinel(namespaces=frozenset({"a_rust"}))
        record = {"event": "a_rust.fallback", "reason": "x"}
        assert sentinel(None, "warning", record) is record
        assert record == {"event": "a_rust.fallback", "reason": "x"}

    def test_strict_raises_on_the_first_degrade(self) -> None:
        sentinel = RustSentinel(strict=True, namespaces=frozenset({"a_rust"}))
        with pytest.raises(RustDegradedError, match="a_rust.fallback"):
            sentinel(None, "warning", {"event": "a_rust.fallback", "reason": "stale"})
        # It still RECORDS before raising, so the report names the cause.
        assert sentinel.degrades[0].suffix == "fallback"

    def test_strict_names_the_missing_reason_rather_than_omitting_it(self) -> None:
        sentinel = RustSentinel(strict=True, namespaces=frozenset({"a_rust"}))
        with pytest.raises(RustDegradedError, match="no reason given"):
            sentinel(None, "warning", {"event": "a_rust.fallback"})

    def test_default_namespaces_come_from_the_derivation(self) -> None:
        assert RustSentinel().namespaces == rust_namespaces()


class TestSentinelScope:
    def test_installs_at_the_front_and_removes_on_exit(self) -> None:
        before = list(structlog.get_config()["processors"])
        with sentinel_scope() as sentinel:
            live = structlog.get_config()["processors"]
            assert live[0] is sentinel
        assert list(structlog.get_config()["processors"]) == before

    def test_mutates_the_live_list_in_place(self) -> None:
        """Replacing the list would orphan every already-bound logger."""
        live = structlog.get_config()["processors"]
        with sentinel_scope():
            assert structlog.get_config()["processors"] is live
        assert structlog.get_config()["processors"] is live

    def test_removes_even_when_the_block_raises(self) -> None:
        before = list(structlog.get_config()["processors"])
        with pytest.raises(ValueError, match="boom"), sentinel_scope():
            raise ValueError("boom")
        assert list(structlog.get_config()["processors"]) == before

    def test_a_sentinel_already_removed_exits_cleanly(self) -> None:
        """Someone else reconfigured structlog mid-block: do not raise on exit."""
        before = list(structlog.get_config()["processors"])
        with sentinel_scope() as sentinel:
            structlog.get_config()["processors"].remove(sentinel)
        assert list(structlog.get_config()["processors"]) == before

    def test_observes_a_real_structlog_emission(self) -> None:
        log = structlog.get_logger("sentinel-test")
        with sentinel_scope() as sentinel:
            log.warning("lake_rust_render.fallback", reason="crate_stale")
        assert sentinel.verdict == DEGRADED

    def test_strict_propagates_out_of_the_block_and_uninstalls(self) -> None:
        before = list(structlog.get_config()["processors"])
        log = structlog.get_logger("sentinel-test")
        with pytest.raises(RustDegradedError), sentinel_scope(strict=True):
            log.warning("money_rust.fallback", reason="wheel_missing")
        assert list(structlog.get_config()["processors"]) == before


class TestRealFacadeDegrade:
    """Drive the PRODUCTION render seam into a real fall-back.

    No hand-emitted events anywhere in this class. It calls
    ``lake_rust_render.render_parquet_bytes`` exactly as
    ``ParquetWriter._render_bytes`` does (parquet_writer.py:2765-2777),
    including the production bars-profile oracle kwargs, and varies ONLY the
    polars version the caller reports — the same input PR #1764's dependency
    bump changes.

    If the crate is not built, both arms are vacuous, so the test skips
    rather than passing on nothing.
    """

    @staticmethod
    def _run(version: str) -> RustSentinel:
        from brutex.data import lake_rust_render
        from brutex.data.parquet_writer import ROW_GROUP_SIZE, ZSTD_LEVEL

        frame = pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})

        def ipc_bytes() -> bytes:
            buffer = io.BytesIO()
            frame.write_ipc(buffer, compression="uncompressed")
            return buffer.getvalue()

        def python_render() -> bytes:
            buffer = io.BytesIO()
            frame.write_parquet(
                buffer,
                compression="zstd",
                compression_level=ZSTD_LEVEL,
                row_group_size=ROW_GROUP_SIZE,
                use_pyarrow=False,
                statistics=False,
            )
            return buffer.getvalue()

        with sentinel_scope() as sentinel:
            for _ in range(3):
                lake_rust_render.render_parquet_bytes(
                    ipc_bytes_factory=ipc_bytes,
                    python_render=python_render,
                    polars_version=version,
                    schema_token=str(frame.schema),
                )
        return sentinel

    def test_the_pinned_wheel_runs_clean(self) -> None:
        pytest.importorskip("brutex_lake")
        assert self._run(pl.__version__).verdict == CLEAN

    def test_a_version_bump_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact failure mode PR #1764 would introduce.

        Pinned ``BRUTEX_RUST_STRICT=0`` for this test ONLY.  This test
        deliberately INDUCES the ``polars_version_mismatch`` degrade so the
        sentinel can observe it; under the shipped strict-ON default
        (DEC-RUST-STRICT-NO-FALLBACK-001) that degrade RAISES
        ``RustFallbackRefused`` at the ``lake_rust_render`` choke point
        before any event reaches the sentinel.  Declaring the mode here
        keeps the sentinel's assertions intact rather than weakening them,
        and leaves the sibling ``test_the_pinned_wheel_runs_clean`` — which
        exercises the REAL installed/crate version pair — under the shipped
        default.
        """
        from brutex.config import rust_strict

        monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")
        pytest.importorskip("brutex_lake")
        sentinel = self._run("1.43.0")
        assert sentinel.verdict == DEGRADED
        assert sentinel.degrades[0].event == "lake_rust_render.rust_path_disabled"
        assert sentinel.degrades[0].detail["reason"] == "polars_version_mismatch"
