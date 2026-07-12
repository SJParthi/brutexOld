# Type stub for the brutex_feed PyO3 wheel (DEC-FEED-RUST-CORE-001).
#
# INTEGERS ONLY across the FFI: `scan_bars` returns kept row indices, owed
# bad-tick WARN coordinates `(emit_at_row, session_first_row, skipped,
# first_prev_row, first_skip_row)`, and the positivity-stop coordinate
# `(row, field)` — or `None` (abstain: a consulted timestamp lies outside
# `[0, 9999-12-31T00:00:00Z)` in epoch microseconds; the caller degrades to
# the Python oracle). No float, string, or timestamp ever crosses
# Rust → Python output (the structural bit-identity guarantee).

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def scan_bars(
    ts_us: list[int],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    screen_enabled: bool,
    max_ratio: float,
) -> (
    tuple[
        list[int],
        list[tuple[int, int, int, int, int]],
        tuple[int, int] | None,
    ]
    | None
): ...
