"""The sign-in flow, from the outside.

The verifier is tested separately against forgeries. These are about the
wiring around it: that a cookie cannot be read by script, that a visitor
cannot reach tenant routes, that a wrong password and an unknown email
are indistinguishable, and that the app still opens when there is no
Supabase at all.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from web import session


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    webapp._auth = None
    webapp._verifier = None
    yield TestClient(webapp.app)
    webapp._auth = None
    webapp._verifier = None


class FakeAuth(session.Auth):
    """Stands in for Supabase Auth. Accepts one account."""

    def __init__(self, **kw):
        super().__init__("https://example.supabase.co", "anon-key")
        self.signed_out = []

    def sign_in(self, email, password):
        if (email, password) != ("real@example.com", "correct-horse"):
            raise session.SignInFailed("rejected")
        return session.Tokens("access-token", "refresh-token", 3600)

    def sign_up(self, email, password):
        return session.Tokens("new-token", "new-refresh", 3600)

    def sign_out(self, token):
        self.signed_out.append(token)


@pytest.fixture
def signed_in(client, monkeypatch):
    """A client holding a real session cookie."""
    fake = FakeAuth()
    monkeypatch.setattr(webapp, "auth", lambda: fake)
    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {
            "verify": staticmethod(lambda t: __import__(
                "engine.supabase_auth", fromlist=["Claims"]).Claims(
                    user_id="33333333-3333-3333-3333-333333333333",
                    email="real@example.com",
                    expires_at=int(time.time()) + 3600))
        })())
    r = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "correct-horse"})
    assert r.status_code == 200
    return client, fake


# ------------------------------------------------------------ the cookie

def test_the_session_cookie_cannot_be_read_by_script(client, monkeypatch):
    """HttpOnly is the whole reason the token lives on the server.

    Without it a single injected script reads a working token for a
    business's inventory and cash position and posts it anywhere.
    """
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "correct-horse"})
    assert r.status_code == 200
    raw = r.headers.get_list("set-cookie")
    access = [c for c in raw if c.startswith(f"{session.ACCESS}=")]
    assert access, raw
    assert "HttpOnly" in access[0]
    assert "SameSite=lax" in access[0].replace("samesite", "SameSite")


def test_no_token_ever_reaches_the_response_body(client, monkeypatch):
    """Not in the JSON either -- a body is readable by script, so putting
    the token there would undo the cookie."""
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "correct-horse"})
    assert "access-token" not in r.text
    assert "refresh-token" not in r.text


def test_cookies_are_marked_secure_in_production(monkeypatch):
    monkeypatch.setenv("LANDED_ENV", "production")
    assert session.secure_cookies() is True
    monkeypatch.setenv("LANDED_ENV", "")
    assert session.secure_cookies() is False


# ------------------------------------------------------------ the answers

def test_a_wrong_password_and_an_unknown_email_read_identically(
    client, monkeypatch
):
    """Otherwise the form is an oracle for whether an address has an
    account here, one request at a time."""
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    a = client.post("/auth/sign-in", json={"email": "real@example.com",
                                           "password": "wrong"})
    b = client.post("/auth/sign-in", json={"email": "nobody@example.com",
                                           "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_a_short_password_is_refused_before_the_round_trip(client, monkeypatch):
    called = []

    class Watch(FakeAuth):
        def sign_up(self, email, password):
            called.append(email)
            return super().sign_up(email, password)

    monkeypatch.setattr(webapp, "auth", lambda: Watch())
    r = client.post("/auth/sign-up", json={"email": "a@b.com", "password": "short"})
    assert r.status_code == 400
    assert not called


# ------------------------------------------------------------- the guard

def test_a_visitor_is_sent_to_the_gate_not_shown_the_shell(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sign-in"


def test_a_visitor_cannot_create_an_organisation(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/api/organisation", json={"name": "Acme"})
    assert r.status_code == 401


def test_a_tampered_cookie_is_not_a_session(client, monkeypatch):
    """The cookie holds a real JWT, so an attacker editing it has to
    forge a signature. The verifier is what notices; this proves the
    route actually consults it rather than reading the cookie."""
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    client.cookies.set(session.ACCESS, "made.up.token")
    r = client.get("/api/me")
    assert r.json() == {"signed_in": False}
    r = client.post("/api/organisation", json={"name": "Acme"})
    assert r.status_code == 401


def test_signing_in_then_out_leaves_no_session(signed_in):
    client, fake = signed_in
    assert client.get("/api/me").json()["signed_in"] is True
    client.post("/auth/sign-out")
    assert fake.signed_out == ["access-token"]
    assert client.get("/api/me").json()["signed_in"] is False


def test_the_gate_redirects_someone_already_signed_in(signed_in):
    client, _ = signed_in
    r = client.get("/sign-in", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ------------------------------------------------- before Supabase exists

def test_the_app_still_opens_with_no_auth_configured(monkeypatch):
    """So the thing can be looked at and demonstrated before anyone has
    created a project."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    webapp._auth = None
    webapp._verifier = None
    c = TestClient(webapp.app)
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/api/state").status_code == 200
    webapp._auth = None


def test_sign_in_says_so_when_auth_is_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    webapp._auth = None
    c = TestClient(webapp.app)
    r = c.post("/auth/sign-in", json={"email": "a@b.com", "password": "x" * 12})
    assert r.status_code == 503
    webapp._auth = None


# ------------------------------------------------- saying so when it is off

def test_health_auth_names_the_missing_variable(monkeypatch):
    """The failure this reports is a silent one: with no anon key the
    app does not break, it drops the gate and serves sample data, which
    looks exactly like a build where login was never written."""
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    webapp._auth = None
    d = TestClient(webapp.app).get("/health/auth").json()
    assert d["sign_in_enabled"] is False
    assert d["missing"] == ["SUPABASE_ANON_KEY"]
    assert "SUPABASE_ANON_KEY" in d["detail"]
    webapp._auth = None


def test_health_auth_never_reports_a_key_value(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "super-secret-anon-value")
    webapp._auth = None
    r = TestClient(webapp.app).get("/health/auth")
    assert r.json()["sign_in_enabled"] is True
    assert "super-secret-anon-value" not in r.text
    webapp._auth = None


def test_config_tells_the_page_whether_to_offer_a_login(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    webapp._auth = None
    c = TestClient(webapp.app)
    assert c.get("/api/config").json()["auth_configured"] is False
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    assert c.get("/api/config").json()["auth_configured"] is True
    webapp._auth = None


def test_the_version_is_reported_so_a_stale_deploy_is_visible(monkeypatch):
    """A deploy that fails leaves the previous image serving. Everything
    answers normally and the new feature is simply absent, which reads
    as a bug in the feature rather than a deploy that never landed.
    """
    r = TestClient(webapp.app).get("/health")
    assert r.json()["version"] == webapp.__version__
    assert TestClient(webapp.app).get("/health/auth").json()["version"] == \
        webapp.__version__
