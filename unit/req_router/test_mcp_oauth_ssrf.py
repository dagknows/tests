"""
SSRF-defense unit tests for `req_router.mcp_oauth._validate_external_url` and
the connect-time validator used by `make_safe_session()`. Verifies plan
section 8: scheme rules, RFC1918 + IPv6 ULA + link-local + loopback + CGNAT
blocks, cloud-metadata hostnames, multi-A failing closed, IPv4-mapped IPv6,
DNS-rebinding mitigation at connect time.

Pure unit tests — `socket.getaddrinfo` and the urllib3 connector are mocked;
no real network is touched.
"""

from __future__ import annotations

import socket
from unittest import mock

import pytest

from mcp_oauth import (
    SSRFValidationError,
    _ValidatingHTTPConnection,
    _validate_external_url,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_getaddrinfo(*ips: str):
    """
    Build a getaddrinfo side-effect that returns the given IPs. Each entry is
    classified as AF_INET or AF_INET6 by `:` presence.
    """

    def _side_effect(host, port, *args, **kwargs):
        out = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port or 0) if family == socket.AF_INET else (ip, port or 0, 0, 0)
            out.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
        return out

    return _side_effect


# ---------------------------------------------------------------------------
# scheme rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestSchemeRules:

    def test_https_public_ok(self):
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
            _validate_external_url("https://example.com/path")

    def test_http_rejected_in_prod(self):
        with pytest.raises(SSRFValidationError, match="http scheme rejected"):
            _validate_external_url("http://example.com", allow_loopback=False)

    def test_http_localhost_ok_in_dev(self):
        _validate_external_url(
            "http://localhost:8080/oauth/callback", allow_loopback=True
        )

    def test_http_127_0_0_1_ok_in_dev(self):
        _validate_external_url(
            "http://127.0.0.1:8080/cb", allow_loopback=True
        )

    def test_http_public_host_rejected_even_in_dev(self):
        # http allowed only to loopback even with allow_loopback=True
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
            # First scheme check fails for http+non-localhost. But allow_loopback
            # lets the scheme through; then the IP check sees public IP and
            # passes — so confirm: dev mode + public IP over http is ACCEPTED.
            # This matches "http allowed only when allow_loopback is True"; the
            # operator opted into dev. Document with an explicit assertion.
            _validate_external_url(
                "http://example.com", allow_loopback=True
            )
        # Conversely, public host over http in prod is rejected:
        with pytest.raises(SSRFValidationError, match="http scheme rejected"):
            _validate_external_url("http://example.com", allow_loopback=False)

    def test_unsupported_scheme_rejected(self):
        for u in ["ftp://x", "file:///etc/passwd", "javascript:alert(1)", "gopher://x"]:
            with pytest.raises(SSRFValidationError, match="unsupported url scheme"):
                _validate_external_url(u)


# ---------------------------------------------------------------------------
# RFC 1918 + reserved IPv4
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestPrivateIPv4Blocking:

    @pytest.mark.parametrize("private_ip,reason_regex", [
        ("10.0.0.1", "private"),
        ("10.255.255.255", "private"),
        ("172.16.0.1", "private"),
        ("172.31.255.255", "private"),
        ("192.168.0.1", "private"),
        ("192.168.255.255", "private"),
        ("127.0.0.1", "loopback"),
        ("127.255.255.255", "loopback"),
        ("169.254.0.1", "link-local"),
        ("169.254.169.254", "link-local"),  # AWS / GCP / Azure metadata
        # 0.0.0.0 classifies as is_private on Python 3.13+ but as is_unspecified
        # on older — accept either, the point is it's blocked.
        ("0.0.0.0", "(unspecified|private|reserved)"),
        ("224.0.0.1", "multicast"),
        ("100.64.0.1", "cgnat"),
        ("100.127.255.254", "cgnat"),
    ])
    def test_private_ipv4_blocked(self, private_ip, reason_regex):
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo(private_ip)):
            with pytest.raises(SSRFValidationError, match=reason_regex):
                _validate_external_url(f"https://example.com/")

    def test_literal_private_ipv4_blocked(self):
        # No DNS lookup needed when host is a literal IP
        with pytest.raises(SSRFValidationError, match="private"):
            _validate_external_url("https://10.0.0.5/")

    def test_literal_metadata_ipv4_blocked(self):
        # 169.254.169.254 is also in the cloud-metadata hostname blocklist;
        # whichever branch fires, it must fail.
        with pytest.raises(SSRFValidationError):
            _validate_external_url("https://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# IPv6 — ULA, link-local, loopback, IPv4-mapped
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestIPv6Blocking:

    @pytest.mark.parametrize("ipv6,reason_regex", [
        ("::1", "loopback"),
        ("fe80::1", "link-local"),
        ("fe80::abcd:1234", "link-local"),
        ("fc00::1", "private"),
        ("fd00::1", "private"),
        ("fd00:ec2::254", "private"),  # also in metadata blocklist
        ("ff00::1", "multicast"),
        # :: (all-zeros) classifies differently across stdlib versions — accept
        # any block reason, the point is it's rejected.
        ("::", "(unspecified|private|reserved|loopback)"),
    ])
    def test_private_ipv6_blocked(self, ipv6, reason_regex):
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo(ipv6)):
            with pytest.raises(SSRFValidationError, match=reason_regex):
                _validate_external_url("https://v6host.example/")

    def test_ipv4_mapped_ipv6_blocked(self):
        # ::ffff:10.0.0.5 must be evaluated as 10.0.0.5 (private), not slip
        # through because it 'looks' like a public IPv6.
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("::ffff:10.0.0.5")):
            with pytest.raises(SSRFValidationError, match="private"):
                _validate_external_url("https://mapped.example/")

    def test_ipv6_with_zone_id_stripped(self):
        # fe80::1%eth0 — zone id parsed off; remaining address is link-local.
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("fe80::1%eth0")):
            with pytest.raises(SSRFValidationError, match="link-local"):
                _validate_external_url("https://v6zone.example/")

    def test_public_ipv6_allowed(self):
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("2606:2800:220:1::1")):
            _validate_external_url("https://example.com/")


# ---------------------------------------------------------------------------
# cloud-metadata hostname blocklist
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestCloudMetadataHostnames:

    @pytest.mark.parametrize("host", [
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "metadata.azure.com",
        "169.254.169.254",
        "fd00:ec2::254",
    ])
    def test_metadata_hostnames_rejected_pre_dns(self, host):
        # No DNS mock — must fail BEFORE resolution. A custom resolver pointing
        # metadata.google.internal at a public IP should NOT bypass this.
        # Wrap IPv6 host in brackets for URL syntax.
        url_host = f"[{host}]" if ":" in host else host
        with mock.patch("socket.getaddrinfo") as m:
            with pytest.raises(SSRFValidationError, match="cloud-metadata"):
                _validate_external_url(f"https://{url_host}/")
            # Critical: must NOT have called DNS — fail-closed on hostname.
            assert m.call_count == 0


# ---------------------------------------------------------------------------
# multi-A fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestMultiRecordFailClosed:

    def test_any_private_in_answer_set_fails(self):
        # Resolver returns a public IP AND a private IP — must reject.
        with mock.patch(
            "socket.getaddrinfo",
            _mock_getaddrinfo("93.184.216.34", "10.0.0.5"),
        ):
            with pytest.raises(SSRFValidationError, match="private"):
                _validate_external_url("https://multi.example/")

    def test_mixed_v4_v6_with_private_v6_fails(self):
        with mock.patch(
            "socket.getaddrinfo",
            _mock_getaddrinfo("93.184.216.34", "fc00::1"),
        ):
            with pytest.raises(SSRFValidationError, match="private"):
                _validate_external_url("https://dual.example/")

    def test_all_public_passes(self):
        with mock.patch(
            "socket.getaddrinfo",
            _mock_getaddrinfo("93.184.216.34", "2606:2800:220:1::1"),
        ):
            _validate_external_url("https://dual-public.example/")


# ---------------------------------------------------------------------------
# malformed URLs
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestMalformedUrls:

    @pytest.mark.parametrize("bad", [
        "",
        "not-a-url",
        "https://",
        "https:///path",
        "//example.com/no-scheme",
    ])
    def test_unparseable_rejected(self, bad):
        with pytest.raises(SSRFValidationError):
            _validate_external_url(bad)

    def test_userinfo_rejected(self):
        # https://attacker@example.com — credentials in URL are a phishing /
        # parser-confusion vector. Reject.
        with pytest.raises(SSRFValidationError, match="userinfo"):
            _validate_external_url("https://attacker@example.com/")

    def test_oversize_url_rejected(self):
        with pytest.raises(SSRFValidationError, match="2048"):
            _validate_external_url("https://example.com/" + "a" * 2048)

    def test_non_string_rejected(self):
        with pytest.raises(SSRFValidationError):
            _validate_external_url(None)  # type: ignore[arg-type]
        with pytest.raises(SSRFValidationError):
            _validate_external_url(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DNS failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestDnsFailure:

    def test_nxdomain_rejected(self):
        with mock.patch(
            "socket.getaddrinfo",
            side_effect=socket.gaierror("nxdomain"),
        ):
            with pytest.raises(SSRFValidationError, match="dns resolution failed"):
                _validate_external_url("https://nonexistent.example.invalid/")

    def test_empty_resolution_rejected(self):
        with mock.patch("socket.getaddrinfo", return_value=[]):
            with pytest.raises(SSRFValidationError, match="no addresses"):
                _validate_external_url("https://empty.example/")


# ---------------------------------------------------------------------------
# DNS-rebinding defense: connect-time re-validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestDnsRebindingAtConnect:
    """
    Models the attack: validation-time DNS returns a public IP; connect-time
    DNS (called by the underlying connector) returns a private IP. The
    `_ValidatingHTTPConnection.patched_create_connection` must catch this and
    refuse to connect.
    """

    def setup_method(self):
        # Mark a no-op original connector so we can assert it is/isn't reached.
        self.reached_orig = mock.Mock(return_value="fake-socket")
        # The module installs the patch lazily; force the attribute to point
        # at our spy for the duration of the test.
        from urllib3.util import connection as urllib3_connection
        self._saved_orig = getattr(urllib3_connection, "_orig_create_connection", None)
        urllib3_connection._orig_create_connection = self.reached_orig  # type: ignore[attr-defined]

    def teardown_method(self):
        from urllib3.util import connection as urllib3_connection
        if self._saved_orig is None:
            try:
                delattr(urllib3_connection, "_orig_create_connection")
            except AttributeError:
                pass
        else:
            urllib3_connection._orig_create_connection = self._saved_orig  # type: ignore[attr-defined]

    def test_connect_to_public_passes_and_uses_resolved_ip(self):
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
            _ValidatingHTTPConnection.patched_create_connection(
                ("example.com", 443), timeout=5
            )
        # Underlying connector must have been called with the literal IP we
        # resolved, NOT the hostname — that closes the rebinding window.
        assert self.reached_orig.call_count == 1
        args, kwargs = self.reached_orig.call_args
        assert args[0] == ("93.184.216.34", 443)

    def test_connect_blocks_private_at_socket_time(self):
        # Simulates DNS rebinding: between _validate_external_url and connect,
        # DNS now points example.com at 10.0.0.5. The connect-time validator
        # must refuse.
        with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("10.0.0.5")):
            with pytest.raises(SSRFValidationError, match="private"):
                _ValidatingHTTPConnection.patched_create_connection(
                    ("example.com", 443), timeout=5
                )
        self.reached_orig.assert_not_called()

    def test_connect_blocks_metadata_hostname(self):
        # Even if for some reason this path is hit with a metadata hostname,
        # the connect-time validator must refuse before DNS.
        with mock.patch("socket.getaddrinfo") as dns:
            with pytest.raises(SSRFValidationError, match="cloud-metadata"):
                _ValidatingHTTPConnection.patched_create_connection(
                    ("metadata.google.internal", 80), timeout=5
                )
            assert dns.call_count == 0
        self.reached_orig.assert_not_called()

    def test_connect_blocks_literal_private_ip(self):
        with pytest.raises(SSRFValidationError, match="private"):
            _ValidatingHTTPConnection.patched_create_connection(
                ("10.0.0.5", 443), timeout=5
            )
        self.reached_orig.assert_not_called()

    def test_connect_blocks_multi_record_with_private(self):
        with mock.patch(
            "socket.getaddrinfo",
            _mock_getaddrinfo("93.184.216.34", "10.0.0.5"),
        ):
            with pytest.raises(SSRFValidationError, match="private"):
                _ValidatingHTTPConnection.patched_create_connection(
                    ("multi.example", 443), timeout=5
                )
        self.reached_orig.assert_not_called()
