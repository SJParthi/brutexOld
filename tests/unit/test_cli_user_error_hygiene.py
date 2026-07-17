# pyright: reportPrivateUsage=none
"""Clean user-error exits on operator-typo inputs (DEC-CLI-USER-ERROR-HYGIENE-001).

The CLI's parse/read/guard helpers raise :class:`brutex.cli.CliUserError`, and
``main()`` converts exactly that exception to the standard one-line
``[brutex] <message>`` on stderr + ``EXIT_USER_ERROR`` — never a raw traceback.
Covered here, per fix area:

* Area 1 — ``_parse_dt``: a typo'd ``--start``/``--end`` on any of the six
  date-taking commands is one clean line (previously a raw ``ValueError``).
* Area 2 — ``_parse_int_csv`` / ``parse_condition_ids``: the ONE ``--conditions``
  / ``--k`` CSV parser behind sweep / sweep-all / sweep-verify.
* Area 3 — ``cmd_status``: a hand-corrupted ``runtime.json`` is a clean user
  error and the file is NEVER auto-deleted.
* Area 4 — the ``Catalog.open(require_exists=)`` seam + the CLI read-command
  guards: a typo'd ``--lake-root``/``--catalog`` fails loudly and never conjures
  a fresh empty lake (the old "partitions=0" silent illusion).
* Area 5 — ``_parse_paisa_list``: a bad ``--sl``/``--tp`` token is clean.

Valid-input behavior is byte-identical everywhere (golden pins below).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from brutex import cli
from brutex.vocabulary.bits import TOTAL_CONDITIONS

pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")  # see tests/conftest.py


# ── Area 1: _parse_dt ───────────────────────────────────────────────────


def test_parse_dt_malformed_raises_clean_user_error() -> None:
    """A malformed date raises CliUserError naming the flag pair + the value.

    Fail-before/pass-after: pre-fix ``_parse_dt`` let ``datetime.fromisoformat``
    raise a raw ValueError that tracebacked out of every date-taking command.
    """
    with pytest.raises(cli.CliUserError) as excinfo:
        cli._parse_dt("not-a-date")
    message = str(excinfo.value)
    assert "--start/--end" in message
    assert "'not-a-date'" in message
    assert "YYYY-MM-DD" in message


def test_parse_dt_valid_inputs_unchanged() -> None:
    """Valid input parses byte-identically (same fromisoformat + tz normalization)."""
    assert cli._parse_dt("2024-01-02") == datetime(2024, 1, 2, tzinfo=UTC)
    ist = timezone(timedelta(hours=5, minutes=30))
    aware = cli._parse_dt("2024-01-02T09:15:00+05:30")
    assert aware == datetime(2024, 1, 2, 9, 15, tzinfo=ist)
    assert aware.tzinfo is not None
    assert aware.utcoffset() == timedelta(hours=5, minutes=30)


def test_fetch_bad_start_via_main_is_clean_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd ``fetch --start`` exits 1 with one clean line — zero call-site edits.

    Fail-before/pass-after: the ``_parse_dt`` call in ``cmd_fetch`` sits before
    any adapter/catalog work; pre-fix it tracebacked with a raw ValueError.
    """
    rc = cli.main(
        ["fetch", "--lake-root", str(tmp_path / "lake"), "--dry-run", "--start", "garbage"]
    )
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert captured.err.startswith("[brutex] invalid --start/--end value 'garbage'")
    assert "Traceback" not in captured.err


# ── main() net ──────────────────────────────────────────────────────────
# The net's try-SUCCESS branch needs no dedicated test: every other
# ``cli.main([...])`` test in the suite (and the fetch/sweep/sweep-all tests
# here) runs the dispatch to completion through it.


def test_main_net_converts_cli_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The net converts CliUserError to ``[brutex] ...`` + EXIT_USER_ERROR.

    Fail-before/pass-after: pre-fix ``catalog reconcile`` on a typo'd lake root
    silently mkdir'd a fresh empty lake + catalog and printed ``partitions=0``
    with exit 0 — the silent illusion this decision kills.
    """
    pytest.importorskip("duckdb")
    pytest.importorskip("polars")
    missing = tmp_path / "nope"
    rc = cli.main(["catalog", "reconcile", "--lake-root", str(missing)])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert "[brutex] lake root not found:" in captured.err
    assert str(missing) in captured.err
    assert not missing.exists(), "the typo'd lake root must NOT be conjured"


# ── Area 2: _parse_int_csv / parse_condition_ids ────────────────────────


def test_parse_int_csv_valid_matches_old_inline() -> None:
    """Valid input is byte-identical to the historical inline expression."""
    assert cli._parse_int_csv("1, 2,,3,", flag="--k") == [1, 2, 3]
    assert cli._parse_int_csv("", flag="--k") == []


def test_parse_int_csv_bad_token_names_flag_and_token() -> None:
    """A non-integer token raises CliUserError naming the flag AND the token.

    Fail-before/pass-after: the inline ``int(tok)`` copies raised a raw
    ValueError (a traceback in sweep-verify; a late one in sweep-all).
    """
    with pytest.raises(cli.CliUserError, match=r"invalid --k token 'x'"):
        cli._parse_int_csv("1,x", flag="--k")


def test_parse_condition_ids_valid_byte_identical() -> None:
    """Order + duplicates preserved; whitespace tolerated (downstream dedups)."""
    assert cli.parse_condition_ids("0, 5,59,5") == [0, 5, 59, 5]


def test_parse_condition_ids_non_integer_token() -> None:
    """A non-integer --conditions token names the token + the flag."""
    with pytest.raises(cli.CliUserError, match=r"invalid --conditions token 'abc'"):
        cli.parse_condition_ids("0,abc")


def test_parse_condition_ids_out_of_range() -> None:
    """A bit outside 0..TOTAL_CONDITIONS-1 is refused in BOTH directions.

    Fail-before/pass-after: pre-fix an out-of-range bit sailed into the engine
    (a late, worse error in sweep/sweep-all; a traceback in sweep-verify).
    """
    with pytest.raises(cli.CliUserError) as excinfo:
        cli.parse_condition_ids(str(TOTAL_CONDITIONS))
    assert f"0..{TOTAL_CONDITIONS - 1}" in str(excinfo.value)
    with pytest.raises(cli.CliUserError, match="out of range"):
        cli.parse_condition_ids("-1")


def test_parse_condition_ids_refuses_a_retired_tombstone_bit() -> None:
    """TOMB-03 — a saved pre-reshape script's ``--conditions 52`` is refused
    LOUDLY, naming the bit, its name and its RETIRED status + the DEC family.

    Fail-before/pass-after: pre-fix bits 52/53 (in-layout, evaluator emits a
    constant 0) sailed into the grid — hours of silent dead compute the
    results store would happily persist (it admits every declared bit)."""
    with pytest.raises(cli.CliUserError) as excinfo:
        cli.parse_condition_ids("0,52")
    message = str(excinfo.value)
    assert "bit 52" in message
    assert "close_above_vwap" in message
    assert "RETIRED" in message
    assert "DEC-ENGINE-SPOT-ONLY-001" in message
    with pytest.raises(cli.CliUserError, match="close_below_vwap.*RETIRED|RETIRED"):
        cli.parse_condition_ids("53")


def test_parse_condition_ids_accepts_the_live_supertrend_bits() -> None:
    """The vocab-66 SuperTrend implemented-flip (crate 0.5.0) ACCEPTANCE pin:
    bits 64/65 joined IMPLEMENTED_MASK, so the former declared-only refusal
    for them is consciously retired."""
    assert cli.parse_condition_ids("64,65") == [64, 65]


def test_parse_condition_ids_refuses_a_declared_unimplemented_bit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G-U64U12-02/G-RETROT-03 — a layout-only bit is refused as
    declared-but-NOT-YET-implemented (never 'retired'), naming the bit +
    name + the grow-only DEC family.

    No LAYOUT-ONLY bit exists on today's 66-bit layout (the SuperTrend pair
    left that class at the vocab-66 implemented-flip), so the arm is kept
    honest by simulating a future manifest where an implemented, non-retired
    bit (63) is layout-only — the ``test_default_excludes_layout_only_bits``
    monkeypatch precedent.
    """
    from brutex.vocabulary.bits import IMPLEMENTED_MASK

    monkeypatch.setattr(cli, "IMPLEMENTED_MASK", IMPLEMENTED_MASK & ~(1 << 63))
    with pytest.raises(cli.CliUserError) as excinfo:
        cli.parse_condition_ids("63")
    message = str(excinfo.value)
    assert "bit 63" in message
    assert "narrow_cpr_day" in message
    assert "NOT YET" in message and "RETIRED" not in message
    assert "DEC-VOCAB-U128-CODEGEN-001" in message


def test_parse_condition_ids_accepts_the_implemented_boundaries() -> None:
    """Bits 59 (BOS/CHoCH edge) and 63 (narrow_cpr_day — the u64 sign bit)
    are implemented and must stay accepted."""
    assert cli.parse_condition_ids("59,63") == [59, 63]


def test_sweep_bad_conditions_via_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``sweep --conditions 0,notabit`` exits 1 cleanly; no lake is conjured."""
    lake = tmp_path / "lake"
    rc = cli.main(
        [
            "sweep",
            "NIFTY",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--lake-root",
            str(lake),
            "--conditions",
            "0,notabit",
        ]
    )
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert "invalid --conditions token 'notabit'" in captured.err
    assert not lake.exists()


def test_sweep_all_bad_k_via_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``sweep-all --k 1,x`` exits 1 cleanly (behavior pin; body is pragma-no-cover)."""
    rc = cli.main(
        [
            "sweep-all",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--lake-root",
            str(tmp_path / "nope"),
            "--k",
            "1,x",
        ]
    )
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert "invalid --k token 'x'" in captured.err


# ── Area 3: cmd_status ──────────────────────────────────────────────────


def _runtime_json(**overrides: object) -> str:
    """A structurally-complete runtime.json body with per-test type poisons."""
    data: dict[str, object] = {
        "pid": 4242,
        "run_id": "run-a",
        "started_at": "2026-06-12T00:00:00Z",
        "host_bind": "127.0.0.1",
        "dashboard_urls": [],
        "metrics_url": "http://127.0.0.1:9080/metrics",
        "phase": "idle",
        "env_snapshot": {},
        "environment": "terminal",
    }
    data.update(overrides)
    return json.dumps(data)


def test_status_corrupt_json_is_clean_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Truncated JSON → one clean line naming the path; the file is NEVER deleted.

    Fail-before/pass-after: pre-fix ``cmd_status`` let RuntimeFileError
    traceback out of ``read_runtime_file``.
    """
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text("{truncated", encoding="utf-8")
    monkeypatch.setenv("BRUTEX_RUNTIME_FILE", str(runtime_file))
    rc = cli.cmd_status(run_id=None)
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert str(runtime_file) in captured.err
    assert "corrupt/unreadable" in captured.err
    assert "never" in captured.err
    assert "auto-deleted" in captured.err
    assert runtime_file.read_text(encoding="utf-8") == "{truncated"


def test_status_wrong_typed_pid_is_clean_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Valid JSON with ``pid: "abc"`` (a ValueError leak from from_json) is clean.

    Fail-before/pass-after: pre-fix the raw ``int("abc")`` ValueError tracebacked.
    """
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(_runtime_json(pid="abc"), encoding="utf-8")
    monkeypatch.setenv("BRUTEX_RUNTIME_FILE", str(runtime_file))
    rc = cli.cmd_status(run_id=None)
    assert rc == cli.EXIT_USER_ERROR
    assert "corrupt/unreadable" in capsys.readouterr().err


def test_status_env_snapshot_not_dict_is_clean_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``env_snapshot: []`` (an AttributeError leak from from_json) is clean."""
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(_runtime_json(env_snapshot=[]), encoding="utf-8")
    monkeypatch.setenv("BRUTEX_RUNTIME_FILE", str(runtime_file))
    rc = cli.cmd_status(run_id=None)
    assert rc == cli.EXIT_USER_ERROR
    assert "corrupt/unreadable" in capsys.readouterr().err


def test_status_valid_file_still_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real runtime file still prints the snapshot through the REAL read path."""
    from brutex.config.runtime import build_runtime_info, write_runtime_file

    runtime_file = tmp_path / "runtime.json"
    monkeypatch.setenv("BRUTEX_RUNTIME_FILE", str(runtime_file))
    info = build_runtime_info(
        pid=os.getpid(),
        run_id="run-hygiene",
        host_bind="127.0.0.1",
        dashboard_urls=["http://127.0.0.1:9080"],
        metrics_url="http://127.0.0.1:9080/metrics",
        phase="idle",
        environment="terminal",
    )
    write_runtime_file(info, path=runtime_file)
    rc = cli.cmd_status(run_id=None)
    captured = capsys.readouterr()
    assert rc == cli.EXIT_OK
    assert f"pid       : {os.getpid()}" in captured.out
    assert "run_id    : run-hygiene" in captured.out


def test_status_missing_file_still_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No runtime file stays the calm exit-0 path (regression pin)."""
    monkeypatch.setenv("BRUTEX_RUNTIME_FILE", str(tmp_path / "absent.json"))
    rc = cli.cmd_status(run_id=None)
    assert rc == cli.EXIT_OK
    assert "no runtime file" in capsys.readouterr().out


# ── Area 4: Catalog.open require_exists seam + read-command guards ──────


def test_catalog_open_require_exists_missing_raises(tmp_path: Path) -> None:
    """``require_exists=True`` on a missing path refuses BEFORE any mkdir.

    Fail-before/pass-after: pre-fix ``Catalog.open`` unconditionally mkdir'd
    the whole missing chain and created a fresh empty DB.
    """
    pytest.importorskip("duckdb")
    from brutex.data.catalog import Catalog
    from brutex.data.errors import CatalogError

    missing = tmp_path / "nolake" / "catalog.duckdb"
    with pytest.raises(CatalogError, match="catalog not found"):
        Catalog.open(missing, require_exists=True)
    assert not (tmp_path / "nolake").exists(), "the guard must precede mkdir"


def test_catalog_open_require_exists_opens_existing(tmp_path: Path) -> None:
    """``require_exists=True`` on an EXISTING catalog opens + reads normally."""
    pytest.importorskip("duckdb")
    from brutex.data.catalog import Catalog

    path = tmp_path / "catalog.duckdb"
    Catalog.open(path).close()
    with Catalog.open(path, require_exists=True) as cat:
        assert cat.count() == 0


def test_catalog_open_default_still_creates(tmp_path: Path) -> None:
    """The default (``require_exists=False``) keeps create-on-open byte-identical."""
    pytest.importorskip("duckdb")
    from brutex.data.catalog import Catalog

    path = tmp_path / "nested" / "deeper" / "catalog.duckdb"
    with Catalog.open(path):
        assert path.exists()
        assert path.parent.is_dir()


def test_require_catalog_exists_ok_and_missing(tmp_path: Path) -> None:
    """Both branches of the catalog-file guard: existing → None, missing → raise."""
    existing = tmp_path / "catalog.duckdb"
    existing.write_bytes(b"")
    assert cli._require_catalog_exists(existing) is None
    missing = tmp_path / "absent.duckdb"
    with pytest.raises(cli.CliUserError) as excinfo:
        cli._require_catalog_exists(missing)
    message = str(excinfo.value)
    assert str(missing) in message
    assert "catalog reconcile" in message


def test_require_lake_dir_ok_and_missing(tmp_path: Path) -> None:
    """Both branches of the lake-dir guard: existing dir → None, missing → raise."""
    assert cli._require_lake_dir(tmp_path, command="sweep-all") is None
    missing = tmp_path / "absent"
    with pytest.raises(cli.CliUserError) as excinfo:
        cli._require_lake_dir(missing, command="sweep-all")
    message = str(excinfo.value)
    assert str(missing) in message
    assert "--lake-root" in message


def test_sweep_missing_catalog_refused_and_no_lake_conjured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd ``sweep --lake-root`` refuses with the reconcile remedy, mkdir-free.

    Fail-before/pass-after: pre-fix ``Catalog.open`` conjured
    ``<typo>/catalog.duckdb`` and the command read "no bars" off an empty lake.
    """
    pytest.importorskip("duckdb")
    typo = tmp_path / "typo"
    rc = cli.main(
        [
            "sweep",
            "NIFTY",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--lake-root",
            str(typo),
        ]
    )
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert "catalog not found:" in captured.err
    assert str(typo / "catalog.duckdb") in captured.err
    assert not typo.exists(), "the typo'd lake must NOT be conjured"


def test_catalog_reconcile_existing_lake_still_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reconcile on a REAL (empty) lake still rebuilds a missing catalog inside it."""
    pytest.importorskip("duckdb")
    pytest.importorskip("polars")
    lake = tmp_path / "lake"
    lake.mkdir()
    rc = cli.main(["catalog", "reconcile", "--lake-root", str(lake)])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_OK
    assert "partitions=0" in captured.out
    assert (lake / "catalog.duckdb").is_file(), "the legit rebuild path is preserved"


def test_sweep_existing_catalog_byte_identical_past_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a real catalog the guard is transparent — downstream behavior unchanged."""
    pytest.importorskip("duckdb")
    from brutex.data.catalog import Catalog

    lake = tmp_path / "lake"
    lake.mkdir()
    Catalog.open(lake / "catalog.duckdb").close()
    rc = cli.main(
        [
            "sweep",
            "NIFTY",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--lake-root",
            str(lake),
        ]
    )
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert "no bars for NIFTY" in captured.err


def test_sweep_all_missing_lake_root_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sweep-all`` on a typo'd lake root refuses loudly; typo dir not created.

    Behavior pin for the daemon-critical guard (the body is pragma-no-cover).
    """
    pytest.importorskip("duckdb")
    typo = tmp_path / "typo"
    rc = cli.main(
        [
            "sweep-all",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
            "--lake-root",
            str(typo),
        ]
    )
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USER_ERROR
    assert "lake root not found:" in captured.err
    assert not typo.exists()


# ── Area 5: _parse_paisa_list ───────────────────────────────────────────


def test_parse_paisa_list_bad_token() -> None:
    """A non-integer, non-'none' token raises CliUserError naming --sl/--tp.

    Fail-before/pass-after: pre-fix the raw ``int("abc")`` ValueError reached
    sweep-all before its try (and sweep-verify with no try at all).
    """
    with pytest.raises(cli.CliUserError) as excinfo:
        cli._parse_paisa_list("100,abc")
    message = str(excinfo.value)
    assert "--sl/--tp" in message
    assert "'abc'" in message


def test_parse_paisa_list_valid_golden_unchanged() -> None:
    """Valid input is byte-identical post-restructure (none/blank/int branches)."""
    assert cli._parse_paisa_list("none,100,NONE, ,250,") == [None, 100, None, 250]
    assert cli._parse_paisa_list("") == []
