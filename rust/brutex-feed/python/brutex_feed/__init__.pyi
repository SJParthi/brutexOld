# Type stub for the brutex_feed PyO3 wheel (DEC-FEED-RUST-CORE-001;
# DEC-BADTICK-HEAD-SEED-001 + DEC-BADTICK-HILO-SCREEN-001 event vocabulary).
#
# INTEGERS ONLY across the FFI: `scan_bars` returns kept row indices, owed
# screen-WARN coordinates — the 9-int decision-plane records shared with
# `bad_tick.BadTickScreen.screen_mask`:
# `(kind, emit_at_row, session_first_row, a, b, c, d, e, f)` where kind
# 0=SKIP `(skipped, first_ref_row, first_skip_row, first_reason, n_high,
# n_low)`, 1=HEAD_UNCONFIRMED `(head_row_0, head_row_1, 0, 0, 0, 0)`,
# 2=MAJORITY_DROPPED `(skipped, kept_in_session, 0, 0, 0, 0)` — and the
# positivity-stop coordinate `(row, field)` — or `None` (abstain: a
# consulted timestamp lies outside `[0, 9999-12-31T00:00:00Z)` in epoch
# microseconds; the caller degrades to the Python oracle). No float, string,
# or timestamp ever crosses Rust → Python output (the structural
# bit-identity guarantee).

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
        list[tuple[int, int, int, int, int, int, int, int, int]],
        tuple[int, int] | None,
    ]
    | None
): ...
