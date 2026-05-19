"""
Phase 5 unit tests for v6 OAuth integration:

  * `taskservice/src/v6/mcp_config.py`:
      - `_required_auth_keys` — oauth subset (just ACCESS_TOKEN)
      - `_compute_expected_vault_keys` — oauth branch returns 5 keys
      - `_apply_vault_creds` — oauth → Authorization: Bearer header injected;
        missing ACCESS_TOKEN → server skipped; missing optional keys → ok
      - `_apply_auth` — oauth pass-through (admin probe doesn't bake tokens)
      - `_server_oauth_expires_at` — reads expires_at from vault
      - `_refresh_oauth_tokens_if_expiring` — calls /refreshExpiring only
        when something is actually expiring; re-read trigger correct.

  * `taskservice/src/v6/mcp_pool.py`:
      - `MCPConnectionPool.invalidate(server_id, role)` — evicts cached
        entry, no-ops on missing entry.

  * `taskservice/src/v6/tools/mcp_client.py`:
      - `_parse_resource_metadata_url` — pulls `resource_metadata=`
        from a WWW-Authenticate value.

Heavy deps (mcp SDK, anyio, httpx) are stubbed at import so this runs
without pulling the full taskservice runtime.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from unittest import mock

import pytest


def _stub(name, **attrs):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    return mod


# Stub the v6 package itself + heavy deps so direct-loading the three
# target modules (mcp_config, mcp_pool, mcp_client) doesn't drag in
# llm_client / litellm / engine.
_stub("v6")
_stub("v6.tools")
_stub("v6.tools.mcp_client",
      connect_mcp_server=mock.MagicMock(),
      MCPToolAdapter=mock.MagicMock(),
      ToolDefinition=mock.MagicMock())
# MCP SDK family — both the top-level `mcp` symbols (`from mcp import
# ClientSession, StdioServerParameters`) and the submodule names mcp_pool /
# mcp_client import.
_stub("mcp",
      ClientSession=mock.MagicMock(),
      StdioServerParameters=mock.MagicMock())
_stub("mcp.client")
_stub("mcp.client.session", ClientSession=mock.MagicMock())
_stub("mcp.client.sse", sse_client=mock.MagicMock())
_stub("mcp.client.stdio", stdio_client=mock.MagicMock(),
      StdioServerParameters=mock.MagicMock())
_stub("mcp.client.streamable_http", streamable_http_client=mock.MagicMock())
_stub("mcp.types")
_stub("anyio")
_stub("httpx", AsyncClient=mock.MagicMock(), Timeout=mock.MagicMock())
# v6 internals referenced from mcp_pool / mcp_client at module load.
_stub("v6.mcp_event_loop", MCPEventLoop=mock.MagicMock())
_stub("v6.tool_interface",
      BaseTool=type("BaseTool", (), {}),
      ToolDefinition=mock.MagicMock(),
      ToolResult=type("ToolResult", (), {
          "__init__": lambda self, **kw: setattr(
              self, "__dict__", dict(self.__dict__, **kw)),
      }))
_stub("v6.session")  # imported by mcp_client only under TYPE_CHECKING


_HERE = os.path.dirname(os.path.abspath(__file__))
_V6_DIR = os.path.normpath(os.path.join(
    _HERE, "..", "..", "..", "taskservice", "src", "v6"
))
_TOOLS_DIR = os.path.join(_V6_DIR, "tools")


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cfg = _load_module("mcp_config_under_test",
                   os.path.join(_V6_DIR, "mcp_config.py"))


# ===========================================================================
# _required_auth_keys + _compute_expected_vault_keys
# ===========================================================================


@pytest.mark.unit
class TestRequiredAuthKeys:

    def test_oauth_requires_only_access_token(self):
        s = {"name": "linear", "auth_type": "oauth"}
        keys = cfg._required_auth_keys(s)
        assert keys == ["LINEAR_OAUTH_ACCESS_TOKEN"]

    def test_bearer_unchanged(self):
        s = {"name": "linear", "auth_type": "bearer"}
        assert cfg._required_auth_keys(s) == ["LINEAR_BEARER_TOKEN"]

    def test_api_key_unchanged(self):
        s = {"name": "linear", "auth_type": "api_key"}
        assert set(cfg._required_auth_keys(s)) == {
            "LINEAR_HEADER_NAME", "LINEAR_HEADER_VALUE",
        }

    def test_basic_unchanged(self):
        s = {"name": "linear", "auth_type": "basic"}
        assert set(cfg._required_auth_keys(s)) == {
            "LINEAR_USERNAME", "LINEAR_PASSWORD",
        }

    def test_none_empty(self):
        s = {"name": "linear", "auth_type": "none"}
        assert cfg._required_auth_keys(s) == []

    def test_no_name_empty(self):
        assert cfg._required_auth_keys({"name": "", "auth_type": "oauth"}) == []


@pytest.mark.unit
class TestComputeExpectedVaultKeysOauth:

    def test_oauth_returns_all_five_keys(self):
        s = {"name": "linear", "auth_type": "oauth"}
        auth_keys, env_keys = cfg._compute_expected_vault_keys(s)
        # Every OAuth key is in the fetch set; the required subset is
        # narrower (tested above).
        assert auth_keys == [
            "LINEAR_OAUTH_ACCESS_TOKEN",
            "LINEAR_OAUTH_REFRESH_TOKEN",
            "LINEAR_OAUTH_EXPIRES_AT",
            "LINEAR_OAUTH_TOKEN_TYPE",
            "LINEAR_OAUTH_SCOPES",
        ]
        assert env_keys == []

    def test_oauth_includes_env_keys(self):
        s = {"name": "linear", "auth_type": "oauth",
             "env": {"EXTRA_VAR": ""}}
        auth_keys, env_keys = cfg._compute_expected_vault_keys(s)
        assert "LINEAR_OAUTH_ACCESS_TOKEN" in auth_keys
        assert env_keys == ["EXTRA_VAR"]


# ===========================================================================
# _apply_vault_creds — oauth branch
# ===========================================================================


@pytest.mark.unit
class TestApplyVaultCredsOauth:

    def test_oauth_injects_bearer_header(self):
        s = {"name": "linear", "auth_type": "oauth",
             "transport": "streamable_http", "url": "https://x"}
        out, ok, missing, missing_env = cfg._apply_vault_creds(s, {
            "LINEAR_OAUTH_ACCESS_TOKEN": "AT_xyz",
            "LINEAR_OAUTH_EXPIRES_AT": "9999999999",
        })
        assert ok is True
        assert missing == [] and missing_env == []
        assert out["headers"]["Authorization"] == "Bearer AT_xyz"

    def test_oauth_strips_leading_bearer_in_token(self):
        # Defense in depth: vault stores `Bearer XYZ` should normalize.
        s = {"name": "linear", "auth_type": "oauth",
             "transport": "sse", "url": "https://x"}
        out, ok, _missing, _menv = cfg._apply_vault_creds(s, {
            "LINEAR_OAUTH_ACCESS_TOKEN": "Bearer XYZ",
        })
        assert ok is True
        assert out["headers"]["Authorization"] == "Bearer XYZ"

    def test_oauth_missing_access_token_skips(self):
        s = {"name": "linear", "auth_type": "oauth",
             "transport": "streamable_http", "url": "https://x"}
        out, ok, missing, _menv = cfg._apply_vault_creds(s, {
            # Only the optional keys are present — required is missing.
            "LINEAR_OAUTH_REFRESH_TOKEN": "RT",
            "LINEAR_OAUTH_EXPIRES_AT": "1000",
        })
        assert ok is False
        assert missing == ["LINEAR_OAUTH_ACCESS_TOKEN"]

    def test_oauth_missing_optional_keys_still_ok(self):
        # We must NOT skip the server just because refresh_token / expires_at
        # are absent — some issuers don't return them.
        s = {"name": "linear", "auth_type": "oauth",
             "transport": "streamable_http", "url": "https://x"}
        out, ok, missing, _menv = cfg._apply_vault_creds(s, {
            "LINEAR_OAUTH_ACCESS_TOKEN": "AT",
        })
        assert ok is True
        assert missing == []
        assert out["headers"]["Authorization"] == "Bearer AT"

    def test_other_auth_types_still_use_full_required_list(self):
        # Regression: changing the required-key logic must not soften
        # `bearer`/`api_key`/`basic` — they remain strict on all keys.
        s = {"name": "linear", "auth_type": "api_key",
             "transport": "sse", "url": "https://x"}
        out, ok, missing, _menv = cfg._apply_vault_creds(s, {
            "LINEAR_HEADER_NAME": "X-Token",
            # HEADER_VALUE missing
        })
        assert ok is False
        assert "LINEAR_HEADER_VALUE" in missing


# ===========================================================================
# _apply_auth — oauth admin-probe path
# ===========================================================================


@pytest.mark.unit
class TestApplyAuthOauth:

    def test_oauth_admin_probe_does_not_bake_tokens(self):
        # _apply_auth is used by Test Connection on the form. For OAuth we
        # return the server untouched — admin probe validates discovery,
        # not per-role consent.
        s = {"name": "linear", "auth_type": "oauth",
             "transport": "streamable_http", "url": "https://x",
             "headers": {"X-Custom": "v"}}
        # Even if some stray creds dict is passed, we ignore.
        out = cfg._apply_auth(s, {"bearer_token": "should_be_ignored"})
        assert "Authorization" not in (out.get("headers") or {})

    def test_bearer_admin_probe_still_works(self):
        # Regression: the existing bearer path must keep injecting.
        s = {"name": "linear", "auth_type": "bearer",
             "transport": "sse", "url": "https://x"}
        out = cfg._apply_auth(s, {"bearer_token": "AT"})
        assert out["headers"]["Authorization"] == "Bearer AT"


# ===========================================================================
# _server_oauth_expires_at
# ===========================================================================


@pytest.mark.unit
class TestServerOauthExpiresAt:

    def test_reads_int_from_vault(self):
        s = {"name": "linear", "auth_type": "oauth"}
        assert cfg._server_oauth_expires_at(s, {
            "LINEAR_OAUTH_EXPIRES_AT": "1700000000",
        }) == 1700000000

    def test_non_oauth_returns_none(self):
        s = {"name": "linear", "auth_type": "bearer"}
        assert cfg._server_oauth_expires_at(s, {
            "LINEAR_OAUTH_EXPIRES_AT": "1700000000",
        }) is None

    def test_missing_returns_none(self):
        s = {"name": "linear", "auth_type": "oauth"}
        assert cfg._server_oauth_expires_at(s, {}) is None

    def test_garbage_returns_none(self):
        s = {"name": "linear", "auth_type": "oauth"}
        assert cfg._server_oauth_expires_at(s, {
            "LINEAR_OAUTH_EXPIRES_AT": "not-an-int",
        }) is None


# ===========================================================================
# _refresh_oauth_tokens_if_expiring
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestRefreshOauthTokensIfExpiring:

    def test_no_oauth_servers_short_circuits(self):
        """Bearer-only servers should NOT trigger any HTTP."""
        servers = [{"name": "linear", "auth_type": "bearer"}]
        with mock.patch.dict(os.environ, {
            "DAGKNOWS_APP_SECRET_KEY": "TOK",
        }):
            with mock.patch("requests.post") as p:
                refreshed, err = cfg._refresh_oauth_tokens_if_expiring(
                    servers, {}, {"org": "acme"}, "acme",
                )
        assert refreshed is False
        assert err is None
        p.assert_not_called()

    def test_oauth_not_expiring_short_circuits(self):
        servers = [{"name": "linear", "auth_type": "oauth"}]
        # Expires far in the future.
        vault = {"LINEAR_OAUTH_EXPIRES_AT": str(2_000_000_000)}
        with mock.patch.dict(os.environ, {
            "DAGKNOWS_APP_SECRET_KEY": "TOK",
        }):
            with mock.patch("requests.post") as p:
                refreshed, err = cfg._refresh_oauth_tokens_if_expiring(
                    servers, vault, {"org": "acme"}, "acme",
                    window_sec=60,
                )
        assert refreshed is False
        p.assert_not_called()

    def test_no_service_token_skips_with_warning(self):
        import time as _time
        servers = [{"name": "linear", "auth_type": "oauth"}]
        vault = {"LINEAR_OAUTH_EXPIRES_AT": str(int(_time.time()) - 10)}
        # Clear both env keys.
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("requests.post") as p:
                refreshed, err = cfg._refresh_oauth_tokens_if_expiring(
                    servers, vault, {"org": "acme"}, "acme",
                )
        assert refreshed is False
        assert err == "service token unconfigured"
        p.assert_not_called()

    def test_calls_refresh_expiring_with_window(self):
        import time as _time
        servers = [{"name": "linear", "auth_type": "oauth"}]
        vault = {"LINEAR_OAUTH_EXPIRES_AT": str(int(_time.time()) - 10)}
        fake_resp = mock.MagicMock(status_code=200)
        fake_resp.json.return_value = {"refreshed": 1}
        with mock.patch.dict(os.environ, {
            "DAGKNOWS_APP_SECRET_KEY": "SECRET",
            "DAGKNOWS_REQROUTER_URL": "http://req-router:8888",
        }):
            with mock.patch("requests.post",
                            return_value=fake_resp) as p:
                refreshed, err = cfg._refresh_oauth_tokens_if_expiring(
                    servers, vault, {"org": "acme", "uid": "u1"},
                    "acme", window_sec=60,
                )
        assert refreshed is True
        assert err is None
        assert p.call_count == 1
        kwargs = p.call_args.kwargs
        assert "/api/v1/mcp/oauth/refreshExpiring" in p.call_args.args[0]
        body = kwargs["json"]
        # Service token must be passed under BOTH api_key and secret so the
        # CSRF middleware exempts the request via the `secret` path.
        assert body["api_key"] == "SECRET"
        assert body["secret"] == "SECRET"
        assert body["within_seconds"] == 60
        assert body["user_info"]["org"] == "acme"
        assert body["user_info"]["role"] == "Supremo"

    def test_transport_error_returns_false_with_reason(self):
        import time as _time
        servers = [{"name": "linear", "auth_type": "oauth"}]
        vault = {"LINEAR_OAUTH_EXPIRES_AT": str(int(_time.time()) - 10)}
        with mock.patch.dict(os.environ, {
            "DAGKNOWS_APP_SECRET_KEY": "TOK",
        }):
            with mock.patch("requests.post",
                            side_effect=Exception("connection refused")):
                refreshed, err = cfg._refresh_oauth_tokens_if_expiring(
                    servers, vault, {"org": "acme"}, "acme",
                )
        assert refreshed is False
        assert "connection refused" in err

    def test_zero_refreshed_does_not_trigger_revaultread(self):
        import time as _time
        servers = [{"name": "linear", "auth_type": "oauth"}]
        vault = {"LINEAR_OAUTH_EXPIRES_AT": str(int(_time.time()) - 10)}
        fake_resp = mock.MagicMock(status_code=200)
        fake_resp.json.return_value = {"refreshed": 0}
        with mock.patch.dict(os.environ, {"DAGKNOWS_APP_SECRET_KEY": "T"}):
            with mock.patch("requests.post", return_value=fake_resp):
                refreshed, err = cfg._refresh_oauth_tokens_if_expiring(
                    servers, vault, {"org": "acme"}, "acme",
                )
        # No refresh happened on the issuer side — caller should NOT re-read
        # vault (nothing changed).
        assert refreshed is False
        assert err is None


# ===========================================================================
# MCPConnectionPool.invalidate
# ===========================================================================


@pytest.mark.unit
class TestPoolInvalidate:

    def _pool_with_entry(self, server_id, role):
        """Build a pool with a fake _PooledSession at the (sid, role) key,
        bypassing the real connect path (which needs an event loop)."""
        pool_mod = sys.modules.get("mcp_pool_under_test") or _load_module(
            "mcp_pool_under_test",
            os.path.join(_V6_DIR, "mcp_pool.py"),
        )
        # Get a fresh pool — singleton would persist across tests; we set
        # _instance to None to force a new one. Tests run in-process so this
        # is fine.
        pool_mod.MCPConnectionPool._instance = None
        with mock.patch.object(pool_mod, "MCPEventLoop") as ml:
            ml.return_value = mock.MagicMock()
            pool = pool_mod.MCPConnectionPool.get()
        # Inject a fake entry.
        entry = mock.MagicMock()
        entry.borrow_count = 0
        entry.server_name = "linear"
        entry.exit_stack = mock.MagicMock()
        key = pool_mod._pool_key(server_id, role)
        pool._sessions[key] = entry
        return pool, pool_mod, key, entry

    def test_invalidate_evicts_existing_entry(self):
        pool, mod, key, entry = self._pool_with_entry("sid", "Admin")
        # Patch _close_entry so we don't try to close real loops.
        with mock.patch.object(pool, "_close_entry"):
            ok = pool.invalidate("sid", "Admin")
        assert ok is True
        assert key not in pool._sessions

    def test_invalidate_missing_returns_false(self):
        pool, mod, _, _ = self._pool_with_entry("sid", "Admin")
        ok = pool.invalidate("does-not-exist", "Admin")
        assert ok is False

    def test_invalidate_empty_server_id_returns_false(self):
        pool, _, _, _ = self._pool_with_entry("sid", "Admin")
        assert pool.invalidate("", "Admin") is False

    def test_invalidate_borrowed_entry_drains(self):
        # An in-use entry must move to _draining rather than close immediately.
        pool, mod, key, entry = self._pool_with_entry("sid", "Admin")
        entry.borrow_count = 1
        with mock.patch.object(pool, "_close_entry") as close_mock:
            pool.invalidate("sid", "Admin")
        assert key not in pool._sessions
        assert entry in pool._draining
        close_mock.assert_not_called()  # close happens on release, not here


# ===========================================================================
# _parse_resource_metadata_url
# ===========================================================================


@pytest.mark.unit
@pytest.mark.security
class TestParseResourceMetadataUrl:

    def setup_method(self):
        mc = sys.modules.get("mcp_client_under_test") or _load_module(
            "mcp_client_under_test",
            os.path.join(_TOOLS_DIR, "mcp_client.py"),
        )
        self.parse = mc.MCPToolAdapter._parse_resource_metadata_url

    def test_quoted_value(self):
        wa = 'Bearer realm="api", resource_metadata="https://mcp.linear.app/.well-known/oauth-protected-resource"'
        assert self.parse(wa) == \
            "https://mcp.linear.app/.well-known/oauth-protected-resource"

    def test_unquoted_value(self):
        wa = 'Bearer resource_metadata=https://x/y'
        assert self.parse(wa) == "https://x/y"

    def test_absent_returns_empty(self):
        wa = 'Bearer realm="api", error="invalid_token"'
        assert self.parse(wa) == ""

    def test_empty_input(self):
        assert self.parse("") == ""
        assert self.parse(None) == ""

    def test_value_with_trailing_param(self):
        # The unquoted regex stops at whitespace/comma/semicolon.
        wa = 'Bearer resource_metadata=https://x/y, error="x"'
        assert self.parse(wa) == "https://x/y"
