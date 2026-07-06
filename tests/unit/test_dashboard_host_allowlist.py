"""Tests for :mod:`brutex.dashboard.host_allowlist` (DEC-DASHBOARD-SECURITY-HYGIENE-001).

The module is the LEAF single source of truth for the dashboard's
loopback-only ``Host``-header allowlist. Three contracts are pinned:

1. CONTENT — exactly the six loopback spellings (IPv4 + name + the bracketed
   IPv6 ``[::1]`` added by DEC-ENGINE-MISC-PACK-002), in deterministic order,
   and ``testserver`` (the FastAPI ``TestClient`` default Host) is NEVER
   shipped (the hostile-review MAJOR: a single-label name an intranet DNS
   can rebind would bypass the guard).
2. RE-EXPORT — :mod:`brutex.dashboard.app` re-exports the SAME object via
   ``__all__``, so every existing ``from brutex.dashboard.app import
   DEFAULT_ALLOWED_HOSTS`` site keeps working.
3. LEAF-NESS — importing the module does NOT import
   :mod:`brutex.dashboard.app` (the CodeQL py/cyclic-import rationale for
   the module's existence: ``brutex.cli`` reads the default WITHOUT adding
   a ``cli -> dashboard.app`` static cycle edge).
"""

from __future__ import annotations

import subprocess
import sys

from brutex.dashboard.host_allowlist import DEFAULT_ALLOWED_HOSTS


class TestDefaultAllowedHostsContent:
    def test_exact_loopback_only_entries_in_order(self) -> None:
        """The default is EXACTLY the six loopback spellings (order pinned).

        ``[::1]`` is the IPv6 loopback in its bracketed RFC 3986 ``Host`` form
        (DEC-ENGINE-MISC-PACK-002) — an ``http://[::1]:port`` client is a
        LOOPBACK client and must not be 400ed by the default guard.
        """
        assert DEFAULT_ALLOWED_HOSTS == (
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        )

    def test_is_an_immutable_tuple_of_strings(self) -> None:
        """The constant is a tuple (immutable) of plain strings."""
        assert isinstance(DEFAULT_ALLOWED_HOSTS, tuple)
        assert all(isinstance(entry, str) for entry in DEFAULT_ALLOWED_HOSTS)

    def test_testserver_never_shipped(self) -> None:
        """``testserver`` must NEVER appear — the DNS-rebinding bypass name.

        Regression pin for the hostile-review MAJOR: tests ride the conftest
        ``base_url="http://localhost"`` re-point instead.
        """
        assert not any("testserver" in entry for entry in DEFAULT_ALLOWED_HOSTS)

    def test_no_wildcard_and_no_non_loopback_host(self) -> None:
        """Loopback-ONLY by design: no ``*`` entry, no routable name."""
        bare_hosts = {entry.removesuffix(":*") for entry in DEFAULT_ALLOWED_HOSTS}
        assert bare_hosts == {"127.0.0.1", "localhost", "[::1]"}
        assert "*" not in DEFAULT_ALLOWED_HOSTS


class TestReExportThroughApp:
    def test_app_re_exports_the_same_object(self) -> None:
        """``brutex.dashboard.app.DEFAULT_ALLOWED_HOSTS`` IS the leaf constant."""
        from brutex.dashboard import app as app_mod

        assert app_mod.DEFAULT_ALLOWED_HOSTS is DEFAULT_ALLOWED_HOSTS

    def test_app_declares_the_re_export_in_all(self) -> None:
        """The re-export is EXPLICIT (``__all__``), not an implicit leak."""
        from brutex.dashboard import app as app_mod

        assert "DEFAULT_ALLOWED_HOSTS" in app_mod.__all__


class TestLeafness:
    def test_import_does_not_pull_in_dashboard_app(self) -> None:
        """Importing the leaf must NOT import ``brutex.dashboard.app``.

        This is the module's reason to exist (CodeQL py/cyclic-import):
        ``brutex.cli`` reads the default host allowlist from here so the
        CLI helper adds no new ``cli -> dashboard.app`` cycle edge. Proven
        in a FRESH interpreter so this test is independent of import order
        in the surrounding suite.
        """
        probe = (
            "import sys\n"
            "import brutex.dashboard.host_allowlist\n"
            "assert 'brutex.dashboard.app' not in sys.modules, 'leaf pulled in app'\n"
            "print('leaf-ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "leaf-ok" in result.stdout
