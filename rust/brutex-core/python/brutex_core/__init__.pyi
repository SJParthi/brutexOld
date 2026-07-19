# Typing surface for the brutex_core PyO3 bridge (slice T35,
# DEC-CONFIG-RUST-LEAVES-001). Mirrors rust/brutex-core/src/lib.rs — keep
# in lockstep with the `#[pymodule]` registration block there.

__version__: str

def round_money_str(rupees: str) -> str:
    """Round a fixed-point rupee string to exactly two places, HALF_UP.

    The ``brutex.money.round_money`` twin on ``format(d, "f")`` strings:
    ties (and everything at-or-past the half) away from zero, carry
    propagated, sign preserved even on a zero result (``"-0.001"`` →
    ``"-0.00"``). Raises ``ValueError`` on a malformed string;
    ``RuntimeError`` if the core panicked (caught at the FFI boundary —
    a panic never crosses raw).
    """

def rupees_to_paisa_str(rupees: str) -> str:
    """Round to 2dp then the exact ×100 digit shift to an integer string.

    The ``brutex.money.rupees_to_paisa`` twin; a negative-zero round
    result collapses to ``"0"`` (the ``int()`` law). Raises ``ValueError``
    / ``RuntimeError`` per :func:`round_money_str`.
    """

def paisa_to_rupees_str(paisa: str) -> str:
    """The exact ÷100 digit shift of a ``str(int)`` paisa string to 2dp.

    The ``brutex.money.paisa_to_rupees`` twin (``"5"`` → ``"0.05"``).
    Raises ``ValueError`` / ``RuntimeError`` per :func:`round_money_str`.
    """

def format_inr_str(paisa: str) -> str:
    """Format a ``str(int)`` paisa string as ``₹`` + grouped 2dp.

    The ``brutex.money.format_inr`` twin — Python's western
    comma-grouping-by-3 (``"123456"`` → ``"₹1,234.56"``). Raises
    ``ValueError`` / ``RuntimeError`` per :func:`round_money_str`.
    """

def hwenv_pool_workers(
    performance_cores: int,
    logical_cores: int,
    prefer_performance: bool,
    reserve: int,
    cap: int | None,
) -> int:
    """The ``cpu_pool_workers`` derivation twin (milestone 2 — hwenv seam).

    ``base = performance_cores if prefer_performance else logical_cores``,
    then ``max(1, base - reserve)``, then the optional ``min(cap)`` (the
    cap applies AFTER the clamp — the Python law verbatim). Raises
    ``ValueError`` on an out-of-bounds crossing (``|v| > 2**31``);
    ``RuntimeError`` if the core panicked (caught at the FFI boundary).
    """

def hwenv_library_thread_budget(physical_cores: int, workers: int) -> int:
    """The ``library_thread_budget`` derivation twin.

    ``max(1, physical_cores // max(1, workers))`` — Python FLOOR division
    (``div_euclid``). Raises ``ValueError`` / ``RuntimeError`` per
    :func:`hwenv_pool_workers`.
    """

def hwenv_memory_budget(
    detected_ram_bytes: int,
    fraction: float,
    floor_bytes: int,
    workers: int,
) -> int:
    """The ``memory_budget_bytes`` post-override twin (DEC-RAM-BUDGETS-001).

    ``detected_ram_bytes == 0`` (the UNDETECTED_RAM sentinel) returns
    ``floor_bytes`` EXACTLY; a detected total derives
    ``max(1, int(total * fraction / workers))`` — never floor-clamped. The
    f64 chain mirrors CPython op-for-op; the facade's eligibility gate
    bounds ``detected < 2**62`` so the trunc→int cast is exact. Raises
    ``ValueError`` on a domain violation; ``RuntimeError`` on a caught
    panic.
    """

def hwenv_hardware_key(
    model_slug: str,
    physical_cores: int,
    performance_cores: int,
    efficiency_cores: int,
    total_ram_gib: int,
    arch: str,
) -> str:
    """The ``format_hardware_key`` render twin (DEC-BENCH-ABS-BASELINE-001).

    ``{model}-{phys}c{perf}p{eff}e-{ram}g-{arch}`` (``ram <= 0`` →
    ``"unk"``) then the ``slugify`` law over ASCII input. Raises
    ``ValueError`` on non-ASCII / out-of-bounds fields; ``RuntimeError``
    on a caught panic.
    """

def paths_lake_root_str(override_value: str | None, home: str) -> str:
    """The ``resolve_lake_root`` decision twin (milestone 3 — paths seam).

    A truthy (non-empty) override wins verbatim (normalized per the
    CPython ``PurePosixPath`` law), else ``<home>/.brutex/lake``. The
    facade gates ``~``-prefixed overrides + non-ASCII to the oracle.
    Raises ``ValueError`` on an out-of-envelope crossing;
    ``RuntimeError`` if the core panicked (caught at the FFI boundary).
    """

def paths_volume_parent_str(path: str) -> str:
    """The ``lake_root_identity`` volume-parent twin (milestone 3).

    ``"/Volumes/<name>"`` when *path*'s parts read ``("/", "Volumes",
    <name>, ...)``, else the empty string (the facade's no-parent
    sentinel). Raises ``ValueError`` / ``RuntimeError`` per
    :func:`paths_lake_root_str`.
    """

def dotenv_parse_pairs(text: str) -> list[tuple[str, str]]:
    """The ``parse_dotenv`` line-parse twin (milestone 3 — dotenv seam).

    Returns the deduped ordered pairs — exactly
    ``list(parse_dotenv(text).items())`` (first-insertion position, last
    value wins). ASCII text only (the facade gate); raises ``ValueError``
    / ``RuntimeError`` per :func:`paths_lake_root_str`.
    """

def backupenv_verify_subset_valid(value: str) -> bool:
    """The ``_valid_verify_subset`` shape twin (milestone 3 — backupenv).

    True iff *value* is a shape restic's ``--read-data-subset`` accepts
    (``n/t`` · ``x%`` · ``<int><b|k|m|g|t>`` — DEC-BACKUP-OFFBOX-001
    round-7/round-9 bounds, incl. the Go int64 overflow). Raises
    ``ValueError`` / ``RuntimeError`` per :func:`paths_lake_root_str`.
    """
