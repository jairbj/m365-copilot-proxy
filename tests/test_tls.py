"""Where TLS trust comes from, and what a certificate failure says."""

from __future__ import annotations

import ssl

import pytest

from m365_copilot_proxy import tls
from m365_copilot_proxy.auth.login import browser_launch_kwargs
from m365_copilot_proxy.config import get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ("M365_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("M365_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "corp.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\nnot a real cert\n-----END CERTIFICATE-----\n")
    return path


class TestCaBundleResolution:
    def test_nothing_configured_means_default_trust(self):
        assert tls.ca_bundle() is None

    def test_our_own_setting_is_used(self, monkeypatch, bundle):
        monkeypatch.setenv("M365_CA_BUNDLE", str(bundle))
        get_settings.cache_clear()
        assert tls.ca_bundle() == str(bundle)

    @pytest.mark.parametrize("name", ["REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"])
    def test_standard_variables_are_honoured(self, monkeypatch, bundle, name):
        # Anyone already working behind a corporate proxy has these exported.
        monkeypatch.setenv(name, str(bundle))
        assert tls.ca_bundle() == str(bundle)

    def test_our_setting_wins_over_the_standard_ones(self, monkeypatch, tmp_path, bundle):
        other = tmp_path / "other.pem"
        other.write_text("x")
        monkeypatch.setenv("M365_CA_BUNDLE", str(bundle))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(other))
        get_settings.cache_clear()
        assert tls.ca_bundle() == str(bundle)

    def test_a_path_that_does_not_exist_is_ignored(self, monkeypatch, tmp_path):
        # Silently trusting a typo'd path would be worse than falling back.
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
        assert tls.ca_bundle() is None


class TestClientArguments:
    def test_without_a_bundle_clients_keep_their_defaults(self):
        assert tls.httpx_verify() is True
        assert tls.ssl_context() is None

    def test_with_a_bundle_clients_are_pointed_at_it(self, monkeypatch, bundle):
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        assert tls.httpx_verify() == str(bundle)
        # A bad PEM is what proves the context really loads the file.
        with pytest.raises(ssl.SSLError):
            tls.ssl_context()


class TestWebSocketSslKwargs:
    """`websockets` refuses an ssl argument on ws:// AND an explicit ssl=None on
    wss://, so the only thing that works for both is omitting the key."""

    def test_a_plain_ws_url_gets_no_ssl_argument(self, monkeypatch, bundle):
        assert tls.websocket_ssl_kwargs("ws://127.0.0.1:1234/chat") == {}
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        assert tls.websocket_ssl_kwargs("ws://127.0.0.1:1234/chat") == {}

    def test_wss_without_a_bundle_omits_the_key_entirely(self):
        # Not {"ssl": None} — that is rejected just as hard as passing one on ws://.
        assert tls.websocket_ssl_kwargs("wss://substrate.office.com/x") == {}

    def test_wss_with_a_bundle_passes_the_context(self, monkeypatch):
        import certifi

        monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())
        kwargs = tls.websocket_ssl_kwargs("wss://substrate.office.com/x")
        assert isinstance(kwargs["ssl"], ssl.SSLContext)


class TestExplainSslError:
    def test_a_certificate_failure_is_explained(self):
        exc = ssl.SSLCertVerificationError("certificate verify failed")
        message = tls.explain_ssl_error(exc, "login.microsoftonline.com")
        assert message is not None
        assert "login.microsoftonline.com" in message
        assert "M365_CA_BUNDLE" in message

    def test_a_wrapped_failure_is_still_recognised(self):
        # Libraries bury the real cause several layers down.
        inner = ssl.SSLCertVerificationError("certificate verify failed")
        middle = OSError("connection broken")
        middle.__cause__ = inner
        outer = RuntimeError("max retries exceeded")
        outer.__cause__ = middle
        assert tls.explain_ssl_error(outer) is not None

    def test_an_unrelated_failure_is_left_alone(self):
        # Dressing up a different problem as a certificate one sends the user
        # chasing the wrong fix.
        assert tls.explain_ssl_error(TimeoutError("too slow")) is None
        assert tls.explain_ssl_error(ValueError("nope")) is None

    def test_the_doctor_does_not_tell_you_to_run_the_doctor(self):
        exc = ssl.SSLCertVerificationError("certificate verify failed")
        message = tls.explain_ssl_error(exc, suggest_doctor=False)
        assert message is not None
        assert "doctor" not in message


class TestBrowserTlsEscapeHatch:
    def test_off_by_default(self):
        assert "ignore_https_errors" not in browser_launch_kwargs()

    def test_opt_in_only(self, monkeypatch):
        monkeypatch.setenv("M365_BROWSER_IGNORE_TLS_ERRORS", "1")
        get_settings.cache_clear()
        assert browser_launch_kwargs()["ignore_https_errors"] is True
