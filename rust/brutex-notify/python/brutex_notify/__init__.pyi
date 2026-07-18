# Type stub for the brutex_notify PyO3 wheel (DEC-NOTIFY-RUST-CORE-001).
#
# The four PURE notification laws. FFI discipline: `str` text, `int | None`
# counts, and IEEE-754 f64 BIT PATTERNS as `int` cross INTO Rust; only
# `str`/`bool` law results cross out. No float object, no datetime, no
# secret (token / chat id / config) ever crosses in either direction.

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def escape_markdown_v2(text: str) -> str: ...
def count_or_question(value: int | None) -> str: ...
def dedupe_should_drop(
    window_bits: int,
    last_seen_bits: int | None,
    now_bits: int,
) -> bool: ...
def truncate_id_12(text: str) -> str: ...
