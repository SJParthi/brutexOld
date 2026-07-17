# Typing surface for the brutex_data PyO3 bridge (ADR-0020 slice T10 PR-1,
# DEC-DATA-RUST-PURELAW-001). Mirrors rust/brutex-data/src/lib.rs — keep in
# lockstep with the `#[pymodule]` registration block there.

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
