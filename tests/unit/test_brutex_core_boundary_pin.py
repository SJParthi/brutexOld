"""The mechanical AWS-boundary pin for the Rust-seam program (slice T35).

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943, milestone 3 — the
fleet-mandated standing obligation). Three mechanical laws, each a pure
source/manifest scan in the #1492-G3 AST-pin idiom (anti-vacuousness
teeth on every detector):

(a) the ``rust/brutex-core`` crate's dependency graph carries ZERO
    AWS-related crates (``aws-sdk-*`` / ``rusoto*`` / ``aws-*`` …) —
    parsed from BOTH ``Cargo.toml`` and the locked full graph in
    ``Cargo.lock``, so a transitive dep cannot sneak in either;
(b) NO ``brutex/**/*_rust.py`` facade imports
    ``brutex.config.credentials_aws`` or ``boto3``/``botocore`` at ANY
    scope (module or function-local — ``ast.walk`` sees both), so no
    seam can ever route through the AWS reader chain;
(c) the credentials boundary: no facade's ``_CRATE_API`` handshake tuple
    names an AWS/credential-related callable, AND
    ``brutex/config/credentials.py`` itself imports no ``*_rust`` facade
    and no ``brutex_core`` — the milestone-3 verdict for that module is
    NO SEAM (``token_shape_problem`` is production-dead;
    ``normalise_token`` is a trivial secret-bearing strip — the smaller
    honest seam is none), so the AWS-delegator arm stays byte-untouched
    Python by construction and this pin keeps it that way mechanically.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CRATE = _REPO / "rust" / "brutex-core"
_BRUTEX = _REPO / "brutex"

#: Crate-name pattern for AWS-related Rust dependencies.
_AWS_CRATE_RE = re.compile(r"^(aws[-_].*|rusoto.*|aws)$", re.IGNORECASE)

#: Python import roots a facade must NEVER touch.
_BANNED_PY_IMPORTS = frozenset({"boto3", "botocore"})
_BANNED_PY_MODULES = frozenset({"brutex.config.credentials_aws", "brutex.config.credentials"})

#: Leaf name → banned module, for the RELATIVE-import belt (r2 NIT): a
#: pure source scan cannot resolve ``from . import credentials_aws`` /
#: ``from .credentials_aws import x`` to an absolute package, so any
#: relative import whose module component or alias matches a banned
#: module's LEAF is CONSERVATIVELY recorded as that banned module.
_BANNED_PY_LEAVES = {m.rsplit(".", maxsplit=1)[-1]: m for m in _BANNED_PY_MODULES}

#: AWS/credential-related tokens banned from any facade's crate-API
#: handshake (the "routes ONLY parse/shape/derivation laws" law).
_BANNED_API_TOKEN_RE = re.compile(
    r"aws|ssm|boto|credential|token|totp|secret|jwt|password", re.IGNORECASE
)


def _facade_files() -> list[Path]:
    return sorted(_BRUTEX.rglob("*_rust.py"))


def _cargo_lock_packages(text: str) -> list[str]:
    """Package names from a Cargo.lock (the ``name = "..."`` rows)."""
    return re.findall(r'^name = "([^"]+)"$', text, flags=re.MULTILINE)


def _cargo_toml_deps(text: str) -> list[str]:
    """Dependency names from a Cargo.toml's ``[dependencies]``-style tables."""
    deps: list[str] = []
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
            continue
        if section is None or "dependencies" not in section:
            continue
        if "=" in line and not line.startswith("#"):
            deps.append(line.split("=", 1)[0].strip().strip('"'))
    return deps


def _imported_roots_and_modules(source: str) -> tuple[set[str], set[str]]:
    """EVERY import in *source* (module scope AND function-local).

    r2 M2 hardening — two evasion forms now detected: (a) the
    ``ImportFrom`` arm ALSO records ``<module>.<alias>`` per alias, so
    ``from brutex.config import credentials_aws`` matches the banned
    MODULE set (the bare ``node.module`` is only ``brutex.config``);
    (b) string constants passed to ``importlib.import_module(...)`` /
    ``import_module(...)`` / ``__import__(...)`` calls are recorded as
    module names, so a dynamic import cannot slip past the scan.

    r2 NIT hardening — (c) RELATIVE imports (``node.level > 0``: ``from
    . import credentials_aws`` / ``from .credentials_aws import x``,
    where ``node.module`` is ``None`` or package-relative): the absolute
    package is unknowable from source alone, so any module component or
    alias whose LEAF matches a banned module's leaf is CONSERVATIVELY
    recorded as that banned module (:data:`_BANNED_PY_LEAVES`).

    HONEST RESIDUAL (documented, not claimed closed): string-built module
    names (``"boto" + "3"``, f-strings, variables) and ``getattr``-chain
    dynamic forms are STATICALLY UNDETECTABLE by any AST scan — this pin
    is defense-in-depth over the behavioral guarantee (each facade
    family's own AST import pins keep its import surface to stdlib +
    structlog + its own seam leaves — none of which touch the AWS reader
    chain), never the sole boundary.
    """
    roots: set[str] = set()
    modules: set[str] = set()

    def _record(name: str) -> None:
        modules.add(name)
        roots.add(name.split(".", maxsplit=1)[0])

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                _record(node.module)
                for alias in node.names:
                    _record(f"{node.module}.{alias.name}")
            if node.level > 0:
                leaves: set[str] = set()
                if node.module is not None:
                    leaves.update(node.module.split("."))
                leaves.update(alias.name for alias in node.names)
                for leaf in leaves:
                    banned_full = _BANNED_PY_LEAVES.get(leaf)
                    if banned_full is not None:
                        _record(banned_full)
        elif isinstance(node, ast.Call):
            func = node.func
            dynamic = (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
                isinstance(func, ast.Name) and func.id in {"import_module", "__import__"}
            )
            if dynamic and node.args:
                head = node.args[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    _record(head.value)
    return roots, modules


def _crate_api_names(source: str) -> list[str]:
    """The string members of a facade's ``_CRATE_API`` tuple (if any)."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "_CRATE_API"
                and isinstance(value, ast.Tuple)
            ):
                for element in value.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        names.append(element.value)
    return names


class TestCrateDependencyGraphIsAwsFree:
    def test_cargo_toml_has_no_aws_crates(self) -> None:
        deps = _cargo_toml_deps((_CRATE / "Cargo.toml").read_text(encoding="utf-8"))
        assert deps, "anti-vacuous: the manifest parser found no dependencies at all"
        assert "pyo3" in deps, "anti-vacuous: the known pyo3 dep must be visible"
        offenders = [d for d in deps if _AWS_CRATE_RE.match(d)]
        assert offenders == [], f"AWS-related crates in Cargo.toml: {offenders}"

    def test_cargo_lock_full_graph_has_no_aws_crates(self) -> None:
        packages = _cargo_lock_packages((_CRATE / "Cargo.lock").read_text(encoding="utf-8"))
        assert len(packages) >= 2, "anti-vacuous: the lock parser found no packages"
        assert "brutex-core" in packages, "anti-vacuous: the crate itself must be listed"
        offenders = [p for p in packages if _AWS_CRATE_RE.match(p)]
        assert offenders == [], f"AWS-related crates in the locked graph: {offenders}"

    def test_aws_crate_detector_has_teeth(self) -> None:
        # Anti-vacuousness (the #1492-G3 idiom): the detector must flag a
        # synthetic offending manifest/lock — else the green above proves
        # nothing.
        toml = '[dependencies]\npyo3 = "0.29"\naws-sdk-ssm = "1.0"\n'
        assert [d for d in _cargo_toml_deps(toml) if _AWS_CRATE_RE.match(d)] == ["aws-sdk-ssm"]
        lock = 'name = "rusoto_core"\nname = "serde"\n'
        assert [p for p in _cargo_lock_packages(lock) if _AWS_CRATE_RE.match(p)] == ["rusoto_core"]


class TestFacadesNeverImportAwsSurfaces:
    def test_facade_census_is_anti_vacuous(self) -> None:
        names = {p.name for p in _facade_files()}
        expected = {
            "money_rust.py",
            "feed_rust.py",
            "hardware_envelope_rust.py",
            "paths_rust.py",
            "dotenv_rust.py",
            "backup_env_rust.py",
        }
        assert expected <= names, f"facade census lost members: {expected - names}"

    @pytest.mark.parametrize("facade", _facade_files(), ids=lambda p: p.name)
    def test_no_facade_imports_credentials_aws_or_boto(self, facade: Path) -> None:
        roots, modules = _imported_roots_and_modules(facade.read_text(encoding="utf-8"))
        assert not (roots & _BANNED_PY_IMPORTS), (
            f"{facade.name} imports a banned AWS root: {sorted(roots & _BANNED_PY_IMPORTS)}"
        )
        banned = modules & _BANNED_PY_MODULES
        assert not banned, f"{facade.name} imports a banned module: {sorted(banned)}"

    def test_import_detector_has_teeth(self) -> None:
        source = (
            "def sneaky():\n"
            "    import boto3\n"
            "    from brutex.config.credentials_aws import load_groww_aws_credentials\n"
        )
        roots, modules = _imported_roots_and_modules(source)
        assert "boto3" in roots
        assert "brutex.config.credentials_aws" in modules

    def test_import_detector_sees_from_pkg_alias_form(self) -> None:
        # r2 M2 teeth (a): the from-package alias form must be DETECTED
        # — pre-hardening `from brutex.config import credentials_aws`
        # recorded only "brutex.config" and evaded the banned set.
        roots, modules = _imported_roots_and_modules("from brutex.config import credentials_aws\n")
        assert "brutex.config.credentials_aws" in modules
        assert modules & _BANNED_PY_MODULES == {"brutex.config.credentials_aws"}
        assert "brutex" in roots

    @pytest.mark.parametrize(
        "source",
        [
            "from . import credentials_aws\n",
            "from .credentials_aws import load_groww_aws_credentials\n",
            "from .. import credentials_aws\n",
            "from ..config.credentials_aws import load_groww_aws_credentials\n",
            "def sneaky():\n    from . import credentials_aws\n",
        ],
        ids=[
            "from-dot-import-leaf",
            "from-dot-leaf-import-name",
            "from-dotdot-import-leaf",
            "from-dotdot-pkg-leaf",
            "function-local-relative",
        ],
    )
    def test_import_detector_sees_relative_forms(self, source: str) -> None:
        # r2 NIT teeth (c): relative forms must be CONSERVATIVELY flagged
        # — pre-hardening `from . import credentials_aws` recorded
        # NOTHING (node.module is None) and evaded the banned set.
        _roots, modules = _imported_roots_and_modules(source)
        assert "brutex.config.credentials_aws" in modules
        assert modules & _BANNED_PY_MODULES, "relative form must map into the banned set"

    def test_relative_detector_ignores_unbanned_leaves(self) -> None:
        # The conservative belt must not false-positive on ordinary
        # relative imports whose leaves are not banned.
        _roots, modules = _imported_roots_and_modules("from . import seam_core\n")
        assert not (modules & _BANNED_PY_MODULES)

    def test_import_detector_sees_dynamic_import_strings(self) -> None:
        # r2 M2 teeth (b): importlib.import_module / bare import_module /
        # __import__ string constants must all be DETECTED.
        source = (
            "import importlib\n"
            "from importlib import import_module\n"
            'x = importlib.import_module("brutex.config.credentials_aws")\n'
            'y = __import__("boto3")\n'
            'z = import_module("botocore")\n'
        )
        roots, modules = _imported_roots_and_modules(source)
        assert "brutex.config.credentials_aws" in modules
        assert modules & _BANNED_PY_MODULES == {"brutex.config.credentials_aws"}
        assert roots & _BANNED_PY_IMPORTS == {"boto3", "botocore"}


class TestCredentialsBoundary:
    def test_no_facade_crate_api_names_an_aws_or_secret_surface(self) -> None:
        found_any = False
        for facade in _facade_files():
            names = _crate_api_names(facade.read_text(encoding="utf-8"))
            if names:
                found_any = True
            offenders = [n for n in names if _BANNED_API_TOKEN_RE.search(n)]
            assert offenders == [], (
                f"{facade.name} routes an AWS/credential-shaped callable: {offenders}"
            )
        assert found_any, "anti-vacuous: no _CRATE_API tuple was found in any facade"

    def test_crate_api_extractor_and_token_detector_have_teeth(self) -> None:
        source = '_CRATE_API = ("credparse_normalise_token", "paths_lake_root_str")\n'
        names = _crate_api_names(source)
        assert names == ["credparse_normalise_token", "paths_lake_root_str"]
        assert [n for n in names if _BANNED_API_TOKEN_RE.search(n)] == ["credparse_normalise_token"]

    def test_credentials_module_is_seam_free(self) -> None:
        # The milestone-3 NO-SEAM verdict for credentials.py, pinned
        # mechanically: it imports no facade and no crate, so the
        # AWS-delegator arm can never route through Rust.
        source = (_BRUTEX / "config" / "credentials.py").read_text(encoding="utf-8")
        roots, modules = _imported_roots_and_modules(source)
        assert "brutex_core" not in roots, "credentials.py must never import the crate"
        seam_imports = [m for m in modules if m.endswith("_rust") or "_rust." in m]
        assert seam_imports == [], (
            f"credentials.py imports a Rust facade: {seam_imports} — the milestone-3 "
            "verdict is NO SEAM (token_shape_problem is production-dead; "
            "normalise_token is a trivial secret-bearing strip)"
        )
        # Anti-vacuous: the module genuinely parses + imports things.
        assert modules, "anti-vacuous: credentials.py import scan found nothing"

    def test_crate_python_shim_exports_no_aws_or_secret_surface(self) -> None:
        shim = (_CRATE / "python" / "brutex_core" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(shim)
        exported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        value = node.value
                        if isinstance(value, ast.List):
                            for element in value.elts:
                                if isinstance(element, ast.Constant) and isinstance(
                                    element.value, str
                                ):
                                    exported.append(element.value)
        assert exported, "anti-vacuous: the shim's __all__ was not found"
        offenders = [n for n in exported if n != "__version__" and _BANNED_API_TOKEN_RE.search(n)]
        assert offenders == [], f"brutex_core exports an AWS/secret-shaped symbol: {offenders}"
