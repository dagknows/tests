"""
Phase 2 unit tests for `req_router.mcp_oauth`:

  - discover_oauth_endpoints: RFC 9728 → RFC 8414 chain, fallback to direct
    RFC 8414, anti-confused-deputy issuer match, SSRF validation on every
    endpoint returned by the issuer.
  - register_client_dcr: RFC 7591 body shape, token_endpoint_auth_method
    selection from server metadata, initial_access_token, error mapping.
  - pick_token_endpoint_auth_method: preference ordering.

No network — `socket.getaddrinfo` and the Session are mocked.
"""

from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest
import requests

from mcp_oauth import (
    AUTHORIZATION_SERVER_METADATA_SUFFIX,
    OAUTH_TIMEOUT,
    OAuthDiscoveryError,
    OAuthHTTPError,
    OAuthIssuerMismatchError,
    PROTECTED_RESOURCE_METADATA_SUFFIX,
    discover_oauth_endpoints,
    pick_token_endpoint_auth_method,
    register_client_dcr,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _public_dns():
    def _side_effect(host, port, *args, **kwargs):
        return [(
            socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0)
        )]
    return mock.patch("socket.getaddrinfo", _side_effect)


class _FakeResponse:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _RoutingSession:
    """
    Session that returns different responses per URL — useful when one call
    chain spans two endpoints (resource-metadata then issuer-metadata).
    """

    def __init__(self, url_map: Dict[str, _FakeResponse]):
        self.url_map = dict(url_map)
        self.calls: List[Dict[str, Any]] = []

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append({
            "method": "GET", "url": url, "headers": headers, "timeout": timeout,
        })
        if url in self.url_map:
            return self.url_map[url]
        # Default: 404 — surfaces as "no such well-known" in discovery flow.
        return _FakeResponse(404, {})

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({
            "method": "POST", "url": url, "json": json,
            "headers": headers, "timeout": timeout,
        })
        if url in self.url_map:
            return self.url_map[url]
        return _FakeResponse(404, {})


# ---------------------------------------------------------------------------
# pick_token_endpoint_auth_method
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestPickTokenEndpointAuthMethod:

    def test_defaults_to_basic_when_metadata_missing(self):
        assert pick_token_endpoint_auth_method(None) == "client_secret_basic"
        assert pick_token_endpoint_auth_method([]) == "client_secret_basic"

    def test_prefers_basic_over_post_over_none(self):
        assert pick_token_endpoint_auth_method(
            ["none", "client_secret_post", "client_secret_basic"]
        ) == "client_secret_basic"
        assert pick_token_endpoint_auth_method(
            ["none", "client_secret_post"]
        ) == "client_secret_post"
        assert pick_token_endpoint_auth_method(["none"]) == "none"

    def test_rejects_when_no_supported_method_advertised(self):
        with pytest.raises(Exception, match="no method DagKnows supports"):
            pick_token_endpoint_auth_method(
                ["private_key_jwt", "tls_client_auth"]
            )

    def test_ignores_non_string_entries(self):
        assert pick_token_endpoint_auth_method(
            [None, 42, "client_secret_basic"]
        ) == "client_secret_basic"


# ---------------------------------------------------------------------------
# discover_oauth_endpoints — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestDiscoverHappyPath:

    ISSUER = "https://issuer.example.com"
    RESOURCE = "https://mcp.linear.app"
    METADATA = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "registration_endpoint": f"{ISSUER}/oauth/register",
        "revocation_endpoint": f"{ISSUER}/oauth/revoke",
        "scopes_supported": ["read", "write"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic", "none"
        ],
    }

    def test_rfc9728_then_rfc8414_chain(self):
        session = _RoutingSession({
            f"{self.RESOURCE}{PROTECTED_RESOURCE_METADATA_SUFFIX}": _FakeResponse(
                200, {"authorization_servers": [self.ISSUER]}
            ),
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, self.METADATA,
            ),
        })
        with _public_dns():
            out = discover_oauth_endpoints(
                self.RESOURCE, self.ISSUER, session=session,
            )
        assert out["authorization_endpoint"] == f"{self.ISSUER}/oauth/authorize"
        assert out["token_endpoint"] == f"{self.ISSUER}/oauth/token"
        assert out["_source"] == "rfc9728+rfc8414"
        assert out["_resource_metadata"] == {
            "authorization_servers": [self.ISSUER]
        }
        # Order: 9728 first, 8414 second.
        urls = [c["url"] for c in session.calls]
        assert urls == [
            f"{self.RESOURCE}{PROTECTED_RESOURCE_METADATA_SUFFIX}",
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}",
        ]

    def test_falls_back_to_direct_rfc8414_when_9728_404s(self):
        session = _RoutingSession({
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, self.METADATA,
            ),
            # 9728 implicitly 404 (not in url_map).
        })
        with _public_dns():
            out = discover_oauth_endpoints(
                self.RESOURCE, self.ISSUER, session=session,
            )
        assert out["_source"] == "rfc8414-direct"
        assert out["_resource_metadata"] is None

    def test_issuer_trailing_slash_normalized(self):
        # Admin types "https://issuer.example.com/" — must still match
        # "https://issuer.example.com" returned by 9728.
        session = _RoutingSession({
            f"{self.RESOURCE}{PROTECTED_RESOURCE_METADATA_SUFFIX}": _FakeResponse(
                200, {"authorization_servers": [self.ISSUER]}
            ),
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, self.METADATA,
            ),
        })
        with _public_dns():
            out = discover_oauth_endpoints(
                self.RESOURCE, self.ISSUER + "/", session=session,
            )
        assert out["token_endpoint"] == f"{self.ISSUER}/oauth/token"


# ---------------------------------------------------------------------------
# discover_oauth_endpoints — anti-confused-deputy
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestDiscoverConfusedDeputy:

    RESOURCE = "https://mcp.linear.app"
    ADMIN_ISSUER = "https://issuer.example.com"
    BAD_ISSUER = "https://evil.example.com"

    def test_issuer_not_in_advertised_list_rejected(self):
        session = _RoutingSession({
            f"{self.RESOURCE}{PROTECTED_RESOURCE_METADATA_SUFFIX}": _FakeResponse(
                200, {"authorization_servers": [self.BAD_ISSUER]}
            ),
        })
        with _public_dns():
            with pytest.raises(OAuthIssuerMismatchError):
                discover_oauth_endpoints(
                    self.RESOURCE, self.ADMIN_ISSUER, session=session,
                )

    def test_rfc8414_issuer_field_mismatch_rejected(self):
        # RFC 9728 absent; admin says issuer.example.com but the metadata at
        # that URL claims its issuer is evil.example.com. Reject.
        session = _RoutingSession({
            f"{self.ADMIN_ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}":
                _FakeResponse(200, {
                    "issuer": self.BAD_ISSUER,
                    "authorization_endpoint": f"{self.BAD_ISSUER}/auth",
                    "token_endpoint": f"{self.BAD_ISSUER}/token",
                }),
        })
        with _public_dns():
            with pytest.raises(OAuthIssuerMismatchError):
                discover_oauth_endpoints(
                    self.RESOURCE, self.ADMIN_ISSUER, session=session,
                )

    def test_empty_authorization_servers_array_rejected(self):
        session = _RoutingSession({
            f"{self.RESOURCE}{PROTECTED_RESOURCE_METADATA_SUFFIX}": _FakeResponse(
                200, {"authorization_servers": []}
            ),
        })
        with _public_dns():
            with pytest.raises(OAuthDiscoveryError, match="no authorization_servers"):
                discover_oauth_endpoints(
                    self.RESOURCE, self.ADMIN_ISSUER, session=session,
                )


# ---------------------------------------------------------------------------
# discover_oauth_endpoints — SSRF on discovered URLs
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestDiscoverSsrf:

    RESOURCE = "https://mcp.linear.app"
    ISSUER = "https://issuer.example.com"

    def test_internal_endpoint_in_issuer_metadata_rejected(self):
        # Hostile issuer returns an internal authorization_endpoint URL.
        # Our endpoint validator (DNS-resolution check) must reject before
        # we ever build the authorize redirect.
        session = _RoutingSession({
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, {
                    "issuer": self.ISSUER,
                    "authorization_endpoint": "https://internal.local/auth",
                    "token_endpoint": f"{self.ISSUER}/token",
                }
            ),
        })

        def _resolve(host, port, *args, **kwargs):
            ip = "10.0.0.5" if "internal" in host else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port or 0))]

        with mock.patch("socket.getaddrinfo", _resolve):
            with pytest.raises(OAuthDiscoveryError, match="authorization_endpoint"):
                discover_oauth_endpoints(
                    self.RESOURCE, self.ISSUER, session=session,
                )

    def test_metadata_endpoint_to_cloud_metadata_host_rejected(self):
        # Token endpoint pointing at AWS metadata. The hostname blocklist
        # fires before DNS.
        session = _RoutingSession({
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, {
                    "issuer": self.ISSUER,
                    "authorization_endpoint": f"{self.ISSUER}/auth",
                    "token_endpoint": "http://169.254.169.254/latest/meta-data/",
                }
            ),
        })
        with _public_dns():
            with pytest.raises(OAuthDiscoveryError):
                discover_oauth_endpoints(
                    self.RESOURCE, self.ISSUER, session=session,
                )


# ---------------------------------------------------------------------------
# discover_oauth_endpoints — required-field validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestDiscoverRequiredFields:

    RESOURCE = "https://mcp.linear.app"
    ISSUER = "https://issuer.example.com"

    def test_missing_authorization_endpoint_rejected(self):
        session = _RoutingSession({
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, {"issuer": self.ISSUER, "token_endpoint": f"{self.ISSUER}/token"}
            ),
        })
        with _public_dns():
            with pytest.raises(OAuthDiscoveryError, match="authorization_endpoint"):
                discover_oauth_endpoints(self.RESOURCE, self.ISSUER, session=session)

    def test_missing_token_endpoint_rejected(self):
        session = _RoutingSession({
            f"{self.ISSUER}{AUTHORIZATION_SERVER_METADATA_SUFFIX}": _FakeResponse(
                200, {
                    "issuer": self.ISSUER,
                    "authorization_endpoint": f"{self.ISSUER}/auth",
                }
            ),
        })
        with _public_dns():
            with pytest.raises(OAuthDiscoveryError, match="token_endpoint"):
                discover_oauth_endpoints(self.RESOURCE, self.ISSUER, session=session)

    def test_8414_non_200_rejected(self):
        session = _RoutingSession({})  # everything 404s
        with _public_dns():
            with pytest.raises(OAuthDiscoveryError, match="metadata fetch failed"):
                discover_oauth_endpoints(self.RESOURCE, self.ISSUER, session=session)


# ---------------------------------------------------------------------------
# register_client_dcr
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestRegisterClientDcr:

    ENDPOINT = "https://issuer.example.com/oauth/register"
    REDIRECT = "https://app.dagknows.com/api/v1/mcp/oauth/callback"

    def _call(self, session, **overrides):
        kwargs = dict(
            redirect_uri=self.REDIRECT,
            scopes=["read", "write"],
            session=session,
            client_name="DagKnows (acme)",
        )
        kwargs.update(overrides)
        with _public_dns():
            return register_client_dcr(self.ENDPOINT, **kwargs)

    def test_201_response_returned(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {
                "client_id": "registered-cid",
                "client_secret": "registered-secret",
                "client_id_issued_at": 1700000000,
                "client_secret_expires_at": 0,
            }),
        })
        out = self._call(session)
        assert out["client_id"] == "registered-cid"
        assert out["client_secret"] == "registered-secret"

    def test_200_response_also_accepted(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(200, {"client_id": "cid"}),
        })
        out = self._call(session)
        assert out["client_id"] == "cid"

    def test_request_body_shape(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {"client_id": "cid"}),
        })
        self._call(session)
        body = session.calls[0]["json"]
        assert body["redirect_uris"] == [self.REDIRECT]
        assert body["grant_types"] == ["authorization_code", "refresh_token"]
        assert body["response_types"] == ["code"]
        assert body["token_endpoint_auth_method"] == "client_secret_basic"
        assert body["software_id"] == "dagknows-mcp-client"
        assert body["software_version"] == "v1"
        assert body["client_name"] == "DagKnows (acme)"
        assert body["scope"] == "read write"

    def test_auth_method_picked_from_server_metadata(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {"client_id": "cid"}),
        })
        self._call(session, server_metadata={
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        })
        # Server doesn't advertise basic — we should pick post (next pref).
        body = session.calls[0]["json"]
        assert body["token_endpoint_auth_method"] == "client_secret_post"

    def test_initial_access_token_sets_bearer_header(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {"client_id": "cid"}),
        })
        self._call(session, initial_access_token="IAT-secret")
        headers = session.calls[0]["headers"]
        assert headers["Authorization"] == "Bearer IAT-secret"

    def test_no_initial_access_token_means_no_auth_header(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {"client_id": "cid"}),
        })
        self._call(session)
        headers = session.calls[0]["headers"]
        assert "Authorization" not in headers

    def test_4xx_raises_with_error_code(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(400, {
                "error": "invalid_redirect_uri",
                "error_description": "redirect_uri must be https",
            }),
        })
        with pytest.raises(OAuthHTTPError) as ei:
            self._call(session)
        assert ei.value.error_code == "invalid_redirect_uri"
        assert ei.value.status_code == 400

    def test_response_without_client_id_raises(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {"client_secret": "orphan"}),
        })
        with pytest.raises(OAuthHTTPError, match="missing client_id"):
            self._call(session)

    def test_response_auth_method_overrides_request(self):
        # RFC 7591 §3.2.1 — server MAY echo a different auth_method.
        # Caller MUST honor the server's choice for subsequent token calls.
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {
                "client_id": "cid",
                "client_secret": "sec",
                "token_endpoint_auth_method": "client_secret_post",
            }),
        })
        out = self._call(session)
        assert out["token_endpoint_auth_method"] == "client_secret_post"

    def test_no_server_auth_method_field_falls_back_to_chosen(self):
        session = _RoutingSession({
            self.ENDPOINT: _FakeResponse(201, {"client_id": "cid"}),
        })
        out = self._call(session)
        # Default is basic since server_metadata wasn't passed.
        assert out["token_endpoint_auth_method"] == "client_secret_basic"

    def test_registration_endpoint_ssrf_rejected(self):
        session = _RoutingSession({})
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                           ("10.0.0.5", 0))]
        ):
            with pytest.raises(Exception):  # SSRFValidationError
                register_client_dcr(
                    "https://internal.local/oauth/register",
                    redirect_uri=self.REDIRECT,
                    scopes=["read"],
                    session=session,
                )

    def test_one_retry_on_connection_failure(self):
        # Simulate first POST raising ConnectionError, second succeeding.
        class FlakySession:
            def __init__(self):
                self.calls = 0
                self.last_call: Dict[str, Any] = {}

            def post(self, url, *, json=None, headers=None, timeout=None):
                self.calls += 1
                self.last_call = {"url": url, "json": json,
                                  "headers": headers, "timeout": timeout}
                if self.calls == 1:
                    raise requests.exceptions.ConnectionError("flaky")
                return _FakeResponse(201, {"client_id": "cid-after-retry"})

        session = FlakySession()
        with _public_dns():
            with mock.patch("mcp_oauth.time.sleep"):  # don't actually sleep
                out = register_client_dcr(
                    self.ENDPOINT,
                    redirect_uri=self.REDIRECT,
                    scopes=["read"],
                    session=session,  # type: ignore[arg-type]
                )
        assert session.calls == 2
        assert out["client_id"] == "cid-after-retry"
