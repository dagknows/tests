"""
Phase 4 unit tests for `req_router/src/mcp_oauth_routes.py`.

The route module imports Flask and DagKnows internals heavily. We stub
those at module load time (matching the approach used for the settings
tests) so the tests are pure and fast. Each test sets up:

  - A fake `request` object with `args`, `is_json`, `get_json()`
  - A fake `BaseTaskResource` returned from `tasks.BaseTaskResource`
  - Patched `mcp_oauth.*` helpers so token exchange / discovery /
    register don't talk to the network
  - A patched `_dispatch_vault_task` so vault writes are observed but
    don't run any real runbook

The tests exercise: admin-only enforcement, missing-field rejection,
state cross-check on /callback, invalid_grant → invalid_grant redirect,
proxy-offline → vault_write_failed redirect, refresh-lock racing,
disconnect ordering, /refreshExpiring service-auth gating, status shape.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest


def _stub(name, **attrs):
    """
    Additive stub: if the module already exists in sys.modules (e.g. another
    test in the same pytest run installed it), fill in any missing attrs but
    leave existing ones alone. This avoids cross-test collisions when two
    files stub the same heavy dep with slightly different shapes.
    """
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    return mod


# ---------- Stub heavy deps before importing mcp_oauth_routes ---------------


# `tasks` is the BaseTaskResource provider — stub it.
class _FakeBaseTaskResource:
    def __init__(self, *, is_admin=True, uid="admin-1", org="acme",
                 role="Admin"):
        self.is_authenticated = True
        self.uid = uid
        self.uname = uid + "@acme.com"
        self.org = org
        self.role = role
        self.custom_headers = {"X-Test": "1"}
        # settings_interface mock — handlers call res.settings.handleSettingsReq
        self.settings = mock.MagicMock()
        # Default: every settings RPC returns a permissive shape
        self.settings.handleSettingsReq.return_value = {
            "responsecode": "True"
        }

    def url(self, path):
        return "http://taskservice" + path


_tasks_mod = _stub("tasks")
_tasks_mod.BaseTaskResource = _FakeBaseTaskResource


# `app` module (for `from app import app` inside _service_token_auth)
_app_mod = _stub("app")
_app_mod.app = types.SimpleNamespace(config={"API_KEY": "TEST_SVC_KEY"})


# `settings_interface` for _settings_call_service path
_si_mod = _stub("settings_interface")
class _FakeSettingsInterface:
    def __init__(self, org, role):
        self.org = org
        self.role = role
        self.handleSettingsReq = mock.MagicMock(return_value={
            "responsecode": "True"
        })
_si_mod.settings_interface = _FakeSettingsInterface


# Flask stub — provides request/jsonify/redirect with predictable shapes.
class _FakeRequest:
    """Mutable per-test; replaced by `_set_request()` helpers."""
    def __init__(self):
        self.args = {}
        self.is_json = False
        self.headers = {}
        self._json = {}
        self.path = "/"
        self.method = "GET"

    def get_json(self):
        return self._json


_fake_request = _FakeRequest()


def _jsonify(payload):
    """Match Flask's jsonify return-shape closely enough for tests."""
    return {"_kind": "json", "payload": payload}


def _redirect(url, code=302):
    return {"_kind": "redirect", "url": url, "code": code}


# flask: get-or-create the module then hard-set our control attrs.
# Other tests may have already set Flask/Response — those are preserved by
# get_or_create, but the four below MUST be ours because the route handlers
# bind them directly and need our recording fakes.
_flask_mod = sys.modules.get("flask")
if _flask_mod is None:
    _flask_mod = types.ModuleType("flask")
    sys.modules["flask"] = _flask_mod
_flask_mod.request = _fake_request
_flask_mod.jsonify = _jsonify
_flask_mod.redirect = _redirect
_flask_mod.session = mock.MagicMock()


# Now safe to import the routes module — it lives in req_router/src/
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "req_router", "src")
))


# Make sure DAGKNOWS_URL is set so _compute_redirect_uri is deterministic.
os.environ["DAGKNOWS_URL"] = "https://app.dagknows.test"


import mcp_oauth_routes as routes  # noqa: E402


# ===========================================================================
# helpers used across tests
# ===========================================================================


def _set_request(*, args=None, is_json=False, body=None, headers=None,
                 method="GET", path="/"):
    _fake_request.args = args or {}
    _fake_request.is_json = is_json
    _fake_request._json = body or {}
    _fake_request.headers = headers or {}
    _fake_request.method = method
    _fake_request.path = path


def _server_record(server_id="sid", name="linear", oauth=None,
                   transport="streamable_http"):
    import time as _t
    return {
        "id": server_id,
        "name": name,
        "transport": transport,
        "url": "https://mcp.linear.app",
        "auth_type": "oauth",
        "oauth": oauth or {
            "issuer_url": "https://auth.linear.app",
            "scopes": ["read"],
            "discovered": {
                "issuer": "https://auth.linear.app",
                "authorization_endpoint": "https://auth.linear.app/oauth/authorize",
                "token_endpoint": "https://auth.linear.app/oauth/token",
                "revocation_endpoint": "https://auth.linear.app/oauth/revoke",
                # `_fetched_at = now` keeps the discovery cache fresh so
                # _ensure_discovery returns without doing live DNS.
                "_fetched_at": int(_t.time()),
            },
            "redirect_uri": "https://app.dagknows.test/api/v1/mcp/oauth/callback",
            "uses_dcr": True,
        },
    }


def _mock_settings(res, response_map):
    """
    Wire res.settings.handleSettingsReq to dispatch into a dict keyed by
    req-name, returning the canned response. Any req-name not in the map
    yields a generic {'responsecode': 'True'}.
    """
    def _dispatch(user_info, req_name, body):
        if req_name in response_map:
            v = response_map[req_name]
            return v(body) if callable(v) else v
        return {"responsecode": "True"}
    res.settings.handleSettingsReq = mock.MagicMock(side_effect=_dispatch)


# ===========================================================================
# /start
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestRouteStart:

    def test_admin_only(self):
        res = _FakeBaseTaskResource(role="Customer")
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True,
                         body={"server_id": "sid", "role": "r",
                               "proxy_alias": "p"})
            out = routes._route_start()
        # _admin_only returns (jsonify(...), 403)
        assert out[1] == 403

    def test_missing_fields(self):
        res = _FakeBaseTaskResource()
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True,
                         body={"server_id": "sid"})
            out = routes._route_start()
        assert out[1] == 400

    def test_server_not_found(self):
        res = _FakeBaseTaskResource()
        _mock_settings(res, {
            "getMCPServers": {"servers": [], "responsecode": "True"},
        })
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "r", "proxy_alias": "p",
            })
            out = routes._route_start()
        assert out[1] == 404

    def test_non_oauth_server_rejected(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        s["auth_type"] = "bearer"
        _mock_settings(res, {
            "getMCPServers": {"servers": [s], "responsecode": "True"},
        })
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "r", "proxy_alias": "p",
            })
            out = routes._route_start()
        assert out[1] == 400

    def test_stdio_rejected(self):
        res = _FakeBaseTaskResource()
        s = _server_record(transport="stdio")
        _mock_settings(res, {
            "getMCPServers": {"servers": [s], "responsecode": "True"},
        })
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "r", "proxy_alias": "p",
            })
            out = routes._route_start()
        assert out[1] == 400

    def test_happy_path_returns_url_and_state(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        _mock_settings(res, {
            "getMCPServers": {"servers": [s], "responsecode": "True"},
            "getMCPOAuthClient": {
                "client": {
                    "client_id": "cid-1",
                    "client_secret": "sec-1",
                    "token_endpoint_auth_method": "client_secret_basic",
                },
                "responsecode": "True",
            },
            "setMCPOAuthState": {"responsecode": "True"},
        })
        # build_authorization_url re-validates the auth + redirect URLs;
        # mock DNS to a public IP so the SSRF check passes without network.
        import socket
        def _fake_dns(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                     ("93.184.216.34", port or 0))]
        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             mock.patch("socket.getaddrinfo", side_effect=_fake_dns):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "Admin", "proxy_alias": "px",
            })
            out = routes._route_start()
        assert out[1] == 200
        payload = out[0]["payload"]
        assert payload["responsecode"] == "True"
        assert "authorization_url" in payload
        # State must be persisted before redirect. Confirm via settings call.
        calls = [c[0][1] for c in res.settings.handleSettingsReq.call_args_list]
        assert "setMCPOAuthState" in calls
        # And the URL carries the standard OAuth params.
        url = payload["authorization_url"]
        assert "response_type=code" in url
        assert "code_challenge_method=S256" in url
        assert "resource=" in url

    def test_state_persist_failure_returns_500(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        _mock_settings(res, {
            "getMCPServers": {"servers": [s]},
            "getMCPOAuthClient": {
                "client": {"client_id": "cid", "client_secret": "sec"},
            },
            "setMCPOAuthState": {"responsecode": "False",
                                  "error": "conflict"},
        })
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "Admin", "proxy_alias": "px",
            })
            out = routes._route_start()
        assert out[1] == 500


# ===========================================================================
# /callback
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestRouteCallback:

    def _patch_service_settings(self, response_map):
        """Patch _settings_call_service to use the per-test map."""
        def fake(org, req_name, body):
            if req_name in response_map:
                v = response_map[req_name]
                return v(body) if callable(v) else v
            return {"responsecode": "True"}
        return mock.patch.object(routes, "_settings_call_service",
                                 side_effect=fake)

    def test_missing_code_or_state_redirects_error(self):
        res = _FakeBaseTaskResource()
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="GET", args={"state": "S"})
            out = routes._route_callback()
        assert out["_kind"] == "redirect"
        assert "oauth=error" in out["url"]
        assert "missing_code_or_state" in out["url"]

    def test_state_expired_redirects(self):
        res = _FakeBaseTaskResource()
        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             self._patch_service_settings({
                 "getMCPOAuthState": {"expired": True, "state_doc": {}},
             }):
            _set_request(method="GET", args={"code": "C", "state": "S"})
            out = routes._route_callback()
        assert "state_expired" in out["url"]

    def test_admin_mismatch_403_redirect(self):
        # Admin uid of the current request session differs from the uid
        # stored in the state doc. State must be torn down + 403.
        res = _FakeBaseTaskResource(uid="admin-X")
        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             self._patch_service_settings({
                 "getMCPOAuthState": {
                     "state_doc": {"server_id": "sid", "admin_uid": "admin-Y",
                                   "role": "r", "proxy_alias": "p",
                                   "code_verifier": "v",
                                   "redirect_uri": "https://app.dagknows.test/cb"},
                     "expired": False,
                 },
             }):
            _set_request(method="GET", args={"code": "C", "state": "S"})
            out = routes._route_callback()
        assert "admin_mismatch" in out["url"]
        assert out["code"] == 403

    def test_invalid_grant_redirects_with_reason(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             self._patch_service_settings({
                 "getMCPOAuthState": {
                     "state_doc": {"server_id": "sid", "admin_uid": "",
                                   "role": "r", "proxy_alias": "p",
                                   "code_verifier": "v",
                                   "redirect_uri": "https://app.dagknows.test/cb"},
                     "expired": False,
                 },
                 "getMCPServers": {"servers": [s]},
                 "getMCPOAuthClient": {"client": {"client_id": "cid",
                                                  "client_secret": "sec"}},
             }), \
             mock.patch.object(routes, "exchange_code_for_tokens",
                               side_effect=routes.OAuthInvalidGrantError("bad")):
            _set_request(method="GET", args={"code": "C", "state": "S"})
            out = routes._route_callback()
        assert "invalid_grant" in out["url"]

    def test_vault_write_failure_redirects(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             self._patch_service_settings({
                 "getMCPOAuthState": {
                     "state_doc": {"server_id": "sid", "admin_uid": "",
                                   "role": "r", "proxy_alias": "p",
                                   "code_verifier": "v",
                                   "redirect_uri": "https://app.dagknows.test/cb"},
                     "expired": False,
                 },
                 "getMCPServers": {"servers": [s]},
                 "getMCPOAuthClient": {"client": {"client_id": "cid",
                                                  "client_secret": "sec"}},
             }), \
             mock.patch.object(routes, "exchange_code_for_tokens",
                               return_value={"access_token": "AT",
                                              "refresh_token": "RT",
                                              "expires_in": 3600,
                                              "token_type": "Bearer",
                                              "scope": "read"}), \
             mock.patch.object(routes, "_dispatch_vault_task",
                               return_value=(False, "", "proxy unreachable")):
            _set_request(method="GET", args={"code": "C", "state": "S"})
            out = routes._route_callback()
        assert "vault_write_failed" in out["url"]

    def test_happy_path_writes_connection_and_redirects_success(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        service_calls = []

        def fake(org, req_name, body):
            service_calls.append((req_name, body))
            if req_name == "getMCPOAuthState":
                return {
                    "state_doc": {"server_id": "sid", "admin_uid": "",
                                  "role": "r", "proxy_alias": "p",
                                  "code_verifier": "v",
                                  "redirect_uri": "https://app.dagknows.test/cb"},
                    "expired": False,
                }
            if req_name == "getMCPServers":
                return {"servers": [s]}
            if req_name == "getMCPOAuthClient":
                return {"client": {"client_id": "cid",
                                   "client_secret": "sec"}}
            return {"responsecode": "True"}

        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             mock.patch.object(routes, "_settings_call_service",
                               side_effect=fake), \
             mock.patch.object(routes, "exchange_code_for_tokens",
                               return_value={"access_token": "AT",
                                              "refresh_token": "RT",
                                              "expires_in": 3600,
                                              "token_type": "Bearer",
                                              "scope": "read write"}), \
             mock.patch.object(routes, "_dispatch_vault_task",
                               return_value=(True, "ok", "")):
            _set_request(method="GET", args={"code": "C", "state": "S"})
            out = routes._route_callback()

        assert out["_kind"] == "redirect"
        assert "oauth=success" in out["url"]
        assert "server_id=sid" in out["url"]
        # Connection row must have been written.
        names = [c[0] for c in service_calls]
        assert "setMCPOAuthConnection" in names
        # And state must have been deleted.
        assert "deleteMCPOAuthState" in names


# ===========================================================================
# /refresh
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestRouteRefresh:

    def test_admin_only(self):
        res = _FakeBaseTaskResource(role="Customer")
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "r", "proxy_alias": "p",
            })
            out = routes._route_refresh()
        assert out[1] == 403

    def test_required_fields(self):
        res = _FakeBaseTaskResource()
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid",
            })
            out = routes._route_refresh()
        assert out[1] == 400

    def test_lock_held_returns_in_progress(self):
        res = _FakeBaseTaskResource()
        _mock_settings(res, {
            "acquireMCPOAuthRefreshLock": {"acquired": False,
                                            "responsecode": "True"},
        })
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "r", "proxy_alias": "p",
            })
            out = routes._route_refresh()
        assert out[1] == 400
        assert out[0]["payload"]["reason"] == "refresh_in_progress"

    def test_invalid_grant_marks_connection_stale(self):
        res = _FakeBaseTaskResource()
        s = _server_record()
        recorded = []

        def dispatch(user_info, req, body):
            recorded.append((req, body))
            if req == "acquireMCPOAuthRefreshLock":
                return {"acquired": True, "responsecode": "True",
                        "expires_at": 9_999_999}
            if req == "getMCPServers":
                return {"servers": [s]}
            if req == "getMCPOAuthClient":
                return {"client": {"client_id": "cid",
                                   "client_secret": "sec"}}
            return {"responsecode": "True"}
        res.settings.handleSettingsReq = mock.MagicMock(side_effect=dispatch)

        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             mock.patch.object(
                 routes, "_dispatch_vault_task",
                 return_value=(True, '{"values": {"LINEAR_OAUTH_REFRESH_TOKEN": "RT_old"}}', "")), \
             mock.patch.object(
                 routes, "refresh_access_token",
                 side_effect=routes.OAuthInvalidGrantError("rt expired")):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "Admin", "proxy_alias": "px",
            })
            out = routes._route_refresh()

        assert out[1] == 400
        assert out[0]["payload"]["reason"] == "invalid_grant"
        # Connection should be marked stale (expires_at <= now).
        conn_calls = [b for r, b in recorded if r == "setMCPOAuthConnection"]
        assert conn_calls
        assert conn_calls[-1]["last_refresh_outcome"] == "invalid_grant"
        # Lock release must have fired.
        assert any(r == "releaseMCPOAuthRefreshLock" for r, _ in recorded)


# ===========================================================================
# /disconnect
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestRouteDisconnect:

    def test_local_cleanup_ordering(self):
        # Plan §8: (1) delete vault keys → (2) delete connection row →
        # (3) invalidate pool. We assert order via the recorded sequence.
        res = _FakeBaseTaskResource()
        s = _server_record()
        # Disable revocation by removing the endpoint.
        s["oauth"]["discovered"].pop("revocation_endpoint", None)
        order = []

        def settings_dispatch(user_info, req, body):
            order.append(("settings:" + req, body))
            if req == "getMCPServers":
                return {"servers": [s]}
            if req == "getMCPOAuthClient":
                return {"client": {"client_id": "cid",
                                   "client_secret": "sec"}}
            return {"responsecode": "True"}
        res.settings.handleSettingsReq = mock.MagicMock(side_effect=settings_dispatch)

        def vault(res_arg, task_id, proxy_alias, params, wsid=""):
            order.append(("vault:" + task_id, params))
            return True, "", ""

        def fake_post(url, headers=None, timeout=None):
            order.append(("invalidate:" + url, None))
            return mock.MagicMock(status_code=200)

        with mock.patch("tasks.BaseTaskResource", return_value=res), \
             mock.patch.object(routes, "_dispatch_vault_task", side_effect=vault), \
             mock.patch.object(routes.requests, "post", side_effect=fake_post):
            _set_request(method="POST", is_json=True, body={
                "server_id": "sid", "role": "Admin", "proxy_alias": "px",
            })
            out = routes._route_disconnect()

        assert out[1] == 200
        # Walk the order list and find indices of the three steps.
        step1 = next(i for i, (k, _) in enumerate(order)
                     if k == "vault:mcp-vault-delete-keys")
        step2 = next(i for i, (k, _) in enumerate(order)
                     if k == "settings:deleteMCPOAuthConnection")
        step3 = next(i for i, (k, _) in enumerate(order)
                     if k.startswith("invalidate:"))
        assert step1 < step2 < step3


# ===========================================================================
# /status
# ===========================================================================


@pytest.mark.unit
class TestRouteStatus:

    def test_admin_only(self):
        res = _FakeBaseTaskResource(role="Customer")
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="GET", args={"server_id": "sid"})
            out = routes._route_status()
        assert out[1] == 403

    def test_shapes_connection_rows(self):
        import time as _time
        future = int(_time.time()) + 600
        past = int(_time.time()) - 60
        res = _FakeBaseTaskResource()
        _mock_settings(res, {
            "listMCPOAuthConnections": {
                "connections": [
                    {"server_id": "sid", "proxy_alias": "px", "role": "Admin",
                     "expires_at": future, "scopes": ["read"]},
                    {"server_id": "sid", "proxy_alias": "px", "role": "RO",
                     "expires_at": past, "scopes": []},
                ],
                "responsecode": "True",
            },
        })
        with mock.patch("tasks.BaseTaskResource", return_value=res):
            _set_request(method="GET", args={"server_id": "sid"})
            out = routes._route_status()
        assert out[1] == 200
        rows = out[0]["payload"]["connections"]
        # First row is connected, second is not.
        assert rows[0]["connected"] is True
        assert rows[1]["connected"] is False


# ===========================================================================
# /refreshExpiring
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestRouteRefreshExpiring:

    def test_service_auth_required(self):
        _set_request(method="POST", is_json=True, body={
            "api_key": "WRONG",
            "user_info": {"org": "acme"},
        })
        out = routes._route_refresh_expiring()
        assert out[1] == 401

    def test_missing_org_rejected(self):
        _set_request(method="POST", is_json=True, body={
            "api_key": "TEST_SVC_KEY",
            "user_info": {},
        })
        out = routes._route_refresh_expiring()
        assert out[1] == 401

    def test_accepts_secret_alias(self):
        # Also accepts the token under `secret` so it doesn't trip CSRF.
        # Run with no rows to verify the path completes.
        _set_request(method="POST", is_json=True, body={
            "secret": "TEST_SVC_KEY",
            "user_info": {"org": "acme"},
        })
        with mock.patch.object(routes, "_settings_call_service",
                               return_value={"connections": []}):
            out = routes._route_refresh_expiring()
        assert out[1] == 200
        assert out[0]["payload"]["refreshed"] == 0
