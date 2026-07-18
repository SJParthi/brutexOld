# Type stubs for brutex_identity (DEC-RUST-RUNID-STORES-001).
#
# Shipped in-wheel beside py.typed so `mypy --strict` + pyright resolve the
# compiled surface exactly (the fno_math / brutex_feed stub convention).

import datetime
from collections.abc import Mapping, Sequence
from typing import Any, Final

__version__: Final[str]
__build_crate_version__: Final[str]
__source_digest__: Final[str]
SOURCE_DIGEST: Final[str]

#: The shim's compiled-surface symbol roster (the fno_math stub precedent —
#: the wheel-handshake suite reads it, so the stub must declare it).
_EXPECTED_COMPILED_SYMBOLS: Final[tuple[str, ...]]

#: The fixed 16-column window-ledger schema order (the ONE schema authority).
#: The pyo3 runtime exports a Python LIST (the store docstring says so) —
#: the stub matches the runtime type exactly (refuter NIT, 2026-07-18).
LEDGER_COLUMNS: Final[list[str]]
#: The 11 required core columns (the read-side wrong-shape guard).
LEDGER_CORE_COLUMNS: Final[list[str]]

#: One serialised window-ledger row — the 16-field tuple of
#: `WindowLedger.to_rows` (`LedgerRowTuple` on the brutex side).
type LedgerRowTupleFfi = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    int,
    str | None,
    str,
    str | None,
    int,
    int,
    int | None,
    int | None,
    int | None,
]

#: One marshalled ResultRow — dataclass field order, condition_names LAST,
#: prices as their exact str(Decimal) texts.
type ResultRowTupleFfi = tuple[
    str,
    str,
    str,
    str,
    str,
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    float,
    float,
    float,
    float,
    float,
    float,
    str,
    str,
    float | None,
    float | None,
    float | None,
    float | None,
    str,
]

def version() -> str: ...
def canonicalise_params_json(params: Mapping[str, Any]) -> str: ...
def combo_hash(
    strategy_id: str,
    direction: str,
    instrument: str,
    tf: str,
    mode: str,
    params: Mapping[str, Any],
    data_snapshot_hash: str,
    indicator_version: str,
    code_commit_sha: str,
) -> str: ...
def find_unit_separator_field(fields: Sequence[tuple[str, str]]) -> str | None: ...
def derive_empty_streak(status: str, attempts: int, skipped_streak: int) -> int: ...
def derive_in_pull_fetches(status: str) -> int: ...
def classify_result_append(
    existing: ResultRowTupleFfi | None,
    incoming: ResultRowTupleFfi,
) -> tuple[
    str,
    list[str],
    tuple[float | None, float | None, float | None] | None,
    list[str],
]: ...
def validate_run_id_error(run_id: str, expected_len: int) -> str | None: ...
def mask_to_limbs(mask: int) -> tuple[int, int]: ...
def mask_from_limbs(lo_signed: int, hi_signed: int) -> int: ...
def rank_top_k(
    run_ids: Sequence[str],
    keys: Sequence[int] | Sequence[float],
    k: int,
    descending: bool,
) -> list[int]: ...
def merge_ledger_rows(
    memory_rows: Sequence[LedgerRowTupleFfi],
    disk_rows: Sequence[LedgerRowTupleFfi],
    pruned_keys: frozenset[tuple[str, str, str, str, str]],
    reopened_keys: frozenset[tuple[str, str, str, str, str]],
    degraded_keys: frozenset[tuple[str, str, str, str, str]],
) -> list[LedgerRowTupleFfi]: ...
def render_ledger_parquet(rows: Sequence[LedgerRowTupleFfi]) -> bytes: ...
def read_ledger_rows(
    data: bytes,
    skipped_streak: int,
    pool: LedgerStringPool | None = None,
) -> list[LedgerRowTupleFfi]: ...

class SnapshotHasher:
    def __init__(self, exchange: str, segment: str, start: str, end: str) -> None: ...
    def add_slice(self, symbol: str, interval: str, bar_count: int) -> None: ...
    def add_bar(
        self,
        ts: datetime.datetime,
        open: str,
        high: str,
        low: str,
        close: str,
        volume: int,
        open_interest: int | None,
    ) -> None: ...
    def hexdigest(self) -> str: ...

class LedgerStringPool:
    def __init__(self) -> None: ...
    def __len__(self) -> int: ...
