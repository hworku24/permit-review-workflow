"""The deployment sign-in gate.

Two states worth testing. Unset, the application behaves exactly as it always has, which is
what keeps a fresh clone running with no credentials (NFR-06). Set, nothing but the health
check and the stylesheet is reachable without signing in.
"""

from __future__ import annotations

import time

import pytest

from permitflow.config import get_settings
from permitflow.ui import gate
from permitflow.ui.deps import ACTOR_COOKIE


@pytest.fixture
def gated(monkeypatch):
    """Turn the gate on for one test, with a known passphrase and a stable secret."""
    monkeypatch.setenv("DEMO_PASSPHRASE", "rivermont-demo")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-a-real-one")
    get_settings.cache_clear()
    yield "rivermont-demo"
    get_settings.cache_clear()


class TestGateOff:
    def test_the_screens_are_reachable_with_no_passphrase_configured(self, api_client) -> None:
        api_client.cookies.set(ACTOR_COOKIE, "pvasquez")
        assert api_client.get("/ui/queue").status_code == 200

    def test_the_gate_page_redirects_away_when_it_is_not_in_use(self, api_client) -> None:
        response = api_client.get("/ui/gate", follow_redirects=False)
        assert response.status_code == 303


class TestGateOn:
    def test_a_screen_redirects_to_the_gate(self, api_client, gated) -> None:
        response = api_client.get("/ui/queue", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/gate"

    def test_the_api_gets_a_status_code_and_not_a_redirect(self, api_client, gated) -> None:
        """A machine caller has nowhere to follow a redirect to."""
        response = api_client.get("/queues/intake", headers={"X-Actor": "mcarrero"})
        assert response.status_code == 401

    def test_health_stays_open(self, api_client, gated) -> None:
        """The load balancer calls it, and it reports only that the database is readable."""
        assert api_client.get("/health").status_code == 200

    def test_the_stylesheet_stays_open_so_the_gate_page_is_legible(
        self, api_client, gated
    ) -> None:
        assert api_client.get("/ui/static/app.css").status_code == 200

    def test_a_wrong_passphrase_is_refused(self, api_client, gated) -> None:
        response = api_client.post(
            "/ui/gate", data={"passphrase": "not it"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "error=1" in response.headers["location"]
        assert gate.GATE_COOKIE not in response.cookies

    def test_the_right_passphrase_lets_you_in(self, api_client, gated) -> None:
        response = api_client.post(
            "/ui/gate", data={"passphrase": gated}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.cookies[gate.GATE_COOKIE]

        api_client.cookies.set(ACTOR_COOKIE, "pvasquez")
        assert api_client.get("/ui/queue").status_code == 200

    def test_the_cookie_is_httponly_and_secure(self, api_client, gated) -> None:
        response = api_client.post(
            "/ui/gate", data={"passphrase": gated}, follow_redirects=False
        )
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "Secure" in header


class TestToken:
    def test_a_tampered_token_is_refused(self, gated) -> None:
        token = gate.issue(int(time.time()) + 3600)
        payload, _, signature = token.partition(".")
        forged = f"{int(payload) + 86400}.{signature}"
        assert gate.valid(token)
        assert not gate.valid(forged)

    def test_an_expired_token_is_refused(self, gated) -> None:
        assert not gate.valid(gate.issue(int(time.time()) - 1))

    def test_rubbish_is_refused(self, gated) -> None:
        for value in (None, "", "no-dot", "abc.def"):
            assert not gate.valid(value)


class TestTheBareDomain:
    """Somebody typing the hostname is asking for a page, not calling an API.

    This was found by opening the deployed URL with no path: the root is not a /ui path, so
    the gate fell through to the machine-caller branch and answered a browser with plain
    text and a 401. That is a broken-looking front door on the one address worth sharing.
    """

    def test_the_root_sends_a_browser_to_the_sign_in(self, api_client, gated) -> None:
        response = api_client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/gate"

    def test_the_root_lands_on_the_picker_when_the_gate_is_off(self, api_client) -> None:
        response = api_client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/ui/"

    def test_a_machine_caller_still_gets_a_status_code(self, api_client, gated) -> None:
        """Curl and integrations have nowhere to follow a redirect to."""
        response = api_client.get("/queues/intake", headers={"Accept": "application/json"})
        assert response.status_code == 401

    def test_any_page_request_reaches_the_sign_in(self, api_client, gated) -> None:
        for path in ("/", "/docs", "/reports/sla-compliance"):
            response = api_client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
            assert response.status_code == 303, path
            assert response.headers["location"] == "/ui/gate", path
