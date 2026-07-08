"""Dormant GPU-gate drift pin — ADR-0004 §6's precondition is mechanically FAIL-CLOSED.

DEC-GPU-GATE-RECONCILE-001 context: ADR-0004 (DEC-GPU-ACCELERATION-ADR-001,
MIGRATION.md #410) hard-gates any GPU offload behind an empirical tri-platform
micro-benchmark (Rust-CPU baseline vs MLX vs CuPy) that has NEVER RUN. The
CPU-first arm was executed + measured the same day (#411/#413/#417); the
benchmark is RETIRED as scheduled work but RETAINED as a dormant fail-closed
PRECONDITION — no GPU code may merge before the benchmark (plus a fresh DEC)
lands. Prose preconditions rot (ADR-0004's "(still-to-be-built)" wording sat
stale for a month despite the D-7 audit flag), so this test pins the
precondition mechanically — the G3 drift-pin pattern of
DEC-SUSPICION-SWEEP-FIXES-001 (MIGRATION.md #1492).

The pin, over SOURCE trees only (``docs/`` and ``tests/`` are never scanned;
Rust ``target/`` build trees are excluded so vendored third-party sources can
never false-positive):

* NO ``brutex/**/*.py`` module imports ``mlx``, ``cupy``, or ``cupyx`` (the
  extension namespace shipped INSIDE the CuPy wheel, importable standalone
  as ``from cupyx import scipy`` — round-2 reconstruction-review fix: the
  {mlx, cupy} roots and the module-path regex both missed it), nor the
  ``numba.cuda`` subtree — ADR-0004 §5 item 3's own sanctioned Numba-CUDA
  FALLBACK kernel host; ``numba`` ITSELF stays an allowed CPU dependency
  (CLAUDE.md §4 "numpy + numba"), so only its ``.cuda`` subpath is
  forbidden, caught in every reachable spelling: ``import numba.cuda``,
  ``from numba import cuda``, ``from numba.cuda import …``, the LITERAL
  dynamic-import string, and the ``numba.cuda`` ATTRIBUTE shape reachable
  from a plain ``import numba`` (round-2 reconstruction-review fix: every
  one of those previously evaded the Python arm because the benign
  ``numba`` root passed the import scan). All AST-level, so a docstring or
  comment mention never false-positives — and a word like ``occupy`` never
  matches at all. None reads ``BRUTEX_GPU_OFFLOAD`` (any NON-docstring
  string constant carrying the flag counts as a read; docstring
  removal-notes stay sanctioned, mirroring the repo's removed-flag
  docstring convention), and no NON-docstring string constant is a LITERAL
  GPU-rooted module path (``"cupy"``, ``"cupyx.scipy"``, ``"mlx.core"``,
  ``"numba.cuda"``, …) — so the dynamic-import shapes
  ``importlib.import_module("cupy")`` / ``__import__("mlx.core")`` are
  caught via their string argument (round-2 hostile-review fix).
* NO ``rust/**/*.rs`` source references an ``mlx``-/``cupy``-PREFIXED
  identifier token (``mlx``, ``mlx_rs``, ``mlx_sys``, ``cupy_backend``, …
  — the real-world MLX crates import as ``mlx_rs``/``mlx_sys``, and ``_``
  is a word character, so the earlier trailing-``\\b`` match missed them:
  round-2 hostile-review fix; the LEADING word boundary still keeps
  ``occupy`` excluded) or ``BRUTEX_GPU_OFFLOAD``, after
  string-literal-aware comment stripping (a ``//`` inside a string literal
  never opens a comment — see ``_strip_rust_comments``).

Honest envelope (Python arm): static imports are AST-detected in every
form and LITERAL module-path/env-flag strings are caught, but a COMPUTED
module path or flag string (``"BRUTEX_GPU_" + "OFFLOAD"``, f-strings,
joins) is NOT detected, and the ``numba.cuda`` ATTRIBUTE check fires only
on the literal ``numba`` name — an offload through a REBOUND root
(``import numba as nb; nb.cuda…``) is the same computed-shape class. Both
are DISCLOSED residuals (catching computation/aliasing needs symbolic
evaluation; the literal import/flag shapes are the ones a real
GPU-offload PR writes, and CI review covers the rest).

A failure means GPU-offload wiring appeared without the sanctioned path:
run the ADR-0004 §6 tri-platform benchmark and record a fresh DEC first.
Stdlib-only, deterministic, O(source files).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import brutex

#: Forbidden GPU package ROOTS. ADR-0004 §5 item 3 names CuPy (with a
#: Numba-CUDA fallback) and item 4 names MLX as the sanctioned future kernel
#: hosts — so the root imports a GPU-offload PR would introduce are ``mlx``,
#: ``cupy``, and ``cupyx`` (the extension namespace shipped INSIDE the CuPy
#: wheel; round-2 reconstruction-review fix — previously missed by both the
#: import roots and the module-path regex, while the Rust arm's PREFIX match
#: would have caught the analogous token).
_GPU_MODULES = frozenset({"mlx", "cupy", "cupyx"})

#: Forbidden DOTTED module paths whose ROOT package stays sanctioned:
#: ``numba`` is an explicitly ALLOWED CPU dependency (CLAUDE.md §4 — "use
#: `polars` or `numpy` + `numba`"), but ADR-0004 §5 item 3 names Numba-CUDA
#: as the sanctioned CuPy FALLBACK kernel host, so only the ``numba.cuda``
#: subtree is forbidden (round-2 reconstruction-review fix: a Numba-CUDA
#: offload previously evaded the Python arm entirely because the
#: benign-looking ``numba`` root passed the import scan).
_GPU_MODULE_PATHS = frozenset({"numba.cuda"})

#: The GPU-offload env flag ADR-0004 §5 item 3 reserves; it exists in docs
#: only and must stay out of ``brutex/`` + ``rust/`` source until the §6
#: benchmark lands.
_GPU_ENV_FLAG = "BRUTEX_GPU_OFFLOAD"

_PACKAGE_ROOT = Path(brutex.__file__).resolve().parent
_REPO_ROOT = _PACKAGE_ROOT.parent
_RUST_ROOT = _REPO_ROOT / "rust"

#: Directory names never scanned: build artifacts (``target``) and the
#: non-production trees the pin deliberately excludes (``tests``, ``docs``).
_EXCLUDED_DIR_NAMES = frozenset({"target", "tests", "docs"})

_FAILURE_HINT = (
    "GPU-offload wiring detected — FORBIDDEN while the ADR-0004 §6 tri-platform "
    "micro-benchmark remains un-run (docs/adr/0004-gpu-acceleration.md §6, "
    "DEC-GPU-GATE-RECONCILE-001): GPU code requires the §6 benchmark (Rust-CPU "
    "baseline vs MLX vs CuPy, measured throughput + bit-exact CPU-GPU parity) "
    "+ a fresh DEC first. Offenders: "
)

#: PREFIX-token match, deliberately NOT ``\b(?:mlx|cupy)\b``: ``_`` is a
#: word character, so a trailing ``\b`` never fires after ``mlx`` in
#: ``mlx_rs``/``mlx_sys`` — the actual crates an MLX-Rust port imports
#: (there is no bare ``mlx`` crate; round-2 hostile-review fix). The
#: LEADING ``\b`` keeps ``occupy``/``preoccupy_mlx`` excluded.
_RS_GPU_TOKEN = re.compile(rf"\b(?:mlx|cupy)\w*|{_GPU_ENV_FLAG}")

#: A LITERAL GPU-rooted module path ("cupy", "cupyx.scipy", "mlx.core",
#: "numba.cuda", …) — the string argument of the dynamic-import shapes
#: ``importlib.import_module(...)`` / ``__import__(...)``. Fullmatch-anchored:
#: ``occupy`` / ``mlx_backend`` / ``x.cupy`` / ``numba`` / ``numba.cudax``
#: never match.
_GPU_MODULE_PATH_RE = re.compile(r"(?:mlx|cupyx?|numba\.cuda)(?:\.\w+)*")


def _is_gpu_module_path(name: str) -> bool:
    """True iff ``name`` is a forbidden module path: rooted at a forbidden
    package (``mlx``/``cupy``/``cupyx``), or equal to / nested under a
    forbidden dotted path (``numba.cuda``)."""
    if name.partition(".")[0] in _GPU_MODULES:
        return True
    return any(name == path or name.startswith(path + ".") for path in _GPU_MODULE_PATHS)


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """Identity set of the string constants sitting in docstring position."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    ids.add(id(value))
    return ids


def _py_gpu_references(tree: ast.Module) -> list[str]:
    """Every GPU-wiring signal in one parsed Python module (empty = clean)."""
    reasons: list[str] = []
    docstrings = _docstring_constant_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            reasons.extend(
                f"import {alias.name} (line {node.lineno})"
                for alias in node.names
                if _is_gpu_module_path(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level != 0:
                continue
            if _is_gpu_module_path(module):
                reasons.append(f"from {module} import ... (line {node.lineno})")
            else:
                # `from numba import cuda` — the forbidden path is assembled
                # from the sanctioned parent module + the imported name.
                reasons.extend(
                    f"from {module} import {alias.name} (line {node.lineno})"
                    for alias in node.names
                    if _is_gpu_module_path(f"{module}.{alias.name}")
                )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and f"{node.value.id}.{node.attr}" in _GPU_MODULE_PATHS
        ):
            # `import numba; numba.cuda.jit(...)` — the forbidden subtree is
            # reachable as an ATTRIBUTE of the sanctioned root with no
            # `.cuda` import statement anywhere.
            reasons.append(f"attribute {node.value.id}.{node.attr} (line {node.lineno})")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if _GPU_ENV_FLAG in node.value:
                reasons.append(f"{_GPU_ENV_FLAG} string constant (line {node.lineno})")
            elif _GPU_MODULE_PATH_RE.fullmatch(node.value):
                reasons.append(
                    f"GPU module-path string {node.value!r} (line {node.lineno}) — "
                    "the dynamic-import shape"
                )
    return reasons


def _strip_rust_comments(source: str) -> str:
    """Remove Rust line + block comments, tracking double-quoted strings.

    A character scanner, not bare regexes: a ``//`` or ``/*`` INSIDE a
    normal double-quoted string literal (with backslash escapes honoured)
    never opens a comment — the round-1 hostile-review silent-pass corner
    where ``let url = "https://x"; use cupy::…`` lost the real wiring to a
    line-comment regex firing on the URL's ``//``. Block comments nest,
    matching the Rust grammar; string literal CONTENT is retained so an
    ``env::var("BRUTEX_GPU_OFFLOAD")`` read stays detectable.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    in_string = False
    block_depth = 0
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if block_depth:
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 2
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                i += 2
            else:
                i += 1
        elif in_string:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            block_depth = 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _rs_gpu_references(source: str) -> list[str]:
    """Every GPU-wiring token in one Rust source string, comments stripped.

    Honest envelope: ``_strip_rust_comments`` approximates Rust lexing —
    normal double-quoted strings (with escapes) and nested block comments
    are tracked exactly, so a comment marker inside such a string can never
    mask real ``use mlx``/``cupy`` wiring (teeth-pinned below). Raw-string
    literals with embedded quotes (``r#"…"…"#``) and char literals holding
    a quote (``'"'``) are mis-lexed: in those corners the scanner errs
    toward KEEPING text (comment text survives → a loud false POSITIVE
    naming the file), but a ``//`` landing in a raw string's mis-lexed
    middle could still shadow same-line code — a narrow DISCLOSED residual,
    not an absolute "never a silent pass" guarantee.
    """
    return [match.group(0) for match in _RS_GPU_TOKEN.finditer(_strip_rust_comments(source))]


def _is_excluded(rel: Path) -> bool:
    return bool(_EXCLUDED_DIR_NAMES & set(rel.parts[:-1]))


def _python_sources() -> list[Path]:
    return sorted(
        path
        for path in _PACKAGE_ROOT.rglob("*.py")
        if not _is_excluded(path.relative_to(_PACKAGE_ROOT))
    )


def _rust_sources() -> list[Path]:
    if not _RUST_ROOT.is_dir():
        return []
    return sorted(
        path for path in _RUST_ROOT.rglob("*.rs") if not _is_excluded(path.relative_to(_RUST_ROOT))
    )


class TestGpuGateDriftPin:
    """The dormant ADR-0004 §6 precondition cannot be silently bypassed."""

    def test_no_gpu_wiring_in_python_sources(self) -> None:
        offenders: list[str] = []
        for path in _python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = path.relative_to(_REPO_ROOT)
            offenders.extend(f"{rel}: {reason}" for reason in _py_gpu_references(tree))
        assert offenders == [], _FAILURE_HINT + str(offenders)

    def test_no_gpu_wiring_in_rust_sources(self) -> None:
        offenders: list[str] = []
        for path in _rust_sources():
            hits = _rs_gpu_references(path.read_text(encoding="utf-8"))
            if hits:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {sorted(set(hits))}")
        assert offenders == [], _FAILURE_HINT + str(offenders)

    def test_scan_is_not_vacuous(self) -> None:
        """The pin can never pass by scanning nothing — a tree move must update
        this test consciously (the G3 sanctioned-site liveness twin)."""
        assert len(_python_sources()) > 100, (
            "brutex/ scan went vacuous — repoint the ADR-0004 §6 drift pin at the new layout"
        )
        assert _RUST_ROOT.is_dir(), (
            "rust/ tree not found next to the brutex package — repoint the "
            "ADR-0004 §6 drift pin at the new layout"
        )
        assert len(_rust_sources()) > 10, (
            "rust/ scan went vacuous — repoint the ADR-0004 §6 drift pin at the new layout"
        )


class TestDetectorTeeth:
    """In-memory negative fixtures prove the detector fires (#1180 harness pattern)."""

    def test_plain_import_detected(self) -> None:
        assert _py_gpu_references(ast.parse("import cupy\n"))

    def test_dotted_aliased_and_from_imports_detected(self) -> None:
        assert _py_gpu_references(ast.parse("import mlx.core as mx\n"))
        assert _py_gpu_references(ast.parse("from cupy import RawKernel\n"))
        assert _py_gpu_references(ast.parse("from mlx.core import array\n"))

    def test_env_flag_read_detected(self) -> None:
        source = 'import os\nFLAG = os.environ.get("BRUTEX_GPU_OFFLOAD", "0")\n'
        assert _py_gpu_references(ast.parse(source))

    def test_docstring_mention_is_sanctioned(self) -> None:
        source = '"""Removal note: BRUTEX_GPU_OFFLOAD does not exist."""\nX = 1\n'
        assert _py_gpu_references(ast.parse(source)) == []

    def test_comments_and_occupy_never_false_positive(self) -> None:
        source = "# forbidden words in a comment: cupy mlx BRUTEX_GPU_OFFLOAD\noccupy = 1\n"
        assert _py_gpu_references(ast.parse(source)) == []

    def test_relative_or_foreign_submodule_never_false_positive(self) -> None:
        assert _py_gpu_references(ast.parse("from . import cupy_shim\n")) == []
        assert _py_gpu_references(ast.parse("from mypkg import cupy_shim\n")) == []

    def test_python_dynamic_import_string_detected(self) -> None:
        """Round-2 hostile-review regression pin: the dynamic-import shapes
        carry no ``import`` statement, so the AST import scan alone passed
        them silently — the LITERAL module-path string argument must be
        caught."""
        assert _py_gpu_references(
            ast.parse('import importlib\nmod = importlib.import_module("cupy")\n')
        )
        assert _py_gpu_references(ast.parse('mx = __import__("mlx.core")\n'))

    def test_python_module_path_lookalikes_sanctioned(self) -> None:
        """Fullmatch anchoring: near-miss string constants never false-positive."""
        for probe in (
            'X = "occupy"\n',
            'X = "mlx_backend"\n',
            'X = "a.cupy"\n',
            'X = "MLX"\n',
            'X = "numba"\n',
            'X = "numba.cudax"\n',
            'X = "cupyxx"\n',
        ):
            assert _py_gpu_references(ast.parse(probe)) == [], probe

    def test_numba_cuda_offload_shapes_detected(self) -> None:
        """Round-2 reconstruction-review regression pin: ADR-0004 §5 item 3
        itself names Numba-CUDA as the sanctioned CuPy FALLBACK kernel host,
        and ``numba`` is an allowed CPU dependency (CLAUDE.md §4) — so every
        ``numba.cuda`` offload spelling looked benign to the {mlx, cupy}
        import scan and evaded the Python arm entirely (fail-open)."""
        assert _py_gpu_references(ast.parse("import numba.cuda\n"))
        assert _py_gpu_references(ast.parse("from numba import cuda\n"))
        assert _py_gpu_references(ast.parse("from numba import njit, cuda\n"))
        assert _py_gpu_references(ast.parse("from numba.cuda import jit\n"))
        assert _py_gpu_references(
            ast.parse('import importlib\nmod = importlib.import_module("numba.cuda")\n')
        )
        assert _py_gpu_references(
            ast.parse(
                "import numba\n\ndef f(k):  # attribute shape, no .cuda import\n"
                "    return numba.cuda.jit(k)\n"
            )
        )

    def test_numba_cpu_usage_stays_sanctioned(self) -> None:
        """CLAUDE.md §4 sanctions numba on CPU hot paths: the plain root
        import, CPU-only from-imports, a bare ``"numba"`` string, and a
        non-numba ``.cuda`` attribute must never false-positive."""
        assert _py_gpu_references(ast.parse("import numba\n")) == []
        assert _py_gpu_references(ast.parse("from numba import njit, prange\n")) == []
        assert _py_gpu_references(ast.parse("dev = torchlike.cuda\n")) == []

    def test_cupyx_import_and_string_detected(self) -> None:
        """Round-2 reconstruction-review regression pin: ``cupyx`` ships
        INSIDE the CuPy wheel and is importable standalone (``from cupyx
        import scipy``) — the {mlx, cupy} import roots and the fullmatch
        module-path regex both missed it (fail-open), while the Rust arm's
        PREFIX match would have caught the analogous token."""
        assert _py_gpu_references(ast.parse("import cupyx\n"))
        assert _py_gpu_references(ast.parse("from cupyx import scipy\n"))
        assert _py_gpu_references(ast.parse('mod = importlib.import_module("cupyx.scipy")\n'))

    def test_rust_real_world_mlx_crate_idents_detected(self) -> None:
        """Round-2 hostile-review regression pin: the real-world MLX Rust
        crates import as ``mlx_rs``/``mlx_sys`` (there is NO bare ``mlx``
        crate), and ``_`` is a word character — the earlier
        trailing-``\\b`` token match returned [] for every probe below
        (fail-open: the Rust arm's teeth reduced to the env-flag string)."""
        assert _rs_gpu_references("use mlx_rs::device;\n")
        assert _rs_gpu_references("use mlx_sys::ffi;\n")
        assert _rs_gpu_references("extern crate mlx_sys;\n")
        assert _rs_gpu_references("let k = cupy_backend::RawKernel::new();\n")

    def test_rust_use_env_and_path_calls_detected(self) -> None:
        assert _rs_gpu_references("use cupy::RawKernel;\n")
        assert _rs_gpu_references('let flag = std::env::var("BRUTEX_GPU_OFFLOAD");\n')
        assert _rs_gpu_references("let dev = mlx::device();\n")

    def test_rust_comments_and_occupy_sanctioned(self) -> None:
        assert _rs_gpu_references("// forbidden in a comment: cupy mlx\nfn occupy() {}\n") == []
        assert _rs_gpu_references("/* mlx cupy BRUTEX_GPU_OFFLOAD\nspanning */ fn f() {}\n") == []

    def test_rust_comment_marker_inside_string_cannot_mask_wiring(self) -> None:
        """Round-1 hostile-review regression pin: a ``//`` or ``/*`` INSIDE a
        normal string literal must never swallow real wiring after it (the
        regex stripper silently passed BOTH of these probes)."""
        assert _rs_gpu_references('let url = "https://x.dev"; use cupy::RawKernel;\n')
        assert _rs_gpu_references('let a = "/*"; use mlx::device; let b = "*/";\n')
        assert _rs_gpu_references(
            'let url = "https://x.dev"; let f = std::env::var("BRUTEX_GPU_OFFLOAD");\n'
        )

    def test_rust_nested_block_comment_sanctioned(self) -> None:
        """Rust block comments NEST; a mention between the nested close and
        the OUTER close must stay sanctioned (the char scanner tracks depth;
        the old non-greedy regex closed at the FIRST ``*/`` and would have
        false-positived here — the loud direction, now simply exact)."""
        assert _rs_gpu_references("/* outer /* nested */ cupy mlx */ fn f() {}\n") == []
