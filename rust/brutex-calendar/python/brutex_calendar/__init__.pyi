# Type stub for the brutex_calendar PyO3 wheel (DEC-RUST-CALENDAR-LEAVES-001).
#
# Ints/strings/bools/bytes ONLY across the FFI. `classify_month_drift`
# consumes DayEvidence-ordered rows and returns the DriftVerdict fields the
# Python side reassembles; `validate_settle_evidence` returns None on a
# JSON parse-engine ABSTAIN (degrade to the Python validator), else
# (valid, message, adopted_hhmm, generated_at_raw) with the refusal message
# byte-identical to the Python ValueError text; `parse_observed_sidecar`
# returns the sorted overlay day list or None (whole-overlay rejection) and
# raises ValueError on a parse-engine ABSTAIN.

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def classify_month_drift(
    evidence: list[tuple[str, bool, int, int, int]],
    existing_year_overlay_days: int,
    max_flags_per_month: int,
    max_overlay_days_per_year: int,
) -> tuple[
    list[str],
    list[tuple[str, int]],
    list[str],
    list[str],
    bool,
    bool,
]: ...
def validate_settle_evidence(
    payload_json: bytes,
    path_repr: str,
) -> tuple[bool, str, str | None, str | None] | None: ...
def parse_observed_sidecar(payload_json: bytes) -> list[str] | None: ...
def version() -> str: ...
