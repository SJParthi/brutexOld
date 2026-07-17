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
