"""
Phase 1 pure-helper unit tests for `req_router.mcp_oauth`:

  generate_pkce, build_authorization_url, _apply_token_endpoint_auth,
  exchange_code_for_tokens, refresh_access_token, revoke_token,
  _parse_token_response.

HTTP behavior is exercised via a mocked Session (no network). SSRF validator
DNS lookups are mocked to public IPs so the validator doesn't reject the test
URLs.
"""

from __future__ import annotations

import base64
import hashlib
import re
import socket
from typing import Any, Dict, List, Optional
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from mcp_oauth import (
    OAUTH_TIMEOUT,
    OAuthError,
    OAuthHTTPError,
    OAuthInvalidGrantError,
    PKCE_METHOD,
    _apply_token_endpoint_auth,
    _parse_token_response,
    build_authorization_url,
    exchange_code_for_tokens,
    generate_pkce,
    refresh_access_token,
    revoke_token,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _public_dns():
    """Mock socket.getaddrinfo so the SSRF validator allows test URLs."""
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


class _RecordingSession:
    """
    Stand-in for `requests.Session` that records POSTs and returns a
    pre-programmed response. Avoids requests-mock as a new dep.
    """

    def __init__(self, responses: Optional[List[_FakeResponse]] = None,
                 exceptions: Optional[List[BaseException]] = None):
        # `responses` is consumed FIFO. `exceptions` is parallel: if entry N
        # is set, that POST raises instead of returning.
        self.responses = list(responses or [])
        self.exceptions = list(exceptions or [None] * len(self.responses))
        self.calls: List[Dict[str, Any]] = []

    def post(self, url, *, data=None, auth=None, headers=None, timeout=None):
        idx = len(self.calls)
        self.calls.append({
            "url": url, "data": data, "auth": auth,
            "headers": headers, "timeout": timeout,
        })
        if idx < len(self.exceptions) and self.exceptions[idx] is not None:
            raise self.exceptions[idx]
        if idx < len(self.responses):
            return self.responses[idx]
        raise AssertionError(f"unexpected POST #{idx} to {url}")


# ---------------------------------------------------------------------------
# generate_pkce
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestGeneratePkce:

    def test_returns_verifier_and_challenge(self):
        verifier, challenge = generate_pkce()
        assert verifier
        assert challenge
        assert verifier != challenge

    def test_verifier_charset_url_safe(self):
        # RFC 7636 §4.1: verifier is [A-Z][a-z][0-9]-._~ only (no padding).
        verifier, _ = generate_pkce()
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", verifier), verifier

    def test_verifier_length_within_rfc_range(self):
        # 43 chars from 32 random bytes via token_urlsafe; well within 43-128.
        verifier, _ = generate_pkce()
        assert 43 <= len(verifier) <= 128

    def test_challenge_is_s256_of_verifier(self):
        verifier, challenge = generate_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_challenge_has_no_padding(self):
        _, challenge = generate_pkce()
        assert "=" not in challenge

    def test_pairs_are_unique_across_calls(self):
        pairs = {generate_pkce() for _ in range(100)}
        # All 100 should be distinct — birthday probability of collision in
        # 32-byte secrets is astronomically low.
        assert len(pairs) == 100

    def test_method_is_s256(self):
        assert PKCE_METHOD == "S256"


# ---------------------------------------------------------------------------
# build_authorization_url
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestBuildAuthorizationUrl:

    BASE_KWARGS = dict(
        client_id="cid-123",
        redirect_uri="https://app.dagknows.com/api/v1/mcp/oauth/callback",
        scopes=["read", "write"],
        state="STATE_XYZ",
        code_challenge="CHALLENGE_ABC",
        resource="https://mcp.linear.app",
    )

    def _build(self, **overrides):
        kwargs = {**self.BASE_KWARGS, **overrides}
        with _public_dns():
            return build_authorization_url(
                "https://issuer.example.com/oauth/authorize", **kwargs
            )

    def _qs(self, url: str) -> Dict[str, List[str]]:
        return parse_qs(urlparse(url).query)

    def test_required_params_present(self):
        url = self._build()
        qs = self._qs(url)
        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == ["cid-123"]
        assert qs["redirect_uri"] == [self.BASE_KWARGS["redirect_uri"]]
        assert qs["state"] == ["STATE_XYZ"]
        assert qs["code_challenge"] == ["CHALLENGE_ABC"]
        assert qs["code_challenge_method"] == ["S256"]

    def test_rfc_8707_resource_included(self):
        url = self._build()
        assert self._qs(url)["resource"] == ["https://mcp.linear.app"]

    def test_scopes_joined_with_space(self):
        url = self._build(scopes=["a", "b", "c"])
        # parse_qs un-decodes the space encoding.
        assert self._qs(url)["scope"] == ["a b c"]

    def test_empty_scopes_omits_scope_param(self):
        url = self._build(scopes=[])
        assert "scope" not in self._qs(url)

    def test_audience_omitted_by_default(self):
        url = self._build()
        assert "audience" not in self._qs(url)

    def test_audience_included_when_provided(self):
        url = self._build(audience="https://api.example.com")
        assert self._qs(url)["audience"] == ["https://api.example.com"]

    def test_extra_params_appended(self):
        url = self._build(extra_params={"prompt": "consent"})
        assert self._qs(url)["prompt"] == ["consent"]

    def test_extra_params_cannot_override_security_keys(self):
        # PKCE and resource params are security-relevant; refuse override.
        with pytest.raises(ValueError, match="may not override"):
            self._build(extra_params={"code_challenge": "EVIL"})
        with pytest.raises(ValueError, match="may not override"):
            self._build(extra_params={"resource": "https://evil.example"})

    def test_existing_query_in_endpoint_preserved(self):
        with _public_dns():
            url = build_authorization_url(
                "https://issuer.example.com/oauth/authorize?tenant=acme",
                **self.BASE_KWARGS,
            )
        qs = self._qs(url)
        assert qs["tenant"] == ["acme"]
        assert qs["client_id"] == ["cid-123"]  # ours also present

    def test_url_validation_rejects_private_endpoint(self):
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                           ("10.0.0.5", 0))]
        ):
            with pytest.raises(Exception):  # SSRFValidationError
                build_authorization_url(
                    "https://internal.local/oauth/authorize",
                    **self.BASE_KWARGS,
                )

    def test_url_validation_rejects_private_redirect(self):
        # The redirect_uri is also user-controllable (via canonical computation
        # at server start). Validate it too — otherwise an attacker who can
        # influence config could redirect through an internal address.
        def _resolve(host, port, *args, **kwargs):
            ip = "10.0.0.5" if "internal" in host else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port or 0))]
        with mock.patch("socket.getaddrinfo", _resolve):
            with pytest.raises(Exception):
                build_authorization_url(
                    "https://issuer.example.com/oauth/authorize",
                    **{**self.BASE_KWARGS, "redirect_uri": "https://internal.local/cb"},
                )


# ---------------------------------------------------------------------------
# _apply_token_endpoint_auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestApplyTokenEndpointAuth:

    def test_basic_returns_httpbasicauth_and_keeps_body_clean(self):
        body = {"grant_type": "authorization_code"}
        out_body, auth = _apply_token_endpoint_auth(
            "client_secret_basic", "cid", "secret", body,
        )
        assert auth is not None
        # Body must NOT also contain client credentials when using Basic.
        assert "client_id" not in out_body
        assert "client_secret" not in out_body

    def test_post_puts_creds_in_body(self):
        body = {"grant_type": "authorization_code"}
        out_body, auth = _apply_token_endpoint_auth(
            "client_secret_post", "cid", "secret", body,
        )
        assert auth is None
        assert out_body["client_id"] == "cid"
        assert out_body["client_secret"] == "secret"

    def test_none_method_omits_secret(self):
        body = {"grant_type": "authorization_code"}
        out_body, auth = _apply_token_endpoint_auth(
            "none", "cid", None, body,
        )
        assert auth is None
        assert out_body["client_id"] == "cid"
        assert "client_secret" not in out_body

    def test_basic_requires_secret(self):
        with pytest.raises(OAuthError, match="client_secret"):
            _apply_token_endpoint_auth(
                "client_secret_basic", "cid", None, {},
            )

    def test_post_requires_secret(self):
        with pytest.raises(OAuthError, match="client_secret"):
            _apply_token_endpoint_auth(
                "client_secret_post", "cid", None, {},
            )

    def test_unsupported_method_rejected(self):
        with pytest.raises(OAuthError, match="unsupported token_endpoint_auth_method"):
            _apply_token_endpoint_auth(
                "private_key_jwt", "cid", "secret", {},
            )


# ---------------------------------------------------------------------------
# _parse_token_response
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestParseTokenResponse:

    def test_success_returns_body(self):
        resp = _FakeResponse(200, {"access_token": "AT", "token_type": "Bearer"})
        out = _parse_token_response(resp)
        assert out["access_token"] == "AT"

    def test_200_without_access_token_raises(self):
        resp = _FakeResponse(200, {"token_type": "Bearer"})
        with pytest.raises(OAuthHTTPError, match="missing access_token"):
            _parse_token_response(resp)

    def test_invalid_grant_raises_specific_subclass(self):
        resp = _FakeResponse(400, {"error": "invalid_grant"})
        with pytest.raises(OAuthInvalidGrantError) as ei:
            _parse_token_response(resp)
        assert ei.value.error_code == "invalid_grant"
        assert ei.value.status_code == 400

    def test_other_oauth_error_raises_http_error(self):
        resp = _FakeResponse(401, {"error": "invalid_client"})
        with pytest.raises(OAuthHTTPError) as ei:
            _parse_token_response(resp)
        assert ei.value.error_code == "invalid_client"
        assert not isinstance(ei.value, OAuthInvalidGrantError)

    def test_non_json_body_raises(self):
        resp = _FakeResponse(200, ValueError("not json"))
        with pytest.raises(OAuthHTTPError, match="non-JSON body"):
            _parse_token_response(resp)


# ---------------------------------------------------------------------------
# exchange_code_for_tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestExchangeCodeForTokens:

    GOOD = {
        "access_token": "AT_abc",
        "refresh_token": "RT_xyz",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "read write",
    }

    def _call(self, session, **kwargs_override):
        kwargs = dict(
            client_id="cid",
            client_secret="secret",
            redirect_uri="https://app.dagknows.com/api/v1/mcp/oauth/callback",
            code="AUTH_CODE",
            code_verifier="VERIFIER",
            resource="https://mcp.linear.app",
            session=session,
        )
        kwargs.update(kwargs_override)
        with _public_dns():
            return exchange_code_for_tokens(
                "https://issuer.example.com/oauth/token", **kwargs,
            )

    def test_happy_path_returns_token_response(self):
        sess = _RecordingSession([_FakeResponse(200, self.GOOD)])
        out = self._call(sess)
        assert out["access_token"] == "AT_abc"
        assert out["refresh_token"] == "RT_xyz"

    def test_sends_correct_grant_type_and_pkce(self):
        sess = _RecordingSession([_FakeResponse(200, self.GOOD)])
        self._call(sess)
        body = sess.calls[0]["data"]
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "AUTH_CODE"
        assert body["code_verifier"] == "VERIFIER"
        assert body["redirect_uri"] == \
            "https://app.dagknows.com/api/v1/mcp/oauth/callback"
        assert body["resource"] == "https://mcp.linear.app"

    def test_basic_auth_header_set_when_method_basic(self):
        sess = _RecordingSession([_FakeResponse(200, self.GOOD)])
        self._call(sess, token_endpoint_auth_method="client_secret_basic")
        # HTTPBasicAuth applied via the `auth=` kwarg; not in body.
        assert sess.calls[0]["auth"] is not None
        body = sess.calls[0]["data"]
        assert "client_id" not in body
        assert "client_secret" not in body

    def test_post_method_puts_creds_in_body(self):
        sess = _RecordingSession([_FakeResponse(200, self.GOOD)])
        self._call(sess, token_endpoint_auth_method="client_secret_post")
        assert sess.calls[0]["auth"] is None
        body = sess.calls[0]["data"]
        assert body["client_id"] == "cid"
        assert body["client_secret"] == "secret"

    def test_invalid_grant_surfaces_specific_exception(self):
        sess = _RecordingSession([_FakeResponse(400, {"error": "invalid_grant"})])
        with pytest.raises(OAuthInvalidGrantError):
            self._call(sess)

    def test_other_4xx_raises_generic_http_error(self):
        sess = _RecordingSession([_FakeResponse(401, {"error": "invalid_client"})])
        with pytest.raises(OAuthHTTPError) as ei:
            self._call(sess)
        # MUST NOT be the invalid_grant subclass — callers branch on type.
        assert not isinstance(ei.value, OAuthInvalidGrantError)

    def test_no_retry_on_4xx(self):
        sess = _RecordingSession([_FakeResponse(400, {"error": "invalid_grant"})])
        with pytest.raises(OAuthInvalidGrantError):
            self._call(sess)
        assert len(sess.calls) == 1  # zero retries on HTTP errors

    def test_one_retry_on_connection_error(self):
        sess = _RecordingSession(
            responses=[None, _FakeResponse(200, self.GOOD)],
            exceptions=[requests.exceptions.ConnectionError("boom"), None],
        )
        with mock.patch("mcp_oauth.time.sleep"):  # don't actually sleep
            out = self._call(sess)
        assert out["access_token"] == "AT_abc"
        assert len(sess.calls) == 2

    def test_two_connection_errors_surface_as_http_error(self):
        sess = _RecordingSession(
            responses=[None, None],
            exceptions=[
                requests.exceptions.ConnectionError("e1"),
                requests.exceptions.ConnectionError("e2"),
            ],
        )
        with mock.patch("mcp_oauth.time.sleep"):
            with pytest.raises(OAuthHTTPError, match="connection.*failed"):
                self._call(sess)
        assert len(sess.calls) == 2  # one retry, then surface

    def test_timeout_passed_to_session(self):
        sess = _RecordingSession([_FakeResponse(200, self.GOOD)])
        self._call(sess)
        assert sess.calls[0]["timeout"] == OAUTH_TIMEOUT


# ---------------------------------------------------------------------------
# refresh_access_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestRefreshAccessToken:

    REFRESHED = {
        "access_token": "AT_new",
        "refresh_token": "RT_rotated",
        "expires_in": 3600,
        "token_type": "Bearer",
    }

    def _call(self, session, **kwargs_override):
        kwargs = dict(
            client_id="cid",
            client_secret="secret",
            refresh_token="RT_old",
            resource="https://mcp.linear.app",
            session=session,
        )
        kwargs.update(kwargs_override)
        with _public_dns():
            return refresh_access_token(
                "https://issuer.example.com/oauth/token", **kwargs,
            )

    def test_happy_path(self):
        sess = _RecordingSession([_FakeResponse(200, self.REFRESHED)])
        out = self._call(sess)
        assert out["access_token"] == "AT_new"
        assert out["refresh_token"] == "RT_rotated"  # rotation case

    def test_sends_refresh_grant_and_resource(self):
        sess = _RecordingSession([_FakeResponse(200, self.REFRESHED)])
        self._call(sess)
        body = sess.calls[0]["data"]
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "RT_old"
        assert body["resource"] == "https://mcp.linear.app"
        assert "scope" not in body  # not passed → not narrowed

    def test_scope_passed_when_provided(self):
        sess = _RecordingSession([_FakeResponse(200, self.REFRESHED)])
        self._call(sess, scopes=["read"])
        assert sess.calls[0]["data"]["scope"] == "read"

    def test_empty_scopes_omits_param(self):
        sess = _RecordingSession([_FakeResponse(200, self.REFRESHED)])
        self._call(sess, scopes=[])
        assert "scope" not in sess.calls[0]["data"]

    def test_invalid_grant_distinguished(self):
        # The whole point of OAuthInvalidGrantError as a separate type.
        sess = _RecordingSession([_FakeResponse(400, {"error": "invalid_grant"})])
        with pytest.raises(OAuthInvalidGrantError):
            self._call(sess)

    def test_5xx_raises_http_error(self):
        sess = _RecordingSession([_FakeResponse(503, {"error": "server_busy"})])
        with pytest.raises(OAuthHTTPError) as ei:
            self._call(sess)
        assert ei.value.status_code == 503


# ---------------------------------------------------------------------------
# revoke_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
class TestRevokeToken:

    def _call(self, session, **kwargs_override):
        kwargs = dict(
            client_id="cid",
            client_secret="secret",
            token="TOKEN_TO_KILL",
            session=session,
        )
        kwargs.update(kwargs_override)
        with _public_dns():
            return revoke_token(
                "https://issuer.example.com/oauth/revoke", **kwargs,
            )

    def test_200_returns_true(self):
        sess = _RecordingSession([_FakeResponse(200, {})])
        assert self._call(sess) is True

    def test_4xx_returns_false_does_not_raise(self):
        # RFC 7009 §2.2 — issuer SHOULD return 200 even for unknown tokens.
        # If we get a 4xx we treat as "issuer is being strict, oh well" and
        # let the caller proceed with the local cleanup.
        sess = _RecordingSession([_FakeResponse(400, {"error": "unsupported_token_type"})])
        assert self._call(sess) is False

    def test_5xx_raises(self):
        sess = _RecordingSession([_FakeResponse(503, {})])
        with pytest.raises(OAuthHTTPError):
            self._call(sess)

    def test_sends_token_and_hint(self):
        sess = _RecordingSession([_FakeResponse(200, {})])
        self._call(sess, token_type_hint="refresh_token")
        body = sess.calls[0]["data"]
        assert body["token"] == "TOKEN_TO_KILL"
        assert body["token_type_hint"] == "refresh_token"

    def test_invalid_hint_rejected(self):
        # Don't even open a socket for an obviously-bad hint.
        sess = _RecordingSession([_FakeResponse(200, {})])
        with pytest.raises(OAuthError, match="token_type_hint"):
            self._call(sess, token_type_hint="id_token")
        assert sess.calls == []

    def test_url_validation_runs(self):
        # Private revocation endpoint must be rejected before any HTTP.
        sess = _RecordingSession([_FakeResponse(200, {})])
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                           ("10.0.0.5", 0))]
        ):
            with pytest.raises(Exception):  # SSRFValidationError
                revoke_token(
                    "https://internal.local/oauth/revoke",
                    client_id="cid", client_secret="secret",
                    token="T", session=sess,
                )
        assert sess.calls == []  # never reached the wire
