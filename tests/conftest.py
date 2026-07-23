"""Root pytest configuration for BruteX."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import structlog
from brutex.adapters.groww.constants import GROWW_TOKEN_CACHE_PATH_ENV
from brutex.calendars.market_hours import MARKET_HOURS_GUARD_ENV
from brutex.calendars.settle_evidence import SETTLE_EVIDENCE_PATH_ENV
from brutex.dashboard.lake_revision import REVISION_PROBE_TTL_ENV
from brutex.data.today_completeness import (
    TODAY_COMPLETENESS_GATE_ENV,
    reset_today_gate_log_state,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from hypothesis import HealthCheck
from hypothesis import settings as _hyp_settings

# DEC-TEST-APPARATUS-DEBTS-001(b): EXPLICIT CI determinism. Honesty note
# (Golden Rule #1): hypothesis >=6.113 ALREADY auto-loads its own built-in
# "ci" profile (derandomize=True) whenever a CI env var (CI /
# GITHUB_ACTIONS / ...) is set, so GHA property runs were implicitly
# derandomized even before this block existed. This block's real job is to
# make the selection EXPLICIT and env-driven, and to pin the profile's
# fields IN-REPO independent of upstream's built-in: every field is set
# explicitly (never inherited from the ambient default at import time), so
# the profile is environment-invariant — a local `HYPOTHESIS_PROFILE=ci`
# run is field-identical to CI. Per-test @settings decorators inherit
# UNSPECIFIED fields from the loaded profile, so derandomize=True
# propagates through every hypothesis test with zero per-file edits.
# Selected via HYPOTHESIS_PROFILE=ci (ci.yml / verify-m4pro.yml /
# mutation.yml). SANCTIONED FLIP: a CI-env run that does NOT set
# HYPOTHESIS_PROFILE (stress-nightly + the other scheduled arms) loads
# "default" here, OVERRIDING hypothesis's auto-loaded built-in ci profile —
# those arms are deliberately RANDOM (exploration off the PR path; before
# this block they were implicitly derandomized by the auto-load). Currently
# vacuous — no @given tests live in those suites — but stated honestly.
# Pinned by tests/unit/test_hypothesis_ci_profile.py (fields + source pin +
# CI-env-stripped subprocess selection pin + run-twice draw identity).
_hyp_settings.register_profile(
    "ci",
    derandomize=True,
    print_blob=True,
    deadline=None,
    database=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
_hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# DEC-GROWW-MARKET-HOURS-GUARD-001 test hermeticity (the #1380 philosophy —
# hermetic against the real HOST, here the WALL CLOCK instead of the disk):
# the market-hours START gate's production default (`guard_active_default`)
# reads the REAL clock + `os.environ`, so any test that starts a
# `BackgroundPullJob` / drives a pull route WITHOUT injecting a guard would
# pass out-of-hours and FAIL between 08:45-15:35 IST on an NSE trading day (a
# wall-clock flake class over the whole suite — reproduced locally 2026-07-10
# 12:02 IST: 13 untouched `test_dashboard_pull_stall.py` starts deferred).
# The suite therefore runs under the DOCUMENTED operator kill switch
# (`BRUTEX_MARKET_HOURS_GUARD=0`, `setdefault` so an explicit outer choice
# wins). The guard's OWN tests stay fully armed on both arms — they inject
# `env=` mappings / guard callables / monkeypatched clocks explicitly
# (test_market_hours.py, the gate tests in test_pull_jobs.py /
# test_cli_retry_storm.py / test_nightly_pipeline_binding.py) — so this
# default disables ONLY the ambient real-clock read, never the tested law.
os.environ.setdefault(MARKET_HOURS_GUARD_ENV, "0")

# DEC-PULL-TODAY-COMPLETENESS-GATE-001 test hermeticity (the #1692 philosophy —
# hermetic against the WALL CLOCK): the gate's four production arm sites resolve
# the REAL ``os.environ`` (``resolve_today_completeness_enabled``) and the gate
# then judges real-clock-clamped windows, so a production-wrapper test that
# fabricates a today-ending window and runs at/after 15:30 IST on a trading day
# would wall-clock-flake. The suite therefore runs under the DOCUMENTED operator
# kill switch (``setdefault`` so an explicit outer choice wins). The gate's OWN
# tests stay fully armed on both arms — they inject ``env=`` mappings + explicit
# ``clamp_today`` dates / ``now_ist`` clocks (test_today_completeness.py, the
# gate tests in test_groww_ingest.py / test_groww_option_ingest.py) — so this
# default disables ONLY the ambient real-environ read, never the tested law.
os.environ.setdefault(TODAY_COMPLETENESS_GATE_ENV, "0")

# DEC-O1-GUARANTEE-HARDEN-001 (H2) test hermeticity (the same philosophy —
# hermetic against the WALL CLOCK): the revision-probe TTL memo
# (``lake_revision.RevisionProbeMemo``, default TTL 1 s) would make any test
# that writes a sweep/run artifact and RE-POLLS a probe-keyed route within one
# second serve the pre-write token — a timing-dependent flake class over every
# /results + oneclick-runs invalidation test. The suite therefore runs under
# the DOCUMENTED kill switch (``BRUTEX_REVISION_PROBE_TTL_S=0`` = the memo is
# a pass-through, byte-identical per-poll probes; ``setdefault`` so an
# explicit outer choice wins). The memo's OWN tests stay fully armed — they
# inject ``ttl_s=`` + clocks explicitly (test_lake_revision.py, the route pins
# in tests/bench/test_revision_probe_ttl_o1.py) — so this default disables
# ONLY the ambient real-clock TTL, never the tested law.
os.environ.setdefault(REVISION_PROBE_TTL_ENV, "0")

# DEC-SETTLE-EVIDENCE-ADOPTION-001 test hermeticity (the market-hours-guard
# philosophy — hermetic against the REPO CHECKOUT instead of the wall clock):
# resolve_settle_time's evidence tier reads the repo-committed
# docs/groww/probe/settle_evidence.json by default, so the DAY a measured
# evidence file lands, every env=None / injected-mapping-without-the-key test
# would silently change settle semantics (17:30 -> the measured value) — a
# whole-suite flake class armed by a FUTURE commit. The suite therefore runs
# with the channel off (`setdefault` so an explicit outer choice wins). The
# channel's OWN tests stay fully armed — they inject env mappings + tmp paths
# and monkeypatch settle_evidence.default_evidence_path explicitly; the ONE
# committed-file CI test bypasses env entirely (validates default_evidence_path()
# directly) — so this default disables ONLY the ambient repo read, never the
# tested law.
os.environ.setdefault(SETTLE_EVIDENCE_PATH_ENV, "off")

# DEC-STRATEGIES-RUST-COMBO-001 test hermeticity (the market-hours-guard
# philosophy — hermetic against the AMBIENT ENVIRONMENT): the combo_rust
# facade's flag (`BRUTEX_STRATEGIES_RUST_COMBO`, default ON since
# DEC-RUST-DEFAULT-ON-WAVE-001) is read per call from the REAL
# ``os.environ``, so an ambient export — either polarity — would flip
# unrelated suites' arms. The suite therefore runs with the flag pinned to
# the explicit "0" kill switch (`setdefault` so an explicit outer choice
# wins) — the byte-identical oracle arm, wheel-state-independent in every
# lane. The facade's OWN tests stay fully armed on both arms — the
# default-is-ON pin uses ``monkeypatch.delenv`` and the ON-arm tests set
# the flag EXPLICITLY via ``monkeypatch.setenv`` (both override this
# setdefault) — so this default disables ONLY the ambient env read, never
# the tested law.
#
# WHEEL-FREE DISCIPLINE: this file must stay importable in the wheel-LESS
# lanes (lake-rust-parity installs only the brutex-lake wheel;
# redteam-nightly installs only ``.[dev]``), and importing
# ``brutex.strategies.combo_rust`` runs the ``brutex.strategies`` package
# __init__, which hard-imports ``combo.py`` -> ``import fno_math`` (the
# blake3 wheel) — reproduced red on the lake lane 2026-07-17 (actions run
# 29599656943, ``ModuleNotFoundError: No module named 'fno_math'`` at this
# very import). The env-name constant is therefore imported when the wheel
# is present (one source of truth) and falls back to the pinned literal
# when it is not — the fallback literal is DRIFT-PINNED equal to
# ``combo_rust.STRATEGIES_RUST_COMBO_ENV`` by
# tests/unit/test_strategies_combo_rust.py::TestConftestBelt (which runs in
# the wheel-present lanes). In a wheel-less lane the strategies package is
# unimportable anyway, so the setdefault is vacuous-but-harmless there.
try:
    from brutex.strategies import combo_rust as _combo_rust
except ModuleNotFoundError:  # the wheel-less lane arm (lake-rust-parity / redteam)
    _combo_rust = None
STRATEGIES_RUST_COMBO_ENV = (
    "BRUTEX_STRATEGIES_RUST_COMBO" if _combo_rust is None else _combo_rust.STRATEGIES_RUST_COMBO_ENV
)
os.environ.setdefault(STRATEGIES_RUST_COMBO_ENV, "0")

# DEC-ALPHA-BUYHOLD-METRIC-001 test hermeticity (the market-hours-guard
# philosophy — hermetic against the AMBIENT ENVIRONMENT): the alpha
# rejection gate (`BRUTEX_ALPHA_GATE`, default OFF) is resolved from the
# REAL ``os.environ`` at cmd_sweep_all launch + the results-depth pass
# entry. When the flag is UNSET this belt pins it explicitly OFF
# (``setdefault``), so the default-build byte-identity pins (no gate keys on
# the top100 ``source``) never depend on the runner's shell; an explicit
# outer export — truthy or junk alike — WINS over the setdefault, and such a
# run will honestly flip those pins / red the fail-loud launches (the
# sibling-belt contract; not something a setdefault can prevent). The gate's
# OWN tests stay fully armed — they inject env mappings or
# ``monkeypatch.setenv`` explicitly (which overrides this setdefault) — so
# this default disables ONLY the ambient env read, never the tested law.
# Pinned literal (the wheel-free discipline above — the constant home
# ``brutex.engine.alpha_metric`` transitively imports the engine package,
# which hard-imports the wheels); drift-pinned equal to
# ``alpha_metric.ALPHA_GATE_ENV`` by tests/unit/test_engine_alpha_metric.py.
os.environ.setdefault("BRUTEX_ALPHA_GATE", "0")

# DEC-BOX-AUTODEPLOY-LANE-001 hermeticity belt: the nightly's pre-chain
# box-update leg runs REAL git (incl. a network `git fetch origin main`) at
# `cmd_nightly_run` time — the whole suite runs with the lane's kill switch
# OFF (`BRUTEX_BOX_UPDATE=0`, `setdefault` so an explicit outer choice wins;
# the #1692 market-hours-guard precedent). The lane's OWN tests inject their
# env/runner explicitly and stay armed on both arms.
os.environ.setdefault("BRUTEX_BOX_UPDATE", "0")

# DEC-RUST-DEFAULT-ON-WAVE-001 hermeticity belt: `BRUTEX_RUST_FRONT` now
# defaults to AUTO (unset => probe for a built `brutexd` binary; found =>
# the Rust front topology). On a box WITH a locally built brutexd every
# pre-existing `cmd_dashboard` test running flag-unset would silently flip
# onto the Rust-front serve path (a host-toolchain flake class — the #1692
# market-hours-guard philosophy: hermetic against the AMBIENT environment,
# never the tested law). Pin the suite explicitly OFF (`setdefault` so an
# explicit outer choice wins); the rust-front suites set the flag / inject
# probes explicitly and stay armed on every arm (off / auto / on / junk).
# Pinned literal — drift-pinned equal to `rust_front.ENV_RUST_FRONT` by
# tests/unit/test_rust_front.py.
os.environ.setdefault("BRUTEX_RUST_FRONT", "off")

# DEC-BOX-AUTODEPLOY-LANE-001 §12 hermeticity (the env_preflight REGISTRY
# flags): a HOST env carrying `BRUTEX_SKIP_WHEEL_FRESHNESS` (the CI wheel
# escape) or a stale `BRUTEX_FILL_MODE` would populate every pre-existing
# nightly test's `env_preflight` block through `cmd_nightly_run`'s
# `os.environ` scan (a host-env flake class — the #1380 hermeticity
# philosophy), and a SET `BRUTEX_FILL_MODE` additionally trips the
# DEC-FILL-WORST-CASE-ONLY-001 launch refusal across the engine suites.
# POP both outright (never `setdefault` — no test relies on a
# host-inherited value: the preflight/doctor/fill_mode suites all inject
# `env=` or `monkeypatch.setenv` explicitly).
os.environ.pop("BRUTEX_SKIP_WHEEL_FRESHNESS", None)
os.environ.pop("BRUTEX_FILL_MODE", None)

# DEC-ABSTAIN-GATE-STREAM-001 + DEC-META-LABEL-ANNOTATE-001 hermeticity: a
# host env carrying the DORMANT `BRUTEX_ABSTAIN_GATE_INSIM` knob would trip
# the sweep-launch refusal across every sweep-all suite, and an ambient
# `BRUTEX_META_GATE` would flip the Rust meta facade (default ON since
# DEC-RUST-DEFAULT-ON-WAVE-001) off its default for unrelated tests. POP
# both (the fill-mode discipline above — the meta suites inject env
# explicitly on both arms).
os.environ.pop("BRUTEX_ABSTAIN_GATE_INSIM", None)
os.environ.pop("BRUTEX_META_GATE", None)


@pytest.fixture(autouse=True)
def _reset_meta_rust_latch() -> Iterator[None]:
    """Belt-and-braces: never let a meta-facade latch leak across suites.

    DEC-META-LABEL-ANNOTATE-001 (round-1 N-2): `brutex.meta.meta_rust`
    keeps a per-process burn-in counter + autorevert LATCH plus a warn-once
    registry. The meta suites reset it locally, but any OTHER test that
    drives the facade (an integration chain with `BRUTEX_META_GATE` set)
    could latch it and silently change a later suite's arm. Reset AFTER
    every test — but only when the module was actually imported (the lazy
    `sys.modules` probe keeps this a no-op for the rest of the battery and
    never imports the facade into tests that pin import-freeness).
    """
    yield
    meta_rust = sys.modules.get("brutex.meta.meta_rust")
    if meta_rust is not None:
        meta_rust.reset_meta_rust_for_tests()


# DEC-TEST-AWS-HERMETICITY-001 — the suite-wide AWS hermeticity belt. The
# loud, named error every blocked live-AWS attempt raises. Message names the
# remediation (inject a fake), never a secret.
AWS_HERMETICITY_BELT_MESSAGE = (
    "test tried to reach live AWS (blocked by the tests/conftest.py AWS "
    "hermeticity belt) — inject a fake store/client instead "
    "(tests/fakes/fake_aws_client.py / tests/fakes/fake_s3_client.py, or the "
    "account_key_resolver / aws_loader / client= injection seams)"
)


def _aws_belt_blocked(*_args: Any, **_kwargs: Any) -> Any:
    """The belt's boundary stand-in — raises loudly instead of touching AWS."""
    raise RuntimeError(AWS_HERMETICITY_BELT_MESSAGE)


@pytest.fixture(autouse=True)
def _aws_hermeticity_belt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make it impossible for ANY test to reach live AWS (SSM/S3) by accident.

    DEC-TEST-AWS-HERMETICITY-001 (the market-hours-guard philosophy — hermetic
    against the NETWORK instead of the wall clock): production resolves the
    Groww credentials + shared token from LIVE AWS SSM SecureStrings
    (DEC-AWS-ONLY-CREDS-001), and ``BackgroundPullJob.start``'s DEFAULT
    ``account_key_resolver`` reaches that path — so on a box with boto3 + ambient
    IAM credentials, a test that starts a job without injecting a resolver
    performed a REAL ``ssm:GetParameter WithDecryption=true`` against the
    production SecureStrings (a hermeticity failure + a secret-exposure class;
    on boto3-less CI the same path merely degraded to ``CredentialsError`` →
    graceful skip, which is why it stayed green). This belt pins EVERY suite
    run to the deterministic no-AWS behaviour:

    * the repo's ONE real-SSM-client factory
      (``brutex.adapters.groww.aws_client.build_real_aws_client``) — plus its
      module-level from-import bindings in ``credentials_aws`` /
      ``token_store_aws`` — is replaced with a stand-in raising
      :data:`AWS_HERMETICITY_BELT_MESSAGE`. Callers keep their DOCUMENTED
      fail-fast mapping (e.g. ``credentials_aws._build_client_or_raise`` maps
      any build failure to ``CredentialsError``), so the suite behaves exactly
      like boto3-less CI on every box — but the live call is now IMPOSSIBLE,
      not merely absent.
    * the boto3 network boundary itself (``boto3.client`` / ``boto3.resource``
      + the ``Session`` method twins) is patched when boto3 is importable —
      covering lazy function-local ``import boto3`` sites (e.g. the
      crossverify S3 publisher's real-client factory) and any direct use.
      Skipped gracefully when boto3 is not installed (the CI posture).

    Tests that deliberately fake the seam (constructor-injected
    ``AwsClient``/S3 fakes, ``aws_loader=``, ``account_key_resolver=``, or a
    test-level monkeypatch of the same attribute — which overrides this belt)
    are untouched: the fakes never construct a real boto3 client. Pinned by
    ``tests/unit/test_aws_hermeticity_belt.py`` (active + loud + source-pinned
    ``autouse=True``).
    """
    import brutex.adapters.groww.aws_client as _aws_client_module
    import brutex.adapters.groww.token_store_aws as _token_store_aws_module
    import brutex.config.credentials_aws as _credentials_aws_module

    monkeypatch.setattr(_aws_client_module, "build_real_aws_client", _aws_belt_blocked)
    # From-import bindings resolve from THEIR module's namespace — patch each.
    monkeypatch.setattr(_credentials_aws_module, "build_real_aws_client", _aws_belt_blocked)
    monkeypatch.setattr(_token_store_aws_module, "build_real_aws_client", _aws_belt_blocked)
    try:
        import boto3  # type: ignore[import-not-found, unused-ignore]
        import boto3.session  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        return  # boto3 not installed (the CI posture) — nothing to patch.
    monkeypatch.setattr(boto3, "client", _aws_belt_blocked)
    monkeypatch.setattr(boto3, "resource", _aws_belt_blocked)
    monkeypatch.setattr(boto3.session.Session, "client", _aws_belt_blocked)
    monkeypatch.setattr(boto3.session.Session, "resource", _aws_belt_blocked)


@pytest.fixture(autouse=True)
def _fresh_today_gate_log_state() -> None:
    """Reset the TODAY-gate's per-pull capped-event counters before EVERY test.

    DEC-PULL-TODAY-COMPLETENESS-GATE-001 round-1 R2-M1: the module keeps
    thread-safe per-``pull_id`` counters for its capped WARN/INFO events (the
    per-pull design — production never resets them). Tests that drive the gate
    without a bound pull scope share the ``""`` key, so without a reset a
    later-added test could inherit a prior test's counts and flake on its
    event-count asserts (measured headroom was 11/20 at review time). The reset
    is the module's OWN test seam (``reset_today_gate_log_state``) — never a
    production-code reset; 5 dict clears per test, negligible.
    """
    reset_today_gate_log_state()


@pytest.fixture
def _inert_sweep_logging_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI dispatch-level sweep logging guard inert (opt-in).

    The ``main()`` sweep dispatch configures PROCESS-GLOBAL structlog state
    (DEC-SWEEP-LOG-VOLUME-GUARD-001). Test files that drive
    ``main([... "sweep*" ...])`` for OTHER behavior opt in via a module-level
    ``pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")`` so
    their assertions stay byte-identical and no global logging config leaks
    into later tests in the same process. The guard's own behavior is pinned
    in ``tests/unit/test_cli_sweep_log_level.py``.
    """
    from brutex import cli

    monkeypatch.setattr(cli, "_configure_sweep_logging", lambda: None)


@pytest.fixture(autouse=True)
def _lake_heal_sweep_daemon_belt(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the dashboard-boot lake-heal sweep DAEMON launcher for every test.

    The #1654-surfaced shard-2 doctest flake (PR #1666, CI jobs 88186693408 +
    88192989598 — twice consecutively): tests that drive the REAL
    ``cli.cmd_dashboard`` (six files stubbed the sibling launchers but not
    this one — e.g. ``test_cli_serve_heartbeat_keepawake.py``) spawned a real
    ``StartupSweeper`` daemon thread (``brutex-lake-heal-sweep``, via
    ``cli._start_lake_heal_sweep``) that outlives its test un-joined. When it
    later logs ``lake.invariant.scan_complete`` / ``lake_heal.sweep_complete``
    via UNCONFIGURED structlog (default ``PrintLogger`` → the CURRENT
    ``sys.stdout``), the lines land in whatever capture a LATER test has
    installed — in shard 2 that was the ``combo_mask`` doctest's swapped-in
    stdout buffer, turning ``bits_to_mask()``'s expected ``0`` into
    ``<log line>\\n0``. #1654's test-file deletes reshuffled the round-robin
    shard partition (``scripts/ci/shard_tests.py``) so the spawner and the
    doctest gate first became shard neighbours there; the leak itself is
    order-dependent, so this belt kills it suite-wide at the ONE spawn point
    (Golden Rule #11 — instead of a seventh per-file stub copy).

    The launcher body is ``# pragma: no cover`` glue; its WIRING tests
    (``test_cli_run.py``) monkeypatch their own recorder/boom over this stub —
    a test-level ``monkeypatch.setattr`` overrides the belt, exactly like the
    AWS belt above. The sweeper's real behaviour stays covered by
    ``test_lake_heal.py`` (``run_sync`` + the ``spawn`` seam) and
    ``test_dashboard_lake_uniqueness.py``. Honest envelope: patched iff
    ``brutex.cli`` is already imported at test setup (every current
    ``cmd_dashboard`` caller imports it at module scope; the conditional keeps
    the redteam python-only lane — which never imports cli — import-free, the
    ``_isolate_run_forensics_mirror`` discipline).
    """
    cli_module = sys.modules.get("brutex.cli")
    if cli_module is not None:
        # No hasattr guard: a renamed launcher must fail LOUDLY here, not
        # silently revive the daemon-leak class.
        monkeypatch.setattr(cli_module, "_start_lake_heal_sweep", lambda *args, **kwargs: None)


@pytest.fixture
def _doctest_structlog_stderr() -> Iterator[None]:
    """Route structlog's default sink to STDERR for a doctest-running test.

    The doctest gates (``test_combo_mask_doctests.py`` +
    ``test_all_brutex_doctests.py``) execute examples under
    ``doctest.testmod``, which swaps ``sys.stdout`` for its capture buffer —
    so ANY background thread logging via unconfigured structlog (default
    ``PrintLogger`` writes to the current ``sys.stdout``) contaminates the
    example's ``Got:`` output (the PR #1666 shard-2 flake; the
    ``_lake_heal_sweep_daemon_belt`` above kills the known spawner, this
    fixture makes the gates immune to the whole class). SINK-level only —
    ``logger_factory`` → a ``PrintLoggerFactory`` bound to the ``sys.stderr``
    OBJECT captured here (doctest never touches stderr); processors, levels
    and event payloads are untouched (the #1630 sink-level posture), and the
    prior configuration is snapshot/restored (the ``capture_logs`` idiom).
    Honest envelope: a logger already CACHED under
    ``cache_logger_on_first_use=True`` by an earlier explicit configure would
    bypass the swap — no suite configuration does that (structlog's default
    is uncached lazy proxies, re-resolved per call).
    """
    saved = structlog.get_config()
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    try:
        yield
    finally:
        structlog.configure(**saved)


@pytest.fixture(autouse=True)
def _isolate_groww_token_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the on-disk Groww daily-token cache (DEC-GROWW-TOKEN-DISK-CACHE-001) at a
    per-test tmp file for the WHOLE suite, so no test ever reads/writes the real
    ``~/.brutex/cache/groww_access_token.json``.

    The cache-first cold-start ``SdkRefresher`` build (and every ``build_live_client`` /
    ``build_live_pool`` that constructs one) would otherwise touch the operator's home
    cache — leaking a fake test token to disk and making a token-bearing run order-
    dependent. A fresh, unwritten tmp path is always a clean cache MISS, so the byte-
    identical mint-at-construction behaviour the legacy tests pin is preserved. Set via
    the public ``BRUTEX_GROWW_TOKEN_CACHE_PATH`` env override (a per-test module/file
    fixture can still re-point it; ``monkeypatch`` auto-restores at teardown).
    """
    cache = tmp_path_factory.mktemp("groww-token-cache") / "groww_access_token.json"
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(cache))


@pytest.fixture(autouse=True)
def _isolate_run_forensics_mirror(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the Row-15 run-forensics mirror at a per-test tmp root for the suite.

    DEC-SWEEP-UNKILLABLE-CORE-001 Row 15: the sweep run core mirrors its R40
    forensics to ``~/.brutex/runs/<run_id>/`` on the INTERNAL disk by default.
    Point every test's mirror at a fresh tmp root (via the public
    ``BRUTEX_RUN_MIRROR_ROOT`` env override) so a run-core exercise NEVER writes
    into the operator's real home — the same isolation discipline as the Groww
    token cache above. A per-file fixture can still re-point or disable it (empty
    value); ``monkeypatch`` auto-restores at teardown.
    """
    # LITERAL env-var name, deliberately NOT `from brutex.engine.run_layout
    # import MIRROR_ROOT_ENV`: this fixture is suite-wide autouse, and the
    # nightly red-team harness (redteam-nightly.yml) runs a PYTHON-ONLY
    # editable install with no Rust wheels — importing brutex.engine here
    # (package __init__ -> combo_mask -> fno_math) errored the SETUP of all
    # 156 pre-existing tests/redteam tests (run 29145422566). The literal is
    # drift-pinned against run_layout.MIRROR_ROOT_ENV by
    # tests/unit/test_run_layout.py (a wheels-present suite).
    mirror = tmp_path_factory.mktemp("run-forensics-mirror")
    monkeypatch.setenv("BRUTEX_RUN_MIRROR_ROOT", str(mirror))


@pytest.fixture(autouse=True)
def _isolate_sweep_apriori_env() -> Iterator[None]:
    """Snapshot-restore ``BRUTEX_SWEEP_APRIORI`` around every test so the
    extinction default never leaks between tests in the one pytest process.

    DEC-SWEEP-APRIORI-DEFAULT-001: the CLI sweep dispatch (``cmd_sweep_all`` /
    the ``exploration`` ladder in ``brutex/cli.py``) writes
    ``os.environ["BRUTEX_SWEEP_APRIORI"] = "1"`` with a RAW assignment (not
    ``monkeypatch``) so spawn workers inherit the pruned path. In a real CLI
    invocation the process exits and nothing leaks; but the suite drives that
    dispatch IN-PROCESS, and the raw write is invisible to a test's own
    ``monkeypatch`` teardown — so without this guard the flag leaks into every
    later test in the same process and silently engages the Apriori frontier
    (reproduced: the ``test_engine_sweep_run`` / ``test_engine_sweep_parallel`` /
    ``test_engine_sweep_run_core`` census + the ``test_sweep_runcore_byte_identity``
    goldens diverge — 12 order-dependent failures in the full-suite coverage
    gate, all green in isolation). This is the harness twin of the dashboard's
    own snapshot-restore law (``run_sweep_from_request``, ruling 5) applied at
    the pytest boundary so EVERY in-process caller of the dispatch is covered,
    and it never fights a test's deliberate ``monkeypatch.setenv`` of the same
    var (autouse setup precedes the test's own ``monkeypatch``, so the snapshot
    is the true pre-test value and both restore to it).

    LITERAL env-var name (drift-pinned against
    ``brutex.engine.sweep_apriori.SWEEP_APRIORI_ENV`` in
    ``tests/unit/test_engine_sweep_apriori.py``), deliberately NOT an import:
    this fixture is suite-wide autouse and the redteam nightly harness
    (redteam-nightly.yml) runs a PYTHON-ONLY editable install with no Rust
    wheels, so importing ``brutex.engine`` here would error the setup of every
    redteam test — the same reason ``_isolate_run_forensics_mirror`` above uses
    a literal.
    """
    env_name = "BRUTEX_SWEEP_APRIORI"
    original = os.environ.get(env_name)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original


@pytest.fixture(autouse=True)
def _testclient_loopback_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every ``TestClient`` at ``base_url="http://localhost"`` by default (R2-DS-F1).

    The dashboard's production ``Host`` allowlist is loopback-ONLY: the
    ``testserver`` convenience name was removed from
    ``brutex.dashboard.app.DEFAULT_ALLOWED_HOSTS`` because an attacker who can
    make that single-label name resolve to the operator machine (malicious
    DHCP search-domain / intranet DNS — the rebinding class the guard closes)
    would bypass it. Starlette's ``TestClient`` still defaults to
    ``http://testserver``, so this fixture re-points the DEFAULT at the
    allowlisted ``http://localhost`` for the whole suite — the test-side home
    of the old convenience. A test that passes its own ``base_url``
    (positional or keyword) is left untouched, and tests may still inject
    ``allowed_hosts`` into ``build_app`` explicitly. No-op when starlette is
    not installed (suites that never touch the dashboard).

    WebSocket twin: ``TestClient.websocket_connect`` HARDCODES
    ``urljoin("ws://testserver", url)`` (starlette ``testclient.py``, ignoring
    ``base_url`` entirely), so the WS handshake's ``Host`` header reads
    ``testserver`` and the loopback-only ``TrustedHostMiddleware`` would 400
    every suite WS connect. The same patch discipline applies: default the
    handshake ``host`` header to ``localhost`` unless the test supplies its
    own (a test probing the guard itself passes an explicit ``host``).
    """
    try:
        from starlette.testclient import TestClient
    except ImportError:
        return
    original_init = TestClient.__init__

    def _loopback_init(self: TestClient, *args: Any, **kwargs: Any) -> None:
        # args[0] is the ASGI app; a positional base_url would be args[1].
        if len(args) < 2 and "base_url" not in kwargs:
            kwargs["base_url"] = "http://localhost"
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", _loopback_init)

    original_ws_connect = TestClient.websocket_connect

    def _loopback_ws_connect(self: TestClient, url: str, *args: Any, **kwargs: Any) -> Any:
        headers = dict(kwargs.get("headers") or {})
        if not any(key.lower() == "host" for key in headers):
            headers["host"] = "localhost"
        kwargs["headers"] = headers
        return original_ws_connect(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "websocket_connect", _loopback_ws_connect)


# ────────────────────────────────────────────────────────────────────────
# Raw-row fixtures mimicking the shape of a broker's pandas DataFrame
# rows. These are dicts (same shape returned by `df.to_dict('records')[i]`).
# Used by orchestrator/data unit tests as generic instrument rows. They
# include an `internal_trading_symbol` column observed in legacy adapter
# output; new adapters do not need to expose it. Phase 2 will reshape
# these fixtures around Groww's response format.
# ────────────────────────────────────────────────────────────────────────


# raw_equity_row_reliance fixture removed in DEC-P0-032 (PR #50e).
# Orphan fixture — zero callers per Agent 4 of PR #50e pre-impl review.
# Track 1 is index-only per CLAUDE.md §3; EQ-rejection-path coverage is
# already provided by inline `_make_equity` helpers in test_universe.py.


@pytest.fixture
def raw_index_row_nifty() -> dict[str, Any]:
    """Spot NIFTY index row."""
    return {
        "exchange": "NSE",
        "exchange_token": 26000,
        "trading_symbol": "NIFTY",
        "vendor_symbol": "NSE-NIFTY",
        "name": "Nifty 50",
        "instrument_type": "IDX",
        "segment": "CASH",
        "series": "",
        "isin": "",
        "underlying_symbol": "",
        "underlying_exchange_token": float("nan"),
        "lot_size": 1,
        "expiry_date": "",
        "strike_price": float("nan"),
        "tick_size": 0.05,  # float rupees form (CSV-text form)
        "freeze_quantity": float("nan"),
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "NIFTY",
    }


@pytest.fixture
def raw_banknifty_ce_row_dec2025() -> dict[str, Any]:
    """Matches the real BANKNIFTY Dec 2025 27000 CE row from the CSV sample.

    lot_size 35 (post-SEBI-revision — see https://groww.in/trade-api/docs/docs/v2/annexure).
    """
    return {
        "exchange": "NSE",
        "exchange_token": 49445,
        "trading_symbol": "BANKNIFTY25DEC27000PE",
        "vendor_symbol": "NSE-BANKNIFTY-24Dec25-27000-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 35,
        "expiry_date": "2025-12-24",
        "strike_price": 27000,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25DEC27000PE",
    }


@pytest.fixture
def raw_banknifty_ce_row_jun2025() -> dict[str, Any]:
    """Matches the real BANKNIFTY Jun 2025 35400 PE row — lot_size 30
    (pre-SEBI-revision). Used to prove temporal lot-size tracking.
    """
    return {
        "exchange": "NSE",
        "exchange_token": 41942,
        "trading_symbol": "BANKNIFTY25JUN35400PE",
        "vendor_symbol": "NSE-BANKNIFTY-26Jun25-35400-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 30,
        "expiry_date": "2025-06-26",
        "strike_price": 35400,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25JUN35400PE",
    }


@pytest.fixture
def raw_sensex_ce_row() -> dict[str, Any]:
    """BSE SENSEX weekly Tuesday option — required for BSE scope coverage."""
    return {
        "exchange": "BSE",
        "exchange_token": 86000,
        "trading_symbol": "SENSEX25SEP79500CE",
        "vendor_symbol": "BSE-SENSEX-25Sep25-79500-CE",
        "name": float("nan"),
        "instrument_type": "CE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "SENSEX",
        "underlying_exchange_token": 1,
        "lot_size": 20,
        "expiry_date": "2025-09-25",
        "strike_price": 79500,
        "tick_size": 0.05,
        "freeze_quantity": 1000,
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "SENSEX25SEP79500CE",
    }


@pytest.fixture
def nifty_option_instance() -> Instrument:
    """A pre-built Instrument useful for universe/scope tests."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=35044,
        symbol_name="NIFTY25MAR29050PE",
        display_name=None,
        instrument=InstrumentType.PE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=26000,
        lot_size=25,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=7200,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("29050"),
        option_type="PE",
        expiry_flag="W",
        asm_gsm_flag=True,
    )
