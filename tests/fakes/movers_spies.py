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
#: The prev-close/OI seams are the enriched resolvers of
#: DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 (``resolve_prev_close`` /
#: ``resolve_prev_oi`` — the renamed ``_prev_day_close`` / ``_prev_day_oi``
#: value seams; same one-read-per-contract law).
MOVERS_READER_NAMES: tuple[str, ...] = (
    "_read_future_session",
    "resolve_prev_close",
    "resolve_prev_oi",
    "_underlying_spot_ltp",
)


def spy_all_movers_readers(module: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap every per-contract reader with a call counter.

    Each seam is patched on the module that actually CALLS it: a reader
    imported into *module*'s namespace is patched there; a reader *module* no
    longer holds is patched at its :mod:`brutex.dashboard.movers_futures` home
    (the DEC-MOVERS-FUTURES-HYPHEN-PARSE-001 W3 fold-in moved the spot probe
    behind ``_SpotLtpResolver``, whose exact-key ``_probe`` calls the
    module-global ``_underlying_spot_ltp`` there — the one-exact-spot-read-
    per-contract law is pinned UNCHANGED through the resolver).
    """
    from brutex.dashboard import movers_futures

    counts = dict.fromkeys(MOVERS_READER_NAMES, 0)
    for name in MOVERS_READER_NAMES:
        target = module if hasattr(module, name) else movers_futures
        real = getattr(target, name)

        def _counting(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            counts[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(target, name, _counting)
    return counts
