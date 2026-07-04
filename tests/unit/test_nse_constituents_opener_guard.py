"""SSRF allowlist guard on the live constituent-CSV opener (defense-in-depth).

R2-DS-F3: ``brutex.dashboard.nse_constituents._default_opener`` previously
re-validated nothing — it fetched whatever URL resolution produced, and
``constituent_url`` accepts ``http://`` plus arbitrary env-override hosts
(``BRUTEX_NSE_CONSTITUENTS_BASE_URL`` / ``BRUTEX_INDEX_CSV_FILENAMES``). The
fix mirrors the instrument-master ``_check_url`` model
(``brutex/adapters/groww/instrument_loader.py``): an INDEPENDENT guard inside
the opener enforces ``https`` + a FIXED NSE/niftyindices host allowlist, so an
env-override host must still pass the guard or fail LOUDLY (``ValueError``),
while every legitimate built-in default URL passes unchanged.
"""

from __future__ import annotations

import pytest
import structlog.testing
from brutex.dashboard import nse_constituents as nc

# pyright: reportPrivateUsage=none

_GOOD_NIFTYINDICES_URL = (
    "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv"
)
_GOOD_NSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"


class TestCheckConstituentUrl:
    """The pure guard: scheme MUST be https AND host MUST be allowlisted."""

    def test_rejects_http_scheme(self) -> None:
        with pytest.raises(ValueError, match="non-allowlisted constituent-CSV URL"):
            nc._check_constituent_url("http://nsearchives.nseindia.com/content/indices/x.csv")

    def test_rejects_non_allowlisted_https_host(self) -> None:
        with pytest.raises(ValueError, match="allowlist"):
            nc._check_constituent_url("https://evil.example.com/ind_nifty50list.csv")

    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="requires https"):
            nc._check_constituent_url("file:///etc/passwd")

    def test_accepts_default_nse_archive_host(self) -> None:
        nc._check_constituent_url(_GOOD_NSE_URL)  # must not raise

    def test_accepts_niftyindices_hosts(self) -> None:
        nc._check_constituent_url(_GOOD_NIFTYINDICES_URL)  # must not raise
        nc._check_constituent_url("https://niftyindices.com/IndexConstituent/x.csv")

    def test_rejection_is_logged_via_structlog(self) -> None:
        with (
            structlog.testing.capture_logs() as logs,
            pytest.raises(ValueError, match="allowlist"),
        ):
            nc._check_constituent_url("https://attacker.invalid/x.csv")
        events = [entry for entry in logs if entry["event"] == "nse_constituents.url_rejected"]
        assert len(events) == 1
        assert events[0]["host"] == "attacker.invalid"
        assert events[0]["scheme"] == "https"

    def test_every_builtin_default_url_passes(self) -> None:
        """Public behavior unchanged: every built-in mapping resolves allowlisted."""
        for symbol in nc.INDEX_CSV_FILENAMES:
            url = nc.constituent_url(symbol)
            assert url is not None
            nc._check_constituent_url(url)  # must not raise

    def test_convention_url_passes(self) -> None:
        url = nc.convention_constituent_url("NIFTYPHARMA")
        assert url is not None
        nc._check_constituent_url(url)  # must not raise


class TestDefaultOpenerGuard:
    """The guard is wired INSIDE ``_default_opener`` — before any network I/O."""

    def test_opener_rejects_http_before_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _must_not_fetch(url: str) -> str:
            raise AssertionError(f"network fetch must never run for {url!r}")

        monkeypatch.setattr(nc, "_open_live_url", _must_not_fetch)
        with pytest.raises(ValueError, match="requires https"):
            nc._default_opener("http://www.niftyindices.com/IndexConstituent/x.csv")

    def test_opener_rejects_non_allowlisted_host_before_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _must_not_fetch(url: str) -> str:
            raise AssertionError(f"network fetch must never run for {url!r}")

        monkeypatch.setattr(nc, "_open_live_url", _must_not_fetch)
        with pytest.raises(ValueError, match="allowlist"):
            nc._default_opener("https://mirror.internal/ind_nifty50list.csv")

    def test_opener_accepts_legitimate_niftyindices_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(nc, "_open_live_url", lambda url: "Symbol\nRELIANCE\n")
        assert nc._default_opener(_GOOD_NIFTYINDICES_URL) == "Symbol\nRELIANCE\n"

    def test_opener_accepts_legitimate_nse_archive_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(nc, "_open_live_url", lambda url: "Symbol\nSBIN\n")
        assert nc._default_opener(_GOOD_NSE_URL) == "Symbol\nSBIN\n"


class TestEnvOverrideStillGuarded:
    """An env-override host must STILL pass the fixed allowlist — or fail loudly."""

    def test_allowlisted_override_url_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            nc.INDEX_CSV_FILENAMES_OVERRIDE_ENV,
            '{"MYIDX": "https://niftyindices.com/IndexConstituent/ind_myidxlist.csv"}',
        )
        url = nc.constituent_url("MYIDX")
        assert url == "https://niftyindices.com/IndexConstituent/ind_myidxlist.csv"
        monkeypatch.setattr(nc, "_open_live_url", lambda u: "Symbol\nTCS\n")
        assert nc._default_opener(url) == "Symbol\nTCS\n"

    def test_non_allowlisted_override_host_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            nc.INDEX_CSV_FILENAMES_OVERRIDE_ENV,
            '{"EVILIDX": "https://evil.example.com/ind_evillist.csv"}',
        )
        url = nc.constituent_url("EVILIDX")
        assert url == "https://evil.example.com/ind_evillist.csv"
        with pytest.raises(ValueError, match="fixed allowlist"):
            nc._default_opener(url)

    def test_http_override_base_style_url_fails_loudly(self) -> None:
        # A repointed BRUTEX_NSE_CONSTITUENTS_BASE_URL (read at import time)
        # would produce URLs of this shape — the opener refuses them.
        with pytest.raises(ValueError, match="requires https"):
            nc._default_opener("http://mirror.local/content/indices/ind_nifty50list.csv")


def test_allowlist_is_exported_and_fixed() -> None:
    assert "NSE_CONSTITUENT_ALLOWED_HOSTS" in nc.__all__
    assert (
        frozenset({"nsearchives.nseindia.com", "niftyindices.com", "www.niftyindices.com"})
        == nc.NSE_CONSTITUENT_ALLOWED_HOSTS
    )
    assert isinstance(nc.NSE_CONSTITUENT_ALLOWED_HOSTS, frozenset)
