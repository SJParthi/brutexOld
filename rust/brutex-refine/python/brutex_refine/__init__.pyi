# Type stubs for the brutex_refine PyO3 wheel (DEC-RUST-STAGE3-REFINE-001).
#
# Hand-maintained beside the compiled module (the brutex_feed shape); the
# runtime handshake in __init__.py pins the symbol surface, and the Python
# consumer (`brutex/engine/refine_rust.py`) type-checks against these under
# `mypy --strict` + pyright.

SOURCE_DIGEST: str
__version__: str
__build_crate_version__: str
__source_digest__: str

def grid_validate(name: str, lo_paisa: int, hi_paisa: int, tick_paisa: int) -> int: ...
def grid_denormalize(name: str, lo_paisa: int, hi_paisa: int, tick_paisa: int, u: float) -> int: ...
def grid_normalize(
    name: str, lo_paisa: int, hi_paisa: int, tick_paisa: int, paisa: int
) -> float: ...

class RefineSession:
    def __init__(
        self,
        x0: tuple[float, float],
        sigma0: float,
        seed: int,
        max_fevals: int,
        max_restarts: int,
        sl_axis: tuple[int, int, int],
        tp_axis: tuple[int, int, int],
    ) -> None: ...
    def ask(self) -> list[tuple[int, int]]: ...
    def tell(self, values: list[float]) -> None: ...
    def done(self) -> bool: ...
    def restarts_run(self) -> int: ...

class SelectSession:
    def __init__(self, top_k: int) -> None: ...
    def push(
        self, instrument: str, tf: str, mode: str, sharpe: float, run_id: str
    ) -> tuple[bool, int | None]: ...
    def finish(self) -> list[tuple[tuple[str, str, str], list[int]]]: ...
