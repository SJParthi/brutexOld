# Typing surface for the brutex_data PyO3 bridge (ADR-0020 slice T10 PR-1,
# DEC-DATA-RUST-PURELAW-001). Mirrors rust/brutex-data/src/lib.rs — keep in
# lockstep with the `#[pymodule]` registration block there.

from collections.abc import Sequence

__version__: str

def parse_fno_symbol_coords(
    symbol: str,
) -> tuple[int, int, int, int, int, int, int, int, int] | None:
    """Decode an ASCII F&O contract symbol into integer coordinates, or ``None``.

    The 9-slot layout (spans are byte offsets into the ORIGINAL string —
    the facade guarantees ASCII, so byte offsets ≡ char offsets):

    ``(type_code, exchange_end, underlying_start, underlying_end,
    expiry_year, expiry_month, expiry_day, strike_start, strike_end)``

    * ``type_code`` — ``0``=FUT · ``1``=CE · ``2``=PE;
    * ``exchange = symbol[0:exchange_end]``;
    * ``underlying = symbol[underlying_start:underlying_end]``;
    * ``expiry_year/month/day`` — the RESOLVED expiry (POSIX ``%y`` pivot
      applied, Gregorian-calendar checked, [2019, 2059] window applied);
    * option: ``strike token = symbol[strike_start:strike_end]`` (the
      facade parses it via the injected Python ``parse_strike_token``);
      future: ``(0, 0)`` (absent — the grammar has no FUT strike token).

    ``None`` mirrors the oracle's never-raises structural rejection (junk /
    spot / short / bad expiry / empty exchange-underlying-strike token).
    Raises ``RuntimeError`` only if the parse panicked (caught at the FFI
    boundary — a panic never crosses raw).
    """

def stock_drift_fold(symbol: str) -> str:
    """Law A — the irreversible evidence-matching fold (ASCII-only by
    facade contract): ``symbol.upper().replace(" ", "").replace("-", "")``
    transcribed token-for-token. The string OUT is the computed fold (the
    window_ledger "computed-ASCII-out" allowance — a conscious deviation
    from PR-1's integers-only discipline). Raises ``RuntimeError`` only on
    a caught panic.
    """

def spot_key_candidates(symbol: str, exchange: str) -> list[str]:
    """Law B — the ordered #911 drift-recovery candidates, identity
    excluded (ASCII-only by facade contract; the facade tuples the list).
    Strings OUT are the computed candidate strings (the computed-ASCII-out
    allowance). Raises ``RuntimeError`` only on a caught panic.
    """

def spot_recovery_candidates(
    underlying: str,
    exchange: str,
    alias_pairs: Sequence[tuple[str, str]],
    drift_bucket: Sequence[str],
) -> list[str]:
    """Law C — the full recovery-ladder CORE with the alias pairs + the
    stock-drift bucket INJECTED (the Python wrapper computes both — no
    Mapping crosses the FFI; ASCII-only by facade contract; the facade
    tuples the list). Strings OUT are the computed candidate strings (the
    computed-ASCII-out allowance). Raises ``RuntimeError`` only on a
    caught panic.
    """
