"""Shared movers-reader spy helpers (DEC-MOVERS-OPTIONS-PCR-001).

The zero-extra-I/O pin is asserted by TWO suites — the bench gate
(``tests/bench/test_movers_pcr_o1.py``) and the integration suite
(``tests/integration/test_dashboard_movers_options.py``) — so the spy lives
here in the COMMON test-helper location (Golden Rule #11), never inside a
collected test module (a rename/quarantine of one suite must not break the
other).
"""

from __future__ import annotations

from typing import Any

import pytest

#: The FOUR per-contract movers reader seams the options scan goes through
#: (movers_options.py read loop) — ALL spied, so "PCR adds NO Parquet read"
#: is pinned as stated, not for one reader (round-1 hostile-review widen).
MOVERS_READER_NAMES: tuple[str, ...] = (
    "_read_future_session",
    "_prev_day_close",
    "_prev_day_oi",
    "_underlying_spot_ltp",
)


def spy_all_movers_readers(module: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap every per-contract reader in *module* with a call counter."""
    counts = dict.fromkeys(MOVERS_READER_NAMES, 0)
    for name in MOVERS_READER_NAMES:
        real = getattr(module, name)

        def _counting(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            counts[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, name, _counting)
    return counts
