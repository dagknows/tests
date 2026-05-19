"""
Phase 3 unit tests for settings.py MCP OAuth doc handlers.

We stub heavy deps (gevent, Crypto, authlib, flask, etc.) before importing
`settings` so the tests run in any Python env. The tests target:

  * `_safe_doc_id_part`, `_structural_url_check`, `_normalize_oauth_config` —
    pure module-level helpers.
  * `setMCPServer` — oauth-specific validation paths.
  * `setMCPOAuthClient` / `get` / `delete`.
  * `setMCPOAuthConnection` / `get` / `delete` / list variants.
  * `setMCPOAuthState` / `get` (incl. TTL behaviour) / `delete`.
  * `acquireMCPOAuthRefreshLock` (incl. 409 + expired-purge retry) / release.
  * `_cascade_delete_oauth_for_server`.

All ES traffic is intercepted via patches on `requests.get/put/post/delete`.
No live ES needed.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Stub heavy deps so `import settings` succeeds even without them installed.
# These stubs are intentionally permissive — we never exercise the stubbed
# behaviour in these tests.
# ---------------------------------------------------------------------------


def _stub(name, **attrs):
    """
    Additive stub: get-or-create the module in sys.modules, then fill in any
    missing attrs. Pre-existing attrs are left alone so a different test
    file's stubs (which install before us in the same pytest run) aren't
    clobbered. Lets routes-test and settings-test coexist when run together.
    """
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    return mod


def _ensure_stubs():
    # gevent + sub-modules
    _stub("gevent", monkey=mock.MagicMock())
    _stub("gevent.monkey", patch_all=lambda *a, **kw: None)
    _stub("gevent.pywsgi", WSGIServer=mock.MagicMock())

    # Crypto family
    for n in (
        "Crypto", "Crypto.PublicKey", "Crypto.PublicKey.RSA",
        "Crypto.Cipher", "Crypto.Cipher.PKCS1_OAEP",
        "Crypto.Signature", "Crypto.Signature.PKCS1_v1_5",
        "Crypto.Hash", "Crypto.Random",
    ):
        _stub(n)
    # provide dummy attrs settings.py references
    sys.modules["Crypto.PublicKey"].RSA = mock.MagicMock()
    sys.modules["Crypto.Cipher"].PKCS1_OAEP = mock.MagicMock()
    sys.modules["Crypto.Signature"].PKCS1_v1_5 = mock.MagicMock()
    h = sys.modules["Crypto.Hash"]
    h.SHA512 = mock.MagicMock()
    h.SHA384 = mock.MagicMock()
    h.SHA256 = mock.MagicMock()
    h.SHA = mock.MagicMock()
    h.MD5 = mock.MagicMock()

    # authlib
    _stub("authlib")
    _stub("authlib.jose", jwt=mock.MagicMock())

    # flask — settings imports Flask, request, redirect, Response, jsonify
    _stub("flask",
          Flask=mock.MagicMock(),
          request=mock.MagicMock(),
          redirect=lambda *a, **kw: None,
          Response=mock.MagicMock(),
          jsonify=lambda *a, **kw: None)

    # flask_restful (Resource is subclassed by `class settings(Resource)`)
    class _DummyResource:
        pass
    _stub("flask_restful", Resource=_DummyResource, Api=mock.MagicMock())

    # elastic_mgr: settings imports * from it. Provide an elasticMgr class
    # with the methods settings.setupSelf uses.
    em = _stub("elastic_mgr")
    class _ElasticMgr:
        def urlForIndex(self, name):
            n = name[1:] if name.startswith("/") else name
            return "http://es/" + n
        def settingsIndexName(self, org):
            return org.lower() + "__settings"
    em.elasticMgr = _ElasticMgr
    em.index_name = lambda org, name: org.lower() + "__" + name
    em.ESURL = "http://es/"

    # utils — settings imports two helpers; we re-export trivial impls.
    _stub("utils",
          hasUnsafeCharacters=lambda s: False,
          hasUnsafeCharactersLLMModelName=lambda s: False)

    # werkzeug.serving (imported at top-level)
    _stub("werkzeug")
    _stub("werkzeug.serving")


_ensure_stubs()


# Patch ES URL so settings module-load doesn't try to talk to anything real.
import os as _os
_os.environ.setdefault("DAGKNOWS_ELASTIC_URL", "http://es")

# Add settings/src to path. This file is at
# tests/unit/settings/test_mcp_oauth_handlers.py — go up three dirs.
_HERE = _os.path.dirname(_os.path.abspath(__file__))
sys.path.insert(0, _os.path.normpath(_os.path.join(_HERE, "..", "..", "..",
                                                  "settings", "src")))

# Now safe to import settings.
import settings as settings_mod  # noqa: E402


# ===========================================================================
# Pure module-level helpers
# ===========================================================================


@pytest.mark.unit
class TestSafeDocIdPart:

    def test_strips_unsafe_chars(self):
        assert settings_mod._safe_doc_id_part("Admin/Supremo") == "Admin_Supremo"

    def test_keeps_alnum_dot_dash_underscore(self):
        assert settings_mod._safe_doc_id_part("a.b-c_d1") == "a.b-c_d1"

    def test_strips_leading_trailing(self):
        assert settings_mod._safe_doc_id_part("_.role.") == "role"

    def test_length_capped(self):
        assert len(settings_mod._safe_doc_id_part("x" * 200)) == 64

    def test_non_string_returns_empty(self):
        assert settings_mod._safe_doc_id_part(None) == ""
        assert settings_mod._safe_doc_id_part(42) == ""


@pytest.mark.unit
@pytest.mark.security
class TestStructuralUrlCheck:

    def test_valid_https(self):
        assert settings_mod._structural_url_check("https://issuer.example/") is None

    def test_empty(self):
        assert "non-empty" in (settings_mod._structural_url_check("") or "")

    def test_unsupported_scheme(self):
        msg = settings_mod._structural_url_check("ftp://x/")
        assert msg and "unsupported scheme" in msg

    def test_http_localhost_ok(self):
        assert settings_mod._structural_url_check("http://localhost:8080/") is None

    def test_http_public_rejected(self):
        msg = settings_mod._structural_url_check("http://issuer.example/")
        assert msg and "localhost" in msg

    def test_userinfo_rejected(self):
        msg = settings_mod._structural_url_check("https://x@issuer.example/")
        assert msg and "userinfo" in msg

    def test_no_host(self):
        msg = settings_mod._structural_url_check("https:///path")
        assert msg and "no host" in msg

    def test_oversize(self):
        msg = settings_mod._structural_url_check("https://x/" + "a" * 2500)
        assert msg and "2048" in msg


@pytest.mark.unit
class TestNormalizeOauthConfig:

    def test_strips_unknown_fields(self):
        out = settings_mod._normalize_oauth_config({
            "issuer_url": "https://x/",
            "scopes": ["a"],
            "MALICIOUS_FIELD": "danger",
        })
        assert "MALICIOUS_FIELD" not in out
        assert out["issuer_url"] == "https://x/"

    def test_defaults(self):
        out = settings_mod._normalize_oauth_config({"issuer_url": "https://x"})
        assert out["uses_dcr"] is True
        assert out["scopes"] == []
        assert out["audience"] is None
        assert out["redirect_uri"] is None
        assert out["discovered"] is None


# ===========================================================================
# Settings-instance test harness: instantiate via __new__ to skip __init__.
# ===========================================================================


def _make_instance(*, role="Admin", org="acme"):
    inst = settings_mod.settings.__new__(settings_mod.settings)
    inst.role = role
    inst.org = org
    inst.uid = "test-uid"
    inst.user_info = {"org": org, "uid": "test-uid", "role": role}
    inst.settings_index = "http://es/" + org.lower() + "__settings"
    inst.index_type = "/_doc/"
    inst.verbose = False
    return inst


class _FakeESResponse:
    """Minimal stand-in for requests.Response."""
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
    def json(self):
        return self._body


class _ESStub:
    """
    Tracks all requests.get/put/post/delete calls and serves canned answers.
    Use `.responses` (a dict keyed by (method, url) tuple) for hits; anything
    else returns 404 / 200 empty as appropriate.
    """

    def __init__(self):
        self.responses = {}
        self.calls = []

    def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        key = (method, url)
        if key in self.responses:
            return self.responses[key]
        # Default sensible behaviour per verb
        if method == "GET":
            return _FakeESResponse(404, {})
        if method == "PUT":
            return _FakeESResponse(201, {"result": "created"})
        if method == "POST":
            return _FakeESResponse(200, {"hits": {"hits": []}})
        if method == "DELETE":
            return _FakeESResponse(200, {})
        return _FakeESResponse(200, {})

    def install(self):
        return mock.patch.multiple(
            settings_mod.requests,
            get=mock.Mock(side_effect=lambda u, **kw: self._record("GET", u, **kw)),
            put=mock.Mock(side_effect=lambda u, **kw: self._record("PUT", u, **kw)),
            post=mock.Mock(side_effect=lambda u, **kw: self._record("POST", u, **kw)),
            delete=mock.Mock(side_effect=lambda u, **kw: self._record("DELETE", u, **kw)),
        )


# ===========================================================================
# setMCPServer — oauth-specific validation paths
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestSetMcpServerOAuth:

    BASE_OAUTH = {
        "name": "linear",
        "transport": "streamable_http",
        "url": "https://mcp.linear.app",
        "auth_type": "oauth",
        "oauth": {
            "issuer_url": "https://auth.linear.app",
            "scopes": ["read"],
        },
    }

    def test_stdio_rejected(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPServer({
                "server": {**self.BASE_OAUTH, "transport": "stdio"}
            })
        assert out["responsecode"] == "False"
        assert "stdio" in out["error"]

    def test_missing_issuer_rejected(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPServer({
                "server": {**self.BASE_OAUTH, "oauth": {}},
            })
        assert out["responsecode"] == "False"
        assert "issuer_url" in out["error"]

    def test_bad_issuer_scheme_rejected(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPServer({
                "server": {
                    **self.BASE_OAUTH,
                    "oauth": {"issuer_url": "ftp://auth.linear.app"},
                },
            })
        assert out["responsecode"] == "False"
        assert "issuer_url" in out["error"]

    def test_scopes_must_be_list_of_strings(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPServer({
                "server": {
                    **self.BASE_OAUTH,
                    "oauth": {**self.BASE_OAUTH["oauth"], "scopes": ["ok", 42]},
                },
            })
        assert out["responsecode"] == "False"
        assert "scopes" in out["error"]

    def test_audience_must_be_string(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPServer({
                "server": {
                    **self.BASE_OAUTH,
                    "oauth": {**self.BASE_OAUTH["oauth"], "audience": 12345},
                },
            })
        assert out["responsecode"] == "False"
        assert "audience" in out["error"]

    def test_happy_path_persists_normalized_oauth(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.setMCPServer({"server": self.BASE_OAUTH})
        assert out["responsecode"] == "True"
        normalized = out["server"]
        assert normalized["auth_type"] == "oauth"
        assert normalized["oauth"]["issuer_url"] == "https://auth.linear.app"
        assert normalized["oauth"]["scopes"] == ["read"]
        assert normalized["oauth"]["uses_dcr"] is True  # default
        # And the PUT happened with the same shape.
        puts = [c for c in stub.calls if c["method"] == "PUT"]
        assert any("__mcp_servers" in c["url"] for c in puts)

    def test_switching_from_oauth_drops_stale_block(self):
        # Round-trip: existing record had auth_type='oauth' and oauth={...};
        # new save with auth_type='bearer' must persist oauth=None.
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.setMCPServer({
                "server": {
                    "name": "linear",
                    "transport": "streamable_http",
                    "url": "https://mcp.linear.app",
                    "auth_type": "bearer",
                    "oauth": {"issuer_url": "https://stale.example/"},
                },
            })
        assert out["responsecode"] == "True"
        # Stale oauth block should be cleared.
        assert out["server"]["oauth"] is None


# ===========================================================================
# OAuth client doc handlers
# ===========================================================================


@pytest.mark.unit
class TestMcpOAuthClientHandlers:

    def test_set_persists(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.setMCPOAuthClient({
                "server_id": "mcp_linear_abcd",
                "client": {
                    "client_id": "cid-1",
                    "client_secret": "sec-1",
                    "source": "dcr",
                },
            })
        assert out["responsecode"] == "True"
        puts = [c for c in stub.calls if c["method"] == "PUT"]
        assert puts and "__mcp_oauth_client__mcp_linear_abcd" in puts[0]["url"]
        assert puts[0]["json"]["client_id"] == "cid-1"

    def test_set_requires_server_id(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPOAuthClient({
                "client": {"client_id": "cid"}
            })
        assert out["responsecode"] == "False"

    def test_set_requires_client_id(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPOAuthClient({
                "server_id": "sid",
                "client": {},
            })
        assert out["responsecode"] == "False"

    def test_get_returns_source(self):
        inst = _make_instance()
        stub = _ESStub()
        stub.responses[("GET", "http://es/acme__settings/_doc/__mcp_oauth_client__sid")] = \
            _FakeESResponse(200, {"_source": {"client_id": "stored"}})
        with stub.install():
            out = inst.getMCPOAuthClient({"server_id": "sid"})
        assert out["client"]["client_id"] == "stored"

    def test_delete_calls_delete(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.deleteMCPOAuthClient({"server_id": "sid"})
        assert out["responsecode"] == "True"
        assert any(c["method"] == "DELETE" for c in stub.calls)


# ===========================================================================
# Connection doc handlers
# ===========================================================================


@pytest.mark.unit
class TestMcpOAuthConnectionHandlers:

    def test_set_uses_composite_doc_id(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            inst.setMCPOAuthConnection({
                "server_id": "mcp_linear_x",
                "proxy_alias": "prox/east",   # contains a slash
                "role": "Admin",
                "expires_at": 1700000000,
                "scopes": ["read"],
            })
        puts = [c for c in stub.calls if c["method"] == "PUT"]
        url = puts[0]["url"]
        # `/` in proxy_alias must have been sanitized to `_`.
        assert "prox_east" in url
        assert "__mcp_oauth_connection__mcp_linear_x__prox_east__Admin" in url
        # And the doc carries `server_id` for cascade queries.
        assert puts[0]["json"]["server_id"] == "mcp_linear_x"

    def test_list_for_server_uses_term_query(self):
        inst = _make_instance()
        stub = _ESStub()
        stub.responses[("POST", "http://es/acme__settings/_search")] = _FakeESResponse(
            200, {"hits": {"hits": [
                {"_source": {"server_id": "sid", "proxy_alias": "p",
                             "role": "r", "expires_at": 100}},
            ]}}
        )
        with stub.install():
            out = inst.listMCPOAuthConnections({"server_id": "sid"})
        # First POST should be a _search with the term query.
        posts = [c for c in stub.calls if c["method"] == "POST"]
        assert posts[0]["json"]["query"] == {"term": {"server_id": "sid"}}
        assert out["connections"][0]["server_id"] == "sid"

    def test_list_expiring_filters_by_range(self):
        inst = _make_instance()
        stub = _ESStub()
        stub.responses[("POST", "http://es/acme__settings/_search")] = _FakeESResponse(
            200, {"hits": {"hits": []}}
        )
        with stub.install():
            inst.listExpiringOAuthConnections({"within_seconds": 120})
        posts = [c for c in stub.calls if c["method"] == "POST"]
        query = posts[0]["json"]["query"]
        assert "bool" in query
        # Range must filter by lte threshold; we don't pin the exact value
        # because it's time.time()-derived but assert the shape.
        clauses = query["bool"]["must"]
        assert any("range" in c and "expires_at" in c["range"]
                   for c in clauses)

    def test_delete_requires_all_three(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.deleteMCPOAuthConnection({
                "server_id": "sid", "proxy_alias": "px",  # role missing
            })
        assert out["responsecode"] == "False"


# ===========================================================================
# State doc handlers
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestMcpOAuthStateHandlers:

    def test_set_uses_op_type_create(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.setMCPOAuthState({
                "state": "STATEXXXXXXXXXXXX",
                "server_id": "sid",
                "role": "Admin",
                "proxy_alias": "px",
                "code_verifier": "VERIFIER",
                "admin_uid": "u1",
                "redirect_uri": "https://app/cb",
                "nonce": "N",
            })
        assert out["responsecode"] == "True"
        puts = [c for c in stub.calls if c["method"] == "PUT"]
        assert "op_type=create" in puts[0]["url"]

    def test_short_state_rejected(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPOAuthState({
                "state": "short", "server_id": "sid",
            })
        assert out["responsecode"] == "False"

    def test_bad_state_charset_rejected(self):
        inst = _make_instance()
        with _ESStub().install():
            out = inst.setMCPOAuthState({
                "state": "abcd!@#$%^&*1234XX", "server_id": "sid",
            })
        assert out["responsecode"] == "False"

    def test_get_expired_treats_as_missing(self):
        import time as _time
        inst = _make_instance()
        stub = _ESStub()
        stub.responses[("GET", "http://es/acme__settings/_doc/__mcp_oauth_state__STATEAAAAAAAAAAA")] = \
            _FakeESResponse(200, {"_source": {
                "state": "STATEAAAAAAAAAAA",
                "server_id": "sid",
                "expires_at": int(_time.time()) - 60,  # already expired
            }})
        with stub.install():
            out = inst.getMCPOAuthState({"state": "STATEAAAAAAAAAAA"})
        assert out["expired"] is True
        assert out["state_doc"] == {}

    def test_get_fresh_returns_doc(self):
        import time as _time
        inst = _make_instance()
        stub = _ESStub()
        stub.responses[("GET", "http://es/acme__settings/_doc/__mcp_oauth_state__STATEBBBBBBBBBBB")] = \
            _FakeESResponse(200, {"_source": {
                "state": "STATEBBBBBBBBBBB",
                "server_id": "sid",
                "expires_at": int(_time.time()) + 600,
            }})
        with stub.install():
            out = inst.getMCPOAuthState({"state": "STATEBBBBBBBBBBB"})
        assert out["expired"] is False
        assert out["state_doc"]["server_id"] == "sid"


# ===========================================================================
# Refresh-lock handlers
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestMcpOAuthRefreshLock:

    def test_acquire_succeeds_on_201(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.acquireMCPOAuthRefreshLock({
                "server_id": "sid", "role": "Admin", "ttl_seconds": 30,
            })
        assert out["acquired"] is True

    def test_acquire_409_held_returns_false(self):
        inst = _make_instance()
        stub = _ESStub()
        # First PUT 409, GET returns NON-expired holder.
        lock_url = ("http://es/acme__settings/_doc/__mcp_oauth_refresh_lock__sid__Admin"
                    "?op_type=create&refresh=true")
        get_url = ("http://es/acme__settings/_doc/__mcp_oauth_refresh_lock__sid__Admin")
        import time as _time
        stub.responses[("PUT", lock_url)] = _FakeESResponse(409, {})
        stub.responses[("GET", get_url)] = _FakeESResponse(200, {"_source": {
            "expires_at": int(_time.time()) + 30,
        }})
        with stub.install():
            out = inst.acquireMCPOAuthRefreshLock({
                "server_id": "sid", "role": "Admin",
            })
        assert out["acquired"] is False
        # No DELETE should have fired because the existing lock is still live.
        deletes = [c for c in stub.calls if c["method"] == "DELETE"]
        assert deletes == []

    def test_acquire_409_with_expired_holder_purges_and_retries(self):
        inst = _make_instance()
        stub = _ESStub()
        lock_url = ("http://es/acme__settings/_doc/__mcp_oauth_refresh_lock__sid__Admin"
                    "?op_type=create&refresh=true")
        get_url = "http://es/acme__settings/_doc/__mcp_oauth_refresh_lock__sid__Admin"
        import time as _time
        # First PUT 409, GET shows expired holder, DELETE clears it, second PUT 201.
        puts = [_FakeESResponse(409, {}), _FakeESResponse(201, {})]
        put_iter = iter(puts)
        def put_side(url, **kw):
            return next(put_iter) if "op_type=create" in url else _FakeESResponse(201, {})
        stub.responses[("GET", get_url)] = _FakeESResponse(200, {"_source": {
            "expires_at": int(_time.time()) - 60,  # expired
        }})
        with mock.patch.object(settings_mod.requests, "put", side_effect=put_side), \
             stub.install():
            out = inst.acquireMCPOAuthRefreshLock({
                "server_id": "sid", "role": "Admin",
            })
        assert out["acquired"] is True

    def test_release_deletes(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            out = inst.releaseMCPOAuthRefreshLock({
                "server_id": "sid", "role": "Admin",
            })
        assert out["responsecode"] == "True"
        assert any(c["method"] == "DELETE" for c in stub.calls)

    def test_ttl_bounds_enforced(self):
        inst = _make_instance()
        with _ESStub().install():
            assert inst.acquireMCPOAuthRefreshLock({
                "server_id": "sid", "role": "Admin", "ttl_seconds": 3,
            })["responsecode"] == "False"
            assert inst.acquireMCPOAuthRefreshLock({
                "server_id": "sid", "role": "Admin", "ttl_seconds": 9999,
            })["responsecode"] == "False"


# ===========================================================================
# Cascade
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestCascadeOAuthDelete:

    def test_cascade_helper_deletes_client_and_runs_dbq(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            inst._cascade_delete_oauth_for_server("mcp_linear_abcd")
        # Client doc deletion
        deletes = [c for c in stub.calls if c["method"] == "DELETE"]
        assert any("__mcp_oauth_client__mcp_linear_abcd" in c["url"]
                   for c in deletes)
        # _delete_by_query call
        posts = [c for c in stub.calls if c["method"] == "POST"]
        assert any(c["url"].endswith("_delete_by_query?refresh=true&conflicts=proceed")
                   and c["json"]["query"] == {"term": {"server_id": "mcp_linear_abcd"}}
                   for c in posts)

    def test_cascade_with_empty_id_is_noop(self):
        inst = _make_instance()
        stub = _ESStub()
        with stub.install():
            inst._cascade_delete_oauth_for_server("")
        assert stub.calls == []
