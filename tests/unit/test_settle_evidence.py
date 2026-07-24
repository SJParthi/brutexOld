# pyright: reportPrivateUsage=none
"""``brutex.calendars.settle_evidence`` — the measured-settle evidence channel.

DEC-SETTLE-EVIDENCE-ADOPTION-001 (MIGRATION.md #1793): the schema-1 validator
(the §6 truth table), the stat-token-memoized loader over the r7-hardened
regular-file-only reader (explicit-path junk fails LOUD — the resolve_* law;
default-path corruption WARNs once + falls back to the conservative 17:30),
the committed-file CI validation tier, and the probe ``--emit-evidence``
subprocess round-trip golden (synthetic transcripts → the REAL emitter → the
REAL validator — no Groww needed). 100% line+branch standalone (the ci.yml
module gate).

Default-tier tests redirect the repo path via
``monkeypatch.setattr(settle_evidence, "default_evidence_path", ...)`` (the
§3.1 call-time-lookup seam) — NO test reaches the real repo default except
the ONE committed-file test, which bypasses env entirely and passes in BOTH
worlds (absent → pass; present → must validate).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from brutex.calendars import settle_evidence
from brutex.calendars.auto_pull_gate import PULL_ALL_SETTLE_ENV
from brutex.calendars.calendar_rust import Raise, Value, unwrap
from brutex.calendars.market_hours import BLOCK_END
from brutex.calendars.settle_evidence import (
    DEFAULT_EVIDENCE_RELPATH,
    MAX_EVIDENCE_BYTES,
    MAX_MARGIN_MINUTES,
    MIN_EVIDENCE_DAYS,
    MIN_MARGIN_MINUTES,
    SETTLE_EVIDENCE_KIND,
    SETTLE_EVIDENCE_OFF,
    SETTLE_EVIDENCE_PATH_ENV,
    SETTLE_EVIDENCE_SCHEMA,
    STALE_EVIDENCE_AFTER_DAYS,
    SettleEvidence,
    default_evidence_path,
    evidence_settle,
    resolve_evidence_path,
    validate_evidence_payload,
)
from brutex.data.sidecar_read import SidecarReadResult
from structlog.testing import capture_logs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE = _REPO_ROOT / "docs" / "groww" / "probe" / "postclose_settle_probe.py"


@pytest.fixture(autouse=True)
def _fresh_memo() -> Generator[None]:
    """Isolate the process-wide single-slot memo between tests."""
    settle_evidence._reset_evidence_memo_for_tests()
    yield
    settle_evidence._reset_evidence_memo_for_tests()


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None]:
    """SIGALRM watchdog — a blocked read fails LOUDLY instead of hanging."""

    def _boom(_signum: int, _frame: object) -> None:
        raise AssertionError(f"evidence read blocked > {seconds}s — the P-1 hang is back")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def make_evidence_payload(**overrides: object) -> dict[str, object]:
    """The canonical VALID schema-1 payload (single-field mutations in tests).

    Shared with the gate / scheduler / CLI suites (imported cross-file — the
    ``_cv_report`` precedent), so every consumer mutates ONE builder.
    """
    base_day = date(2026, 7, 13)  # Monday; 5 consecutive weekdays Mon-Fri
    days = [(base_day + timedelta(days=i)).isoformat() for i in range(5)]
    per_day: dict[str, object] = {
        day: {
            "15:35": {
                "records": 14,
                "all_pass": False,
                "failed_checks": ["rows_all_len7", "count_ok"],
            },
            "15:45": {"records": 14, "all_pass": True, "failed_checks": []},
        }
        for day in days
    }
    payload: dict[str, object] = {
        "schema": 1,
        "kind": "groww-postclose-settle-evidence",
        "earliest_all_pass_ist": "15:45",
        "adopted_settle_ist": "16:00",
        "margin_minutes": 15,
        "distinct_trading_days": 5,
        "per_day": per_day,
        "generated_at_utc": "2026-07-13T12:31:04+00:00",
        "probe_script_version": "1.1",
        "checks": ["rows_all_len7", "count_ok", "last_bar_ok", "fut_fields_ok"],
        "slots_observed": ["15:35", "15:45"],
        "transcripts": ["postclose-2026-07-13-1535.jsonl"],
        "excluded_days": {"2026-07-18": "non_regular_session (expected_minutes=0)"},
        "ignored_records": 0,
    }
    payload.update(overrides)
    return payload


def write_evidence(path: Path, **overrides: object) -> Path:
    """Write a (by-default valid) evidence artifact to *path*."""
    path.write_text(
        json.dumps(make_evidence_payload(**overrides), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


# ── constants / path resolution ──────────────────────────────────────────


def test_default_evidence_path_is_repo_docs_groww_probe() -> None:
    """The parents[2] law (the dotenv._default_repo_root precedent)."""
    resolved = default_evidence_path()
    assert resolved == _REPO_ROOT / DEFAULT_EVIDENCE_RELPATH
    assert resolved.as_posix().endswith("docs/groww/probe/settle_evidence.json")


def test_settle_override_env_literal_matches_the_gate_constant() -> None:
    """The ``settle_evidence.valid`` detail's env literal is a mirror of
    ``auto_pull_gate.PULL_ALL_SETTLE_ENV`` (a literal, not an import — the
    gate imports THIS module, so an import back would cycle); drift-pinned."""
    assert settle_evidence._SETTLE_OVERRIDE_ENV == PULL_ALL_SETTLE_ENV


def test_resolve_evidence_path_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_evidence_path({}) == (default_evidence_path(), False)
    assert resolve_evidence_path({SETTLE_EVIDENCE_PATH_ENV: "   "}) == (
        default_evidence_path(),
        False,
    )
    for token in ("off", "OFF", "Off"):
        assert resolve_evidence_path({SETTLE_EVIDENCE_PATH_ENV: token}) == (None, False)
    assert resolve_evidence_path({SETTLE_EVIDENCE_PATH_ENV: "/x/e.json"}) == (
        Path("/x/e.json"),
        True,
    )
    # env=None reads os.environ (the conftest belt writes "off" there; an
    # explicit setenv proves the live read).
    monkeypatch.setenv(SETTLE_EVIDENCE_PATH_ENV, "/y/e.json")
    assert resolve_evidence_path() == (Path("/y/e.json"), True)
    assert SETTLE_EVIDENCE_OFF == "off"
    assert SETTLE_EVIDENCE_SCHEMA == 1
    assert SETTLE_EVIDENCE_KIND == "groww-postclose-settle-evidence"


def test_resolve_evidence_path_default_seam_is_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[§3.1] ``default_evidence_path`` is looked up as a module global at
    CALL time — the monkeypatchable seam every default-tier test rides."""
    target = tmp_path / "seam.json"
    monkeypatch.setattr(settle_evidence, "default_evidence_path", lambda: target)
    assert resolve_evidence_path({}) == (target, False)


# ── validate_evidence_payload (§6 truth-table rows 9-23) ─────────────────


class TestValidateEvidencePayload:
    def test_valid_full(self) -> None:
        ev = validate_evidence_payload(make_evidence_payload(), path="p.json")
        assert ev == SettleEvidence(
            adopted=time(16, 0),
            earliest_all_pass="15:45",
            margin_minutes=15,
            distinct_trading_days=5,
            generated_at_utc="2026-07-13T12:31:04+00:00",
            probe_script_version="1.1",
            path="p.json",
        )

    def test_valid_minimal_without_informational_keys(self) -> None:
        payload = make_evidence_payload()
        for key in ("checks", "slots_observed", "transcripts", "excluded_days", "ignored_records"):
            del payload[key]
        assert validate_evidence_payload(payload, path="p.json").adopted == time(16, 0)

    def test_unknown_extra_keys_ignored(self) -> None:
        payload = make_evidence_payload(some_future_key={"nested": True})
        assert validate_evidence_payload(payload, path="p.json").margin_minutes == 15

    def test_floor_boundary_15_35_exact_allowed(self) -> None:
        """Row 15's boundary: 15:35-exact ADOPTS (the env-tier floor mirrored)."""
        base_day = date(2026, 7, 13)
        per_day: dict[str, object] = {
            (base_day + timedelta(days=i)).isoformat(): {
                "15:25": {"records": 14, "all_pass": True, "failed_checks": []}
            }
            for i in range(5)
        }
        payload = make_evidence_payload(
            earliest_all_pass_ist="15:25",
            adopted_settle_ist="15:35",
            margin_minutes=10,
            per_day=per_day,
        )
        ev = validate_evidence_payload(payload, path="p.json")
        assert ev.adopted == BLOCK_END

    @pytest.mark.parametrize("root", [[], "x", 42, None, True])
    def test_non_object_root(self, root: object) -> None:
        with pytest.raises(ValueError, match="not a JSON object"):
            validate_evidence_payload(root, path="p.json")

    @pytest.mark.parametrize(
        ("mutation", "match"),
        [
            ({"schema": True}, "schema must be exactly 1"),
            ({"schema": "1"}, "schema must be exactly 1"),
            ({"schema": 2}, "schema must be exactly 1"),
            # Refuter r3 F-R3a-1: a JSON float 1.0 == 1 numerically — the
            # pre-fix guard refused bool but not float (unlike _require_int),
            # so 1.0 (and 1e0) VALIDATED; the exact-int guard refuses it.
            ({"schema": 1.0}, "schema must be exactly 1"),
            ({"schema": None}, "schema must be exactly 1"),
            ({"kind": "something-else"}, "kind must be"),
            ({"earliest_all_pass_ist": None}, "earliest_all_pass_ist must be a string"),
            ({"earliest_all_pass_ist": "١٥:٤٥"}, "not strict ASCII HH:MM"),
            ({"earliest_all_pass_ist": "3pm"}, "not strict ASCII HH:MM"),
            # Refuter r1 F-R1-1: under ``.match`` the ``$`` anchor matches
            # BEFORE a single trailing newline — pre-fix this fell through the
            # shape check (only the support law caught the mutated field);
            # the fullmatch sweep refuses it AT the shape check.
            ({"earliest_all_pass_ist": "15:45\n"}, "not strict ASCII HH:MM"),
            (
                {"earliest_all_pass_ist": "24:00", "adopted_settle_ist": "24:15"},
                "not a real wall-clock time",
            ),
            ({"adopted_settle_ist": 1600}, "adopted_settle_ist must be a string"),
            ({"adopted_settle_ist": "16:60"}, "not a real wall-clock time"),
            ({"margin_minutes": True}, "bool refused"),
            ({"margin_minutes": "15"}, "must be an int"),
            ({"margin_minutes": 9}, "below the minimum 10"),
            (
                {"margin_minutes": 241, "adopted_settle_ist": "19:46"},
                "above the maximum 240",
            ),
            (
                {
                    "earliest_all_pass_ist": "23:55",
                    "adopted_settle_ist": "00:05",
                    "margin_minutes": 10,
                },
                "wraps past midnight",
            ),
            ({"adopted_settle_ist": "16:01"}, "must equal earliest_all_pass_ist"),
            (
                {
                    "earliest_all_pass_ist": "15:20",
                    "adopted_settle_ist": "15:30",
                    "margin_minutes": 10,
                },
                "before the market-hours guard's BLOCK_END",
            ),
            ({"distinct_trading_days": True}, "bool refused"),
            ({"distinct_trading_days": "5"}, "must be an int"),
            ({"distinct_trading_days": 4}, "below the minimum 5"),
            ({"per_day": None}, "per_day must be an object"),
            ({"per_day": {}}, "per_day is empty"),
            ({"generated_at_utc": None}, "generated_at_utc must be an ISO-8601 string"),
            ({"generated_at_utc": "yesterday"}, "is not ISO-8601"),
            ({"generated_at_utc": "2026-07-13T12:31:04"}, "tz-naive"),
            # r1 correctness NIT-1: aware-but-non-UTC refused — the field
            # NAME promises UTC and the emitter always writes UTC.
            ({"generated_at_utc": "2026-07-13T18:01:04+05:30"}, "must be UTC"),
            ({"probe_script_version": None}, "probe_script_version must be a non-empty"),
            ({"probe_script_version": ""}, "probe_script_version must be a non-empty"),
            ({"probe_script_version": 1.1}, "probe_script_version must be a non-empty"),
            ({"checks": "rows_all_len7"}, "checks must be a list of strings"),
            ({"slots_observed": ["15:45", 1600]}, "slots_observed must be a list of strings"),
            ({"transcripts": [None]}, "transcripts must be a list of strings"),
            ({"excluded_days": ["x"]}, "excluded_days must be an object of strings"),
            ({"excluded_days": {"2026-07-18": 0}}, "excluded_days must be an object of strings"),
            ({"ignored_records": True}, "bool refused"),
            ({"ignored_records": -1}, "below the minimum 0"),
        ],
    )
    def test_single_field_mutations_refused(self, mutation: dict[str, object], match: str) -> None:
        payload = make_evidence_payload(**mutation)
        with pytest.raises(ValueError, match=match) as excinfo:
            validate_evidence_payload(payload, path="mut.json")
        # Every refusal names the path, repr()'d — the #907 convention shared
        # with _absent_error (r1 security F2).
        assert "'mut.json'" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("per_day", "match"),
        [
            ({1: {"15:45": {}}}, "is not an ISO calendar day"),
            ({"2026/07/13": {}}, "is not an ISO calendar day"),
            # Refuter r1 F-R1-1: a trailing newline sailed past the ``.match``
            # ``$`` anchor — the DAY key was pre-fix caught only by the
            # strptime realness belt ("not a real calendar day"), the SLOT key
            # by nothing at all; both now refuse AT the shape check.
            ({"2026-07-10\n": {}}, "is not an ISO calendar day"),
            ({"2026-07-13": {"15:45\n": {}}}, "is not HH:MM"),
            ({"2026-02-30": {}}, "is not a real calendar day"),
            ({"2026-07-13": "x"}, "must be a non-empty object of slots"),
            ({"2026-07-13": {}}, "must be a non-empty object of slots"),
            ({"2026-07-13": {2: {}}}, "is not HH:MM"),
            ({"2026-07-13": {"3pm": {}}}, "is not HH:MM"),
            ({"2026-07-13": {"15:45": "x"}}, "must be an object"),
            (
                {"2026-07-13": {"15:45": {"records": 0, "all_pass": True, "failed_checks": []}}},
                "below the minimum 1",
            ),
            (
                {"2026-07-13": {"15:45": {"records": True, "all_pass": True, "failed_checks": []}}},
                "bool refused",
            ),
            (
                {"2026-07-13": {"15:45": {"records": 14, "all_pass": 1, "failed_checks": []}}},
                "all_pass must be a bool",
            ),
            (
                {"2026-07-13": {"15:45": {"records": 14, "all_pass": True, "failed_checks": "x"}}},
                "failed_checks must be a list of strings",
            ),
        ],
    )
    def test_per_day_shape_refusals(self, per_day: object, match: str) -> None:
        payload = make_evidence_payload(per_day=per_day)
        # A too-small per_day also violates the support law — the shape
        # refusal must fire FIRST (shape before arithmetic), so `match` pins it.
        with pytest.raises(ValueError, match=match):
            validate_evidence_payload(payload, path="p.json")

    def test_trailing_newline_repro_payload_refused(self) -> None:
        """Refuter r1 F-R1-1 — the executed repro's EXACT previously-PASSING
        payload now yields invalid: ``earliest_all_pass_ist="15:45\\n"`` with
        matching newline slot keys (so the support law held) VALIDATED at the
        pre-fix head under the ``.match`` ``$``-before-trailing-newline quirk,
        and the raw newline reached the ``settle_evidence.valid`` INFO + the
        gate provenance un-repr'd. The fullmatch sweep refuses it at the
        shape check — no content field can carry a trailing newline."""
        payload = make_evidence_payload(earliest_all_pass_ist="15:45\n")
        per_day = payload["per_day"]
        assert isinstance(per_day, dict)
        payload["per_day"] = {
            day: {"15:45\n": {"records": 1, "all_pass": True, "failed_checks": []}}
            for day in per_day
        }
        with pytest.raises(ValueError, match="not strict ASCII HH:MM") as excinfo:
            validate_evidence_payload(payload, path="repro.json")
        assert "'15:45\\n'" in str(excinfo.value)  # the value reprs, newline escaped

    def test_per_day_support_count_must_equal_distinct_days(self) -> None:
        """Row 18: the earliest slot must be observed-and-passing on EXACTLY
        ``distinct_trading_days`` days."""
        payload = make_evidence_payload()
        per_day = payload["per_day"]
        assert isinstance(per_day, dict)
        first_day = sorted(per_day)[0]
        per_day[first_day]["15:45"]["all_pass"] = False  # type: ignore[index]
        with pytest.raises(ValueError, match="per_day support .* is 4 day\\(s\\)"):
            validate_evidence_payload(payload, path="p.json")

    def test_probe_script_version_not_exact_matched(self) -> None:
        """Row 22: any non-empty version string adopts (informational)."""
        payload = make_evidence_payload(probe_script_version="2.0")
        assert validate_evidence_payload(payload, path="p.json").probe_script_version == "2.0"

    def test_generated_at_utc_accepts_both_utc_spellings(self) -> None:
        """[r1 NIT-1 boundary] UTC-exact acceptance covers BOTH ISO spellings
        (``Z`` and ``+00:00``) — the tightening refuses only non-zero offsets."""
        for token in ("2026-07-13T12:31:04Z", "2026-07-13T12:31:04+00:00"):
            ev = validate_evidence_payload(
                make_evidence_payload(generated_at_utc=token), path="p.json"
            )
            assert ev.generated_at_utc == token


# ── evidence_settle — the DEFAULT tier (truth-table col D) ───────────────


class TestEvidenceSettleDefaultTier:
    """Every corruption shape → ONE ``settle_evidence.invalid`` WARN per
    stat-token then the honest ``None`` (tier-3 17:30 fallback) — the r6/r7
    "never crash OR hang the boot" law. Default-path redirection rides the
    §3.1 ``default_evidence_path`` monkeypatch seam."""

    def _bind_default(self, monkeypatch: pytest.MonkeyPatch, target: Path) -> dict[str, str]:
        monkeypatch.setattr(settle_evidence, "default_evidence_path", lambda: target)
        return {}  # an env WITHOUT the path key → the default tier

    def test_default_path_absent_is_silent_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The wheel-install behavioral pin: an absent default path is a
        SILENT ``None`` — zero log records (the per-poll path)."""
        env = self._bind_default(monkeypatch, tmp_path / "missing" / "e.json")
        with capture_logs() as logs:
            assert evidence_settle(env) is None
            assert evidence_settle(env) is None  # re-probed, still silent
        assert logs == []

    def test_off_token_is_silent_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert evidence_settle({SETTLE_EVIDENCE_PATH_ENV: "off"}) is None
        # env=None reads os.environ — the conftest belt's "off" applies.
        monkeypatch.setenv(SETTLE_EVIDENCE_PATH_ENV, "off")
        assert evidence_settle() is None

    def test_valid_adopts_with_one_info_per_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = write_evidence(tmp_path / "e.json")
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            first = evidence_settle(env)
            second = evidence_settle(env)  # the memo hit — no second INFO
        assert isinstance(first, SettleEvidence)
        assert first.adopted == time(16, 0)
        assert second is first
        events = [e for e in logs if e["event"] == "settle_evidence.valid"]
        assert len(events) == 1
        assert events[0]["adopted_settle_ist"] == "16:00"
        assert events[0]["earliest_all_pass_ist"] == "15:45"
        assert events[0]["margin_minutes"] == 15
        assert events[0]["distinct_trading_days"] == 5
        assert events[0]["path"] == str(target)
        assert "adopted whenever BRUTEX_PULL_ALL_SETTLE_IST is unset" in str(events[0]["detail"])
        assert [e for e in logs if e["event"] == "settle_evidence.stale"] == []

    def test_stale_adopts_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Row 20: a >180-day-old valid file still ADOPTS + one stale WARN."""
        old = (datetime.now(UTC) - timedelta(days=STALE_EVIDENCE_AFTER_DAYS + 30)).isoformat()
        target = write_evidence(tmp_path / "e.json", generated_at_utc=old)
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            first = evidence_settle(env)
            evidence_settle(env)  # memo hit — no re-WARN
        assert isinstance(first, SettleEvidence)
        stale = [e for e in logs if e["event"] == "settle_evidence.stale"]
        assert len(stale) == 1
        assert stale[0]["stale_after_days"] == STALE_EVIDENCE_AFTER_DAYS
        # The file-content field is repr()'d AT SOURCE (the #907 convention,
        # r1 security F1 — fromisoformat accepts ANY single separator char,
        # so even a validated value could carry e.g. a raw newline).
        assert stale[0]["generated_at_utc"] == repr(old)
        assert "still ADOPTED" in str(stale[0]["detail"])

    @pytest.mark.parametrize(
        ("body", "token"),
        [
            (b"\xffnot-utf8", "undecodable"),
            (b"{not json", "undecodable"),
            (b"[1, 2, 3]", "not a JSON object"),
            (b'{"schema": 2}', "schema must be exactly 1"),
        ],
    )
    def test_corrupt_shapes_warn_once_then_fall_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        body: bytes,
        token: str,
    ) -> None:
        target = tmp_path / "e.json"
        target.write_bytes(body)
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            assert evidence_settle(env) is None
            assert evidence_settle(env) is None  # memoized — silent
        warns = [e for e in logs if e["event"] == "settle_evidence.invalid"]
        assert len(warns) == 1
        assert token in str(warns[0]["detail"])
        assert "17:30" in str(warns[0]["fallback"])

    def test_float_schema_full_valid_file_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[refuter r3 F-R3a-1] the executed end-to-end repro shape: an
        otherwise fully VALID artifact whose ``"schema"`` is the JSON float
        ``1.0`` (``json.loads`` yields a float; numerically == 1) VALIDATED
        and was ADOPTED end-to-end at the pre-fix head — the exact-int guard
        now takes the default tier's corrupt path: ONE WARN + the
        conservative 17:30 fallback, never adoption."""
        target = write_evidence(tmp_path / "e.json", schema=1.0)
        assert json.loads(target.read_text(encoding="utf-8"))["schema"] == 1.0
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            assert evidence_settle(env) is None
        warns = [e for e in logs if e["event"] == "settle_evidence.invalid"]
        assert len(warns) == 1
        assert "schema must be exactly 1, got 1.0" in str(warns[0]["detail"])
        assert "17:30" in str(warns[0]["fallback"])

    def test_nesting_bomb_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The r6 N-1 ``RecursionError`` shape folds into ``undecodable``."""
        depth = 200_000
        target = tmp_path / "e.json"
        target.write_text("[" * depth + "]" * depth, encoding="utf-8")
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            assert evidence_settle(env) is None
        assert [e["event"] for e in logs] == ["settle_evidence.invalid"]

    def test_fifo_no_hang_warns_not_regular(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[the r7 P-1 pin] a FIFO at the default path returns within bound."""
        target = tmp_path / "e.json"
        os.mkfifo(target)
        env = self._bind_default(monkeypatch, target)
        with _hang_watchdog(), capture_logs() as logs:
            assert evidence_settle(env) is None
        warns = [e for e in logs if e["event"] == "settle_evidence.invalid"]
        assert len(warns) == 1
        assert "not a regular file" in str(warns[0]["detail"])

    def test_directory_warns_not_regular(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "e.json"
        target.mkdir()
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            assert evidence_settle(env) is None
        assert [e["event"] for e in logs] == ["settle_evidence.invalid"]

    def test_device_symlink_warns_not_regular(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        if not Path("/dev/zero").exists():  # pragma: no cover — POSIX always has it
            pytest.skip("/dev/zero not available on this platform")
        target = tmp_path / "e.json"
        target.symlink_to("/dev/zero")
        env = self._bind_default(monkeypatch, target)
        with _hang_watchdog(), capture_logs() as logs:
            assert evidence_settle(env) is None
        assert [e["event"] for e in logs] == ["settle_evidence.invalid"]

    def test_oversized_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "e.json"
        target.write_bytes(b" " * (MAX_EVIDENCE_BYTES + 1))
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            assert evidence_settle(env) is None
        warns = [e for e in logs if e["event"] == "settle_evidence.invalid"]
        assert len(warns) == 1
        assert "oversized" in str(warns[0]["detail"])

    def test_stat_fault_warns_once_under_degenerate_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stat-level OSError (no content token exists) folds into the
        read_error family under the degenerate token — WARN once, not per poll."""
        target = write_evidence(tmp_path / "e.json")
        env = self._bind_default(monkeypatch, target)
        real_stat = os.stat

        def _boom_stat(p: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if str(p) == str(target):
                raise PermissionError(13, "Permission denied")
            return real_stat(p, *args, **kwargs)

        monkeypatch.setattr(settle_evidence.os, "stat", _boom_stat)
        with capture_logs() as logs:
            assert evidence_settle(env) is None
            assert evidence_settle(env) is None  # memoized degenerate token
        warns = [e for e in logs if e["event"] == "settle_evidence.invalid"]
        assert len(warns) == 1
        assert "PermissionError" in str(warns[0]["detail"])

    def test_raced_away_between_stat_and_open_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Stat OK then the open reads ABSENT (an out-of-band unlink) — the
        silent absent answer, nothing memoized."""
        target = write_evidence(tmp_path / "e.json")
        env = self._bind_default(monkeypatch, target)
        monkeypatch.setattr(
            settle_evidence,
            "read_sidecar_bytes",
            lambda _path, *, max_bytes: SidecarReadResult(shape="absent"),
        )
        with capture_logs() as logs:
            assert evidence_settle(env) is None
        assert logs == []

    def test_replaced_file_revalidates_and_relogs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Row 25: a stat-token move (replaced file) → fresh validate + log."""
        target = write_evidence(tmp_path / "e.json")
        env = self._bind_default(monkeypatch, target)
        with capture_logs() as logs:
            first = evidence_settle(env)
            # Replace with a DIFFERENT valid artifact (size + mtime move).
            write_evidence(
                target,
                earliest_all_pass_ist="16:00",
                adopted_settle_ist="16:30",
                margin_minutes=30,
                per_day={
                    d: {"16:00": {"records": 14, "all_pass": True, "failed_checks": []}}
                    for d in [(date(2026, 7, 13) + timedelta(days=i)).isoformat() for i in range(5)]
                },
            )
            second = evidence_settle(env)
        assert isinstance(first, SettleEvidence)
        assert isinstance(second, SettleEvidence)
        assert second.adopted == time(16, 30)
        # Scoped to the settle loader's OWN events: the Rust routing facade
        # (calendar_rust, default-ON DEC-RUST-CALENDAR-FACADE-FLIP-001) emits a
        # benign one-time ``calendar_rust.engaged`` INFO on the first flag-ON
        # engaged call — out of THIS loader-contract assertion's scope.
        settle_events = [e["event"] for e in logs if e["event"].startswith("settle_evidence.")]
        assert settle_events == ["settle_evidence.valid", "settle_evidence.valid"]

    def test_deleted_mid_process_reads_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Row 26: a valid file deleted mid-process → the next call is absent."""
        target = write_evidence(tmp_path / "e.json")
        env = self._bind_default(monkeypatch, target)
        assert isinstance(evidence_settle(env), SettleEvidence)
        target.unlink()
        with capture_logs() as logs:
            assert evidence_settle(env) is None
        assert logs == []


# ── evidence_settle — the EXPLICIT tier (truth-table col X) ──────────────


class TestEvidenceSettleExplicitTier:
    """An operator-typed :data:`SETTLE_EVIDENCE_PATH_ENV` fails LOUD on every
    junk shape — :class:`ValueError` naming the env var (the resolve_* law)."""

    def test_absent_raises_naming_env(self, tmp_path: Path) -> None:
        env = {SETTLE_EVIDENCE_PATH_ENV: str(tmp_path / "missing.json")}
        with pytest.raises(ValueError, match=SETTLE_EVIDENCE_PATH_ENV):
            evidence_settle(env)

    def test_dangling_symlink_raises(self, tmp_path: Path) -> None:
        link = tmp_path / "dangling.json"
        link.symlink_to(tmp_path / "gone.json")
        with pytest.raises(ValueError, match="absent"):
            evidence_settle({SETTLE_EVIDENCE_PATH_ENV: str(link)})

    def test_fifo_raises_no_hang(self, tmp_path: Path) -> None:
        target = tmp_path / "e.json"
        os.mkfifo(target)
        with _hang_watchdog(), pytest.raises(ValueError, match="not a regular file"):
            evidence_settle({SETTLE_EVIDENCE_PATH_ENV: str(target)})

    def test_oversized_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "e.json"
        target.write_bytes(b" " * (MAX_EVIDENCE_BYTES + 1))
        with pytest.raises(ValueError, match="oversized"):
            evidence_settle({SETTLE_EVIDENCE_PATH_ENV: str(target)})

    def test_junk_json_raises_and_memo_reraises(self, tmp_path: Path) -> None:
        target = tmp_path / "e.json"
        target.write_bytes(b"{junk")
        env = {SETTLE_EVIDENCE_PATH_ENV: str(target)}
        with pytest.raises(ValueError, match="undecodable"):
            evidence_settle(env)
        # The memo hit re-raises the SAME refusal (never a silent adopt).
        with pytest.raises(ValueError, match=SETTLE_EVIDENCE_PATH_ENV):
            evidence_settle(env)

    def test_validation_failure_raises(self, tmp_path: Path) -> None:
        target = write_evidence(tmp_path / "e.json", distinct_trading_days=4)
        with pytest.raises(ValueError, match="below the minimum 5"):
            evidence_settle({SETTLE_EVIDENCE_PATH_ENV: str(target)})

    def test_raced_away_explicit_raises_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = write_evidence(tmp_path / "e.json")
        monkeypatch.setattr(
            settle_evidence,
            "read_sidecar_bytes",
            lambda _path, *, max_bytes: SidecarReadResult(shape="absent"),
        )
        with pytest.raises(ValueError, match="absent"):
            evidence_settle({SETTLE_EVIDENCE_PATH_ENV: str(target)})

    def test_nul_byte_path_raises_wrapped_naming_env(self) -> None:
        """[r1 correctness NIT-2] an embedded NUL byte makes ``os.stat`` raise
        a raw ValueError — folded into the module's uniform refusal naming the
        env var (fail-loud direction preserved, never an unwrapped raw raise)."""
        env = {SETTLE_EVIDENCE_PATH_ENV: "/tmp/evi\x00dence.json"}
        with pytest.raises(ValueError, match=SETTLE_EVIDENCE_PATH_ENV) as excinfo:
            evidence_settle(env)
        msg = str(excinfo.value)
        # Pins that the RAW ValueError class was folded (not OSError).
        assert "unreadable (ValueError:" in msg
        # Common substring across CPython patch versions: 3.12.3 renders
        # "embedded null byte" while 3.12.13+/3.13 render
        # "stat: embedded null character in path" — never pin the
        # interpreter-internal tail verbatim.
        assert "embedded null" in msg
        # The memoized degenerate token re-raises the SAME wrapped refusal.
        with pytest.raises(ValueError, match=SETTLE_EVIDENCE_PATH_ENV):
            evidence_settle(env)

    def test_float_schema_explicit_raises_naming_env(self, tmp_path: Path) -> None:
        """[refuter r3 F-R3a-1] the explicit-path twin of the float-schema
        repro — the same full otherwise-valid ``"schema": 1.0`` file fails
        LOUD with a ValueError naming the env var."""
        target = write_evidence(tmp_path / "e.json", schema=1.0)
        env = {SETTLE_EVIDENCE_PATH_ENV: str(target)}
        with pytest.raises(ValueError, match="schema must be exactly 1") as excinfo:
            evidence_settle(env)
        assert SETTLE_EVIDENCE_PATH_ENV in str(excinfo.value)
        assert "got 1.0" in str(excinfo.value)

    def test_valid_explicit_adopts(self, tmp_path: Path) -> None:
        target = write_evidence(tmp_path / "e.json")
        result = evidence_settle({SETTLE_EVIDENCE_PATH_ENV: str(target)})
        assert isinstance(result, SettleEvidence)
        assert result.adopted == time(16, 0)


# ── the committed-file CI tier (§3.1 rule b) ─────────────────────────────


def test_committed_evidence_file_if_present_is_valid() -> None:
    """The CI fail-loud tier for COMMITTED content: reads the REAL
    ``default_evidence_path()`` DIRECTLY (bypassing env — immune to the
    conftest belt). ABSENT → pass (this PR's ship state — no measured number
    exists); PRESENT → the future operator commit must validate cleanly AND
    respect the BLOCK_END floor, making that commit a zero-code-churn,
    CI-guarded event."""
    committed = default_evidence_path()
    if not committed.exists():
        assert True  # the honest ship state — nothing to validate yet
        return
    payload = json.loads(committed.read_text(encoding="utf-8"))  # pragma: no cover
    ev = validate_evidence_payload(payload, path=str(committed))  # pragma: no cover
    assert ev.adopted >= BLOCK_END  # pragma: no cover


# ── memo thread safety ───────────────────────────────────────────────────


def test_memo_thread_safety_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """8 threads over one valid file → ONE compute (read), ONE INFO."""
    target = write_evidence(tmp_path / "e.json")
    monkeypatch.setattr(settle_evidence, "default_evidence_path", lambda: target)
    reads: list[str] = []
    real_read = settle_evidence.read_sidecar_bytes

    def counting_read(path: Path, *, max_bytes: int) -> SidecarReadResult:
        reads.append(str(path))
        return real_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(settle_evidence, "read_sidecar_bytes", counting_read)
    results: list[SettleEvidence | None] = []
    with capture_logs() as logs:
        threads = [
            threading.Thread(target=lambda: results.append(evidence_settle({}))) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(results) == 8
    assert all(isinstance(r, SettleEvidence) for r in results)
    assert len(reads) == 1  # one compute; seven memo hits
    # Scoped to the settle loader's OWN events (the routing facade's one-time
    # ``calendar_rust.engaged`` INFO is out of this loader-contract's scope).
    settle_events = [e["event"] for e in logs if e["event"].startswith("settle_evidence.")]
    assert settle_events == ["settle_evidence.valid"]


# ── the probe --emit-evidence round-trip (the channel keystone) ──────────


def _probe_record(
    day: str,
    slot: str,
    *,
    kind: str = "INDEX",
    ok: bool = True,
    expected: int = 375,
) -> dict[str, object]:
    """One synthetic transcript record using the REAL probe record keys."""
    return {
        "probe_ts_ist": f"{day}T{slot}:05+05:30",
        "slot": slot,
        "symbol": "NSE-NIFTY" if kind == "INDEX" else "NSE-NIFTY-30Jul26-FUT",
        "kind": kind,
        "exchange": "NSE",
        "segment": "CASH" if kind != "FUT" else "FNO",
        "expected_minutes": expected,
        "error": None,
        "raw_rows": 375,
        "rows_len6": 0 if ok else 5,
        "rows_len7": 375 if ok else 370,
        "raw_null_volume": 0,
        "raw_null_oi": 0,
        "parsed_bars": 375,
        "count_ok": ok,
        "first_bar_ist": "09:15",
        "last_bar_ist": "15:29",
        "last_bar_is_1529": ok,
        "fut_volume_nonnull": kind == "FUT",
        "fut_oi_nonnull": kind == "FUT",
    }


_ROUNDTRIP_SLOTS = ("15:35", "15:45", "16:00", "16:30", "17:30")
_ROUNDTRIP_DAYS = ("2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20")


def _write_transcripts(
    directory: Path,
    *,
    days: tuple[str, ...] = _ROUNDTRIP_DAYS,
    clean_from_slot: str = "15:45",
    all_failing: bool = False,
) -> None:
    """5 regular days × 5 slots × 3 records (2 INDEX + 1 FUT) + a trailer."""
    slot_rank = {slot: i for i, slot in enumerate(_ROUNDTRIP_SLOTS)}
    for day in days:
        for slot in _ROUNDTRIP_SLOTS:
            ok = (not all_failing) and slot_rank[slot] >= slot_rank[clean_from_slot]
            rows = [
                _probe_record(day, slot, ok=ok),
                _probe_record(day, slot, ok=ok),
                _probe_record(day, slot, kind="FUT", ok=ok),
            ]
            path = directory / f"postclose-{day}-{slot.replace(':', '')}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.write(
                    json.dumps({"slot_summary": slot, "probe_ts_ist": f"{day}T{slot}:59+05:30"})
                    + "\n"
                )


def _run_probe(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PROBE), *argv],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=False,
    )


class TestProbeEmitterRoundtrip:
    """Synthetic transcripts → the REAL ``--emit-evidence`` subprocess → the
    REAL validator (no Groww needed — the channel keystone)."""

    def test_roundtrip_validates(self, tmp_path: Path) -> None:
        _write_transcripts(tmp_path)
        # + one non-trading day, one Muhurat-shaped day, one off-grid record.
        (tmp_path / "postclose-2026-07-19-1535.jsonl").write_text(
            json.dumps(_probe_record("2026-07-19", "15:35", expected=0)) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "postclose-2026-07-21-1730.jsonl").write_text(
            json.dumps(_probe_record("2026-07-21", "17:30", expected=60)) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "postclose-2026-07-22-1200.jsonl").write_text(
            json.dumps(_probe_record("2026-07-22", "12:00")) + "\n",
            encoding="utf-8",
        )
        out = tmp_path / "settle_evidence.json"
        proc = _run_probe(
            "--summarize", str(tmp_path), "--emit-evidence", str(out), "--margin-minutes", "15"
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "evidence written" in proc.stdout
        assert "off-grid-slot record(s) ignored" in proc.stdout
        # A clean monotonic fixture prints NO non-monotonic note (r1 NIT-3).
        assert "non-monotonic" not in proc.stdout
        payload = json.loads(out.read_text(encoding="utf-8"))
        ev = validate_evidence_payload(payload, path=str(out))
        assert ev.adopted == time(16, 0)  # 15:45 earliest + 15 min margin
        assert ev.earliest_all_pass == "15:45"
        assert ev.distinct_trading_days == 5
        assert payload["ignored_records"] == 1
        assert payload["excluded_days"] == {
            "2026-07-19": "non_regular_session (expected_minutes=0)",
            "2026-07-21": "non_regular_session (expected_minutes=60)",
        }
        assert payload["probe_script_version"] == "1.1"
        assert "15:35" in payload["slots_observed"]
        assert len(payload["transcripts"]) == 28  # 5×5 regular + 3 specials

    def test_too_few_regular_days_refused(self, tmp_path: Path) -> None:
        _write_transcripts(tmp_path, days=_ROUNDTRIP_DAYS[:4])
        out = tmp_path / "settle_evidence.json"
        proc = _run_probe("--summarize", str(tmp_path), "--emit-evidence", str(out))
        assert proc.returncode == 2
        assert "evidence NOT emitted" in proc.stdout
        assert not out.exists()  # refusals never write a half-artifact

    def test_no_all_pass_slot_refused(self, tmp_path: Path) -> None:
        _write_transcripts(tmp_path, all_failing=True)
        out = tmp_path / "settle_evidence.json"
        proc = _run_probe("--summarize", str(tmp_path), "--emit-evidence", str(out))
        assert proc.returncode == 2
        assert "no canonical slot" in proc.stdout
        assert not out.exists()

    def test_empty_directory_refused(self, tmp_path: Path) -> None:
        out = tmp_path / "settle_evidence.json"
        proc = _run_probe("--summarize", str(tmp_path), "--emit-evidence", str(out))
        assert proc.returncode == 2
        assert not out.exists()

    @pytest.mark.parametrize("margin", ["5", "241", "junk"])
    def test_junk_margin_refused(self, tmp_path: Path, margin: str) -> None:
        _write_transcripts(tmp_path)
        out = tmp_path / "settle_evidence.json"
        proc = _run_probe(
            "--summarize", str(tmp_path), "--emit-evidence", str(out), "--margin-minutes", margin
        )
        assert proc.returncode == 2
        assert not out.exists()

    def test_emit_without_summarize_refused(self, tmp_path: Path) -> None:
        proc = _run_probe("--emit-evidence", str(tmp_path / "e.json"))
        assert proc.returncode == 2
        assert "--emit-evidence requires --summarize" in proc.stderr

    def test_margin_bounds_mirror_validator_constants(self) -> None:
        """The probe hardcodes [10, 240] + >=5 days (it must run without
        brutex importable) — drift-pinned against the validator's constants."""
        source = _PROBE.read_text(encoding="utf-8")
        assert f"_MARGIN_BOUNDS = ({MIN_MARGIN_MINUTES}, {MAX_MARGIN_MINUTES})" in source
        assert f"_MIN_EVIDENCE_DAYS = {MIN_EVIDENCE_DAYS}" in source

    def test_non_monotonic_later_slot_failure_prints_note_emit_path_only(
        self, tmp_path: Path
    ) -> None:
        """[r1 correctness NIT-3] the earliest slot all-pass on EVERY observed
        day while a strictly LATER slot fails on some day prints ONE
        informational note naming day+slot — the adopted value + the evidence
        schema are UNCHANGED, and the note lives in the EMIT path only (a
        plain ``--summarize`` keeps its pre-#1793 note-free stdout)."""
        _write_transcripts(tmp_path)  # clean from 15:45 on all 5 days
        day, slot = "2026-07-16", "16:30"
        rows = [
            _probe_record(day, slot, ok=False),
            _probe_record(day, slot, ok=False),
            _probe_record(day, slot, kind="FUT", ok=False),
        ]
        target = tmp_path / f"postclose-{day}-{slot.replace(':', '')}.jsonl"
        with target.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        out = tmp_path / "settle_evidence.json"
        proc = _run_probe("--summarize", str(tmp_path), "--emit-evidence", str(out))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "note: non-monotonic observations" in proc.stdout
        assert f"(first: {day} at {slot})" in proc.stdout
        assert "1 later-slot failure(s) after the all-pass 15:45" in proc.stdout
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["earliest_all_pass_ist"] == "15:45"  # value UNCHANGED
        assert payload["adopted_settle_ist"] == "16:00"
        assert validate_evidence_payload(payload, path=str(out)).adopted == time(16, 0)
        # The note is EMIT-path-only: a plain --summarize over the SAME
        # fixture prints no note (the reviewers' byte-identity pin was the
        # plain --summarize stdout — kept intact by construction).
        plain = _run_probe("--summarize", str(tmp_path))
        assert plain.returncode == 0, plain.stdout + plain.stderr
        assert "non-monotonic" not in plain.stdout


def _load_probe_module() -> Any:
    """Import the probe as a module (module-level = constants only — safe)."""
    spec = importlib.util.spec_from_file_location("_postclose_settle_probe_pin", _PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_record_keys_drift_pin() -> None:
    """[r1 coverage INFO-4] BOTH sides pinned to the probe's ``_RECORD_KEYS``:
    the round-trip suite's synthetic record uses EXACTLY the writer's key set,
    AND the writer's literal keys (the base record dict + the ``record.update``
    dict in ``main()``, extracted via AST) equal the constant — a key
    rename/add on either side fails HERE, never a silently-vacuous golden.

    [post-open CodeQL fold] the writer additionally SELF-CHECKS each
    successful record against ``_RECORD_KEYS`` at runtime (fail-loud
    RuntimeError on drift); the writer is inline in ``main()`` behind the
    read-only vendor-client build, so the self-check's raise arm is NOT
    unit-driven here (no vendor creds in CI) — this AST pin remains the
    teeth, and the consumption pin below keeps the self-check from
    regressing back to an unused global."""
    probe = _load_probe_module()
    keys = probe._RECORD_KEYS
    assert isinstance(keys, frozenset)
    # Side 1: the synthetic fixture emits exactly the writer's key set.
    assert frozenset(_probe_record("2026-07-14", "15:45")) == keys
    # Side 2: the writer's literal key set equals the constant. The base
    # record dict is the one carrying expected_minutes+error (the trailer has
    # neither); the update dict is the one carrying raw_rows+fut_oi_nonnull
    # (raw_stats has no fut_oi_nonnull).
    tree = ast.parse(_PROBE.read_text(encoding="utf-8"))
    literal_dicts = [
        {key.value for key in node.keys if isinstance(key, ast.Constant)}
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and node.keys
        and all(isinstance(key, ast.Constant) for key in node.keys)
    ]
    base = next(d for d in literal_dicts if {"expected_minutes", "error"} <= d)
    update = next(d for d in literal_dicts if {"raw_rows", "fut_oi_nonnull"} <= d)
    assert base | update == keys
    # Side 3 (consumption pin): the writer genuinely LOADS _RECORD_KEYS inside
    # a function body (the runtime self-check) — never again a module-level
    # constant only the tests read (the CodeQL unused-global class).
    function_loads = {
        name.id
        for func in ast.walk(tree)
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        for name in ast.walk(func)
        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
    }
    assert "_RECORD_KEYS" in function_loads


# ── the calendar-rust routing seam (DEC-RUST-CALENDAR-FACADE-FLIP-001) ────
#
# Direct-call teeth on the two PURE materialize helpers the ``routed(...)`` seam
# hands the crate return to. These branches are dead wheel-ABSENT (the facade
# short-circuits to the Python oracle without a crate), so they need crate-free /
# flag-free unit coverage to hold the per-module 100% line+branch gate on the
# main (wheel-less) CI test job. No crate, no flag, no monkeypatch — pure input
# → outcome. (The genuinely-engaged real-crate differential lives in the
# non-required calendar-rust-parity lane.)


class TestCalendarRustSettleSeam:
    """``_materialize_settle`` / ``_materialize_settle_valid`` refuse-poison + happy."""

    @staticmethod
    def _warn_reasons(logs: Sequence[Mapping[str, object]]) -> list[object]:
        return [e["reason"] for e in logs if e["event"] == "calendar_rust.fallback"]

    @pytest.fixture(autouse=True)
    def _fresh_calendar_rust(self) -> Generator[None]:
        from brutex.calendars import calendar_rust as _cr

        _cr.reset_calendar_rust_for_tests()
        yield
        _cr.reset_calendar_rust_for_tests()

    # ── _materialize_settle (the 4-tuple re-hydrator) ────────────────────

    def test_settle_none_abstains_silently(self) -> None:
        """serde ABSTAIN (crate ``None``) → ``None`` with NO WARN (designed degrade)."""
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle(None, payload={}, path="p.json")
        assert out is None
        assert self._warn_reasons(logs) == []

    def test_settle_non_tuple_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle(["not", "a", "tuple"], payload={}, path="p")
        assert out is None
        assert self._warn_reasons(logs) == ["settle_shape"]

    def test_settle_wrong_len_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle((True, ""), payload={}, path="p")
        assert out is None
        assert self._warn_reasons(logs) == ["settle_shape"]

    def test_settle_valid_not_bool_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle((1, "", "17:30", "x"), payload={}, path="p")
        assert out is None
        assert self._warn_reasons(logs) == ["settle_leg_types"]

    def test_settle_message_not_str_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle(
                (False, 42, "17:30", "x"), payload={}, path="p"
            )
        assert out is None
        assert self._warn_reasons(logs) == ["settle_leg_types"]

    def test_settle_invalid_defers_the_bytewise_valueerror(self) -> None:
        """``valid=False`` → :class:`Raise` carrying the crate's message VERBATIM."""
        out = settle_evidence._materialize_settle(
            (False, "settle evidence 'p.json': boom", "", ""), payload={}, path="p.json"
        )
        assert isinstance(out, Raise)
        with pytest.raises(ValueError, match=r"^settle evidence 'p\.json': boom$"):
            unwrap(out)

    def test_settle_valid_delegates_to_valid_hydrator(self) -> None:
        """``valid=True`` over a conforming payload → :class:`SettleEvidence`."""
        out = settle_evidence._materialize_settle(
            (True, "", "17:30", "2026-07-13T12:31:04+00:00"),
            payload=make_evidence_payload(),
            path="p.json",
        )
        assert out == Value(
            SettleEvidence(
                adopted=time(17, 30),
                earliest_all_pass="15:45",
                margin_minutes=15,
                distinct_trading_days=5,
                generated_at_utc="2026-07-13T12:31:04+00:00",
                probe_script_version="1.1",
                path="p.json",
            )
        )

    # ── _materialize_settle_valid (the valid=True re-hydrator) ───────────

    def test_valid_hydrator_non_dict_payload_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle_valid("nope", "17:30", "x", path="p")
        assert out is None
        assert self._warn_reasons(logs) == ["settle_valid_shape"]

    def test_valid_hydrator_non_str_adopted_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle_valid(
                make_evidence_payload(), 1730, "x", path="p"
            )
        assert out is None
        assert self._warn_reasons(logs) == ["settle_valid_shape"]

    def test_valid_hydrator_non_str_generated_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle_valid(
                make_evidence_payload(), "17:30", 42, path="p"
            )
        assert out is None
        assert self._warn_reasons(logs) == ["settle_valid_shape"]

    @pytest.mark.parametrize(
        "mutation",
        [
            {"earliest_all_pass_ist": None},
            {"margin_minutes": True},
            {"margin_minutes": "15"},
            {"distinct_trading_days": True},
            {"distinct_trading_days": "5"},
            {"probe_script_version": 11},
        ],
    )
    def test_valid_hydrator_bad_pluck_refuses(self, mutation: dict[str, object]) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle_valid(
                make_evidence_payload(**mutation), "17:30", "x", path="p"
            )
        assert out is None
        assert self._warn_reasons(logs) == ["settle_valid_pluck"]

    def test_valid_hydrator_bad_adopted_shape_refuses(self) -> None:
        with capture_logs() as logs:
            out = settle_evidence._materialize_settle_valid(
                make_evidence_payload(), "99-99", "x", path="p"
            )
        assert out is None
        assert self._warn_reasons(logs) == ["settle_valid_adopted_shape"]

    def test_valid_hydrator_builds_settle_evidence(self) -> None:
        out = settle_evidence._materialize_settle_valid(
            make_evidence_payload(),
            "17:30",
            "2026-07-13T12:31:04+00:00",
            path="probe.json",
        )
        assert out == Value(
            SettleEvidence(
                adopted=time(17, 30),
                earliest_all_pass="15:45",
                margin_minutes=15,
                distinct_trading_days=5,
                generated_at_utc="2026-07-13T12:31:04+00:00",
                probe_script_version="1.1",
                path="probe.json",
            )
        )
