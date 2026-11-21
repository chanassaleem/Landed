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
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    webapp._auth = None
    d = TestClient(webapp.app).get("/health/auth").json()
    assert d["sign_in_enabled"] is False
    assert d["missing"] == ["SUPABASE_PUBLISHABLE_KEY (or SUPABASE_ANON_KEY)"]
    assert "SUPABASE_PUBLISHABLE_KEY" in d["detail"]
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


# ------------------------------------------------------ the new key scheme

def test_either_key_name_switches_sign_in_on(monkeypatch):
    """Supabase is retiring the anon key for a publishable one.

    A project created today has only the new key, so reading just
    SUPABASE_ANON_KEY would leave sign-in off with no way to switch it
    on. An older project has only the old one. Both must work.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    for name in ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY"):
        for other in ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY"):
            monkeypatch.delenv(other, raising=False)
        monkeypatch.setenv(name, "sb_publishable_example")
        webapp._auth = None
        d = TestClient(webapp.app).get("/health/auth").json()
        assert d["sign_in_enabled"] is True, f"{name} was not read"
    webapp._auth = None


def test_the_publishable_key_wins_when_both_are_present(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "legacy-jwt")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_new")
    assert session.public_key() == "sb_publishable_new"


def test_what_is_missing_names_the_current_key_first(monkeypatch):
    """Sending someone to look for an anon key in a project that has
    none is a wasted trip through the dashboard."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    webapp._auth = None
    d = TestClient(webapp.app).get("/health/auth").json()
    assert d["missing"] == ["SUPABASE_PUBLISHABLE_KEY (or SUPABASE_ANON_KEY)"]
    webapp._auth = None


# ------------------------------------------------- the confirmation link

class ConfirmingAuth(FakeAuth):
    """Accepts one token hash, as Supabase would accept one link once."""

    def __init__(self, good="good-hash"):
        super().__init__()
        self.good, self.seen = good, []

    def verify_otp(self, token_hash, kind="signup"):
        self.seen.append((token_hash, kind))
        if token_hash != self.good:
            raise session.SignInFailed("link rejected")
        return session.Tokens("confirmed-token", "confirmed-refresh", 3600)


def test_a_good_confirmation_link_signs_the_person_straight_in(
    client, monkeypatch
):
    """They have just proved they own the address. Asking for the
    password again at that exact moment is a step for its own sake."""
    fake = ConfirmingAuth()
    monkeypatch.setattr(webapp, "auth", lambda: fake)
    r = client.get("/auth/confirm?token_hash=good-hash&type=signup",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert any(c.startswith(f"{session.ACCESS}=")
               for c in r.headers.get_list("set-cookie"))
    assert fake.seen == [("good-hash", "signup")]


def test_no_token_ever_appears_in_a_url(client, monkeypatch):
    """The whole reason for redeeming the link on the server.

    A session in a URL is kept: browser history, the referrer header,
    a screenshot in a bug report. The redirect after confirming must be
    a plain path.
    """
    monkeypatch.setattr(webapp, "auth", lambda: ConfirmingAuth())
    r = client.get("/auth/confirm?token_hash=good-hash&type=signup",
                   follow_redirects=False)
    assert r.headers["location"] == "/"
    assert "token" not in r.headers["location"]
    assert "confirmed-token" not in r.text


def test_a_used_or_expired_link_says_which(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: ConfirmingAuth())
    r = client.get("/auth/confirm?token_hash=stale&type=signup",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sign-in?problem=expired"


def test_an_unknown_link_type_is_not_passed_to_the_auth_server(
    client, monkeypatch
):
    """The value goes straight on to Supabase, so it is allow-listed
    rather than forwarded and reasoned about."""
    fake = ConfirmingAuth()
    monkeypatch.setattr(webapp, "auth", lambda: fake)
    r = client.get("/auth/confirm?token_hash=good-hash&type=whatever",
                   follow_redirects=False)
    assert r.headers["location"] == "/sign-in?problem=link"
    assert fake.seen == []


def test_a_link_with_no_token_is_not_a_crash(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: ConfirmingAuth())
    r = client.get("/auth/confirm", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sign-in?problem=link"


# ------------------------------------------------ the default-template door

def test_the_session_endpoint_refuses_a_token_it_cannot_verify(
    client, monkeypatch
):
    """This endpoint is public and takes a token from whoever posts one.

    If it trusted what it was given, anybody could hand it a string
    naming any user and be issued a working session cookie for them.
    The signature check is the entire security of this route.
    """
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    r = client.post("/auth/session",
                    json={"access_token": "made.up.token",
                          "refresh_token": "also-made-up"})
    assert r.status_code == 401
    assert not any(c.startswith(f"{session.ACCESS}=")
                   for c in r.headers.get_list("set-cookie"))


def test_a_verified_token_becomes_an_httponly_cookie(client, monkeypatch):
    import engine.supabase_auth as sa

    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    monkeypatch.setattr(webapp, "verifier", lambda: type("V", (), {
        "verify": staticmethod(lambda t: sa.Claims(
            user_id="55555555-5555-5555-5555-555555555555",
            email="d@example.com", expires_at=int(time.time()) + 3600))
    })())
    r = client.post("/auth/session",
                    json={"access_token": "real", "refresh_token": "r"})
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith(f"{session.ACCESS}=") and "HttpOnly" in c
               for c in cookies)


def test_sign_up_sends_the_redirect_as_a_query_parameter(client, monkeypatch):
    """GoTrue ignores redirect_to in the JSON body and falls back to the
    Site URL -- which defaults to localhost, so the only symptom is a
    confirmation email pointing at a machine that is not running."""
    seen = {}

    class Watch(FakeAuth):
        def sign_up(self, email, password, redirect_to=""):
            seen["redirect_to"] = redirect_to
            return None

    monkeypatch.setattr(webapp, "auth", lambda: Watch())
    monkeypatch.setenv("LANDED_BASE_URL", "https://landed.example.com")
    r = client.post("/auth/sign-up",
                    json={"email": "a@b.com", "password": "x" * 12})
    assert r.json()["confirm"] is True
    assert seen["redirect_to"] == "https://landed.example.com/auth/landing"


def test_the_redirect_url_carries_no_query_string_of_its_own(
    client, monkeypatch
):
    """GoTrue truncates a redirect_to at its first query parameter by
    the time it reaches the email, so the URL has to be a bare path."""
    seen = {}

    class Watch(FakeAuth):
        def sign_up(self, email, password, redirect_to=""):
            seen["redirect_to"] = redirect_to
            return None

    monkeypatch.setattr(webapp, "auth", lambda: Watch())
    client.post("/auth/sign-up", json={"email": "a@b.com", "password": "x" * 12})
    assert "?" not in seen["redirect_to"]


def test_the_base_url_is_https_behind_a_proxy(monkeypatch):
    """Render terminates TLS, so the request arrives as http. A
    redirect_to that does not match the allow-list exactly is discarded
    without a word, and http:// against an https:// entry does not
    match."""
    monkeypatch.delenv("LANDED_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://landed.onrender.com")
    c = TestClient(webapp.app)

    class Req:
        base_url = "http://landed.onrender.com/"

    assert webapp.base_url(Req()) == "https://landed.onrender.com"
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert webapp.base_url(Req()) == "https://landed.onrender.com"


# ---------------------------------------------- whose figures are on screen

def test_a_visitor_gets_the_sample_importer(client, monkeypatch):
    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
    d = client.get("/api/state").json()
    assert d["source"] == "Sample data"
    assert d["stock"]


def test_a_signed_in_person_never_gets_someone_elses_figures(
    signed_in, monkeypatch
):
    """The failure this prevents is the quiet one.

    Falling back to the sample importer when the database cannot be
    reached shows a signed-in customer a fictional company's healthy
    numbers, labelled as their dashboard, with nothing on screen saying
    otherwise. An error they can see is strictly better than figures
    they might believe.
    """
    client, _ = signed_in

    class Boom:
        def as_user(self, _uid):
            raise RuntimeError("no database")

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database", lambda: Boom())
    d = client.get("/api/state").json()
    assert d["source"] == "Error"
    assert d["stock"] == []
    assert "unreachable" in d["error"]


def test_an_empty_organisation_says_so_and_says_what_to_do(
    signed_in, monkeypatch
):
    from engine.db.snapshot import Snapshot

    client, _ = signed_in

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class DB:
        def as_user(self, _uid): return Conn()

    import engine.db.supabase as sup
    import engine.db.snapshot as snapmod
    monkeypatch.setattr(sup, "database", lambda: DB())
    monkeypatch.setattr(
        snapmod, "read",
        lambda conn, as_of, currency="PKR": Snapshot([], [], [], "PKR"))

    d = client.get("/api/state").json()
    assert d["source"] == "No data yet"
    assert d["stock"] == []
    assert sum(d["line"].values()) == 0
    # It has the same shape as a full payload, so the screen needs no
    # second code path and cannot drift from one.
    assert set(d) >= {"line", "stock", "shipments", "decisions", "branches"}
    assert len(d["decisions"]) == 1
    assert d["decisions"][0]["action"]
