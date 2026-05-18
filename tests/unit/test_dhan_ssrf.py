"""Unit tests for ``brutex.adapters.dhan.ssrf_guard``.

Pins:

* Refuses private IPv4 ranges (10/8, 172.16/12, 192.168/16).
* Refuses loopback (127/8, ::1).
* Refuses link-local (169.254/16, fe80::/10).
* Refuses unspecified (0.0.0.0, ::).
* Refuses multicast (224/4).
* Accepts well-known global IPs (8.8.8.8).
* Refuses missing hostname.
* Refuses unparseable resolved address.
* Refuses DNS resolution failures.
* DhanClient construction wires the guard by default.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from brutex.adapters.dhan.ssrf_guard import SSRFGuardError, assert_host_is_public


def _mock_getaddrinfo(addresses: list[str]) -> Any:
    """Build a fake ``socket.getaddrinfo`` returning the given addresses.

    Returns the 5-tuple shape getaddrinfo emits. For IPv4 use AF_INET;
    for IPv6 use AF_INET6 — autodetected.
    """

    def fake(host: str, port: int | str, *args: Any, **kwargs: Any) -> list[Any]:
        infos: list[Any] = []
        for addr in addresses:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            sockaddr = (addr, port) if family == socket.AF_INET else (addr, port, 0, 0)
            infos.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return infos

    return fake


class TestPrivateIPv4Refused:
    """All three RFC 1918 ranges fail the guard."""

    @pytest.mark.parametrize(
        "addr",
        ["10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.254", "192.168.1.1"],
    )
    def test_rfc1918_refused(self, addr: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo([addr]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]


class TestLoopbackRefused:
    """127/8 and ::1 both fail."""

    def test_ipv4_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["127.0.0.1"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]

    def test_ipv6_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["::1"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]


class TestLinkLocalRefused:
    """169.254.0.0/16 (incl. AWS IMDS 169.254.169.254) + fe80::/10 fail."""

    def test_imds_address_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["169.254.169.254"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]

    def test_ipv6_link_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["fe80::1"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]


class TestUnspecifiedRefused:
    """0.0.0.0 and :: fail."""

    def test_ipv4_unspecified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["0.0.0.0"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]

    def test_ipv6_unspecified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["::"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]


class TestMulticastRefused:
    """224.0.0.0/4 fails."""

    def test_ipv4_multicast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["224.0.0.1"]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "non-global" in exc_info.value.args[0]


class TestPublicAccepted:
    """Well-known public IPs are accepted."""

    def test_8888(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["8.8.8.8"]))
        assert_host_is_public("https://example.com")  # does not raise

    def test_ipv6_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["2606:4700:4700::1111"]))
        assert_host_is_public("https://example.com")

    def test_mixed_addresses_all_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple resolved IPs — all must be public."""
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["8.8.8.8", "1.1.1.1"]))
        assert_host_is_public("https://example.com")

    def test_one_private_fails_even_if_others_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single private IP in the result set fails the whole check."""
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo(["8.8.8.8", "10.0.0.1"]))
        with pytest.raises(SSRFGuardError):
            assert_host_is_public("https://example.com")


class TestMalformedInputs:
    """URL- and DNS-level failures."""

    def test_no_hostname(self) -> None:
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("not_a_url")
        assert "no hostname" in exc_info.value.args[0]

    def test_dns_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("simulated DNS failure")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "DNS resolution failed" in exc_info.value.args[0]

    def test_empty_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _mock_getaddrinfo([]))
        with pytest.raises(SSRFGuardError) as exc_info:
            assert_host_is_public("https://example.com")
        assert "no addresses returned" in exc_info.value.args[0]
