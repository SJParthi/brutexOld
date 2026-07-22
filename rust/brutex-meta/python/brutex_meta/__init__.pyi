# Type stub for the brutex_meta PyO3 wheel (DEC-META-LABEL-ANNOTATE-001 +
# DEC-ABSTAIN-GATE-STREAM-001).
#
# Floats/ints-only FFI: `score_logistic` returns one probability per row of
# the flattened row-major feature matrix; `abstain_masks` returns one reason
# BITMASK per element (bit order = brutex/meta/abstain.py::ABSTAIN_REASON_ORDER;
# 0 = pass). NaN encodings: vix NaN = missing stamp, vix_lo/vix_hi NaN = that
# band side unbounded, confidence NaN = missing P(win), tau NaN = no
# confidence gate. Structural violations raise ValueError; a caught kernel
# panic raises RuntimeError — the facade degrades both to the Python oracle.

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def score_logistic(
    features: list[float],
    n_features: int,
    weights: list[float],
    bias: float,
) -> list[float]: ...
def abstain_masks(
    regimes: list[int],
    allowed_mask: int,
    vix: list[float],
    vix_lo: float,
    vix_hi: float,
    event_flags: list[int] | bytes,
    block_event_days: bool,
    confidence: list[float],
    tau: float,
) -> bytes: ...
