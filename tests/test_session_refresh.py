"""Renewing the access token before it expires.

Supabase access tokens last an hour. Auth.refresh and REFRESH_WITHIN
were both written and neither was ever called, so an hour into any
session every request started failing: the access cookie was there, the
verifier rejected it as expired, `who` returned None and `require`
raised 401. The refresh cookie sat beside it, valid for thirty days,
unused. What a person saw was the application going dead while they
were working in it.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import web.app as webapp
from engine.supabase_auth import AuthError, Claims
from web import session


def cookies_set(response) -> dict:
    """What the response tells the browser to store.

    Asserted on instead of the test client's jar: delete_cookie sends
    an expired cookie rather than removing one, so httpx ends up
    holding two of the same name and refuses to say which is current.
    A browser applies them in order and keeps neither.
    """
    out = {}
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        value = rest.split(";")[0]
        expired = ("Max-Age=0" in header
                   or "01 Jan 1970" in header
                   or value == '""' or value == "")
        out[name.strip()] = None if expired else value
    return out


def _exp(access: str) -> int | None:
    """The `exp` in a token, for the stand-in verifier below only."""
    try:
        body = access.split(".")[1]
        body += "=" * (-len(body) % 4)
        return int(json.loads(base64.urlsafe_b64decode(body))["exp"])
    except Exception:                                    # noqa: BLE001
        return None


def token(exp: float) -> str:
    """A JWT-shaped string whose `exp` says what we want it to say.

    Unsigned: the point of these tests is what the application does
    about expiry, and nothing here is authorised on the strength of an
    unverified claim.
    """
    body = base64.urlsafe_b64encode(
        json.dumps({"sub": "u-1", "exp": int(exp)}).encode()
    ).decode().rstrip("=")
    return f"header.{body}.signature"


# ------------------------------------- the decision, on its own
#
# It takes the expiry from claims the verifier has already checked
# against the project's signing keys. An earlier version read `exp` out
# of the token itself without verifying it, which put a second, weaker
# opinion about expiry beside the verifier's -- and treated anything it
# could not parse as due for renewal, so a session the verifier was
# perfectly happy with got a round trip to Supabase on every request
# and was cleared when that failed. Forty tests caught it.


def test_a_token_with_hours_left_is_left_alone():
    assert session.needs_refresh(int(time.time()) + 3600) is False


def test_a_token_about_to_expire_is_renewed_early():
    """Renewed before it goes, not after: a request that starts valid
    and lands expired is the one nobody can reproduce."""
    assert session.needs_refresh(int(time.time()) + 10) is True


def test_a_token_the_verifier_rejected_is_renewed():
    """None is what the verifier says about an expired, malformed or
    wrongly signed token. All three mean the same thing here."""
    assert session.needs_refresh(None) is True


def test_the_boundary_is_the_window_not_the_expiry():
    now = 1_000_000.0
    assert session.needs_refresh(int(now) + session.REFRESH_WITHIN,
                                 now=now) is True
    assert session.needs_refresh(int(now) + session.REFRESH_WITHIN + 1,
                                 now=now) is False


# ------------------------------------- through the application


class Spy(session.Auth):
    """An auth server that renews once, or refuses."""

    def __init__(self, *, accept=True):
        super().__init__("https://example.supabase.co", "anon-key")
        self.accept = accept
        self.calls = 0

    def refresh(self, refresh_token):
        self.calls += 1
        if not self.accept:
            raise session.SignInFailed("refresh rejected")
        return session.Tokens(token(time.time() + 3600), "refresh-2", 3600)

    def sign_in(self, email, password):
        # Overridden so this test file never reaches the network.
        return session.Tokens(token(time.time() + 3600), "refresh-1", 3600)


@pytest.fixture
def app(monkeypatch):
    """The real application, with the verifier honouring `exp`.

    A verifier that accepted anything would make every one of these
    tests pass whatever the middleware did.
    """
    def verify(t):
        """Honours the `exp` in the token, and rejects anything else.

        A verifier that accepted whatever it was given would make every
        test below pass regardless of what the middleware did.
        """
        exp = _exp(t)
        if exp is None or exp <= time.time():
            raise AuthError("expired")
        return Claims(user_id="u-1", email="a@b.test", expires_at=exp)

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(verify)})())
    monkeypatch.setenv("LANDED_ENV", "dev")
    return webapp


def test_a_session_outlives_its_access_token(app, monkeypatch):
    """The acceptance criterion. An hour in, the request still works."""
    spy = Spy()
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() - 60))
    client.cookies.set(session.REFRESH, "refresh-1")

    r = client.get("/api/home")
    assert r.status_code != 401, "an expired token was not renewed"
    assert spy.calls == 1
    # The browser is given the new pair, so the next request is cheap.
    given = cookies_set(r)
    assert given[session.REFRESH] == "refresh-2"
    assert given[session.ACCESS] not in (None, "")


def test_a_healthy_session_is_not_refreshed_on_every_request(app,
                                                             monkeypatch):
    """A refresh is a round trip to Supabase. Doing it per request
    would put one on the critical path of every page."""
    spy = Spy()
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() + 3600))
    client.cookies.set(session.REFRESH, "refresh-1")

    for _ in range(3):
        client.get("/api/home")
    assert spy.calls == 0


def test_a_rejected_refresh_clears_the_session(app, monkeypatch):
    """So the next request arrives as a visitor and gets the sign-in
    screen, rather than carrying a cookie that cannot work."""
    spy = Spy(accept=False)
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() - 60))
    client.cookies.set(session.REFRESH, "stale")

    r = client.get("/api/home")
    assert r.status_code == 401
    given = cookies_set(r)
    assert given[session.ACCESS] is None
    assert given[session.REFRESH] is None


def test_there_is_no_endless_refresh_loop(app, monkeypatch):
    """The guard is that a failed refresh leaves nothing to retry with
    -- which cannot be got wrong the way a counter can."""
    spy = Spy(accept=False)
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() - 60))
    client.cookies.set(session.REFRESH, "stale")

    first = client.get("/api/home")
    assert spy.calls == 1
    # The response clears both cookies, so a browser sends neither next
    # time. The jar in this client keeps the expired copies alongside
    # the live ones, so the browser's behaviour is simulated here by
    # dropping them.
    assert cookies_set(first)[session.REFRESH] is None
    client.cookies.clear()

    for _ in range(3):
        client.get("/api/home")
    assert spy.calls == 1, "it kept trying with no refresh token at all"


def test_a_visitor_with_no_cookies_costs_no_round_trip(app, monkeypatch):
    spy = Spy()
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    assert client.get("/api/home").status_code == 401
    assert spy.calls == 0


def test_supabase_being_unreachable_does_not_wedge_the_session(app,
                                                               monkeypatch):
    """A refresh that raises anything at all is treated as a refusal.
    Letting it propagate would turn a blip at the auth server into a
    500 on a page that only needed to say 'sign in'."""
    class Broken(Spy):
        def refresh(self, refresh_token):
            self.calls += 1
            raise OSError("connection reset")

    spy = Broken()
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() - 60))
    client.cookies.set(session.REFRESH, "refresh-1")

    r = client.get("/api/home")
    assert r.status_code == 401
    assert spy.calls == 1


def test_health_and_static_never_look_at_the_session(app, monkeypatch):
    """/health is polled every few seconds by the platform."""
    spy = Spy()
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() - 60))
    client.cookies.set(session.REFRESH, "refresh-1")

    assert client.get("/health").status_code == 200
    assert spy.calls == 0


def test_signing_in_is_not_intercepted(app, monkeypatch):
    """Nothing to renew: the request that creates the session must not
    be made to depend on one existing."""
    spy = Spy()
    monkeypatch.setattr(app, "auth", lambda: spy)
    client = TestClient(app.app)
    client.cookies.set(session.ACCESS, token(time.time() - 60))
    client.cookies.set(session.REFRESH, "refresh-1")

    client.post("/auth/sign-in", json={"email": "a@b.test", "password": "x"})
    assert spy.calls == 0


def test_the_route_sees_the_renewed_token_not_the_expired_one(app,
                                                              monkeypatch):
    """The whole point of refreshing before the route rather than after.
    By the time a handler has run it has already returned its 401."""
    spy = Spy()
    monkeypatch.setattr(app, "auth", lambda: spy)
    seen = {}

    @app.app.get("/__whoami")
    def whoami(request: Request):   # annotated, or FastAPI reads it
                                    # as a query parameter and 422s
        seen["access"] = request.cookies.get(session.ACCESS)
        claims = app.who(request)
        seen["signed_in"] = claims is not None
        return {"ok": True}

    client = TestClient(app.app)
    expired = token(time.time() - 60)
    client.cookies.set(session.ACCESS, expired)
    client.cookies.set(session.REFRESH, "refresh-1")
    client.get("/__whoami")

    assert seen["access"] != expired, "the route read the expired token"
    assert seen["signed_in"] is True
